# =============================================================================
# SWERefactorBench -- fw01-httpbin-flask-to-asgi
# Agent environment image.
#
# Build-time responsibilities (network IS available during build; the trial
# itself runs with network_mode = "no-network"):
#
#   1. Verify + extract the frozen State A repo snapshot to /workspace/repo.
#   2. Initialise a git repo containing exactly ONE baseline commit.
#   3. Offline-install the project's dependencies and complete the initial
#      build, twice:
#        - /opt/oracle       : the full State A (Flask/WSGI) stack, with
#                              httpbin 0.10.2 installed non-editable.  This is
#                              the read-only reference oracle.
#        - /opt/venv         : the State B allowlist ONLY (no Flask, no
#                              Werkzeug, no flasgger, no gunicorn, no gevent).
#                              This is the delivery environment and the default
#                              python on PATH.
#   4. Leave /opt/wheelhouse in place so the agent can re-install offline as
#      often as it likes -- with exactly the same wheel set the verifier uses.
#
# The image pins its base by digest so it can never drift.
# =============================================================================

FROM python@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

# --- OS packages -------------------------------------------------------------
# curl/ca-certificates: so the agent can drive its own server over HTTP.
# git: required for the baseline snapshot commit.
# build-essential + libffi-dev: brotlicffi/cffi may need to build on some archs.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        less \
        vim-tiny \
        file \
        xxd \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# =============================================================================
# 1. Fetch and verify the two offline wheel sets.
# =============================================================================
COPY requirements-baseline.txt /opt/pins/requirements-baseline.txt
COPY requirements-target.txt   /opt/pins/requirements-target.txt

# Hash-checked download (--require-hashes is implied by the lockfiles).  If any
# upstream artifact were ever tampered with, the image build fails loudly.
# `pip wheel` (rather than `pip download`) so that any sdist in the closure --
# flasgger ships no wheel -- is built here, while the build still has network
# and a compiler.  Both wheelhouses then contain nothing but wheels, and every
# later install is a pure offline unpack.
RUN python -m pip install --upgrade "pip==24.3.1" \
 && mkdir -p /opt/wheelhouse /opt/wheelhouse-baseline \
 && pip wheel --wheel-dir /opt/wheelhouse-baseline \
        --require-hashes -r /opt/pins/requirements-baseline.txt \
 && pip wheel --wheel-dir /opt/wheelhouse \
        --require-hashes -r /opt/pins/requirements-target.txt \
 && test -z "$(find /opt/wheelhouse /opt/wheelhouse-baseline -type f ! -name '*.whl')" \
 # Hash verification has now happened, against the upstream artifacts, above.
 # The later offline installs read these verified wheelhouses, so they use
 # hash-free pin lists: a wheel built here from an sdist cannot carry the
 # sdist's hash.  Versions stay exactly pinned.
 && for f in baseline target; do \
        grep -oE '^[A-Za-z0-9._-]+==[^ ]+' /opt/pins/requirements-$f.txt \
            > /opt/pins/pinned-$f.txt; \
    done \
 && wc -l /opt/pins/pinned-baseline.txt /opt/pins/pinned-target.txt

# =============================================================================
# 2. Unpack the frozen State A snapshot into /workspace/repo.
#
# The tarball is content-addressed: the build refuses to continue if the
# snapshot does not match its recorded digest, so every trial in every run
# starts from bit-identical sources.
# =============================================================================
COPY original.tar.gz  /opt/snapshot/original.tar.gz
COPY original.sha256  /opt/snapshot/original.sha256

RUN cd /opt/snapshot \
 && sha256sum -c original.sha256 \
 && mkdir -p /workspace \
 && tar xzf original.tar.gz -C /workspace \
 && test -f /workspace/repo/pyproject.toml \
 && test -f /workspace/repo/httpbin/core.py \
 && ! find /workspace/repo -name '.git' -o -name '.hg' -o -name '.svn' | grep -q . \
 && cp -a /workspace/repo /opt/oracle-src

# =============================================================================
# 3. Initialise a git repo holding exactly one baseline commit.
#
# The agent gets `git diff` / `git stash` / `git checkout -- .` against State A
# and nothing else.  There is no history to mine and no remote to fetch.
# =============================================================================
RUN cd /workspace/repo \
 && git init -q -b main \
 && git config user.email "bench@swerefactor.local" \
 && git config user.name  "SWERefactorBench" \
 && git config commit.gpgsign false \
 && git add -A \
 && git commit -q -m "Baseline snapshot: httpbin 0.10.2 (Flask/WSGI)" \
 && git tag baseline \
 && test "$(git rev-list --count HEAD)" = "1"

# =============================================================================
# 4a. Reference oracle venv: the complete State A stack, offline install.
#
# Runs the ORIGINAL Flask application, permanently, from its own site-packages
# copy -- so it keeps answering correctly no matter how the agent rewrites
# /workspace/repo.  This is the behavioural ground truth the agent diffs
# against.  Nothing here is on the default PATH.
# =============================================================================
RUN python -m venv /opt/oracle \
 && /opt/oracle/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/oracle/bin/pip install --no-cache-dir --no-index \
        --find-links /opt/wheelhouse-baseline \
        -r /opt/pins/pinned-baseline.txt \
 && /opt/oracle/bin/pip install --no-cache-dir --no-index --no-deps \
        --find-links /opt/wheelhouse-baseline \
        /opt/oracle-src \
 && /opt/oracle/bin/python -c "import httpbin, flask, flasgger; print('oracle httpbin', httpbin.__file__)" \
 && chmod -R a-w /opt/oracle /opt/oracle-src

# =============================================================================
# 4b. Delivery venv: the State B allowlist ONLY.
#
# This is `python3` / `pip` on PATH.  Flask is simply not installable here.
# The agent discovers the hard constraint immediately instead of at grading
# time.  Its contents are exactly the wheel set the verifier uses.
# =============================================================================
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/venv/bin/pip install --no-cache-dir --no-index \
        --find-links /opt/wheelhouse \
        -r /opt/pins/pinned-target.txt \
 && /opt/venv/bin/python -c "import starlette, uvicorn, jinja2, httpx, pytest; print('target stack ok')" \
 && ! /opt/venv/bin/python -c "import flask" 2>/dev/null

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    PIP_NO_INDEX=1 \
    PIP_FIND_LINKS=/opt/wheelhouse \
    HTTPBIN_ORACLE_URL=http://127.0.0.1:8899

# The baseline wheel set has served its purpose (it built the oracle).  Remove
# it so the delivery environment has exactly one wheelhouse, matching the
# verifier's, and so Flask wheels are not lying around to be vendored.
RUN rm -rf /opt/wheelhouse-baseline

# --- Helper scripts ----------------------------------------------------------
# The oracle is deliberately here: the agent is allowed to see State A's
# behaviour, because reproducing it is the task.  Nothing in this image reports
# on the grading gates -- an agent that can query its own score optimises the
# report instead of the migration.
COPY scripts/httpbin-oracle       /usr/local/bin/httpbin-oracle
RUN chmod +x /usr/local/bin/httpbin-oracle

WORKDIR /workspace/repo
