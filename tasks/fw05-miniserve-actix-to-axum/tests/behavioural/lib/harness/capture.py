"""Write the golden file.

Run at image build time against the State A oracle, and never again: the golden
file that ships in ``tests/golden/`` is the contract, and the graded run only
reads it.

The corpus is played **twice**, against two independently materialised trees,
and the two recordings are diffed. Anything that differs between them cannot be
a contract -- it is per-boot noise that survived normalisation -- and is written
into the golden file as a per-case ``volatile`` list which the graders then skip.
Deriving that set empirically rather than declaring it up front is the whole
point of capturing twice: a hand-written list of "fields that probably vary"
would be guesswork in both directions, hiding real regressions in the fields it
over-covers and producing false failures in the ones it misses.

If the two passes disagree about a case's *status*, that is not noise, it is a
non-deterministic baseline, and the capture fails rather than papering over it.

    python -m harness.capture --repo /workspace/repo \\
        --binary /opt/srb/oracle/miniserve --out golden/responses.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import cli, corpus
from .runner import TREE_SPEC, ServerFailed, play_session

#: Fields inside a recorded response that are compared. Everything else in the
#: record is diagnostic.
COMPARED = ("status", "headers", "header_names", "body_len", "raw_len",
            "decompressed", "body_sha256", "body", "html", "archive",
            "is_text", "humanised_spans", "nonce")


def _flatten(entry: dict, prefix: str = "") -> dict[str, object]:
    """Flatten a recorded response to leaf paths, for a field-level diff."""
    out: dict[str, object] = {}
    for key, value in entry.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(_flatten(value, path + "."))
        elif isinstance(value, list):
            # Lists are compared whole: a listing's entry order is contract, so
            # a diff inside one is a diff of the list.
            out[path] = json.dumps(value, sort_keys=True, default=repr)
        else:
            out[path] = value
    return out


def volatile_fields(first: dict, second: dict) -> list[str]:
    """Leaf paths that differ between two recordings of the same case."""
    a, b = _flatten(first), _flatten(second)
    keys = set(a) | set(b)
    return sorted(k for k in keys if a.get(k, "<absent>") != b.get(k, "<absent>"))


def capture(repo: Path, binary: str, workdir: Path, spec: Path,
            *, passes: int = 2, verbose: bool = True) -> dict:
    sessions = corpus.SESSIONS
    runs: list[dict[str, dict[str, dict]]] = []
    cli_runs: list[dict[str, dict]] = []

    for index in range(passes):
        workdir.mkdir(parents=True, exist_ok=True)
        cli_runs.append({inv.id: cli.run(binary, inv, workdir)
                         for inv in cli.INVOCATIONS})
        # Each pass gets its own workdir, so its sample trees are materialised
        # from scratch. That is what makes inode-derived ETags and any
        # accidental cross-session dependency show up as volatility here rather
        # than as a mystery failure during grading.
        #
        # The names differ in *length*, deliberately. miniserve's 500 pages name
        # the filesystem path they failed to write to, and the harness scrubs the
        # path out of the text -- but a length taken before scrubbing still
        # carries it. Two equal-length workdirs made that invisible to the survey
        # and it only surfaced when the graded run used a shorter path than the
        # capture had. Unequal names mean the survey sees it.
        root = workdir / ("p" * (index + 1) + f"ass{index}")
        recorded: dict[str, dict[str, dict]] = {}
        for position, session in enumerate(sessions, 1):
            if verbose:
                print(f"[pass {index}] {position}/{len(sessions)} {session.id}",
                      file=sys.stderr, flush=True)
            started = time.monotonic()
            try:
                recorded[session.id] = play_session(
                    repo, binary, session, root / session.id, spec)
            except ServerFailed as exc:
                print(f"FAILED to boot session {session.id}: {exc}",
                      file=sys.stderr)
                raise
            if verbose:
                print(f"            {time.monotonic() - started:5.1f}s",
                      file=sys.stderr, flush=True)
        runs.append(recorded)

    golden: dict = {
        "schema": 1,
        "fingerprint": corpus.fingerprint(),
        "summary": corpus.summary(),
        "passes": passes,
        "sessions": {},
        "cli": {},
    }

    inconsistent: list[str] = []

    for inv in cli.INVOCATIONS:
        first = cli_runs[0][inv.id]
        volatile: set[str] = set()
        for other in cli_runs[1:]:
            twin = other[inv.id]
            if first.get("exit") != twin.get("exit"):
                inconsistent.append(
                    f"cli::{inv.id} exit {first.get('exit')} vs "
                    f"{twin.get('exit')}")
            volatile.update(volatile_fields(first, twin))
        record = dict(first)
        record["volatile"] = sorted(volatile)
        record["argv"] = list(inv.argv)
        record["contains"] = list(inv.contains)
        golden["cli"][inv.id] = record
    for session in sessions:
        entries: dict[str, dict] = {}
        first = runs[0][session.id]
        for case_id, entry in first.items():
            volatile: set[str] = set()
            for other in runs[1:]:
                twin = other[session.id].get(case_id)
                if twin is None:
                    inconsistent.append(f"{session.id}::{case_id} missing")
                    continue
                if entry.get("status") != twin.get("status"):
                    inconsistent.append(
                        f"{session.id}::{case_id} status "
                        f"{entry.get('status')} vs {twin.get('status')}")
                volatile.update(volatile_fields(entry, twin))
            record = dict(entry)
            record["volatile"] = sorted(volatile)
            entries[case_id] = record
        golden["sessions"][session.id] = {
            "port": session.port,
            "argv": list(session.argv),
            "cases": entries,
        }

    if inconsistent:
        raise SystemExit("baseline is not deterministic:\n  "
                         + "\n  ".join(inconsistent))
    return golden


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True,
                        help="repository root, for path scrubbing")
    parser.add_argument("--binary", required=True,
                        help="miniserve executable to drive")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--workdir", type=Path,
                        default=Path("/tmp/srb-capture"))
    parser.add_argument("--spec", type=Path, default=TREE_SPEC)
    parser.add_argument("--passes", type=int, default=2)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    golden = capture(args.repo.resolve(), args.binary, args.workdir, args.spec,
                     passes=args.passes, verbose=not args.quiet)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(golden, handle, indent=1, sort_keys=True, ensure_ascii=True)
        handle.write("\n")

    cases = sum(len(s["cases"]) for s in golden["sessions"].values())
    volatile = sum(1 for s in golden["sessions"].values()
                   for c in s["cases"].values() if c["volatile"])
    print(f"wrote {args.out}: {len(golden['sessions'])} sessions, "
          f"{cases} recorded responses, {volatile} with volatile fields, "
          f"{len(golden['cli'])} cli invocations",
          file=sys.stderr)
    missing = {i: r["missing"] for i, r in golden["cli"].items() if r["missing"]}
    if missing:
        # Not fatal, but always a mistake in the corpus rather than in the
        # baseline: a substring the oracle itself does not produce would be
        # graded against every submission and fail all of them.
        raise SystemExit(f"cli substrings absent from the baseline output: "
                         f"{json.dumps(missing, indent=1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
