"""Whether ``run_stage`` mounts the reference tree, and who gets to decide.

The ladder copies State A to ``/opt/original`` in every stage container.  For most
tasks that copy *is* the mount: fw04 and fw06 read ``SRB_ORIGINAL`` at runtime and
their behavioural images carry no reference of their own, so without the copy those
modules have nothing to compare against.

lang07 is the opposite.  Its behavioural image deletes ``data/original.tar.gz`` in
the final layer and asserts ``test ! -d /opt/original``, because a reference
reachable from the container where the submission's own build runs is a reference a
``dist/probe.js`` could unpack and forward every case to -- scoring full marks as no
port at all.  Its ``lib/provenance.py`` lists ``/opt/original`` in
``REFERENCE_PATHS`` and, finding one, emits ``status error`` on a check marked
``required``, which invalidates the whole stage.

So an unconditional copy makes the harness fail its own check, and the failure is
charged to the submission: ``valid=false`` with a defaulted ``score: 0.0`` on a run
whose other twelve modules all passed.  That is what ``lang07/high`` recorded.  The
copy is now gated on ``[stages.<name>] reference``, default true.

Driven through ``run_stage`` rather than ``_copy_in``, because the gate is in
``run_stage`` and the property under test is *which destinations are copied for
which declaration*.  Docker is replaced wholesale: the argv and the recorded
destinations are the observable, and a real daemon in between would only add ways
for a case to pass without the assertion having run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import config, ladder


class Recorder:
    """Collects every ``_copy_in`` destination and every docker argv."""

    def __init__(self) -> None:
        self.dests: list[str] = []
        self.argv: list[list[str]] = []


@pytest.fixture
def rec(monkeypatch, tmp_path):
    r = Recorder()

    class Done:
        returncode = 0
        stdout = "cid0001\n"
        stderr = ""

    def fake_run(argv, **kw):
        r.argv.append(list(argv))
        return Done()

    monkeypatch.setattr(ladder, "_run", fake_run)
    monkeypatch.setattr(ladder, "_copy_in",
                        lambda cid, src, dest: r.dests.append(dest))
    monkeypatch.setattr(ladder, "_copy_out", lambda cid, src, dest: None)
    monkeypatch.setattr(ladder, "_log", lambda *a, **k: None)

    class Proc:
        returncode = 0

    monkeypatch.setattr(ladder.subprocess, "run", lambda *a, **k: Proc())
    return r


def _stage(**options) -> config.StageConfig:
    opts = {"image": "swerefactor/x-behavioural:1", "context": "behavioural"}
    opts.update(options)
    return config.StageConfig(name="behavioural", runner="behavioural",
                              result="behavioural.json", timeout_sec=60.0,
                              options=opts)


def _paths(tmp_path: Path) -> dict:
    (tmp_path / "orig").mkdir()
    (tmp_path / "res").mkdir()
    (tmp_path / "eval.toml").write_text("", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    return {"eval_path": tmp_path / "eval.toml", "repo": tmp_path / "repo",
            "original": tmp_path / "orig", "results": tmp_path / "res"}


def test_the_reference_is_copied_by_default(rec, tmp_path):
    """No declaration copies the reference, which fw04 and fw06 depend on."""
    code, note = ladder.run_stage(_stage(), **_paths(tmp_path))
    assert "/opt/original" in rec.dests, note
    assert code == 0


def test_reference_false_withholds_it(rec, tmp_path):
    """lang07's declaration: the repo still arrives, the reference does not."""
    code, note = ladder.run_stage(_stage(reference=False), **_paths(tmp_path))
    assert "/opt/original" not in rec.dests, note
    # The opt-out must not cost the stage anything else it needs.
    assert "/tests/evaluation.toml" in rec.dests
    assert any(d.startswith("/workspace") or d.startswith("/opt/repo")
               or "repo" in d for d in rec.dests), rec.dests
    assert code == 0


def test_the_flag_only_governs_the_copy_not_the_argument(rec, tmp_path):
    """``--original`` is still passed, and that is deliberate.

    ``cli.py`` resolves a path that does not exist to ``None`` and then never sets
    ``SRB_ORIGINAL`` -- the optional-mount state fw07's build module already relies
    on.  Dropping the argument instead would make the driver fall back to its own
    ``DEFAULT_ORIGINAL`` of ``/opt/original``, which is the same string, so the
    argument is not what decides anything; the copy is.
    """
    ladder.run_stage(_stage(reference=False), **_paths(tmp_path))
    create = next(a for a in rec.argv if "create" in a)
    assert "--original" in create and "/opt/original" in create


def test_a_missing_original_is_still_skipped_quietly(rec, tmp_path):
    """``original=None`` (no tarball for the task) must not become an error."""
    paths = _paths(tmp_path)
    paths["original"] = None
    code, _ = ladder.run_stage(_stage(), **paths)
    assert "/opt/original" not in rec.dests
    assert code == 0
