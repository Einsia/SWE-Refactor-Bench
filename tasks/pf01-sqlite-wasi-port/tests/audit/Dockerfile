# =============================================================================
# SWERefactorBench -- pf01-sqlite-wasi-port
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/pf01-audit:1 \
#         tasks/pf01-sqlite-wasi-port/tests/audit
#
# A reviewer's workbench.  It holds a model client, tools for reading code, and
# two trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts rather than image layers, because the same image reviews
# every submission for this task.  The original is mounted read-only and the
# workspace is a copy: a review must not be able to change what stage 2 later
# measures, and it must not be able to change the thing it is comparing against.
#
# There is deliberately no compiler here, no tclsh and no wasm runtime.  This
# stage answers six questions -- did the POSIX layer leave, is the layer that
# replaced it a real filesystem engine, was the database engine left alone, does
# the build produce the module from this repository's own sources, can the module
# reach anything outside its sandbox, does the port recognise the grader -- and
# every one of them is settled by reading platform code against State A's
# platform code.  Give the review a clang and it starts building and grading a
# module, which is stage 2's job in an image built to do it: stage 2 builds native
# 3.31.1 from State A at image build time, builds the submission's
# `build-wasi.sh` at run time, and compares the two processes on stdout, stderr,
# exit status and the bytes of the databases they write.  Those are measurements.
# This stage is for the judgement no comparison settles -- whether a VFS method
# was implemented or made to return a value that satisfies most inputs.
#
# The asymmetry runs the other way too, and the three images together are the
# reason the split holds: this stage cannot build anything, stage 2 has the full
# wasi-sdk and a frozen native reference, and stage 3 is the only image that can
# drive both build systems, because "native 3.31.1 answers this and the module
# does not" is a claim an adversary has to be able to execute.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by
# reading only; the tools the runner exposes are list_dir, fetch_source, search and
# diff.  ripgrep, diffutils and tree back those; the rest is for a human reading
# a transcript afterwards.  `file` is here because the question "is this
# delivered blob a wasm module" comes up while reading a tree, and answering it
# is an identification, not an execution.
#
# Network: the model API and nothing else.  It is the one stage that is not
# offline, which is the other half of why it gets no toolchain -- there is no
# point in this container where submitted code executes.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM python@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        diffutils \
        file \
        git \
        jq \
        less \
        ripgrep \
        tree \
    && rm -rf /var/lib/apt/lists/*

# --- The harness -------------------------------------------------------------
# swerefactor.models talks to the API over urllib, so there is no vendor SDK here
# and nothing to drift.  The credential arrives as an environment variable at run
# time and is never baked into a layer.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# The prompt is this task's most load-bearing file: it is what replaced the regex
# gate list.  Check at build time that it is present, that it is prose rather than
# a stub, and that its placeholders are ones the runner fills -- an unknown
# placeholder survives into the transcript verbatim, which is visible but late.
RUN python -c "\
import re, sys, pathlib; \
from swerefactor import audit; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt is too short to be a review brief') if len(text) < 2000 else None; \
found = set(re.findall(r'{{\s*([a-z_]+)\s*}}', text)); \
unknown = sorted(found - set(audit.PLACEHOLDERS)); \
sys.exit(f'prompt.txt uses unknown placeholder(s): {unknown}') if unknown else None; \
sys.exit('prompt.txt has no {{gates}} placeholder, so the gates declared in \
evaluation.toml would never reach the review') if 'gates' not in found else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# --- The read-only scan ------------------------------------------------------
# pytest is here and a toolchain is not, which is not the contradiction it looks
# like.  The scan is four pytest modules that hash files and match strings; they
# import nothing from the submission and could not -- there is no compiler and no
# wasm runtime in this image, so a module that tried to build anything would die on
# a missing binary.  What pytest buys is the module contract: a scan module and a
# stage-2 module are the same shape, so one can move between the stages without a
# rewrite, and `swerefactor.pytest_module` turns each collected test into one check
# keyed by nodeid without a parallel runner having to exist.
#
# Pinned by hash so the two pytest-using stages of this benchmark cannot drift into
# different collection behaviour.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# Note what this image does *not* carry: a copy of State A's checksums.  The
# reference tree is mounted at /opt/original, so the scan hashes it directly.  One
# fewer duplicated input, no manifest that can be regenerated alone and drift, and
# a stronger claim: the comparison is against the same State A the reviewer is
# reading, not against a file asserting what State A used to contain.
#
# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.
#
# The collection floor is 30 because of the decision above: with no reference tree
# mounted the 1,856-file comparison contributes its unparametrized checks and one
# placeholder rather than 1,856 cases, so the build-time count is 33 and the run-time
# count is ~1,890.  That gap is exactly the risk in deriving a parametrize list from a
# mount, so collect-check.sh's second phase points SRB_ORIGINAL at a synthetic tree and
# asserts the count follows it.  A module that stopped deriving from the mount --
# and would therefore report a clean tree against a missing one -- fails this layer.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 30

# A platform review that can build the platform it is reviewing has stopped being
# a review, so assert here that no toolchain leaked in through a base image update
# or a careless apt line.  `sqlite3` is on this list for a second reason: a native
# SQLite in this image is an oracle, and a reviewer with an oracle starts answering
# stage 2's questions from a shell instead of stage 1's questions from the source.
RUN for forbidden in clang clang-18 gcc cc c++ g++ ld.lld wasm-ld make gmake \
                     cmake ninja tclsh wasmtime wasmer wasm3 node sqlite3; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden -- a review" >&2; \
            echo "that can build or run the port starts grading its output," >&2; \
            echo "which is stage 2, in an image that freezes a reference." >&2; \
            exit 1; \
        fi; \
    done

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
