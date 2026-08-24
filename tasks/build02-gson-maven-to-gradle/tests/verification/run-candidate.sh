#!/usr/bin/env bash
# Run one verification candidate against one tree.
#
# The harness calls this once per (candidate, tree) pair, twice per candidate.  It
# arrives with:
#
#   $SRB_TARGET         the tree to run against -- a copy, staged under a token
#   $SRB_TARGET_TOKEN   that token; the only name for the tree a candidate can see
#   $SRB_CANDIDATE      the .py file the adversary wrote
#   $1                  "original" or "submission"
#   cwd                 $SRB_TARGET
#
# WHY THE ROLE IS AN ARGUMENT
#
# Because a child process inherits the environment automatically and inherits argv
# never.  The harness passes the role here rather than in the environment so that
# forgetting to scrub leaks nothing: this script has to pass it on deliberately, and
# it does not.  What it does with it is turn it into a build dialect, below.
#
# WHY THIS TASK NEEDS IT AT ALL
#
# Most tasks in this benchmark have two trees that install the same way, and their
# run-candidate.sh ignores the role entirely.  This one cannot: the original is a
# Maven reactor and the submission is a Gradle multi-project, so "build this tree"
# is two different commands.  The translation is in lib/srbgson.py, written once per
# configuration, and a candidate cannot reach it -- it names a configuration and
# gets artifacts back.
#
# The consequence to be clear about: the harness decides how each tree is built, so
# a difference a candidate finds is a difference in what those two builds PRODUCED.
# It is never a difference in how they were spelled.
set -uo pipefail

CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
ROLE="${1:?the target role was not passed as an argument}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

case "$ROLE" in
    original)   DIALECT=maven  ;;
    submission) DIALECT=gradle ;;
    *) echo "unknown target role: $ROLE" >&2; exit 64 ;;
esac

# Nothing downstream learns the role.  The candidate's process gets the token, and
# the token is a salted hash the adversary never sees the input to.
unset SRB_TARGET_ROLE SRB_TARGET_NAME SRB_ORIGINAL

# ---------------------------------------------------------------------------
# Does the tree agree with the role?
#
# A mis-mount is the failure mode that costs the most and shows the least.  If the
# submission were handed over as the original, every candidate would build a Gradle
# tree with Maven, the build would fail, the runner would record "did not pass on
# the original", the adjudicator would read six rounds finding nothing, and the
# submission would be paid all sixty points for a bug of ours.  So the tree is asked
# to confirm what it is before anything is built, and a disagreement is a hard exit
# rather than a failed candidate.
#
# Note the asymmetry, which is deliberate.  The original MUST have a pom.xml: it is
# unpacked from original.tar.gz and cannot be anything else.  The submission MUST
# have a Gradle settings file: a submission without one has not migrated, and stage
# 2 has already scored that -- but if it also has no pom.xml then there is nothing
# here to build either way, and exiting 65 tells the harness this is a harness-level
# problem rather than twelve findings.
# ---------------------------------------------------------------------------
case "$DIALECT" in
maven)
    if [ ! -f "$TARGET/pom.xml" ]; then
        echo "the tree presented as the original has no pom.xml at its root." >&2
        echo "The original is unpacked from a frozen archive and always has one," >&2
        echo "so this is a staging fault, not a property of any submission." >&2
        exit 65
    fi
    ;;
gradle)
    if [ ! -f "$TARGET/settings.gradle" ] \
       && [ ! -f "$TARGET/settings.gradle.kts" ]; then
        echo "the tree presented as the submission has no Gradle settings file." >&2
        echo "Stage 2 has already scored that; this stage has nothing to build." >&2
        exit 65
    fi
    ;;
esac

# ---------------------------------------------------------------------------
# Per-tree state, keyed by token
#
# Everything a candidate builds lands under $WORK/$TOKEN.  Keyed by the token and
# not by the role, so a directory listing here names neither tree -- and so the
# four configurations, which are expensive and identical for every candidate in the
# stage, are built once per tree and reused by every candidate after the first.
# ---------------------------------------------------------------------------
STATE="$WORK/$TOKEN"
mkdir -p "$STATE" || { echo "cannot create $STATE" >&2; exit 70; }

# The dialect is recorded once, for srbgson to read.  A candidate that reads it
# learns which tree it is on and gains nothing: see the deny list.
printf '%s\n' "$DIALECT" > "$STATE/dialect"

# ---------------------------------------------------------------------------
# Scrub, once per tree
#
# The submission is whatever the agent left in /workspace/repo.  That may include a
# target/ from before the migration, a build/ from the agent's own last run, or a
# jar sitting beside the sources that produced it.  Left in place, a stale artifact
# gets picked up as "the jar this build produced", and a candidate comparing the two
# trees is comparing this container's javac against whatever built that file.
#
# EVERY NAME BELOW IS EXACT, and the reason is a bug this benchmark has already
# paid for once: on another task a `build-*` glob written to catch build-release/
# also caught build-aux/, which was delivered content, and deleting it stopped the
# ORIGINAL from building -- which made every candidate in the stage "fail on the
# original", which reads as six survivals, which pays the submission all sixty
# points.  A silent sixty is the worst failure mode this benchmark has.
#
# The near-miss on THIS task is the string `build`.  `build/` is Gradle's output
# directory and must go; `build.gradle` is the submission's build system and must
# not.  So the loop below tests -d and an exact name, and build.gradle,
# build.gradle.kts, settings.gradle and gradle.properties are all untouched by
# construction.  There is no glob anywhere in it.
# ---------------------------------------------------------------------------
# The list is three names long, and the shortness is the design.  `target/`,
# `build/` and `.gradle/` are output directories for these two build systems and
# cannot be anything else.  `out/`, `bin/` and `classes/` were considered and
# rejected: each is a plausible name for delivered content in a Java project, and
# a scrub that removed delivered content from the SUBMISSION would break its build,
# which makes every candidate "pass on the original, fail on the submission" --
# six breaks, and a zero for a bug of ours.  That is the mirror image of the
# silent sixty and just as bad.  Both trees were checked: neither has a directory
# by any of the six names, and neither delivers a .jar or a .class.
if [ ! -f "$STATE/.scrubbed" ]; then
    for name in target build .gradle; do
        find "$TARGET" -depth -type d -name "$name" -prune \
             -exec rm -rf {} + 2>/dev/null
    done
    find "$TARGET" -type f \
         \( -name '*.jar' -o -name '*.class' -o -name '*.war' \) \
         -delete 2>/dev/null
    # A tree that lost its build system to the scrub is a bug of ours, and it must
    # not be discovered later as a build failure that looks like a finding.
    case "$DIALECT" in
    maven)  keep="$TARGET/pom.xml" ;;
    gradle) keep="$TARGET/settings.gradle" ;;
    esac
    if [ ! -e "$keep" ] && [ ! -e "$keep.kts" ]; then
        echo "the scrub removed $keep -- refusing to continue, because a tree" >&2
        echo "that cannot build makes every candidate look like a finding." >&2
        exit 70
    fi
    touch "$STATE/.scrubbed"
fi

# ---------------------------------------------------------------------------
# Fresh scratch per (candidate, tree), persistent state per tree
# ---------------------------------------------------------------------------
export SRB_STATE="$STATE"
export SRB_SCRATCH="$STATE/scratch/$(basename "$CANDIDATE" .py)"
rm -rf "$SRB_SCRATCH"
mkdir -p "$SRB_SCRATCH"

# No stub directory, and the absence is deliberate.  Stage 2 shadows `mvn` and
# fourteen other engine names to prove the migrated tree does not need them; this
# stage must not, because a submission that cannot build here would make every
# candidate pass on the original and fail on the submission -- six breaks, and a
# zero for a bug of ours.  Stage 2 has already decided that question where it is
# measured.
export SRB_MVN_SETTINGS="${SRB_MVN_SETTINGS:-/root/.m2/settings.xml}"
export JAVA_HOME="${JAVA_HOME:-/opt/java/openjdk}"
export GRADLE_USER_HOME="${GRADLE_USER_HOME:-/root/.gradle}"
export HOME="${HOME:-/root}"

# Nothing from the adversary's own process reaches a build.  A build that only
# succeeds because JAVA_TOOL_OPTIONS was set is not the build, and the two trees
# would not be getting the same treatment.
unset MAVEN_OPTS MAVEN_ARGS M2_HOME ANT_HOME ANT_OPTS GRADLE_OPTS CLASSPATH
unset JAVA_TOOL_OPTIONS _JAVA_OPTIONS

# ---------------------------------------------------------------------------
# Run it
#
# pytest because a candidate is a test file and a test file's PASS/FAIL is the
# signal the adjudicator reads.  `-p no:cacheprovider` because a cache directory
# written into the tree would be a difference between the two runs that the
# candidate caused.  The timeout is below the harness's own so a hung candidate
# reports as a candidate failure rather than as the whole round timing out.
# ---------------------------------------------------------------------------
cd "$TARGET" || exit 70
exec env PYTHONDONTWRITEBYTECODE=1 PYTHONHASHSEED=0 TZ=UTC LC_ALL=C.UTF-8 \
     PYTHONPATH=/tests/verification/lib \
     /opt/venv/bin/python -m pytest \
         -p no:cacheprovider -q --no-header --tb=short \
         --timeout=2100 \
         "$CANDIDATE"
