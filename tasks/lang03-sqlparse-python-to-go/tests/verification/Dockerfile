# =============================================================================
# SWERefactorBench -- lang03-sqlparse-python-to-go
# Stage 3 image: the verification rounds.
#
#     docker build -t swerefactor/lang03-verification:1 tasks/lang03-.../tests/verification
#
# ---------------------------------------------------------------------------
# This build needs stage 2's image first
# ---------------------------------------------------------------------------
# Alone among the three stages, and for a reason that is not incidental: the
# operation table a candidate is handed is DERIVED from the catalog stage 2 froze.
# An operation is offered to an adversary only if stage 2 actually sent it, in the
# tier stage 2 routed it to, with the argument shape stage 2 used -- otherwise the
# adversary spends a round discovering that the harness disagrees with itself.
#
# So the table cannot be written here; it has to be computed from the artifact that
# already exists in swerefactor/lang03-behavioural:1.  Build that image first.
#
# What comes across is only what a candidate may see.  catalog.json, the frozen
# documents and expectations.bin stay in the `derived` stage and never reach the
# final image -- see check-probe.py, which asserts their absence rather than
# trusting that this file stays written the way it is today.
#
# ---------------------------------------------------------------------------
# The asymmetry this stage deliberately has
# ---------------------------------------------------------------------------
# This is the only stage where the reference is runnable.  Stage 1 has no
# toolchain at all and stage 2 asserts that `import sqlparse` fails -- there, the
# reference exists only as the answers it produced at freeze time.  Here the
# original tree is mounted, importable, and answering questions.
#
# That is not a loophole, it is the entire point of the stage: an adversary's claim
# is empty unless the original demonstrably passes the same test the submission
# fails.  Stage 2 has already measured whether the submission needs an interpreter.
#
# ---------------------------------------------------------------------------
# What is deliberately NOT here
# ---------------------------------------------------------------------------
# Stage 2's document corpus.  Stage 3 is asked only of a submission that passed
# every scored check, so every case in that corpus is already known to pass on it.
# An adversary handed the corpus would spend its hour on the one body of input
# measured to find nothing, and re-running a passing case is not a finding.
# A candidate composes its own documents; the original's own tests/ directory is
# readable in the tree and is a better source of difficult SQL anyway.
#
# expectations.bin for a stronger reason: the frozen answers ARE the original's
# answers, so a candidate holding them could decide which tree it is on without
# asking the artifact anything.  That is the one thing the scope forbids outright.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz        (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# Network: none.  Go is configured offline and the build has no proxy to reach.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
ARG BEHAVIOURAL_IMAGE=swerefactor/lang03-behavioural:1

FROM ${INFRA_IMAGE} AS infra
FROM ${BEHAVIOURAL_IMAGE} AS behavioural

# ---------------------------------------------------------------------------
# derived: compute the op table, in the image that has the catalog
#
# Three sources, cross-checked against each other: the catalog says which
# operations stage 2 sent and with what argument tags, probe.py's AST says which
# the reference registers, and each tier's main.go says which the Go registers.  A
# disagreement fails this build -- which is the point of doing it here rather than
# writing a table by hand and hoping it stays true.
#
# Read with `ast` rather than by importing: probe.py imports sqlparse at module
# scope, and no stage-3 image has an importable sqlparse.
# ---------------------------------------------------------------------------
FROM behavioural AS derived

COPY lib/derive-ops.py /tmp/derive-ops.py

# The two files that cross into the shipped image are assembled into one directory
# here, so the COPY below names a directory contents rather than reaching into
# /opt/assets twice.  That is the point: the only thing the final stage can copy is
# what this step chose to put here.
#
# ops.json is then re-read and checked against the tiers the probe module actually
# has directories for.  derive-ops.py cross-checks the catalog against probe.py and
# each tier's main.go; what it cannot check is that it wrote what it computed, and a
# truncated write is a build that ships an op table missing a tier.
RUN set -eux; \
    /usr/bin/python3 -B /tmp/derive-ops.py \
      --catalog /opt/assets/catalog.json \
      --probe-py /tests/behavioural/lib/probe/probe.py \
      --probe-dir /tests/behavioural/lib/probe \
      --out /tmp/ops.json; \
    install -d -m 0755 /tmp/out; \
    install -m 0444 /tmp/ops.json /tmp/out/ops.json; \
    install -m 0444 /opt/assets/spec.json /tmp/out/spec.json; \
    /usr/bin/python3 -B -c "\
import json, sys; \
from pathlib import Path; \
ops = json.loads(Path('/tmp/out/ops.json').read_text(encoding='utf-8')); \
named = sorted({entry['tier'] for entry in ops.values()}); \
dirs = sorted(p.name for p in Path('/tests/behavioural/lib/probe').iterdir() \
              if p.is_dir() and (p / 'main.go').exists()); \
sys.exit('ops.json names tiers %s, probe has tier dirs %s' % (named, dirs)) \
    if named != dirs else \
    print('ops.json: %d ops over %d tiers' % (len(ops), len(dirs)))"

# ---------------------------------------------------------------------------
# The stage image proper: the same pinned base stage 2 uses, so a submission that
# builds there builds here for the same reasons.
# ---------------------------------------------------------------------------
FROM golang:1.25.12-bookworm@sha256:ea341baa9bd5ba6784f6d7161ace70544349a6242d54d34a0fbfd2c4d51c9d58

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20260713T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same snapshot as the other two stages, and for the same reason: the toolchain
# closure has to be reproducible years from now.
#
# sources.list.d/debian.sources is removed, not just overwritten -- the base image
# ships its pins there, and writing /etc/apt/sources.list alone would leave the
# original in place and resolve against the live archive.
#
# python3-venv is the one package the other stages do not need: a candidate runs
# under pytest, and pytest is installed into a venv of its own.  The read-only
# toolbox the adversary itself uses is the rest.
RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
      'Suites: bookworm bookworm-updates' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      '' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
      'Suites: bookworm-security' \
      'Components: main' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
      > /etc/apt/sources.list.d/debian.sources; \
    echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot; \
    apt-get -o Acquire::Retries=8 update; \
    apt-get install -y --no-install-recommends \
      python3-venv=3.11.2-1+b1 \
      diffutils \
      file \
      jq \
      less \
      ripgrep \
      tree \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin; \
    go version | grep -Fx 'go version go1.25.12 linux/amd64'; \
    /usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'

# The Go environment both roles build with.  run-candidate.sh exports these itself
# for the build it runs, and srbsqlparse reaches vlib.base_env() for the processes
# it starts, so these are belt-and-braces for anything run by hand in this image --
# but they must agree with vlib.base_env(), and the assertions below say so.
RUN set -eux; \
    go env -w GOFLAGS=-mod=mod; \
    go env -w GOPROXY=off; \
    go env -w GOSUMDB=off; \
    go env -w GOTOOLCHAIN=local; \
    go env -w CGO_ENABLED=0; \
    test "$(go env GOPROXY)" = "off"; \
    test "$(go env GOTOOLCHAIN)" = "local"

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, at one set of versions for both trees.  A candidate
# that ran under different pytest versions on the two trees would not be comparing
# the two implementations.
#
# Deliberately not in here: sqlparse, at any version.  A candidate imports
# srbsqlparse and the standard library; the original tree is importable by the
# probe process only, which is a different interpreter with a PYTHONPATH the
# candidate never gets.  A candidate that could `import sqlparse` in its own
# process would be testing one tree against a third thing.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN set -eux; \
    /usr/bin/python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1"; \
    /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-candidate.txt; \
    /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')"; \
    ! /opt/venv/bin/python -c "import sqlparse" 2>/dev/null

# --- The harness -------------------------------------------------------------
# Runs the six rounds and the adjudicator.  The venv above is what candidates
# run in; this is the system python.  The process orchestrating the stage does not
# share an interpreter with code an adversary wrote.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# infra/ is its own build context with no `.dockerignore`, so the author's host
# bytecode rides in with the source -- a `cpython-310` tag in an image whose python
# is 3.11.  Stripped here, and the assertion after the last build-time step below
# covers /opt/swerefactor too, so an inherited violation cannot pass quietly.
RUN find /opt/swerefactor -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/swerefactor -name '*.py[co]' -delete

# --- The reference probe, and the two manifests a candidate is allowed ---------
# From the `derived` stage, and only these: the probe source, the op table it
# computed, and the spec both halves resolve names against.  The frozen catalog,
# documents.json, expectations.bin and the docs directory stay behind -- they are
# stage 2's answers, and a candidate holding them could tell the two trees apart
# without asking either one a question.
#
# /opt/probe is read at run time by both roles: run-candidate.sh puts probe.py on
# an interpreter for the original, and copies the Go tiers into a scratch module
# that reaches the submission through `replace` for the submission.  Read-only, so
# neither role can leave anything behind for the other.
COPY --from=derived /tests/behavioural/lib/probe /opt/probe
COPY --from=derived /tmp/out/ /opt/probe-assets/

# Write bits off, and the modes asserted rather than assumed -- `chmod -R a-w` is
# how stage 2 holds /opt/assets and this is the same intent.  Not a security
# boundary: everything in this image runs as root and root ignores the mode.  What
# it is, is a tripwire on the arrangement that makes the two roles symmetric --
# run-candidate.sh copies /opt/probe into per-run scratch precisely because
# `go build` writes go.sum into the module directory, and a build that started
# writing here instead would be leaving state where the other role will read it.
#
# `a-s` and not only `a-w`: the task tree lives on a shared filesystem whose
# directories carry setgid, COPY preserves it, and the tier directories arrive as
# 2755.  Measured -- the first spelling of this step asserted 555 and was told
# 2555.  Stripped rather than accommodated, because run-candidate.sh copies this
# tree with `cp -a` into a scratch build directory, and a setgid bit riding along
# into a directory `go build` writes into is a difference between the two roles
# that has nothing to do with either implementation.
RUN set -eux; \
    chmod -R a-w,a-s /opt/probe /opt/probe-assets; \
    test "$(stat -c '%a' /opt/probe-assets/ops.json)" = "444"; \
    test "$(stat -c '%a' /opt/probe/probe.py)" = "444"; \
    test "$(stat -c '%a' /opt/probe/core)" = "555"; \
    test -z "$(find /opt/probe /opt/probe-assets -perm /7000 -print -quit)"; \
    test -f /opt/probe/go.mod; \
    test -f /opt/probe/probeutil/probeutil.go; \
    for tier in core model keywords parts; do \
      test -f "/opt/probe/$tier/main.go"; \
    done

# --- The stage's own files ----------------------------------------------------
COPY . /tests/verification

# vlib.py and executor.py, from the image that grades with them.  srbsqlparse
# imports the verifier's own protocol client rather than shipping a second one:
# a candidate speaks the wire stage 2 spoke, so a "break" cannot be an artifact of
# the candidate's framing.  Copied rather than duplicated in the tree for the
# obvious reason -- two copies of a protocol client is one copy that drifts.
COPY --from=derived /tests/behavioural/lib/vlib.py \
                    /tests/verification/lib/probelib/vlib.py
COPY --from=derived /tests/behavioural/lib/executor.py \
                    /tests/verification/lib/probelib/executor.py

# The build-time gate.  A scope-less probe is unwinnable and a mismatched adversary
# count creates points nobody can earn; check-probe.py is run by `swerefactor
# validate` as well, but this is the image that would otherwise discover the
# problem six rounds in.  It also asserts that none of stage 2's answers reached
# this image, which is the `COPY --from=derived` selection above being checked
# rather than trusted.
#
# The syntax check runs under the venv, because the venv is the interpreter that
# will import these.  It covers probelib as well as srbsqlparse: those two modules
# were written against stage 2's 3.11 and this is the same 3.11, but the import
# that would discover otherwise happens inside a candidate's test run, where a
# harness failure reads as a break.
#
# Found by `find` rather than named, so a module added to lib/ cannot be the one
# module never checked -- and with the built-in compile() rather than py_compile,
# for the reason stage 2's Dockerfile records at length: py_compile writes a
# __pycache__ beside each source, and pointing it at /dev/null is not a way out
# because it refuses non-regular targets on purpose.  Measured here too, and the
# error it produced is above this line in the build log of the first attempt.
#
# The import of executor is a real check and not a spelling of the syntax check
# twice over: srbsqlparse reaches it through a sys.path insert of lib/probelib, and
# executor imports vlib by bare name.  If only one of the two had been copied, or
# copied to the wrong directory, this is the line that says so.
RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    find /tests/verification -name '*.py' -print0 \
      | xargs -0 -n 40 /opt/venv/bin/python -c \
        'import sys; [compile(open(f, "rb").read(), f, "exec") for f in sys.argv[1:]]'; \
    PYTHONPATH=/tests/verification/lib/probelib /opt/venv/bin/python -B -c "\
import executor, vlib; \
print('probelib ok: case timeout', executor.CASE_TIMEOUT, \
      'go', vlib.base_env()['GOFLAGS'])"; \
    PYTHONPATH=/opt/swerefactor /usr/bin/python3 -B \
      /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# Asserted after the last step that could have written bytecode, which is the only
# position where the assertion means anything: /opt/swerefactor was stripped above,
# but check-probe.py and the compile step run after it, and both import.
RUN test -z "$(find /tests/verification /opt/probe /opt/swerefactor \
      \( -name '__pycache__' -o -name '*.py[cod]' \) -print -quit)"

# PYTHONPATH is the harness's, not the candidate's: run-candidate.sh sets the
# candidate's own PYTHONPATH explicitly for the pytest process.  SWEREFACTOR_* are
# the paths the runner uses, kept so that an operator running a round by hand in
# this image sees what the stage would.
ENV TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_PROBE=/opt/probe \
    SWEREFACTOR_PROBE_ASSETS=/opt/probe-assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_ORIGINAL=/opt/original \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier /tmp/gotelemetry

WORKDIR /tests/verification
