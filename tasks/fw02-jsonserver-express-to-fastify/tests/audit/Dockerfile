# =============================================================================
# SWERefactorBench -- fw02-jsonserver-express-to-fastify
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/fw02-audit:1 \
#         tasks/fw02-jsonserver-express-to-fastify/tests/audit
#
# A reviewer's workbench.  It holds a model client, tools for reading code, and
# two trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts rather than image layers, because the same image
# reviews every submission for this task.  The original is mounted read-only and
# the workspace is a copy: a review must not be able to change what stage 2 later
# measures, and must not be able to change the thing it is comparing against.
#
# There is no node here, and no npm.  That is the load-bearing property of this
# image rather than an omission.  This stage decides whether the migration
# happened by reading two trees; code that has to be built to be judged invites
# the review to grade a build log instead of an implementation, and a container
# that can run `npm ci` is a container where the submission's own code executes
# during its own audit review.  Whether the project installs, boots and
# serves is stage 2's question, asked in an image equipped to ask it.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It
# runs scan.toml: file-reading checks whose output is substituted into the prompt
# as findings for the reviewer to follow up.  They import nothing from the
# submission -- srbscan opens files, matches text and parses JSON -- and there is
# no interpreter here that could satisfy `require('fastify')` if one tried, which
# is the property that keeps a scan a scan.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by
# reading only: the tools are list_dir, fetch_source, search and diff.  The
# tree-walking utilities below are for a human reading a transcript afterwards.
#
# Network: this stage needs to reach the model API and nothing else.  It is the
# one stage that is not offline, which is exactly why it gets no submission
# runtime -- there is no point in this container where submitted code runs.
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
# a transcript by hand afterwards.  Note what is absent: nodejs.
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
# pytest is the scan's runner and swerefactor.pytest_module turns each collected
# test into one check, so findings arrive keyed by node id.  Pinned by hash like
# everything else in this benchmark: a scan whose collection count moved between
# builds would change what the reviewer is told without anyone editing a file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# The prompt is this task's most load-bearing file: it is what replaced the gate
# functions.  Check at build time that it is present, that it is prose rather
# than a stub, and that its placeholders are ones the runner fills -- an unknown
# placeholder survives into the transcript verbatim, which is visible but late.
RUN python -c "\
import re, sys, pathlib; \
from swerefactor import audit; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt is too short to be a review brief') if len(text) < 2000 else None; \
found = set(re.findall(r'{{\s*([a-z_]+)\s*}}', text)); \
unknown = sorted(found - set(audit.PLACEHOLDERS)); \
sys.exit(f'prompt.txt uses unknown placeholder(s): {unknown}') if unknown else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The retired list this stage reads is the same file the environment image
# publishes and the agent was given.  Check the copy in data/ is intact rather
# than trusting that it was copied, since a truncated list would narrow every
# closure finding silently.
RUN python -c "\
import pathlib, sys; \
names = [l.split('#')[0].strip() for l in pathlib.Path('/tests/audit/data/retired-packages.txt').read_text().splitlines()]; \
names = [n for n in names if n]; \
sys.exit(f'retired-packages.txt has only {len(names)} entries') if len(names) < 50 else None; \
missing = [n for n in ('express', '@fastify/express', 'middie', 'connect', 'router', 'supertest') if n not in names]; \
sys.exit(f'retired-packages.txt is missing {missing}') if missing else None; \
print(f'retired-packages.txt ok: {len(names)} distributions')"

# The scan has to collect, here, at build time.  A module that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to
# know it was told nothing.  Collection runs against the empty /opt/workspace
# mount point, which is enough to execute every import and parametrize call.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 150

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
