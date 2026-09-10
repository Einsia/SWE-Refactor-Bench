# =============================================================================
# SWERefactorBench -- fw01-httpbin-flask-to-asgi
# Stage 3 image: the verification probe.
#
#     docker build -t srb/fw01-verification:1 tasks/fw01-.../tests/verification
#
# Six models, an hour each, trying to write a test the original passes and the
# submission fails.  So this image is the only one that has to run BOTH
# applications: the original on Flask/Werkzeug/gunicorn, the submission on
# whatever it declares.  It therefore carries two wheelhouses, and
# run-candidate.sh chooses per tree.
#
# That is not a loophole.  The wheelhouses are separate and a submission is
# installed only from the target one, so it cannot pass by reaching for a
# distribution only the original's environment has -- and stage 2, in an image
# where Flask is not installable at all, has already measured whether it
# installs cleanly.  What this image adds is the ability to ask the original a
# question at grading time, which is the whole point of the stage.
#
# A third, tiny wheelhouse holds the candidate's own dependencies -- pytest and
# httpx -- at one set of versions for both trees.  A candidate that ran under
# pytest 7 on one tree and pytest 8 on the other would not be comparing the two
# services.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The stage copies both trees before installing or serving anything, so what
# stage 2 measured is not what stage 3 mutates.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  Candidate tests run with SRB_NETWORK=denied: a test that needs
# the internet is not measuring this migration.
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
        diffutils \
        file \
        jq \
        procps \
        ripgrep \
        util-linux \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# --- Both stacks, hash-verified at build time --------------------------------
# The same two lockfiles the environment image uses, so the original this stage
# serves is bit-identical to the oracle the agent developed against.  If they
# ever diverge, "passes on the original" would stop meaning what it says.
COPY requirements-baseline.txt /opt/pins/requirements-baseline.txt
COPY requirements-target.txt   /opt/pins/requirements-target.txt

RUN python -m pip install --upgrade "pip==24.3.1" \
 && mkdir -p /opt/wheelhouse /opt/wheelhouse-baseline \
 && pip wheel --wheel-dir /opt/wheelhouse-baseline \
        --require-hashes -r /opt/pins/requirements-baseline.txt \
 && pip wheel --wheel-dir /opt/wheelhouse \
        --require-hashes -r /opt/pins/requirements-target.txt \
 && test -z "$(find /opt/wheelhouse /opt/wheelhouse-baseline -type f ! -name '*.whl')" \
 && for f in baseline target; do \
        grep -oE '^[A-Za-z0-9._-]+==[^ ]+' /opt/pins/requirements-$f.txt \
            > /opt/pins/pinned-$f.txt; \
    done

# --- The candidate's own test stack, one version for both trees ---------------
# Declared in a lockfile like the other three tasks' stage 3, rather than inline
# here: the versions a candidate may import are part of the task definition, and
# a reviewer should find them by opening a file named after them.
#
# Resolved from the target wheelhouse rather than the network, so every wheel was
# already hash-verified by the layer above.  httpx needs
# httpcore/anyio/h11/sniffio/certifi/idna, all of which are in that lockfile;
# nothing here pulls in starlette or uvicorn.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN mkdir -p /opt/wheelhouse-candidate \
 && pip wheel --wheel-dir /opt/wheelhouse-candidate --no-index \
        --find-links /opt/wheelhouse -r /opt/pins/requirements-candidate.txt \
 && test -z "$(find /opt/wheelhouse-candidate -type f ! -name '*.whl')" \
 && grep -oE '^[A-Za-z0-9._-]+==[^ ]+' /opt/pins/requirements-candidate.txt \
        > /opt/pins/pinned-candidate.txt \
 && test "$(wc -l < /opt/pins/pinned-candidate.txt)" = "3"

# --- The harness's own interpreter -------------------------------------------
# Runs the six rounds and the adjudicator.  Kept apart from the per-tree venvs
# that run-candidate.sh builds: the process orchestrating the stage must not
# share an environment with code an adversary wrote.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1" \
 && /opt/venv/bin/python -c "print('harness venv ok')"

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN chmod +x /tests/verification/run-candidate.sh \
 && bash -n /tests/verification/run-candidate.sh \
 # A scope-less probe is unwinnable and a mismatched adversary count creates
 # points nobody can earn.  Checked here as well as by `swerefactor validate`,
 # because this is the image that would otherwise discover it six rounds in.
 && /opt/venv/bin/python /tests/verification/check-probe.py

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
