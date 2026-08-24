# =============================================================================
# SWERefactorBench -- fw04-chartmuseum-gin-to-chi
# Stage 2 image: the behavioural suite.
#
#     docker build -t srb/fw04-behavioural:1 tasks/fw04-.../tests/behavioural
#
# Build context is this directory, so the suite is in the image and nothing else
# is.  The agent container never sees it.
#
# What makes this a measurement rather than a claim: the image carries its OWN
# offline module mirrors -- rebuilt here from the same pinned go.mod/go.sum the
# environment image used -- and the `build` module recompiles the submission from
# source against one of them before any other module runs.  Nothing the agent left
# behind is used: vendor/, bin/ and testbin/ are removed from a copy of the tree
# first, so a vendored Gin cannot survive the trip.
#
# There are TWO mirrors, and which one is built against is the whole of what this
# image changed on 2026-08-04:
#
#   /opt/goproxy         COMPLETE.  Every module State A's go.sum verified, the
#                        retired Gin stack included.  Everything here builds
#                        against this one, because State A imports Gin and State A
#                        is the oracle every expectation in data/responses.json was
#                        recorded from.  Graded against the pruned mirror, State A
#                        scored 0/40; against a complete one, 40.000/40.  With the
#                        mirror pruned, `build` cannot resolve, so it leaves scored
#                        checks unanswered and the stage pays nothing, and every
#                        module downstream is left to fail on its own evidence
#                        against a tree with no binary.  Either way a behaviour-
#                        preservation suite whose own baseline cannot build is not
#                        measuring preservation.
#   /opt/goproxy-pruned  the AGENT's mirror, reproduced: no .zip for
#                        github.com/gin-gonic/gin, github.com/gin-contrib/size or
#                        github.com/zsais/go-gin-prometheus.  Nothing builds
#                        against it; the `build` module reads it for one unscored
#                        observation.
#
# This does not weaken the dependency boundary, because the boundary was never
# here.  The AGENT's mirror is still pruned, so a tree that keeps importing Gin
# cannot be compiled or tested while the work is being done -- a stronger gate than
# any grader check, since it bites at authoring time.  And whether the rewrite
# happened is stage 1's question, which it answers across the import graph, both
# manifests, vendor/ and the replace directives, with a gate failure scoring the
# whole submission zero before this stage runs.  Both mirrors are proven by
# construction below rather than asserted in prose.
#
# What this image deliberately does NOT contain
# ---------------------------------------------
# State A's binary.  Every module here compares against data/responses.json --
# expectations frozen at construction time -- so no reference implementation is
# needed at grading time, and shipping one would put a working Gin binary inside
# the image whose job is to establish that Gin is gone.  Stage 3 is the only image
# that builds both trees, and it is a separate context for exactly that reason.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                          (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz      (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# Pinned by digest, like the environment image, so the submission is graded on the
# same Go toolchain it was developed against.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# golang:1.24 -- go1.24.13, the same digest environment/Dockerfile pins.
FROM golang@sha256:d2d2bc1c84f7e60d7d2438a3836ae7d0c847f4888464e7ec9ba3a1339a1ee804

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=mod \
    GOPATH=/opt/go \
    GOMODCACHE=/opt/go/pkg/mod \
    GOCACHE=/opt/gocache \
    GOSUMDB=off \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        file \
        xxd \
        make \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- The runner's Python stack, hash-verified --------------------------------
# In a venv rather than --break-system-packages, and that is not a style
# preference.  Debian's python3-packaging ships 25.0 with no RECORD file, so pip
# refuses to replace it with the pinned 24.2 ("Cannot uninstall packaging 25.0
# ... no RECORD file was found") and the build fails.  Measured while building
# the construction-time equivalent of this image.
COPY requirements-runner.txt /opt/pins/requirements-runner.txt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-runner.txt \
 && /opt/venv/bin/python -c \
        "import pytest, yaml; print('runner stack:', pytest.__version__, '/ PyYAML', yaml.__version__)"

# The Go toolchain stays first on PATH: four modules shell out to `go` and
# `make`, and the runner's interpreter is reached explicitly through
# /opt/venv/bin.
ENV PATH=/usr/local/go/bin:/opt/go/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# --- helm 3.8.0, pinned by digest --------------------------------------------
# The `own_tests` module runs the repository's own Go suite, and that suite's
# setup script (scripts/setup-test-environment.sh, driven by `make
# setup-test-environment`) fetches helm 3.8.0 into ./testbin.  This stage has no
# network, so the binary is pre-placed from this copy and the script's own
# `if [ ! -f testbin/helm ]` skips the download -- the repository's script is left
# untouched and still does the packaging work it exists to do.
#
# Same version and same digest as the environment image.  A skew would change
# what `helm package` writes during that module and turn a passing suite into a
# failing one for a reason having nothing to do with the submission.
ENV HELM_BIN=/opt/helm/helm
RUN set -eux; \
    mkdir -p /opt/helm; \
    cd /tmp; \
    curl -fsSLO https://get.helm.sh/helm-v3.8.0-linux-amd64.tar.gz; \
    echo "8408c91e846c5b9ba15eb6b1a5a79fc22dd4d33ac6ea63388e5698d1b2320c8b  helm-v3.8.0-linux-amd64.tar.gz" | sha256sum -c -; \
    tar xzf helm-v3.8.0-linux-amd64.tar.gz; \
    mv linux-amd64/helm /opt/helm/helm; \
    chmod +x /opt/helm/helm; \
    rm -rf /tmp/linux-amd64 /tmp/helm-v3.8.0-linux-amd64.tar.gz; \
    /opt/helm/helm version --short; \
    ln -s /opt/helm/helm /usr/local/bin/helm

# --- The lockfiles this mirror is built from ---------------------------------
# Copies of State A's go.mod/go.sum and of the same ban list the environment
# image used, digested here against the values recorded at construction time.
#
# The digests are the whole point.  The two mirrors are built by two different
# scripts in two different build contexts, and if their inputs ever drift the
# symptom is a submission that resolved fine for the agent and fails to resolve
# here, naming a module the agent never touched.  Failing this build instead
# makes that a construction error, which is where it belongs.
ENV SRB_GOMOD_SHA256=b0d132a3794999b93bcd8e994ce8319cc0b737b9f9028fb6d0ac50128c478369 \
    SRB_GOSUM_SHA256=8eed76cca471be7b9b0da971fcf88544fb175efede2f3c20473723304272c544 \
    SRB_RETIRED_SHA256=81a24df3452a58a20448d7a54b144b9f1bf5f7157b5dd3b06bee05789f2e7034

COPY data/lockfiles/go.mod              /opt/srb/lockfiles/go.mod
COPY data/lockfiles/go.sum              /opt/srb/lockfiles/go.sum
COPY data/lockfiles/retired-modules.txt /opt/srb/retired-modules.txt

RUN set -eu; \
    echo "$SRB_GOMOD_SHA256  /opt/srb/lockfiles/go.mod"      | sha256sum -c -; \
    echo "$SRB_GOSUM_SHA256  /opt/srb/lockfiles/go.sum"      | sha256sum -c -; \
    echo "$SRB_RETIRED_SHA256  /opt/srb/retired-modules.txt" | sha256sum -c -; \
    echo "lockfiles and ban list match the environment image's inputs"

# --- The two offline module mirrors -------------------------------------------
# Rebuilt from those lockfiles, offline-usable.  Network is available during this
# build; it is not available when the mirrors are used.  See lib/build-goproxy.sh
# for why this is a different script from the environment's: that one warms from
# the source tree via `go mod tidy`, and a stage context has no source tree.
#
# /opt/goproxy is COMPLETE and is what every module here builds against.  It
# carries an archive for the retired Gin stack because State A imports it and
# State A is the behavioural oracle this stage replays: with the pruned mirror,
# State A scored 0/40, and 40.000/40 with a complete one (measured 2026-08-04).
# /opt/goproxy-pruned reproduces the agent's mirror
# and is built against by nothing; the `build` module reads it for one unscored
# observation.  Whether the rewrite happened is stage 1's question.
COPY lib/build-goproxy.sh /opt/srb/scripts/build-goproxy.sh
RUN chmod +x /opt/srb/scripts/build-goproxy.sh \
 && /opt/srb/scripts/build-goproxy.sh /opt/srb/lockfiles \
        /opt/goproxy /opt/goproxy-pruned

ENV SRB_GOPROXY_ROOT=/opt/goproxy \
    SRB_GOPROXY_PRUNED=/opt/goproxy-pruned \
    SRB_RETIRED_MODULES=/opt/srb/retired-modules.txt \
    GOPROXY=off \
    GONOSUMDB= \
    GOPRIVATE=

# Each mirror against its own role.  The complete one must serve every retired
# archive, or State A cannot be built and the oracle fails its own suite.  The
# pruned one must serve none of them, or it is not the agent's mirror and the
# build module's retirement note would read "no archive needed" for every tree.
# Both directions have failed silently in this ladder's history.
RUN set -eu; \
    while read -r m; do \
      [ -n "$m" ] || continue; \
      case "$m" in \#*) continue ;; esac; \
      czip=$(find "/opt/goproxy/$m" -name '*.zip' 2>/dev/null | wc -l); \
      pzip=$(find "/opt/goproxy-pruned/$m" -name '*.zip' 2>/dev/null | wc -l); \
      pmod=$(find "/opt/goproxy-pruned/$m" -name '*.mod' 2>/dev/null | wc -l); \
      [ "$czip" != 0 ] || { echo "FATAL: $m has no zip in the COMPLETE mirror; State A cannot build" >&2; exit 1; }; \
      [ "$pzip" = 0 ] || { echo "FATAL: $m has $pzip zip(s) in the PRUNED mirror" >&2; exit 1; }; \
      [ "$pmod" != 0 ] || { echo "FATAL: $m has no .mod, tidy would break" >&2; exit 1; }; \
      printf '  %-45s complete.zip=%s pruned.zip=%s pruned.mod=%s\n' "$m" "$czip" "$pzip" "$pmod"; \
    done < /opt/srb/retired-modules.txt; \
    test -d /opt/goproxy/github.com/go-chi/chi; \
    echo "mirrors: chi present, every retired module archived in complete and absent from pruned"

# Proven by construction rather than asserted in prose, in both directions: a
# scratch module that requires Gin builds against the complete mirror and does
# not build against the pruned one.  The first half is the identity property the
# whole stage rests on -- State A imports Gin, so a mirror that cannot compile it
# cannot grade the oracle.
RUN set -eu; \
    d=$(mktemp -d); cd "$d"; \
    printf 'module srb.local/banprobe\n\ngo 1.17\n\nrequire github.com/gin-gonic/gin v1.8.1\n' > go.mod; \
    printf 'package main\n\nimport "github.com/gin-gonic/gin"\n\nfunc main() { _ = gin.New() }\n' > main.go; \
    if ! GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy" GOSUMDB=off GONOSUMDB= \
       GOMODCACHE="$d/mod1" go build ./... > /tmp/ok.log 2>&1; then \
        echo "FATAL: gin is NOT buildable from the complete mirror, so State A is not either" >&2; \
        sed -n 1,40p /tmp/ok.log >&2; exit 1; \
    fi; \
    echo "the complete mirror builds gin (State A's own dependency)"; \
    rm -rf "$d/mod1"; \
    if GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy-pruned" GOSUMDB=off GONOSUMDB= \
       GOMODCACHE="$d/mod2" go build ./... > /tmp/ban.log 2>&1; then \
        echo "FATAL: gin is buildable from the pruned mirror, which is the agent's" >&2; \
        sed -n 1,40p /tmp/ban.log >&2; exit 1; \
    fi; \
    grep -q 'gin' /tmp/ban.log \
      && echo "the pruned mirror refuses to build gin:" \
      && sed -n 1,3p /tmp/ban.log; \
    cd /; rm -rf "$d" /tmp/ban.log /tmp/ok.log

# --- The frozen fixture tree --------------------------------------------------
# The same committed tree the environment image publishes, installed by the same
# script, asserted against the same aggregate digest.  Both are load-bearing:
# index.yaml embeds the sha256 of the chart bytes and the `index` module compares
# those digests against expectations captured in the environment image, so a
# fixture that differed by one byte between the two images would fail a correct
# submission on every index response.
#
# `helm package` cannot produce these reproducibly -- chartutil.Save stamps
# time.Now() into every tar header -- which is why they are frozen once at
# construction time and copied, never rebuilt.  See lib/install-testdata.sh.
ENV SRB_TESTDATA_AGGREGATE=6d888ec7db7d0a6944d7507ffd977f775457f09976859db33ca28c86cc496593
COPY data/testdata-frozen /opt/srb/testdata-frozen
COPY lib/install-testdata.sh /opt/srb/scripts/install-testdata.sh
RUN chmod +x /opt/srb/scripts/install-testdata.sh \
 && /opt/srb/scripts/install-testdata.sh /opt/srb/testdata-frozen \
        /opt/testdata /opt/webtemplate

# Names taken from lib/harness/launch.py, which reads CM_ORACLE_CHARTS and
# CM_WEB_TEMPLATES (plural).  Both default to these same paths in code; set here
# so the values are visible in `docker inspect` rather than only in a default.
ENV CM_ORACLE_CHARTS=/opt/testdata \
    CM_WEB_TEMPLATES=/opt/webtemplate

# data/responses.json records the fixture aggregate it was captured against, and
# the suite refuses to run on a mismatch rather than reporting a hundred digest
# failures.  Checked here too, at build time, so the mismatch is a build error.
RUN set -eu; \
    got=$(cat /opt/testdata/FIXTURES.aggregate); \
    [ "$got" = "$SRB_TESTDATA_AGGREGATE" ] || { \
        echo "FATAL: fixtures aggregate to $got, expected $SRB_TESTDATA_AGGREGATE" >&2; \
        exit 1; }; \
    echo "fixtures match the aggregate the environment image installed: $got"

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the mirror.
COPY . /tests/behavioural

RUN set -eu; \
    chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh; \
    rm -rf /tests/behavioural/__pycache__ /tests/behavioural/*/__pycache__; \
    test -f /tests/behavioural/data/responses.json; \
    test -f /tests/behavioural/data/gotest-baseline.json; \
    echo "suite installed"

ENV SRB_GOTEST_BASELINE=/tests/behavioural/data/gotest-baseline.json

# Prove the suite is internally consistent before any submission arrives: the
# corpus imports, the recording parses, and the recording was captured against
# THESE fixtures and THIS corpus.  A fingerprint mismatch here means the corpus
# was edited after the capture, which would silently grade a different set of
# requests than the one that was measured.
RUN /opt/venv/bin/python - <<'PY'
import json, sys
sys.path.insert(0, "/tests/behavioural/lib")
from harness import cli as cli_corpus
from harness import corpus

rec = json.load(open("/tests/behavioural/data/responses.json"))
cases = sum(len(p.cases) for p in corpus.PROFILES)
print(f"corpus    : {len(corpus.PROFILES)} profiles, {cases} cases, "
      f"fingerprint {corpus.fingerprint()}")
print(f"cli       : {len(cli_corpus.CLI_CASES)} cases, "
      f"fingerprint {cli_corpus.fingerprint()}")
print(f"recording : {len(rec['profiles'])} profiles, {len(rec['cli'])} cli records")

problems = []
if rec["corpus_fingerprint"] != corpus.fingerprint():
    problems.append(f"corpus fingerprint {corpus.fingerprint()} != "
                    f"the recording's {rec['corpus_fingerprint']}")
if rec["cli_fingerprint"] != cli_corpus.fingerprint():
    problems.append(f"cli fingerprint {cli_corpus.fingerprint()} != "
                    f"the recording's {rec['cli_fingerprint']}")
want = open("/opt/testdata/FIXTURES.aggregate").read().strip()
if rec["fixtures_aggregate"] != want:
    problems.append(f"the recording was captured against fixtures "
                    f"{rec['fixtures_aggregate']}, image has {want}")
recorded = sum(len(p.get("cases", {})) for p in rec["profiles"].values())
if recorded != cases:
    problems.append(f"the recording holds {recorded} cases, corpus declares {cases}")
if problems:
    sys.exit("FATAL: the suite and its recording disagree:\n  "
             + "\n  ".join(problems))
print(f"the recording agrees with the corpus on all {recorded} cases")
PY

# The manifest is the thing most likely to be edited and least likely to be
# tested, so it is checked at build time: a malformed weight, a duplicate id or a
# module directory that does not exist fails the build, not the run.  A missing
# entry point is checked the same way -- a module whose declared command is not
# there scores zero at grading time and looks exactly like a submission that
# broke it.
RUN /opt/venv/bin/python - <<'PY'
import sys
from swerefactor import config

s = config.Suite.load("/tests/behavioural/suite.toml")
problems = []
for m in s.modules:
    d = s.root / (m.dir or f"modules/{m.id}")
    if not d.is_dir():
        problems.append(f"{m.id}: no directory at {d}")
        continue
    entry = m.command[-1] if m.command else "run.sh"
    if not (d / entry).exists():
        problems.append(f"{m.id}: declares {m.command} but {entry} is not in {d}")
# The first module is the one that builds the tree every later module measures, and
# the property is that it leads and carries weight.  A weight-0 lead module would be
# the failure this guards: the build would run, everything after it would depend on
# it, and it would contribute nothing to the number.
if s.modules[0].id != "build":
    problems.append(f"the first module is {s.modules[0].id}, not build; nothing "
                    f"else can be measured without a build")
elif s.modules[0].weight <= 0:
    problems.append("the build module carries no weight; it is what every later "
                    "module depends on and must count for something")
if problems:
    sys.exit("FATAL: suite.toml does not match the tree:\n  " + "\n  ".join(problems))
print(f"suite: {len(s.modules)} modules, weight total {s.weight_total:g}, "
      f"first is {s.modules[0].id} (weight {s.modules[0].weight:g})")
PY

# Collect every pytest module without running one.  A collection error at grading
# time reads as a zero for that module, so it is found here instead.
#
# The count is asserted, not printed.  Piping pytest into `tail` hands the RUN
# the tail's exit status, so a collection error scrolls past and the image ships
# anyway; and a module that silently collects a fraction of its tests still
# scores 1.0 on the fraction it kept, which is the failure mode a printed number
# does nothing about.
#
# The per-file counts are summed rather than read off pytest's `N tests collected`
# summary, because `pytest.ini` already carries `-q` and the `-q` below makes it
# `-qq`, at which verbosity that summary is not printed at all.  Parsing it found
# no number, read the empty string as a count, and failed the build with `collected
# no tests` on a collection that had in fact just succeeded.  Summing the `path: N`
# lines is what the sibling suites do and it holds at either verbosity.
RUN set -eu; cd /tests/behavioural; \
    export PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor; \
    out=$(/opt/venv/bin/python -m pytest -c pytest.ini -p srbfixtures \
              --collect-only -q modules 2>&1) || { \
        printf '%s\n' "$out" | tail -20 >&2; \
        echo "FATAL: collection failed" >&2; exit 1; }; \
    got=$(printf '%s\n' "$out" | awk -F': ' '/: [0-9]+$/{s+=$2} END{print s+0}'); \
    [ "$got" = "11510" ] || { \
        printf '%s\n' "$out" | tail -5 >&2; \
        echo "FATAL: collected ${got:-no} tests, expected 11510" >&2; exit 1; }; \
    echo "the eleven pytest modules collect $got tests"

ENV SRB_REPO=/workspace/repo \
    SRB_ORIGINAL=/opt/original \
    SRB_RESULTS=/logs/verifier

WORKDIR /tests/behavioural
