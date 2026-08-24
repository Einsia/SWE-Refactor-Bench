# =============================================================================
# SWERefactorBench -- fw07-graphhopper-dropwizard-to-springboot
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/fw07-audit:1 \
#         tasks/fw07-graphhopper-dropwizard-to-springboot/tests/audit
#
# A reviewer's workbench.  It holds a model client, tools for reading code, and
# two trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts, not image layers, because the same image reviews every
# submission for this task.  The original is mounted read-only and the workspace is
# a copy: a review must not be able to change what stage 2 later measures, and it
# must not be able to change the thing it is comparing against.
#
# There is deliberately no JDK here and no Maven.  On a task whose subject is which
# framework dispatches an HTTP request, that omission is the point rather than an
# oversight -- and it is a sharper omission here than on a smaller task, because
# this repository comes with a 350 MB local .m2 and a `mvnw` that would otherwise
# make a build one command away.  This stage decides whether the migration happened
# by reading two trees; an image that could resolve artifacts could be talked into
# grading a build log, and a build log on this task says almost nothing -- a servlet
# mounted on /* that reflects over the submission's own @Path annotation compiles,
# packages, and answers every request in stage 2's corpus.  The absence also removes
# the only way a scan could accidentally become an execution: there is no javac, no
# `mvn dependency:tree`, nothing that loads a submitted class.  Whether the project
# builds and answers correctly is stage 2's question, in an image equipped to ask it;
# whether the two builds disagree anywhere is stage 3's.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It runs
# `scan.toml`: 114 file-reading checks whose output is substituted into the prompt as
# findings for the reviewer to follow up.  They import nothing from the submission --
# `srbscan` opens files, parses import blocks and pom XML, and compares the two trees
# as text -- and Python cannot link a Java class even if a check tried, which is the
# property that keeps a scan a scan.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by reading
# only; the tools available are list_dir, fetch_source, search and diff.  The
# tree-walking utilities below are for a human debugging a transcript.
#
# Network: this stage needs to reach the model API and nothing else.  It is the one
# stage that is not offline, which is why it gets no submission runtime -- there is
# no point in the container where submitted code executes.
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
# transcript by hand afterwards.  Note what is not here: no default-jre, which the
# `file` and `jq` line might tempt someone to add for looking inside a jar.  There
# are no jars to look inside -- both mounts are source trees.
RUN apt-get -o Acquire::Retries=8 update \
    && apt-get -o Acquire::Retries=8 install -y --no-install-recommends \
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
# swerefactor.models talks to the API over urllib, so there is no vendor SDK here and
# nothing to drift.  The credential arrives as an environment variable at run time
# and is never baked into a layer.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The scan ----------------------------------------------------------------
# pytest is the scan's runner and `swerefactor.pytest_module` turns each collected test
# into one check, so the findings arrive keyed by node id.  Pinned by hash like
# everything else in this benchmark: a scan whose collection count moved between
# builds would change what the reviewer is told without anyone editing a file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# data/retired-stack.txt is the input three stages share, and it is the reason
# `closure` can be written without naming Dropwizard in a single check: every
# parametrized case there comes from this file.  It is counted at build time because
# a truncated copy would silently shrink the scan instead of failing it -- fewer
# checks, no error, and a reviewer told less about this submission than about the
# last one.
#
# The two kinds of line are counted separately because they are consumed by different
# things and a copy that lost one kind would still look plausible: `group` prunes jars
# in environment/prune-m2.sh, `package` drives the import scan here.  Losing every
# `package` line would leave a scan that collects, runs, and reports no retired
# imports on a tree that is still pure Dropwizard.
#
# The counts strip comments.  This file carries a long header explaining why Jetty,
# argparse4j and the jakarta.* specification APIs are deliberately absent, so
# `grep -c .` would count prose.
RUN set -eu; \
    f=/tests/audit/data/retired-stack.txt; \
    test -s "$f" || { echo "FATAL: $f is missing or empty" >&2; exit 1; }; \
    grep -vE '^[[:space:]]*(#|$)' "$f" > /tmp/stack.lst; \
    g=$(awk '$1 == "group"   {n++} END {print n + 0}' /tmp/stack.lst); \
    p=$(awk '$1 == "package" {n++} END {print n + 0}' /tmp/stack.lst); \
    [ "$g" = 5 ] || { echo "FATAL: expected 5 group lines, found $g" >&2; exit 1; }; \
    [ "$p" = 7 ] || { echo "FATAL: expected 7 package lines, found $p" >&2; exit 1; }; \
    o=$(awk '$1 != "group" && $1 != "package" {print}' /tmp/stack.lst); \
    [ -z "$o" ] || { echo "FATAL: unknown kind in retired-stack.txt: $o" >&2; exit 1; }; \
    echo "retired stack: $g group prefixes, $p package prefixes"; \
    sed 's/^/  /' /tmp/stack.lst; \
    rm -f /tmp/stack.lst

# The prompt is this task's most load-bearing file: it is what replaced the regex
# gate list.  Check at build time that it is present, that it is prose rather than a
# stub, and that its placeholders are ones the runner fills -- an unknown placeholder
# survives into the transcript verbatim, which is visible but late.
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

# The gate questions live in evaluation.toml, which is this context's parent and so is
# not copied into the image -- the runner reads it outside and substitutes the
# rendered text into {{gates}}.  That makes the placeholder load-bearing: a brief
# without it still gets the seven gate ids in the tool schema, and the model answers
# them from their titles alone.
#
# On this task that failure mode is specific and expensive.  `spring_does_the_
# dispatching` reads, as a title, like a question a grep can answer -- and a reviewer
# working from the title alone will accept @GetMapping annotations as the answer.  The
# body in evaluation.toml is where it says that annotations present is not the
# question, that what matters is whether the container maps them, and that the
# failure to look for is one class holding every path with a switch inside it.  Same
# for `configuration_is_bound`, whose body carries the only statement anywhere that
# re-reading the YAML by hand into a Map is the shortcut to look for; and for
# `own_tests_were_ported`, whose title sounds satisfiable by the directory existing.
#
# Checked separately from the placeholder validation above, which only requires every
# placeholder present to be a known one and would pass a brief with none.
RUN python -c "\
import pathlib, sys; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt has no {{gates}} placeholder, so the gate questions in \
evaluation.toml would never reach the reviewer') if '{{gates}}' not in text else None; \
print('gate rendering ok: {{gates}} present')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in the
# rendered prompt -- the reviewer would be told nothing and have no way to know it was
# told nothing.  Collection runs against the empty /opt/workspace mount point, which
# is enough to execute every import and parametrize call.
#
# Note what that empty-tree collection proves and what it does not.  The parametrized
# cases come from data/retired-stack.txt and from constants inside the modules, not
# from the tree, so the count is stable before either mount arrives.  A scan that
# derived its cases from the submission would collect a different number of checks per
# submission, which is a property this benchmark does not want -- the reviewer should
# be told the same things about every tree.  What it cannot prove is that a mounted
# tree is readable: every check here is differential, and `srbscan._require_tree`
# raises INFRASTRUCTURE FAULT against an empty original, which is exactly the state at
# build time.  That guard exists because against an empty original the differential
# questions invert -- "an import State A did not have" is true of every import -- and
# a reviewer would receive a full page of confident, phantom findings.
#
# The floor is 105 against a current collection of 114: high enough that losing a
# module trips it -- the smallest, `wiring`, is 7 -- loose enough that merging or
# splitting a handful of checks does not require editing a Dockerfile in the same
# commit.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 105

# The two mount points exist in the image so a misconfigured run fails with an empty
# directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

ENV SRB_REPO=/opt/workspace \
    SRB_ORIGINAL=/opt/original \
    SRB_RETIRED_STACK=/tests/audit/data/retired-stack.txt

WORKDIR /tests/audit
