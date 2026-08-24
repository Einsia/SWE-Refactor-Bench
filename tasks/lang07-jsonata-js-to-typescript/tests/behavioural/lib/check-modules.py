#!/usr/bin/env python3
"""Assert that every module `suite.toml` declares has a working entry point.

Run by the verifier Dockerfile, and runnable from the host against the suite
directory:

    python3 lib/check-modules.py [suite-dir]

Three failures, all of which are silent until grading time:

1.  A declared module with no `modules/<id>/run.sh`.  The behavioural runner
    reports "no runner" for that module, which the report shows as a module the
    submission failed.  A submission that has nothing to do with it is graded
    down for it.

2.  A `run.sh` that is a symlink to nowhere.  All fifteen are symlinks to
    `lib/run-module.sh`, and a symlink survives `git clone` but not every way of
    moving a tree: `git archive` without `--format=tar` dereferences, a copy onto
    a filesystem without symlinks silently makes an empty file, and a Docker
    build context that excluded `lib/` would leave fifteen dangling links.
    `os.path.exists` follows the link, so a dangling one is caught here.

3.  A `run.sh` that resolves somewhere *else*.  This is the one worth naming: a
    module with its own real script is legitimate -- `run-module.sh` says so -- but
    it is a decision, and a decision made by accident (an editor writing a copy
    instead of a link) means one module stops picking up changes to the engine.
    So a divergence is reported rather than failed, and the exit code counts only
    the first two.

The module list comes from `suite.toml` rather than from listing `modules/`,
because the failure being checked is a *declared* module having no entry point.
Deriving the list from the directory would make the check vacuous: it would
compare the directory against itself and pass on a suite missing four modules.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ID_RE = re.compile(r'^\s*id\s*=\s*"([^"]+)"', re.M)
CANONICAL = "lib/run-module.sh"


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    suite = root / "suite.toml"
    if not suite.is_file():
        print(f"check-modules: {suite} is missing", file=sys.stderr)
        return 1

    ids = ID_RE.findall(suite.read_text(encoding="utf-8"))
    if not ids:
        print(f"check-modules: {suite} declares no module ids; the id regex found "
              f"nothing, so either the suite is empty or its format changed",
              file=sys.stderr)
        return 1

    canonical = (root / CANONICAL).resolve()
    problems: list[str] = []
    notes: list[str] = []
    linked = 0
    for module_id in ids:
        run = root / "modules" / module_id / "run.sh"
        if not run.exists():  # follows symlinks: a dangling link lands here
            if run.is_symlink():
                problems.append(f"modules/{module_id}/run.sh is a symlink to "
                                f"{run.readlink()}, which does not exist")
            else:
                problems.append(f"modules/{module_id}/run.sh is missing, so the "
                                f"runner reports this module as failed for every "
                                f"submission")
            continue
        target = run.resolve()
        if target == canonical:
            linked += 1
        else:
            notes.append(f"modules/{module_id}/run.sh resolves to "
                         f"{target} rather than {CANONICAL}; if that is "
                         f"deliberate it stops tracking changes to the engine")

    for note in notes:
        print(f"check-modules: note: {note}")
    if problems:
        print(f"check-modules: {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"check-modules: {len(ids)} declared module(s), {linked} resolving to "
          f"{CANONICAL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
