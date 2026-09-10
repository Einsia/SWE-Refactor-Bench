# =============================================================================
# SWERefactorBench -- lang07-jsonata-js-to-typescript
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang07-audit:1 \
#         tasks/lang07-jsonata-js-to-typescript/tests/audit
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
# This stage carries more of this task than its equivalent carries anywhere else in
# the benchmark, and the reason is one measurement.  Copy State A's nine `src/*.js`
# to `src/*.ts`, add `// @ts-nocheck` to each, write a strict-looking tsconfig, and
# run `tsc`: that submission scores 40.000 out of 40 on the behavioural stage --
# every one of the 13,940 cases -- because it *is* the original, and a differential
# test cannot distinguish a program from itself.  A real port with one changed line
# in `$uppercase` fails one case and scores 0.  So "was the repository rewritten"
# is not a question stage 2 can answer at any resolution, and it is asked here
# instead.  Stage 2 is left to do what it is good at: 13,940 byte-exact behavioural
# comparisons, a build from a scrubbed copy, a grep of the emitted JavaScript for
# process spawning, a re-run with the corpus unreadable, and an ill-typed file
# compiled to find out whether `strict` means anything.
#
# There is deliberately no Node here, and on this task that absence does more work
# than on any other.  Elsewhere the reference cannot execute in the submission's
# environment -- no .NET, no compiler for the old language -- so withholding a
# toolchain is a structural defence.  Here both runtimes are `node`: every State A
# file that survived into the submission is an executable implementation, reachable
# by `require` with no build step and no network.  A scan module with `node` on
# PATH could therefore run the submission's probe, in the one container in this
# benchmark that has network access to the model API.  That is asserted below
# rather than assumed.
#
# There is a pytest, and it is not a contradiction.  It runs `scan.toml`: 84
# file-reading checks whose output is substituted into the prompt as findings for
# the reviewer to follow up.  They walk directories, hash every file in both trees,
# count logic lines with the same counter the contract's own figures were produced
# by, lex JavaScript string literals, and read the first bytes of a file to see
# whether it is an ELF image or a zip.  Two of their reports are lists rather than
# complaints -- the inventory of surviving JavaScript with each file's bytes
# compared against State A, and the diff -- because on this task no extension is a
# finding on its own and only location plus byte-identity carries information.
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

# The prompt is this task's most load-bearing file.  Check at build time that it is
# present, that it is prose rather than a stub, and that its placeholders are ones
# the runner fills -- an unknown placeholder survives into the transcript verbatim,
# which is visible but late.
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

# The three statements this task's prompt cannot be missing, checked as text.
#
# Every other build-time prompt check in this benchmark is structural -- length,
# placeholders -- because elsewhere the prompt's content is the task author's
# business.  Here three specific statements are load-bearing in a way that outlasts
# any edit: `dist/` is emitted JavaScript by design, no extension is a finding on
# its own, and `tools/build.js` may legitimately stay JavaScript.  A reviewer not
# told those reports `.js` files as findings and fails honest submissions -- on a
# required gate, which is reward zero.
#
# Compared after collapsing whitespace, in both the prompt and the pattern.  The
# prompt is prose wrapped at 80 columns, so any of these sentences can land across
# a line break, and a check that breaks when a paragraph is rewrapped is a check
# that fires on an edit which changed nothing.  Rewording is fine; deleting the
# point is not.
RUN python -c "\
import re, sys, pathlib; \
text = pathlib.Path('/tests/audit/prompt.txt').read_text(encoding='utf-8'); \
flat = ' '.join(text.split()); \
required = { \
  'Do not report the presence of \`.js\` files.': \
      'without it a reviewer reports dist/ as a finding and fails an honest port', \
  'No extension is a finding on this task.': \
      'the one rule that separates this task from every other language pair', \
  'tools/build.js': \
      'the build helper may stay JavaScript and the prompt must say so', \
  'dist/': \
      'the reviewer has to be told that emitted JavaScript is required', \
}; \
missing = {k: why for k, why in required.items() if ' '.join(k.split()) not in flat}; \
[print(f'prompt.txt is missing {k!r}: {why}', file=sys.stderr) \
 for k, why in missing.items()]; \
sys.exit(f'prompt.txt is missing {len(missing)} required statement(s)') if missing else None; \
print(f'prompt.txt states the .js exemptions ({len(required)} checked)')"

# The contract in this suite's data/ directory is what every derived list in the
# scan comes from -- the forbidden extensions and directories, the preserved,
# removable and must-author paths, the harness vocabulary, the tsconfig flags, the
# logic-line floor.  `swerefactor validate` holds it byte-identical to
# environment/source-contract.json, so the scan reasons from the same file the
# agent's own environment carried.  Checked here because a scan pointed at a
# missing contract still collects: every parametrize falls back to a single
# placeholder, the module reports one failure instead of eighty-four, and in the
# rendered prompt that is nearly indistinguishable from a clean tree.
RUN python -c "\
import json, sys, pathlib; \
path = pathlib.Path('/tests/audit/data/source-contract.json'); \
sys.exit(f'{path} is missing') if not path.is_file() else None; \
contract = json.loads(path.read_text(encoding='utf-8')); \
sys.exit(f\"the contract describes {contract.get('task')!r}, not lang07\") \
    if contract.get('task') != 'lang07-jsonata-js-to-typescript' else None; \
missing = [k for k in ('forbidden_paths', 'preserved_paths', 'removable_paths', \
                       'submission_must_author', 'source_tree_facts', \
                       'ts_code_policy', 'tsconfig_contract', 'build_contract', \
                       'declarations_contract', 'verifier_vocabulary') \
           if k not in contract]; \
sys.exit(f'the contract is missing {missing}') if missing else None; \
floor = contract['ts_code_policy'].get('min_ts_logic_lines'); \
sys.exit('the contract states no min_ts_logic_lines, so the closure module\\'s \
line-count check has no floor to compare against') if not floor else None; \
forbidden = contract['forbidden_paths'].get('extensions') or []; \
sys.exit('the contract forbids a JavaScript extension. dist/ is emitted \
JavaScript by design on this task and tools/build.js may stay JavaScript, so \
such a list would forbid the build\\'s own output') \
    if {'.js', '.mjs', '.cjs'} & set(forbidden) else None; \
print('source-contract.json ok:', contract['schema_version'], 'floor', floor)"

# The counter is one function, and this asserts it.
#
# `source_tree_facts` states a logic-line count per State A file and a total of
# 6,376 under src/, and `ts_code_policy.min_ts_logic_lines` is measured against a
# submission by `srbscan.count_ts_logic_lines`.  If those are two different
# counters, the floor means something other than what the contract says it means --
# and the direction of the error is the dangerous one: a counter that mis-handles a
# block comment swallows arbitrary spans of a submission's TypeScript and reports a
# port as a stub.  The check is only possible where State A is present, so it runs
# against the mount when there is one and prints that it was skipped when there is
# not, rather than passing silently.
RUN python -c "\
import json, pathlib, sys; \
sys.path.insert(0, '/tests/audit/lib'); \
contract = json.loads(pathlib.Path( \
    '/tests/audit/data/source-contract.json').read_text()); \
rows = [r for r in contract['source_tree_facts']['files'] \
        if r.get('logic_lines') is not None]; \
sys.exit('the contract states no per-file logic_lines to check the counter against') \
    if not rows else None; \
import srbscan; \
sys.exit('srbscan exposes no count_ts_logic_lines') \
    if not hasattr(srbscan, 'count_ts_logic_lines') else None; \
root = pathlib.Path('/opt/original'); \
present = [r for r in rows if (root / r['path']).is_file()]; \
print(f'counter identity: no State A mounted at build time, ' \
      f'{len(rows)} row(s) unchecked here -- tests/check-task.py checks them') \
    if not present else None; \
bad = [(r['path'], r['logic_lines'], \
        srbscan.count_ts_logic_lines([root / r['path']])) for r in present]; \
bad = [b for b in bad if b[1] != b[2]]; \
sys.exit(f'the contract and srbscan disagree on logic lines: {bad}') if bad else None; \
print(f'counter identity ok on {len(present)} file(s)') if present else None"

# Neither runtime is here, and that is asserted rather than assumed.  The claim in
# the header -- that a scan module cannot build or run the submission -- is only
# true while it stays true, and `apt-get install ripgrep` pulling something in as a
# recommendation is exactly the kind of change nobody would notice.
#
# `node` heads this list and on this task it is the one that matters.  The base
# image does not carry it, but a future edit adding a JavaScript linter to this
# stage would, and both trees here are JavaScript or compile to it: a scan module
# with `node` could `require` State A's evaluator directly, or run the
# submission's `dist/probe.js`.  The rest of the list is the same one every stage-1
# image in this benchmark checks, kept identical so a shared base image change is
# caught by whichever task rebuilds first.
RUN for prog in node nodejs npm npx pnpm yarn tsc ts-node deno bun \
                dotnet mono csc msbuild cc gcc make; do \
        if command -v "$prog" >/dev/null 2>&1; then \
            echo "$prog is on PATH in the stage 1 image; a scan module could build \
or run the submission, and this stage's whole claim is that it cannot" >&2; \
            exit 1; \
        fi; \
    done; \
    echo "toolchain absent: no node, npm, tsc, deno, bun, dotnet or C compiler"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and no findings is indistinguishable from a clean tree in
# the rendered prompt -- the reviewer would be told nothing and have no way to know
# it was told nothing.  Collection runs against the empty mount points, which is
# enough to execute every import and every parametrize call.
#
# The floor is 78 against a measured count of 84 (closure 31, contract 36,
# declarations 10, provenance 7), and the number is chosen against the failure it
# has to catch rather than as round headroom.  Losing the smallest module takes the
# total to 77, so 78 is the largest floor that no legitimate removal trips and the
# smallest one that catches a module going quiet.  The count does not move when the
# mounts are empty -- verified, not assumed: every parametrized list comes from the
# contract in data/ rather than from walking a tree, which is why this floor can be
# exact rather than generous.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 78

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
