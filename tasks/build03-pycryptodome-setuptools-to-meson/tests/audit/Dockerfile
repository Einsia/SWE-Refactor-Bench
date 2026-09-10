# =============================================================================
# SWERefactorBench -- build03-pycryptodome-setuptools-to-meson
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/build03-audit:1 \
#         tasks/build03-pycryptodome-setuptools-to-meson/tests/audit
#
# A reviewer's workbench.  It holds a model client, tools for reading code, and
# two trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts rather than image layers, because the same image reviews
# every submission for this task.  The original is mounted read-only and the
# workspace is a copy: a review must not be able to change what stage 2 later
# measures, and it must not be able to change the thing it is comparing against.
#
# There is deliberately neither Meson nor a compiler here.  For a build-system
# migration that is the whole point, so it is worth being explicit: this stage
# answers six questions -- did setuptools leave, is Meson the build rather than a
# wrapper, is the compiler detection real, is the artifact shape deliberate, were
# the sources left alone, does the build know it is being graded -- and every one
# of them is settled by reading build code against State A's build code.  Give the
# review a Meson and it starts configuring and grading the output, which is stage
# 2's job in an image built to do it: stage 2 builds the submission five times,
# runs 39,245 of pycryptodome's own self-tests against the install tree, diffs the
# dynamic symbol table of 41 libraries and takes `-maes` away from the compiler to
# see whether the build notices.  Those are measurements.  This stage is for the
# judgement no build log settles -- whether eleven compiler questions are being
# asked or remembered.
#
# The asymmetry runs the other way too, and the three images together are the
# reason the split holds: this stage has no build system at all, so nothing
# submitted can execute and the review is by reading.  Stages 2 and 3 can both build
# either state, because every number stage 2 scores against was recorded from the
# setuptools build and "the setuptools build passes this test" is a claim an
# adversary has to be able to execute.  Which build system a submission actually
# uses is therefore decided *here*, by `setuptools_retired` and `meson_is_the_build`,
# with both trees open -- not by what a container happens to have installed.
#
# One hazard peculiar to a *Python* subject, and the reason for the pip surgery
# below.  For build01 a stage-1 image with no CMake genuinely cannot build the
# submission.  Here the submission is a PEP 517 package, so `pip install
# /opt/workspace` is one line away from downloading meson-python and running the
# submission's build backend -- in the stage whose whole claim is that nothing
# submitted executes.  So pip and setuptools are removed after the scan's four
# packages are installed, and `ensurepip` with them, because `python -m ensurepip`
# carries its own bundled pip wheel and works offline.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by reading
# only; the tools the runner exposes are list_dir, fetch_source, search and diff.
# ripgrep, diffutils and tree back those; the rest is for a human reading a
# transcript afterwards.
#
# Network: the model API and nothing else.  It is the one stage that is not
# offline, which is the other half of why it gets no toolchain -- there is no point
# in this container where submitted code executes.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# The same pinned interpreter the agent image uses.  It matters less here than it
# does in stage 2 -- nothing in this image imports the payload -- but the review
# reads Python code and quotes version-dependent behaviour at it, and there is no
# reason for two Pythons in one task.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

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
        less \
        ripgrep \
        tree \
    && rm -rf /var/lib/apt/lists/*

# --- The harness -------------------------------------------------------------
# swerefactor.models talks to the API over urllib, so there is no vendor SDK here
# and nothing to drift.  The credential arrives as an environment variable at run
# time and is never baked into a layer.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor:/tests/audit/lib

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# The prompt is this task's most load-bearing file: it is what replaced the regex
# gate list.  Check at build time that it is present, that it is prose rather than
# a stub, and that its placeholders are ones the runner fills -- an unknown
# placeholder survives into the transcript verbatim, which is visible but late.
RUN python -c "\
import re, sys, pathlib; \
from swerefactor import audit; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
sys.exit('prompt.txt is too short to be a review brief') if len(text) < 2000 else None; \
found = set(re.findall(r'{{\s*([a-z_]+)\s*}}', text)); \
unknown = sorted(found - set(audit.PLACEHOLDERS)); \
sys.exit(f'prompt.txt uses unknown placeholder(s): {unknown}') if unknown else None; \
sys.exit('prompt.txt has no {{gates}} placeholder, so the gates declared in \
evaluation.toml would never reach the review') if 'gates' not in found else None; \
sys.exit('prompt.txt has no {{findings}} placeholder, so the scan would run and \
be discarded') if 'findings' not in found else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# --- The read-only scan ------------------------------------------------------
# pytest is here and a toolchain is not, which is not the contradiction it looks
# like.  The scan is three pytest modules that hash files and match strings; they
# import nothing from the submission and could not -- there is no Meson and no
# compiler in this image, so a module that tried to build anything would die on a
# missing binary.  What pytest buys is the module contract: a scan module and a
# stage-2 module are byte-for-byte the same shape, so one can move between the
# stages without a rewrite, and `swerefactor.pytest_module` turns each collected test
# into one check keyed by nodeid without a parallel runner having to exist.
#
# Pinned by hash, from the same versions stage 2 pins its pytest with, so the two
# stages cannot drift into different collection behaviour.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# --- Removing the ability to build anything ----------------------------------
# The four packages above are in.  Now take away the machinery that could install
# a fifth, because for this subject that machinery is also the machinery that
# builds the submission.
#
# `pip install /opt/workspace` would resolve the submission's build-backend
# requirement, fetch meson-python and meson from the network this stage has, and
# run the submitted build.  Deleting pip is what makes that a missing-command
# error instead of a silent success.  `ensurepip` goes too: it ships a pip wheel
# inside the standard library and `python -m ensurepip` restores pip with no
# network at all.  setuptools and its `_distutils_hack` go because they are the
# retired toolchain -- an import of `distutils` in this image should fail the way
# it fails on a clean 3.12, not succeed through a vendored copy.
RUN pip uninstall -y pip setuptools wheel 2>/dev/null || true; \
    rm -rf /usr/local/lib/python3.12/ensurepip \
           /usr/local/lib/python3.12/site-packages/pip \
           /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
           /usr/local/lib/python3.12/site-packages/setuptools \
           /usr/local/lib/python3.12/site-packages/setuptools-*.dist-info \
           /usr/local/lib/python3.12/site-packages/_distutils_hack \
           /usr/local/lib/python3.12/site-packages/distutils-precedence.pth \
           /usr/local/lib/python3.12/site-packages/wheel \
           /usr/local/lib/python3.12/site-packages/wheel-*.dist-info \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
    && python -c "import pytest; print('pytest', pytest.__version__)"

# Note what this image does *not* carry: a copy of State A's source checksums.
# Stage 2 grades against a frozen `data/baseline.json`, because by the time it runs
# it holds five build trees and no reference tree.  This stage has the reference
# tree mounted at /opt/original, so the scan hashes it directly.  One fewer
# duplicated input, no manifest that can be regenerated alone and drift, and a
# stronger claim: the comparison is against the same State A the reviewer is
# reading, not against a file asserting what State A used to contain.

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.
#
# The floor is 60.  With no reference tree mounted the frozen-source comparison
# contributes its six unparametrized checks and one placeholder rather than 583, so
# the build-time count is 81 (51 buildsystem + 23 provenance + 7 sources) and the
# run-time count is 663 (51 + 23 + 589, where 589 is those 583 plus the same six).
# That gap is exactly the risk in deriving a parametrize list from a mount, so
# collect-check.sh's second phase points SRB_ORIGINAL at a synthetic tree holding
# four frozen files and asserts the count follows it, and its third phase mounts a
# synthetic submission and asserts the *constant*-parametrized module does not
# follow it -- an empty submission must not be able to reduce the number of checks
# run against it.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 60

# A build-system review that cannot read the build system it is comparing against
# has nothing to do, so assert here that no toolchain leaked in through a base
# image update or a careless apt line.  pip is on this list for the reason given
# above: here it is not a package manager, it is a build front end.
RUN for forbidden in meson ninja cmake make gcc cc g++ ld pip pip3 pkg-config; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden -- a review" >&2; \
            echo "that can build starts grading build logs, which is stage 2." >&2; \
            exit 1; \
        fi; \
    done

# The same assertion at the import level, which is where it would actually happen.
# A scan module cannot `import mesonbuild` or `import setuptools` and get anywhere,
# and `import distutils` fails as it does on any clean 3.12 -- which is the fact
# that makes this migration necessary in the first place.
RUN python - <<'PY'
import importlib.util, sys
forbidden = ("mesonbuild", "meson", "setuptools", "distutils", "pkg_resources",
             "pip", "build", "wheel", "ensurepip")
present = [m for m in forbidden if importlib.util.find_spec(m) is not None]
if present:
    sys.exit("FATAL: the stage-1 image can import " + ", ".join(present)
             + " -- a scan module could build the submission with it")
print("no build machinery importable:", ", ".join(forbidden))
PY

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
