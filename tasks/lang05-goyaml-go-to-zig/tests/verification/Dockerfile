# syntax=docker/dockerfile:1
# =============================================================================
# SWERefactorBench -- lang05-goyaml-go-to-zig
# Stage 3 image: the verification probe.
#
#     docker build -t swerefactor/lang05-behavioural:1  ../behavioural   # FIRST
#     docker build -t swerefactor/lang05-verification:1 .
#
# Six models, an hour each, each trying to write one pytest file that the
# original passes and the submission fails.  A candidate that both trees pass, or
# that both fail, is worth nothing: the verdict this stage produces is a
# disagreement, so the stage has to be able to run both sides.
#
# Which makes the reference binary the whole design problem here, and it is worth
# being explicit about how it is resolved.  State A is a Go *library*; it has no
# executable, so "build the original and run it" is not available the way it is
# for lang01, whose original is a C program with a command line.  What plays the
# original's part is the verifier's own Go program -- reference/main.go, importing
# gopkg.in/yaml.v3 -- which speaks the same NDJSON protocol the submission's
# zig-out/bin/yaml-probe does.
#
# That binary already exists: stage 2 builds it, asks it every generated case, and
# freezes the answers.  This image copies it from there rather than building a
# second one, and the reason is not build time.  Two Go builds would give the
# benchmark two oracles.  Stage 2 would freeze 8,000-odd answers against one, and
# this stage would assert "the original passes this candidate" against the other,
# and a submission could then match the graded oracle while disagreeing with the
# verification one -- 60 points decided by a difference between two Dockerfiles.
# One binary, copied, with its digest checked against the manifest of the image
# that froze the answers, makes them the same artefact structurally instead of by
# cross-check.  It also means there is no Go in this image at all, which is a
# stronger statement than installing a toolchain and deleting it.
#
# The cost is a build-order dependency, stated in the two commands above and
# enforced by the FROM below: without swerefactor/lang05-behavioural:1 present, this
# build fails immediately with "image not found" rather than producing something
# subtly wrong.  README.md's documented order already builds behavioural first.
#
# At runtime the harness provides:
#
#     /workspace/repo    the submission                            (read-only)
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /logs/verifier     where the stage result JSON is written
#
# swerefactor.verification stages its own copy of each tree under $SRB_WORK, keyed by
# an opaque token, so what stage 2 measured is not what a candidate build mutates.
#
# Network: the model API for the six rounds and the adjudicator, and nothing
# else.  `zig build` runs with no package fetches -- the contract forbids
# build.zig.zon dependencies -- so a submission cannot acquire anything here that
# it could not acquire in the agent's own offline environment.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
ARG BEHAVIOURAL_IMAGE=swerefactor/lang05-behavioural:1

FROM ${INFRA_IMAGE} AS infra

# The stage-2 image, read for exactly two files: the reference binary and the
# manifest recording its digest.  Nothing from it survives into a layer here
# except the binary -- see the --mount below.
FROM ${BEHAVIOURAL_IMAGE} AS behavioural

# ---------------------------------------------------------------------------
# The grading image
# ---------------------------------------------------------------------------
# The same digest-pinned Debian as the agent environment and as stage 2, and this
# is load-bearing rather than tidy.  A submission that builds in stage 2 has to
# build here: if `zig build` fails in this image and succeeded there, every
# candidate is answered "the submission failed" and all six rounds are upheld,
# which costs the submission the whole 60 points for a difference between two base
# images.  The failure is silent in exactly the way that matters -- the report says
# six models found defects.
#
# Five values are therefore shared with stage 2's Dockerfile and the agent's:
# this digest, DEBIAN_SNAPSHOT, ZIG_VERSION, ZIG_SHA256 and the python3 version.
# tests/check-task.py cross-checks them across the files rather than trusting
# three copies to stay equal.
FROM debian@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818

ARG DEBIAN_FRONTEND=noninteractive
ARG DEBIAN_SNAPSHOT=20251020T000000Z
ARG ZIG_VERSION=0.14.1
ARG ZIG_SHA256=24aeeec8af16c381934a6cd7d95c807a8cb2cf7df9fa40d359aa884195c4716c

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
ENV TZ=UTC \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

# The frozen snapshot, and `rm -f sources.list.d/*` for the reason it is in all
# three of this task's images: writing sources.list does not replace the
# distribution's own debian.sources, and without the removal these pins resolve
# against the live archive.
RUN rm -f /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm main" \
      "deb http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT} bookworm-security main" \
      "deb http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT} bookworm-updates main" \
      > /etc/apt/sources.list; \
    printf 'Acquire::Check-Valid-Until "false";\n' \
      > /etc/apt/apt.conf.d/99no-check-valid-until; \
    printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/99retries

# Two groups, and the split is deliberate.
#
# The first four are pinned because a version change would change what this stage
# does: python3 runs the harness, python3-venv builds the interpreter every
# candidate runs under, and curl and xz-utils fetch and unpack the compiler that
# builds the tree under test.
#
# The rest are unpinned, as they are in lang01's stage 3 and this task's stage 1,
# because nothing checks a version of them and nothing should: they exist so that a
# person reading a transcript afterwards can follow what a round did.  The model
# cannot reach them.  `allowed_commands = []` in probe.toml means the toolbox
# exposes list_dir, fetch_source, search and diff and no `run` tool at all, so there
# is no path from an adversary to a subprocess in this container -- the only thing
# it gets to execute is the pytest file it writes, under the screen.
RUN apt-get -qq update \
 && apt-get -qq install -y --no-install-recommends \
      ca-certificates=20230311+deb12u1 \
      curl=7.88.1-10+deb12u14 \
      xz-utils=5.4.1-1 \
      python3=3.11.2-1+b1 \
      python3-venv=3.11.2-1+b1 \
      diffutils \
      file \
      jq \
      less \
      ripgrep \
      tree \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb /var/cache/apt/*.bin \
 && python3 --version | cut -d' ' -f2 | grep -Fx '3.11.2'

# Zig, the one compiler here.  Same version and same digest as stage 2 and the
# agent environment: this is the toolchain the submission is entitled to be built
# with, and a candidate's "the submission fails" has to mean the port is wrong
# rather than that it met a different compiler.
RUN cd /tmp \
 && curl -fsSLO "https://ziglang.org/download/${ZIG_VERSION}/zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
 && echo "${ZIG_SHA256}  zig-x86_64-linux-${ZIG_VERSION}.tar.xz" | sha256sum -c - \
 && install -d /opt/zig \
 && tar -xJf "zig-x86_64-linux-${ZIG_VERSION}.tar.xz" -C /opt/zig --strip-components=1 \
 && rm -f "zig-x86_64-linux-${ZIG_VERSION}.tar.xz" \
 && ln -s /opt/zig/zig /usr/local/bin/zig \
 && zig version | grep -Fx "${ZIG_VERSION}"

# --- The reference: stage 2's oracle, not a second build of it ----------------
# One RUN with a build mount, rather than `COPY --from=behavioural`, for two
# reasons.  The mount lets the digest be checked against the manifest in the same
# step that installs the binary, so a copy of the wrong file cannot reach a layer;
# and nothing else from that image is mounted into one.  Stage 2's /opt/assets is
# 40-odd MB of frozen answers to 8,000-odd cases, and this stage has no business
# carrying them: the adversary is supposed to find a disagreement by reading the
# two implementations, and a directory of pre-computed reference answers sitting
# in the same container is at best irrelevant and at worst a shortcut nobody
# intended.  A `COPY --from` of the binary alone would also do -- the mount adds
# the digest check.
#
# The reference is static (CGO_ENABLED=0), so it does not care that this image's
# libc is not the one it was linked on, and it answers with no arguments:
# reference/main.go treats an empty argv as `serve`, which is the same protocol
# entry point the submission's yaml-probe has.  That is why run-candidate.sh can
# execute both sides with one argv and srbyaml.Probe can spawn `[self.binary]`.
#
# /opt/reference/ rather than /opt/assets/: the convention in this benchmark is
# that /opt/assets is what a *stage* ships, and this file is not this stage's
# asset -- it arrives from another image, and the name should say so.
RUN --mount=from=behavioural,source=/opt/assets,target=/mnt/assets,ro \
    install -d -m 0755 /opt/reference \
 && python3 - <<'EOF'
import hashlib, json, pathlib, shutil, sys

assets = pathlib.Path("/mnt/assets")
manifest = json.loads((assets / "verifier-manifest.json").read_text("utf-8"))

if manifest.get("task") != "lang05-goyaml-go-to-zig":
    sys.exit(f"the behavioural image grades {manifest.get('task')!r}, not lang05; "
             f"BEHAVIOURAL_IMAGE points at another task's stage 2")

want = (manifest.get("digests") or {}).get("reference")
if not want:
    sys.exit("the behavioural image's manifest records no `reference` digest, so "
             "there is nothing to verify this binary against")

src = assets / "bin" / "reference"
got = hashlib.sha256(src.read_bytes()).hexdigest()
if got != want:
    sys.exit(f"{src} hashes {got}, but the manifest that froze the answers "
             f"records {want}; the oracle and the binary this stage would call "
             f"the original are not the same file")

dst = pathlib.Path("/opt/reference/yaml-probe")
shutil.copy2(src, dst)
dst.chmod(0o755)

# Where it came from, kept in the image.  A grading run that produced a strange
# result is read months later, and "which reference answered these rounds" is
# the first question.  The digest is here so the answer does not depend on the
# stage-2 image still existing.
pathlib.Path("/opt/reference/provenance.json").write_text(
    json.dumps({
        "note": "built by tests/behavioural's freeze.py; copied, not rebuilt",
        "reference_sha256": want,
        "behavioural_image": manifest.get("image"),
        "behavioural_built_at": manifest.get("built_at"),
        "upstream_version": manifest.get("upstream_version"),
        "graded_cases_frozen_against_it":
            (manifest.get("counts") or {}).get("graded_total"),
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"reference: {dst.stat().st_size} bytes, sha256 {want[:16]}..., "
      f"from {manifest.get('image')}")
EOF

# Asked, not trusted.  A reference that cannot answer `hello` in *this* image
# would make every round of this stage come back INVALID -- the adjudicator
# requires the original to pass a candidate before it will look at the
# submission -- and the report would read as six models finding nothing, which
# is the same shape as a submission earning all 60 points.
RUN printf '%s\n' '{"id":1,"op":"hello"}' \
      | /opt/reference/yaml-probe \
      | tee /dev/stderr \
      | grep -F '"upstream":"v3.0.1"' >/dev/null \
 && echo "the reference answers hello in this image"

# --- The candidate's test stack -----------------------------------------------
# A venv, separate from the system interpreter on purpose: the process that
# orchestrates the stage and talks to the model API does not share an interpreter
# with code an adversary wrote.  pytest and pytest-timeout, at one set of versions
# for both trees, and nothing else -- see requirements-candidate.txt for why the
# absences are deliberate.
COPY requirements-candidate.txt check-venv.py /opt/pins/

RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        --require-hashes -r /opt/pins/requirements-candidate.txt \
 && /opt/venv/bin/python -c "import pytest, pytest_timeout; print('candidate stack ok')" \
 && /opt/venv/bin/python /opt/pins/check-venv.py

# --- The harness -------------------------------------------------------------
# Runs the six rounds, the candidate screen and the adjudicator.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The probe ---------------------------------------------------------------
COPY . /tests/verification

# The build context is this directory, so these two are context rather than
# grading input; leaving them in the image would ship a Dockerfile that describes
# an image built from a directory that no longer looks like this one.
RUN rm -f /tests/verification/Dockerfile /tests/verification/.dockerignore \
 && chmod +x /tests/verification/run-candidate.sh \
                /tests/verification/screen-candidate.py \
                /tests/verification/check-probe.py \
 && bash -n /tests/verification/run-candidate.sh \
 && python3 -m py_compile /tests/verification/screen-candidate.py \
                          /tests/verification/check-probe.py \
                          /tests/verification/lib/srbyaml.py \
 && echo "the probe's scripts parse"

# The screen decides whether a candidate runs at all, and it fails dangerously in
# one direction: a screen that refused everything would refuse the original too,
# the adjudicator would mark every round INVALID, six INVALID rounds report as
# SURVIVED, and the submission would collect all 60 points from a broken script.
# That is indistinguishable in the final report from six models finding nothing.
#
# So it is exercised here, both ways, on two candidates written inline.  They are
# not shipped: a stage directory holds lib/, modules/ and data/, and two strings
# that exist to make a script go green and red once at build time are neither code
# the stage imports nor an input it grades.
RUN set -e; \
    d="$(mktemp -d)"; \
    printf '%s\n' \
      'import srbyaml' \
      'def test_scalar():' \
      '    assert srbyaml.node("a: 1")["kind"] == "mapping"' \
      > "$d/honest.py"; \
    printf '%s\n' \
      'import os' \
      'def test_which_tree():' \
      '    assert "original" not in os.environ["SRB_TARGET"]' \
      > "$d/cheat.py"; \
    if ! python3 /tests/verification/screen-candidate.py "$d/honest.py"; then \
      echo "the screen refuses an honest candidate; every round would be INVALID \
and the submission would collect all 60 points" >&2; exit 1; \
    fi; \
    if python3 /tests/verification/screen-candidate.py "$d/cheat.py" 2>/dev/null; then \
      echo "the screen admits a candidate that reads SRB_TARGET; an adversary \
could identify which tree it is running against" >&2; exit 1; \
    fi; \
    rm -rf "$d"; \
    echo "the screen admits honest work and refuses tree identification"

# probe.toml and prompt.txt, through the loader the harness itself uses.  A
# scope-less probe is unwinnable, a mismatched adversary count creates points
# nobody can earn, and a prompt that documents an `srbyaml` function the library
# does not have spends a candidate on a TypeError that reads as "fails on both
# trees".  check-probe.py checks all three; it is run here as well as by
# `swerefactor validate` because this is the image that would otherwise discover it
# six rounds in.  It also verifies that candidate_command names a script that
# is really in the image, so a COPY that missed one fails the build.
RUN python3 /tests/verification/check-probe.py

# No Go, asserted by name.  There is no `rm` above this to distrust -- no Go was
# ever installed -- so what this catches is a future edit: a base image that
# started shipping one, or an `apt-get install` that acquired `golang-go` as a
# recommendation.  The claim it protects is the one that makes this stage's
# original credible: a submission cannot shell out to a Go toolchain and be the
# reference, because there is not one here to be.
#
# `command -v` over the names a distribution might install rather than a test for
# /usr/local/go, which would pass on an image where the package manager had put
# `go` in /usr/bin.  Zig is deliberately not in this list: this stage builds the
# submission, so its compiler belongs here.
RUN for tool in go gofmt godoc gopls gccgo go1.23.4 gorun goimports \
                golangci-lint dlv cgo; do \
      if command -v "$tool" >/dev/null 2>&1; then \
        echo "a Go toolchain is on PATH in the verification image: $tool at \
$(command -v "$tool"); a submission could call it and be the reference" >&2; \
        exit 1; \
      fi; \
    done \
 && test ! -e /usr/local/go \
 && test -z "$(find / -xdev -type f -name 'go' -perm -u+x 2>/dev/null | head -1)" \
 && command -v zig >/dev/null \
 && echo "no Go toolchain in the verification image; zig $(zig version) is present"

# Zig writes .zig-cache into the working directory by default, and the working
# directory during a candidate build is a staged copy of the submission.  Pointed
# outside it so a build cannot deposit anything in the tree being read -- an
# adversary reads both trees, and a cache directory that appeared in one of them
# after the first candidate ran would be a way to tell them apart.
# run-candidate.sh sets both again, per tree, under $SRB_WORK; these are the
# defaults for anything else that invokes the compiler.
ENV ZIG_GLOBAL_CACHE_DIR=/tmp/zig-cache-global \
    ZIG_LOCAL_CACHE_DIR=/tmp/zig-cache-local
RUN install -d -m 0777 /tmp/zig-cache-global /tmp/zig-cache-local

# No bytecode.  This is the last RUN that imports anything under /tests, so .pyc
# files written now would be the only mutable bytes in the image outside the
# mounts and the work directory -- and `screen-candidate.py` refuses a candidate
# that touches sys.modules, which is a weaker claim if the image ships caches a
# candidate's interpreter would load.  PYTHONDONTWRITEBYTECODE is set below rather
# than above, so the checks that had to import were still allowed to write it.
# The assertion names the same three roots as the sweep.  It named one, so this
# passed on whatever the sweep happened to reach: stage 2's equivalent line swept
# and asserted a single root and shipped twenty .pyc files under /opt/swerefactor,
# found by looking in the built image.  tests/check-task.py now compares the two
# lists in all three stage Dockerfiles.
RUN find /tests/verification /opt/swerefactor /opt/venv -name __pycache__ -type d \
      -prune -exec rm -rf {} + \
 && test -z "$(find /tests/verification /opt/swerefactor /opt/venv -name '*.pyc' \
      -print -quit)"

# PYTHONHASHSEED, because a candidate that iterated a set or a dict of parse
# results and asserted on the order would otherwise be non-deterministic, and
# `require_deterministic = true` in probe.toml would reject it on a rerun rather
# than telling the adversary what went wrong.  NO_COLOR and the locale for the
# same reason: a candidate's failure output is read by the adjudicator, and escape
# codes in it are noise in a transcript.
#
# Not set here: SRB_PROBE, SRB_SCRATCH, SRB_TARGET or anything else the harness
# owns.  Those arrive per run, and run-candidate.sh re-exports the two the library
# reads under `env -i` -- a default baked into this image would be a second answer
# to a question the harness already answers, and one that survived `env -i`.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0 \
    NO_COLOR=1

RUN install -d -m 0755 /opt/original /workspace /logs/verifier

WORKDIR /tests/verification

