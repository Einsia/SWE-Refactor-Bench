# =============================================================================
# SWERefactorBench -- pf02-stylus-web-platform-port
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/pf02-audit:1 \
#         tasks/pf02-stylus-web-platform-port/tests/audit
#
# A reviewer's workbench.  It holds a model client, tools for reading code, and
# two trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts, not image layers, because the same image reviews
# every submission for this task.  The original is mounted read-only and the
# workspace is a copy: a review must not be able to change what stage 2 later
# measures, and it must not be able to change the thing it is comparing against.
#
# There is deliberately no Node here.  Not "no node_modules" -- no interpreter.
# This stage decides whether the port happened by reading two trees, and the
# submission under review is a JavaScript project whose own test suite is sitting
# in it: an image with a `node` on the path is one where a scan module could
# execute what it is reading, and where a reviewer's shell-out could end up
# grading `npm test`.  Whether the project builds, installs and compiles a
# stylesheet is stage 2's question, in an image equipped to ask it.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It
# runs `scan.toml`: 66 file-reading checks whose output is substituted into the
# prompt as findings for the reviewer to follow up.  They import nothing from the
# submission -- `srbscan` opens files, blanks their comments and strings, and
# matches -- and the base image above is what makes that structural rather than a
# promise.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by
# reading only; the tools available are list_dir, fetch_source, search and diff.
# The tree-walking utilities below are for a human debugging a transcript.
#
# Network: this stage needs to reach the model API and nothing else.  It is the
# one stage that is not offline, which is why it gets no submission runtime --
# there is no point in the container where submitted code executes.
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

# ripgrep and diffutils back the search and diff tools; the rest is for reading
# a transcript by hand afterwards.
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
# and nothing to drift.  The credential arrives as an environment variable at
# run time and is never baked into a layer.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The scan ----------------------------------------------------------------
# pytest is the scan's runner and `swerefactor.pytest_module` turns each collected
# test into one check, so the findings arrive keyed by node id.  Pinned by hash
# like everything else in this benchmark: a scan whose collection count moved
# between builds would change what the reviewer is told without anyone editing a
# file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# The prompt is this task's most load-bearing file: it is what replaced the
# regex gate list.  Check at build time that it is present, that it is prose
# rather than a stub, and that its placeholders are ones the runner fills -- an
# unknown placeholder survives into the transcript verbatim, which is visible
# but late.  `findings` is checked by name because it is the seam between the two
# halves of this stage: a prompt without it renders a review that was never shown
# what the scan found, and nothing else would report that.
RUN python -c "\
import re, sys, pathlib; \
from swerefactor import audit; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt is too short to be a review brief') if len(text) < 2000 else None; \
found = set(re.findall(r'{{\s*([a-z_]+)\s*}}', text)); \
unknown = sorted(found - set(audit.PLACEHOLDERS)); \
sys.exit(f'prompt.txt uses unknown placeholder(s): {unknown}') if unknown else None; \
missing = sorted({'gates', 'findings'} - found); \
sys.exit(f'prompt.txt never substitutes {missing}') if missing else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to
# know it was told nothing.  Collection runs against the empty /opt/workspace
# mount point, which is enough to execute every import and parametrize call.
#
# The floor is 60 against a measured inventory of 66 (closure 19, entrypoint 9,
# frozen 31, provenance 7).  It is under the real number rather than equal to it
# so that adding a check does not fail the build, and close enough that losing a
# module does: the smallest module here is 7 checks, so any module going missing
# takes the total below 60.  Note what this does *not* check -- a suite can
# collect all 66 and still be blind, because collection says nothing about
# whether a check would fire.  That is what the tamper probes are for, and they
# are not in this image.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 60

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.  An empty
# /opt/original is not silent either: `frozen` reports the missing mount as a
# finding rather than skipping thirty comparisons and reporting a clean tree.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
