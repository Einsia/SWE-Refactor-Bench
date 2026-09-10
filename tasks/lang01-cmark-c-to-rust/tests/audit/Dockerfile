# =============================================================================
# SWERefactorBench -- lang01-cmark-c-to-rust
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang01-audit:1 \
#         tasks/lang01-.../tests/audit
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
# There is deliberately no Rust toolchain and no CMake here, which for a language
# migration is worth spelling out.  This stage answers twelve questions -- did the
# C leave, is the Rust the implementation, is the graded path the default one --
# and every one of them is settled by reading source and build files.  Give the
# review a compiler and it starts grading build logs, which is stage 2's job in an
# image built to do it: stage 2 configures both link configurations under a
# shimmed cc, watches for a cc1 that runs, and reads rustc's provenance out of the
# shipped ELF.  Those are measurements.  This stage is for the judgement calls
# that no amount of build output settles, chiefly whether 22,000 lines of C were
# reimplemented or transliterated.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It runs
# `scan.toml`: 145 file-reading checks whose output is substituted into the prompt
# as findings for the reviewer to follow up.  They walk directories, hash files and
# sniff the first four bytes of one to see whether it is an object file; there is no
# rustc here that could satisfy a module which tried to compile anything, which is
# the property that keeps a scan a scan.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by
# reading only; the tools the runner exposes are list_dir, fetch_source, search and
# diff.  ripgrep, diffutils and tree back those; the rest is for a human reading a
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

# --- The scan ----------------------------------------------------------------
# pytest is the scan's runner and `swerefactor.pytest_module` turns each collected
# test into one check, so the findings arrive keyed by node id.  Pinned by hash like
# everything else in this benchmark: a scan whose collection count moved between
# builds would change what the reviewer is told without anyone editing a file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

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
sys.exit('prompt.txt has no {{findings}} placeholder, so the scan would run and \
its output would reach nobody') if 'findings' not in found else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in the
# rendered prompt -- the reviewer would be told nothing and have no way to know it
# was told nothing.  Collection runs against the empty mount points, which is enough
# to execute every import and every parametrize call; the floor is below the 145 a
# real pair of trees yields because the lists parametrized off State A collapse to
# their fallbacks when /opt/original is empty.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 90

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
