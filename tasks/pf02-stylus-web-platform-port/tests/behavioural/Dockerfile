# =============================================================================
# SWERefactorBench -- pf02-stylus-web-platform-port
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/pf02-behavioural:1 tasks/pf02-.../tests/behavioural
#
# The build context is this directory, so the suite is in the image and nothing
# else is.  The agent container never sees it.
#
# What makes this a measurement rather than a claim: the image carries State A --
# unmodified Stylus 0.63.0 with its dependencies installed -- and runs it in the
# same container, on the same machine, in the same run, to produce the expected
# answer for every one of the ~2,500 checks.  There is no table of recorded
# output.  A submission cannot be tuned to a golden file because there is no
# golden file to tune to, and the suite cannot drift away from upstream behaviour
# because upstream behaviour is what it asks.
#
# Nothing here reads the submission's source text.  Every module starts an
# artifact and watches what comes out.  The checks that read code are in
# ../audit, which sees both trees and starts neither.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-only mount)
#     /opt/original      State A as shipped, without an install (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# Pinned by digest, like the environment image, so the grader cannot drift.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM node:22.14.0-bookworm-slim@sha256:1c18d9ab3af4585870b92e4dbc5cac5a0dc77dd13df1a5905cea89fc720eb05b

# The same two payloads, with the same digests, that environment/Dockerfile
# unpacks as the agent's starting tree.  One archive and one digest for both
# images: "unchanged relative to what you were given" must not be able to drift
# into "unchanged relative to some other copy of upstream".
ARG REPO_SHA256=919ce95f40ea747fd1b0fd5b8dd40c7f40fdfa8cb5102368df2343df3f6f4221
ARG NPM_CACHE_SHA256=fc1947e124174fb27a32d6f6ec6e0eab3dc7e7b456e529827985d2d1550f8d4b

# The apt version on bookworm, asserted below rather than merely declared.
ARG PYTEST_VERSION=7.2.1

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_CACHE=/opt/npm-cache

# --- toolchain ---------------------------------------------------------------
# python3-pytest from apt rather than pip: this image is built with network
# access but should not depend on PyPI resolution to be reproducible, and the
# distribution pins the version.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 \
      python3-pytest \
      python3-pytest-timeout \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/UTC /etc/localtime \
 && echo UTC > /etc/timezone

ARG PYTEST_VERSION
RUN set -eu; \
    got="$(python3 -m pytest --version 2>&1 | head -1 | awk '{print $2}')"; \
    [ "$got" = "$PYTEST_VERSION" ] || { \
        echo "FATAL: pytest is $got, this image is pinned to $PYTEST_VERSION" >&2; \
        exit 1; \
    }; \
    python3 -c "import pytest_timeout" || { \
        echo "FATAL: pytest-timeout is missing; pytest.ini sets a per-test timeout" >&2; \
        exit 1; \
    }; \
    echo "pytest $got, as pinned"

# The verifier's node must be the agent's node.  The realm loads the submission's
# ESM through `vm.SourceTextModule`, which is still behind a flag, and both its
# availability and its semantics are version-specific: a submission that works on
# the agent's runtime and not on the verifier's would be failed for the verifier's
# reason.  Both images derive from the digest above; this asserts it rather than
# trusting it.
RUN set -eu; \
    want=v22.14.0; \
    got="$(node --version)"; \
    [ "$got" = "$want" ] || { echo "FATAL: verifier node is $got, expected $want" >&2; exit 1; }; \
    node --experimental-vm-modules -e 'const vm=require("node:vm"); \
      if (typeof vm.SourceTextModule !== "function") throw new Error("no vm.SourceTextModule")'

# --- State A -----------------------------------------------------------------
WORKDIR /opt/payload
COPY data/original.tar.gz data/npm-offline-cache.tar.gz data/SHA256SUMS ./
RUN sha256sum -c SHA256SUMS \
 && echo "${REPO_SHA256}  original.tar.gz" | sha256sum -c - \
 && echo "${NPM_CACHE_SHA256}  npm-offline-cache.tar.gz" | sha256sum -c -

# State A with its dependencies, able to run.  The suite drives it through
# `bin/stylus` and through `lib/js/oracle.cjs`, which requires `./lib`.
#
# The npm cache stays in the image after this, because the `prepare` module
# installs the *submission's* lockfile from it, offline.  It holds exactly what
# State A declared, so a submission that added a dependency does not install here
# at all -- which is the no-network rule being enforced rather than stated.
RUN set -eu; \
    tar -xzf original.tar.gz -C /opt/payload; \
    mv /opt/payload/repo /opt/state-a; \
    mkdir -p /opt/npm-cache; \
    tar -xzf npm-offline-cache.tar.gz -C /opt; \
    mv /opt/npmcache/* /opt/npm-cache/ 2>/dev/null || true; \
    rmdir /opt/npmcache 2>/dev/null || true; \
    cd /opt/state-a; \
    npm ci --offline --no-audit --no-fund; \
    rm -f /opt/payload/original.tar.gz /opt/payload/npm-offline-cache.tar.gz

# Read-only from here.  State A is an input to every check, and a run that
# mutated it would measure later checks against a reference earlier ones had
# already altered.
RUN chmod -R a-w /opt/state-a

# --- prove State A answers ---------------------------------------------------
# Before the suite is copied in.  A broken reference is an unusable image, and
# finding that out at verify time costs a trial to learn something knowable now.
RUN set -eu; \
    cd /opt/state-a; \
    v="$(node -p "require('./package.json').version")"; \
    [ "$v" = "0.63.0" ] || { echo "FATAL: State A is stylus $v, expected 0.63.0" >&2; exit 1; }; \
    out="$(printf 'a\n  color red\n' | node ./bin/stylus)"; \
    echo "$out" | grep -q '#f00' || { echo "FATAL: State A CLI printed: $out" >&2; exit 1; }; \
    node -e 'const s=require("/opt/state-a"); \
      const css=s.render("a\n  color red\n"); \
      if (css.trim() !== "a {\n  color: #f00;\n}") throw new Error("render: "+JSON.stringify(css));'; \
    n="$(find /opt/state-a/test/cases -name "*.styl" | wc -l)"; \
    [ "$n" -ge 300 ] || { echo "FATAL: only $n .styl inputs under test/cases" >&2; exit 1; }; \
    echo "State A OK: stylus $v, $n case inputs"

# --- the harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- the suite ---------------------------------------------------------------
# Copied last so that editing a module does not rebuild State A.
COPY . /tests/behavioural
RUN rm -rf /tests/behavioural/data /tests/behavioural/lib/port-suites.sh \
 && chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh

# The manifest is the file most likely to be edited and least likely to be
# tested, so it is checked here: a malformed weight, a duplicate id, or a module
# directory that does not exist fails the build rather than the run.
RUN set -eu; \
    python3 -c "\
import sys; \
from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
lead = s.modules[0] if s.modules else None; \
sys.exit('suite.toml declares no modules') if lead is None else None; \
sys.exit(f'the first module is {lead.id}, not prepare: staging is what every later module reads') if lead.id != 'prepare' else None; \
sys.exit('the prepare module carries no weight: a submission that cannot be staged would score on the modules that skip it') if lead.weight <= 0 else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}, lead={lead.id} (weight {lead.weight:g})')"

# --- the premises this suite depends on --------------------------------------
# The `harness` checks ask State A about the assumptions the graded checks rest
# on: that `compress` really does change this output, that these two option rows
# really do differ, that no case names a host path.  They consult the submission
# not at all, which is why pytest.ini deselects them from the graded run -- left
# in, they would pay the same credit to every submission alike, including one
# that compiled nothing.
#
# So they run here instead, against State A and an empty workspace.  A premise
# that has broken fails the build.  The alternative -- discovering it at verify
# time -- means every submission graded by this image was scored against an
# assumption that was no longer true.
#
# $SRB_SUITE_WORK is a scratch directory that gets thrown away; no submission
# exists yet, and none is needed, because these checks never look at one.
#
# Written without a pipe on purpose.  `pytest ... | tail` reports tail's exit
# status, so a piped step could not fail: the premises would run, the output
# would be trimmed, and the build would go green whatever they said.
RUN set -eu; \
    cd /tests/behavioural; \
    SRB_SUITE_DIR=/tests/behavioural \
    SRB_SUITE_WORK=/var/tmp/pf02-selfcheck \
    SRB_WORK=/var/tmp/pf02-selfcheck/work \
    PF03_STATE_A=/opt/state-a \
    PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor \
    python3 -m pytest -c pytest.ini -p srbstylus -m harness modules/; \
    rm -rf /var/tmp/pf02-selfcheck

# The count as well as the outcome.  A refactor that deleted half the premises
# would leave the run above green and this image no longer checked.
RUN set -eu; \
    cd /tests/behavioural; \
    want=162; \
    got="$(SRB_SUITE_DIR=/tests/behavioural \
           SRB_SUITE_WORK=/var/tmp/pf02-count \
           SRB_WORK=/var/tmp/pf02-count/work \
           PF03_STATE_A=/opt/state-a \
           PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor \
           python3 -m pytest -c pytest.ini -p srbstylus -m harness \
             --collect-only -q modules/ 2>/dev/null \
           | awk -F': ' '/: [0-9]+$/{s+=$2} END{print s+0}')"; \
    [ "$got" = "$want" ] || { \
        echo "FATAL: $got premise checks collected, expected $want. If premises were" >&2; \
        echo "added or removed on purpose, update the number here in the same commit." >&2; \
        exit 1; \
    }; \
    rm -rf /var/tmp/pf02-count; \
    echo "$got premise checks, as expected"

# --- the premise the run above cannot reach ----------------------------------
# Everything so far consults State A, and State A is pre-migration, so
# `harness.runners.sandbox()` routes all of it to js/oracle.cjs.  The realm driver
# js/sandbox.mjs -- which answers every *ported* tree, meaning every real
# submission -- is not exercised by any of it, and was not exercised by the
# identity run either, for the same reason.
#
# That blind spot refused 23% of this stage's checks to every submission alike: the
# two faces disagreed about whether `renderer.set('paths', [...])` may take a list,
# the oracle answered those ops and the realm driver refused them, so a flawless
# port could not answer every scored check -- which under an all-or-nothing stage 2
# pays nothing and admits nothing to stage 3.  Nothing in a grading run
# distinguishes that from a bad port; a refused op reports as a behavioural
# failure.
#
# So the shapes are put through both faces here and the two must agree on
# acceptance.  A fixture core stands in for the submission, which is what makes it
# a build-time premise rather than something a submission has to reveal.
RUN set -eu; \
    python3 /tests/behavioural/selftest.py --report /var/tmp/pf02-selftest.json; \
    python3 -c "\
import json, sys; \
r = json.load(open('/var/tmp/pf02-selftest.json')); \
f = r['findings']; \
sys.exit('selftest reported failures') if not r['ok'] else None; \
n = len(f['sandbox']); \
sys.exit(f'selftest exercised {n} ops on the sandbox face; it is not asserting anything') \
    if n < 4 else None; \
sys.exit('selftest ran both faces but compared none of them') \
    if sorted(f['sandbox']) != sorted(f['oracle']) else None; \
p = f['paths-list']; \
sys.exit('selftest passed without checking the realm invariant it exists to keep') \
    if not (p['isArray'] and p['protoIsRealmArray'] and p['reachesRealmFunction']) else None; \
print(f'driver parity: {n} op shapes accepted by both faces')"; \
    rm -f /var/tmp/pf02-selftest.json

# And the graded inventory, for the same reason in the other direction: a module
# that stopped collecting would score zero for the submission rather than fail
# the build, and the report would read as a defect in the port.
#
# Counted per module, not as one total.  A floor on the total is the wrong
# instrument for the thing this step is here to catch: with 2,581 checks across
# twelve modules, `platform` could stop collecting entirely and the total would
# still clear any floor low enough to leave room for ordinary drift.  The
# submission would then lose that module's six weight points for a reason that has
# nothing to do with the submission.  So each module has to collect something, and
# the suite as a whole has to clear a floor -- the second catches a broad
# regression the first would let through one check per module.
RUN set -eu; \
    cd /tests/behavioural; \
    floor=2400; total=0; \
    for dir in modules/*/; do \
        id="$(basename "$dir")"; \
        got="$(SRB_SUITE_DIR=/tests/behavioural \
               SRB_SUITE_WORK=/var/tmp/pf02-count \
               SRB_WORK=/var/tmp/pf02-count/work \
               PF03_STATE_A=/opt/state-a \
               PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor \
               python3 -m pytest -c pytest.ini -p srbstylus \
                 --collect-only -q "$dir" 2>/dev/null \
               | awk -F': ' '/: [0-9]+$/{s+=$2} END{print s+0}')"; \
        if [ "$id" = "prepare" ]; then \
            echo "  $id: not a pytest module, checks written by run.py"; \
            continue; \
        fi; \
        [ "$got" -ge 1 ] || { \
            echo "FATAL: module $id collects no graded checks. It would score 0 for" >&2; \
            echo "every submission, which reads as a defect in the port and is not." >&2; \
            exit 1; \
        }; \
        echo "  $id: $got"; \
        total=$((total + got)); \
    done; \
    [ "$total" -ge "$floor" ] || { \
        echo "FATAL: only $total graded checks collect, below the $floor floor" >&2; \
        exit 1; \
    }; \
    rm -rf /var/tmp/pf02-count; \
    echo "$total graded checks collect across $(ls -d modules/*/ | wc -l) modules"

# --- non-root ----------------------------------------------------------------
# The suite writes only to the scratch directories the module contract names.
# Running unprivileged means a module that starts a submitted program cannot be
# tricked by it into writing outside them, and State A above stays read-only.
#
# Not UID 1000: the node base image already has a `node` user there. The number
# carries no meaning outside this image, so an unused high one is the fix --
# asserted rather than assumed, because a base image that later adds a second
# user would otherwise reintroduce the collision silently.
RUN set -eu; \
    if getent passwd 1001 >/dev/null; then \
        echo "FATAL: UID 1001 is taken by $(getent passwd 1001 | cut -d: -f1)" >&2; \
        exit 1; \
    fi; \
    useradd --create-home --uid 1001 --shell /bin/bash verifier; \
    mkdir -p /logs/verifier /var/tmp/pf02; \
    chown -R verifier:verifier /logs /var/tmp/pf02; \
    chmod -R a-w /tests/behavioural

# npm writes an index into its cache even for an offline install, so the cache is
# the one thing outside scratch the suite must be able to write.  Handing it to
# the unprivileged user is safe: the container is discarded after the run, and
# what it installs comes from the lockfile, checked against this cache.
RUN chown -R verifier:verifier /opt/npm-cache

USER verifier
ENV HOME=/home/verifier \
    PF03_STATE_A=/opt/state-a
WORKDIR /tests/behavioural
