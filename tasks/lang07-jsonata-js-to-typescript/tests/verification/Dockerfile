# syntax=docker/dockerfile:1
# =============================================================================
# lang07 stage 3 -- the verification image
#
#     docker build -t swerefactor/lang07-verification:1 \
#         -f tests/verification/Dockerfile tests/verification
#
# Six models get a budget and a copy of the repository and try to find one request
# the TypeScript port answers differently from the JavaScript engine.  A candidate
# is a pytest file; it reaches both trees through `srbjsonata`, which speaks the
# same JSON-lines protocol stage 2 uses, and it counts as a break only when it
# PASSES on the original and FAILS on the submission.
#
# THE REFERENCE IS PUBLISHED HERE, NOT AT GRADING TIME
#
# That asymmetry is the whole design and it follows from how the stage scores.
# Because a candidate counts only if it passes on the original, anything that stops
# the original from answering turns every candidate into "does not pass on the
# original" -- no break is upheld, and the submission is paid all sixty points.  A
# broken stage 3 does not fail loudly; it awards full marks and looks exactly like
# a submission nobody could break.
#
# So the reference is built in the `reference` stage below, from the same
# digest-checked State A tarball stage 2 uses, and `check-probe.py` runs real
# candidates through the real `run-candidate.sh` against it before this image is
# allowed to exist.  Nothing rebuilds it during a grading run.
#
# WHY THIS IMAGE IS NOT `tommy`'S  (`tommy` is a C#-to-TypeScript pair, the
# compiled-reference shape this task's design was settled against)
#
# On every other language pair in this family the submission's image cannot run the
# reference: withhold the .NET SDK, or the Go compiler, or the JVM, and a C# file
# sitting in plain sight answers nothing.  The absence of a toolchain does most of
# the work and the filesystem mode is the belt to that braces.
#
# Here both sides are `node`, and node has to be present because the submission is
# JavaScript after `tsc`.  State A's `src/jsonata.js` is a complete, working JSONata
# engine, and `require`ing it is not a compile -- it is one line.  There is no
# toolchain to withhold, so the filesystem mode is not the belt.  It is the only
# thing there is, and this image is built around that:
#
#   * /opt/reference is 0700 and owned by `srbref`, the user the ORIGINAL role runs
#     as.  `srbprobe` -- the user everything on the submission's side runs as,
#     including its own `npm run build` -- can neither read the built reference nor
#     list the directory holding it.
#   * /opt/original is 0700 root:root.  tommy can leave State A world-readable
#     because State A is C# and there is no compiler; State A here is a runnable
#     engine, so it is sealed against BOTH unprivileged users.  Nothing at grading
#     time reads it: it exists for `check-probe.py`, which runs as root at build
#     time.  A submission that found it would have found a reference implementation
#     with a `require` away, which is precisely the thing being measured.
#   * `run-candidate.sh` chmods the harness's staged-trees directory to 0700 for the
#     duration of a run, because SRB_TARGET's *sibling* is the other tree and the
#     harness leaves it at 0755.  On tommy that sibling is inert; here it is a
#     second working engine, and it is the one seal this Dockerfile cannot apply
#     because the directory does not exist until the harness makes it.
#   * The seal is checked from the finished image at the bottom of this file, and
#     again at run time by `run-candidate.sh` on the submission's side, where a
#     failure is loud.  A stage-3 fault that fails on the original's side would be
#     discarded as INVALID and would pay the submission the round.
#
# There is no "no JavaScript in the image" assertion to make, and pretending
# otherwise would be the worst kind of check: the one that looks like proof.  What
# is enforceable is that exactly one built reference and exactly one copy of State A
# exist, both sealed, and no third copy of the engine is reachable -- which is the
# sweep near the bottom.
#
# Network: none at grading time.  Every artifact arrives in `fetch`, at build time,
# from a URL with a digest beside it.
# =============================================================================

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z
ARG INFRA_IMAGE=swerefactor/infra:1

FROM ${INFRA_IMAGE} AS infra

# --------------------------------------------------------------------------- #
# Stage `fetch` -- the pinned artifacts
# --------------------------------------------------------------------------- #
# The same three artifacts stage 2 fetches, at the same digests, plus pip's wheel.
# A pinned version is not a pin: npm and PyPI both allow a version to be
# republished, and the digest is what makes "this image was built from exactly
# that" a claim worth making.  tests/check-task.py holds this list byte-identical
# to tests/behavioural/Dockerfile's, because two copies of the same digests is a
# thing that drifts and neither build would notice.
FROM debian@sha256:26f2a7cab45014541c65f9d140ccfa6aaefbb49686c6759bea9c6f7f5bb3d72f AS fetch
ARG DEBIAN_FRONTEND
ARG DEBIAN_SNAPSHOT
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# `rm -f /etc/apt/sources.list.d/*` before writing is not tidiness: this base image
# ships a sources file of its own, and leaving it beside the snapshot one means apt
# resolves from whichever it prefers -- which is not a pin.
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

RUN set -eux; \
    curl -fsSL --retry 5 --retry-connrefused -o node.tar.xz \
      https://nodejs.org/dist/v24.18.1/node-v24.18.1-linux-x64.tar.xz; \
    echo 'd6c664df3f3f61458e8c277585571328522d705166723a7c7823a9253a4d15a0  node.tar.xz' \
      | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-connrefused -o typescript.tgz \
      https://registry.npmjs.org/typescript/-/typescript-5.9.3.tgz; \
    echo '10e108c9cf7d5f2879053dff18515fb405abf2ccef63eaaf017d9c571687a1d3  typescript.tgz' \
      | sha256sum -c -; \
    curl -fsSL --retry 5 --retry-connrefused -o pip-24.3.1-py3-none-any.whl \
      https://files.pythonhosted.org/packages/ef/7d/500c9ad20238fcfcb4cb9243eede163594d7020ce87bd9610c9e02771876/pip-24.3.1-py3-none-any.whl; \
    echo '3790624780082365f47549d032f3770eeb2b1e8bd1f7b2e02dace1afa361b4ed  pip-24.3.1-py3-none-any.whl' \
      | sha256sum -c -

# --------------------------------------------------------------------------- #
# Stage `toolchain` -- what the reference and the grading image share
# --------------------------------------------------------------------------- #
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
      python3-venv=3.11.2-1+b1 \
      tzdata=2025b-0+deb12u2 \
      xz-utils=5.4.1-1 \
    ; \
    rm -rf /var/lib/apt/lists/*

# Node, and tsc as a global.  Global is the contract, not a shortcut: the runner
# deletes `node_modules` from its copy of a tree before `npm run build`, so a
# submission whose build resolved `tsc` from a local install would fail on a tree
# with nothing wrong with it.
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

# The three versions are load-bearing enough to assert rather than print.  A
# toolchain that drifted by a major would change what `strict` means and what the
# emitted JavaScript looks like, and stage 2 grades both against these exact pins.
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
# The same tarball and the same digest file stage 2 uses; `swerefactor validate`
# holds this copy byte-identical to `environment/`.  The file count is asserted
# because a tarball that unpacked partially -- a truncated transfer that still had
# a valid gzip trailer -- would otherwise present as a reference that answers most
# requests, which is the shape of a stage-3 failure that pays sixty points.
FROM toolchain AS baseline
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

COPY data/original.tar.gz data/original.sha256 /opt/baseline/
RUN set -eux; \
    cd /opt/baseline; \
    sha256sum -c original.sha256; \
    install -d /opt/original; \
    tar -xzf original.tar.gz -C /opt/original --strip-components=1; \
    for f in package.json tools/build.js src/probe.js src/jsonata.js \
             src/probe-wire.js src/probe-fixtures.js jsonata.d.ts; do \
      test -f "/opt/original/$f" \
        || { echo "State A has no $f; the reference cannot be built" >&2; exit 1; }; \
    done; \
    test ! -e /opt/original/dist \
      || { echo "State A ships a dist/; its absence is what makes building the \
reference also a test that the shipped tree builds" >&2; exit 1; }; \
    test ! -e /opt/original/node_modules \
      || { echo "State A ships node_modules; the repository has no dependencies" >&2; exit 1; }; \
    count="$(find /opt/original -type f | wc -l)"; \
    test "$count" = 15 \
      || { echo "State A has $count files, expected 15" >&2; exit 1; }; \
    rm -f original.tar.gz original.sha256; \
    echo "State A unpacked: $count files"

# --------------------------------------------------------------------------- #
# Stage `reference` -- build State A, and nothing else
# --------------------------------------------------------------------------- #
# The reference *is* State A, built in place by the script it ships with.  There is
# no second copy of `jsonata.js` to keep in step with the first, and no patch
# applied on the way past: a drift between two copies would present as every
# candidate finding a difference, which reads like a port that got everything
# wrong.
#
# `dist/` is copied out to /opt/reference rather than the whole tree, because
# /opt/reference is the directory `srbref` is given and the source has no business
# being in it.  The `src/jsonata.js` digest travels alongside so `run-candidate.sh`
# can check that the tree it was handed as the original is the tree this reference
# was built from -- a staging mix-up that swapped the two roles would otherwise
# grade the submission against itself.
FROM baseline AS reference
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN set -eux; \
    cd /opt/original; \
    npm run build; \
    test -f dist/probe.js || { echo "the build produced no dist/probe.js" >&2; exit 1; }; \
    test -f dist/jsonata.js || { echo "the build produced no dist/jsonata.js" >&2; exit 1; }; \
    install -d /opt/reference; \
    cp -a dist /opt/reference/dist; \
    sha256sum src/jsonata.js | awk '{print $1}' > /opt/reference/jsonata.js.sha256; \
    printf '%s\n' '{"id":1,"op":"hello"}' \
      | node /opt/reference/dist/probe.js \
      | grep -q '"protocol":"jsonata-probe/1"' \
      || { echo "the built reference does not answer hello with the declared \
protocol" >&2; exit 1; }; \
    echo "reference built: $(wc -c < /opt/reference/dist/probe.js) bytes of probe"

# --------------------------------------------------------------------------- #
# Stage `final` -- the grading image
# --------------------------------------------------------------------------- #
FROM toolchain
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# --- The two unprivileged users -----------------------------------------------
# Two rather than root-for-the-reference.  Running one side of a differential
# comparison privileged and the other not is an asymmetry in the thing being
# compared; State A's probe only reads stdin and writes stdout, so the asymmetry is
# inert for it, but "inert for the reference" is not the same as "inert for a port"
# and the whole stage is about what a port does differently.
#
# `setpriv` and `useradd` come from util-linux and passwd, which are Essential in
# Debian and so are not in the apt list above.  Asserted rather than assumed:
# without setpriv every run fails to start, which on the submission's side reads as
# a port that cannot build -- an honest submission losing sixty points.
RUN set -eux; \
    command -v setpriv >/dev/null || { echo "setpriv is missing" >&2; exit 1; }; \
    useradd --system --no-create-home --shell /usr/sbin/nologin srbref; \
    useradd --system --no-create-home --shell /usr/sbin/nologin srbprobe; \
    id -u srbref; id -u srbprobe

# --- The reference, and the mode that keeps it out of reach --------------------
# 0700 srbref:srbref on the directory.  `srbref` runs the original role and needs
# to read what is in there; `srbprobe` runs everything on the submission's side and
# can neither read the built probe nor list the directory holding it.  The files
# are 0600: the directory does the work, but a file mode that contradicted it would
# be a confusing thing to leave behind for whoever edits this next.
COPY --from=reference /opt/reference /opt/reference
RUN set -eux; \
    chown -R srbref:srbref /opt/reference; \
    chmod 0700 /opt/reference; \
    chmod 0700 /opt/reference/dist; \
    find /opt/reference -type f -exec chmod 0600 {} +; \
    test -f /opt/reference/dist/probe.js; \
    test -f /opt/reference/jsonata.js.sha256

# --- State A, sealed ----------------------------------------------------------
# Baked in because `check-probe.py` builds a submission out of it at build time,
# which is the only positive check this stage can make about the submission path.
# 0700 root:root, and this is the line tommy does not have: State A is a working
# JavaScript engine in an image that must contain node, so it is sealed against
# both unprivileged users rather than left readable.  Nothing at grading time needs
# it -- the harness stages its own copies of both trees.
COPY --from=baseline /opt/original /opt/original
RUN set -eux; \
    chown -R root:root /opt/original; \
    chmod 0700 /opt/original; \
    test -f /opt/original/src/jsonata.js

# --- The candidate's test stack ----------------------------------------------
# pytest and pytest-timeout, one set of versions for both trees: a candidate that
# ran under different pytest versions on the two halves of a comparison would not
# be comparing the two implementations.  pip comes from the digest-checked wheel
# rather than from a resolve, so the venv is pinned all the way down.
COPY --from=fetch /dl/pip-24.3.1-py3-none-any.whl /opt/pins/
COPY requirements-candidate.txt /opt/pins/requirements-candidate.txt
RUN set -eux; \
    python3 -m venv /opt/venv; \
    /opt/venv/bin/pip install --no-cache-dir --no-index \
        /opt/pins/pip-24.3.1-py3-none-any.whl; \
    /opt/venv/bin/pip --version | grep -q '^pip 24.3.1' \
      || { echo "the venv's pip is $(/opt/venv/bin/pip --version)" >&2; exit 1; }; \
    /opt/venv/bin/pip install --no-cache-dir \
        --require-hashes -r /opt/pins/requirements-candidate.txt; \
    /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')"

# --- The harness -------------------------------------------------------------
# Runs the six rounds and the adjudicator, on the system python.  The venv above
# is what candidates run in: the process deciding what an adversary proved does not
# share an interpreter with the code that adversary wrote.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- This suite --------------------------------------------------------------
COPY . /tests/verification

# `data/` goes, and on this stage that is a seal and not tidying.
#
# The context carries `data/original.tar.gz` -- State A, the same 84 KB tarball
# `baseline` unpacks -- and `COPY .` above lands it at mode 664 root:root, which
# `srbprobe` can read.  `srbprobe` is the user the submission's own `npm run build`
# and probe run as.  So the thirty lines at the top of this file seal `/opt/original`
# and `/opt/reference` at 0700, the six setpriv checks below prove both seals hold,
# and a third copy of the same tree sat world-readable a directory away from the
# submission's own workspace.  One `tar -xzf` and a dishonest port has the engine it
# was asked to replace, with no mode to defeat.
#
# The sweep below did not catch it and could not: it looks for `jsonata*.js`,
# `probe-wire.js`, `probe-fixtures.js` and a `jsonata` directory, and a gzipped
# tarball is none of those.  That sweep's comment claims "no third copy of the engine
# anywhere"; this RUN is what makes the claim true, so it runs before the sweep
# rather than after it.
#
# Nothing here needs the directory at grade time.  The adversary is handed both trees
# by the harness, staged at run time and addressed by the names in `prompt.txt`, not
# from this suite's context; `source-contract.json` is read by the verifier stage and
# by `check-task.py`, neither of which is this image; and `original.sha256` did its
# work in `baseline`.  The tarball is checked for by name first, because a rename
# would otherwise turn this whole RUN into a no-op that still printed success.
RUN set -eux; \
    test -f /tests/verification/data/original.tar.gz \
      || { echo "/tests/verification/data/original.tar.gz is absent before the \
removal that exists to delete it: either the context stopped carrying State A or the \
path changed, and in the second case a readable copy of the engine is still here \
under a name this RUN no longer names" >&2; exit 1; }; \
    rm -rf /tests/verification/data; \
    echo "data/ removed: the readable copy of State A is gone, leaving the two \
sealed ones"

# `bash -n` first: a syntax error in run-candidate.sh is six rounds of "the
# original failed", which pays sixty points.  Then the harness end to end --
# check-probe.py runs real candidates through the real script against the published
# reference, in both directions, because a harness stuck at "pass" upholds nothing
# and one stuck at "fail" upholds everything.
RUN set -eux; \
    chmod +x /tests/verification/run-candidate.sh; \
    bash -n /tests/verification/run-candidate.sh; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/srbjsonata.py; \
    /opt/venv/bin/python -m py_compile /tests/verification/lib/handshake.py; \
    python3 /tests/verification/check-probe.py

# One reference, one State A, and no third copy of the engine anywhere.
#
# This is the assertion that replaces tommy's "no compiler on PATH", and it is
# weaker in a way worth stating plainly rather than dressing up: node is here and
# has to be, so a copy of the engine that this sweep misses is a copy a dishonest
# port could require.  What the sweep buys is that the two copies which legitimately
# exist are the only ones, both behind 0700 directories, so reaching either means
# defeating a filesystem mode rather than guessing a path.
#
# `find` on a pattern rather than `test -e` on fixed paths: the claim is that no
# other copy exists, including one a future edit puts somewhere convenient.  Note
# `-xdev` is deliberate and `/opt/venv` is not excluded -- a `jsonata` that arrived
# through pip would be exactly the kind of accident this is for.
RUN set -eux; \
    stray="$(find / -xdev \
             \( -name 'jsonata*.js' -o -name 'probe-wire.js' \
                -o -name 'probe-fixtures.js' \) \
             -not -path '/opt/original/*' \
             -not -path '/opt/reference/*' -print 2>/dev/null || true)"; \
    stray="$stray$(find / -xdev -type d -name jsonata \
             -not -path '/opt/original/*' \
             -not -path '/opt/reference/*' -print 2>/dev/null || true)"; \
    if [ -n "$stray" ]; then \
      echo "a copy of the engine exists outside State A and the reference:" >&2; \
      echo "$stray" >&2; exit 1; \
    fi; \
    refs="$(find /opt/reference -name 'probe.js' | wc -l)"; \
    test "$refs" -eq 1 \
      || { echo "/opt/reference holds $refs probe.js, expected exactly 1" >&2; exit 1; }; \
    test ! -e /opt/baseline \
      || { echo "/opt/baseline survived into the grading image" >&2; exit 1; }; \
    echo "one reference, one State A, no third copy of the engine"

# The same claim about archives, which is where the sweep above cannot look.
#
# It searches for `jsonata*.js`, `probe-wire.js`, `probe-fixtures.js` and a `jsonata`
# directory. A third copy of the engine was in this image the whole time as
# `data/original.tar.gz`, matching none of those, at mode 664 where `srbprobe` could
# read it -- so the sweep above printed "no third copy of the engine" over a readable
# one. The `rm -rf` beside the `COPY .` is what removes it; this is what makes the
# claim checkable rather than a description of what the author remembered copying.
#
# Byte-identical to `tests/behavioural/lib/check-archives.py`, held so by
# `check-task.py`'s `duplicates` group. The two stages need the same sweep because
# they acquired the same file the same way.
#
# On the system interpreter, not the venv: the venv is what candidates run in, and
# `py_compile` under it would both check the wrong interpreter and leave a
# `__pycache__` for this stage's own bytecode sweep to find. Running the sweep is the
# syntax check.
RUN python3 /tests/verification/lib/check-archives.py

# Neither unprivileged user can reach what it is not supposed to, checked from the
# finished image rather than reasoned about.  The chmods are forty lines up and the
# COPY of this suite came after them; an edit that reordered those, or a COPY that
# reset a mode, would leave a stage that looks identical and grades one engine
# against a second copy of itself.
#
# Six checks, and the pair at the end matters as much as the four denials: a
# `srbprobe` that cannot run node, or a `srbref` that cannot run the reference, is
# a stage that fails every candidate on one side.  On the original's side that is
# the sixty-point giveaway, so it is checked here where it costs nothing.
RUN set -eux; \
    if setpriv --reuid=srbprobe --regid=srbprobe --init-groups -- \
         test -r /opt/reference/dist/probe.js 2>/dev/null; then \
      echo "srbprobe can read the reference probe; a dishonest port could answer \
every candidate by requiring it" >&2; \
      exit 1; \
    fi; \
    if setpriv --reuid=srbprobe --regid=srbprobe --init-groups -- \
         ls /opt/reference >/dev/null 2>&1; then \
      echo "srbprobe can list /opt/reference" >&2; exit 1; \
    fi; \
    if setpriv --reuid=srbprobe --regid=srbprobe --init-groups -- \
         test -r /opt/original/src/jsonata.js 2>/dev/null; then \
      echo "srbprobe can read State A's engine source, which is a working engine \
one require away" >&2; \
      exit 1; \
    fi; \
    if setpriv --reuid=srbref --regid=srbref --init-groups -- \
         ls /opt/original >/dev/null 2>&1; then \
      echo "srbref can list /opt/original" >&2; exit 1; \
    fi; \
    setpriv --reuid=srbprobe --regid=srbprobe --init-groups -- \
      node -e 'process.stdout.write("")' \
      || { echo "srbprobe cannot run node, so no submission could be probed" >&2; \
           exit 1; }; \
    printf '%s\n' '{"id":1,"op":"hello"}' \
      | setpriv --reuid=srbref --regid=srbref --init-groups -- \
          node /opt/reference/dist/probe.js \
      | grep -q '"protocol":"jsonata-probe/1"' \
      || { echo "srbref cannot run the reference, so every candidate would be \
discarded as INVALID and the submission would be paid every round" >&2; exit 1; }; \
    echo "srbprobe: runs node, reaches neither the reference nor State A"

# No bytecode.  PYTHONDONTWRITEBYTECODE is set in the toolchain stage and
# .dockerignore excludes `**/__pycache__` from the context, but check-probe.py and
# the two py_compile calls above run Python, and this assertion has to come after
# the last RUN that does or it reports nothing and passes.
#
# Scoped to /tests/verification.  The venv's site-packages legitimately holds the
# bytecode `pip install` compiled, and a check that swept it too would have to
# carve that back out with a `grep -v` -- a filter wide enough to hide the thing
# this is looking for.
RUN set -eux; \
    find /tests/verification -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true; \
    found="$(find /tests/verification \( -name '*.pyc' -o -name '*.pyo' \
             -o -name __pycache__ \) -print)"; \
    if [ -n "$found" ]; then \
      echo "bytecode survived in /tests/verification:" >&2; echo "$found" >&2; exit 1; \
    fi; \
    echo "no bytecode in /tests/verification"

# Deliberately no ENV naming the reference or this directory.  `run-candidate.sh`
# holds both as constants; an environment variable saying the same thing is a
# second copy that stays correct until one of them moves, and this one would also
# hand the reference's path to every candidate through os.environ.
RUN mkdir -p /workspace /logs/verifier

WORKDIR /tests/verification
