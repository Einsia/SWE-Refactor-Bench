# =============================================================================
# SWERefactorBench -- lang06-jsonnet-cpp-to-csharp
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang06-audit:1 \
#         tasks/lang06-jsonnet-cpp-to-csharp/tests/audit
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
# There is deliberately no .NET SDK and no C++ toolchain here.  This stage answers
# eleven questions -- did the C++ leave, is the C# the implementation, is
# std.jsonnet evaluated or was it hand-translated -- and every one of them is
# settled by reading source and project files.  Give the review a compiler and it
# starts grading build logs, which is stage 2's job in an image built to do it:
# stage 2 publishes both CLIs offline with single-file, AOT, trimming, ReadyToRun
# and self-contained forced off, then reads the ImplMap, ModuleRef and MemberRef
# tables of what it published.  Those are measurements of an artefact.  This stage
# is for the judgements no build output settles, chiefly whether 14,958 lines of
# C++ were reimplemented or transliterated.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It runs
# `scan.toml`: 179 file-reading checks whose output is substituted into the prompt
# as findings for the reviewer to follow up.  They walk directories, hash files and
# sniff the first bytes of one to see whether it is an assembly; there is no SDK
# here that could satisfy a module which tried to build anything, which is the
# property that keeps a scan a scan.
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
# data/source-contract.json rides along in this context on purpose.  The scan reads
# its path lists from that file instead of hardcoding them, and the RUN below
# asserts its sha256 -- the same digest environment/Dockerfile asserts over the copy
# the submitter is handed -- so the rules the scan applies cannot drift from the
# rules the submission was given.  It is the same bytes the submitter already has,
# and it names which paths must go and which must survive -- not what any CLI
# prints.
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

# The contract has to be the right bytes, it has to parse, and the keys the scan
# reads have to be in it.  Those are three separate failures and this is both halves
# of the drift check: the digest proves this context's copy is the one the submitter
# was handed, and the key list proves those bytes still say what the modules ask
# them for.  `contract_section` raises on a missing key rather than defaulting to an
# empty list, so a renamed key is an import error and the next RUN catches it -- but
# catching it here names the key instead of printing a traceback.
#
# The digest is a pin in its third place: environment/ asserts it over the copy the
# agent reads, stage 2 asserts it before freezing expectations from it, and this
# stage asserts it before scanning against it.  Three copies of one pin need a
# cross-check that no single build context can perform, because none of them can see
# the other two -- so a host validator compares all three and confirms each asserted
# digest is the file's real sha256.
RUN set -eux; \
    echo '2c6a07be63642e6408dc814bf895798183c8b654f6b6c937c3e3f93b5c5f6f19  /tests/audit/data/source-contract.json' \
      | sha256sum -c -
RUN python -c "\
import json, sys, pathlib; \
doc = json.loads(pathlib.Path('/tests/audit/data/source-contract.json').read_text(encoding='utf-8')); \
sys.exit(f'contract is for task {doc.get(\"task\")!r}') if doc.get('task') != 'lang06-jsonnet-cpp-to-csharp' else None; \
need = [('preserved_paths', 'files'), ('preserved_paths', 'directories'), \
        ('preserved_paths', 'byte_identical'), ('forbidden_paths', 'directories'), \
        ('forbidden_paths', 'files'), ('forbidden_paths', 'bazel_build_names'), \
        ('forbidden_paths', 'bazel_build_extensions'), ('conformance_data', 'directories'), \
        ('native_code_policy', 'extensions'), ('native_code_policy', 'allowed_prefixes'), \
        ('behavioral_contract', 'upstream_version'), ('behavioral_contract', 'stdlib_sha256')]; \
missing = [f'{a}.{b}' for a, b in need if not isinstance(doc.get(a), dict) or b not in doc[a]]; \
sys.exit(f'contract is missing key(s) the scan reads: {missing}') if missing else None; \
print(f'contract ok: {len(need)} keys present, task {doc[\"task\"]!r}')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.  An import error is caught by pytest's exit status; the floor
# catches the quieter failure, a module that collects but collapses to nothing.
#
# Collection runs against the empty mount points, which is enough to execute every
# import and every parametrize call.  Note what the floor is therefore a floor on:
# with /opt/original empty, the lists derived from State A fall back and the suite
# collects 145 rather than the 179 a real pair of trees yields.  The floor is 120
# because the three modules contribute 46, 35 and 64, so losing even the smallest of
# them leaves 110 -- a floor of 80 would have let any one of the three disappear
# without failing the build, which is the failure this line exists to prevent.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 120

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
#
# The bytecode assertion is last so it covers every RUN above it.  This stage is the
# one of the three that gets it right by construction -- PYTHONDONTWRITEBYTECODE is
# in an ENV before the COPY, so the pytest collection above inherits it -- and the
# assertion is here so that stays true if the ENV is ever moved or split.  A
# collect-check that imports every module is exactly the thing that would fill
# /tests with .pyc the moment it stopped being inherited.
RUN set -eux; \
    mkdir -p /opt/original /opt/workspace /logs/verifier; \
    found="$(find /tests -name '__pycache__' -o -name '*.pyc' | head -20)"; \
    if [ -n "$found" ]; then \
      echo "build-time bytecode shipped under /tests:" >&2; \
      echo "$found" >&2; \
      exit 1; \
    fi; \
    echo "no build-time bytecode under /tests"

WORKDIR /tests/audit
