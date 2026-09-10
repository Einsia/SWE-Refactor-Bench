# =============================================================================
# SWERefactorBench -- build02-gson-maven-to-gradle
# Stage 1 image: the audit review.
#
#     docker build -t swerefactor/build02-audit:1 \
#         tasks/build02-gson-maven-to-gradle/tests/audit
#
# A reviewer's workbench. It holds a model client, tools for reading code, and two
# trees:
#
#     /opt/original      State A, unpacked from original.tar.gz    (read-only)
#     /opt/workspace     the submission, as collected                  (a copy)
#
# Both are runtime mounts rather than image layers, because the same image reviews
# every submission for this task. The original is mounted read-only and the
# workspace is a copy: a review must not be able to change what stage 2 later
# measures, and it must not be able to change the thing it is comparing against.
#
# There is deliberately neither Gradle nor Maven nor a JDK here. This stage answers
# six questions -- did Maven leave, does Gradle describe the build or drive something
# else, were the 241 sources left alone, are the four artifacts built from those
# sources, are the computed values computed or pasted, does the build recognise the
# grader -- and every one of them is settled by reading build code against State A's
# build code. Give the review a Gradle and it starts building and grading the output,
# which is stage 2's job in an image built to do it: stage 2 builds the submission
# seven ways, compares 282 jar entries and 271 class files against the frozen
# inventory, and runs 1,328 tests. Those are measurements. This stage is for the
# judgement no build log settles -- whether bnd was configured or its output was
# transcribed.
#
# The asymmetry runs the other way too, and the three images together are the reason
# the split holds: this stage has no build system at all, stage 2 has Gradle and
# deliberately no other engine (its Dockerfile fails the build if any of fifteen
# names is on PATH, Maven included), and stage 3 is the only image that can drive
# both Maven and Gradle, because "the Maven build passes this test" is a claim an
# adversary has to be able to execute.
#
# `allowed_commands = []` in evaluation.toml means this task's review is by reading
# only; the tools the runner exposes are list_dir, fetch_source, search and diff.
# ripgrep, diffutils and tree back those; the rest is for a human reading a
# transcript afterwards. xmllint is here because State A's build system is five POMs
# and "show me this pom with its inheritance visible" is a reading aid, not a build.
#
# Network: the model API and nothing else. It is the one stage that is not offline,
# which is the other half of why it gets no toolchain -- there is no point in this
# container where submitted code executes.
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
        libxml2-utils \
        ripgrep \
        tree \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# --- The harness -------------------------------------------------------------
# swerefactor.models talks to the API over urllib, so there is no vendor SDK here and
# nothing to drift. The credential arrives as an environment variable at run time
# and is never baked into a layer.
COPY --from=infra /opt/swerefactor /opt/swerefactor
ENV PYTHONPATH=/opt/swerefactor

# --- The review --------------------------------------------------------------
COPY . /tests/audit

# The prompt is this task's most load-bearing file: it is what replaced the regex
# gate list. Check at build time that it is present, that it is prose rather than a
# stub, and that its placeholders are ones the runner fills -- an unknown placeholder
# survives into the transcript verbatim, which is visible but late.
#
# The last two assertions are this task's, and they are about the scan rather than
# the gates. The findings are advisory, and the only thing that makes them read as
# advisory to the review is the prompt saying so. A prompt that renders `{{findings}}`
# without framing them is a prompt that invites a reviewer to copy a regex's opinion
# into a verdict, which is the failure mode this whole stage was rebuilt to remove.
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
report to nobody') if 'findings' not in found else None; \
sys.exit('prompt.txt renders the scan findings without calling them leads or \
advisory, so nothing tells the review they are not verdicts') \
if not any(w in text for w in ('lead', 'advisory')) else None; \
print(f'prompt.txt ok: {len(text)} chars, placeholders {sorted(found)}')"

# --- The read-only scan ------------------------------------------------------
# pytest is here and a toolchain is not, which is not the contradiction it looks
# like. The scan is three pytest modules that hash files and match strings; they
# import nothing from the submission and could not -- there is no Gradle and no JDK in
# this image, so a module that tried to build anything would die on a missing binary.
# What pytest buys is the module contract: a scan module and a stage-2 module are
# byte-for-byte the same shape, so one can move between the stages without a rewrite,
# and `swerefactor.pytest_module` turns each collected test into one check keyed by
# nodeid without a parallel runner having to exist.
#
# Pinned by hash, from the same versions stage 2 pins its pytest with, so the two
# stages cannot drift into different collection behaviour.
COPY requirements-scan.txt /opt/requirements-scan.txt
RUN pip install --require-hashes --no-deps -r /opt/requirements-scan.txt

# Note what this image does *not* carry: a copy of State A's checksums. Stage 2 grades
# against frozen `data/*.json`, because by the time it runs it holds eight Gradle build
# trees and no reference tree. This stage has the reference tree mounted at
# /opt/original, so the scan hashes it directly. One fewer duplicated input, no
# manifest that can be regenerated alone and drift, and a stronger claim: the
# comparison is against the same State A the reviewer is reading, not against a file
# asserting what State A used to contain.
#
# The scan has to collect, here, at build time. A scan that raises on import produces
# no findings, and no findings is indistinguishable from a clean tree in the rendered
# prompt -- the reviewer would be told nothing and have no way to know it was told
# nothing.
#
# The collection floor is 40 because of the decision above: with no reference tree
# mounted the source comparison contributes its six unparametrized checks and one
# placeholder rather than 241, so the build-time count is 45 and the run-time count is
# 285. That gap is exactly the risk in deriving a parametrize list from a mount, so
# collect-check.sh's second phase points SRB_ORIGINAL at two synthetic trees of known
# and different shape and asserts the count moves by the difference between them. A
# module that stopped deriving from the mount -- and would therefore report a clean
# tree against a missing one -- fails this layer.
RUN mkdir -p /opt/original /opt/workspace \
    && bash /tests/audit/lib/collect-check.sh 40

# A build-system review that cannot read the build system it is comparing against has
# nothing to do, so assert here that no toolchain leaked in through a base image
# update or a careless apt line. `unzip` is not on this list on purpose: a reviewer
# may want to look inside an archive, which is reading.
RUN for forbidden in mvn mvnd mvnw ant gradle gradlew ivy groovy groovyc kotlinc \
                     javac java jar javap make gmake cmake ninja bazel sbt; do \
        if command -v "$forbidden" >/dev/null 2>&1; then \
            echo "FATAL: the stage-1 image must not ship $forbidden -- a review" >&2; \
            echo "that can build starts grading build logs, which is stage 2." >&2; \
            exit 1; \
        fi; \
    done

# The compiled scan modules would otherwise ship as a layer that differs per build.
RUN find /tests/audit -name __pycache__ -prune -exec rm -rf {} + || true

# The two mount points exist in the image so a misconfigured run fails with an empty
# directory rather than a confusing mkdir error mid-review.
RUN mkdir -p /opt/original /opt/workspace /logs/verifier

WORKDIR /tests/audit
