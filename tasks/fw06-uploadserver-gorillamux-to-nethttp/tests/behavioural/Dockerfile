# =============================================================================
# fw06 stage 2 -- the behavioural suite's image.
#
# What this image is for: build the submission, run it, and compare what it
# answers on the wire against what State A answered on the same 87 requests.
# It never reads a line of the submitted source.  There is no `repo` fixture in
# this suite and `lib/srbcheck.py` has no filesystem access to /workspace/repo,
# so "grade behaviour, not implementation" is a property of the plumbing rather
# than a rule the module author has to remember.  Reading source is stage 1's
# job; constructing new inputs is stage 3's.
#
# Four things have to be true when this image ships, and each one is asserted
# below rather than assumed:
#
#   1. The submission can be resolved and compiled OFFLINE, against a module
#      mirror carrying the same versions the agent had and no alternative router.
#      Built here from the committed lockfiles by lib/build-goproxy.sh, then
#      checked by lib/check-mirror.py.  A drift between the mirrors would
#      otherwise surface as an honest submission failing to resolve a module it
#      never touched.
#
#      That mirror is COMPLETE: it serves the retired router's archive.  This
#      stage grades one thing -- whether the rewrite still answers what the
#      original answered -- and the original imports that router, so a mirror
#      that refused it could not build the very tree every expectation here was
#      recorded from.  Whether the rewrite actually happened is stage 1's
#      question, asked over both trees' source, and a stage-1 failure zeroes the
#      submission before this image runs.  The authoring constraint is unchanged:
#      the AGENT's mirror is still pruned, so a tree that keeps importing the
#      retired router cannot be built by the agent in the first place.  A second,
#      pruned mirror is built here for one unscored observation and nothing else.
#
#   2. The fixture docroot is byte-identical to the one the recording was
#      captured against, and its mtimes are stamped to a constant.  The corpus
#      sends `If-Modified-Since: Thu, 01 Jan 2026 00:00:00 GMT` and the
#      recording holds an unmasked `Last-Modified` for the same instant, so a
#      docroot carrying checkout mtimes turns `ims-equal` from a 304 into a 200
#      for every submission, correct ones included.
#
#   3. The suite agrees with its own recording.  All 87 corpus case ids appear
#      in data/golden-statea.json and nothing else does -- checked id by id, not
#      by count, because two edits that cancel out keep the count.
#
#   4. Every module in suite.toml exists, and every pytest module collects.  A
#      collection error at grading time is indistinguishable from a submission
#      that broke the module: both read as zero.  It is found here instead.
#
# Network is available while this builds and gone when it runs.  That asymmetry
# is the entire reason the mirror is baked in.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# golang:1.25 -- go1.25.12, the same digest environment/Dockerfile pins.
#
# Not a nearby version.  State A's go.mod declares `go 1.25.0` and GOTOOLCHAIN
# is local in both images, so a 1.24 toolchain here would refuse the module
# outright -- the required build module would fail for every submission, and the
# error would name the toolchain rather than anything a submission controls.
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

# xxd and file earn their place: the fixture docroot holds a PNG, a 4 KiB binary
# and two extensionless files, and when a body comparison fails the useful thing
# to have in the log is a hexdump rather than mojibake.
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

# --- The runner's Python stack, hash-verified --------------------------------
# In a venv rather than --break-system-packages: Debian's python3-packaging ships
# without a RECORD file, so pip refuses to replace it with the pinned version and
# the build fails on a message about an uninstallable package.
COPY requirements-runner.txt /opt/pins/requirements-runner.txt

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
        -r /opt/pins/requirements-runner.txt \
 && /opt/venv/bin/python -c \
        "import pytest; print('runner stack: pytest', pytest.__version__)"

# The Go toolchain stays first: three modules shell out to `go`.  The venv comes
# before /usr/bin so that lib/run-pytest.sh's bare `python3` is the interpreter
# holding pytest, which is the only place that name is resolved implicitly.
ENV PATH=/usr/local/go/bin:/opt/go/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# --- The lockfiles and the two module lists ----------------------------------
# State A's go.mod/go.sum, and the same retired/banned lists the environment
# image used, digested here against the values recorded at construction time.
#
# The digests are the point.  The two mirrors are built by two different scripts
# in two different build contexts; if their inputs drift, the symptom is a
# submission that resolved fine for the agent and fails to resolve here, naming a
# module the agent never touched.  Failing this build makes that a construction
# error, which is where it belongs.
#
# One copy of each list, in data/lockfiles/, read both by the mirror builder and
# by modules/build/run.py.  There were briefly two copies in this context, and two
# copies of a list is how a mirror gets stocked against one list while the tree is
# judged against the other.
ENV SRB_GOMOD_SHA256=351b1f3a2655d666907972682e4bfc0e844cfe0d2bfd3b18f77d9f701f74b712 \
    SRB_GOSUM_SHA256=1b578b5ce75f9716fddeca4db53d5e76bdad7b2331312c57521fee3d5e1df933 \
    SRB_RETIRED_SHA256=0804f4f27db2b32ea768d37ed840e3061e851bf73e80340672d9cea4ae54373c \
    SRB_BANNED_SHA256=bc2bb655a9791420d19ab9bec84032d570e8933884ec2f36df261545f0bfae86

COPY data/lockfiles/go.mod               /opt/srb/lockfiles/go.mod
COPY data/lockfiles/go.sum               /opt/srb/lockfiles/go.sum
COPY data/lockfiles/retired-modules.txt  /opt/srb/lockfiles/retired-modules.txt
COPY data/lockfiles/banned-routers.txt   /opt/srb/lockfiles/banned-routers.txt

RUN set -eu; \
    echo "$SRB_GOMOD_SHA256  /opt/srb/lockfiles/go.mod"              | sha256sum -c -; \
    echo "$SRB_GOSUM_SHA256  /opt/srb/lockfiles/go.sum"              | sha256sum -c -; \
    echo "$SRB_RETIRED_SHA256  /opt/srb/lockfiles/retired-modules.txt" | sha256sum -c -; \
    echo "$SRB_BANNED_SHA256  /opt/srb/lockfiles/banned-routers.txt"   | sha256sum -c -; \
    echo "lockfiles and both lists match the environment image's inputs"

# --- The two offline module mirrors -------------------------------------------
# Rebuilt from those lockfiles, offline-usable, and none at all for the 27
# ALTERNATIVE routers in either.  See lib/build-goproxy.sh for why this is a
# different script from the environment's: that one warms from the source tree and
# can run `go mod tidy`, and a stage context has no source tree to read.
#
# /opt/goproxy is COMPLETE and is what every module here builds against.  It
# carries the retired router's archive because State A imports it and State A is
# the oracle this stage replays -- see the role comment at the top of the builder.
# /opt/goproxy-pruned reproduces the agent's mirror and is built against by
# nothing; the build module reads it once, for an unscored observation.
COPY lib/build-goproxy.sh /opt/srb/scripts/build-goproxy.sh
RUN chmod +x /opt/srb/scripts/build-goproxy.sh \
 && SRB_RETIRED_MODULES=/opt/srb/lockfiles/retired-modules.txt \
    SRB_BANNED_ROUTERS=/opt/srb/lockfiles/banned-routers.txt \
    /opt/srb/scripts/build-goproxy.sh /opt/srb/lockfiles \
        /opt/goproxy /opt/goproxy-pruned

ENV SRB_GOPROXY_ROOT=/opt/goproxy \
    SRB_GOPROXY_PRUNED=/opt/goproxy-pruned \
    GOPROXY=off \
    GONOSUMDB= \
    GOPRIVATE=

# Each mirror against the lockfile it was built from, and against its own role.
# go.mod names the versions minimal version selection will actually choose, so its
# requires are a demand: every one present, with a .zip -- and for the retired
# module, `--retired-role` decides whether that .zip is required or forbidden.
# go.sum is wider and advisory: it accumulates superseded versions MVS will never
# select, and their absence cannot fail a correct submission.
#
# Both roles are re-checked here rather than trusted from the builder, which is
# what copies and deletes: a checker that trusts the deleter checks nothing.  And
# the two assertions are opposites, so running the same check twice with one flag
# changed is what makes a mirror that got the wrong role a build failure.
COPY lib/check-mirror.py /opt/srb/scripts/check-mirror.py
RUN /opt/venv/bin/python /opt/srb/scripts/check-mirror.py \
        --mirror   /opt/goproxy \
        --gomod    /opt/srb/lockfiles/go.mod \
        --gosum    /opt/srb/lockfiles/go.sum \
        --retired  /opt/srb/lockfiles/retired-modules.txt \
        --retired-role complete \
        --banned   /opt/srb/lockfiles/banned-routers.txt \
 && /opt/venv/bin/python /opt/srb/scripts/check-mirror.py \
        --mirror   /opt/goproxy-pruned \
        --gomod    /opt/srb/lockfiles/go.mod \
        --gosum    /opt/srb/lockfiles/go.sum \
        --retired  /opt/srb/lockfiles/retired-modules.txt \
        --retired-role pruned \
        --banned   /opt/srb/lockfiles/banned-routers.txt

# Proven by construction rather than asserted in prose, in both directions, because
# the whole design of this stage turns on which mirror does which.
#
# The complete mirror MUST compile the retired router.  State A imports it from
# pkg/server.go, so a complete mirror that cannot serve it is a stage that cannot
# build the oracle -- and every behavioural expectation in data/golden-statea.json
# was recorded from that oracle.  Feeding State A in as a submission would then
# score zero on a suite that is a transcript of its own answers, which is not a
# measurement of anything.  That inversion is precisely the bug this stage shipped
# with, and it is checked here where it costs a build.
RUN set -eu; \
    d=$(mktemp -d); cd "$d"; \
    printf 'module srb.local/oracleprobe\n\ngo 1.21\n\nrequire github.com/gorilla/mux v1.8.1\n' > go.mod; \
    printf 'package main\n\nimport (\n\t"net/http"\n\n\t"github.com/gorilla/mux"\n)\n\nfunc main() {\n\tr := mux.NewRouter()\n\tr.PathPrefix("/files").Methods(http.MethodGet).HandlerFunc(\n\t\tfunc(w http.ResponseWriter, req *http.Request) { _ = mux.Vars(req) })\n}\n' > main.go; \
    GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy" GOSUMDB=off GONOSUMDB= \
      GOMODCACHE="$d/mod" go build -buildvcs=false -o /dev/null . \
      > /tmp/oracle.log 2>&1 \
      || { echo "FATAL: the retired router does NOT compile from the complete" >&2; \
           echo "       mirror, so State A cannot be built in this stage and the" >&2; \
           echo "       behavioural oracle cannot be reproduced." >&2; \
           sed -n 1,40p /tmp/oracle.log >&2; exit 1; }; \
    echo "the complete mirror compiles State A's router, PathPrefix and Vars included"; \
    cd /; rm -rf "$d" /tmp/oracle.log

# And the pruned mirror must refuse it, because that mirror exists to reproduce
# what the agent was given.  Nothing is built against it here -- the build module
# reads it for one unscored observation -- but if it stopped refusing, that
# observation would report "needs no retired archive" for every tree including one
# that still imports the router.
RUN set -eu; \
    d=$(mktemp -d); cd "$d"; \
    printf 'module srb.local/banprobe\n\ngo 1.21\n\nrequire github.com/gorilla/mux v1.8.1\n' > go.mod; \
    printf 'package main\n\nimport "github.com/gorilla/mux"\n\nfunc main() { _ = mux.NewRouter() }\n' > main.go; \
    if GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy-pruned" GOSUMDB=off GONOSUMDB= \
       GOMODCACHE="$d/mod" go build -buildvcs=false ./... > /tmp/ban.log 2>&1; then \
        echo "FATAL: gorilla/mux is buildable from the PRUNED mirror, which is" >&2; \
        echo "       supposed to reproduce the agent's own." >&2; \
        sed -n 1,40p /tmp/ban.log >&2; exit 1; \
    fi; \
    grep -q 'mux' /tmp/ban.log \
      && echo "the pruned mirror refuses gorilla/mux, as the agent's does:" \
      && sed -n 1,3p /tmp/ban.log; \
    cd /; rm -rf "$d" /tmp/ban.log

# And the converse, which matters just as much: the mirror must still be able to
# resolve and compile the dependencies State A keeps.  A mirror that banned
# everything would pass every check above and fail every submission.
RUN set -eu; \
    d=$(mktemp -d); cd "$d"; \
    cp /opt/srb/lockfiles/go.mod /opt/srb/lockfiles/go.sum .; \
    printf 'package main\n\nimport (\n\t"fmt"\n\n\t"github.com/google/uuid"\n\t"github.com/spf13/afero"\n\t"dario.cat/mergo"\n)\n\nfunc main() {\n\t_ = afero.NewMemMapFs()\n\t_ = mergo.Merge\n\tfmt.Println(uuid.New().String() != "")\n}\n' > main.go; \
    GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy" GOSUMDB=off \
      GOMODCACHE="$d/mod" go build -buildvcs=false -o /dev/null . \
      || { echo "FATAL: the kept dependencies do not resolve from the mirror" >&2; exit 1; }; \
    echo "the mirror resolves uuid, afero and mergo offline"; \
    cd /; rm -rf "$d"

# The graph closure, asserted as a property instead of enumerated as a list.
#
# `go list -m all` reaches golang.org/x/mod, x/sync and x/tools, none of which
# appear in go.mod -- they arrive through the test requirements of modules that
# do.  So "every version go.mod names is present", which is what check-mirror
# derives, is NOT sufficient for the toolchain to compute the graph.  A mirror
# could satisfy check-mirror and still fail `go mod tidy` -- which the own_tests
# module's `go test ./...` needs, and which the build module records the graph
# from -- for every submission.
#
# Rather than extend the derivation and guess at the next such module, ask the
# toolchain: if `go mod graph` and `go list -m all` succeed offline here, the
# closure is stocked, whatever it happens to contain.
RUN set -eu; \
    d=$(mktemp -d); cd "$d"; \
    cp /opt/srb/lockfiles/go.mod /opt/srb/lockfiles/go.sum .; \
    printf 'package main\n\nfunc main() {}\n' > main.go; \
    export GOFLAGS=-mod=mod GOPROXY="file:///opt/goproxy" GOSUMDB=off \
           GOMODCACHE="$d/mod"; \
    go mod graph > graph.txt 2>err.txt \
      || { echo "FATAL: go mod graph cannot run offline against this mirror" >&2; \
           sed -n 1,15p err.txt >&2; exit 1; }; \
    go list -m all > list.txt 2>err.txt \
      || { echo "FATAL: go list -m all cannot run offline against this mirror" >&2; \
           sed -n 1,15p err.txt >&2; exit 1; }; \
    echo "the graph closure resolves offline: $(wc -l < graph.txt) edge(s), \
$(wc -l < list.txt) module(s)"; \
    cd /; rm -rf "$d"

# The retirement, from the point of view of a submission mid-migration: go.mod
# still requires the retired router and the source no longer imports it.  That is
# the state a correct port is in when it runs `go mod tidy`, and keeping the .mod
# after deleting the .zip is what lets that command work.
#
# Run against BOTH mirrors, and the pruned one is the load-bearing half: it is the
# agent's own mirror, so if tidy fails there, every correct submission's first
# command fails while the work is being done, and the error names a module the
# agent was just told to remove.  Asserted here so that is a build error rather
# than a task-wide false negative nobody sees until a submission arrives.
RUN set -eu; \
    for m in /opt/goproxy /opt/goproxy-pruned; do \
      d=$(mktemp -d); cd "$d"; \
      cp /opt/srb/lockfiles/go.mod /opt/srb/lockfiles/go.sum .; \
      printf 'package main\n\nimport (\n\t"net/http"\n\n\t"github.com/spf13/afero"\n)\n\nfunc main() {\n\t_ = afero.NewMemMapFs()\n\tmux := http.NewServeMux()\n\tmux.HandleFunc("GET /files/", func(http.ResponseWriter, *http.Request) {})\n\t_ = http.ListenAndServe(":0", mux)\n}\n' > main.go; \
      export GOFLAGS=-mod=mod GOPROXY="file://$m" GOSUMDB=off \
             GOMODCACHE="$d/mod"; \
      go mod tidy > tidy.log 2>&1 \
        || { echo "FATAL: go mod tidy fails offline against $m for a source tree" >&2; \
             echo "       that has already dropped the retired import -- which is" >&2; \
             echo "       every correct submission" >&2; \
             sed -n 1,20p tidy.log >&2; exit 1; }; \
      if grep -q 'gorilla/mux' go.mod; then \
          echo "FATAL: tidy left gorilla/mux in go.mod against $m for a tree that" >&2; \
          echo "       does not import it" >&2; \
          cat go.mod >&2; exit 1; \
      fi; \
      go build -buildvcs=false -o /dev/null . \
        || { echo "FATAL: the tidied module does not build offline against $m" >&2; exit 1; }; \
      echo "$m: a tree that dropped the retired import tidies, loses the require, and builds"; \
      cd /; rm -rf "$d"; \
    done; \
    echo "the retirement works as designed in both mirror roles"

# --- The frozen fixture docroot -----------------------------------------------
# The same nine committed files the environment image publishes, asserted against
# the same aggregate.  Both are load-bearing, and for a sharper reason than
# "keep the images alike": the recording holds an unmasked Last-Modified and one
# unmasked Content-Type per file, so a docroot differing by one byte or one mtime
# would fail a correct submission on every GET of that file.
#
# The aggregate is computed over NUL-delimited "sha256  path" lines under LC_ALL=C
# so that `sp ace.txt` cannot split a field.  Computing it with a bare
# `xargs sha256sum` yields a stable but WRONG value in both images -- the failure
# mode that never announces itself.
#
# SRB_FIXTURE_MTIME is declared, not merely defaulted.  harness/replay.py reads it
# and re-stamps a fresh copy of this tree before every launch, so the image's own
# COPY mtimes never reach the server; what does reach it is this string.  The
# corpus sends `If-Modified-Since: Thu, 01 Jan 2026 00:00:00 GMT` and expects a
# 304, so a stage that let this default drift from the environment's value would
# turn `ims-equal` and `ims-head` into 200s for everyone.
ENV SRB_FIXTURE_ROOT=/opt/fixtures/docroot \
    SRB_FIXTURE_AGGREGATE=bfa364db081206cba49def81caf967e5b80785bcdcfca4c4565017c5d6b51c2f \
    SRB_FIXTURE_MTIME="2026-01-01 00:00:00 UTC"

COPY data/fixtures/docroot /opt/fixtures/docroot

RUN set -eu; \
    cd /opt/fixtures/docroot; \
    got=$(find . -type f -print0 | LC_ALL=C sort -z \
            | xargs -0 sha256sum | sha256sum | cut -d' ' -f1); \
    n=$(find . -type f | wc -l); \
    echo "fixture docroot: $n files, aggregate $got"; \
    [ "$n" = "9" ] || { echo "FAIL: expected 9 fixture files, found $n" >&2; exit 1; }; \
    [ "$got" = "$SRB_FIXTURE_AGGREGATE" ] || { \
        echo "FAIL: fixture aggregate drifted from the environment image's" >&2; \
        echo "  expected $SRB_FIXTURE_AGGREGATE" >&2; \
        echo "  actual   $got" >&2; exit 1; }; \
    test -f 'sp ace.txt'; \
    test -f 'odd+name.txt'; \
    test -f README; \
    test -f sub/b.txt; \
    test "$(stat -c%s big.bin)" = "4096"; \
    echo "fixture docroot verified"

# The stamp itself, end to end: seed a docroot the way every launch will and
# check that the mtime the server would report is the one the corpus asks about.
# Asserting the env var's value would only prove the string was typed correctly;
# this proves `touch -d` accepts it and lands on the second the recording holds.
RUN set -eu; \
    d=$(mktemp -d); \
    cp -r /opt/fixtures/docroot "$d/docroot"; \
    find "$d/docroot" -depth -exec touch -h -d "$SRB_FIXTURE_MTIME" {} +; \
    got=$(TZ=GMT stat -c '%y' "$d/docroot/a.txt" | cut -d. -f1); \
    [ "$got" = "2026-01-01 00:00:00" ] || { \
        echo "FAIL: stamped mtime is '$got', the corpus asks about 2026-01-01 00:00:00 GMT" >&2; \
        exit 1; }; \
    echo "a seeded docroot stamps to $got GMT, which is what ims-equal expects"; \
    rm -rf "$d"

# --- The harness -------------------------------------------------------------
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The suite ---------------------------------------------------------------
# Copied last so that iterating on a module does not rebuild the mirror.
COPY . /tests/behavioural

RUN set -eu; \
    chmod +x /tests/behavioural/lib/run-pytest.sh /tests/behavioural/modules/*/run.sh; \
    find /tests/behavioural -name __pycache__ -type d -prune -exec rm -rf {} +; \
    test -f /tests/behavioural/data/golden-statea.json; \
    test -f /tests/behavioural/data/lockfiles/retired-modules.txt; \
    test -f /tests/behavioural/data/lockfiles/banned-routers.txt; \
    echo "suite installed"

# Prove the suite is internally consistent before any submission arrives.
#
# The fingerprint comparison is the one that matters.  A recording cannot notice
# being asked about different requests than the ones it recorded: edit a target
# and every comparison still runs, still passes against the stored response, and
# silently stops grading the behaviour the case exists for.  `encoded-slash` is
# the sharpest instance -- `/files/sub%2Fb.txt` answers 200 under the retired
# router and 404 under a naive port, and rewriting it to `/files/sub/b.txt` makes
# the case pass for everyone.
#
# Ids are checked in both directions rather than by count, because two edits that
# cancel out keep the count identical.
RUN /opt/venv/bin/python - <<'PY'
import json
import sys
sys.path.insert(0, "/tests/behavioural/lib")
from harness import corpus

doc = json.load(open("/tests/behavioural/data/golden-statea.json"))
cases = corpus.all_cases()
ids = {c["id"] for c in cases}
records = doc["records"]

print(f"corpus    : {len(cases)} cases over {len(corpus.PROFILES)} profiles, "
      f"fingerprint {corpus.fingerprint()}")
print(f"recording : {len(records)} records, captured against "
      f"{doc.get('corpus_fingerprint', '(unstamped)')}")

problems = []
if doc.get("corpus_fingerprint") != corpus.fingerprint():
    problems.append(f"the recording answers corpus {doc.get('corpus_fingerprint')}, "
                    f"this corpus is {corpus.fingerprint()}")
missing = sorted(ids - set(records))
if missing:
    problems.append(f"{len(missing)} case(s) have no record: {missing[:6]}")
orphan = sorted(set(records) - ids)
if orphan:
    problems.append(f"{len(orphan)} record(s) match no case: {orphan[:6]}")
if problems:
    sys.exit("FATAL: the suite and its recording disagree:\n  " + "\n  ".join(problems))
print(f"the recording answers exactly this corpus on all {len(records)} cases")
PY

# The fixture aggregate the recording was captured against, against the tree this
# image actually holds.  Separate from the check above because it fails for a
# different reason: same questions, different files behind them.  The recording
# holds unmasked Last-Modified and Content-Type values, so a docroot that differed
# by a byte would fail a correct submission on every GET.
RUN set -eu; \
    stamped=$(/opt/venv/bin/python -c \
        "import json;print(json.load(open('/tests/behavioural/data/golden-statea.json')).get('fixtures_aggregate',''))"); \
    [ -n "$stamped" ] || { echo "FATAL: the recording records no fixture aggregate" >&2; exit 1; }; \
    [ "$stamped" = "$SRB_FIXTURE_AGGREGATE" ] || { \
        echo "FATAL: the recording was captured against fixtures $stamped," >&2; \
        echo "       this image holds $SRB_FIXTURE_AGGREGATE" >&2; exit 1; }; \
    echo "the recording was captured against the fixtures in this image: $stamped"

# The manifest is the thing most likely to be edited and least likely to be
# tested, so it is checked at build time: a malformed weight, a duplicate id or a
# module directory that does not exist fails the build, not the run.  A missing
# entry point is checked the same way -- a module whose declared command is not
# there scores zero at grading time and looks exactly like a submission that
# broke it.
RUN /opt/venv/bin/python - <<'PY'
import sys
from swerefactor import config

s = config.Suite.load("/tests/behavioural/suite.toml")
problems = []
declared = set()
for m in s.modules:
    d = s.root / (m.dir or f"modules/{m.id}")
    declared.add(d.resolve())
    if not d.is_dir():
        problems.append(f"{m.id}: no directory at {d}")
        continue
    entry = m.command[-1] if m.command else "run.sh"
    if not (d / entry).exists():
        problems.append(f"{m.id}: declares {m.command} but {entry} is not in {d}")

# And the reverse direction.  A module dropped from suite.toml but left on disk is
# dead code that still collects under pytest, so it inflates the collected-check
# count below and reads as a live module to anyone browsing the tree.
for d in sorted((s.root / "modules").iterdir()):
    if d.is_dir() and d.name != "__pycache__" and d.resolve() not in declared:
        problems.append(f"{d.name}: a module directory suite.toml does not declare")
# The first module builds the tree every later module measures, so what this
# asserts is that the build module leads and carries weight.  No module's failure
# cancels another's result, and none needs a flag to zero the stage: it pays only
# for a submission that passed every scored check in every weighted module.
if s.modules[0].id != "build":
    problems.append(f"the first module is {s.modules[0].id}, not build; nothing "
                    f"else can be measured without a build")
elif s.modules[0].weight <= 0:
    problems.append("the build module carries no weight; it is what every later "
                    "module depends on and must count for something")
if abs(s.weight_total - 1.0) > 1e-9:
    problems.append(f"weights total {s.weight_total!r}, not 1.0 -- the runner "
                    f"normalises them, so this is a sign one was edited without "
                    f"adjusting the rest")
if problems:
    sys.exit("FATAL: suite.toml does not match the tree:\n  " + "\n  ".join(problems))
print(f"suite: {len(s.modules)} modules, weight total {s.weight_total:g}, "
      f"first is {s.modules[0].id} (weight {s.modules[0].weight:g})")
PY

# Collect every pytest module without running one.  A collection error at grading
# time reads as a zero for that module, so it is found here instead.
#
# The count is asserted, not printed.  Piping pytest into `tail` hands the RUN the
# tail's exit status, so a collection error would scroll past and the image would
# ship anyway; and a module that silently collects a fraction of its tests still
# scores 1.0 on the fraction it kept, which is the failure mode a printed number
# does nothing about.  This exact number caught a real bug: six modules had a
# parametrised `case_id` missing from their signatures and collected zero.
COPY lib/count-collected.py /opt/srb/scripts/count-collected.py
RUN set -eu; cd /tests/behavioural; \
    export PYTHONPATH=/tests/behavioural/lib:/opt/swerefactor; \
    out=$(/opt/venv/bin/python -m pytest -c pytest.ini -p srbfixtures \
              --collect-only -q modules 2>&1) || { \
        printf '%s\n' "$out" | tail -20 >&2; \
        echo "FATAL: collection failed" >&2; exit 1; }; \
    got=$(printf '%s\n' "$out" \
            | /opt/venv/bin/python /opt/srb/scripts/count-collected.py); \
    [ "$got" = "478" ] || { \
        printf '%s\n' "$out" | tail -12 >&2; \
        echo "FATAL: collected ${got:-no} checks, expected 478" >&2; exit 1; }; \
    echo "the seven pytest modules collect $got checks"

ENV SRB_REPO=/workspace/repo \
    SRB_ORIGINAL=/opt/original \
    SRB_RESULTS=/logs/verifier

WORKDIR /tests/behavioural
