# =============================================================================
# fw06 stage 3 -- the verification probe's image.
#
# What this image is for: give six models an hour each to find one request the
# original answers correctly and the submission does not.  Stage 2 asks whether
# the submission reproduces a fixed recording; this stage asks whether anything
# OUTSIDE that recording diverges, which means it has to be able to run both trees
# and compare them live.
#
# That single requirement is what makes this image different from the other two.
# It is the only one carrying two module mirrors, because State A imports the
# retired router and must build, while the submission must be built against a
# mirror that will not serve it.  Stage 1 has no Go toolchain at all; stage 2 has
# one mirror, the pruned one.
#
# Three things have to be true when this image ships, and each is asserted below:
#
#   1. State A builds from the baseline mirror and answers HTTP.  If it does not,
#      every candidate "fails on the original", the adjudicator upholds none of
#      them, and all six rounds record a survival that was never earned.  This
#      is the failure mode that matters most here, because it is silent and it
#      pays the submission all 60 points.
#
#   2. The pruned mirror still refuses the retired router's archive, exactly as
#      stage 2's does.  A submission that compiles here and not there -- or the
#      reverse -- would gain or lose points for a difference between two mirrors.
#
#   3. probe.toml declares the six adversaries the scoring policy pays for, and
#      every flag its profiles pass is a flag the original's FlagSet actually
#      declares.  See check-probe.py: this server uses flag.ExitOnError, so one
#      invented flag name means no candidate can be run at all, which again scores
#      as six free survivals.
#
# Network is available while this builds and gone when it runs.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# golang:1.25 -- go1.25.12, the same digest stage 2 and environment/Dockerfile pin.
#
# Not a nearby version.  State A's go.mod declares `go 1.25.0` and GOTOOLCHAIN is
# local in every image, so a 1.24 toolchain here would refuse the module outright
# and neither tree would build.
FROM golang@sha256:9006890ecba0a168034d99516084099ae3114d9f2b7d6572c77f2dde57ebc980

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    GOTOOLCHAIN=local \
    GOFLAGS=-mod=mod \
    GOPATH=/opt/go \
    GOMODCACHE=/opt/go/pkg/mod \
    GOCACHE=/opt/gocache \
    GOSUMDB=off \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        jq \
        procps \
        file \
        xxd \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

# --- swerefactor, for the adjudicator and the probe loader ----------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The candidate's Python stack --------------------------------------------
# In a venv rather than --break-system-packages: Debian's python3-packaging ships
# without a RECORD file, so pip refuses to replace it with the pinned version.
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir \
        -r /opt/pins/requirements-candidate.txt \
 && /opt/venv/bin/python -c \
        "import pytest; print('candidate stack: pytest', pytest.__version__)"

# The Go toolchain stays first: run-candidate.sh shells out to `go`.  The venv
# comes before /usr/bin so that run-candidate.sh's bare `python3` is the
# interpreter holding pytest.
ENV PATH=/usr/local/go/bin:/opt/go/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# --- The lockfiles and the two module lists ----------------------------------
# The same four files stage 2 digests, at the same digests.  Verified byte for
# byte rather than assumed: the two mirrors are built by two scripts in two build
# contexts, and if their inputs drift the symptom is a submission that compiled in
# stage 2 and fails here, naming a module it never touched.
ENV SRB_GOMOD_SHA256=351b1f3a2655d666907972682e4bfc0e844cfe0d2bfd3b18f77d9f701f74b712 \
    SRB_GOSUM_SHA256=1b578b5ce75f9716fddeca4db53d5e76bdad7b2331312c57521fee3d5e1df933 \
    SRB_RETIRED_SHA256=0804f4f27db2b32ea768d37ed840e3061e851bf73e80340672d9cea4ae54373c \
    SRB_BANNED_SHA256=bc2bb655a9791420d19ab9bec84032d570e8933884ec2f36df261545f0bfae86

COPY data/lockfiles/go.mod               /opt/verification/go.mod
COPY data/lockfiles/go.sum               /opt/verification/go.sum
COPY data/lockfiles/retired-modules.txt  /opt/verification/retired-modules.txt
COPY data/lockfiles/banned-routers.txt   /opt/verification/banned-routers.txt

RUN set -eu; \
    echo "$SRB_GOMOD_SHA256  /opt/verification/go.mod"               | sha256sum -c -; \
    echo "$SRB_GOSUM_SHA256  /opt/verification/go.sum"               | sha256sum -c -; \
    echo "$SRB_RETIRED_SHA256  /opt/verification/retired-modules.txt"  | sha256sum -c -; \
    echo "$SRB_BANNED_SHA256  /opt/verification/banned-routers.txt"    | sha256sum -c -; \
    echo "lockfiles and both lists match stage 2's and the environment's inputs"

# --- The two offline module mirrors -------------------------------------------
# One warm, one copy, one prune -- see lib/build-goproxy.sh for why the copy has
# to happen before the prune and not as a second warming pass.
COPY lib/build-goproxy.sh /opt/verification/build-goproxy.sh
RUN chmod +x /opt/verification/build-goproxy.sh \
 && SRB_RETIRED_MODULES=/opt/verification/retired-modules.txt \
    SRB_BANNED_ROUTERS=/opt/verification/banned-routers.txt \
    /opt/verification/build-goproxy.sh /opt/verification /opt/goproxy \
        /opt/goproxy-baseline

# GOPROXY is off by default; run-candidate.sh sets it per tree, to one of these
# two, and that assignment is the only place the role has any effect.
ENV SRB_GOPROXY_ROOT=/opt/goproxy \
    SRB_GOPROXY_BASELINE=/opt/goproxy-baseline \
    GOPROXY=off \
    GONOSUMDB= \
    GOPRIVATE=

# --- The fixture docroot ------------------------------------------------------
# The same nine files stage 2 serves, at the same stamped mtime.  Byte-identical
# because a candidate is allowed to assert on a fixture's Last-Modified, and a
# docroot carrying checkout mtimes would make that assertion find a divergence
# that is an artifact of when the image was built.
COPY data/fixtures/docroot /opt/fixtures/docroot

ENV SRB_FIXTURE_ROOT=/opt/fixtures/docroot \
    SRB_FIXTURE_AGGREGATE=bfa364db081206cba49def81caf967e5b80785bcdcfca4c4565017c5d6b51c2f \
    SRB_FIXTURE_MTIME="2026-01-01 00:00:00 UTC"

# The aggregate formula is stage 2's, character for character, and that matters
# more than it looks.  Any stable hash over these nine files would catch a drifted
# docroot; only the SAME hash lets the two numbers be compared.  A draft here
# folded the digests with a `while read` loop -- correct, stable, and a completely
# different value, which turns an assertion that the two stages serve one tree into
# an assertion that this stage serves whatever it shipped with.
#
# NUL-delimited under LC_ALL=C because `sp ace.txt` is one of the nine.
RUN set -eu; \
    cd /opt/fixtures/docroot; \
    find . -depth -exec touch -h -d "$SRB_FIXTURE_MTIME" {} + ; \
    got=$(find . -type f -print0 | LC_ALL=C sort -z \
            | xargs -0 sha256sum | sha256sum | cut -d' ' -f1); \
    n=$(find . -type f | wc -l); \
    echo "fixture docroot: $n files, aggregate $got"; \
    [ "$n" = "9" ] || { echo "FAIL: expected 9 fixture files, found $n" >&2; exit 1; }; \
    [ "$got" = "$SRB_FIXTURE_AGGREGATE" ] || { \
        echo "FAIL: this stage does not serve the tree stage 2 recorded against." >&2; \
        echo "  A candidate may assert on a fixture's Last-Modified or ETag, and" >&2; \
        echo "  those would then diverge for a reason neither tree is responsible" >&2; \
        echo "  for." >&2; \
        echo "  expected $SRB_FIXTURE_AGGREGATE" >&2; \
        echo "  actual   $got" >&2; exit 1; }; \
    test -f 'sp ace.txt'; \
    test -f 'odd+name.txt'; \
    test -f sub/b.txt; \
    test "$(stat -c%s big.bin)" = "4096"; \
    echo "fixture docroot verified against stage 2's aggregate"

# The stamp end to end: seed a docroot the way run-candidate.sh will, and check the
# mtime a server would report is the second the fixture set claims.  Asserting the
# env var's text would only prove it was typed correctly; this proves `touch -d`
# accepts it and lands where stage 2's recording sits.
RUN set -eu; \
    d=$(mktemp -d); \
    cp -r /opt/fixtures/docroot "$d/docroot"; \
    find "$d/docroot" -depth -exec touch -h -d "$SRB_FIXTURE_MTIME" {} + ; \
    got=$(date -u -r "$d/docroot/a.txt" '+%a, %d %b %Y %H:%M:%S GMT'); \
    want="Thu, 01 Jan 2026 00:00:00 GMT"; \
    [ "$got" = "$want" ] || { \
        echo "FAIL: the stamped mtime is '$got', not '$want'" >&2; exit 1; }; \
    echo "fixture mtime stamps to $got"; \
    rm -rf "$d"

# --- The probe itself ---------------------------------------------------------
COPY probe.toml         /tests/verification/probe.toml
COPY prompt.txt         /tests/verification/prompt.txt
COPY run-candidate.sh   /tests/verification/run-candidate.sh
COPY check-probe.py     /tests/verification/check-probe.py
COPY lib/wire.py        /tests/verification/lib/wire.py
COPY lib/srbcandidate.py /tests/verification/lib/srbcandidate.py

RUN chmod +x /tests/verification/run-candidate.sh

# The candidate plugin is loaded by name (`-p srbcandidate`), so its directory has
# to be importable.  On PYTHONPATH rather than pip-installed: it is two files, and
# a package here would need a version number nothing would ever read.
ENV PYTHONPATH=/tests/verification/lib:/opt/swerefactor

# --- Assertion 3: the probe is coherent --------------------------------------
# Before the expensive check, because it is the cheap one.
RUN python3 /tests/verification/check-probe.py

# --- Assertion 1: State A builds from the baseline and answers HTTP ----------
#
# Both probes below use a throwaway GOMODCACHE, and that is not tidiness.  A warm
# module cache makes GOPROXY irrelevant -- `go build` reads an already-extracted
# module without consulting any proxy at all.  Measured: with the image-wide cache,
# this probe extracted gorilla/mux and the NEXT layer, whose whole purpose is to
# prove the pruned mirror refuses that module, compiled it happily.
#
# The same mechanism is why the shipped cache is asserted empty further down.  Had
# this probe been allowed to warm /opt/go/pkg/mod, the image would have carried an
# extracted copy of the retired router, and a submission that never migrated could
# have compiled against it.
#
# The one that matters most, because its failure mode is silent and generous.
#
# State A is not in this build context and must not be -- shipping a working
# gorilla/mux checkout inside the grader is what stage 1's snapshot refuses to do.
# So what is proved here is the weaker statement that can be proved without it:
# that the baseline mirror serves an archive of the retired router, that a module
# requiring it resolves and compiles offline, and that the candidate plugin
# imports.  build-goproxy.sh already asserted the .zip is present; this compiles
# something against it, which is the part a present-but-corrupt archive fails.
RUN set -eu; \
    work=$(mktemp -d); cd "$work"; \
    cp /opt/verification/go.mod /opt/verification/go.sum .; \
    mkdir -p probe; \
    printf '%s\n' \
      'package main' \
      '' \
      'import (' \
      '	"fmt"' \
      '	"net/http"' \
      '' \
      '	"github.com/gorilla/mux"' \
      ')' \
      '' \
      'func main() {' \
      '	r := mux.NewRouter()' \
      '	r.PathPrefix("/files").HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {' \
      '		w.WriteHeader(http.StatusNoContent)' \
      '	})' \
      '	fmt.Println("the retired router compiles from the baseline mirror")' \
      '}' > probe/main.go; \
    GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy-baseline" GOSUMDB=off \
      GOPRIVATE= GONOSUMDB= GOTOOLCHAIN=local \
      GOMODCACHE="$work/gomod" GOCACHE="$work/gocache" \
      go build -o /tmp/baseline-probe ./probe; \
    /tmp/baseline-probe; \
    rm -rf "$work" /tmp/baseline-probe

# --- Assertion 2: the pruned mirror refuses it -------------------------------
# The mirror image of the above, and the reason a fake port cannot pass stage 3
# either.  A build that SUCCEEDS here is the failure.
RUN set -eu; \
    work=$(mktemp -d); cd "$work"; \
    cp /opt/verification/go.mod /opt/verification/go.sum .; \
    mkdir -p probe; \
    printf '%s\n' \
      'package main' \
      '' \
      'import (' \
      '	"net/http"' \
      '' \
      '	"github.com/gorilla/mux"' \
      ')' \
      '' \
      'func main() {' \
      '	_ = mux.NewRouter()' \
      '	_ = http.StatusOK' \
      '}' > probe/main.go; \
    if GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy" GOSUMDB=off \
         GOPRIVATE= GONOSUMDB= GOTOOLCHAIN=local \
         GOMODCACHE="$work/gomod" GOCACHE="$work/gocache" \
         go build -o /tmp/pruned-probe ./probe 2>/tmp/pruned.log; then \
      echo "FAIL: the retired router compiled from the PRUNED mirror. The" >&2; \
      echo "dependency gate is not being enforced in this stage, so a" >&2; \
      echo "submission that never migrated would build and be attacked as" >&2; \
      echo "though it had." >&2; \
      exit 1; \
    fi; \
    grep -qi "zip\|not found\|missing\|cannot find\|no matching" /tmp/pruned.log \
      || { echo "the pruned build failed for an unexpected reason:" >&2; \
           cat /tmp/pruned.log >&2; exit 1; }; \
    echo "the pruned mirror refuses the retired router's archive, as stage 2's does"; \
    rm -rf "$work" /tmp/pruned.log

# --- The shipped module cache holds no retired module -------------------------
# The leak the two probes above are careful not to create, checked rather than
# trusted.  An extracted module in the image's own GOMODCACHE is served to any
# build that does not override the variable, whatever GOPROXY says -- so a single
# warm copy of gorilla/mux here would undo the dependency gate for the whole stage,
# and it would do it silently: the fake port compiles, boots, and answers every
# request exactly as the original does, so no adversary can find a divergence and
# all six rounds report a survival.
#
# run-candidate.sh sets GOMODCACHE per target token and so never reads this path.
# The assertion is for the case that is not run-candidate.sh: a reviewer building a
# tree by hand inside this image, and any future layer added above.
RUN set -eu; \
    for mod in $(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' \
                     /opt/verification/retired-modules.txt | tr -d '[:blank:]'); do \
      if [ -e "/opt/go/pkg/mod/$mod" ] || \
         [ -n "$(find /opt/go/pkg/mod/cache/download/$mod -name '*.zip' 2>/dev/null)" ]; then \
        echo "FAIL: $mod is present in the image's module cache." >&2; \
        echo "A build that does not override GOMODCACHE would compile against it" >&2; \
        echo "regardless of GOPROXY, so an unmigrated submission would build and" >&2; \
        echo "behave identically to the original -- six unearned survivals." >&2; \
        exit 1; \
      fi; \
    done; \
    echo "the shipped module cache holds no retired module"

# --- The candidate plugin imports --------------------------------------------
# A syntax error here would surface as every candidate failing on both trees,
# which reads as six survivals.  Checked at build time for that reason.
RUN python3 -c "\
import sys; sys.path.insert(0, '/tests/verification/lib');\
import srbcandidate, wire;\
print('candidate plugin: %d profiles, wire sender present: %s'\
      % (len(srbcandidate.PROFILES), hasattr(wire, 'request')))"

# lib/wire.py must stay byte-identical to stage 2's harness/wire.py.  Asserted
# because the whole claim that a stage-3 finding is a real divergence rests on
# both stages asking the question with the same client.
RUN set -eu; \
    got=$(sha256sum /tests/verification/lib/wire.py | cut -d' ' -f1); \
    want=3e27fe4e7a2665df95bf9d1a867d2b5cf32a183febe564df9c99fd1db74855bb; \
    [ "$got" = "$want" ] || { \
      echo "lib/wire.py is not stage 2's sender (got $got)" >&2; exit 1; }; \
    echo "wire sender matches stage 2 byte for byte"

WORKDIR /workspace
ENV SRB_INFRA=/opt/swerefactor \
    SRB_RESULTS=/logs/verifier

RUN mkdir -p /workspace /logs/verifier /opt/original

CMD ["/bin/bash"]
