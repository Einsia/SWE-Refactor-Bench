# =============================================================================
# SWERefactorBench -- pf03-quickjs-byteorder-port
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/pf03-audit:1 \
#         tasks/pf03-quickjs-byteorder-port/tests/audit
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
# There is deliberately no compiler here -- not gcc, not either cross compiler,
# not make, and not qemu.  For a task whose subject is a compile-time conditional
# that deserves an explanation rather than an assertion.
#
# The temptation is obvious.  The question "will `WORDS_BIGENDIAN` be defined when
# this is compiled for s390x" has an exact answer, and a compiler in this image
# could get it in one command.  The reason not to is that it is the wrong question
# for this stage, and having the tool available is how a review ends up answering
# it instead of the one it was asked.
#
# Stage 2 answers it, properly and at scale: it builds the submission for x86-64,
# s390x and armhf, builds the reference for the same three, and compares roughly
# seventy behavioural probes per target -- so a detection that concludes wrongly
# shows up as a wrong value, whatever it was written with, on the target where it
# is wrong.  That is a stronger result than any preprocessor query, and it is not
# reproducible here at any cost: this stage would need three toolchains, two
# emulators and the better part of an hour to reach a worse version of the same
# answer.
#
# What this stage has instead is the thing no build log settles.  A submission can
# compute the right answer on all three of the targets this benchmark happens to
# build and still have written `#define WORDS_BIGENDIAN 1` under a check on a
# distribution name; can have fixed six of the eight conditional sites and been
# lucky about the other two; can have swapped bytes somewhere the format does not
# call for it and been saved by a probe nobody wrote.  Those are questions about
# what the code means, they are answered by reading it against State A, and a
# reader who can compile will stop reading and start compiling.
#
# So the asymmetry is deliberate and it runs both ways: this stage may read and
# may not execute, stage 2 executes and is handed no source tree to walk (its
# fixtures do not offer one).  Stage 3 has everything, because an adversary has to
# be able to build both trees to claim one of them is wrong.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by reading
# only; the tools the runner exposes are list_dir, fetch_source, search and diff.
# ripgrep, diffutils and tree back those; the rest is for a human reading a
# transcript afterwards.
#
# Network: the model API and nothing else.  It is the one stage that is not
# offline, which is the other half of why it gets no toolchain -- there is no point
# in this container where submitted code executes.
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

# The prompt is this task's most load-bearing file: it is what replaced the gate
# list.  Check at build time that it is present, that it is prose rather than a
# stub, and that its placeholders are ones the runner fills -- an unknown
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
# pytest is here and a toolchain is not, which is less of a contradiction than it
# looks.  The scan is three pytest modules that hash files and match strings; they
# import nothing from the submission and could not -- there is no compiler in this
# image, so a module that tried to build something would die on a missing binary.
# What pytest buys is the module contract: a scan module and a stage-2 module are
# byte-for-byte the same shape, so one can move between the stages without a
# rewrite, and `swerefactor.pytest_module` turns each collected test into one check
# keyed by nodeid without a parallel runner having to exist.
#
# Pinned by hash, from the same versions stage 2 pins, so the two stages cannot
# drift into different collection behaviour.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# Note what this image does *not* carry: a frozen manifest of State A's checksums.
# State A is mounted at /opt/original and the scan hashes it directly.  One fewer
# duplicated input, nothing that can be regenerated alone and drift, and a
# stronger claim -- the comparison is against the same tree the reviewer is
# reading, not against a file asserting what that tree used to contain.
#
# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt: the reviewer would be told nothing and have no way to know
# it was told nothing.
#
# The floor is the second half of the same argument.  `changed-files` derives its
# parametrize list from the reference tree mounted at /opt/original, so the number
# of checks it collects is a function of a mount that does not exist at build
# time -- an empty mount collects the module's four unparametrized checks and one
# placeholder, which is a plausible-looking number that measures nothing.  So
# collect-check.sh's second phase points SRB_ORIGINAL at a synthetic tree with a
# known number of files in it and asserts the count follows.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 12

# A review that reads code cannot be allowed to build it: with a toolchain here it
# would answer stage 2's question badly instead of its own.  Asserted, because a
# base image update or a careless apt line is all it would take.
RUN for forbidden in gcc cc c++ g++ make cmake \
                     s390x-linux-gnu-gcc arm-linux-gnueabihf-gcc \
                     qemu-s390x-static qemu-arm-static; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden." >&2; \
            echo "This stage reads; stage 2 builds all three targets and" >&2; \
            echo "compares what they compute. A reviewer that can compile" >&2; \
            echo "answers stage 2's question and skips its own." >&2; \
            exit 1; \
        fi; \
    done

# No JavaScript engine either.  The subject is an interpreter, and a review with a
# working `node` on PATH can evaluate the submission's own test files and start
# reporting on runtime behaviour it has no way to attribute to a target.
RUN for forbidden in node nodejs deno bun d8 js jsc qjs quickjs; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden" >&2; exit 1; \
        fi; \
    done

# The mount points exist in the image so a misconfigured run fails with an empty
# directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
