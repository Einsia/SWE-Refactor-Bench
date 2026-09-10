"""Record State A's answers -- how ``data/recorded-responses.json`` was made.

This is not run during grading. It ships because the alternative is a 4.6 MB data
file with no reproducible provenance: if the request list in ``exchanges.py`` is
ever edited, the fingerprint check refuses to grade, and the message tells the
maintainer to run this. It has to exist for that instruction to be true.

    python -m harness.capture --repo /path/to/state-a --out data/recorded-responses.json

Run three times against three independently started servers, keeping only what
all three agreed on. A field lands on the volatile list because State A itself
disagreed with itself about it -- never because it looked risky. Two runs was not
enough: a second-precision timestamp agreed by luck across one pair and became an
expectation that failed later, so a disagreement is masked rather than dropped and
every derived field is computed from the masked form.

The launcher is a repository-relative script, so the same code records from a built
State A and from a candidate State B, which is how a rewrite is checked against
the same contract the submission faces.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from . import exchanges
from .runner import play_all


def _diff_entry(a: dict, b: dict) -> dict:
    """What two recordings of the same request disagreed about."""
    out: dict[str, object] = {}
    if a["status"] != b["status"]:
        out["status"] = [a["status"], b["status"]]

    header_names = set(a["headers"]) | set(b["headers"])
    unstable_headers = sorted(
        name for name in header_names
        if a["headers"].get(name) != b["headers"].get(name)
    )
    if unstable_headers:
        out["headers"] = unstable_headers

    if a["body"] != b["body"]:
        out["body"] = True
        if a["json_shape"] != b["json_shape"]:
            out["json_shape"] = True
        if a["json_masked"] != b["json_masked"]:
            out["json_masked"] = True
    return out


def capture(repo: Path, launcher: str, out_path: Path, runs: int = 3) -> dict:
    recordings = []
    for run in range(runs):
        with tempfile.TemporaryDirectory(prefix=f"srb-capture-{run}-") as tmp:
            print(f"[capture] run {run + 1}/{runs}", file=sys.stderr)

            def announce(session, _run=run):
                print(f"[capture]   run {_run + 1} session {session.id} "
                      f"({len(session.cases)} cases)", file=sys.stderr)

            recordings.append(play_all(
                repo=repo,
                launcher=launcher,
                sessions=exchanges.SESSIONS,
                workdir=Path(tmp),
                seed_source=repo / "db.seed.json",
                routes=repo / "routes.json",
                on_session=announce,
            ))

    first, *rest = recordings
    volatile: dict[str, dict] = {}
    for session_id, cases in first.items():
        for case_id, entry in cases.items():
            merged: dict[str, object] = {}
            for other in rest:
                diff = _diff_entry(entry, other[session_id][case_id])
                for key, value in diff.items():
                    if key == "headers":
                        merged.setdefault("headers", [])
                        merged["headers"] = sorted(
                            set(merged["headers"]) | set(value))
                    else:
                        merged[key] = value
            if merged:
                volatile[exchanges.case_key(session_id, case_id)] = merged

    recorded = {
        "schema": 1,
        "captured_from": launcher,
        # Digest of the requests, not just their ids: an edited request cannot
        # silently grade against an answer recorded for a different one.
        "exchanges_fingerprint": exchanges.fingerprint(),
        "runs": runs,
        "session_count": len(exchanges.SESSIONS),
        "case_count": sum(len(s.cases) for s in exchanges.SESSIONS),
        "sessions": {
            s.id: {
                "argv": list(s.argv),
                "seed": s.seed,
                "mutating": s.mutating,
                "case_ids": [c.id for c in s.cases],
                "note": s.note,
            }
            for s in exchanges.SESSIONS
        },
        "responses": first,
        # Determined empirically: independent runs of State A disagreed here.
        "volatile": volatile,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(recorded, indent=1, sort_keys=True))
    return recorded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--launcher", default="serve.sh")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    recorded = capture(args.repo, args.launcher, args.out, args.runs)

    total = recorded["case_count"]
    print(f"\n[capture] {total} exchanges across {recorded['session_count']} "
          f"sessions -> {args.out}", file=sys.stderr)
    print(f"[capture] volatile entries: {len(recorded['volatile'])}",
          file=sys.stderr)
    for key, diff in sorted(recorded["volatile"].items()):
        print(f"[capture]   {key}: {sorted(diff)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
