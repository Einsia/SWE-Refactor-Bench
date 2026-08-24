#!/usr/bin/env python3
"""Python interpreter shim installed ahead of the real interpreters during grading.

The migration claim under test is that the Python implementation is gone -- not
hidden, not vendored, not shelled out to.  Rather than inferring that from the
build log, the verifier makes it structurally impossible: `python3`, `python`,
`python3.11`, `pypy3` and the rest resolve to this shim, which refuses to run
anything and records every invocation it saw.

This shim is stricter than its C counterpart in the sibling tasks, and can afford
to be.  A CMake build legitimately compiles probe sources, so the C shim has to
tell a configure probe from a repository compile.  A Go build has no legitimate
reason to run Python at all: `go build`, `go vet`, `go install` and `gofmt` are
one self-contained toolchain, and the module closure is the standard library plus
itself.  So there is no allowed case to carve out.  Every invocation is refused.

What that catches, concretely:

* a Makefile or `go:generate` line that regenerates the keyword tables from the
  Python source at build time -- the tables would be correct, and the repository
  would still need Python to build;
* a `cmd/sqlformat` that execs an interpreter, which would fail here and also
  fail the binary-level gates;
* a build script that calls python for something incidental, which is not a
  migration failure in spirit but is a dependency the contract says is gone.

The refusal is loud: exit 97, a message naming the shim on stderr, and a JSONL
record appended to $PYSHIM_LOG.  Exit 97 rather than 1 or 127 so the cause is
identifiable in a build log that mentions nothing else -- 127 would read as
"command not found", which is the one thing this is not.

The ledger is the evidence; this program only refuses and records.  The gate that
reads it is `guard-python-shim-clean`, and it is mandatory: a submission whose
graded build reached for an interpreter does not get a behavioural score.

One implementation note, because it is a trap the C shims never hit.  This file's
shebang below is a placeholder: `#!/usr/bin/env python3` cannot be used, because
the shim is installed *as* `python3` at the front of PATH and would resolve to
itself, recursing until the process table gives out.  build.py rewrites the first
line to name the real interpreter by absolute path when it materializes the shim,
so the copy that runs never performs a PATH lookup to find its own interpreter.
Running this file straight from the source tree works too -- an absolute-path
shebang is only required for the installed copy.
"""

from __future__ import annotations

import json
import os
import sys
import time

REFUSAL_EXIT = 97


def ledger_write(record: dict) -> None:
    """Append one decision to the ledger, never failing the caller.

    A shim that crashed while recording would turn an audit finding into a
    build error, and the two must not be confusable: the finding is the point,
    and it has to survive the recording being impossible.
    """
    path = os.environ.get("PYSHIM_LOG")
    if not path:
        return
    try:
        line = json.dumps(record, sort_keys=True)
    except (TypeError, ValueError):
        line = json.dumps({"tool": record.get("tool", "?"),
                           "note": "record not serializable"})
    try:
        # Opened per call and appended: several build steps can invoke the shim
        # concurrently under `go build`'s parallelism, and O_APPEND on a single
        # short write is what keeps their lines from interleaving.
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def main(argv: list[str]) -> int:
    tool = os.path.basename(argv[0]) if argv else "python"
    args = argv[1:]

    record = {
        "tool": tool,
        "argv": args[:64],
        "argc": len(args),
        "cwd": os.getcwd(),
        "time": time.time(),
        "decision": "refused",
        # The build step that reached for Python is usually more informative than
        # the argv, and `go build` does not say who invoked a subprocess.  These
        # three are what the toolchain sets when it runs one.
        "caller": {
            "make": os.environ.get("MAKELEVEL"),
            "go_generate": os.environ.get("GOFILE"),
            "package": os.environ.get("GOPACKAGE"),
        },
    }
    ledger_write(record)

    sys.stderr.write(
        f"pyshim: refusing to run {tool}: this repository is graded as a Go "
        f"module and must build without a Python interpreter\n"
    )
    if args:
        sys.stderr.write(f"pyshim: argv was {args[:8]}\n")
    return REFUSAL_EXIT


if __name__ == "__main__":
    sys.exit(main(sys.argv))
