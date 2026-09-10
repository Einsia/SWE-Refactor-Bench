# =============================================================================
# SWERefactorBench -- fw06-uploadserver-gorillamux-to-nethttp
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/fw06-audit:1 \
#         tasks/fw06-uploadserver-gorillamux-to-nethttp/tests/audit
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
# There is deliberately no Go toolchain here and no module mirror.  On a task
# whose subject is a dependency change that omission is the point, not an
# oversight: this stage decides whether the migration happened by reading two
# trees, and an image that could resolve modules could be talked into grading a
# `go build` log instead of an implementation.  It also removes the only way a
# scan could accidentally become an execution -- there is no `go vet`, no
# `go list`, nothing that loads the submitted packages.  Whether the project
# builds and answers correctly is stage 2's question, in an image equipped to ask
# it; whether the two builds disagree anywhere is stage 3's.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It
# runs `scan.toml`: 152 file-reading checks whose output is substituted into the
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

# The two module lists are the inputs the scan and the other two stages share,
# and they are the reason `closure` can be written without naming gorilla/mux in
# a single check: every parametrized case comes from one of these files.  Both
# are counted here because a truncated copy would silently shrink the scan
# instead of failing it -- 27 fewer checks, no error, and a reviewer told less
# about this submission than about the last one.
#
# The counts strip comments, which the equivalent check on the Gin task does not
# have to do.  Both of these files carry a header explaining what "retired" means
# on a task that bans a category rather than a library, so `grep -c .` would
# count prose.
RUN set -eu; \
    d=/tests/audit/data; \
    for f in "$d/retired-modules.txt" "$d/banned-routers.txt"; do \
        test -s "$f" || { echo "FATAL: $f is missing or empty" >&2; exit 1; }; \
    done; \
    grep -vE '^[[:space:]]*(#|$)' "$d/retired-modules.txt" > /tmp/retired.lst; \
    grep -vE '^[[:space:]]*(#|$)' "$d/banned-routers.txt"  > /tmp/banned.lst; \
    n=$(grep -c . /tmp/retired.lst); \
    [ "$n" = 1 ] || { echo "FATAL: expected 1 retired module, found $n" >&2; exit 1; }; \
    m=$(grep -c . /tmp/banned.lst); \
    [ "$m" = 28 ] || { echo "FATAL: expected 28 banned routers, found $m" >&2; exit 1; }; \
    while read -r line; do \
        grep -qxF "$line" /tmp/banned.lst \
        || { echo "FATAL: retired module $line is not in the banned list" >&2; exit 1; }; \
    done < /tmp/retired.lst; \
    echo "retired module(s) ($n):"; sed 's/^/  /' /tmp/retired.lst; \
    echo "banned routers: $m, retired list is a subset"; \
    rm -f /tmp/retired.lst /tmp/banned.lst

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
sys.exit('prompt.txt has no {{findings}} placeholder, so the scan output would \
be computed and discarded') if 'findings' not in found else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The gate questions live in evaluation.toml, which is this context's parent and
# so is not copied into the image -- the runner reads it outside and substitutes
# the rendered text into {{gates}}.  That makes the placeholder load-bearing: a
# brief without it still gets the six gates in the tool schema, and the model
# answers them from their titles alone.  On this task that means answering
# `prefix_rule_is_derived` from the words "not as a list of the corpus's paths",
# without having been told that a correct port necessarily contains a `/files`
# prefix test -- which is the single most likely way to fail a submission that
# did the work.
#
# Checked separately from the placeholder validation above, which only requires
# every placeholder present to be a known one and would pass a brief with none.
RUN python -c "\
import pathlib, sys; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt has no {{gates}} placeholder, so the gate questions in \
evaluation.toml would never reach the reviewer') if '{{gates}}' not in text else None; \
print('gate rendering ok: {{gates}} present')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to
# know it was told nothing.  Collection runs against the empty /opt/workspace
# mount point, which is enough to execute every import and parametrize call.
#
# Note what that empty-tree collection proves and what it does not: the
# parametrized cases come from data/retired-modules.txt and
# data/banned-routers.txt, not from the tree, so the count is stable before
# either mount arrives.  A scan that derived its cases from the submission would
# collect a different number of checks per submission, which is a property this
# benchmark does not want -- the reviewer should be told the same things about
# every tree.
#
# The floor is 140 against a current collection of 152: high enough that losing a
# module trips it, loose enough that adding or merging a handful of checks does
# not require editing a Dockerfile in the same commit.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 140

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

ENV SRB_REPO=/opt/workspace \
    SRB_ORIGINAL=/opt/original \
    SRB_RETIRED_MODULES=/tests/audit/data/retired-modules.txt \
    SRB_BANNED_ROUTERS=/tests/audit/data/banned-routers.txt

WORKDIR /tests/audit
