"""The two checks that keep every task's stage directories the same shape.

Both exist because of a real drift.  Four tasks had grown four different answers
to "where does a stage keep its inputs" -- ``data/``, ``reference/``, a bare
tarball in the stage root -- and only three of twelve stage directories had a
``.dockerignore``, which meant nine graded images could carry whatever a local
test run had left behind.

The properties worth testing are the ones a reviewer cannot check by eye:
duplicated inputs stay byte-identical, and the layout rules fire on the shapes
that actually went wrong rather than only on the ones easy to construct.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import config, tomlcompat
from swerefactor.cli import (_copy_sources, _instruction_arithmetic,
                          _recorded_digest_drift, _shared_input_drift,
                          _stage_layout)

REPO = Path(__file__).resolve().parents[2]
TASKS = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir()) \
    if (REPO / "tasks").is_dir() else []

EVALUATION = """\
schema = "swerefactor.evaluation/1"
task = "t"

[stages.audit]
enabled = true
image = "swerefactor/t-audit:1"
context = "audit"

[stages.behavioural]
enabled = true
image = "swerefactor/t-behavioural:1"
context = "behavioural"
"""


def make_task(root: Path) -> Path:
    """A minimal task tree with two stages, both laid out correctly."""
    (root / "environment").mkdir(parents=True)
    (root / "tests" / "behavioural" / "data").mkdir(parents=True)
    (root / "tests" / "audit").mkdir(parents=True)
    (root / "environment" / "Dockerfile").write_text("FROM scratch\n")
    for stage in ("behavioural", "audit"):
        (root / "tests" / stage / "Dockerfile").write_text("FROM scratch\n")
        (root / "tests" / stage / ".dockerignore").write_text("__pycache__/\n")
    (root / "tests" / "evaluation.toml").write_text(EVALUATION)
    return root


def evaluation_for(root: Path) -> config.Evaluation:
    return config.Evaluation.load(root / "tests" / "evaluation.toml")


def state_a_paths(tarball: Path) -> list[str]:
    """The files State A ships, as repository-relative POSIX paths.

    The corpus does not agree on how ``original.tar.gz`` is shaped, so the
    wrapper directory has to be derived rather than assumed.  Three shapes are
    in the tree: seventeen tasks wrap everything in one directory (``repo/``,
    but also ``graphhopper/`` and ``jsonnet-0.20.0/``), build02 wraps in ``./``,
    and build01 and lang03 are flat with files at the archive root.  The
    Dockerfiles differ to match -- some extract with ``--strip-components=1``,
    some into ``/workspace`` and some into ``/workspace/repo``.

    A single top-level entry is a wrapper exactly when no file sits beside it at
    the archive root; a repository root always has *some* file in it.  That rule
    was checked against every Dockerfile's own ``test -f /workspace/repo/<x>``
    assertions after extraction: 16 of the 20 tasks make at least one such
    assertion, 44 markers in all, and every one of them resolves under the
    derived prefix.  The other four assert nothing to check against.
    """
    with tarfile.open(tarball) as tf:
        members = [m.name for m in tf.getmembers() if m.isfile()]
    assert members, f"{tarball}: lists no files"
    tops = {m.split("/", 1)[0] for m in members}
    at_root = any("/" not in m for m in members)
    prefix = f"{tops.copy().pop()}/" if (len(tops) == 1 and not at_root) else ""
    delivered = sorted(m[len(prefix):] for m in members if m.startswith(prefix))
    assert len(delivered) == len(members), f"{tarball}: prefix {prefix!r} lost members"
    assert any("/" not in d for d in delivered), (
        f"{tarball}: prefix {prefix!r} leaves no file at the repository root, so "
        f"it stripped one level too many")
    return delivered


def exclusion_casualties(artifacts: list[dict], delivered: list[str]) -> list[str]:
    """Which reference files each exclude pattern would delete, both semantics."""
    hits = []
    for art in artifacts:
        for pattern in art.get("exclude") or []:
            for rel in delivered:
                # A directory pattern takes the subtree with it, so test every
                # ancestor of the file too, not only the file's own path.
                parts = PurePosixPath(rel).parts
                candidates = {rel, PurePosixPath(rel).name}
                candidates |= {"/".join(parts[:i]) for i in range(1, len(parts))}
                candidates |= set(parts[:-1])
                if any(fnmatch.fnmatch(c, pattern) for c in candidates):
                    hits.append(f"{pattern!r} deletes {rel}")
    return sorted(set(hits))


# --------------------------------------------------------------------------- #
# Recorded digests must not drift
#
# Four spellings record the tarball's digest across the benchmark, and every one
# of them is written by hand: `tools/build_repo_snapshot.py` prints the new digest
# and updates none of the files that hold it.  A check that reads three of the
# four is worse than useless, because it reports coverage it does not have -- so
# each spelling gets a case that corrupts exactly one fact and requires the
# corruption to be named.  The size and file-count facts are here for the same
# reason: they are re-pinned by the same hand at the same time.
# --------------------------------------------------------------------------- #

def digest_task(root: Path, dockerfile: str, *, size: int | None = None) -> Path:
    """A task whose environment holds one tarball and one recorded digest."""
    make_task(root)
    env = root / "environment"
    tarball = env / "original.tar.gz"
    with tarfile.open(tarball, "w:gz") as tf:
        for name in ("repo/README.md", "repo/src/main.c"):
            data = f"// {name}\n".encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    (env / "original.sha256").write_text(f"{digest}  original.tar.gz\n")
    (env / "Dockerfile").write_text(
        dockerfile.replace("@DIGEST@", digest)
                  .replace("@SIZE@", str(size if size is not None
                                         else tarball.stat().st_size)))
    return root


PIPED = """\
FROM scratch
COPY original.tar.gz /run/state-a/original.tar.gz
RUN set -eux; \\
    echo '@DIGEST@  /run/state-a/original.tar.gz' \\
      | sha256sum -c -; \\
    test "$(stat -c '%s' /run/state-a/original.tar.gz)" = "@SIZE@"
"""

VIA_ARG = """\
FROM scratch
ARG REPO_SHA256=@DIGEST@
ARG REPO_SIZE=@SIZE@
COPY original.tar.gz /opt/swerefactor/repo.tar.gz
RUN echo "${REPO_SHA256}  /opt/swerefactor/repo.tar.gz" | sha256sum -c - \\
 && test "$(stat -c '%s' /opt/swerefactor/repo.tar.gz)" = "${REPO_SIZE}"
"""

SPLIT = """\
FROM scratch
ARG REPO_SHA256=@DIGEST@
COPY original.tar.gz /tmp/original.tar.gz
RUN set -eu; \\
    actual="$(sha256sum /tmp/original.tar.gz | cut -d' ' -f1)"; \\
    [ "$actual" = "$REPO_SHA256" ] || exit 1
"""

MULTI_SOURCE = """\
FROM scratch
ARG REPO_SHA256=@DIGEST@
WORKDIR /run/state-a
COPY original.tar.gz original.sha256 ./
RUN echo "${REPO_SHA256}  original.tar.gz" | sha256sum -c -
"""


@pytest.mark.parametrize("shape,dockerfile", [
    ("a literal piped to sha256sum -c, spanning two lines", PIPED),
    ("an ARG expansion piped to sha256sum -c", VIA_ARG),
    ("sha256sum into a shell variable, compared to an ARG", SPLIT),
    ("a relative path from a multi-source COPY", MULTI_SOURCE),
])
def test_a_correct_digest_passes(tmp_path, shape, dockerfile):
    root = digest_task(tmp_path / "t", dockerfile)
    notes: list[str] = []
    assert _recorded_digest_drift(root, root / "environment", notes) == [], shape
    assert notes, f"{shape}: checked nothing, so it proves nothing"


@pytest.mark.parametrize("shape,dockerfile", [
    ("a literal piped to sha256sum -c, spanning two lines", PIPED),
    ("an ARG expansion piped to sha256sum -c", VIA_ARG),
    ("sha256sum into a shell variable, compared to an ARG", SPLIT),
    ("a relative path from a multi-source COPY", MULTI_SOURCE),
])
def test_a_wrong_digest_is_caught(tmp_path, shape, dockerfile):
    root = digest_task(tmp_path / "t", dockerfile)
    env = root / "environment"
    real = hashlib.sha256(
        (env / "original.tar.gz").read_bytes()).hexdigest()
    df = env / "Dockerfile"
    df.write_text(df.read_text().replace(real, "deadbeef" * 8))
    problems = _recorded_digest_drift(root, env, [])
    assert problems, f"{shape}: a wrong digest was not reported"
    assert "deadbeef" in problems[0]


def test_a_wrong_recorded_sha256_file_is_caught(tmp_path):
    """The one copy no build reads, in the three tasks that read no other."""
    root = digest_task(tmp_path / "t", PIPED)
    (root / "environment" / "original.sha256").write_text(
        f"{'deadbeef' * 8}  original.tar.gz\n")
    problems = _recorded_digest_drift(root, root / "environment", [])
    assert any("original.sha256" in p for p in problems)


def test_a_digest_recorded_against_the_wrong_filename_is_caught(tmp_path):
    root = digest_task(tmp_path / "t", PIPED)
    env = root / "environment"
    real = hashlib.sha256(
        (env / "original.tar.gz").read_bytes()).hexdigest()
    (env / "original.sha256").write_text(f"{real}  repo.tar.gz\n")
    problems = _recorded_digest_drift(root, env, [])
    assert any("rather than for original.tar.gz" in p for p in problems)


def test_a_wrong_size_is_caught(tmp_path):
    root = digest_task(tmp_path / "t", PIPED, size=999999)
    problems = _recorded_digest_drift(root, root / "environment", [])
    assert any("999999" in p for p in problems)


def test_a_wrong_file_count_is_caught(tmp_path):
    root = digest_task(tmp_path / "t", VIA_ARG)
    df = root / "environment" / "Dockerfile"
    df.write_text(df.read_text() + "ARG REPO_FILES=99\n")
    problems = _recorded_digest_drift(root, root / "environment", [])
    assert any("REPO_FILES=99" in p for p in problems)


def test_a_tool_download_is_not_mistaken_for_a_committed_file(tmp_path):
    """pf01's zlib block invokes a copied script while guarding a download.

    Pairing a digest with any path its block mentions checks the download's
    digest against the script and reports a file that is fine as corrupt.
    """
    root = digest_task(tmp_path / "t", PIPED)
    env = root / "environment"
    (env / "build-zlib.sh").write_text("#!/bin/sh\nexit 0\n")
    df = env / "Dockerfile"
    df.write_text(df.read_text() + """
ARG ZLIB_SHA256=c3e5e9fdd5004dcb542feda5ee4f0ff0744628baf8ed2dd5d66f8ca1197cb1a1
COPY build-zlib.sh /opt/scripts/build-zlib.sh
RUN set -eux; \\
    curl -fsSLo /tmp/zlib.tar.gz https://example.invalid/zlib.tar.gz; \\
    echo "${ZLIB_SHA256}  /tmp/zlib.tar.gz" | sha256sum -c -; \\
    sh /opt/scripts/build-zlib.sh
""")
    assert _recorded_digest_drift(root, env, []) == []


def test_a_stage_dockerfiles_own_copy_is_read(tmp_path):
    """Each stage keeps its own digest beside its own copy of the tarball."""
    root = digest_task(tmp_path / "t", PIPED)
    stage = root / "tests" / "behavioural"
    (stage / "data").mkdir(exist_ok=True)
    shutil.copy(root / "environment" / "original.tar.gz",
                stage / "data" / "original.tar.gz")
    (stage / "Dockerfile").write_text(
        "FROM scratch\n"
        "COPY data/original.tar.gz /run/baseline/original.tar.gz\n"
        "RUN echo '" + "deadbeef" * 8 + "  /run/baseline/original.tar.gz' \\\n"
        "      | sha256sum -c -\n")
    problems = _recorded_digest_drift(root, root / "environment", [])
    assert any("tests/behavioural/Dockerfile" in p for p in problems)


# --------------------------------------------------------------------------- #
# Shared inputs must not drift
# --------------------------------------------------------------------------- #

def test_identical_copy_is_fine(tmp_path):
    root = make_task(tmp_path / "t")
    (root / "environment" / "requirements.txt").write_text("pytest==8.3.4\n")
    (root / "tests" / "behavioural" / "requirements.txt").write_text("pytest==8.3.4\n")
    assert _shared_input_drift(root, root / "environment") == []


def test_drifted_file_is_caught(tmp_path):
    root = make_task(tmp_path / "t")
    (root / "environment" / "requirements.txt").write_text("pytest==8.3.4\n")
    (root / "tests" / "behavioural" / "requirements.txt").write_text("pytest==7.0.0\n")
    problems = _shared_input_drift(root, root / "environment")
    assert len(problems) == 1
    assert "requirements.txt" in problems[0]


def test_drift_is_caught_under_data_too(tmp_path):
    """The tarball and the source contract live in ``data/`` now, not the root."""
    root = make_task(tmp_path / "t")
    (root / "environment" / "source-contract.json").write_text('{"v": 1}\n')
    (root / "tests" / "behavioural" / "data" / "source-contract.json").write_text(
        '{"v": 2}\n')
    problems = _shared_input_drift(root, root / "environment")
    assert len(problems) == 1
    assert "data/source-contract.json" in problems[0]


def test_drift_in_a_duplicated_tree_is_caught(tmp_path):
    """A warm-up cache is a directory, and comparing only its name is not enough."""
    root = make_task(tmp_path / "t")
    for where, body in ((root / "environment" / "warmup", "a\n"),
                        (root / "tests" / "behavioural" / "data" / "warmup", "b\n")):
        where.mkdir()
        (where / "plugins.txt").write_text(body)
    problems = _shared_input_drift(root, root / "environment")
    assert len(problems) == 1
    assert "plugins.txt" in problems[0]


def test_a_tree_missing_a_file_is_caught(tmp_path):
    root = make_task(tmp_path / "t")
    src = root / "environment" / "warmup"
    dst = root / "tests" / "behavioural" / "data" / "warmup"
    src.mkdir()
    dst.mkdir()
    (src / "plugins.txt").write_text("a\n")
    (src / "extra.txt").write_text("a\n")
    (dst / "plugins.txt").write_text("a\n")
    problems = _shared_input_drift(root, root / "environment")
    assert len(problems) == 1
    assert "different file lists" in problems[0]


def test_dockerfile_is_not_a_shared_input(tmp_path):
    """Each stage has its own.  Four images sharing one would be one image."""
    root = make_task(tmp_path / "t")
    assert (root / "environment" / "Dockerfile").read_text() \
        == (root / "tests" / "behavioural" / "Dockerfile").read_text()
    (root / "tests" / "behavioural" / "Dockerfile").write_text("FROM debian\n")
    assert _shared_input_drift(root, root / "environment") == []


def test_a_lib_module_is_not_a_copy_of_an_environment_file(tmp_path):
    """Only the stage root and ``data/`` are compared, not code directories."""
    root = make_task(tmp_path / "t")
    (root / "environment" / "verify.py").write_text("# the environment's\n")
    lib = root / "tests" / "behavioural" / "lib"
    lib.mkdir()
    (lib / "verify.py").write_text("# the suite's own, unrelated\n")
    assert _shared_input_drift(root, root / "environment") == []


# --------------------------------------------------------------------------- #
# Stage layout
# --------------------------------------------------------------------------- #

def test_correct_layout_passes(tmp_path):
    root = make_task(tmp_path / "t")
    assert _stage_layout(root, evaluation_for(root)) == []


def test_missing_dockerignore_is_caught(tmp_path):
    root = make_task(tmp_path / "t")
    (root / "tests" / "behavioural" / ".dockerignore").unlink()
    problems = _stage_layout(root, evaluation_for(root))
    assert len(problems) == 1
    assert ".dockerignore is missing" in problems[0]


def test_unexpected_directory_is_caught(tmp_path):
    """``fixtures/`` and ``corpus/`` are what the tree was called out for."""
    root = make_task(tmp_path / "t")
    (root / "tests" / "behavioural" / "fixtures").mkdir()
    problems = _stage_layout(root, evaluation_for(root))
    assert len(problems) == 1
    assert "fixtures/" in problems[0]


@pytest.mark.parametrize("name", ["original.tar.gz", "corpus.zip", "hsqldb.jar",
                                  "expectations.bin", "archive.tar"])
def test_loose_payload_is_caught(tmp_path, name):
    root = make_task(tmp_path / "t")
    (root / "tests" / "behavioural" / name).write_bytes(b"")
    problems = _stage_layout(root, evaluation_for(root))
    assert len(problems) == 1
    assert name in problems[0] and "data/" in problems[0]


def test_manifest_in_the_stage_root_is_fine(tmp_path):
    """suite.toml, probe.toml and prompt.txt belong at the top level."""
    root = make_task(tmp_path / "t")
    for name in ("suite.toml", "probe.toml", "prompt.txt", "pytest.ini",
                 "requirements-candidate.txt", "check-probe.py",
                 "run-candidate.sh"):
        (root / "tests" / "behavioural" / name).write_text("x\n")
    assert _stage_layout(root, evaluation_for(root)) == []


def test_disabled_stage_is_not_checked(tmp_path):
    root = make_task(tmp_path / "t")
    (root / "tests" / "evaluation.toml").write_text(EVALUATION.replace(
        '[stages.behavioural]\nenabled = true',
        '[stages.behavioural]\nenabled = false'))
    (root / "tests" / "behavioural" / ".dockerignore").unlink()
    assert _stage_layout(root, evaluation_for(root)) == []


# --------------------------------------------------------------------------- #
# Reading State A out of its archive
# --------------------------------------------------------------------------- #

def write_tarball(path: Path, names: list[str]) -> Path:
    import io
    with tarfile.open(path, "w:gz") as tf:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    return path


@pytest.mark.parametrize("names,expected", [
    # Seventeen tasks: one wrapper directory, under three different names.
    (["repo/go.mod", "repo/pkg/a.go"], ["go.mod", "pkg/a.go"]),
    (["graphhopper/pom.xml", "graphhopper/core/x.java"], ["core/x.java", "pom.xml"]),
    (["jsonnet-0.20.0/Makefile"], ["Makefile"]),
    # build02 wraps in "./".
    (["./pom.xml", "./gson/pom.xml"], ["gson/pom.xml", "pom.xml"]),
    # build01 and lang03 are flat, with files at the archive root.
    (["AUTHORS", "src/x.c"], ["AUTHORS", "src/x.c"]),
    # A flat archive whose only root entry is a directory keeps it: nothing sits
    # beside it, but one file does live at the root, so it is not a wrapper.
    (["Makefile", "src/x.c", "src/y.c"], ["Makefile", "src/x.c", "src/y.c"]),
])
def test_prefix_is_derived_not_assumed(tmp_path, names, expected):
    assert state_a_paths(write_tarball(tmp_path / "o.tar.gz", names)) == expected


def test_stripping_one_level_too_many_is_caught(tmp_path):
    """A wrapper holding a single directory and no file would leave no root file."""
    tarball = write_tarball(tmp_path / "o.tar.gz", ["repo/pkg/a.go", "repo/pkg/b.go"])
    with pytest.raises(AssertionError, match="one level too many"):
        state_a_paths(tarball)


def test_a_directory_pattern_is_charged_for_its_subtree(tmp_path):
    """`node_modules` deletes what is under it, which is how the collector behaves."""
    artifacts = [{"exclude": ["node_modules"]}]
    hits = exclusion_casualties(artifacts, ["test/cases/node_modules/lookup/x.styl"])
    assert len(hits) == 1 and "x.styl" in hits[0]


def test_an_unrelated_pattern_charges_nothing(tmp_path):
    assert exclusion_casualties([{"exclude": ["bin", "*.tgz"]}],
                                ["pkg/bindata/x.go", "testdata/a.tgz.sha256"]) == []


# --------------------------------------------------------------------------- #
# The shipped tasks, held to the same rules
# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_shipped_task_layout(task):
    evaluation = config.Evaluation.load(task / "tests" / "evaluation.toml")
    assert _stage_layout(task, evaluation) == []
    assert _shared_input_drift(task, task / "environment") == []


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_every_stage_dockerignore_is_identical(task):
    """One list in twelve places.  A stage excluding something the others keep
    produces an image nobody can reproduce from the tree."""
    bodies = {p.read_bytes() for p in (task / "tests").glob("*/.dockerignore")}
    assert len(bodies) == 1, f"{task.name}: {len(bodies)} distinct .dockerignore"


# --------------------------------------------------------------------------- #
# Every prompt names the tools that exist, and only those
# --------------------------------------------------------------------------- #

# A prompt is the only place a model learns what it can do, and two ways that goes
# wrong are both live risks: a prompt describes reading the trees and never says
# execution is available, so the round reasons about tests instead of running them;
# and a rename lands in the toolbox while the prompts keep naming the old verb, so
# the model calls a tool that is not there.


#: Tool names the toolbox does not answer to.  Kept in one place because two
#: different kinds of file name these tools -- the prompts a model reads, and the
#: Dockerfile comments an author reads -- and a rename that reaches only one of
#: them leaves the other lying.
RENAMED_AWAY = {"read_file"}


def prompts_for(task: Path, stage: str) -> list[Path]:
    return sorted((task / "tests" / stage).glob("**/prompt.txt"))


def stage_dockerfiles(task: Path) -> list[Path]:
    return sorted((task / "tests").glob("*/Dockerfile"))


# --------------------------------------------------------------------------- #

#: Tasks whose exclude list is known to delete files State A ships, with the
#: evidence, so the invariant can be enforced on the rest.  These are strict
#: xfails: narrowing one of these manifests makes this test pass, which fails here
#: and is the prompt to delete the entry.
#:
#: Empty, and meant to stay that way.  A waiver here and `swerefactor validate`
#: disagree about whether a known defect blocks a release, so whether one does
#: would depend on which of the two you ran.
KNOWN_EXCLUSION_CASUALTIES: dict[str, str] = {}


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_every_verification_prompt_says_how_to_execute(task):
    """An adversary that does not know it can run a test will not run one.

    ``try_test`` is the round's only way to execute anything, and a candidate that
    was reasoned about but never run is worth nothing to this stage -- so naming it
    is not decoration, it is the difference between the stage measuring the
    migration and the stage measuring how the migration reads.
    """
    prompts = prompts_for(task, "verification")
    if not prompts:
        pytest.skip(f"{task.name} has no verification prompt")
    for prompt in prompts:
        text = prompt.read_text(encoding="utf-8")
        assert "try_test" in text, (
            f"{task.name}/{prompt.name} never names try_test, so the round has no "
            f"way to know it can execute anything"
        )


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_no_prompt_names_a_tool_the_toolbox_dropped(task):
    """Guards the rename against the prompts, which the toolbox cannot see.

    ``read_file`` is a name a gateway claims, rewriting the schema under it, so the
    toolbox does not offer it.  A prompt still instructing the model to call
    ``read_file`` sends it at a tool that does not answer, which is the same dead
    round arriving by a slower route.
    """
    from swerefactor.tools import toolbox_for

    live = {s.name for s in toolbox_for(task, task).specs()}
    assert not (RENAMED_AWAY & live), (
        "this list is stale: the toolbox offers it again")

    for stage in ("verification", "audit"):
        for prompt in prompts_for(task, stage):
            text = prompt.read_text(encoding="utf-8")
            for name in RENAMED_AWAY:
                assert name not in text, (
                    f"{task.name}/{stage}/{prompt.name} names {name!r}, which the "
                    f"toolbox no longer offers"
                )


#: The published ``(model, effort)`` for every graded position.  Keyed by position
#: rather than task: stage 1 and the adjudicator are one measurement each across the
#: twenty, while the six adversary slots are six deliberately different models.
GRADED_ROSTER: dict[str, tuple[str, str]] = {
    "[stages.audit]": ("gpt-5.6-sol", "medium"),
    "[stages.verification.adjudicator]": ("claude-opus-5", "high"),
    "a01": ("gpt-5.6-sol", "high"),
    "a02": ("gpt-5.6-terra", "high"),
    "a03": ("gpt-5.6-luna", "max"),
    "a04": ("claude-sonnet-5", "high"),
    "a05": ("claude-opus-5", "medium"),
    "a06": ("claude-opus-5", "high"),
}


def graded_models(task: Path) -> dict[str, tuple[str, str | None]]:
    """What each graded position of ``task`` declares: ``(model, effort)``.

    ``effort`` is None where no ``reasoning_effort`` is named -- absent is not a
    value, it is the endpoint's default applied silently.
    """
    ev = config.Evaluation.load(task / "tests" / "evaluation.toml")
    found: dict[str, tuple[str, str | None]] = {}

    def note(where: str, spec: dict) -> None:
        if not spec.get("model"):
            return
        effort = (spec.get("driver_options") or {}).get("reasoning_effort")
        found[where] = (str(spec["model"]),
                        None if effort is None else str(effort))

    for name, stage in sorted(ev.stages.items()):
        if not stage.enabled:
            continue
        note(f"[stages.{name}]", stage.options)
        # A separate call, with its own driver and options.
        note(f"[stages.{name}.adjudicator]",
             stage.options.get("adjudicator") or {})

    stage = ev.stages.get("verification")
    if stage is not None and stage.enabled:
        probe = ev.resolve(stage.options.get("probe") or "verification/probe.toml")
        if probe.is_file():
            for adv in config.Probe.load(probe).adversaries:
                note(adv.id.split("-")[0],
                     {"model": adv.model,
                      "driver_options": adv.options.get("driver_options")})
    return found


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_every_graded_model_call_is_the_published_one(task):
    """Every graded position declares the published ``(model, effort)`` pair.

    The effort is part of the measurement, not a tuning knob: stage 1 is a gate
    whose failure zeroes a submission, and the adjudicator decides whether a
    defect is upheld.  A position naming no ``reasoning_effort`` sends none, so it
    runs at whatever the endpoint defaults to.

    Checked against the roster rather than across the twenty, because files that
    all omit an effort agree with each other while none of them is pinned.  Stage
    3 runs six different models by design, so the property there is per slot: a03
    is one model at one effort everywhere.
    """
    found = graded_models(task)
    assert found, f"{task.name} declares no graded model call at all"

    for where, (model, effort) in sorted(found.items()):
        assert where in GRADED_ROSTER, (
            f"{task.name} {where}: not in GRADED_ROSTER; add it there, so the "
            f"graded models stay named in one place"
        )
        want_model, want_effort = GRADED_ROSTER[where]
        assert (model, effort) == (want_model, want_effort), (
            f"{task.name} {where}: declares {model!r} at "
            + (f"{effort!r} effort" if effort else
               "*no* effort, so it runs at the endpoint's default")
            + f"; the published roster is {want_model!r} at {want_effort!r}"
        )

    missing = sorted(set(GRADED_ROSTER) - set(found))
    assert not missing, (
        f"{task.name} declares no model at: {', '.join(missing)} -- every graded "
        f"position is scored, so none of them can be left to a default"
    )


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_no_stage_dockerfile_describes_a_tool_the_toolbox_dropped(task):
    """The same rename, guarded against the prose an author reads.

    Each stage Dockerfile opens with a header saying what that stage's toolbox
    offers -- it is where someone goes to learn whether a review can execute
    anything.  Nothing breaks at run time when that list is out of date, which is
    why it needs a test: twenty of these still described ``read_file`` after the
    toolbox had stopped answering to it, and the only symptom was an author
    writing a prompt around a tool that does not exist.

    ``COPY . /tests/<stage>`` also bakes the Dockerfile into the image it builds,
    so the stale sentence ships to the grader and outlives the tree it came from.
    """
    for path in stage_dockerfiles(task):
        text = path.read_text(encoding="utf-8")
        for name in RENAMED_AWAY:
            assert name not in text, (
                f"{task.name}/{path.parent.name}/Dockerfile describes {name!r}, "
                f"which the toolbox no longer offers"
            )


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_the_candidate_command_is_findable_from_the_tree_it_runs_in(task):
    """`try_test` is stage 3's only way to execute anything; it has to work.

    ``CandidateRunner.execute`` runs ``candidate_command`` with ``cwd`` set to a
    salted copy of the tree under test, not to the stage directory.  So a command
    naming its script relatively -- ``["bash", "run-candidate.sh"]`` -- is looked
    for at the root of the repository under test, is not there, and comes back exit
    127 for every candidate on both trees.

    Nothing reports that.  The round reads "did not pass on the original" as a
    candidate that cannot count, tries the next one, finds nothing, and the
    submission is paid its ten points; six rounds do it six times.  Measured on
    lang07, which shipped this way: as declared, 4 of 4 runs exit 127; with the
    path made absolute, 4 of 4 correct in the same image on the same trees.

    So the assertion is on the token, not on the outcome: any argument that names a
    file present in the stage directory must say so absolutely.  ``bash`` and the
    like are unaffected -- they are found on PATH and are not files here.
    """
    for probe_path in sorted((task / "tests").glob("*/probe.toml")):
        stage = probe_path.parent
        command = config.Probe.load(probe_path).candidate_command
        for token in command:
            if token.startswith("/") or token.startswith("-"):
                continue
            assert not (stage / token).exists(), (
                f"{task.name}/{stage.name}/probe.toml candidate_command names "
                f"{token!r} relatively, and that file exists here -- but the "
                f"harness runs this command from a copy of the tree under test, "
                f"where it does not exist.  Write it as /tests/{stage.name}/{token}."
            )


@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_a_harness_module_copied_between_stages_stays_identical(task):
    """A file a stage COPIED from another stage must still equal its source.

    Where a task shares harness code between stage 2 and stage 3, the shared claim
    is that a stage-3 finding is a divergence between two repositories rather than
    between two harnesses -- which holds only while the client that asks and the
    command that builds are the same on both sides.

    The stages assert this themselves and cannot.  A stage image's build context is
    its own directory, so a digest in tests/verification/Dockerfile is a constant
    re-derived from the verification copy; it pins that copy against local edits and
    is blind to the file it was supposed to be equal to.  fw07 shipped that way:
    stage 2 adopted ``swerefactor.contract.submission_env`` in maven.py and server.py,
    stage 3's copies kept ``dict(os.environ)``, the two were 12 lines apart, every
    pin passed, and the image's own harness/__init__.py said all three were
    byte-identical to tests/behavioural/lib/harness/.

    This test is the only place both directories exist at once, so it is the only
    place the invariant can be held.  Discovery is by filename rather than a list:
    a task that copies a fourth module gets it checked without editing this file,
    and a stage's own files (harness/__init__.py, which documents what was copied
    and what was deliberately not) are only compared when the other stage has a
    file of the same name to compare against.
    """
    stage_dirs = sorted(p for p in (task / "tests").iterdir()
                        if p.is_dir() and (p / "lib" / "harness").is_dir())
    if len(stage_dirs) < 2:
        return

    for i, left in enumerate(stage_dirs):
        for right in stage_dirs[i + 1:]:
            ldir, rdir = left / "lib" / "harness", right / "lib" / "harness"
            shared = ({p.name for p in ldir.glob("*.py")}
                      & {p.name for p in rdir.glob("*.py")})
            for name in sorted(shared):
                lhs, rhs = (ldir / name).read_bytes(), (rdir / name).read_bytes()
                if name == "__init__.py":
                    # Each stage's own manifest of what it took and what it left.
                    continue
                assert lhs == rhs, (
                    f"{task.name}: tests/{left.name}/lib/harness/{name} and "
                    f"tests/{right.name}/lib/harness/{name} have drifted "
                    f"({len(lhs)} vs {len(rhs)} bytes).  One stage was edited and "
                    f"the other was not, so the two stages no longer ask the same "
                    f"question -- a candidate can pass in one and fail in the "
                    f"other, and the report will name the submission for it.  "
                    f"Copy the intended version over the other, then re-pin "
                    f"whichever stage Dockerfile records its sha256."
                )


# --------------------------------------------------------------------------- #

@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_no_artifact_exclusion_deletes_something_state_a_ships(task, request):
    """The collector must not remove a file the reference tree commits.

    An exclude list is written against build output, and build output is exactly
    what a repository sometimes commits on purpose.  fw04 is the measured case:
    ``*.tgz`` and ``*.tgz.prov`` matched
    ``testdata/badcharts/mybadchart/mybadchart-1.0.0.tgz{,.prov}``, which State A
    ships *because* helm refuses to build them -- that chart is named
    ``../../../../charts/org2/repo2/evil``, and ``helm package .`` "succeeds" by
    writing outside the repository, so the setup script cannot put them back and
    its badcharts loop has no ``--sign`` to make the ``.prov`` at all.  The
    repository's own suite reads both with ``suite.Nil(err)``, so every submission
    was charged for a file the harness deleted: State A took full marks handed to
    the stage directly and failed a check collected through its own manifest.

    Read from ``original.tar.gz`` because that is the same archive the environment
    image extracts and sha256-verifies, so this cannot drift from State A.

    Both matching rules are checked, and so is every ancestor directory, because a
    pattern that matches a directory takes the subtree with it.  Harbor is not in
    this repository and the corpus does not settle which rule it uses -- almost
    every entry is a bare name, but fw02 ships ``.husky/_``, which only means
    anything against a path.  A pattern has to be safe under either reading, since
    the wrong guess deletes a submission rather than a build.
    """
    if task.name in KNOWN_EXCLUSION_CASUALTIES:
        request.applymarker(pytest.mark.xfail(
            strict=True, reason=KNOWN_EXCLUSION_CASUALTIES[task.name]))
    artifacts = tomlcompat.load(task / "task.toml").get("artifacts") or []
    if not artifacts:
        pytest.skip(f"{task.name} declares no [[artifacts]]")
    tarball = task / "environment" / "original.tar.gz"
    if not tarball.is_file():
        pytest.skip(f"{task.name} ships no environment/original.tar.gz")

    delivered = state_a_paths(tarball)
    casualties = exclusion_casualties(artifacts, delivered)
    assert not casualties, (
        f"{task.name}: [[artifacts]].exclude removes {len(casualties)} path(s) "
        f"State A commits, so the reference cannot score full marks once collected. "
        f"Either the file is regenerable and the suite should rebuild it, or the "
        f"pattern has to be narrowed:\n"
        + "\n".join(f"  {c}" for c in casualties[:20])
        + (f"\n  ... and {len(casualties) - 20} more" if len(casualties) > 20 else "")
    )


# --------------------------------------------------------------------------- #
# a COPY whose source is not in the build context
# --------------------------------------------------------------------------- #


def make_context(tmp_path: Path, dockerfile: str, *present: str) -> Path:
    root = tmp_path / "ctx"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    for name in present:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    return root


def test_a_copy_of_a_present_file_is_fine(tmp_path):
    ctx = make_context(tmp_path, "FROM x\nCOPY original.tar.gz /opt/repo.tar.gz\n",
                       "original.tar.gz")
    assert _copy_sources(ctx, "environment") == []


def test_a_copy_of_an_absent_file_is_caught(tmp_path):
    """The shape lang04 shipped with: the environment image could not build.

    Renaming the payload and not the COPY is invisible to every other check --
    the Dockerfile is present, the layout is right, the tarball is right -- and
    the task reads as consistent until someone tries to build it.
    """
    ctx = make_context(tmp_path, "FROM x\nCOPY repo.tar.gz /opt/repo.tar.gz\n",
                       "original.tar.gz")
    problems = _copy_sources(ctx, "environment")
    assert len(problems) == 1
    assert "repo.tar.gz" in problems[0]
    # The near-miss in the context is named, because that is the fix.
    assert "original.tar.gz" in problems[0]


def test_the_reported_line_survives_a_continuation(tmp_path):
    """Joining continuations must not shift the line numbers after them.

    A line number pointing at an unrelated instruction is worse than no line
    number, because the reader trusts it.
    """
    ctx = make_context(tmp_path, "FROM x\nRUN echo one \\\n && echo two \\\n"
                                 " && echo three\nCOPY gone.txt /gone.txt\n")
    problems = _copy_sources(ctx, "environment")
    assert len(problems) == 1
    assert "Dockerfile:5" in problems[0]


def test_a_multiline_copy_is_read_as_one_instruction(tmp_path):
    ctx = make_context(tmp_path, "FROM x\nCOPY \\\n  present.txt \\\n"
                                 "  gone.txt \\\n  /opt/\n", "present.txt")
    problems = _copy_sources(ctx, "environment")
    assert len(problems) == 1
    assert "gone.txt" in problems[0] and "Dockerfile:2" in problems[0]


def test_a_copy_from_another_stage_is_not_a_context_path(tmp_path):
    """``COPY --from=infra`` resolves in another image; no file listing answers it."""
    ctx = make_context(tmp_path, "FROM x AS infra\nFROM y\n"
                                 "COPY --from=infra /opt/swerefactor /opt/swerefactor\n")
    assert _copy_sources(ctx, "[stages.behavioural]") == []


def test_flags_are_not_mistaken_for_sources(tmp_path):
    ctx = make_context(tmp_path, "FROM x\n"
                                 "COPY --chown=agent:agent tree/ /workspace/tree/\n",
                       "tree/a")
    assert _copy_sources(ctx, "environment") == []


def test_a_glob_that_matches_nothing_is_caught(tmp_path):
    ctx = make_context(tmp_path, "FROM x\nCOPY lib/*.py /opt/lib/\n", "lib/a.txt")
    problems = _copy_sources(ctx, "environment")
    assert len(problems) == 1
    assert "nothing in the build context matches it" in problems[0]


def test_a_glob_that_matches_is_fine(tmp_path):
    ctx = make_context(tmp_path, "FROM x\nCOPY lib/*.py /opt/lib/\n", "lib/a.py")
    assert _copy_sources(ctx, "environment") == []


def test_an_add_of_a_url_is_not_a_context_path(tmp_path):
    ctx = make_context(tmp_path,
                       "FROM x\nADD https://example.invalid/x.tar.gz /tmp/\n")
    assert _copy_sources(ctx, "environment") == []


def test_a_source_built_from_an_arg_is_left_alone(tmp_path):
    """``COPY ${THING}`` cannot be resolved without the daemon's ARG values.

    Guessing at one would report a problem the build does not have, and a check
    that cries wolf gets switched off.
    """
    ctx = make_context(tmp_path, "FROM x\nARG THING=a.txt\nCOPY ${THING} /tmp/\n")
    assert _copy_sources(ctx, "environment") == []


def test_a_context_with_no_dockerfile_is_not_a_problem(tmp_path):
    """_stage_images already reports that; two checks on one fact reads as two."""
    root = tmp_path / "empty"
    root.mkdir()
    assert _copy_sources(root, "environment") == []


#: Tasks whose build context is known not to hold together, each with the reason.
#: Empty, and the machinery below is kept for the next one rather than deleted with
#: the list: an allowlist that has to be rebuilt before it can be used is an
#: allowlist nobody adds to, and a task xfailed here at least names its own defect.
#:
#: `strict=True` is what keeps it honest.  An entry whose defect gets fixed XPASSes
#: and fails the suite, so the list cannot quietly outlive the thing it excuses --
#: and the red is the signal to delete the entry rather than relax the marker.
KNOWN_UNBUILDABLE: dict[str, str] = {}

BUILD_CONTEXT_TASKS = [
    pytest.param(t, marks=pytest.mark.xfail(strict=True,
                                            reason=KNOWN_UNBUILDABLE[t.name]))
    if t.name in KNOWN_UNBUILDABLE else pytest.param(t)
    for t in TASKS
]


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", BUILD_CONTEXT_TASKS, ids=lambda p: p.name)
def test_every_shipped_build_context_copies_what_it_ships(task):
    """An image that cannot build is not a consistent task, in any of the twelve.

    Every context the evaluation declares, plus the agent's environment -- the
    same set ``validate`` reads, so a task passing this passes that.
    """
    problems = _copy_sources(task / "environment", "environment")
    evaluation = config.Evaluation.load(task / "tests" / "evaluation.toml")
    for name, stage in sorted(evaluation.stages.items()):
        context = str(stage.options.get("context") or "")
        if stage.enabled and context:
            cdir = evaluation.resolve(context)
            if cdir.is_dir():
                problems.extend(_copy_sources(cdir, f"[stages.{name}]"))
    assert problems == [], f"{task.name}: " + "; ".join(problems)


# --------------------------------------------------------------------------- #
# The ladder's numbers, where the instruction quotes them
# --------------------------------------------------------------------------- #

# Retuning the ladder is a two-line edit to `[scoring]`, and prose does not follow
# an edit: a task left saying six adversaries at some other figure come to some
# other total, or that stage 3 is where some other share of the 100 points is, is
# telling an agent to budget against a stage worth what it is not.  The instruction
# is the one graded artifact no code consumes, so nothing else can catch it.  The
# tests below are the four sentence shapes the corpus uses, each on prose that
# disagrees with the policy, and the three that look like ladder figures and are
# not.


def instruction_task(root: Path, body: str, *, scoring: str = "") -> Path:
    """A task whose only content is an instruction saying `body`."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "instruction.md").write_text(body, encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "evaluation.toml").write_text(EVALUATION + scoring)
    return root


def arithmetic(root: Path) -> tuple[list[str], list[str]]:
    notes: list[str] = []
    return _instruction_arithmetic(root, evaluation_for(root), notes), notes


def test_a_stage_share_of_the_whole_must_be_a_stage_total(tmp_path):
    """A share the policy pays for neither stage, across a line break."""
    root = instruction_task(tmp_path / "t", "* That is what\n  stage 3 is for, "
                            "and it is where 85 of the 100 points are.\n")
    problems, _ = arithmetic(root)
    assert len(problems) == 1
    assert "85 of the 100" in problems[0]
    # Both stage totals, so the sentence tells its reader what the figure should
    # have been rather than only that it is wrong.
    assert "40" in problems[0] and "60" in problems[0]


@pytest.mark.parametrize("part", ["40", "60", "100"])
def test_a_share_the_policy_pays_is_fine(tmp_path, part):
    root = instruction_task(tmp_path / f"t{part}",
                            f"stage 3 is where {part} of the 100 points are.\n")
    problems, notes = arithmetic(root)
    assert problems == []
    assert any("1 ladder figure" in n for n in notes)


def test_a_per_adversary_figure_multiplied_out_is_checked(tmp_path):
    """The per-adversary figure right and the product wrong, across a line break."""
    root = instruction_task(tmp_path / "t", "Every adversary that fails to find "
                            "one is worth\n**10 points**, for **45**. The "
                            "divergences that are found are not worth "
                            "anything.\n")
    problems, _ = arithmetic(root)
    assert len(problems) == 1
    assert "6 x 10 = 60" in problems[0]


def test_the_corrected_product_passes(tmp_path):
    root = instruction_task(tmp_path / "t", "worth **10 points**, for **60**.\n")
    assert arithmetic(root)[0] == []


def test_a_product_of_something_other_than_the_per_model_figure_is_ignored(
        tmp_path):
    """`13,940 cases, for 16 modules` is not the verification arithmetic."""
    root = instruction_task(tmp_path / "t",
                            "Sixteen modules over 13,940 points, for 16.\n")
    problems, notes = arithmetic(root)
    assert problems == []
    assert any("no ladder figures" in n for n in notes)


def test_the_full_marks_entry_condition_is_the_gate(tmp_path):
    root = instruction_task(tmp_path / "t", "**Stage 3 runs only at 35 of 35** "
                            "-- full marks in stage 2, every scored case "
                            "answered.\n")
    problems, _ = arithmetic(root)
    assert len(problems) == 1
    assert "35 of 35" in problems[0] and "40" in problems[0]


@pytest.mark.parametrize("text", [
    "it runs only if stage 2 scored at least 40 of its 40 -- full marks",
    "Run only if stage 2 scores all 40 of its 40 -- full marks, every check",
    "**Stage 3 runs only at 40 of 40** -- full marks in stage 2",
])
def test_the_three_spellings_the_corpus_uses_pass(tmp_path, text):
    root = instruction_task(tmp_path / "t", text + "\n")
    assert arithmetic(root)[0] == []


@pytest.mark.parametrize("text", [
    "1200 of 1277 cases pass under State A's own suite.",
    "13 of 35 handlers are registered by name.",
])
def test_an_equal_pair_is_needed_and_a_count_is_not_one(tmp_path, text):
    """A pass count and an inventory are not the stage-3 gate."""
    problems, notes = arithmetic(instruction_task(tmp_path / "t", text + "\n"))
    assert problems == []
    assert any("no ladder figures" in n for n in notes)


def test_an_equal_pair_outside_a_gate_sentence_is_not_read_as_one(tmp_path):
    """Both halves equal, and nothing about full marks or stage 3 nearby."""
    problems, notes = arithmetic(instruction_task(
        tmp_path / "t", "The suite ships 12 of 12 fixtures for this module.\n"))
    assert problems == []
    assert any("no ladder figures" in n for n in notes)


@pytest.mark.parametrize("count", ["Six", "six", "6"])
def test_the_adversary_count_is_checked_in_words_and_digits(tmp_path, count):
    good = instruction_task(tmp_path / f"ok{count}",
                            f"{count} independent adversaries get both trees.\n")
    assert arithmetic(good)[0] == []
    bad = instruction_task(tmp_path / f"no{count}",
                           "Twelve independent adversaries get both trees.\n")
    problems, _ = arithmetic(bad)
    assert len(problems) == 1
    assert "verification_models = 6" in problems[0]


def test_a_stage_named_by_number_in_a_table_is_not_an_adversary_count(tmp_path):
    """lang05's ladder table.  Reading `3 verification` as a count reported a
    task whose prose was right, which is the failure mode a rule like this has
    instead of missing something."""
    root = instruction_task(tmp_path / "t", (
        "| stage | worth | what happens |\n"
        "| --- | --- | --- |\n"
        "| 1 audit | pass or fail | a failure scores **0** |\n"
        "| 2 behavioural | **40 points, or none** | short of every case, 0 |\n"
        "| 3 verification | **60 points** | 10 points each for the models |\n"))
    problems, notes = arithmetic(root)
    assert problems == []
    assert any("no ladder figures" in n for n in notes)


def test_a_count_outside_an_verification_paragraph_is_not_checked(tmp_path):
    """`six models` in a paragraph about stage 1 is not this stage's count."""
    root = instruction_task(tmp_path / "t", "Twelve models review the tree by "
                            "reading it.\n\nStage 3: six adversaries.\n")
    assert arithmetic(root)[0] == []


def test_an_instruction_stating_nothing_says_so(tmp_path):
    """Fourteen of the twenty are this, and the note is how a reader tells that
    from a check that passed."""
    root = instruction_task(tmp_path / "t", "Port the tree. Keep behaviour.\n")
    problems, notes = arithmetic(root)
    assert problems == []
    assert notes == ["instruction.md quotes no ladder figures, so none were "
                     "checked against [scoring]"]


def test_a_task_that_retuned_its_own_ladder_is_read_against_its_own_toml(
        tmp_path):
    """The figures are per task, not the published ones: a task paying 50/50
    with four adversaries is checked against what it declares."""
    body = ("Stage 3 is where 50 of the 100 points are. **Four** independent "
            "adversaries each get an hour, and each is worth 12.5 points, "
            "for 50.\n")
    root = instruction_task(tmp_path / "t", body, scoring=(
        "\n[scoring]\nbehavioural_points = 50.0\n"
        "verification_points = 50.0\nverification_models = 4\n"
        "points_per_survived_model = 12.5\n"))
    problems, notes = arithmetic(root)
    assert problems == []
    assert any("3 ladder figure" in n for n in notes)


def test_a_missing_instruction_is_left_to_the_caller(tmp_path):
    """`validate` already reports it; two problems for one file is noise."""
    root = tmp_path / "t"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "evaluation.toml").write_text(EVALUATION)
    assert _instruction_arithmetic(root, evaluation_for(root), []) == []


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_every_shipped_instruction_agrees_with_its_scoring(task):
    evaluation = config.Evaluation.load(task / "tests" / "evaluation.toml")
    problems = _instruction_arithmetic(task, evaluation, [])
    assert problems == [], f"{task.name}: " + "; ".join(problems)
