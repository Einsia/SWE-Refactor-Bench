# =============================================================================
# SWERefactorBench -- fw01-httpbin-flask-to-asgi
# Stage 2 image: the behavioural suite.
#
#     docker build -t srb/fw01-behavioural:1 tasks/fw01-.../tests/behavioural
#
# Build context is this directory, so the suite is in the image and nothing else
# is.  The agent container never sees it.
#
# What makes this a measurement rather than a claim: the image carries its own
# wheelhouse and its own empty venv, and the `install` module rebuilds the
# submission from source into that venv before any other module runs.  Nothing
# the agent left behind is on the path -- no .venv, no *.egg-info, no
# __pycache__, no wheel it built itself.  A submission whose pyproject.toml is
# broken cannot borrow a working install from the environment image.
#
# The grading wheelhouse is COMPLETE: it carries State A's Flask stack as well as
# the target one, because State A is the oracle every scored expectation here was
# recorded from and `install` is required, so a wheelhouse without Flask scored
# State A zero for the entire stage.  The agent's own wheelhouse, reproduced at
# /opt/wheelhouse-pruned, is what withholds Flask -- and the agent's delivery venv
# is where that constraint bites, while the work is being done.  Whether the
# retirement happened is stage 1's question; `flask_retired` owns it.  See the
# wheelhouse section below.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# Pinned by digest, like the environment image, so the grader cannot drift.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM python@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        jq \
        procps \
        file \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# --- The wheel sets, hash-verified at build time ------------------------------
# `pip wheel` rather than `pip download`, so the one sdist in each closure is
# built here while there is still network and a compiler.  Every later install
# is an offline unpack of a verified wheel.
#
# TWO wheelhouses, and which one the install module reads is the whole of what
# this image changed on 2026-08-04:
#
#   /opt/wheelhouse          COMPLETE: the target wheel set plus State A's own
#                            baseline, so State A can be installed here.  State A
#                            declares flask, flasgger, werkzeug, brotlicffi,
#                            decorator, six and greenlet, and State A is the
#                            oracle every scored expectation in data/ was
#                            recorded from -- a wheelhouse it cannot install from
#                            is one that can never show this suite is satisfiable.
#                            The `install` module is required, so State A scored 0
#                            for the whole stage on a wheelhouse without Flask.
#   /opt/wheelhouse-pruned   the AGENT's wheelhouse, reproduced: target pins only.
#                            Nothing is installed from it; the `install` module
#                            reads it for one unscored observation.
#
# The dependency boundary is unmoved, because it was never here.  The AGENT's
# delivery venv still has no Flask -- environment/Dockerfile deletes its baseline
# wheelhouse after building the oracle -- so a tree that still imports Flask
# cannot be installed or tested while the work is being done.  And whether the
# retirement happened is stage 1's question: `flask_retired` owns it, and a gate
# failure there zeroes the submission before this stage runs.
#
# Two versions each of pytest (7.4.4 / 8.3.4) and pluggy (1.4.0 / 1.5.0) land in
# the complete wheelhouse, one from each pin list.  That is what a wheelhouse is
# for: `--find-links` resolves against a directory, and the pin list passed to
# pip decides which version is installed.
COPY requirements-target.txt /opt/pins/requirements-target.txt
COPY requirements-baseline.txt /opt/pins/requirements-baseline.txt

RUN python -m pip install --upgrade "pip==24.3.1" \
 && mkdir -p /opt/wheelhouse \
 && pip wheel --wheel-dir /opt/wheelhouse \
        --require-hashes -r /opt/pins/requirements-target.txt \
 # The pruned wheelhouse is a COPY taken before the baseline set is added, not a
 # second `pip wheel` run: two resolutions of the same pin list at different
 # times can build a different sdist, and the two wheelhouses would then disagree
 # about a distribution neither tree chose.  One resolve, one copy.
 && cp -a /opt/wheelhouse /opt/wheelhouse-pruned \
 && pip wheel --wheel-dir /opt/wheelhouse \
        --require-hashes -r /opt/pins/requirements-baseline.txt \
 && test -z "$(find /opt/wheelhouse /opt/wheelhouse-pruned -type f ! -name '*.whl')" \
 # Hash checking has happened, above, against the upstream artifacts.  The
 # offline installs below read these verified wheelhouses, so they use a hash-free
 # pin list: a wheel built here from an sdist cannot carry the sdist's hash.
 && grep -oE '^[A-Za-z0-9._-]+==[^ ]+' /opt/pins/requirements-target.txt \
        > /opt/pins/pinned-target.txt \
 && grep -oE '^[A-Za-z0-9._-]+==[^ ]+' /opt/pins/requirements-baseline.txt \
        > /opt/pins/pinned-baseline.txt

# Each wheelhouse against its own role, in both directions.  Both have failed
# silently in this ladder's history: a complete wheelhouse without Flask cannot
# install State A, and a pruned one with Flask is not the agent's.
RUN set -eu; \
    for d in flask werkzeug flasgger; do \
      ls /opt/wheelhouse/ | grep -qi "^${d}-" \
        || { echo "FATAL: no $d wheel in the COMPLETE wheelhouse; State A cannot install" >&2; exit 1; }; \
      ls /opt/wheelhouse-pruned/ | grep -qi "^${d}-" \
        && { echo "FATAL: $d wheel present in the PRUNED wheelhouse, which is the agent's" >&2; exit 1; }; \
      echo "  $d: in complete, absent from pruned"; \
    done; \
    echo "wheelhouses: complete installs State A, pruned reproduces the agent's"

# --- The suite's own interpreter ---------------------------------------------
# This runs the harness and the pytest modules.  It is NOT where the submission
# gets installed: `install` builds a second venv under $SRB_SUITE_WORK for that,
# from the same wheelhouse, so a broken submission cannot damage the runner that
# is grading it and the grading venv starts empty on every run.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/venv/bin/pip install --no-cache-dir --no-index \
        --find-links /opt/wheelhouse \
        -r /opt/pins/pinned-target.txt \
 && /opt/venv/bin/python -c "import httpx, pytest, pytest_timeout; print('runner stack ok')" \
 && ! /opt/venv/bin/python -c "import flask"    2>/dev/null \
 && ! /opt/venv/bin/python -c "import werkzeug" 2>/dev/null

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_NO_INDEX=1 \
    PIP_FIND_LINKS=/opt/wheelhouse \
    SRB_WHEELHOUSE_PRUNED=/opt/wheelhouse-pruned

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the wheelhouse.
COPY . /tests/behavioural

RUN chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh \
 && test -f /tests/behavioural/data/responses.json \
 && test -f /tests/behavioural/data/expected-openapi.json \
 && /opt/venv/bin/python -c "\
import sys; sys.path.insert(0, '/tests/behavioural/lib'); \
from harness import corpus; print(f'corpus: {len(corpus.CASES)} cases')" \
 # The manifest is the thing most likely to be edited and least likely to be
 # tested, so it is checked at build time: a malformed weight, a duplicate id or
 # a module directory that does not exist fails the build, not the run.
 && /opt/venv/bin/python -c "\
import sys, pathlib; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"

WORKDIR /tests/behavioural
