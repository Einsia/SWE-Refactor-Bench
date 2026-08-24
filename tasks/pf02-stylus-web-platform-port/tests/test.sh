#!/bin/sh
# Harbor's verifier entry point.  The name is fixed: in `environment_mode =
# "separate"` Harbor passes skip_tests_upload, which means it does not discover
# a test script -- it execs /tests/test.sh in the verifier image and reads
# /logs/verifier/reward.json afterwards.
#
# Everything below is `swerefactor ladder`, which drives the three stage images as
# sibling containers over the socket that tests/docker-compose.yaml mounts, and
# then applies the published ladder to what they wrote.  Identical in all twenty
# tasks: what differs between tasks is tests/evaluation.toml, which the driver
# reads.  Run the same thing by hand with
#
#     docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \
#         -v "$PWD/repo:/workspace/repo" swerefactor/pf02-verifier:1
#
# `--task-dir /` because the verifier image holds this task at its root:
# /tests/evaluation.toml, and /tests/<context> for each stage.
set -eu

exec python3 -m swerefactor ladder --task-dir / --repo /workspace/repo
