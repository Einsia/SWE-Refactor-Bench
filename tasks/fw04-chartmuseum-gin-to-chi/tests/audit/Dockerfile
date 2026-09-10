# =============================================================================
# SWERefactorBench -- fw04-chartmuseum-gin-to-chi
# Stage 1 image: the audit review.
#
#     docker build -t srb/fw04-verifier:1 tasks/fw04-.../tests/audit
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
# There is deliberately no Go toolchain here and no module mirror.  On a task
# whose subject is a dependency change that omission is the point, not an
# oversight: this stage decides whether the migration happened by reading two
# trees, and an image that could resolve modules could be talked into grading a
# `go build` log instead of an implementation.  It also removes the only way a
# scan could accidentally become an execution -- there is no `go vet`, no
# `go list`, nothing that loads the submitted packages.  Whether the project
# builds and answers correctly is stage 2's question, in an image equipped to
# ask it; whether the two builds disagree anywhere is stage 3's.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It
# runs `scan.toml`: 94 file-reading checks whose output is substituted into the
# prompt as findings for the reviewer to follow up.  They import nothing from the
# submission -- `srbscan` opens files, parses import blocks and compares the two
# trees as text -- and Python cannot link a Go package even if a check tried,
# which is the property that keeps a scan a scan.
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

# The list of retired modules is the one input the scan and the other two stages
# share, and it is the reason `closure` can be written without naming Gin: every
# parametrized case comes from this file.  Checked here because a truncated copy
# would silently shrink the scan instead of failing it.
RUN set -eu; \
    f=/tests/audit/data/retired-modules.txt; \
    test -s "$f" || { echo "FATAL: $f is missing or empty" >&2; exit 1; }; \
    n=$(grep -c . "$f"); \
    [ "$n" = 3 ] || { echo "FATAL: expected 3 retired modules, found $n" >&2; exit 1; }; \
    echo "retired modules ($n):"; sed 's/^/  /' "$f"

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
sys.exit('prompt.txt has no {{findings}} placeholder, so the scan output would \
be computed and discarded') if 'findings' not in found else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The ban list, at the behavioural stage's path as well as this stage's own.
#
# The two paths are not a choice between them; they are the same bytes reachable
# by either name, and this stage needs that because it does not control which name
# it is handed.  task.toml declares SRB_RETIRED_MODULES=/opt/srb/retired-modules.txt
# in [verifier.env], which is correct for the behavioural image; without the copy
# below it is a path with no file behind it here, and a stage handed that name reads
# nothing.  Reading nothing is not a smaller scan: ten of `closure`'s 37 checks are
# parametrised over this list, and they are the ten that ask whether the retired
# stack is gone.  What is left is 27 checks that pass, which is what a clean tree
# looks like.
#
# Verified against the same digest the behavioural image checks, so the two cannot
# drift into disagreeing about which modules the task retires.
ENV SRB_RETIRED_SHA256=81a24df3452a58a20448d7a54b144b9f1bf5f7157b5dd3b06bee05789f2e7034
COPY data/retired-modules.txt /opt/srb/retired-modules.txt
RUN set -eu; \
    echo "$SRB_RETIRED_SHA256  /opt/srb/retired-modules.txt" | sha256sum -c -; \
    cmp /opt/srb/retired-modules.txt \
        /tests/audit/data/retired-modules.txt; \
    echo "ban list reachable by both names, same bytes"

# Set before the collect-check below, not after it.  The check exists to prove the
# scan collects everything it should, and a check that runs with this variable
# unset proves it for the fallback path while the image ships an override -- it
# would pass a build whose declared list was a typo, and every run of that image
# would quietly measure 27 checks instead of 37.
ENV SRB_REPO=/opt/workspace \
    SRB_ORIGINAL=/opt/original \
    SRB_RETIRED_MODULES=/tests/audit/data/retired-modules.txt

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to
# know it was told nothing.  Collection runs against the empty /opt/workspace
# mount point, which is enough to execute every import and parametrize call.
#
# Note what that empty-tree collection proves and what it does not: the
# parametrized cases in `closure` are generated from data/retired-modules.txt,
# not from the tree, so the count is stable before either mount arrives.  A
# scan that derived its cases from the submission would collect a different
# number of checks per submission, which is a property this benchmark does not
# want -- the reviewer should be told the same things about every tree.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 90

# And the same count with the other name, since that is a configuration a graded
# run can arrive with.  Both must collect the same number: if one of them is
# short, the review it feeds is short in the direction of passing.
RUN SRB_RETIRED_MODULES=/opt/srb/retired-modules.txt \
    bash /tests/audit/lib/collect-check.sh 90

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
