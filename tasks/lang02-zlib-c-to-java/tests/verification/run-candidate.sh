#!/usr/bin/env bash
# =============================================================================
# Run one verification candidate against one tree.
#
# Invoked by swerefactor.verification.CandidateRunner, once per (candidate, tree),
# with cwd set to the tree and:
#
#   SRB_CANDIDATE       absolute path to the candidate's pytest file
#   SRB_CANDIDATE_NAME  its short name
#   SRB_TARGET          the tree to test  (a copy: safe to build in)
#   SRB_TARGET_TOKEN    an opaque per-run label for the same tree
#   SRB_WORK            scratch, shared across candidates and rounds
#
# The role does not reach the candidate: a candidate that reads the role meets
# every mechanical condition for a break while establishing nothing.
#
# and one positional argument, "original" or "submission", which is this script's
# to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# Both trees are configured, built and installed with the SAME published argv --
# the `shared` configuration from source-contract.json, which is the one
# instruction.md gives the agent and the one stage 2 grades first:
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
#         -DZLIB_BUILD_EXAMPLES=ON -DCMAKE_INSTALL_PREFIX=<prefix>
#
# ZLIB_BUILD_EXAMPLES is on because the two test drivers are behind it, and they
# are half of what a candidate can attack.
#
# One toolchain image serves both trees, which is why this is the only stage with
# a C compiler and a JDK in the same container.  That is not a loophole: stage 2
# runs under a shimmed cc that fails on any compile action and has already
# measured whether the submission needs one.  What this image adds is the ability
# to ask the C a question at grading time, which is the entire point of the stage
# -- a candidate's claim is empty unless the original demonstrably passes it.
#
# WHERE THIS DIFFERS FROM A SAME-LANGUAGE TASK
#
# The two trees do not install the same kind of artifact.  State A installs
# libz.so.1.3.1 and zlib.h; State B installs share/java/zlib-1.3.1.jar and no
# header, because that is the migration.  So the probe cannot be one program: it is
# probe.c compiled with cc against one install, and Probe.java compiled with javac
# against the other.
#
# This script picks by looking at what the install produced, not by reading the
# role it was passed.  Both are available to it and the role would be simpler, but
# then a submission that installed a jar under a different layout would be probed
# as though it had installed one, and the failure would arrive as an unreadable
# javac error rather than as "the install inventory is wrong" -- which stage 2
# already measures and says clearly.  Detecting the artifact keeps this script
# honest about what it found.
#
# The result is wrapped in a launcher at $STATE/probe-launch, and srbzlib runs
# that.  The candidate sees one command that takes a corpus directory and a
# scratch directory, whichever tree it is on.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
# Passed as this script's last argument, never in the environment: a child process
# inherits the environment automatically and inherits argv never, so the role
# cannot reach the candidate by being forgotten about.
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
JAVAC="$JAVA_HOME/bin/javac"
JAVA="$JAVA_HOME/bin/java"
PROBE_SRC=/tests/verification/lib/probe
MODULE_NAME=org.zlib
JAVA_RELEASE=17

# Keyed by the token, not the role: the candidate is handed $PREFIX, and a path
# with "original" in it would answer the question it is meant to answer by
# observing behaviour.
STATE="$WORK/$TOKEN"
PREFIX="$STATE/install"
BUILD="$STATE/build"
LAUNCH="$STATE/probe-launch"
mkdir -p "$STATE"

case "$ROLE" in
    original|submission) ;;
    *) echo "unknown target role" >&2; exit 64 ;;
esac

# Belt and braces.  The harness does not set these, but this script may be run by
# hand or from a shell that has them, and an inherited value is inherited all the
# way down to the candidate.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

log() { printf '[%s] %s\n' "$ROLE" "$*" >&2; }

# --- 1. Build and install the tree, once per tree per stage --------------------
# Six rounds share this.  Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on cmake rather than on finding defects, and the tree
# does not change between them.  The marker file records a completed install *and
# a built probe*: a prefix that exists because a build was interrupted must not be
# reused, and neither must a launcher whose javac step failed.
if [ ! -f "$STATE/.ready" ]; then
    log "building, installing and probing the tree (first candidate pays for this)"
    rm -rf "$BUILD" "$PREFIX" "$LAUNCH" "$STATE/probe-classes" "$STATE/probe-bin"

    # A submission may arrive carrying its own build output.  A stale build/
    # directory with a CMakeCache.txt pointing at another prefix makes the
    # configure step below reuse decisions nobody here made, and a checked-in
    # target/ could hold class files from a build this container never ran -- the
    # source contract forbids .class in the source tree and exempts the build
    # directory, so a tree that has both is exactly the case this removes.
    find "$TARGET" -maxdepth 2 -depth \
        \( -name build -o -name target -o -name '_build' \
           -o -name 'cmake-build*' \) \
        -type d -prune -exec rm -rf {} + 2>/dev/null

    {
        cmake -S "$TARGET" -B "$BUILD" \
              -DCMAKE_BUILD_TYPE=Release \
              -DBUILD_SHARED_LIBS=ON \
              -DZLIB_BUILD_EXAMPLES=ON \
              -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        && cmake --build "$BUILD" --parallel 4 \
        && cmake --install "$BUILD"
    } >>"$STATE/build.log" 2>&1 || {
        log "the tree does not build; see $STATE/build.log"
        tail -30 "$STATE/build.log" >&2
        # A submission that will not build is stage 2's finding, not stage 3's.
        # 71 is in the fault range the adjudicator reads (sysexits 64-78), so the
        # candidate is invalid on whichever tree this happened to: nothing was
        # learned about the submission either way.  Requiring a PASS on the original
        # does not cover this on its own -- a submission that will not build passes
        # there, and the round would read the pair as a divergence.
        exit 71
    }

    # --- 2. Build the probe for whatever this tree installed -------------------
    # Checked in this order because the jar is the more specific finding: a tree
    # that installed both would be a submission that kept its C, which stage 1 and
    # stage 2 both judge, and probing its Java is the more informative half.
    JAR="$PREFIX/share/java/zlib.jar"
    if [ -e "$JAR" ]; then
        log "install carries a jar; compiling Probe.java against it"
        # --add-modules is required rather than decorative: Probe is in the unnamed
        # package, nothing `requires` org.zlib, and without it the module sits on
        # the path unresolved and every reference to it fails to compile.
        if ! "$JAVAC" --release "$JAVA_RELEASE" \
                -d "$STATE/probe-classes" \
                -p "$JAR" --add-modules "$MODULE_NAME" \
                "$PROBE_SRC/Probe.java" >>"$STATE/probe-build.log" 2>&1; then
            log "Probe.java does not compile against the installed jar"
            tail -30 "$STATE/probe-build.log" >&2
            # A downstream Java program that cannot compile against what the
            # submission installed is a real defect, and stage 2 grades it as its
            # own case.  Here it means no comparison is possible, so it reads as a
            # candidate that could not run.
            exit 73
        fi
        # The properties are the behavioural stage's, and every one of them is about
        # the comparison rather than about cheating.  probe.c formats its records
        # through printf under LC_ALL=C; Probe.java formats its through
        # String.format, which consults the default locale unless told otherwise,
        # and a locale with a comma decimal separator would make every numeric
        # record differ from the C half for a reason that is not the port.
        # java.library.path= is empty so System.loadLibrary("z") finds nothing.
        cat > "$LAUNCH" <<LAUNCHER
#!/bin/sh
exec "$JAVA" -Djava.library.path= -Duser.language=en -Duser.country=US \\
    -Dfile.encoding=UTF-8 -Duser.timezone=UTC \\
    -p "$JAR" --add-modules "$MODULE_NAME" \\
    -cp "$STATE/probe-classes" Probe "\$@"
LAUNCHER
    elif [ -e "$PREFIX/include/zlib.h" ]; then
        log "install carries a C header; compiling probe.c against it"
        # The same argv build.py's compile_probe() uses, including the rpath: only
        # the install tree's own include and lib directories, so a probe that
        # compiled because /usr/include/zlib.h was found instead would fail here
        # rather than silently grading the system library, and the -rpath is what
        # makes the result runnable without an LD_LIBRARY_PATH in the candidate's
        # environment.  There is no system zlib development package in this image,
        # but a submission that vendored one could otherwise answer with it.
        if ! /usr/bin/cc -std=c11 -O1 -Wall \
                -o "$STATE/probe-bin" "$PROBE_SRC/probe.c" \
                -I"$PREFIX/include" \
                -L"$PREFIX/lib" -lz -Wl,-rpath,"$PREFIX/lib" \
                >>"$STATE/probe-build.log" 2>&1; then
            log "probe.c does not compile against the installed library"
            tail -30 "$STATE/probe-build.log" >&2
            exit 73
        fi
        cat > "$LAUNCH" <<LAUNCHER
#!/bin/sh
exec "$STATE/probe-bin" "\$@"
LAUNCHER
    else
        log "the install produced neither share/java/zlib.jar nor include/zlib.h"
        log "install tree: $(find "$PREFIX" -maxdepth 3 2>/dev/null | head -30 | tr '\n' ' ')"
        exit 72
    fi
    chmod +x "$LAUNCH"

    # The probe answers `--list-keys` without touching a corpus, so this proves
    # the launcher starts and the library loads without asking it a question whose
    # answer differs between the trees.  A launcher that cannot do this would
    # otherwise fail inside every candidate, on both trees, and read as six
    # rounds of broken candidates.
    if ! "$LAUNCH" --list-keys >"$STATE/probe-keys.txt" 2>>"$STATE/probe-build.log"
    then
        log "the probe does not start; see $STATE/probe-build.log"
        tail -30 "$STATE/probe-build.log" >&2
        exit 74
    fi
    if ! [ -s "$STATE/probe-keys.txt" ]; then
        log "the probe started but --list-keys produced nothing"
        exit 74
    fi

    for driver in example minigzip; do
        [ -x "$BUILD/$driver" ] || log "note: the build produced no executable $driver"
    done

    touch "$STATE/.ready"
    log "installed into $PREFIX, probe launcher at $LAUNCH"
fi

# --- 3. Hand the candidate the launcher and the build ------------------------
# srbzlib.py is on PYTHONPATH and reads these.  It is the only import a candidate
# may make beyond the standard library, and it exists because the differential
# probe is the instrument this task is graded with: a candidate composing its own
# corpus and driving the shipped ProbeRunner is exercising the verifier's own
# execution path rather than a lookalike it wrote.
export SRB_PROBE_LAUNCH="$LAUNCH"
export SRB_PREFIX="$PREFIX"
export SRB_BUILD_DIR="$BUILD"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$STATE/candidates/${SRB_CANDIDATE_NAME:-candidate}"

# JAVA_HOME so a submission's driver wrapper can find a JVM by that route; PATH
# includes /usr/bin, where the image's `java` alternative is, so it can find one by
# the other.  Nothing beyond that: the wrapper is the submission's, and a driver
# that needs the harness to tell it where its own jar is would not run for anyone.
export JAVA_HOME

# A fresh scratch directory per (candidate, tree).  Two reasons, and the first is
# the load-bearing one: the corpus lives in here, and a candidate's payload
# indices are handed out from 0 on each tree, so a corpus surviving from the other
# tree's run would make index 3 mean two different things in one comparison.  The
# second is the ordinary one -- a compiled helper or a gz file left behind must not
# still be there when the same candidate runs against the other tree.
rm -rf "$SRB_SCRATCH"; mkdir -p "$SRB_SCRATCH"

# --- 4. Run the candidate -----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=300 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
