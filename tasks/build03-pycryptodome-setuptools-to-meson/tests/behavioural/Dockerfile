# =============================================================================
# SWERefactorBench -- build03-pycryptodome-setuptools-to-meson
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/build03-behavioural:1 \
#         tasks/build03-pycryptodome-setuptools-to-meson/tests/behavioural
#
# Build context is this directory, so the suite is in the image and nothing else
# is.  The agent container never sees it.
#
# BOTH BACKENDS ARE HERE, AND THAT IS THE POINT OF THIS STAGE
#
# This stage grades behaviour preservation, and it grades it against State A.
# Every number in data/ was measured from a real setuptools build: the wheel's 336
# members, the 277 exported symbols per object, the artefact counts under four
# crippled compilers, the 39,245 self-test ids.  So State A has to be buildable
# *here*, or the suite cannot be run against the tree that defines what passing
# means -- and a suite its own reference fails is not measuring the reference.
#
# Withholding setuptools, distutils and pkg_resources and asserting all three
# unimportable would make "the submission stopped needing the old build system" a
# property of the environment, which is the tempting version of this image and the
# wrong one twice over.  That assertion is detection, and detection of the retired
# toolchain belongs to stage 1: `setuptools_retired` reads both trees with a
# reviewer, and a failure there scores the submission zero before this image
# starts.  Its price is that the reference cannot be built, which leaves every
# recorded expectation in data/ unverifiable in the image that grades against it.
#
# So the assertion below runs the same direction as stage 3's: both backends must
# be importable, and a missing one is fatal at image-build time.  The front end
# never names a backend -- `python -m build --wheel --no-isolation`, and the tree's
# own `[build-system]` table chooses -- so having both installed is what makes the
# front end neutral between the two states rather than a Meson harness.
#
# One consequence worth stating, because it is easy to assume otherwise: `import
# distutils` works in this image, through setuptools' vendored copy and its
# `distutils-precedence.pth`.  Nothing in the suite asserts otherwise.
#
# The same pinned interpreter as the agent image, because the ground truth in
# data/ is interpreter- and compiler-dependent: the symbol tables, the emitted
# instructions, the wheel tags and the 39,245 self-test ids were all recorded
# against a specific pair.  A different gcc or a different CPython would make
# State A's own answers wrong.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                       (read-write copy)
#     /logs/verifier     where the stage result JSON is written
#
# /opt/original is deliberately *not* required.  State A's side of every
# comparison in the suite comes from data/, recorded once and shipped, because
# ground truth a run can recompute is not ground truth.  That the image can build
# State A is what makes those recordings checkable, not a second source for them.
# Reading State A's sources is still stage 1's job.
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
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC

# --- The compiler, and the tools this suite reads objects with -----------------
# gcc and libc6-dev because 47 translation units have to compile.  binutils for
# readelf and nm, which is how the artifacts, symbols and isa modules ask what is
# actually in a delivered object rather than what a build file said about it.
# libgmp because Crypto.Math opens libgmp.so.10 through ctypes at import time and
# the frozen ground truth records `gmp` as State A's integer backend -- an image
# without it would score a correct submission down to a fallback.
#
# No autoconf, no automake, no cmake: the target build system is Meson, and it
# arrives through pip below rather than through apt, so its version is pinned to
# the byte along with everything else.
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc libc6-dev \
      binutils file \
      libgmp10 libgmp-dev \
      xz-utils \
      coreutils findutils diffutils \
    && rm -rf /var/lib/apt/lists/* \
    && gcc --version | head -1 \
    && readelf --version | head -1 \
    && python3 -c "import ctypes.util, ctypes; \
n = ctypes.util.find_library('gmp'); \
print('libgmp:', n); \
ctypes.CDLL(n)"

# --- The new toolchain, hash-pinned ------------------------------------------
# --require-hashes, so every byte of the toolchain is fixed.  The whole stage
# rests on "the submission built against a known toolchain", and a toolchain that
# moved between the run that recorded data/ and the run that grades a submission
# turns a measurement into a coincidence.
#
# --no-deps is not passed: the pins are closed over their own dependencies, and
# pip refusing to resolve is a better failure than pip quietly pulling an
# unpinned transitive package into an image that claims to be reproducible.
COPY requirements-target.txt /tmp/requirements-target.txt
RUN python3 -m pip install --require-hashes -r /tmp/requirements-target.txt \
    && rm /tmp/requirements-target.txt \
    && meson --version \
    && ninja --version \
    && patchelf --version \
    && python3 -c "import build, pytest, pyproject_metadata, packaging; \
print('build', build.__version__, '| pytest', pytest.__version__)" \
    && python3 -c "import setuptools, wheel; \
print('for State A: setuptools', setuptools.__version__, '| wheel', wheel.__version__)"

# --- The project's own test vectors, without a build backend ------------------
# 443 data files and no code.  The only distribution PyPI has is an sdist, and
# preparing an sdist's metadata builds it in an isolated environment that fetches
# its own *unpinned* build backend -- which `pip download --no-binary :all:` can
# decide to do as readily as `pip install`.  A resolver choosing what goes into an
# image that claims to be reproducible is the thing --require-hashes exists to
# prevent, and pinning the backends this image does install buys nothing if a
# nested build fetches another.  So the archive is fetched by URL and checked
# against its digest, with no build backend involved at any point.
#
# Crypto/SelfTest/loader.py reaches the package with
# `import pycryptodome_test_vectors` and `os.path.dirname(__file__)` -- no
# metadata, no entry points -- so copying the directory into site-packages is a
# complete install for this purpose.
#
# Without it the self-test collects 3,603 instances instead of 42,784, and the
# 39,000 known-answer tests that would notice a wrong compiler flag never run.
# That is a failure that looks like a pass, which is why the check below loads an
# actual vector instead of counting files.
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
 # The import path the loader will use, and the vector files behind it.  A
 # directory that exists but holds no vectors would let the self-test collect
 # 3,603 instances and report them as a pass.
 #
 # `Crypto` is deliberately not imported here: pycryptodome is not installed in
 # this image and must not be.  The only Crypto that exists during a run is the
 # one the submission's own wheel installs into a scratch tree, and importing a
 # different one at image-build time would be measuring the image.
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

# --- BOTH backends answer the front end, and this is the assertion ------------
# Checked by building rather than by importing, because an import proves the module
# is on the path and this stage needs the *front end* to reach the backend through
# `[build-system] build-backend`.  That is the one mechanism both states share and
# the only one `builder.py` uses.
#
# Two synthetic three-file projects, one per backend, built with exactly the argv
# the suite uses.  Each takes a couple of seconds and each fails loudly at
# image-build time rather than inside a graded run.
#
# The cost of getting this wrong is asymmetric, which is why it is fatal:
#
#   setuptools missing  -> State A cannot be built in the image that grades every
#                          submission against State A's recorded numbers, and the
#                          reference implementation fails the stage built from it.
#   meson-python missing -> every migrated submission fails to build and is
#                          reported as broken.
#
# What is deliberately NOT asserted, in either direction: that setuptools,
# distutils and pkg_resources are unimportable.  That is detection of the retired
# toolchain, stage 1's `setuptools_retired` owns it over both trees with a
# reviewer, and asserting it here would cost the image the ability to build its own
# oracle.
RUN set -eu; \
    for name in setuptools wheel mesonpy mesonbuild build pyproject_hooks; do \
      python3 -c "import $name" >/dev/null 2>&1 || { \
        echo "FATAL: the stage-2 image cannot import $name." >&2; \
        echo "This stage grades against numbers recorded from State A, so it has" >&2; \
        echo "to be able to build State A (setuptools/wheel) as well as a" >&2; \
        echo "migrated tree (meson-python).  A missing backend makes one of the" >&2; \
        echo "two unbuildable here and the comparison meaningless." >&2; \
        exit 1; }; \
    done; \
    mkdir -p /tmp/becheck/setuptools/pkgdemo /tmp/becheck/mesonpy; \
    cd /tmp/becheck/setuptools; \
    printf 'def f():\n    return 1\n' > pkgdemo/__init__.py; \
    printf '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n\n[project]\nname = "becheck-setuptools"\nversion = "0.0.1"\n' > pyproject.toml; \
    cd /tmp/becheck/mesonpy; \
    printf 'def f():\n    return 1\n' > mesondemo.py; \
    printf "project('becheck-mesonpy', 'c', version: '0.0.1')\npy = import('python').find_installation()\npy.install_sources('mesondemo.py')\n" > meson.build; \
    printf '[build-system]\nrequires = ["meson-python"]\nbuild-backend = "mesonpy"\n\n[project]\nname = "becheck-mesonpy"\nversion = "0.0.1"\n' > pyproject.toml; \
    for backend in setuptools mesonpy; do \
      cd "/tmp/becheck/$backend"; \
      python3 -m build --wheel --no-isolation -o dist > build.log 2>&1 || { \
        echo "FATAL: the front end could not build through the $backend backend:" >&2; \
        tail -30 build.log >&2; \
        exit 1; }; \
      test -n "$(ls dist/*.whl 2>/dev/null)" || { \
        echo "FATAL: the $backend backend produced no wheel" >&2; exit 1; }; \
      echo "  $backend -> $(basename dist/*.whl)"; \
    done; \
    cd /; rm -rf /tmp/becheck; \
    python3 -c "import setuptools, wheel, mesonpy; \
print('both backends answer the front end: setuptools', setuptools.__version__, \
'/ wheel', wheel.__version__, '/ meson-python', mesonpy.__version__)"

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor

# lib/ is on the path for `-p srbinputs` and for the helpers the modules import
# by name; /opt/swerefactor is there for `-p swerefactor.pytest_module`.
ENV PYTHONPATH=/opt/swerefactor:/tests/behavioural/lib

# --- The suite ---------------------------------------------------------------
# Copied last so iterating on a module does not rebuild the toolchain layer.
COPY . /tests/behavioural

RUN chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh \
 # The frozen ground truth has to be present and parseable, or every comparison
 # in the suite silently has nothing to compare against.  Both the plain and the
 # gzipped files are opened, because selftest.json.gz is the largest piece of
 # ground truth here and a truncated copy of it reads as a passing suite.
 && python3 -c "\
import gzip, json, pathlib, sys; \
d = pathlib.Path('/tests/behavioural/data'); \
plain = sorted(d.glob('*.json')); \
packed = sorted(d.glob('*.json.gz')); \
[json.loads(p.read_text()) for p in plain]; \
[json.load(gzip.open(p, 'rt', encoding='utf-8')) for p in packed]; \
sys.exit('no ground truth in data/') if not (plain or packed) else None; \
print(f'ground truth: {len(plain)} json + {len(packed)} json.gz')" \
 # The self-test ground truth decides 76 checks on its own, so its shape is
 # asserted rather than assumed: a `tests` map that lost its entries, or an
 # `implementation` that is a string holding a dict's repr, both read as a
 # passing module.
 && python3 -c "\
import gzip, json, sys; \
d = json.load(gzip.open('/tests/behavioural/data/selftest.json.gz', 'rt', encoding='utf-8')); \
sys.exit('selftest ground truth has no tests') if len(d.get('tests', {})) < 30000 else None; \
sys.exit('implementation must be an object') if not isinstance(d.get('implementation'), dict) else None; \
groups = {'.'.join(k.split('.')[:4]) for k in d['tests']}; \
print(f'selftest ground truth: {len(d[\"tests\"])} ids in {len(groups)} groups, {d[\"implementation\"]}')" \
 # The manifest is the thing most likely to be edited and least likely to be
 # tested, so it is checked at build time: a malformed weight, a duplicate id or
 # a module directory that does not exist fails the build, not the run.
 && PYTHONPATH=/opt/swerefactor python3 -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')" \
 # Every pytest module must at least import and collect.  A module whose test
 # file has a syntax error collects nothing, and a module that collects nothing
 # is scored as a harness error -- found here rather than during a graded run.
 #
 # Both plugins, and the exit code kept.  Two bugs lived in the earlier version of
 # this line and they hid each other: `-p swerefactor.pytest_module` was missing, so
 # `--strict-markers` rejected `@pytest.mark.srb_weight` and the cleanliness module
 # failed to collect at all -- and `| tail -3` handed the pipeline the exit status
 # of `tail`, so the image built anyway with that error scrolling past in the log.
 # The plugin list here is now exactly lib/run-pytest.sh's, which is the only way
 # this check can answer the question it is asking.
 && cd /tests/behavioural \
 && collect_log=$(mktemp) \
 && if ! python3 -m pytest -c pytest.ini -p srbinputs -p swerefactor.pytest_module \
      --collect-only -q modules > "$collect_log" 2>&1; then \
      echo "FATAL: the suite does not collect in this image:" >&2; \
      tail -30 "$collect_log" >&2; \
      exit 1; \
    fi \
 && tail -3 "$collect_log" \
 && rm -f "$collect_log"

WORKDIR /tests/behavioural
