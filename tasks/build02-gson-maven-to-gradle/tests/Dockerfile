# =============================================================================
# SWERefactorBench -- build02, the Harbor verifier image.
#
# This is the container Harbor starts to grade a trial, and it is not a stage.
# It holds no toolchain, builds nothing, and never runs the submission: it
# drives the three stage images as sibling containers over a mounted Docker
# socket and applies the ladder to what they wrote.  See infra/swerefactor/
# ladder.py for why the three stay separate -- stage 2's isolation is an
# assertion made at its own build time, which one merged image could not make.
#
# `environment_mode = "separate"` in task.toml puts Harbor's verifier here, with
# tests/ as the build context, and passes skip_tests_upload -- so /tests/test.sh
# has to exist in the image under exactly that name.
#
# Built by Harbor on demand.  To build it by hand:
#
#     docker build -t swerefactor/build02-verifier:1 tasks/build02-gson-maven-to-gradle/tests/
#
# The socket is the one thing to be deliberate about.  A Docker socket is root
# on the host, and this image has one so that it can start the stage containers.
# What it does with that reach is start three images from this repository; what
# it never does is run submitted code, which runs where it ran before -- inside a
# stage container, with no socket of its own, and for stage 2 with no network.
# =============================================================================

ARG INFRA_IMAGE=swerefactor/infra:1

# The Docker CLI only: a client, not a daemon.  Pinned by digest for the same
# reason every other image here is -- `docker:28.3.2-cli` is a moving tag, and a
# verifier that changed underneath a published score would make the score
# unreproducible.  The binary is static, so it needs nothing else from Alpine.
FROM docker@sha256:a07d3db92e0ca51de11fd47c9b45257a7c3d64a5b6dae2393382a86a100f42a5 AS cli

FROM ${INFRA_IMAGE}

COPY --from=cli /usr/local/bin/docker /usr/local/bin/docker

# The task's own orchestration, and nothing else from tests/: the stage images
# already carry their own contexts, and the largest of them is a State A tarball
# that has no reader here.
COPY evaluation.toml /tests/evaluation.toml
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh

# Where the driver writes, and where Harbor bind-mounts the trial's verifier
# directory.  /workspace/repo is the submission's own path: Harbor's artifact
# collection re-materializes an artifact at its original source, so a submission
# collected from /workspace/repo arrives here at /workspace/repo.
RUN mkdir -p /logs/verifier /workspace

# A verifier that cannot reach its own driver is a trial that fails after the
# agent phase has already been paid for, so the parts are checked at build time:
# the CLI runs, the driver imports, and this task's evaluation.toml parses.
RUN docker --version \
 && python3 -c "\
from swerefactor import ladder, config; \
e = config.Evaluation.load('/tests/evaluation.toml'); \
missing = sorted(set(e.stages) - set(ladder.REPO_IN)); \
assert not missing, 'no repo path for stage(s): %s' % missing; \
print('build02 verifier ok: %s, stages %s' % (e.task, ' '.join(sorted(e.stages))))"

WORKDIR /tests
CMD ["/tests/test.sh"]
