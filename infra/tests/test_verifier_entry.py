"""The Harbor verifier entry point, across all twenty tasks.

Harbor's separate-verifier contract has one hard requirement and no discovery:
it execs ``/tests/test.sh`` in the verifier image by that exact name, and reads
``/logs/verifier/reward.json`` afterwards.  A task missing that file does not
fail visibly at authoring time -- it fails after the agent phase has been paid
for, which is the most expensive place to find out.

``test_layout.py`` globs ``tests/*/Dockerfile``, one level down, so the verifier
image at ``tests/Dockerfile`` is invisible to it.  These checks cover that file
and the two beside it.

The substantive one is ``test_unpack_reproduces_every_environment_recipe``: the
driver derives State A's shape from the archive rather than from a per-task
setting, and this asserts that derivation against all twenty real tarballs and
the twenty environment Dockerfiles that are the existing answer.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import config, ladder

REPO = Path(__file__).resolve().parents[2]
TASKS = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir()) \
    if (REPO / "tasks").is_dir() else []
IDS = [p.name for p in TASKS]


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_verifier_entry_point_exists_and_is_executable(task: Path) -> None:
    """The one filename Harbor does not discover."""
    script = task / "tests" / "test.sh"
    assert script.is_file(), (
        f"{task.name} has no tests/test.sh, so `harbor run` reaches the verifier "
        f"and finds nothing to exec")
    assert os.access(script, os.X_OK), f"{script} is not executable"
    body = script.read_text()
    assert "swerefactor ladder" in body, (
        f"{script} does not call the ladder driver")
    assert "--task-dir /" in body, (
        f"{script} must pass --task-dir /: the verifier image holds the task at "
        f"its root, as /tests/evaluation.toml")


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_verifier_image_copies_the_entry_point(task: Path) -> None:
    """The script has to be *in the image*: separate mode uploads no tests."""
    dockerfile = task / "tests" / "Dockerfile"
    assert dockerfile.is_file(), (
        f"{task.name} has no tests/Dockerfile, so Harbor has nothing to build "
        f"the verifier from")
    body = dockerfile.read_text()
    assert re.search(r"^COPY test\.sh /tests/test\.sh", body, re.M), body[:400]
    assert re.search(r"^COPY evaluation\.toml /tests/evaluation\.toml", body, re.M)
    assert "chmod +x /tests/test.sh" in body
    assert "/usr/local/bin/docker" in body, (
        "the verifier drives sibling containers, so it needs a docker client")
    assert re.search(r"^FROM docker@sha256:[0-9a-f]{64} AS cli", body, re.M), (
        "the docker CLI must be pinned by digest, not by a moving tag")


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_compose_mounts_the_socket_and_state_a(task: Path) -> None:
    compose = task / "tests" / "docker-compose.yaml"
    assert compose.is_file(), f"{task.name} has no tests/docker-compose.yaml"
    body = compose.read_text()
    assert "/var/run/docker.sock" in body
    assert "SRB_DOCKER_SOCKET" in body, (
        "a rootless daemon's socket is not at the default path; the override "
        "has to exist or the verifier cannot run there")
    assert "../environment/original.tar.gz:/environment/original.tar.gz:ro" in body, (
        "State A must be mounted where the driver looks for it -- "
        "<task-dir>/environment/original.tar.gz, read-only")
    # Uncommented lines only.
    keys = [ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    assert not any(k.startswith("build:") for k in keys), (
        "Harbor's own compose defines main's build; redefining it here would "
        "fight it rather than override a scalar")


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_every_declared_stage_has_a_repo_path(task: Path) -> None:
    """A stage the driver has no path for would be handed nothing to grade.

    Not hypothetical: the three images do not agree on where the submission
    goes, and each creates only its own mount point.  Stage 1 documents
    ``/opt/workspace``; stages 2 and 3 rebuild in ``/workspace/repo``.
    """
    evaluation = config.Evaluation.load(task / "tests" / "evaluation.toml")
    missing = sorted(set(evaluation.stages) - set(ladder.REPO_IN))
    assert not missing, (
        f"{task.name} declares stage(s) {missing} that ladder.REPO_IN has no "
        f"path for")


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_repo_path_is_creatable_in_each_stage_image(task: Path) -> None:
    """The path the driver writes to must be one the image can receive.

    ``docker cp -`` creates missing parents, so what matters is that the path is
    absolute and not a mount the image declares as a volume -- a VOLUME would
    discard what is copied in before the container starts.
    """
    evaluation = config.Evaluation.load(task / "tests" / "evaluation.toml")
    for name, stage in sorted(evaluation.stages.items()):
        target = ladder.REPO_IN[name]
        assert target.startswith("/"), target
        context = stage.options.get("context")
        if not context:
            continue
        dockerfile = task / "tests" / str(context) / "Dockerfile"
        if not dockerfile.is_file():
            continue
        for line in dockerfile.read_text().splitlines():
            if line.startswith("VOLUME") and target in line:
                pytest.fail(f"{dockerfile} declares VOLUME over {target}, so a "
                            f"submission copied there would be discarded")


def _archive_strip(tarball: Path) -> int:
    """What the driver will decide for this archive."""
    with tarfile.open(tarball) as tf:
        roots = {
            [p for p in m.name.split("/") if p not in ("", ".")][0]
            for m in tf.getmembers()
            if [p for p in m.name.split("/") if p not in ("", ".")]
        }
    return 1 if len(roots) == 1 else 0


def _declared_repo_paths(task: Path) -> list[str]:
    """Paths the environment Dockerfile states are at State A's repo root.

    Each of the twenty asserts its own unpack by naming files it expects at
    /workspace/repo -- ``test -f /workspace/repo/CMakeLists.txt`` and friends.
    Those names are the recipe's own statement of where the root is, which makes
    them the oracle for whether this driver put it in the same place.
    """
    dockerfile = task / "environment" / "Dockerfile"
    if not dockerfile.is_file():
        return []
    found = re.findall(r"/workspace/repo/([A-Za-z0-9._/-]+)",
                       dockerfile.read_text())
    return sorted(set(found))


@pytest.mark.parametrize("task", TASKS, ids=IDS)
def test_unpack_reproduces_every_environment_recipe(task: Path) -> None:
    """One derived rule must land the root where twenty recipes put it.

    The alternative was a per-task ``strip`` setting: twenty chances for a
    task's declared shape to drift from the archive it describes, and the
    failure would be silent.  State A unpacked one level off is a State A that
    exists, is non-empty, and matches nothing -- so stage 1 reviews a submission
    against a tree with no files in common and reports every one as added.

    Compared against the archive rather than against a directory, so this costs
    no unpacking: a path the recipe expects at the root must sit at exactly that
    place in the archive once the driver's own strip is applied.
    """
    tarball = task / "environment" / "original.tar.gz"
    if not tarball.is_file():
        pytest.skip("no State A archive")
    declared = _declared_repo_paths(task)
    if not declared:
        pytest.skip("environment Dockerfile names no path at the repo root")

    strip = _archive_strip(tarball)
    with tarfile.open(tarball) as tf:
        members = set()
        for m in tf.getnames():
            parts = [p for p in m.split("/") if p not in ("", ".")]
            if len(parts) > strip:
                members.add("/".join(parts[strip:]))

    # Some declared paths are created after the unpack -- a baseline .gitignore
    # copied in, a symlink built over the tree -- so absence from the archive is
    # not a disagreement.  A path that *is* in the archive must be at the depth
    # the recipe states.
    checked = [p for p in declared if p in members]
    misplaced = [p for p in declared
                 if p not in members
                 and any(n.endswith("/" + p) for n in members)]
    assert not misplaced, (
        f"{task.name}: the driver strips {strip} component(s), which leaves "
        f"{misplaced} below the root that environment/Dockerfile puts them at")
    if not checked:
        pytest.skip("no declared path is present in the archive itself")


def test_unpack_puts_the_repo_root_at_the_target(tmp_path: Path) -> None:
    """Both shapes land the same way: `into` *is* the repository root."""
    for prefix in ("repo/", ""):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name in ("CMakeLists.txt", "src/main.c"):
                data = b"x\n"
                info = tarfile.TarInfo(prefix + name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        tarball = tmp_path / f"t{len(prefix)}.tar.gz"
        tarball.write_bytes(buf.getvalue())
        into = tmp_path / f"out{len(prefix)}"
        ladder.unpack_original(tarball, into)
        assert (into / "CMakeLists.txt").is_file(), sorted(
            p.name for p in into.iterdir())
        assert (into / "src" / "main.c").is_file()


def test_offline_stage_is_given_no_gateway(monkeypatch) -> None:
    """Stage 2's isolation is graded, so it gets no key and no network."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "probe")
    monkeypatch.setenv("OPENAI_API_KEY", "probe")
    assert ladder._stage_env(ladder.OFFLINE) == []
    assert ladder._network_for(ladder.OFFLINE) == "none"
    monkeypatch.setenv("SRB_STAGE_NETWORK", "hostnet")
    assert ladder._network_for(ladder.OFFLINE) == "none", (
        "stage 2 offline is not configurable: a run that let it reach a network "
        "would measure something other than the migration")
    assert ladder._network_for("audit") == "hostnet"


def test_forwarded_keys_never_enter_a_command_line(monkeypatch) -> None:
    """``-e VAR``, not ``-e VAR=value``: ``ps`` can read an argument list."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    monkeypatch.setenv("SRB_FORWARD_ENV", "MY_OWN_KEY")
    monkeypatch.setenv("MY_OWN_KEY", "also-secret")
    flags = ladder._stage_env("audit")
    assert "ANTHROPIC_API_KEY" in flags
    assert "MY_OWN_KEY" in flags, "SRB_FORWARD_ENV is the escape hatch for a " \
                                  "task naming its own api_key_env"
    for flag in flags:
        assert "secret" not in flag, f"a value reached argv: {flag}"
        assert "=" not in flag, f"a value reached argv: {flag}"


def test_unset_variables_are_not_forwarded(monkeypatch) -> None:
    """``-e VAR`` for an unset VAR would define it empty in the container.

    An empty ``ANTHROPIC_BASE_URL`` is not the same as an absent one: the driver
    would be overriding the stage's own default with nothing.
    """
    for var in ladder.GATEWAY_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("SRB_FORWARD_ENV", raising=False)
    assert ladder._stage_env("audit") == []
