# =============================================================================
# SWERefactorBench -- fw04-chartmuseum-gin-to-chi
# Stage 3 image: the verification probe.
#
#     docker build -t srb/fw04-verification:1 tasks/fw04-.../tests/verification
#
# Six models, an hour each, trying to write a test the original passes and the
# submission fails.  So this image is the only one that has to build and run BOTH
# servers, and on a task whose subject is a dependency change that has one
# specific consequence: it carries two Go module mirrors.
#
#     /opt/goproxy-baseline   complete.  Every zip, Gin's included.
#     /opt/goproxy            the pruned delivery mirror, no retired zips.
#
# run-candidate.sh selects between them from the role argument.  What that split is
# for, and what it is NOT for:
#
#   * it is for making both trees buildable from their own requirement sets in one
#     offline container.  The original requires Gin and the delivery mirror carries
#     no Gin zip, so a single mirror cannot serve both sides;
#   * the baseline mirror is used with cwd inside the original tree and never
#     inside the submission's;
#   * it is NOT a dependency gate at grade time, and this is the part that reads
#     the other way if left unsaid.  GOMODCACHE is the container default and is
#     shared by both builds, and CandidateRunner runs the original FIRST
#     (verification.py: execute(original) then execute(submission)).  The original's
#     build unpacks its whole closure, Gin included, and Go then compiles the
#     submission from the extracted copy without consulting the pruned proxy.
#     Measured in this image: one shared cache and the delivery mirror "resolves"
#     Gin; a fresh cache per build and it refuses.  The ban is enforced by a
#     missing zip, and that only bites a module not already unpacked.
#
# Left shared deliberately, because an isolated cache scores worse.  With one, a
# Gin-keeping submission fails to build and run-candidate.sh exits 71, which the
# adjudicator reads as an infrastructure fault: the candidate is invalid.  Read any
# other way -- "passes on the original, fails on the submission" -- it is a
# divergence, so all six rounds find something and the submission takes 0/60 for a
# dependency violation that is stage 1's question, scored by the stage that measures
# behaviour.  That reading is what the fault range exists to prevent.  Shared, the
# tree builds, behaves like the original, and six rounds correctly report no
# behavioural divergence: there is none, because nothing was migrated.  Either way
# the submission should never have got here -- stage 1 is a pass/fail gate and its
# failure zeroes every stage -- so the leak is only reachable once stage 1 has
# already missed, and stage 3 is not where that is repaired.  The probe below still
# asserts the mirror property with a fresh cache per build, because "the mirror was
# constructed correctly" is a different claim from "the mirror constrains this
# build", and only the first one is true here.
#
#   * stage 2 has already measured whether the submission builds against the
#     pruned mirror alone, in an image that has no other mirror to offer.  A
#     submission that only builds here scored zero on stage 2's first module and
#     never reached this stage.  Note its retired-dependency checks are weight 0 by
#     design -- "is Gin gone" is stage 1's -- so stage 2 does not gate it either.
#
# What this image adds is the ability to ask the original a question at grading
# time.  That is the point of the stage: a candidate has to pass on the original
# before its failure on the submission means anything, and "passes on the
# original" is not checkable without an original that runs.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before compiling or serving anything, so what stage
# 2 measured is not what stage 3 mutates.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  Candidate tests run with SRB_NETWORK=denied: a test that needs
# the internet is not measuring this migration.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# golang:1.24 -- go1.24.13, the same digest environment/Dockerfile and stage 2
# pin.  The original this stage serves must be bit-identical to the oracle the
# agent developed against and to the reference stage 2 compared; a different
# compiler would make "passes on the original" mean something slightly else.
FROM golang@sha256:d2d2bc1c84f7e60d7d2438a3836ae7d0c847f4888464e7ec9ba3a1339a1ee804

ENV DEBIAN_FRONTEND=noninteractive \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=mod \
    CGO_ENABLED=0 \
    GOPATH=/opt/go \
    GOMODCACHE=/opt/go/pkg/mod \
    GOCACHE=/opt/gocache \
    GOSUMDB=off \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

# util-linux for setsid: run-candidate.sh starts each server in its own process
# group so cleanup can signal the group, and an orphaned server holding a port
# would leak from one candidate into the next.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        diffutils \
        file \
        git \
        jq \
        procps \
        ripgrep \
        util-linux \
        xxd \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- The candidate's test stack, one set of versions for both trees -----------
# A candidate that ran under one pytest on one tree and another on the other
# would not be comparing the two servers.  Declared in a lockfile beside this
# Dockerfile rather than inline, because the versions a candidate may import are
# part of the task definition and a reviewer should find them by opening a file
# named after them.
#
# In a venv rather than --break-system-packages for the reason stage 2 records:
# Debian's python3-packaging ships without a RECORD file, so pip refuses to
# replace it with a pinned version and the build fails.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-candidate.txt \
 && /opt/venv/bin/python -c \
        "import pytest, yaml; print('candidate stack:', pytest.__version__, '/ PyYAML', yaml.__version__)"

# The Go toolchain stays first on PATH -- run-candidate.sh shells out to `go
# build` -- and the candidate's interpreter is reached explicitly through
# /opt/venv/bin so that a candidate cannot be run by the harness's own Python.
ENV PATH=/usr/local/go/bin:/opt/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# --- The two mirrors ---------------------------------------------------------
# Digested against the values recorded at construction time before the mirror
# build reads them.  The three files must be byte-identical to the environment
# image's: a drift shows up as a submission that resolved fine for the agent and
# fails to resolve here, naming a module the agent never touched.
ENV SRB_GOMOD_SHA256=b0d132a3794999b93bcd8e994ce8319cc0b737b9f9028fb6d0ac50128c478369 \
    SRB_GOSUM_SHA256=8eed76cca471be7b9b0da971fcf88544fb175efede2f3c20473723304272c544 \
    SRB_RETIRED_SHA256=81a24df3452a58a20448d7a54b144b9f1bf5f7157b5dd3b06bee05789f2e7034

COPY data/lockfiles /opt/srb/lockfiles
RUN set -eu; \
    mkdir -p /opt/srb; \
    mv /opt/srb/lockfiles/retired-modules.txt /opt/srb/retired-modules.txt; \
    echo "$SRB_GOMOD_SHA256  /opt/srb/lockfiles/go.mod"      | sha256sum -c -; \
    echo "$SRB_GOSUM_SHA256  /opt/srb/lockfiles/go.sum"      | sha256sum -c -; \
    echo "$SRB_RETIRED_SHA256  /opt/srb/retired-modules.txt" | sha256sum -c -; \
    echo "lockfiles match the environment image"

COPY lib/build-goproxy.sh /opt/srb/scripts/build-goproxy.sh
RUN chmod +x /opt/srb/scripts/build-goproxy.sh \
 && /opt/srb/scripts/build-goproxy.sh /opt/srb/lockfiles \
        /opt/goproxy /opt/goproxy-baseline

# The property the whole stage rests on, asserted rather than assumed: Gin is
# buildable from the baseline mirror and not from the delivery mirror.  Checked
# here as well as inside the mirror build, because these two facts are what make
# the stage's two halves mean different things, and a silent failure of either
# would look like a working stage.
RUN set -eu; \
    while read -r m; do \
      [ -n "$m" ] || continue; \
      case "$m" in \#*) continue ;; esac; \
      b=$(find "/opt/goproxy-baseline/$m" -name '*.zip' 2>/dev/null | wc -l); \
      d=$(find "/opt/goproxy/$m" -name '*.zip' 2>/dev/null | wc -l); \
      dm=$(find "/opt/goproxy/$m" -name '*.mod' 2>/dev/null | wc -l); \
      [ "$b" != 0 ] || { echo "FATAL: baseline has no zip for $m" >&2; exit 1; }; \
      [ "$d" = 0 ]  || { echo "FATAL: delivery has $d zip(s) for $m" >&2; exit 1; }; \
      [ "$dm" != 0 ] || { echo "FATAL: delivery has no .mod for $m, tidy breaks" >&2; exit 1; }; \
      printf '  %-45s baseline.zip=%s  delivery.zip=%s .mod=%s\n' "$m" "$b" "$d" "$dm"; \
    done < /opt/srb/retired-modules.txt; \
    test -d /opt/goproxy/github.com/go-chi/chi; \
    test -d /opt/goproxy-baseline/github.com/go-chi/chi; \
    echo "both mirrors carry chi; only the baseline can resolve a retired module"

# A compile is the only proof that either mirror works.  Three of them: the
# original's own requirement set must build against the baseline and must NOT
# build against the delivery mirror, and chi must build against the delivery
# mirror.  A mirror that cannot resolve what it claims to carry would otherwise
# be discovered by the first candidate, an hour into the stage.
#
# Two things about how this is written are load-bearing, and both were found by
# it failing:
#
# ITS REQUIREMENT SET IS THE ORIGINAL'S, not a synthetic one-line require.  The
# mirrors were warmed from go.mod/go.sum, so they carry one .zip per module --
# the version State A's minimal version selection settles on -- plus the .mod
# files needed to compute the graph.  A throwaway `require gin v1.8.1` alone has
# nothing to raise gin's own older minimums, so MVS picks x/net@20210226,
# x/text@0.3.6, x/sys@20210806 and x/crypto@20210711, none of which has a zip
# here and none of which the original ever asks for.  That probe therefore
# reported "the baseline mirror cannot build gin" about a baseline that builds
# the original perfectly well -- an assertion failing on a property nothing in
# the stage depends on.  Building the original's requirement set asks the
# question the stage actually rests on, and asks it in the versions the stage
# will really resolve.
#
# EACH PROBE GETS ITS OWN GOMODCACHE.  Sharing one is how the gate assertion goes
# quietly vacuous: the baseline probe extracts gin into the cache, and the
# delivery probe then compiles against the extracted copy without consulting the
# pruned proxy at all.  Measured -- with a shared cache the delivery mirror
# "resolved" gin and this step reported the dependency gate open.  The ban is
# enforced by the absence of a zip, and that only bites a module that is not
# already unpacked, which is the same reason build-goproxy.sh throws its warming
# cache away.
RUN set -eu; \
    probe_original() { \
      d=$(mktemp -d); mc=$(mktemp -d); cd "$d"; \
      cp /opt/srb/lockfiles/go.mod /opt/srb/lockfiles/go.sum .; \
      mkdir -p p; \
      printf 'package p\n\nimport _ "%s"\n' "$2" > p/p.go; \
      env GOFLAGS=-mod=mod GOMODCACHE="$mc" GOPROXY="file://$1" GOSUMDB=off \
          GOPRIVATE= go build ./... >/dev/null 2>&1; \
      rc=$?; cd /; chmod -R u+w "$mc" 2>/dev/null || true; rm -rf "$d" "$mc"; \
      return $rc; \
    }; \
    probe_scratch() { \
      d=$(mktemp -d); mc=$(mktemp -d); cd "$d"; \
      printf 'module srb.local/probe\n\ngo 1.17\n\nrequire %s %s\n' "$2" "$3" > go.mod; \
      mkdir -p p; \
      printf 'package p\n\nimport _ "%s"\n' "$4" > p/p.go; \
      env GOFLAGS=-mod=mod GOMODCACHE="$mc" GOPROXY="file://$1" GOSUMDB=off \
          GOPRIVATE= go build ./... >/dev/null 2>&1; \
      rc=$?; cd /; chmod -R u+w "$mc" 2>/dev/null || true; rm -rf "$d" "$mc"; \
      return $rc; \
    }; \
    gin=$(head -1 /opt/srb/retired-modules.txt); \
    if probe_original /opt/goproxy-baseline "$gin"; then \
      echo "  baseline builds the original's requirement set including $gin"; \
    else \
      echo "FATAL: the baseline mirror cannot build $gin from the original's own" >&2; \
      echo "       go.mod/go.sum.  The original is not buildable, so every" >&2; \
      echo "       candidate would report 'does not pass on the original' and the" >&2; \
      echo "       stage would award all sixty points having compared nothing." >&2; \
      exit 1; \
    fi; \
    if probe_original /opt/goproxy "$gin"; then \
      echo "FATAL: the delivery mirror resolved $gin -- the dependency gate is open" >&2; \
      exit 1; \
    else \
      echo "  delivery mirror refuses $gin -- the gate holds"; \
    fi; \
    if probe_scratch /opt/goproxy github.com/go-chi/chi/v5 v5.3.1 github.com/go-chi/chi/v5; then \
      echo "  delivery mirror resolves chi v5.3.1 -- a correct port is buildable"; \
    else \
      echo "FATAL: the delivery mirror cannot build chi" >&2; exit 1; \
    fi

# --- The charts a candidate may upload ----------------------------------------
# The same frozen tree the environment image publishes, installed by the same
# script and asserted against the same aggregate digest.  A candidate needs
# something valid to POST, and generating a chart at run time would not do:
# `helm package` stamps time.Now() into every tar header, so two candidates would
# be uploading different bytes and a digest assertion could not be written.
ENV SRB_TESTDATA_AGGREGATE=6d888ec7db7d0a6944d7507ffd977f775457f09976859db33ca28c86cc496593
COPY data/testdata-frozen /opt/srb/testdata-frozen
COPY lib/install-testdata.sh /opt/srb/scripts/install-testdata.sh
RUN chmod +x /opt/srb/scripts/install-testdata.sh \
 && /opt/srb/scripts/install-testdata.sh /opt/srb/testdata-frozen \
        /opt/testdata /opt/webtemplate \
 && got=$(cat /opt/testdata/FIXTURES.aggregate) \
 && [ "$got" = "$SRB_TESTDATA_AGGREGATE" ] \
 && echo "fixtures match the environment image's aggregate: $got"

# --- The harness -------------------------------------------------------------
# Runs the six rounds and the adjudicator.  Kept out of /opt/venv, which is
# the candidates' interpreter: the process orchestrating the stage must not share
# an environment with code an adversary wrote.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN chmod +x /tests/verification/run-candidate.sh \
 && bash -n /tests/verification/run-candidate.sh \
 # A scope-less probe is unwinnable and a mismatched adversary count creates
 # points nobody can earn.  Checked here as well as by `swerefactor validate`,
 # because this is the image that would otherwise discover it six rounds in.
 && python3 /tests/verification/check-probe.py

RUN python3 -c "\
import re, sys, pathlib; \
from swerefactor import audit; \
text = pathlib.Path('/tests/verification/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt is too short to be a probe brief') if len(text) < 2000 else None; \
found = set(re.findall(r'{{\s*([a-z_]+)\s*}}', text)); \
unknown = sorted(found - set(audit.PLACEHOLDERS)); \
sys.exit(f'prompt.txt uses unknown placeholder(s): {unknown}') if unknown else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
