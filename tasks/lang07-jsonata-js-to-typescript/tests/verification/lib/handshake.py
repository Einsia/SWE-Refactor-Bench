#!/usr/bin/env python3
"""Does this built tree answer at all?

Run by run-candidate.sh against each tree before any candidate sees either of
them.  A tree that fails here is a tree no candidate can say anything about, and
the distinction matters for who gets blamed: a reference that cannot answer is a
broken harness (exit 70), a submission that cannot answer is a broken submission
(exit 73), and neither is a finding about the migration.

It goes through `srbjsonata.op("hello")` rather than writing a line to the probe
itself, so it also proves the thing every candidate depends on: that the helper
imports, that `SRB_PROBE_ARGV` points at something runnable, and that a response
can be decoded.  A handshake that bypassed the helper could pass while every
candidate in the round failed to start.

Checks, in order of how loudly they fail:

  1. the process runs and writes one parseable response
  2. `ok` is true and `op` is echoed as `hello`
  3. `protocol` is exactly `jsonata-probe/1`
  4. `ops`, `impls` and `engines` are the declared vocabularies, sorted

4 is not pedantry.  A submission that dropped a fixture would answer every
candidate that never names it and fail the ones that do, so a round could spend
its whole budget discovering a gap that is visible in one request.  Better to
report it here, once, in a sentence.
"""
from __future__ import annotations

import sys

import srbjsonata as R


def main() -> int:
    try:
        rec = R.op("hello")
    except AssertionError as exc:  # no response at all
        print(f"handshake: no response: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # could not even spawn
        print(f"handshake: could not run the probe: {exc!r}", file=sys.stderr)
        return 1

    problems: list[str] = []
    if not rec.ok:
        problems.append(f"ok is not true: {rec.text()[:200]}")
    if rec.op != "hello":
        problems.append(f"op echoed as {rec.op!r}, expected 'hello'")

    result = rec.result if isinstance(rec.result, dict) else {}
    if result.get("protocol") != R.PROTOCOL:
        problems.append(
            f"protocol is {result.get('protocol')!r}, expected {R.PROTOCOL!r}")
    for key, expected in (("ops", R.OPS), ("impls", R.IMPLS),
                          ("engines", R.ENGINES)):
        got = result.get(key)
        if got != sorted(expected):
            missing = sorted(set(expected) - set(got or []))
            extra = sorted(set(got or []) - set(expected))
            detail = ", ".join(
                part for part in (
                    f"missing {missing}" if missing else "",
                    f"unexpected {extra}" if extra else "",
                    "" if (missing or extra) else f"out of order: {got}",
                ) if part)
            problems.append(f"{key}: {detail}")

    if problems:
        for problem in problems:
            print(f"handshake: {problem}", file=sys.stderr)
        return 1

    print(f"handshake: {R.TARGET_NAME} answers {R.PROTOCOL}; "
          f"{len(R.OPS)} ops, {len(R.IMPLS)} impls, {len(R.ENGINES)} engines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
