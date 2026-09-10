# =============================================================================
# SWERefactorBench -- lang02-zlib-c-to-java
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang02-audit:1 \
#         tasks/lang02-zlib-c-to-java/tests/audit
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
# There is deliberately no JDK and no CMake here, which for a language migration is
# worth spelling out.  This stage answers thirteen questions -- did the C leave, is
# the Java the implementation, does the jar's deflate belong to this repository --
# and every one of them is settled by reading source and build files.  Give the
# review a compiler and it starts grading build logs, which is stage 2's job in an
# image built to do it: stage 2 configures both link configurations under a shimmed
# cc, watches for a cc1 that runs, reads every shipped class file's SourceFile
# attribute, and runs the jar under a class loader that refuses java.util.zip.
# Those are measurements.  This stage is for the judgement calls that no amount of
# build output settles, chiefly whether 13,200 lines of C were reimplemented or
# delegated to the JDK's own copy of zlib.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It runs
# `scan.toml`: 130 file-reading checks whose output is substituted into the prompt
# as findings for the reviewer to follow up.  They walk directories, hash files and
# read the first four bytes of one to see whether it is a class file; there is no
# javac here that could satisfy a module which tried to compile anything, which is
# the property that keeps a scan a scan.
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

# The contract in this suite's data/ directory is what every derived list in the
# scan comes from -- the eighteen translation units, the nine private headers, the
# forbidden extensions, the logic-line floor.  `swerefactor validate` holds it
# byte-identical to environment/source-contract.json, so the scan reasons from the
# same file the agent's own environment carried.  Checked here because a scan
# pointed at a missing contract still collects: every parametrize falls back to a
# single placeholder, the module reports one failure instead of seventy, and in the
# rendered prompt that is nearly indistinguishable from a clean tree.
RUN python -c "\
import json, sys, pathlib; \
path = pathlib.Path('/tests/audit/data/source-contract.json'); \
sys.exit(f'{path} is missing') if not path.is_file() else None; \
contract = json.loads(path.read_text(encoding='utf-8')); \
sys.exit(f\"the contract describes {contract.get('task')!r}, not lang02\") \
    if contract.get('task') != 'lang02-zlib-c-to-java' else None; \
missing = [k for k in ('forbidden_paths', 'preserved_paths', 'jvm_code_policy', \
                       'classfile_contract', 'driver_contract') \
           if k not in contract]; \
sys.exit(f'the contract is missing {missing}') if missing else None; \
print('source-contract.json ok:', contract['schema_version'])"

# Neither toolchain is here, and that is asserted rather than assumed.  The claim
# in the header -- that a scan module cannot build the submission -- is only true
# while it stays true, and `apt-get install ripgrep` pulling in a compiler as a
# recommendation is exactly the kind of change nobody would notice.
RUN for prog in javac java jar cmake cc gcc make; do \
        if command -v "$prog" >/dev/null 2>&1; then \
            echo "$prog is on PATH in the stage 1 image; a scan module could build \
the submission, and this stage's whole claim is that it cannot" >&2; \
            exit 1; \
        fi; \
    done; \
    echo "toolchain absent: no javac, java, jar, cmake or C compiler"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.  Collection runs against the empty mount points, which is
# enough to execute every import and every parametrize call.
#
# The floor is 120 against a real count of 130.  Unlike lang01's, this suite's
# count does not move when the mounts are empty: every parametrized list comes from
# the contract in data/ rather than from walking State A, so collection is
# deterministic.  The gap is headroom for a check to be removed on purpose; the
# failure it catches is a module raising on import, which drops 27 checks at the
# smallest and takes the total under the floor.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 120

# Collection proves every module imports; it does not prove the predicates decide
# correctly, and one of them could not.  `find_name` is what answers "does this
# authored file mention one of nineteen third-party deflate implementations", and
# the substring test it replaced answered yes for `testInflate` -- zlib's own
# test_inflate driver, spelled as Java spells it -- because "tes|tinflate" contains
# the name.  That is a finding of vendoring, against the required `no-foreign-port`
# gate, aimed at a faithful port.
#
# It reached a live submission because nothing before one could show it up: State A
# is C, spells the name `test_inflate`, and the underscore breaks the accidental
# match.  So the identity run passes and every correct Java port fails -- the
# failure mode is invisible to exactly the instrument built to catch failures.  A
# case table is the only thing that sees it, and it runs here so that it runs at
# all.  Measured against the call site it replaced: 6 of the 15 cases wrong -- three
# false positives (`testInflate`, `testDictInflate`, `latestinflateState` all read as
# mentions of `tinflate`) and three real mentions that matched but cited line 0,
# because `locate()` is case-sensitive and `JZlibDeflater`, `MINIZ_EXPORT` and
# `useTinflate` do not contain the lowercase name.  A finding at line 0 quoting only
# the needle is one a reviewer can neither check nor dismiss.
RUN python /tests/audit/lib/selfcheck_srbscan.py

# No bytecode in the image.  PYTHONDONTWRITEBYTECODE is set above, but pip and the
# collect-check both run before this line and either could have left a __pycache__
# behind; a stage image whose contents depend on who built it is not reproducible.
# Placed after the last RUN that executes Python, because a check placed before the
# step that writes the file reports nothing and passes.
RUN find /tests/audit /opt/swerefactor -name __pycache__ -type d -prune \
        -exec rm -rf {} + 2>/dev/null || true; \
    found="$(find /tests/audit /opt/swerefactor \
        \( -name '*.pyc' -o -name '*.pyo' -o -name __pycache__ \) -print)"; \
    if [ -n "$found" ]; then \
        echo "bytecode survived in the image:" >&2; echo "$found" >&2; exit 1; \
    fi; \
    echo "no bytecode in /tests/audit or /opt/swerefactor"

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
