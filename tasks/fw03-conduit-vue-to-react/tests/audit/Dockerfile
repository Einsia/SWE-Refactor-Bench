# =============================================================================
# SWERefactorBench -- fw03-conduit-vue-to-react
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/fw03-audit:1 \
#         tasks/fw03-conduit-vue-to-react/tests/audit
#
# A reviewer's workbench.  It holds a model client, tools for reading code, and
# two trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts, not image layers, because the same image reviews every
# submission for this task.  The original is mounted read-only and the workspace
# is a copy: a review must not be able to change what stage 2 later measures, and
# it must not be able to change the thing it is comparing against.
#
# There is deliberately no Node, no npm, no npm cache and no browser here.  This
# stage decides whether the migration happened by reading the two trees, and code
# that has to be built to be judged invites the review to grade a build log
# instead of an implementation.  Whether the project installs, builds and renders
# is stage 2's question, in an image equipped to ask it -- and the absence of a
# toolchain here is what makes the read-only claim true rather than promised.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It
# runs `scan.toml`: 129 file-reading checks whose output is substituted into the
# prompt as findings for the reviewer to follow up.  They import nothing from the
# submission -- `srbscan` opens files, blanks comments and matches patterns -- and
# there is no interpreter here that could satisfy `import vue` or run `vite
# build` if one tried, which is the property that keeps a scan a scan.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by
# reading only; the tools available are list_dir, fetch_source, search and diff. The
# tree-walking utilities below are for a human debugging a transcript.
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

# ripgrep and diffutils back the search and diff tools; the rest is for reading a
# transcript by hand afterwards.
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
# test into one check, so the findings arrive keyed by node id.  Pinned by hash
# like everything else in this benchmark: a scan whose collection count moved
# between builds would change what the reviewer is told without anyone editing a
# file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# Nothing that could build or run the submission may be installed above this
# line.  Asserted rather than trusted, because the claim in this file's header is
# the whole reason stage 1 is allowed to be a model reading code.
RUN set -eu; \
    for tool in node npm npx yarn pnpm bun vite; do \
        if command -v "$tool" >/dev/null 2>&1; then \
            echo "stage 1 has $tool on PATH; a review that can build grades a build" >&2; \
            exit 1; \
        fi; \
    done; \
    echo "no submission toolchain present: ok"

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# The prompt is this task's most load-bearing file: it is what replaced the regex
# gate list.  Check at build time that it is present, that it is prose rather
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
missing = sorted({'findings', 'gates'} - found); \
sys.exit(f'prompt.txt never substitutes {missing}') if missing else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to
# know it was told nothing.  Collection runs against the empty /opt/workspace
# mount point, which is enough to execute every import and every parametrize
# list, which is where a scan of this kind breaks: a typo in a pattern, a missing
# helper, a module that shadows a fixture name.
#
# The floor is the second half of the same argument. A scan that collects one
# check per module has also gone quiet, and quietly. 129 collect today.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 120

# The two decisions in `srbscan` that the modules cannot check for themselves,
# because a module that got either one wrong would report nothing and look
# clean: whether a single-file component is still recognised by shape, and
# whether prose and source are being blanked differently.  Both are one line to
# break and neither shows up as a failing check.
RUN python /tests/audit/lib/selfcheck.py

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
