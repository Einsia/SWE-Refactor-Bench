"""lang04's crate graph survives the trip between two modules.

The build's outcome crosses a process boundary as JSON, and `cargo metadata` is
longer than the clip `brief()` applies, so the document travels parsed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "tasks" / "lang04-acorn-js-to-rust" / "tests" / "behavioural" / "lib"


@pytest.fixture(scope="module")
def lib():
    """lang04's build/audit/vlib modules, importable without a stage image."""
    sys.path.insert(0, str(LIB))
    try:
        import audit
        import build
        import vlib
        yield build, audit, vlib
    finally:
        sys.path.remove(str(LIB))


def _metadata_json(crates: int) -> dict:
    """A `cargo metadata` document shaped like the real one, of a chosen size."""
    return {
        "workspace_members": [f"crate{i} 1.0.0 (path+file:///w/crate{i})"
                              for i in range(crates)],
        "packages": [
            {
                "name": f"crate{i}",
                "version": "1.0.0",
                "dependencies": [],
                "targets": [{"kind": ["lib"], "name": f"crate{i}",
                             "src_path": f"/w/crate{i}/src/lib.rs"}],
                "manifest_path": f"/w/crate{i}/Cargo.toml",
                "description": "a crate in the workspace " * 8,
            }
            for i in range(crates)
        ],
        "workspace_root": "/w",
    }


def _outcome(build, vlib, document: dict, *, returncode: int = 0):
    """A BuildOutcome whose `cargo metadata` returned `document`."""
    stdout = json.dumps(document).encode("utf-8")
    result = vlib.Result(
        argv=["cargo", "metadata", "--format-version", "1", "--no-deps", "--offline"],
        cwd="/w", returncode=returncode, stdout=stdout, stderr=b"", duration=0.1,
    )
    return build.BuildOutcome(build_dir=Path("/w"), prefix=Path("/i"),
                              metadata=result), stdout


def _cross_process(build, outcome):
    """What the next module reads: the summary, through JSON, rehydrated."""
    payload = json.loads(json.dumps(outcome.summary()))
    return build.BuildOutcome.from_summary(payload, Path("/w"))


def _auditor(audit, vlib, tmp_path, outcome, crates: int):
    contract = {"state_b": {"crates": [{"name": f"crate{i}", "version": "1.0.0"}
                                       for i in range(crates)]}}
    return audit.IntegrityAuditor(
        repo=tmp_path / "repo", outcome=outcome, baseline=tmp_path / "baseline",
        contract=contract, scratch=tmp_path / "scratch", log=vlib.Log(None),
    )


@pytest.mark.parametrize("crates", [2, 40])
def test_the_document_survives_whatever_its_size(lib, tmp_path, crates):
    """Small enough to fit the clip, and far too large for it, both arrive."""
    build, audit, vlib = lib
    document = _metadata_json(crates)
    outcome, _ = _outcome(build, vlib, document)
    rehydrated = _cross_process(build, outcome)
    assert _auditor(audit, vlib, tmp_path, rehydrated, crates).metadata() == document


def test_the_large_case_is_the_one_that_used_to_break(lib):
    """The 40-crate document really is past the clip, so the case above is real."""
    build, audit, vlib = lib
    outcome, stdout = _outcome(build, vlib, _metadata_json(40))
    assert len(stdout) > 4000
    clipped = outcome.metadata.brief()["stdout"]
    assert len(clipped) < len(stdout.decode())
    with pytest.raises(ValueError):
        json.loads(clipped)


def test_the_gates_read_the_document_rather_than_the_log(lib, tmp_path):
    """workspace-shape passes on a document larger than the clip."""
    build, audit, vlib = lib
    outcome, _ = _outcome(build, vlib, _metadata_json(40))
    auditor = _auditor(audit, vlib, tmp_path, _cross_process(build, outcome), 40)
    passed, detail, _ = auditor.gate_workspace_shape()
    assert passed, detail


def test_a_failed_command_is_still_a_failure(lib, tmp_path):
    """The fix must not make an absent crate graph look like a present one."""
    build, audit, vlib = lib
    outcome, _ = _outcome(build, vlib, _metadata_json(4), returncode=101)
    auditor = _auditor(audit, vlib, tmp_path, _cross_process(build, outcome), 4)
    assert auditor.metadata() == {}
    passed, detail, _ = auditor.gate_workspace_shape()
    assert not passed
    assert "failed (rc=101)" in detail


def test_a_wrong_version_is_still_a_failure(lib, tmp_path):
    """And the gate still reads the document it was given."""
    build, audit, vlib = lib
    document = _metadata_json(40)
    document["packages"][0]["version"] = "0.0.1"
    outcome, _ = _outcome(build, vlib, document)
    auditor = _auditor(audit, vlib, tmp_path, _cross_process(build, outcome), 40)
    passed, detail, _ = auditor.gate_workspace_shape()
    assert not passed
    assert "crate0: version is 0.0.1" in detail


def test_an_unparseable_document_names_the_verifier(lib, tmp_path):
    """A capture this verifier mangled is reported as its own defect."""
    build, audit, vlib = lib
    result = vlib.Result(argv=["cargo", "metadata"], cwd="/w", returncode=0,
                         stdout=b'{"packages": [', stderr=b"", duration=0.1)
    outcome = build.BuildOutcome(build_dir=Path("/w"), prefix=Path("/i"),
                                 metadata=result)
    auditor = _auditor(audit, vlib, tmp_path, _cross_process(build, outcome), 1)
    assert auditor.metadata() == {}
    passed, detail, _ = auditor.gate_workspace_shape()
    assert not passed
    assert "did not parse" in detail and "not in the submission" in detail
