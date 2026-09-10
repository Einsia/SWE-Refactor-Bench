#!/usr/bin/env python3
"""Interpreter tripwire installed ahead of every JavaScript runtime during grading.

The claim under test is that acorn is now a Rust program.  A submission that
kept the JavaScript and shelled out to `node` would pass every behavioural
comparison in this verifier by construction -- it would *be* the reference -- so
the possibility is removed rather than detected after the fact: `node`, `npm`,
`npx`, `deno`, `bun`, `qjs` and friends resolve to this program, which refuses
and records.

Where the C-compiler shim in the sibling C-to-Rust task has to allow link-only
invocations (rustc needs a C compiler as its linker driver, and forbidding that
would make the task impossible), this shim has no such exemption.  A Rust build
of a Rust parser needs no JavaScript interpreter for any purpose, at any stage.
Every invocation is refused.

The ledger at $JSSHIM_LOG is the evidence the audit gates read.  This
program only enforces and records; it decides nothing about the score.
"""

from __future__ import annotations

import json
import os
import sys
import time

LEDGER = os.environ.get("JSSHIM_LOG", "")
# Kept small and appended atomically: several build jobs can run at once, and a
# torn line would make the ledger unparseable exactly when it matters.
MAX_ARGV_CHARS = 4000


def record(entry: dict) -> None:
    if not LEDGER:
        return
    entry["at"] = round(time.time(), 3)
    line = json.dumps(entry, sort_keys=True)[:MAX_ARGV_CHARS] + "\n"
    try:
        # O_APPEND on a single write under the pipe-buffer size is atomic on
        # Linux, which is what keeps concurrent build jobs from interleaving.
        fd = os.open(LEDGER, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8", "replace"))
        finally:
            os.close(fd)
    except OSError:
        # A missing ledger must not turn into a build that quietly succeeds:
        # the refusal below happens either way.
        pass


def main(argv: list[str]) -> int:
    invoked = os.path.basename(argv[0]) if argv else "unknown"
    cwd = ""
    try:
        cwd = os.getcwd()
    except OSError:
        pass
    record({
        "tool": invoked,
        "argv": [str(a) for a in argv[1:60]],
        "cwd": cwd,
        "ppid": os.getppid(),
        "decision": "refused",
    })
    sys.stderr.write(
        f"jsshim: refusing to run `{invoked}`.\n"
        f"jsshim: this task is graded on a Rust implementation; no JavaScript\n"
        f"jsshim: interpreter is available to the build or to acorn-probe.\n"
        f"jsshim: the invocation has been recorded.\n"
    )
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv))
