# =============================================================================
# SWERefactorBench -- build03-pycryptodome-setuptools-to-meson
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/build03-verification:1 \
#         tasks/build03-pycryptodome-setuptools-to-meson/tests/verification
#
# Six models, an hour each, trying to write a test the setuptools build passes
# and the Meson build fails.  So this is the only one of build03's three images
# that can build EITHER tree, and it therefore ships both backends: setuptools and
# wheel next to meson, meson-python, ninja and patchelf.
#
# That reads like a loophole and is the opposite of one.  Stage 1 has already
# judged whether setuptools left the submission; stage 2 runs in an image where
# setuptools, distutils and pkg_resources cannot be imported at all and asserts
# their absence before it grades anything.  Both verdicts are in before this image
# starts.  What this image adds is the ability to ask State A a question at grading
# time -- because a candidate's claim that "the setuptools build ships this and the
# Meson build does not" is worth nothing unless the setuptools build is right here
# to demonstrate it.  A stage that could not build the original could only take an
# adversary's word for what the original does.
#
# A MISSING BACKEND HERE WOULD NOT FAIL VISIBLY
#
# This image fails its build if setuptools is not importable, and the reason is
# worth stating plainly.  A missing backend would make `tree()` raise on the
# original for every candidate in the stage, which the runner records as "did not
# pass on the original", which the adjudicator reads as six rounds finding
# nothing, which pays the submission all 60 points.  A silent 60 is the worst
# failure mode this benchmark has, so both toolchains are checked by import and by
# an actual build below.
#
# Stage 2 installs and asserts the same two backends, for the neighbouring reason:
# every number it scores against was recorded from the setuptools build, so an image
# that could not build State A could not check its own oracle.  Whether the retired
# toolchain is really gone is stage 1's question, and `setuptools_retired` answers it
# with both trees open.
#
# Same pinned base and same pinned toolchain as stage 2, because what a candidate
# measures is interpreter- and compiler-dependent.  The wheel's ABI tag, the
# symbols visibility leaves in each object, the instructions gcc emitted and the
# 39,245 answers the library's own suite gives are all facts about a specific
# python and a specific gcc.  A different one would make State A's own answers
# different, and every candidate comparing the two trees would be comparing two
# toolchains instead.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# srbcrypto copies whichever tree it was pointed at before building, so what stage
# 2 measured is not what stage 3 mutates, and a build never writes into the tree
# the harness handed over.
#
# Network: reaches the model API for the six rounds and the adjudicator, and
# nothing else.  Builds are `--no-isolation` precisely so that no build fetches a
# backend: the backends are in this image, pinned, and a build that resolved its
# own would be grading a resolver rather than a repository.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

ENV DEBIAN_FRONTEND=noninteractive \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    TZ=UTC

# --- The compiler, and the tools for reading what it produced -----------------
# gcc and libc6-dev because both trees compile 47 translation units of C.
#
# binutils because the interesting half of this subject is only visible through it:
# 41 shared objects, 277 exported symbols and an instruction-set elevation confined
# to two of them.  A candidate asks `nm`, `objdump` and `readelf` those questions
# through srbcrypto, and none of them is reachable from a build log.
#
# libgmp because Crypto.Math opens libgmp.so.10 through ctypes at import time.
# Without it the library silently falls back to its own integer implementation, both
# trees fall back identically, and a whole class of comparison quietly stops
# measuring anything.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libc6-dev \
      binutils file \
      libgmp10 libgmp-dev \
      xz-utils \
      coreutils findutils diffutils \
      jq less ripgrep tree \
    && rm -rf /var/lib/apt/lists/* \
    && gcc --version | head -1 \
    && nm --version | head -1 \
    && objdump --version | head -1 \
    && readelf --version | head -1 \
    && python3 -c "import ctypes.util, ctypes; \
n = ctypes.util.find_library('gmp'); \
print('libgmp:', n); \
ctypes.CDLL(n)"

# --- Both toolchains, hash-pinned --------------------------------------------
# Installed into the system interpreter, which is the one that builds either tree.
# $SRB_BUILD_PYTHON below names it explicitly rather than leaving srbcrypto to
# search PATH for a python3, so that adding anything to PATH later cannot silently
# redirect every build at an interpreter with no backends in it.
COPY requirements-toolchain.txt /opt/pins/requirements-toolchain.txt

RUN python3 -m pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-toolchain.txt \
    && meson --version \
    && ninja --version \
    && patchelf --version \
    && python3 -c "import build; print('build', build.__version__)"

# --- Both toolchains are PRESENT, and this is the assertion -------------------
# The inverse of stage 2's check.  Two loops: the importable names, then the
# executables.  Each failure message says what a missing entry would cost, because
# the cost is not obvious and is the whole reason this runs at image-build time.
# `mesonbuild`, not `meson`: pip's meson distribution installs the package under
# that name and `meson` is only the console script, which the second loop checks.
RUN set -eu; \
    for name in setuptools wheel distutils mesonpy mesonbuild build \
                pyproject_hooks pyproject_metadata packaging; do \
      python3 -c "import $name" >/dev/null 2>&1 || { \
        echo "FATAL: the stage-3 image cannot import $name." >&2; \
        echo "This stage builds BOTH trees.  A backend the ORIGINAL needs and" >&2; \
        echo "does not have makes tree() raise on the original for every" >&2; \
        echo "candidate, which is recorded as 'did not pass on the original'," >&2; \
        echo "read as 'six rounds found nothing', and paid as 60 points." >&2; \
        exit 1; }; \
    done; \
    for required in gcc cc nm objdump readelf strings file \
                    meson ninja patchelf python3; do \
      command -v "$required" >/dev/null 2>&1 || { \
        echo "FATAL: the stage-3 image is missing $required." >&2; \
        exit 1; }; \
    done; \
    python3 -c "\
import setuptools, wheel; \
print('for the original: setuptools', setuptools.__version__, '| wheel', wheel.__version__)"; \
    python3 -c "\
import mesonpy; \
print('for the submission: meson-python', mesonpy.__version__)"

# --- The project's own test vectors, without setuptools -----------------------
# Byte-identical to stage 2's step, and deliberately so: the self-test is 39,245
# known-answer comparisons and it is the sharpest instrument a candidate has.  It
# has to give the same answers in both images or a candidate's finding would be an
# artifact of which stage it ran in.
#
# The archive is fetched by URL and checked against its digest, with no build
# backend involved -- not because a backend is unavailable, but because `pip install
# pycryptodome-test-vectors` would build an sdist through one and make the vectors
# depend on which backend won, and these 431 files are the reference both trees are
# measured against.
#
# Crypto/SelfTest/loader.py reaches the package with `import
# pycryptodome_test_vectors` and `os.path.dirname(__file__)` -- no metadata, no
# entry points -- so copying the directory into site-packages is a complete install
# for this purpose.
#
# Without it the suite collects 3,603 instances instead of 42,784, and the tests
# that would notice a wrong compiler flag never run.  That is a failure that looks
# like a pass, which is why the check below loads actual vectors instead of
# counting files.
ARG VECTORS_URL=https://files.pythonhosted.org/packages/70/a4/d9da499e91dfc288aca86933d519adc2f6bfd28414df824c10cac32f0191/pycryptodome-test-vectors-1.0.14.zip
ARG VECTORS_SHA=d437b44a35dcafc0626c3561bfb1b506a1b7c7445cc5fb9d36e2374161bc9a37
RUN python3 -c "\
import hashlib, shutil, sys, sysconfig, urllib.request, zipfile; \
from pathlib import Path; \
url, want = '$VECTORS_URL', '$VECTORS_SHA'; \
raw = urllib.request.urlopen(url, timeout=300).read(); \
got = hashlib.sha256(raw).hexdigest(); \
sys.exit(f'vector archive digest {got} != {want}') if got != want else None; \
archive = Path('/tmp/vectors.zip'); \
archive.write_bytes(raw); \
zipfile.ZipFile(archive).extractall('/tmp/vectors'); \
src = Path('/tmp/vectors/pycryptodome-test-vectors-1.0.14/pycryptodome_test_vectors'); \
sys.exit(f'no package directory in the archive: {src}') if not src.is_dir() else None; \
dst = Path(sysconfig.get_paths()['purelib']) / 'pycryptodome_test_vectors'; \
shutil.copytree(src, dst); \
print('vectors:', sum(1 for p in dst.rglob('*') if p.is_file()), 'files ->', dst)" \
 && rm -rf /tmp/vectors /tmp/vectors.zip \
 && python3 -c "\
import os, sys; \
import pycryptodome_test_vectors as v; \
where = os.path.dirname(v.__file__); \
names = sorted(n for n in os.listdir(where) if not n.startswith('__')); \
sys.exit('the vector tree is empty') if not names else None; \
sys.exit(f'the vector tree has too few data directories: {names}') if len(names) < 4 else None; \
files = sum(1 for _, _, fs in os.walk(where) for _ in fs); \
sys.exit(f'the vector tree holds only {files} files') if files < 400 else None; \
print(f'vectors {v.__version__}: {files} files in {names}')"

# --- The candidate's test stack ----------------------------------------------
# pytest, pytest-timeout and their closure, in a virtual environment that is NOT
# the build interpreter.  A candidate's code runs here; the builds run in the
# system python.  The process orchestrating the stage does not share an interpreter
# with code an adversary wrote.
#
# --without-pip after the install: nothing in a graded run should be installing
# anything, and the image has no network beyond the model API.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --upgrade "pip==24.3.1"; \
    /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-candidate.txt; \
    /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')"; \
    # The separation is only real if it holds in this direction too: a candidate
    # that could `import setuptools` in its own process would get the same answer
    # on both trees and might read it as a fact about one of them.
    ! /opt/venv/bin/python -c "import setuptools" >/dev/null 2>&1; \
    echo "the candidate venv has neither backend"

# --- pycryptodome itself is NOT here, and this is the other assertion ---------
# The library under test must exist only as something a tree under test built.  An
# importable Crypto anywhere on the default path would let a candidate compare a
# build against the image and call the difference a migration defect -- and it
# would answer identically on both trees, so it would look like a clean finding.
#
# Both interpreters, and after the vector install rather than before: the vectors
# are a sibling package in the same site-packages, and a mistake in that copy is
# exactly how a Crypto/ would arrive here.
RUN set -eu; \
    for interpreter in /usr/local/bin/python3 /opt/venv/bin/python; do \
      if "$interpreter" -c "import Crypto" >/dev/null 2>&1; then \
        echo "FATAL: pycryptodome is importable in $interpreter." >&2; \
        "$interpreter" -c "import Crypto; print(Crypto.__file__)" >&2; \
        echo "The only Crypto in this stage must be the one a tree built." >&2; \
        exit 1; \
      fi; \
    done; \
    echo "pycryptodome is not importable: the only Crypto will be a build's own"

# --- The harness -------------------------------------------------------------
# Runs the six rounds and the adjudicator, on the system python.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# The interpreter that builds either tree, named rather than searched for.
ENV SRB_BUILD_PYTHON=/usr/local/bin/python3

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbcrypto.py; \
    # srbcrypto is the whole capability surface of this stage: four named
    # configurations, a wheel, the install it produces, and the library's own
    # suite.  A candidate that cannot import it can do nothing, and an __all__
    # entry that does not exist is a name the prompt advertises and the module does
    # not have.
    SRB_TARGET=/nonexistent SRB_TARGET_TOKEN=check \
    PYTHONPATH=/tests/verification/lib /opt/venv/bin/python -c "\
import sys; import srbcrypto as m; \
missing = [n for n in m.__all__ if not hasattr(m, n)]; \
sys.exit(f'srbcrypto.__all__ names missing: {missing}') if missing else None; \
leaked = [n for n in dir(m) if not n.startswith('_') and 'TARGET' in n.upper() \
          and n != 'TARGET_NAME']; \
sys.exit(f'srbcrypto exposes the tree under test: {leaked}') if leaked else None; \
print('srbcrypto ok: %d configurations, %d exports' \
      % (len(m.CONFIGURATIONS), len(m.__all__)))"; \
    # A scope-less probe is unwinnable and a mismatched adversary count creates
    # points nobody can earn.  Checked here as well as by `swerefactor validate`,
    # because this is the image that would otherwise discover it six rounds in.
    python3 /tests/verification/check-probe.py; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} +

# --- Both trees actually build, and this is the last assertion ----------------
# The import checks above prove the backends are installed.  This proves they
# work, on a package whose sources are not either tree: a two-file setuptools
# project and a two-file meson-python project, each built with the same
# `python -m build --wheel --no-isolation` a candidate's configuration uses.
#
# It is here because "setuptools is importable" and "setuptools can produce a
# wheel in this image" are different claims, and the second is the one the stage
# depends on.  A C toolchain that cannot link, a missing python3-dev header, a
# meson that cannot find ninja: all of them pass the import check and fail here,
# and all of them would otherwise be discovered as six rounds of "the original
# does not build".
RUN set -eux; \
    mkdir -p /tmp/smoke/st /tmp/smoke/mp/src; \
    cd /tmp/smoke/st; \
    printf '%s\n' \
      '[build-system]' \
      'requires = ["setuptools"]' \
      'build-backend = "setuptools.build_meta"' \
      '[project]' \
      'name = "srb-smoke-st"' \
      'version = "0"' > pyproject.toml; \
    printf 'def f():\n    return 1\n' > srb_smoke_st.py; \
    python3 -m build --wheel --no-isolation -o dist . >/dev/null; \
    cd /tmp/smoke/mp; \
    printf '%s\n' \
      '[build-system]' \
      'requires = ["meson-python"]' \
      'build-backend = "mesonpy"' \
      '[project]' \
      'name = "srb-smoke-mp"' \
      'version = "0"' > pyproject.toml; \
    printf '%s\n' \
      "project('srb-smoke-mp', 'c', version: '0')" \
      "py = import('python').find_installation(pure: false)" \
      "py.extension_module('_smoke', 'src/smoke.c', install: true)" > meson.build; \
    printf '%s\n' \
      '#include <Python.h>' \
      'static struct PyModuleDef m = {PyModuleDef_HEAD_INIT, "_smoke", NULL, -1, NULL};' \
      'PyMODINIT_FUNC PyInit__smoke(void) { return PyModule_Create(&m); }' \
      > src/smoke.c; \
    python3 -m build --wheel --no-isolation -o dist . >/dev/null; \
    python3 -c "\
import pathlib, sys, zipfile; \
st = sorted(pathlib.Path('/tmp/smoke/st/dist').glob('*.whl')); \
mp = sorted(pathlib.Path('/tmp/smoke/mp/dist').glob('*.whl')); \
sys.exit('the setuptools backend produced no wheel') if not st else None; \
sys.exit('the meson-python backend produced no wheel') if not mp else None; \
objects = [n for n in zipfile.ZipFile(mp[0]).namelist() if n.endswith('.so')]; \
sys.exit('meson-python built no extension: the C toolchain is not usable') \
    if not objects else None; \
print('both backends build:', st[0].name, '|', mp[0].name, objects)"; \
    rm -rf /tmp/smoke

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/verification
