# =============================================================================
# SWERefactorBench -- build01-libsodium-autotools-to-cmake
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/build01-audit:1 \
#         tasks/build01-.../tests/audit
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
# There is deliberately neither CMake nor a compiler here.  For a build-system
# migration that is the whole point, so it is worth being explicit: this stage
# answers six questions -- did Autotools leave, is CMake the build rather than a
# wrapper, is the feature detection real, is the ISA partition understood, were
# the sources left alone, does the build know it is being graded -- and every one
# of them is settled by reading build code against State A's build code.  Give
# the review a CMake and it starts configuring and grading the output, which is
# stage 2's job in an image built to do it: stage 2 configures the submission
# eleven times across both generators, compiles 80 of libsodium's own test programs
# against the install tree, and reads the ISA flags off 119 translation units.
# Those are measurements.  This stage is for the judgement no build log settles --
# whether forty compiler questions are being asked or remembered.
#
# The asymmetry runs the other way too, and the three images together are the
# reason the split holds: this stage has no build system at all, stage 2 has CMake
# and deliberately no Autotools (its Dockerfile fails the build if autoconf,
# automake, libtool or m4 is on PATH), and stage 3 is the only image that can
# configure either build system, because "the Autotools build passes this test" is
# a claim an adversary has to be able to execute.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by reading
# only; the tools the runner exposes are list_dir, fetch_source, search and diff.
# ripgrep, diffutils and tree back those; the rest is for a human reading a
# transcript afterwards.
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
# like. The scan is three pytest modules that hash files and match strings; they
# import nothing from the submission and could not -- there is no CMake and no
# compiler in this image, so a module that tried to configure anything would die
# on a missing binary. What pytest buys is the module contract: a scan module and a
# stage-2 module are byte-for-byte the same shape, so one can move between the
# stages without a rewrite, and `swerefactor.pytest_module` turns each collected test
# into one check keyed by nodeid without a parallel runner having to exist.
#
# Pinned by hash, from the same file stage 2 pins its pytest with, so the two
# stages cannot drift into different collection behaviour.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# Note what this image does *not* carry: a copy of State A's source checksums.
# Stage 2 grades against a frozen `data/sources.json`, because by the time it runs
# it has a matrix of build trees and no reference tree. This stage has the reference tree
# mounted at /opt/original, so the scan hashes it directly. One fewer duplicated
# input, no manifest that can be regenerated alone and drift, and a stronger claim:
# the comparison is against the same State A the reviewer is reading, not against a
# file asserting what State A used to contain.

# The scan has to collect, here, at build time. A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.
#
# The floor is 80 rather than fw01's 100 because of the decision above. With no
# reference tree mounted the source comparison contributes its six unparametrized
# checks and one placeholder rather than 353, so the build-time count is 82 and the
# run-time count is 434. That gap is exactly the risk in deriving a parametrize list
# from a mount, so collect-check.sh's second phase points SRB_ORIGINAL at a synthetic
# tree holding four immutable files and asserts the count follows it. A module that
# stopped deriving from the mount -- and would therefore report a clean tree against
# a missing one -- fails this layer.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 80

# A build-system review that cannot read the build system it is comparing against
# has nothing to do, so assert here that no toolchain leaked in through a base
# image update or a careless apt line.
RUN for forbidden in cmake make gcc cc autoconf automake libtool; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden -- a review" >&2; \
            echo "that can build starts grading build logs, which is stage 2." >&2; \
            exit 1; \
        fi; \
    done

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
