# syntax=docker/dockerfile:1
# =============================================================================
# SWERefactorBench -- lang05-goyaml-go-to-zig
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/lang05-audit:1 \
#       tasks/lang05-goyaml-go-to-zig/tests/audit
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
# There is deliberately no Zig here, and no Go.  This stage answers ten questions
# -- did the Go leave, is the Zig the implementation, does anything here recognise
# the grader -- and every one of them is settled by reading source and build files.
# Give the review a compiler and it starts grading build logs, which is stage 2's
# job in an image built to do it: stage 2 runs `zig build` on the submission and
# puts over 8,000 differential cases through the executable it produces, against a
# reference frozen before the submission existed.  Those are measurements.  This
# stage is for the judgement calls no amount of build output settles, chiefly
# whether 7,609 lines of Go were reimplemented or routed around.
#
# The absent Go toolchain is worth a second sentence, because it is a different
# argument from stage 2's.  Stage 2 deletes Go so that a *submission* cannot shell
# out to the reference during grading.  Stage 1 never executes the submission at
# all, so that risk is not the reason; the reason is that with no Go and no PyYAML
# in this image, nothing here can parse a YAML document, so no scan module can
# quietly become a second opinion on the library's behaviour.  Behaviour is stage
# 2's question and it is answered there against a frozen reference.
#
# There is a pytest, and it is not a contradiction of the paragraph above.  It runs
# `scan.toml`: 103 file-reading checks whose output is substituted into the prompt
# as findings for the reviewer to follow up.  They walk directories, hash files and
# read the first four bytes of one to see whether it is an object file.  There is
# no `zig` here that could satisfy a module which tried to build something, which
# is the property that keeps a scan a scan.
#
# `allowed_commands` is unset in evaluation.toml, so this task's review is by
# reading only: the tools the runner exposes are list_dir, fetch_source, search and
# diff.  ripgrep, diffutils and tree back those; the rest is for a human reading a
# transcript afterwards.
#
# Network: the model API and nothing else.  It is the one stage that is not
# offline, which is the other half of why it gets no toolchain -- there is no point
# in this container where submitted code executes.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1
FROM ${INFRA_IMAGE} AS infra

# python:3.11-slim.  The digest is the same one lang01's stage 1 pins, which makes
# the two tasks' reviews run on one interpreter; `python --version` is asserted
# below rather than trusted, because the version is written into scan.toml's
# metadata and a base that moved would leave that metadata describing nothing.
# Note the image provides `python` and not `python3`, which is why lib/run-scan.sh
# resolves both names instead of hard-coding one.
FROM python@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    TZ=UTC

RUN python --version | cut -d' ' -f2 | grep -Fx '3.11.15'

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
# test into one check, so the findings arrive keyed by node id.  Pinned by hash like
# everything else in this benchmark: a scan whose collection count moved between
# builds would change what the reviewer is told without anyone editing a file.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# Asserted, because "no toolchain" is a claim this file makes in prose three times
# and prose is not a check.  `zig` and `go` are the two that would change what this
# stage is; the rest are the names a distribution package might install them under.
RUN for tool in zig go gofmt cc gcc clang cmake make ninja; do \
      if command -v "$tool" >/dev/null 2>&1; then \
        echo "stage 1 has a toolchain it should not: $tool at $(command -v "$tool")" >&2; \
        exit 1; \
      fi; \
    done \
 && python -c "import importlib.util, sys; \
sys.exit('a YAML parser is importable here; a scan module could become a second \
opinion on behaviour') if importlib.util.find_spec('yaml') else None" \
 && echo "no toolchain and no YAML parser in the review image"

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

# scan.toml is parsed by the loader the harness will use, so a duplicate module id,
# a weight that does not parse, or a module directory that does not exist fails the
# build rather than the run.  There is no driver.py --self-check to pair this with:
# a scan module is a pytest and its equivalent is the collection below.
RUN chmod +x /tests/audit/lib/*.sh \
 && for d in /tests/audit/modules/*/; do \
      test -x "$d/run.sh" || { echo "no runner in $d" >&2; exit 1; }; \
    done \
 && python -c "\
import sys; from swerefactor import config; \
s = config.Suite.load('/tests/audit/scan.toml', schema=config.SCAN_SCHEMA); \
missing = [m.id for m in s.modules if not (s.root / m.dir).is_dir()]; \
sys.exit(f'scan.toml names missing module dir(s): {missing}') if missing else None; \
print(f'scan: {len(s.modules)} modules, weight {s.weight_total:g}')"

# The scan has to collect, here, at build time.  A scan that raises on import
# produces no findings, and in the rendered prompt no findings is nearly
# indistinguishable from a clean tree -- `scan.digest([])` returns "(the scan
# produced no findings)" where a clean run would have said "103 found nothing".
# The reviewer's prompt now explains that difference, but explaining it is the
# second line of defence; this is the first.
#
# Collection runs against the empty mount points, which is enough to execute every
# import and every parametrize call.  The floor is 80 and the measured count there
# is 85: below the 103 a real pair of trees yields, because the lists parametrized
# off /opt/original collapse to their fallbacks when the mount is empty.  Both
# numbers are measured, not intended -- a floor written from an estimate is a floor
# that either never fires or fires on every honest edit.
RUN mkdir -p /opt/original /opt/workspace \
 && bash /tests/audit/lib/collect-check.sh 80

# No bytecode: this RUN is the last thing that imports the scan, so .pyc files
# written now would be the only mutable bytes in the image outside the mounts.
# PYTHONDONTWRITEBYTECODE is set in the ENV above, so this is belt and braces
# against a pip or an apt hook having written some anyway -- and it runs after the
# collection above rather than before, because that step is allowed to produce the
# bytecode it is checking.
# The assertion names the same roots as the sweep.  It named one of the two, so a
# sweep that stopped working on /opt/swerefactor would have left bytecode behind and
# passed; stage 2 shipped twenty such files under exactly that root, caught by
# looking in the built image rather than by any of these lines.
RUN find /tests/audit /opt/swerefactor -name __pycache__ -type d -prune \
      -exec rm -rf {} + \
 && test -z "$(find /tests/audit /opt/swerefactor -name '*.pyc' -print -quit)"

# The two mount points exist in the image so a misconfigured run fails with an
# empty directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
