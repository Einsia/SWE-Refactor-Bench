# =============================================================================
# SWERefactorBench -- lang04-acorn-js-to-rust
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang04-audit:1 \
#         tasks/lang04-acorn-js-to-rust/tests/audit
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
# There is deliberately no Rust toolchain here, and -- the part specific to this
# task -- no node either.  Both absences are asserted below rather than assumed.
# The Rust half is the same argument every language task makes: give the review a
# compiler and it starts grading build logs, which is stage 2's job in an image
# built to do it.  The node half is this task's own: State A is a JavaScript
# parser written in JavaScript, so a node in this image could run the original,
# and a review that can run the original can be talked into comparing outputs --
# which is the verification stage's job, with both toolchains and a reference
# build, three stages later.
#
# What is left is reading, which is what this stage is for.  The questions it
# answers are the ones no build settles: whether 116 JavaScript files became a
# Rust workspace or moved to a subdirectory, whether the recursive-descent
# structure in `acorn/src/*.rs` is acorn's or some other parser's, whether the
# token-context stack that decides `/` between regex and division was ported or
# guessed at.  A pair of binaries that agree on 16,049 cases cannot answer any of
# them, because a repository that kept every `.js` file *and* wrote a complete
# port builds a perfectly good pair of binaries.
#
# There is a pytest here, and it is not a contradiction of the paragraph above.
# It runs `scan.toml`: 158 file-reading checks whose output is substituted into
# the prompt at {{findings}} as leads for the reviewer to follow.  They walk
# directories, hash files and sniff the first four bytes to tell an ELF from a
# wasm module; nothing in them could satisfy a module that tried to compile, which
# is the property that keeps a scan a scan.  `swerefactor.scan` forces
# `required = false` on every check they emit, so no finding can fail the gate on
# its own -- the gates are the prose questions in evaluation.toml and only those.
#
# Network: the model API and nothing else.  It is the one stage that is not
# offline, which is the other half of why it gets no toolchain -- there is no
# point in this container where submitted code executes.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

FROM python@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
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
        git \
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
ENV PYTHONPATH=/opt/swerefactor

# --- The scan ----------------------------------------------------------------
# pytest is the scan's runner and `swerefactor.pytest_module` turns each collected
# test into one check, so the findings arrive keyed by node id.  Pinned by hash
# like everything else in this benchmark: a scan whose collection count moved
# between builds would change what the reviewer is told without anyone editing a
# file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

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
its output would reach nobody') if 'findings' not in found else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# The manifest, read by the same loader the runner uses, so a key this schema does
# not recognise fails here.  It is worth the four lines: `ModuleConfig` reads
# `title` and `about` and ignores anything else, so a module whose prose sat under
# a key with another name would load, run, score, and arrive in the report with
# its rationale silently dropped.
RUN python -c "\
import sys; \
from swerefactor.config import Suite, SCAN_SCHEMA; \
s = Suite.load('/tests/audit/scan.toml', schema=SCAN_SCHEMA); \
missing = [m.id for m in s.modules if not m.about.strip()]; \
sys.exit(f'scan.toml: module(s) whose prose did not load: {missing}') if missing else None; \
print('scan.toml ok: %d modules, %s' % (len(s.modules), ', '.join(m.id for m in s.modules)))"

# The contract every list in the scan is derived from.  `swerefactor validate` holds
# this copy byte-identical with environment/source-contract.json; what this adds is
# that it parses here and carries the four keys the library reads, because a scan
# whose contract failed to load reports nothing and says so nowhere.
RUN python -c "\
import json; \
c = json.load(open('/tests/audit/data/source-contract.json', encoding='utf-8')); \
assert c['task'] == 'lang04-acorn-js-to-rust', c['task']; \
assert c['forbidden_paths']['extensions'], 'no forbidden extensions'; \
assert c['retained_paths']['required'], 'no retained paths'; \
assert c['native_code_policy']['min_rust_logic_lines'] > 0, 'no Rust logic floor'; \
print('source-contract.json ok: %s, %d forbidden extensions, %d retained paths, %d-line floor' % ( \
    c['task'], len(c['forbidden_paths']['extensions']), len(c['retained_paths']['required']), \
    c['native_code_policy']['min_rust_logic_lines']))"

# No toolchain, asserted.  The stage's contract -- may read, may not execute -- is
# made true by the image and by nothing else, so it is checked in the image.  The
# list covers both languages: the one the submission is written in, and the one it
# was ported from, including the four JavaScript engines that ship as a single
# binary and would otherwise be an easy way to run State A from inside a review.
RUN set -eux; \
    for prog in cargo rustc rustup cc gcc g++ clang make cmake \
                node nodejs npm npx yarn pnpm deno bun qjs d8 js; do \
        if command -v "$prog" >/dev/null 2>&1; then \
            echo "stage 1 must not be able to build or run anything, but $prog is at $(command -v "$prog")" >&2; \
            exit 1; \
        fi; \
    done; \
    echo "no toolchain: confirmed absent for 19 programs"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.  Collection runs against the empty mount points, which is
# enough to execute every import and every parametrize call.  The floor is below
# the 158 a real pair of trees yields, because two of this suite's lists are
# derived from State A's tree rather than from the contract and collapse to their
# fallbacks when /opt/original is empty.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 120

# One runner per module, and each is a symlink to the single script in lib/.  A
# broken link is a module that scores zero for a reason no log explains, so it is
# checked with `-e`, which follows the link rather than merely finding it.
RUN set -eux; \
    for m in /tests/audit/modules/*/; do \
        test -e "${m}run.sh" || { echo "no runner for ${m}" >&2; exit 1; }; \
        test -x "${m}run.sh" || { echo "runner not executable: ${m}run.sh" >&2; exit 1; }; \
    done

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
