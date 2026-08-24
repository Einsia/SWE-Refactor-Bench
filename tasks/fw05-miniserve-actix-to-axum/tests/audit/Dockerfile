# =============================================================================
# SWERefactorBench -- fw05-miniserve-actix-to-axum
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/fw05-audit:1 \
#         tasks/fw05-miniserve-actix-to-axum/tests/audit
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
# There is deliberately no Rust toolchain here, and for this task that is worth
# spelling out.  The subject is a Rust program; the temptation is to put cargo in
# the image so the review can run `cargo tree` and settle the dependency question
# mechanically.  Two reasons not to.  A `cargo` invocation on a submitted
# manifest executes that submission's `build.rs`, which makes this the one stage
# where submitted code could run with the model's credential in the environment.
# And `cargo tree` answers "what does the lockfile resolve to", which the scan
# below already answers by reading the lockfile, while the gates ask what the
# code *does* -- a question no build produces an answer to.  Whether the project
# compiles and what it serves is stage 2's business, in an image equipped to ask.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It
# runs `scan.toml`: 142 file-reading checks whose output is substituted into the
# prompt as findings for the reviewer to follow up.  They import nothing from the
# submission -- `srbscan` opens files, projects Rust source into code-only and
# comment-only forms, and scans manifests line by line -- and there is no Rust
# here that could build one if it tried, which is the property that keeps a scan
# a scan.
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

# The retired crate list, which the scan reads to find out what "retired" means.
# It is the same file the offline registry was built from -- `data/warmup/` is a
# copy of `environment/warmup/` and `swerefactor validate` compares them byte for
# byte -- so the eighteen names the environment made unreachable are the eighteen
# names the scan looks for, without a second list to keep in step.
RUN mkdir -p /opt/srb \
    && cp /tests/audit/data/warmup/retired-crates.txt \
          /opt/srb/retired-crates.txt
ENV SRB_RETIRED=/opt/srb/retired-crates.txt

# The list has to be here and it has to be whole, and neither is visible in a
# run that goes wrong.  `srbscan.retired` skips when the file is absent, and
# seven closure checks skip with it -- the seven that ask whether any retired
# crate is still declared, pinned, patched or named, which is the greater part of
# what the old_stack_retired gate is given to work with.  A skip is not a failure
# and does not reach the digest, so the reviewer would be told nothing about the
# dependency closure and would have no way to know it was told nothing.  So the
# count is asserted, and so are the three names that are not spelled "actix":
# `awc`, `local-channel` and `local-waker` are the ones a hand-edited list loses,
# because they do not look like they belong to the family they are part of.
RUN python -c "\
import sys; \
sys.path.insert(0, '/tests/audit/lib'); \
import srbscan; \
names = srbscan.retired_names(); \
sys.exit(f'retired list holds {len(names)} names, expected 18') if len(names) != 18 else None; \
missing = [n for n in ('awc', 'local-channel', 'local-waker') if n not in names]; \
sys.exit(f'retired list is missing the non-actix names: {missing}') if missing else None; \
print(f'retired list ok: {len(names)} names, {sum(1 for n in names if \"actix\" in n)} of them actix-*')"

# The prompt is this task's most load-bearing file: it is what replaced the
# regex gate list.  Check at build time that it is present, that it is prose
# rather than a stub, and that its placeholders are ones the runner fills -- an
# unknown placeholder survives into the transcript verbatim, which is visible
# but late.
RUN python -c "\
import re, sys, pathlib; \
from swerefactor import audit; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt is too short to be a review brief') if len(text) < 2000 else None; \
found = set(re.findall(r'{{\s*([a-z_]+)\s*}}', text)); \
unknown = sorted(found - set(audit.PLACEHOLDERS)); \
sys.exit(f'prompt.txt uses unknown placeholder(s): {unknown}') if unknown else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to
# know it was told nothing.  Collection runs against the empty /opt/workspace
# mount point, which is enough to execute every import and parametrize call; all
# 142 checks are parametrized over constants in the modules rather than over
# anything found in the tree, so the count does not depend on the submission.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 130

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
