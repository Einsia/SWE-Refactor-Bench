#!/usr/bin/env python3
"""Grade a lang06 submission.

Everything about how grading works lives in `tests/evaluation.toml` and in the
shared harness under `infra/swerefactor/`.  This file exists so Harbor has a
`score.py` to run, and so running it by hand needs no arguments.

The ladder is the benchmark's, not this task's: an audit review that can read
both trees and execute neither, then a behavioural suite worth 40, paid whole or
not at all, then -- only if it was paid in full -- six adversaries worth 10 each.
`harbor.main` applies it and writes `reward.json`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The harness is vendored into every stage image at /opt/swerefactor.  Outside a
# stage image -- authoring, `swerefactor validate`, a local dry run -- it is the
# checkout's own infra/ directory.  Nothing is added to sys.path when swerefactor is
# already imported, so a caller that set this up keeps its own copy.
if "swerefactor" not in sys.modules:
    for candidate in (Path(os.environ.get("SRB_INFRA", "/opt/swerefactor")),
                      HERE.parent.parent / "infra"):
        if (candidate / "swerefactor" / "__init__.py").exists():
            sys.path.insert(0, str(candidate))
            break

try:
    from swerefactor import harbor
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    print(f"cannot import the swerefactor harness: {exc}", file=sys.stderr)
    print("expected it on PYTHONPATH, at $SRB_INFRA, or in infra/ of the "
          "repository", file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a.startswith("--task-dir") for a in argv):
        argv += ["--task-dir", str(HERE)]
    raise SystemExit(harbor.main(argv))
