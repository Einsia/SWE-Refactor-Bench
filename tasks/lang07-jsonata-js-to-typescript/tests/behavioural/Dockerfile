# =============================================================================
# SWERefactorBench -- lang07-jsonata-js-to-typescript
# Stage 2 image: the behavioural suite.
#
#     docker build -t swerefactor/lang07-behavioural:1 \
#         tasks/lang07-jsonata-js-to-typescript/tests/behavioural
#
# Fifteen modules run here, each in its own process, and every one of them
# compares the submission against answers frozen into this image before any
# submission existed.
#
#     stage `fetch`      two pinned artifacts, each checked by sha256
#     stage `toolchain`  debian + node + tsc, and nothing else
#     stage `baseline`   State A unpacked, its shape asserted
#     stage `oracle`     State A built, 13,940 cases put to it, answers frozen
#     final              node + tsc + the frozen answers, and no reference tree
#
# ---------------------------------------------------------------------------
# The constraint that shapes this file
# ---------------------------------------------------------------------------
# Every other language task can withhold the old toolchain from the grading
# image: `tommy`, a C#-to-TypeScript pair, asserts `dotnet` is off PATH, and a
# submission that tried to shell out to C# finds nothing to shell out to.  This
# task cannot do that.
# JavaScript is what the repository is being ported *from* and TypeScript
# compiles *to* it, so `node` is both the old runtime and the new one.  Removing
# it would leave nothing to grade.
#
# So the thing withheld is not the runtime, it is the *reference*:
#
#   * `data/original.tar.gz` is copied into `baseline`, and `baseline` is not the
#     final stage's parent.  Of the stages, only `/opt/assets` crosses over.
#
#     That is *not* on its own enough, and this bullet said it was for most of
#     this file's life.  The final stage also does `COPY . /tests/behavioural`, and
#     `data/original.tar.gz` is in that context -- so State A arrived in the
#     grading image beside `lib/` and `suite.toml`, by a route that has nothing to
#     do with which stage is the parent.  It is deleted now, in the `rm -rf` under
#     "data/ is removed" below, and the deletion is asserted rather than assumed.
#     `docker save | tar x` does recover it from that layer's parent, which is the
#     honest form of this claim: absent from the image that runs, present in its
#     history, and the history is not something a submission can read.
#   * the `oracle` stage builds State A, captures its answers, and is thrown
#     away.  What ships is 13,940 questions and 13,940 answers.
#   * the final stage proves the absence three independent ways, none of which
#     is a list of filenames.  An *allowlist*: no `.js`, `.cjs`, `.mjs`, `.ts`,
#     `.mts` or `.cts` exists anywhere outside `/usr/local/lib/nodejs` and
#     `/opt/assets/consumers` -- a description of what this image is, which a name
#     sweep is not.  A *content* sweep: no file anywhere carries twenty or more of
#     jsonata's 101 `[TDS]NNNN` diagnostic codes, which is what an implementation
#     of it looks like whatever it is called.  And an *archive* sweep, which is
#     the one the first two were missing: a gzipped tarball has no matching name
#     and no greppable byte, which is exactly how the copy described above sat
#     here while six checks reported it was not.
#     A copy left anywhere would be a working JavaScript implementation sitting
#     in the same image as a suite asking whether a TypeScript one behaves
#     identically -- and the submission's build runs in this image, with
#     `require()` available and the whole filesystem readable.
#
# The one thing this image cannot prove by absence is that a submission's build
# does not fetch jsonata from a registry.  That is why `npm_config_offline` is
# set for every build (`vlib.base_env`) and why the grading run has no network:
# absence of a reference here, absence of a route to one there.
#
# Network: none at grading time.  Both fetches happen in the first stage, at
# build time, from a URL with a digest beside it.
# =============================================================================

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z

# The shared grader, out of the donor image, exactly as the other two stages of
# this task and the behavioural stage of every other task do it.  The sixteen
# `run.sh` links are driven by `swerefactor.behavioural`, so without this the stage
# could not start -- and a host-side check with `PYTHONPATH=infra` would not
# notice, because the host's copy answers the import.
ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# --------------------------------------------------------------------------- #
# Stage `fetch` -- the pinned artifacts
# --------------------------------------------------------------------------- #
# A pinned version is not a pin.  npm allows a version to be republished and
# nodejs.org serves through caches that have been wrong before; the digest is the
# pin and the version in the URL is a comment on it.  `--fail` so an HTML error
# page cannot be mistaken for a tarball, and each `sha256sum -c` runs beside its
# own download rather than in one block at the end.
FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS fetch
ARG DEBIAN_FRONTEND
ARG DEBIAN_SNAPSHOT
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

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
    apt-get -o Acquire::Retries=8 update; \
    apt-get install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      curl=7.88.1-10+deb12u14 \
    ; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /dl

# node 24 is the current LTS line.  The contract requires the submission's
# tsconfig to declare `"types": []`, so the port compiles without @types/node and
# nothing here depends on which minor it is beyond the digest.
#
# tsc is the whole toolchain the port needs.  There is no third artifact: the
# repository has no runtime dependencies and the contract keeps it that way, so
# `npm install` has nothing to resolve and `npm_config_offline` costs nothing.
RUN set -eux; \
    curl -fsSL --retry 5 --retry-connrefused -o node.tar.xz \
      https://nodejs.org/dist/v24.18.1/node-v24.18.1-linux-x64.tar.xz; \
    echo 'd6c664df3f3f61458e8c277585571328522d705166723a7c7823a9253a4d15a0  node.tar.xz' \
      | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-connrefused -o typescript.tgz \
      https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz; \
    echo '10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3  typescript.tgz' \
      | sha256sum -c -

# --------------------------------------------------------------------------- #
# Stage `toolchain` -- what the oracle and the grading image share
# --------------------------------------------------------------------------- #
# Short list, each entry earning its place: python3 runs this suite; xz-utils
# unpacks node; tzdata and C.UTF-8 settle two things the corpus depends on --
# `$fromMillis` formats through the process timezone, and a probe exchanging
# NDJSON byte for byte must not have its stdio recoded.
#
# Both halves of that are graded, so neither is left to the host.  `TZ=UTC` is
# not a convenience: 82 datetime cases were frozen under it, and an image
# inheriting the daemon's zone would fail all of them for every submission.
FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS toolchain
ARG DEBIAN_FRONTEND
ARG DEBIAN_SNAPSHOT
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC \
    NODE_OPTIONS=--max-old-space-size=2048 \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    npm_config_audit=false

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
    apt-get -o Acquire::Retries=8 update; \
    apt-get install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      python3=3.11.2-1+b1 \
      tzdata=2025b-0+deb12u2 \
      xz-utils=5.4.1-1 \
    ; \
    rm -rf /var/lib/apt/lists/*

# Node, and tsc as a global.  Global is the contract, not a shortcut: `build`
# deletes `node_modules` from its copy before running `npm run build`, so a
# submission whose build resolved `tsc` from a local install would fail on a tree
# with nothing wrong with it.  The contract requires both dependency sets empty
# and a `build` script that works against a global tsc, which finds this one.
COPY --from=fetch /dl/node.tar.xz /dl/typescript.tgz /tmp/
RUN set -eux; \
    install -d /usr/local/lib/nodejs; \
    tar -xJf /tmp/node.tar.xz -C /usr/local/lib/nodejs --strip-components=1; \
    ln -s /usr/local/lib/nodejs/bin/node /usr/local/bin/node; \
    ln -s /usr/local/lib/nodejs/bin/npm /usr/local/bin/npm; \
    ln -s /usr/local/lib/nodejs/bin/npx /usr/local/bin/npx; \
    npm install -g --no-audit --no-fund /tmp/typescript.tgz; \
    ln -sf /usr/local/lib/nodejs/bin/tsc /usr/local/bin/tsc; \
    npm cache clean --force; \
    rm -rf /root/.npm; \
    rm -f /tmp/node.tar.xz /tmp/typescript.tgz

# The three versions are load-bearing enough to assert rather than print.  `tsc
# --version` succeeding says tsc runs; it does not say it is the pin, and a
# toolchain that drifted by a major would change what `strict` means and what the
# emitted JavaScript looks like -- both of which this suite grades.
RUN set -eux; \
    test "$(node --version)" = "v24.18.1" \
      || { echo "node is $(node --version), expected v24.18.1" >&2; exit 1; }; \
    test "$(tsc --version)" = "Version 5.9.3" \
      || { echo "tsc is $(tsc --version), expected Version 5.9.3" >&2; exit 1; }; \
    python3 -c 'import sys; assert sys.version_info[:2] == (3, 11), sys.version'; \
    echo "toolchain pinned: node 24.18.1, tsc 5.9.3, python 3.11"

# --------------------------------------------------------------------------- #
# Stage `baseline` -- State A, unpacked and checked
# --------------------------------------------------------------------------- #
# `data/original.tar.gz` is State A: the tree the submission starts from, digest
# checked here against the same `original.sha256` that `environment/` ships, which
# `swerefactor validate` holds byte-identical to this copy.
#
# The assertions are about what the *oracle* needs, not about what a good tree
# looks like. `src/probe.js` and `tools/build.js` are the two files the freeze
# invokes; `dist/` must be absent because its absence is what makes the freeze
# also a test that the shipped tree builds. The file count is asserted because a
# tarball that unpacked partially -- a truncated transfer that still had a valid
# gzip trailer -- would otherwise present as a reference that answers most cases.
FROM toolchain AS baseline
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY data/original.tar.gz data/original.sha256 /opt/baseline/
RUN set -eux; \
    cd /opt/baseline; \
    sha256sum -c original.sha256; \
    install -d /opt/original; \
    tar -xzf original.tar.gz -C /opt/original --strip-components=1; \
    for f in package.json tools/build.js src/probe.js src/jsonata.js \
             src/probe-wire.js jsonata.d.ts; do \
      test -f "/opt/original/$f" \
        || { echo "State A has no $f; the reference cannot be built" >&2; exit 1; }; \
    done; \
    test ! -e /opt/original/dist \
      || { echo "State A ships a dist/; the freeze must build it, so its presence \
means the tarball was cut from a built tree and the build is untested" >&2; exit 1; }; \
    test ! -e /opt/original/node_modules \
      || { echo "State A ships node_modules; the repository has no dependencies" >&2; exit 1; }; \
    count="$(find /opt/original -type f | wc -l)"; \
    test "$count" = 15 \
      || { echo "State A has $count files, expected 15" >&2; exit 1; }; \
    rm -f original.tar.gz original.sha256; \
    echo "State A unpacked: $count files"

# --------------------------------------------------------------------------- #
# Stage `oracle` -- build the reference, generate the cases, freeze the answers
# --------------------------------------------------------------------------- #
# This stage exists so that its output can be a directory of JSON and nothing
# else. `freeze.py` runs State A's own `npm run build`, generates all 13,940
# cases, puts them to the reference in four batches, and writes
# `/opt/assets/corpus/*.jsonl`. It also runs `catalog.check_catalog`, so a
# generator change that quietly stopped producing a family fails the build here
# rather than shrinking a module's denominator at grading time.
FROM baseline AS oracle
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY lib /opt/freeze/lib
COPY data /opt/freeze/data
COPY suite.toml /opt/freeze/suite.toml

# Nothing is copied into the reference tree. The reference *is* State A, built in
# place by the script it ships with, so there is no second copy of `jsonata.js` to
# keep in step with the first -- and a drift between two copies would present as
# every case disagreeing, which reads like a bad port.
RUN set -eux; \
    cd /opt/freeze; \
    python3 lib/freeze.py \
      --source /opt/original \
      --assets /opt/assets \
      --work /opt/freeze/work \
      --data /opt/freeze/data \
      --suite /opt/freeze/suite.toml

# The consumers travel with the corpus. Both are read at grade time through
# `SWEREFACTOR_ASSETS`, and assembling them in one directory here means the final
# stage copies one path and `check-assets.py` below checks exactly what ships --
# rather than checking the build context and hoping the COPY that followed took
# the same files.
RUN set -eux; \
    cp -a /opt/freeze/data/consumers /opt/assets/consumers; \
    python3 /opt/freeze/lib/check-assets.py /opt/assets

# State A, graded as if it were a submission, through the same executor and the
# same byte comparison the grading modules use. It must answer every frozen case
# exactly, in both probe invocations, and satisfy all 24 type rows.
#
# This runs here rather than at grading time because it needs the reference, and
# it runs after the freeze because it is the check that the freeze produced a
# *passable* corpus: a rate below 1.0 is a bug in this suite, never a fact about
# anyone's port, so it fails the build.
#
# It has earned its place three times, and each time on a case that was unpassable
# by construction and invisible to code review:
#
#   * a `$shuffle` case whose answer depended on a permutation no implementation can
#     be required to reproduce.
#   * a recovered diagnostic that published a V8 stack trace -- absolute paths from
#     this build machine, the reference's own line numbers, and an `async` frame
#     label that differed between two readings of the same case.
#   * `$toMillis('7 pm', '[h] [P]')`, whose frozen answer was the date this image was
#     built. The picture names no year, so `parseDateTime` defaults the date from
#     `environment.timestamp` -- and `clock: "pinned"` shadows the `$now` binding,
#     not that field. It passed on one day in history: the day it was frozen.
#
# The third one is why this run now also answers the whole corpus with the probe's
# wall clock moved 400 days, and requires every answer byte-identical. Freeze and
# identity both run here on the same clock, so an expectation that captured *today's
# date* agreed with itself and failed every day after. Nothing that reads this corpus
# could have found that; only answering it twice on two different days can, and moving
# the clock is how one build does that.
RUN set -eux; \
    cd /opt/freeze; \
    python3 lib/identity.py \
      --source /opt/original \
      --corpus /opt/assets/corpus \
      --work /opt/freeze/identity \
      --consumers /opt/assets/consumers

# --------------------------------------------------------------------------- #
# Final stage -- the grading image
# --------------------------------------------------------------------------- #
# node, tsc, python3, this suite, and the frozen answers. Only `/opt/assets` is
# copied out of the oracle stage -- but `COPY .` below brings the build context,
# `data/original.tar.gz` is in it, and that is State A. The RUN under "data/ is
# removed" deletes it and the archive sweep at the bottom proves no copy is left.
FROM toolchain
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY --from=oracle /opt/assets /opt/assets
COPY --from=infra /opt/swerefactor /opt/swerefactor
COPY . /tests/behavioural

# `PYTHONPATH` is set here rather than left to the caller. The grader is imported
# from three places with three different entry points -- the stage runner, the
# sixteen `run.sh` links, and the RUN below that proves it is importable -- and a
# caller that has to remember an environment variable is one that gets it right
# twice and wrong once.
ENV SWEREFACTOR_ASSETS=/opt/assets \
    SRB_SUITE_DIR=/tests/behavioural \
    PYTHONPATH=/opt/swerefactor

# `data/` is removed, and this is the load-bearing RUN in the final stage rather
# than the tidying step it looks like.
#
# It carries State A. `COPY . /tests/behavioural` above copies the whole build
# context, and `data/original.tar.gz` is in that context -- the same 84 KB tarball
# `baseline` unpacks. Reasoning about the `COPY --from=` lines answers a different
# question: the reference does not arrive from a stage, it arrives from the context
# alongside `lib/` and `suite.toml`.
#
# None of the five sweeps below can see it. Four look for `.js`/`.ts` by name; the
# fifth greps those same extensions for jsonata's error codes. A gzipped tarball has
# neither the extension nor a greppable byte in it, which is why the archive sweep
# further down exists and why this `rm -rf` is not optional.
#
# Why it matters in grading and not just on paper: a submission is *built and run
# in this image*. The port itself is fixed before grading, so reading the reference
# here cannot help it be written -- but it can be delegated to. A `dist/probe.js`
# that unpacked this tarball and forwarded every request to the JavaScript would
# answer all 13,940 cases byte-for-byte and score 40/40 as no port at all.
#
# Nothing needs `data/` at grade time. The corpus is in `/opt/assets`, and
# `check-assets.py` requires the upstream fixture tarball only when there is no
# frozen corpus -- there is one, in the image, which is why the check below still
# passes with the directory gone. The consumers move out for a second reason: two
# copies of 25 files that the driver picks between by environment variable is a way
# for a fix to land in the one nothing reads, and `types-surface.py` falls back to
# the suite-relative path exactly when `SWEREFACTOR_ASSETS` is unset.
RUN set -eux; \
    test -d /opt/assets/consumers \
      || { echo "/opt/assets/consumers is missing; removing the suite copy would \
leave nothing for the type module to compile" >&2; exit 1; }; \
    test -f /tests/behavioural/data/original.tar.gz \
      || { echo "/tests/behavioural/data/original.tar.gz is absent before the \
removal that exists to delete it: either the context stopped carrying State A or \
this path changed, and in the second case the reference is still here under a \
name this RUN no longer names" >&2; exit 1; }; \
    rm -rf /tests/behavioural/data; \
    echo "data/ removed: State A, the upstream fixtures, and the suite's own \
copy of the consumers"

# `/opt/assets` holds exactly two directories and nothing else.
#
# Asserted rather than assumed, and this is the only place the shape is checked.
# A reference left at `/opt/assets/reference` or an `original.tar.gz` beside the
# corpus would be reachable from where the submission's own build runs, and catching
# a wrong shape at build time costs one RUN. Three entries: the two directories, and
# `inventory.json` -- the counts and weights `freeze.py` records beside them. That one
# is content-free by construction (case totals, family counts, the weight table, the
# error codes reached) and the report reads it, so it ships. Nothing else may.
RUN set -eux; \
    shape="$(cd /opt/assets && ls -A | sort | tr '\n' ' ')"; \
    test "$shape" = "consumers corpus inventory.json " \
      || { echo "/opt/assets holds '$shape', expected \
'consumers corpus inventory.json '" >&2; exit 1; }; \
    python3 -c "\
import json; \
d = json.load(open('/opt/assets/inventory.json')); \
bad = sorted(k for k in d if 'case' in k and k != 'case_totals'); \
assert not bad, f'inventory.json carries case data: {bad}'; \
print('inventory.json:', ', '.join(sorted(d)))"; \
    echo "/opt/assets: consumers/, corpus/, inventory.json, nothing else"

# Fifteen modules, fifteen symlinks to one script, and a runner that only requires
# `run.sh` to exist. A link that lost its target -- a `git archive` that did not
# follow links, a checkout on a filesystem without them -- produces "no such file"
# per module at grading time and nothing at build time. `suite.toml` is the list;
# the check reads it rather than repeating it, so adding a module cannot leave this
# behind.
RUN python3 /tests/behavioural/lib/check-modules.py /tests/behavioural

# The grader imports, from the image, with no PYTHONPATH on the command line.
# `behavioural.SuiteRunner` is named rather than the bare package, because `import
# swerefactor` succeeds against an empty `__init__.py`.
RUN set -eux; \
    python3 -c "\
from swerefactor import behavioural, result, scoring; \
from swerefactor.config import Suite; \
s = Suite.load('/tests/behavioural/suite.toml'); \
assert behavioural.SuiteRunner and result and scoring, 'grader is incomplete'; \
print(f'grader imports from the image: {len(s.modules)} modules in suite.toml')"

# The catalog agrees with the assets, checked from the grading image rather than
# from the oracle stage. The freeze already ran `check_catalog` -- but against the
# `lib/` *it* copied, and this stage copies `lib/` again from the build context. A
# `catalog.py` differing between the two would mean the weights used for grading
# are not the ones the expectations were checked against, and nothing else in the
# build would notice.
#
# `--expected` is the corpus directory, which expands to all four
# `*-expected.jsonl`: code coverage is the union over the four stems, the way
# `freeze.py` computes it, and passing one stem alone reports codes as missing that
# another stem reaches.
RUN set -eux; \
    cd /tests/behavioural; \
    python3 lib/catalog.py --check \
        --corpus /opt/assets/corpus \
        --expected /opt/assets/corpus \
        --suite suite.toml >/dev/null; \
    python3 lib/check-assets.py /opt/assets; \
    echo "catalog agrees with the frozen assets"

# Every release case in `catalog.STRUCT_CASES` has a handler in `structure.py`, and
# the handler table has no entries the catalog does not declare. A build assertion
# rather than a runtime one because of what the runtime failure looks like:
# `structure.run` records one check per catalog entry, so a case whose handler is
# missing would either raise inside one module -- reading as a submission that
# broke the structural checks -- or, if skipped, quietly shrink the module's
# denominator so the remaining cases scored full marks. Neither is distinguishable
# from a bad port by anyone reading the report.
RUN set -eux; \
    cd /tests/behavioural; \
    python3 lib/structure.py --check; \
    python3 lib/types-surface.py --check; \
    echo "every release case has a handler; every type row has a consumer"

# The reference is absent. Asserted by allowlist, not by name.
#
# This is this task's version of tommy's "no dotnet on PATH", and it has to be
# different in kind. node cannot be removed -- it is the runtime the port targets
# -- so what must not be here is jsonata itself.
#
# A sweep for the reference's seven module basenames cannot do that. Four of
# them -- `parser.js`, `signature.js`, `functions.js`, `utils.js` -- are among
# the most common filenames in the JavaScript ecosystem, and two are present
# in this image already: `postcss-selector-parser/dist/parser.js` and
# `@tufjs/models/dist/signature.js`, inside node's bundled npm. So a name sweep
# cannot separate the reference from a dependency of the package manager. And the
# converse hole is worse: renaming `jsonata.js` to `engine.js` defeats a name
# sweep completely, which makes it an assertion about spelling rather than about
# contents.
#
# What the image actually looks like is a stronger claim than any list of names.
# Every `.js` in the final image lives under `/usr/local/lib/nodejs` -- node,
# npm's bundled tree, and the global `tsc`. This build context contributes zero
# JavaScript (measured: `find` over it finds none), and so does `/opt/swerefactor`
# from the donor image. The only `.ts` outside that tree is the 24 type-surface
# consumers under `/opt/assets/consumers`, which are files that *use* the
# declarations rather than implement anything.
#
# So the rule is an allowlist: outside those two directories, no JavaScript or
# TypeScript exists in this image at all. That holds whatever the reference's
# files are called, catches a bundle as readily as a source tree, and has no
# false positives because it is a description of the image rather than a guess
# about it. `-xdev` keeps it off mounts.
RUN set -eux; \
    found="$(find / -xdev \
               -path /usr/local/lib/nodejs -prune -o \
               -path /opt/assets/consumers -prune -o \
               -path /proc -prune -o \
               -path /sys -prune -o \
               \( -name '*.js' -o -name '*.cjs' -o -name '*.mjs' \
                  -o -name '*.ts' -o -name '*.mts' -o -name '*.cts' \) \
               -type f -print 2>/dev/null || true)"; \
    if [ -n "$found" ]; then \
      echo "JavaScript or TypeScript exists outside the two directories that \
are allowed to hold any:" >&2; \
      echo "$found" >&2; \
      echo "node is both runtimes here, so the reference cannot be kept out by \
withholding a toolchain -- the image has to contain no implementation at all" >&2; \
      exit 1; \
    fi; \
    echo "no .js/.ts outside /usr/local/lib/nodejs and /opt/assets/consumers"

# The same claim once more by content, because the allowlist above is a statement
# about *where* and this one is a statement about *what*. They fail independently:
# a reference copied into `/usr/local/lib/nodejs/lib/node_modules` to hide would
# pass the allowlist and fail this, and a port's own `dist/` landing somewhere
# unexpected would fail the allowlist and pass this.
#
# The marker is the error-code table. jsonata's diagnostics are 101 codes of the
# form T0410, D2014, S0202, and any implementation that answers this corpus has to
# carry essentially all of them -- they are the answers. Measured in the image
# before choosing the threshold: no `.js` or `.ts` file anywhere carries even one,
# so 20 is not a tuned number, it is far above zero and far below the 100 that
# `src/jsonata.js` alone carries. `catalog.py` carries all 101 legitimately, since
# documenting which codes the corpus reaches is its job; it is Python, and this
# sweep looks at the languages an implementation would be written in.
#
# `/opt/assets` is included here rather than excluded. The frozen answers are
# `.jsonl`, so they are not matched by extension, but a reference smuggled in
# under the corpus directory would be -- and the corpus is the one tree in this
# image that this task's own build wrote.
# NUL-delimited throughout. `for f in $(find ...)` would split npm's tree on the
# first filename containing a space, and a sweep that skips files is worse than no
# sweep because it reports success. The counting runs inside `find -exec` rather
# than a `while read` loop for the same class of reason: piping into `while` puts
# the loop in a subshell, so a hit recorded there would not survive the pipeline,
# and the sweep would print nothing and pass.
RUN set -eux; \
    find / -xdev -path /proc -prune -o -path /sys -prune -o \
      \( -name '*.js' -o -name '*.cjs' -o -name '*.mjs' \
         -o -name '*.ts' -o -name '*.mts' -o -name '*.cts' \) \
      -type f -exec bash -c \
        'n="$(grep -ohE "\b[TDS][0-9]{4}\b" "$1" 2>/dev/null | sort -u | wc -l)"; \
         if [ "$n" -ge 20 ]; then echo "$n $1"; fi' _ {} \; \
      2>/dev/null > /tmp/codehits.txt; \
    if [ -s /tmp/codehits.txt ]; then \
      echo "a file carries 20 or more of jsonata's diagnostic codes, which is \
what an implementation of it looks like regardless of its name:" >&2; \
      cat /tmp/codehits.txt >&2; exit 1; \
    fi; \
    rm -f /tmp/codehits.txt; \
    echo "no file carries jsonata's error-code table"

# And once more inside archives, which is the gap the two sweeps above share.
#
# Both of them look at loose files -- one by name, one by grepping those same names
# for the error-code table -- and a gzipped tarball is invisible to each. So is it to
# the three directory checks below, which test for paths rather than contents. A
# complete reference can therefore sit in this image as one `.tar.gz` while every
# other assertion here passes, and the `rm -rf` above is what removes the one this
# build context carries: this sweep is what would find the next one.
#
# It is the only check for that shape at grade time, which is why it is not optional:
# a reference that reached the image unnoticed is a stage that could answer every
# case by forwarding to it.
#
# `lib/check-archives.py` rather than an inline heredoc because the verification stage
# needs the identical sweep for the identical reason, and `check-task.py`'s
# `duplicates` group holds the two copies byte-identical. It detects containers by
# magic bytes: the image has ~1,550 files whose name ends in `.gz` and almost all are
# man pages, so reading names here would mean either noise or a stale exclusion list.
RUN python3 /tests/behavioural/lib/check-archives.py

# And the three build directories are gone by name, which the sweeps above do not
# cover: an empty `/opt/freeze` holds no `.js` and would pass both while still
# meaning a stage leaked.
RUN set -eux; \
    test ! -d /opt/original || { echo "/opt/original survived" >&2; exit 1; }; \
    test ! -d /opt/freeze || { echo "/opt/freeze survived" >&2; exit 1; }; \
    test ! -d /opt/baseline || { echo "/opt/baseline survived" >&2; exit 1; }; \
    echo "no reference implementation anywhere in the grading image"

# And no packaged copy either. A submission's build runs in this image with
# `require()` available and the whole filesystem readable, so a jsonata inside any
# `node_modules` would be reachable -- and `npm_config_offline` stops a fetch, not
# a read of something already here. node's own bundled npm tree is the one large
# directory in the image nobody reads, which is exactly why it is swept rather
# than excluded.
RUN set -eux; \
    found="$(find / -xdev -type d -name 'jsonata' -print 2>/dev/null || true)"; \
    if [ -n "$found" ]; then \
      echo "a packaged jsonata is present in the grading image:" >&2; \
      echo "$found" >&2; exit 1; \
    fi; \
    echo "no packaged jsonata in the image"

# No bytecode. PYTHONDONTWRITEBYTECODE is set in the toolchain stage and
# `.dockerignore` excludes `**/__pycache__` from the context, but the RUNs above
# import `catalog` and `gen`, so this assertion has to come after the last RUN
# that executes Python or it reports nothing and passes.
#
# Both roots, because both hold Python this build imports: `/opt/swerefactor`
# arrives from the donor image and is imported by the grader check. A sweep
# narrower than what it cleans is the shape of an assertion that passes by luck --
# and the donor image is not this task's to keep clean, so checking only
# `/tests/behavioural` would be trusting a tree built somewhere else.
RUN set -eux; \
    for root in /tests/behavioural /opt/swerefactor; do \
      test -d "$root" || { echo "$root is missing; this sweep would pass by \
checking nothing" >&2; exit 1; }; \
      find "$root" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true; \
    done; \
    found="$(find /tests/behavioural /opt/swerefactor \( -name '*.pyc' -o -name '*.pyo' \
             -o -name __pycache__ \) -print)"; \
    if [ -n "$found" ]; then \
      echo "bytecode survived in the image:" >&2; echo "$found" >&2; exit 1; \
    fi; \
    echo "no bytecode in /tests/behavioural or /opt/swerefactor"

WORKDIR /tests/behavioural
