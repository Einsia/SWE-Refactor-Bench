# =============================================================================
# SWERefactorBench -- lang03-sqlparse-python-to-go
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang03-behavioural:1 tasks/lang03-.../tests/behavioural
#
# Built from the same digest-pinned base and the same Debian snapshot as the
# agent environment, so a submission that builds there builds here for the same
# reasons.
#
# Three properties of this file are load-bearing:
#
#  1. It carries its own copy of original.tar.gz and never reads the submitted
#     tree to learn what State A was.  The baseline is how the suite tells an
#     unchanged file from a rewritten one, so it cannot come from the party being
#     graded.  The digest below is the same one environment/original.sha256
#     records; if the two copies ever diverge, one of the two builds fails on its
#     checksum instead of grading against the wrong baseline.
#
#  2. freeze.py runs here, at build time.  It unpacks the pinned upstream
#     sqlparse, generates the document set, and records the expected answer to
#     every case into an immutable blob.  By the time a submission exists the
#     answers are already fixed and digested; grading only compares against them.
#     Computing expectations at grading time would put the submission in the
#     container while the answers were being decided.
#
#  3. The reference does not survive the build.  In a same-language migration the
#     reference install is also what downstream code links against; here nothing
#     links against it -- State B is a Go module, and the only program ever run
#     against the pinned sqlparse is the Python half of the probe, which runs at
#     freeze time and never again.  So the reference is deleted with the rest of
#     the scratch, and what ships is the expectation blob it produced.  A
#     submission cannot consult an oracle that is not in the image.
#
# This task's source language cannot be removed from the image: python3 ships in
# the base and the suite is written in Python.  "The old implementation is gone"
# is therefore enforced behaviourally -- during grading every interpreter name on
# PATH is a shim that fails and records the attempt (lib/shim/pyshim.py, staged
# into the assets and installed by build.py).  Deleting python3 here would delete
# the suite.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-write copy)
#     /opt/original      State A, unpacked from original.tar.gz        (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# The suite reads its baseline from the frozen assets, not from /opt/original --
# that mount is there for the stages that compare trees.  Network: none.  Go is
# configured offline and the build has no proxy to reach.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM golang:1.25.12-bookworm@sha256:ea341baa9bd5ba6784f6d7161ace70544349a6242d54d34a0fbfd2c4d51c9d58 AS toolchain

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20260713T000000Z

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# The same snapshot as the environment image, and for the same reason: the
# toolchain closure has to be reproducible years from now.
#
# sources.list.d/debian.sources is removed, not just overwritten -- the base image
# ships its pins there, and writing /etc/apt/sources.list alone would leave the
# original in place and resolve against the live archive.
#
# Nothing is installed.  The environment image adds python3-pytest so a solver can
# run the reference's own suite; grading never runs pytest, and the ELF and archive
# inspection the structural phase does is implemented in pure Python (lib/golib.py)
# rather than shelled out to binutils.  So this stage only re-points apt and then
# asserts the closure it was calibrated against, which is what makes a base-image
# bump fail here instead of at grading time.
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
    go version | grep -Fx 'go version go1.25.12 linux/amd64'; \
    gofmt --help 2>&1 | head -1 | grep -F 'usage: gofmt'; \
    /usr/bin/python3 -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'; \
    tar --version | head -1

# The Go environment grading builds with.  vlib.base_env() constructs these from
# scratch for every command it runs rather than inheriting them, so these values
# are belt-and-braces for anything run by hand in this image -- but they must
# agree with vlib.base_env(), and the assertions below are what say so.
RUN set -eux; \
    go env -w GOFLAGS=-mod=mod; \
    go env -w GOPROXY=off; \
    go env -w GOSUMDB=off; \
    go env -w GOTOOLCHAIN=local; \
    go env -w CGO_ENABLED=0; \
    test "$(go env GOPROXY)" = "off"; \
    test "$(go env GOTOOLCHAIN)" = "local"

# ---------------------------------------------------------------------------
# Baseline: the suite's own authenticated copy of State A, plus the pinned
# upstream tarball the oracle is computed from.
#
# Two archives, and they are not interchangeable.  original.tar.gz is State A --
# the 73-file curated snapshot the solver receives, which is what "unchanged" and
# "rewritten" are measured against.  sqlparse-0.5.3.tar.gz is the PyPI sdist the
# expectations are frozen from.  State A is derived from that sdist but is not it:
# it has no packaging metadata, no CI config and no upstream test suite, so it
# cannot be installed and cannot answer a case.  Freezing from the sdist and
# diffing against State A is why both are here.
#
# original.tar.gz stores paths at its root rather than under a repo/ prefix, so the
# destination is created first and tar extracts into it.  The sdist does have a
# top-level sqlparse-0.5.3/ directory; build.ReferenceInstaller strips it.
# ---------------------------------------------------------------------------
FROM toolchain AS baseline

COPY --chown=0:0 data/original.tar.gz /run/baseline/original.tar.gz
COPY --chown=0:0 data/source-contract.json /run/baseline/source-contract.json
COPY --chown=0:0 data/sqlparse-0.5.3.tar.gz /run/baseline/sqlparse-0.5.3.tar.gz

RUN set -eux; \
    echo '1e1873b15c03f7984badb9c1a52a15cd416e4c8d21371bd3e2e5c906362b1b1f  /run/baseline/original.tar.gz' \
      | sha256sum -c -; \
    echo '2df0cf4b8486f5acac3d2ae44cdf7d0681be66171d131504d1ca606ac2d12b35  /run/baseline/source-contract.json' \
      | sha256sum -c -; \
    echo '09f67787f56a0b16ecdbde1bfc7f5d9c3371ca683cfeaa8e6ff60b4807ec9272  /run/baseline/sqlparse-0.5.3.tar.gz' \
      | sha256sum -c -; \
    test "$(stat -c '%s' /run/baseline/original.tar.gz)" = "73963"; \
    test "$(stat -c '%s' /run/baseline/sqlparse-0.5.3.tar.gz)" = "84999"; \
    install -d -m 0755 /run/unpack/repo; \
    tar --extract --file=/run/baseline/original.tar.gz --directory=/run/unpack/repo \
        --no-same-owner --no-same-permissions; \
    test -f /run/unpack/repo/sqlparse/__init__.py; \
    test -f /run/unpack/repo/pyproject.toml; \
    test "$(find /run/unpack/repo -type f | wc -l)" = "73"

# ---------------------------------------------------------------------------
# Freeze: unpack the reference, build the document set, record the expectations.
#
# freeze.py stages its own fixed inputs (the probe source, the API dumper, the
# interpreter shim, the source contract) plus a copy of the baseline tree into
# <out>/assets, so the whole grading input set is one directory that can be sealed
# in one step.
#
# Hence the doubled path.  The assets root is /opt/assets and the staged inputs are
# read from <root>/assets/... -- so `--out` is /opt/assets and the staged inputs
# land at /opt/assets/assets.  It reads oddly and it is not an accident: the outer
# directory is what gets sealed, and the inner one is the subset that was copied in
# rather than computed.  The paths asserted below are the driver's, spelled the way
# the driver spells them, so the two cannot drift without this build failing.
#
# /opt/assets and not /opt/swerefactor/assets: /opt/swerefactor arrives from the shared
# infra image and holds the runner every task uses.  One directory should not be
# half vendored harness and half this task's answers.
# ---------------------------------------------------------------------------
FROM baseline AS frozen

COPY --chown=0:0 . /tests/behavioural

# The build context is copied wholesale, so the archives land in /tests/behavioural
# too.  They are removed rather than left: lib/ is on the suite's import path, and
# a 73-file State A tarball sitting beside the grader is a tarball a submission
# could not read but an auditor would have to explain.  The authenticated copies
# under /run are the ones freeze.py uses, and they go too, at the end of this
# stage.
#
# The syntax gate on every module the suite will import runs before the expensive
# freeze rather than being discovered 40 minutes in.  The list is derived from the
# directory rather than typed out: a module added to lib/ and left out of a
# hand-written list would be the one module never syntax-checked.
#
# It uses the built-in compile() and not `python3 -m py_compile`, and that is the
# whole reason it is spelled this way.  py_compile writes __pycache__ next to each
# source file.  The image built green, and every grading run died in the
# self-check with "assets/probe/__pycache__/probe.cpython-311.pyc: recorded but
# absent from the image" -- measured, on State A.  compile() does the same parse
# and bytecode generation and writes nothing anywhere; redirecting py_compile at
# /dev/null is not an alternative, because it refuses non-regular targets on
# purpose.
#
# freeze.py gets -B, not PYTHONDONTWRITEBYTECODE=1.  It is run with -I, isolated
# mode implies -E, and -E ignores the environment -- so the variable had no effect
# and freeze's own imports left lib/__pycache__ behind.  Also measured.  The
# assertion at the end of this stage is what caught it, and it covers
# /tests/behavioural as well as the assets for that reason: nothing in the graded
# image should carry bytecode compiled at build time.
RUN set -eux; \
    rm -f /tests/behavioural/data/original.tar.gz \
      /tests/behavioural/data/sqlparse-0.5.3.tar.gz \
      /tests/behavioural/Dockerfile /tests/behavioural/.dockerignore; \
    find /tests/behavioural -name '*.py' -print0 \
      | xargs -0 -n 40 /usr/bin/python3 -c \
        'import sys; [compile(open(f, "rb").read(), f, "exec") for f in sys.argv[1:]]'; \
    test -z "$(find /tests/behavioural -name '__pycache__' -print -quit)"; \
    /usr/bin/python3 -I -B /tests/behavioural/lib/freeze.py \
      --tarball /run/baseline/sqlparse-0.5.3.tar.gz \
      --baseline /run/unpack/repo \
      --data /tests/behavioural/data \
      --out /opt/assets \
      --workspace /tmp/freeze; \
    rm -rf /tmp/freeze /run/unpack /run/baseline; \
    test -z "$(find /tests/behavioural /opt/assets \
      \( -name '__pycache__' -o -name '*.py[cod]' \) -print -quit)"

# The frozen assets are the answers; nothing that runs later has any business
# writing to them.  Grading runs as root because it installs a submission into a
# prefix and runs binaries out of it, so read-only bits are a guard rail rather
# than a boundary -- but they do mean an accidental write fails loudly.
#
# check-frozen.py re-asserts the published case floors, the exact declared counts
# and the per-family weight concentration against the manifest that actually
# shipped.  catalog.py enforces the same three when it builds, but that runs on a
# catalog in memory; this runs on the artifact being graded with.
#
# The last two assertions are about what must NOT be here.  freeze.py deletes the
# unpacked reference itself, and these check the outcome: no importable sqlparse
# anywhere the suite's interpreter would find it, and no reference tree left in the
# assets.  A suite that shipped its own oracle would let a submission read the
# answers instead of computing them, and the deletion is 400 lines away from the
# code that depends on it.
#
# -B for the same reason freeze.py gets it, one step earlier: -I implies -E, so the
# PYTHONDONTWRITEBYTECODE below cannot reach this interpreter, and check-frozen
# imports catalog which imports spec.  Without it this stage shipped
# lib/__pycache__/{catalog,spec}.cpython-311.pyc -- measured, in a built image,
# after the two assertions above had already passed.  They ran before the last step
# that could violate them, which is why the no-bytecode assertion is repeated at
# the end of this stage rather than only at the end of the previous one.
RUN set -eux; \
    /usr/bin/python3 -I -B /tests/behavioural/lib/check-frozen.py \
      --manifest /opt/assets/manifest.json; \
    test -f /opt/assets/expectations.bin; \
    test -f /opt/assets/assets/probe/probe.py; \
    test -f /opt/assets/assets/shim/pyshim.py; \
    test -f /opt/assets/assets/source-contract.json; \
    test "$(find /opt/assets/assets/baseline -type f | wc -l)" = "73"; \
    chmod -R a-w /opt/assets; \
    test "$(stat -c '%a' /opt/assets/catalog.json)" = "444"; \
    if /usr/bin/python3 -c 'import sqlparse' 2>/dev/null; then \
      echo 'FAIL: the verifier image can import sqlparse' >&2; exit 1; \
    fi; \
    test -z "$(find / -xdev -name 'sqlparse' -type d \
      -not -path '/opt/assets/assets/baseline/*' -print -quit)"; \
    test -z "$(find /tests/behavioural /opt/assets \
      \( -name '__pycache__' -o -name '*.py[cod]' \) -print -quit)"

# --- The harness -------------------------------------------------------------
# The module runner, the result contract and the scorer, shared by every task.
COPY --from=infra /opt/swerefactor /opt/swerefactor

# That COPY arrives carrying eight `.pyc` files tagged `cpython-310`: `infra/` is a
# build context with no `.dockerignore`, so the author's host bytecode rides in with
# the package.  On this image `python3 -V` is 3.11, so the tag does not match and the
# interpreter will never load them -- dead files, but a stale-module hazard the moment
# a name collides, and enough to fail the assertion at the end of this stage, which
# covers /opt/swerefactor precisely so that an inherited violation cannot pass quietly.
# Stripping here is what lets that assertion keep its scope instead of gaining an
# exception.
RUN find /opt/swerefactor -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/swerefactor -name '*.py[co]' -delete

# --- The manifest, checked at build time -------------------------------------
# suite.toml is the file most likely to be edited and least likely to be tested,
# and the family map in driver.py is the thing that decides whether a case is
# graded at all.  Both are checked here: a duplicate id, a weight that does not
# parse, a module directory that does not exist, or a case family no module claims
# fails the build rather than the run.
#
# Every modules/<id>/run.sh in this suite is a symlink to lib/run-module.sh, so the
# executable bit is set on the target once rather than fifteen times, and the loop
# below follows the links -- a dangling symlink is a module that would fail at
# grading time with "no such file", 40 minutes in.
RUN set -eux; \
    chmod +x /tests/behavioural/lib/run-module.sh; \
    for d in /tests/behavioural/modules/*/; do \
      test -x "$d/run.sh" || { echo "no runnable run.sh in $d" >&2; exit 1; }; \
    done; \
    PYTHONPATH=/opt/swerefactor /usr/bin/python3 -B -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/behavioural/suite.toml'); \
missing = [m.id for m in s.modules if not (s.root / (m.dir or f'modules/{m.id}')).is_dir()]; \
sys.exit(f'suite.toml names missing module dir(s): {missing}') if missing else None; \
print(f'suite: {len(s.modules)} modules, weight {s.weight_total:g}')"; \
    /usr/bin/python3 -I -B /tests/behavioural/lib/driver.py --self-check; \
    test -z "$(find /tests/behavioural /opt/swerefactor \
      \( -name '__pycache__' -o -name '*.py[cod]' \) -print -quit)"

# SWEREFACTOR_ASSETS is read by the driver and by suite.toml's [env]; the other two
# are documentation of the driver's defaults, which it holds as argparse defaults
# rather than requiring from the environment.  Kept because an operator running a
# module by hand in this image should see the same paths the runner would use.
ENV TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8 \
    PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/swerefactor \
    SWEREFACTOR_ASSETS=/opt/assets \
    SWEREFACTOR_REPO=/workspace/repo \
    SWEREFACTOR_LOG_DIR=/logs/verifier

RUN mkdir -p /opt/original /workspace /logs/verifier

WORKDIR /tests/behavioural
