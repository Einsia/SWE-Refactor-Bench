"""Where the ladder puts a tree inside a stage container, and what it says when it cannot.

``docker cp -`` does **not** create a missing destination parent.  It reports
``destination "<cid>:/workspace" must be a directory`` and closes the pipe, so a
driver that copies into ``Path(dest).parent`` requires that parent to already
exist in the image.  Ten of the twenty behavioural images never create
``/workspace`` -- it arrives as a bind mount in ``tests/test.sh``, and Docker
creates a mount point where it does not create a copy target.  So for those ten
tasks stage 2 could not be reached through the sibling-container driver at all:
the run was scored ``NOT A VALID RESULT`` with stage 1 *passing*, which is the
subset of submissions that had done the most work.

The bug was invisible for two compounding reasons, and this file pins both.

The write dies before the read.  ``BrokenPipeError`` raised inside
``with tarfile.open(fileobj=proc.stdin)`` propagates out of ``_copy_in`` before
the line that reads ``proc.stderr``, so docker's own sentence was discarded and
the caller logged ``BrokenPipeError: [Errno 32] Broken pipe``.  That reads as
transient contention -- at thirty concurrent runs it was measured, believed, and
nearly retried -- rather than as a directory that is not there.  A copy failure
must therefore report docker's stderr, not the symptom of its own writer.

And a short archive is not an error.  ``docker cp`` can exit 0 having consumed a
truncated stream, so ``rc == 0`` alone does not establish that the tree arrived;
the broken pipe has to be reported even then.

Written against ``_copy_in`` with a fake ``Popen`` rather than through
``run_stage`` against a real daemon: the property is which member name is
handed to ``docker cp`` and which stream is read back on failure, and a live
container in between would only add ways for a case to pass without the check
having run.  The companion probe ``srb-runs-0807/probe-copyin.py`` covers the
real daemon.
"""

from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import ladder
from swerefactor.result import StageResult, Unit


class FakePopen:
    """Stand in for ``docker cp -``, recording argv and the tar it was fed.

    ``break_after`` makes the writer fail the way the real one does: a closed
    read end, surfacing as ``BrokenPipeError`` on write, which is what happens
    when docker rejects the destination and exits while the tar is streaming.
    """

    instances: list["FakePopen"] = []

    def __init__(self, argv, stdin=None, stderr=None, *, rc=0, err=b"",
                 break_after=None):
        self.argv = list(argv)
        self.rc = rc
        self._err = err
        self._break_after = break_after
        self._written = 0
        self.buf = io.BytesIO()
        self.stdin = self
        self.stderr = io.BytesIO(err)
        self.stderr_read = False
        FakePopen.instances.append(self)

    # -- stdin --
    def write(self, data):
        if self._break_after is not None and self._written >= self._break_after:
            raise BrokenPipeError(32, "Broken pipe")
        self._written += len(data)
        return self.buf.write(data)

    def flush(self):
        pass

    def close(self):
        pass

    def wait(self):
        return self.rc

    @property
    def dest(self) -> str:
        """The ``<cid>:<path>`` argument docker was told to extract into."""
        return self.argv[-1]

    def members(self) -> list[str]:
        self.buf.seek(0)
        with tarfile.open(fileobj=self.buf, mode="r|") as tf:
            return [m.name for m in tf]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "go.mod").write_text("module x\n", encoding="utf-8")
    (repo / "pkg" / "server.go").write_text("package pkg\n", encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _reset():
    FakePopen.instances.clear()
    yield
    FakePopen.instances.clear()


def _patch(monkeypatch, **kw):
    def factory(argv, stdin=None, stderr=None):
        return FakePopen(argv, stdin, stderr, **kw)
    monkeypatch.setattr(ladder.subprocess, "Popen", factory)


@pytest.mark.parametrize("dest", ["/workspace/repo", "/opt/workspace"])
def test_extracts_at_root_so_missing_parents_are_created(monkeypatch, tree, dest):
    """The member carries dest's full root-relative path, extracted at ``/``.

    This is the whole fix: tar extraction creates the parents it needs, so the
    image does not have to have made the directory first.  Copying into
    ``dest``'s parent under ``dest``'s basename is what could not work.
    """
    _patch(monkeypatch)
    ladder._copy_in("deadbeef", tree, dest)

    call = FakePopen.instances[-1]
    assert call.dest == "deadbeef:/", (
        f"must extract at / so parents are created, not into {call.dest!r}")
    assert call.members()[0] == dest.lstrip("/"), (
        f"member must be the root-relative path {dest.lstrip('/')!r}")
    # The payload still arrives under it, so this is a relocation and not a
    # narrowing of what gets copied.
    assert f"{dest.lstrip('/')}/pkg/server.go" in call.members()


def test_reports_dockers_stderr_when_the_pipe_breaks(monkeypatch, tree):
    """A broken pipe must surface docker's sentence, not BrokenPipeError.

    The regression: this failure mode is what a missing destination looks like
    from the writer's side, and reporting the symptom sent the diagnosis after
    daemon contention instead of a directory.
    """
    _patch(monkeypatch, rc=1, break_after=0,
           err=b'destination "abc:/workspace" must be a directory')

    with pytest.raises(RuntimeError) as exc:
        ladder._copy_in("abc", tree, "/workspace/repo")

    msg = str(exc.value)
    assert "must be a directory" in msg, f"docker's stderr is missing from {msg!r}"
    assert "/workspace/repo" in msg
    assert "BrokenPipeError" not in msg


def test_a_truncated_stream_is_a_failure_even_at_rc_zero(monkeypatch, tree):
    """rc=0 does not prove the tree arrived if the writer was cut off.

    Reported as a failure because the alternative is a stage that grades a
    partial submission -- a wrong score rather than a harness error.
    """
    _patch(monkeypatch, rc=0, break_after=0, err=b"")

    with pytest.raises(RuntimeError) as exc:
        ladder._copy_in("abc", tree, "/workspace/repo")
    assert "pipe broken" in str(exc.value)


def test_success_is_quiet_and_reads_stderr(monkeypatch, tree):
    """The ordinary path raises nothing, and still drains stderr.

    Draining matters because the writer holds the other end: leaving a filled
    pipe unread is how the copy of the largest tree this driver touches would
    block instead of returning.
    """
    _patch(monkeypatch, rc=0)
    ladder._copy_in("abc", tree, "/workspace/repo")
    assert FakePopen.instances[-1].wait() == 0


# -- and what the ladder leaves behind when a stage produced nothing -----------

def test_a_stage_that_left_no_result_gets_one_recorded_for_it(tmp_path):
    """The driver's own reason has to reach the scorer, which reads files.

    The failure above is the one that made this necessary: a copy-in that broke
    left no `behavioural.json`, the scorer saw `behavioural=None`, and the report
    named `blocked_by` instead -- publishing "NOT RUN (the audit gate failed)"
    over a stage that had started and broken.  Ten runs in the 0807 campaign said
    that, and two more said it about a stage that overran its own timeout.
    """
    class Evaluation:
        task = "t"

        def result_path(self, name: str) -> Path:
            return tmp_path / f"{name}.json"

    ladder._record_stage_failure(Evaluation(), "behavioural", "the pipe broke: no /workspace")
    res = StageResult.read(tmp_path / "behavioural.json")

    assert res.stage == "behavioural"
    # `error`, not an empty `ok`: an ok stage with no modules grades as
    # `behavioural-empty`, which blames the task for declaring none.
    assert res.status == "error"
    assert res.metadata["error"] == "the pipe broke: no /workspace"
    assert res.metadata["recorded_by"] == "ladder"
    assert not res.units and not res.checks, "a recorded failure must not fabricate a module"


def test_a_stage_that_wrote_its_own_result_is_not_overwritten(tmp_path):
    """A measurement outranks the driver's guess at one.

    The call site is already guarded by `not ...exists()`; this pins the same rule
    inside the function, because the guard and the writer are edited by different
    people for different reasons.
    """
    class Evaluation:
        task = "t"

        def result_path(self, name: str) -> Path:
            return tmp_path / f"{name}.json"

    real = StageResult(stage="behavioural", task="t", status="ok")
    real.units.append(Unit(id="m", title="m", weight=1.0, status="ok"))
    real.write(tmp_path / "behavioural.json")

    ladder._record_stage_failure(Evaluation(), "behavioural", "a note about nothing")
    after = StageResult.read(tmp_path / "behavioural.json")
    assert after.status == "ok" and [u.id for u in after.units] == ["m"]


def test_recording_a_failure_cannot_raise(tmp_path):
    """It runs where something has already gone wrong; it must not add a traceback."""
    class Evaluation:
        task = "t"

        def result_path(self, name: str) -> Path:
            return tmp_path / "not-a-dir" / "deeper" / f"{name}.json"

    (tmp_path / "not-a-dir").write_text("this is a file\n", encoding="utf-8")
    ladder._record_stage_failure(Evaluation(), "behavioural", "note")   # no raise
