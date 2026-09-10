# =============================================================================
# SWERefactorBench -- lang03-sqlparse-python-to-go
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang03-audit:1 \
#         tasks/lang03-sqlparse-python-to-go/tests/audit
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
# There is deliberately no Go toolchain here.  This stage answers eleven questions
# -- did the Python leave, is the Go the implementation, does the default path lead
# to it -- and every one of them is settled by reading source and build files.  Give
# the review a compiler and it starts grading build logs, which is stage 2's job in
# an image built to do it: stage 2 builds the submission and the frozen reference
# side by side and compares 8,453 behavioural cases through the built artefacts.
# Those are measurements.  This stage is for the judgement calls no amount of build
# output settles -- chiefly whether 4,024 lines of Python were reimplemented or
# transliterated, and whether the fifty-two regular expressions RE2 cannot compile
# were rewritten or worked around.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It runs
# `scan.toml`: file-reading checks across three modules whose output is substituted
# into the prompt as findings for the reviewer to follow up.  They walk directories,
# hash files and sniff the first bytes of one to see whether it is an object file or
# a zip; nothing here could satisfy a module that tried to compile or import
# anything, which is the property that keeps a scan a scan.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by reading
# only; the tools the runner exposes are list_dir, fetch_source, search and diff.
# ripgrep, diffutils and tree back those; the rest is for a human reading a
# transcript afterwards.
#
# Network: the model API and nothing else.  It is the one stage that is not offline,
# which is the other half of why it gets no toolchain -- there is no point in this
# container where submitted code executes.
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

# That COPY arrives carrying eight `.pyc` files tagged `cpython-310`, because `infra/`
# is a build context with no `.dockerignore` and the author's host runs 3.10.  They are
# dead weight on a 3.11 image -- a tag mismatch means the interpreter never loads them
# -- but they are also a stale-module hazard the moment a name collides, and leaving
# them would mean the assertion at the end of this file had to be scoped around a
# violation it can see.  Stripping here is the alternative to that: the check below
# then covers both trees and has no exceptions to explain.
RUN find /opt/swerefactor -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/swerefactor -name '*.py[co]' -delete

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

# The contract the scan reads is the same file the solver was given.  Stage 2's
# baseline layer checks this digest against its own copy; the two copies exist
# because the two images have separate build contexts, and this line is what keeps
# them from being two different contracts.  A scan reading a stale copy would report
# a package directory missing that the submission was never asked for.
RUN echo '2df0cf4b8486f5acac3d2ae44cdf7d0681be66171d131504d1ca606ac2d12b35  /tests/audit/data/source-contract.json' \
      | sha256sum -c -

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in the
# rendered prompt -- the reviewer would be told nothing and have no way to know it
# was told nothing.
#
# The floor is 160 against the 169 that empty mounts yield and the 189 a real pair of
# trees yields.  That gap is the risk in deriving a parametrize list from a mount: the
# lists taken from State A's own tree collapse to one placeholder item when
# /opt/original is empty, and a module that stopped deriving from the mount would
# report a clean tree against a missing one while still collecting a plausible number.
# So collect-check.sh's second phase points SRB_ORIGINAL at a synthetic tree holding
# four Python modules and asserts the closure module's count follows it.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 160

# No Go toolchain, asserted rather than assumed: the base image is pinned by digest,
# but apt lines get edited and a base image can be repointed.
RUN for forbidden in go gofmt gccgo tinygo; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden -- a review" >&2; \
            echo "that can build starts grading build logs, which is stage 2." >&2; \
            exit 1; \
        fi; \
    done

# And no importable sqlparse, which is this task's own hazard rather than a shared
# one.  The source language is Python and the reviewer's runtime is Python, so the
# reference implementation is one `pip install` away from being on this image's path
# -- at which point a review could answer a behavioural question by running the
# reference, and would be measuring what stage 2 measures, less well.  There is also
# a subtler reason: with sqlparse importable, `python -c 'import sqlparse'` becomes a
# working oracle for "did the Python leave", and it would answer about this image
# instead of about the submission.
RUN if python -c 'import sqlparse' 2>/dev/null; then \
        echo "FATAL: sqlparse is importable on the stage-1 image.  The reference" >&2; \
        echo "must not be reachable from the image that reviews its replacement." >&2; \
        exit 1; \
    fi

# No compiled Python anywhere the harness imports from, and this is the last RUN in
# the file for a reason.  The thing that writes bytecode here is the pytest collection
# in collect-check.sh above; the thing that copies it in is the `COPY . /tests` and the
# `COPY --from=infra` above that.  A check placed before either one passes while the
# image still ships the files, which is the failure this assertion exists to catch, so
# it has to come after every step that could produce one.
#
# `PYTHONDONTWRITEBYTECODE=1` is set at the top of this file and does not make this
# redundant.  It governs what the image writes, not what COPY brings in, and the two
# cases are indistinguishable from inside the Dockerfile except by the interpreter tag
# on the filename.  `.dockerignore` handles the COPY side with `**/`-prefixed patterns;
# this is the line that notices when it stops working.
RUN found=$(find /tests /opt/swerefactor -name '*.py[co]' -o -name '__pycache__' -type d); \
    if [ -n "$found" ]; then \
        echo "FATAL: compiled Python shipped in the stage-1 image:" >&2; \
        echo "$found" >&2; \
        echo "A cpython-310 tag on a 3.11 image means .dockerignore missed it on the" >&2; \
        echo "way in -- check that every pattern is prefixed '**/'." >&2; \
        exit 1; \
    fi

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
