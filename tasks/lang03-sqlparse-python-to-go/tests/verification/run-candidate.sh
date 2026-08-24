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
# and one positional argument, "original" or "submission", which is this
# script's to know and not the candidate's.
#
# Exit 0 means the candidate passed against this tree; non-zero means it failed.
# That is the whole contract, and it is why a broken candidate reads as "does not
# pass on the original" rather than as a defect in the submission.
#
# ---------------------------------------------------------------------------
# The asymmetry, and what is done about it
# ---------------------------------------------------------------------------
# The two trees are not the same kind of thing.  The original is a Python package
# that needs an interpreter and a PYTHONPATH; the submission is a Go module that
# needs building, and it produces five programs -- four probe tiers and a
# sqlformat.  So this script cannot hand the candidate one command line.
#
# What it hands over instead is a *table*: for each tier, the argv that answers
# it and the environment overlay it needs.  The original's overlay puts the tree
# on PYTHONPATH and its argv is one interpreter for all four tiers; the
# submission's overlay is empty and its argv is a different binary per tier.
# srbsqlparse.py applies whatever it was given and cannot tell the two apart,
# which is what makes the comparison symmetric rather than merely fair-looking.
#
# This is the only stage where the reference is runnable, and that is the point
# of the stage: a candidate's claim is empty unless the original demonstrably
# passes it.  Stage 2 has already measured whether the submission needs a Python
# -- there, importing sqlparse fails by construction.
#
# What a candidate gets is *built* programs, never a build tree and never a
# source file.  A migration is graded on what it ships.
# =============================================================================
set -uo pipefail

TARGET="${SRB_TARGET:?SRB_TARGET is not set}"
# Passed as this script's last argument, never in the environment: a child
# process inherits the environment automatically and inherits argv never, so the
# role cannot reach the candidate by being forgotten about.
ROLE="${1:?the target role was not passed as an argument}"
TOKEN="${SRB_TARGET_TOKEN:?SRB_TARGET_TOKEN is not set}"
CANDIDATE="${SRB_CANDIDATE:?SRB_CANDIDATE is not set}"
WORK="${SRB_WORK:-/tmp/srb-verification}"

# Keyed by the token, not the role: the candidate is handed paths into this, and
# a path with "original" in it would answer the question it is meant to answer by
# observing behaviour.
STATE="$WORK/$TOKEN"
BIN="$STATE/bin"
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

# The interpreter by absolute path.  /opt/venv/bin/python runs the candidate's
# pytest; this one runs the reference probe, and they are deliberately not the
# same interpreter -- the venv has pytest in it and the reference must not.
PYTHON=/usr/bin/python3
PROBE_SRC=/opt/probe
TIERS="core model keywords parts"

# --- 1. Build the tree, once per tree per stage -------------------------------
# Six rounds share this.  Rebuilding for each of ~120 candidate runs would
# spend the stage's budget on the Go linker rather than on finding defects, and
# the tree does not change between them.  The marker file records a completed
# build: a bin directory that exists because a build was interrupted must not be
# reused.
if [ ! -f "$STATE/.built" ]; then
    log "preparing the tree (first candidate pays for this)"
    rm -rf "$BIN" "$STATE/gobuild"
    mkdir -p "$BIN"

    if [ "$ROLE" = original ]; then
        # No build step: the reference is a Python package.  What it needs is a
        # sqlformat whose argv[0] is a real path with the right basename, because
        # the library derives `prog` in its usage and error text from it -- and
        # `python -m sqlparse.cli` would put "cli.py" or "__main__.py" there,
        # making every diagnostic differ in the program name and nothing else.
        # This is the same launcher stage 2 froze its CLI answers through.
        #
        # PYTHONPATH is set *inside* the shim rather than left to the caller, and
        # that is not tidiness.  The probe gets its PYTHONPATH from the argv table,
        # which srbsqlparse applies per tier -- but sqlformat() runs this program
        # under vlib.base_env(), which by construction carries no PYTHONPATH,
        # because the submission's sqlformat is a static binary that must not be
        # handed one.  So the reference's launcher has to be self-contained: the
        # only asymmetry a candidate can observe is that one of the two programs
        # happens to be a shell script, and neither of them needs an environment
        # the other does not get.  Measured -- without this line every sqlformat()
        # call on the original exits 1 with ModuleNotFoundError, which reads to a
        # candidate as the reference having no CLI at all.
        cat >"$BIN/sqlformat" <<LAUNCHER
#!/bin/sh
PYTHONPATH=$TARGET
export PYTHONPATH
exec $PYTHON -c 'import sys
from sqlparse.cli import main
sys.exit(main())' "\$@"
LAUNCHER
        chmod 0755 "$BIN/sqlformat"
        # The shim is the reference's whole CLI, and a broken one would look like
        # a submission that ships no sqlformat.  Checked once, here, rather than
        # discovered by the first candidate that runs the command line.
        printf 'select 1;' | "$BIN/sqlformat" - >/dev/null 2>>"$STATE/build.log" || {
            log "the reference sqlformat shim does not run; see $STATE/build.log"
            tail -20 "$STATE/build.log" >&2
            exit 71
        }
    else
        # The submission is a Go module, and the probe tiers live in a module of
        # their own that reaches it through `replace ... => ../submission`.  Those
        # two directory names are fixed by that directive, not chosen here.
        GOBUILD="$STATE/gobuild"
        mkdir -p "$GOBUILD"
        cp -a "$TARGET" "$GOBUILD/submission"
        rm -rf "$GOBUILD/submission/.git"
        cp -a "$PROBE_SRC" "$GOBUILD/probe"

        # A submission may arrive carrying build output.  A stale directory here
        # cannot poison a Go build the way a CMakeCache can, but it can hold
        # object files from a build this container never ran, and the tree under
        # test should be the tree that was submitted.
        find "$GOBUILD/submission" -maxdepth 2 -depth \
            \( -name bin -o -name obj -o -name '_build' \) \
            -type d -prune -exec rm -rf {} + 2>/dev/null

        export GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local
        export CGO_ENABLED=0 GOCACHE="$STATE/gocache" GOMODCACHE="$STATE/gomod"
        export GOTELEMETRY=off HOME="$STATE"

        {
            ( cd "$GOBUILD/submission" \
              && go build -trimpath -o "$BIN/sqlformat" ./cmd/sqlformat ) \
            && for tier in $TIERS; do
                   ( cd "$GOBUILD/probe" \
                     && go build -trimpath -o "$BIN/probe-$tier" "./$tier" ) \
                   || exit 1
               done
        } >>"$STATE/build.log" 2>&1 || {
            log "the tree does not build; see $STATE/build.log"
            tail -30 "$STATE/build.log" >&2
            # A submission that will not build is stage 2's finding, not stage
            # 3's.  71 is in the fault range the adjudicator reads (sysexits
            # 64-78), so the candidate is invalid on whichever tree this happened
            # to.  Requiring a PASS on the original does not cover this on its own
            # -- a submission that will not build passes there, and the round would
            # read the pair as a divergence.
            exit 71
        }

        # All four tiers, strictly.  A submission reaching stage 3 passed every
        # scored check, and a missing tier fails the checks that measure it, so
        # this is unreachable rather than merely improbable.  The check stays
        # anyway: a missing tier would otherwise manufacture six free breaks
        # out of one defect stage 2 already charged for, and a stage that trusts
        # its predecessor's arithmetic for a fact it can test in one line is
        # trusting the thing most likely to have been misconfigured.
        for tier in $TIERS; do
            if [ ! -x "$BIN/probe-$tier" ]; then
                log "the build produced no probe-$tier"
                exit 72
            fi
        done
    fi

    if [ ! -x "$BIN/sqlformat" ]; then
        log "there is no sqlformat to test"
        exit 72
    fi
    touch "$STATE/.built"
    log "built into $BIN"
fi

# --- 2. Put the tree behind paths that do not name it -------------------------
# Everything the candidate can see a path to lives here, at the same absolute
# path for both roles, and is rebuilt per run.  $BIN is keyed by the token, and
# the library prints paths: sqlparse's CLI reports the file it could not open, so
# a candidate diffing two error messages would be diffing two temp paths.
# Stage 2 normalizes those paths away when it compares; a candidate compares raw
# bytes, so here they are made equal instead.
#
# (`prog` in usage text is not affected either way -- upstream's cli.py passes
# prog='sqlformat' literally -- but the file operands are.)
VIEW=/tmp/srb-candidate-view
rm -rf "$VIEW"
mkdir -p "$VIEW/docs"
ln -s "$BIN/sqlformat" "$VIEW/sqlformat"

# The argv table.  One entry per tier, and the difference between the two roles
# lives entirely in here: the reference answers all four tiers from one
# interpreter (it accepts --tier and ignores it), the submission has a binary
# each.  srbsqlparse.py applies the table it is given.
"$PYTHON" - "$ROLE" "$BIN" "$VIEW" "$TARGET" <<'PY'
import json, sys
from pathlib import Path
role, bin_dir, view, target = sys.argv[1:5]
tiers = ("core", "model", "keywords", "parts")
common = ["--docs", f"{view}/docs",
          "--documents", f"{view}/documents.json",
          "--spec", f"{view}/spec.json"]
if role == "original":
    argv = {t: ["/usr/bin/python3", "/opt/probe/probe.py", *common, "--tier", t]
            for t in tiers}
    # The reference needs its own tree importable; the submission needs nothing.
    # PYTHONPATH is prepended by srbsqlparse through vlib.base_env, so the pinned
    # tree wins over anything else that might call itself sqlparse.
    overlay = {t: {"PYTHONPATH": target} for t in tiers}
else:
    argv = {t: [f"{bin_dir}/probe-{t}", *common, "--tier", t] for t in tiers}
    overlay = {t: {} for t in tiers}
Path(view, "argv.json").write_text(json.dumps(argv), encoding="utf-8")
Path(view, "argvenv.json").write_text(json.dumps(overlay), encoding="utf-8")
PY
if [ $? -ne 0 ]; then
    log "could not build the argv table"
    exit 73
fi

export SRB_PROBE_ARGV="$(cat "$VIEW/argv.json")"
export SRB_PROBE_ENV="$(cat "$VIEW/argvenv.json")"
export SRB_SQLFORMAT="$VIEW/sqlformat"
export SRB_DOCS_DIR="$VIEW/docs"
export SRB_DOCS_JSON="$VIEW/documents.json"
export SRB_SPEC_JSON="$VIEW/spec.json"
export SRB_OPS_JSON="$VIEW/ops.json"
export SRB_TARGET_TOKEN="$TOKEN"
export SRB_SCRATCH="$VIEW/scratch"
mkdir -p "$SRB_SCRATCH"

# The spec is writable because add_format_preset() appends to it; the op table is
# not.  Both are copied per run, so a preset a candidate added while testing one
# tree is not still there when it tests the other.
cp /opt/probe-assets/spec.json "$SRB_SPEC_JSON"
cp /opt/probe-assets/ops.json "$SRB_OPS_JSON"
chmod 0644 "$SRB_SPEC_JSON"
printf '{"documents": []}\n' >"$SRB_DOCS_JSON"

# --- 3. Run the candidate -----------------------------------------------------
# -p no:cacheprovider because both trees share the candidates directory, and a
# .pytest_cache beside the candidate would be the one piece of state that crosses
# between the two halves of a comparison.
#
# PYTHONPATH carries srbsqlparse.py and, through it, the verifier's own protocol
# client: a candidate speaks the same wire the graded run did, so a "break" cannot
# come from a second implementation of the framing.
env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PYTHONPATH=/tests/verification/lib \
    /opt/venv/bin/python -m pytest \
        -p no:cacheprovider -q --no-header -x --timeout=180 \
        "$CANDIDATE"
STATUS=$?
log "candidate ${SRB_CANDIDATE_NAME:-?}: pytest exit $STATUS"
exit $STATUS
