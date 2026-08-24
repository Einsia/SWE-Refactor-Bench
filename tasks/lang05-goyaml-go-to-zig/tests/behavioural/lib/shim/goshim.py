#!/usr/bin/env python3
"""Go-toolchain tripwire, installed ahead of every Go binary during grading.

The claim under test is that go-yaml is now a Zig program.  A submission that
kept the Go and shelled out to it would pass every behavioural comparison in this
verifier by construction -- it would *be* the reference -- so the possibility is
removed rather than detected after the fact: `go`, `gofmt`, `godoc` and friends
resolve to this program, which refuses and records.

The verifier image has no Go toolchain at all; these names exist only as links to
this script.  That is belt and braces on purpose.  A submission whose build
invokes `go` gets a recorded refusal naming the tool and the arguments, which is
a specific finding in the report, rather than a bare "command not found" that
reads like a broken container.

There is no exemption.  A Zig build of a YAML parser needs no Go toolchain for
any purpose, at any stage -- unlike the C-compiler shim in the C-to-Rust sibling
task, which has to allow link-only invocations because rustc uses cc as its
linker driver.  Every invocation here is refused.

The ledger at $GOSHIM_LOG is the evidence the audit gates read.  This program
only enforces and records; it decides nothing about the score.
"""

from __future__ import annotations

import json
import os
import sys
import time

LEDGER = os.environ.get("GOSHIM_LOG", "")
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
        # A missing ledger must not turn into a build that quietly succeeds: the
        # refusal below happens either way.
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
        f"goshim: refusing to run `{invoked}`.\n"
        f"goshim: this task is graded on a Zig implementation; no Go toolchain\n"
        f"goshim: is available to the build or to yaml-probe.\n"
        f"goshim: the invocation has been recorded.\n"
    )
    return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv))
