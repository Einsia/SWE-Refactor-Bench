# =============================================================================
# SWERefactorBench -- shared evaluation infrastructure.
#
# This image is not a stage.  It exists so the sixty stage images (three per
# task, twenty tasks) do not each carry their own copy of the harness: they take
# it from here with
#
#     ARG  INFRA_IMAGE=swerefactor/infra:1
#     FROM ${INFRA_IMAGE} AS infra
#     ...
#     COPY --from=infra /opt/swerefactor /opt/swerefactor
#
# which is a build-context-free way to vendor a directory that lives outside the
# stage's own tree.  Build it once before the stage images:
#
#     docker build -t swerefactor/infra:1 infra/
#
# The alternative -- building every stage image with the repository root as its
# build context -- would upload twenty State A tarballs and every other task's
# suite into every build.  The alternative to that, a copy of the harness per
# stage directory, is sixty copies to keep in step.
#
# The harness has no third-party dependencies.  It talks to models over
# urllib, so there is no vendor SDK to pin, and its own tests run on the
# standard library alone.  tomli is here only because Python 3.10 has no
# tomllib; swerefactor.tomlcompat prefers the stdlib module when it exists.
# =============================================================================

FROM python@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Only needed on Python < 3.11.  Installed unconditionally so the image works
# as a donor for a stage image on either side of that line.
RUN python -m pip install --no-cache-dir "tomli==2.0.1"

COPY swerefactor /opt/swerefactor/swerefactor

# A donor image whose payload does not import is worse than no donor image:
# the failure would surface inside a stage container, hours later.
#
# The fingerprint is printed so the build log carries it.  `swerefactor/infra:1` is
# a mutable tag: rebuilding it changes what grades every stage image built
# afterwards while the tag stays ":1".  With this line, the one artifact that
# records which source went into a given image is the build log of that image, and
# a score.json's `harness.fingerprint` can be matched against it.  It is recorded
# on every stage result and rendered in the report footer -- see docs/SCHEMA.md.
RUN PYTHONPATH=/opt/swerefactor python -c "\
from swerefactor import cli, config, scoring, behavioural, audit, verification; \
import swerefactor; \
print('swerefactor infra ok: %s' % swerefactor.fingerprint())"

# swerefactor.contract, separately and first, because the guarantee it makes is
# not "it imports" but "it imports with nothing else present".  A task's lib/
# imports it to scrub the grading contract out of a submission's environment,
# and two of the twenty behavioural images have no TOML reader -- so a contract
# module that had quietly acquired a dependency on one would be a scrub that is
# not there, in the two images least able to say so.  -I ignores the
# environment and PYTHONPATH both; the path is passed in argv instead.
RUN python -I -c "\
import importlib.util, sys; \
spec = importlib.util.spec_from_file_location( \
    'srb_contract_probe', '/opt/swerefactor/swerefactor/contract.py'); \
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); \
assert len(mod.CONTRACT_ENV) == 8, sorted(mod.CONTRACT_ENV); \
assert mod.submission_env({'SRB_RESULT': 'x', 'PATH': '/bin'}) == {'PATH': '/bin'}; \
print('swerefactor.contract stands alone ok')"

ENV PYTHONPATH=/opt/swerefactor
WORKDIR /opt/swerefactor
