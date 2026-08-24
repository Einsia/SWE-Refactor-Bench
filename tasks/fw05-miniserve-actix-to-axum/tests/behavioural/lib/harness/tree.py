"""The deterministic serve root, built the same way on both sides.

This is the grader's own copy of the tree builder. The agent image ships the
same logic as ``srb-sample-tree`` so a submission can reproduce the tree while
developing, but the grader never calls that script: a submission could replace
it, and then it would be grading against a tree of the submission's choosing.

``tree_digest`` exists for the same reason in the other direction. The golden
capture records the digest of the tree it served; the graded run recomputes it
and refuses to score if it differs. A capture taken against one tree and a run
taken against another would produce thousands of "failures" that say nothing
about the port, and that failure mode is silent unless something checks.

The pinned mtime is what makes the listing gradeable at all -- see the module
docstring of ``normalize`` for the four kinds of variation that get removed and
why this is not one of them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

#: 2021-06-15T12:34:56Z. Far enough in the past that the humanised mtime column
#: is coarse-grained and stable within a run, and with no sub-second component
#: so it survives every filesystem the image might run on.
FIXED_MTIME = 1623760496


def build(spec: dict, root: Path, *, clean: bool = True) -> None:
    root = Path(root)
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    made: list[Path] = []

    for entry in spec["entries"]:
        target = root / entry["path"]
        kind = entry.get("kind", "file")
        if kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
            made.append(target)
            continue
        if kind == "symlink":
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            os.symlink(entry["target"], target)
            made.append(target)
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        if "bytes_b64" in entry:
            target.write_bytes(base64.b64decode(entry["bytes_b64"]))
        elif "repeat" in entry:
            unit, count = entry["repeat"]["unit"], entry["repeat"]["count"]
            target.write_bytes(unit.encode("utf-8") * count)
        else:
            target.write_text(entry["content"], encoding="utf-8")
        made.append(target)

    # Deepest first, so setting a directory's mtime is not undone by writing a
    # child of it afterwards.
    for path in sorted(set(made) | {root},
                       key=lambda p: len(p.parts), reverse=True):
        os.utime(path, (FIXED_MTIME, FIXED_MTIME), follow_symlinks=False)
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts),
                       reverse=True):
        os.utime(path, (FIXED_MTIME, FIXED_MTIME), follow_symlinks=False)
    os.utime(root, (FIXED_MTIME, FIXED_MTIME))


def load_spec(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tree_digest(root: Path, *, times: bool = True) -> str:
    """A digest over everything the server can observe about the tree.

    Path, kind, size, mtime and content for files; path and target for symlinks;
    path and mtime for directories. Deliberately not a plain content hash: the
    listing renders sizes and timestamps, so a tree with the right bytes and the
    wrong mtimes is the wrong tree.

    With ``times=False`` the mtimes are left out, which is the only form that
    means anything after an upload: creating a file sets its mtime, and its
    parent directory's, to the wall clock. A timed digest over a tree that has
    been written to compares two clocks. An untimed one over the same tree
    compares what the uploads actually did -- which files, where, with which
    bytes -- and that is the contract.
    """
    root = Path(root)
    lines: list[str] = []
    for path in sorted(root.rglob("*"), key=lambda p: str(p)):
        rel = path.relative_to(root).as_posix()
        st = path.lstat()
        if stat.S_ISLNK(st.st_mode):
            lines.append(f"L {rel} -> {os.readlink(path)}")
        elif path.is_dir():
            lines.append(f"D {rel}" + (f" {int(st.st_mtime)}" if times else ""))
        else:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"F {rel} {st.st_size}"
                         + (f" {int(st.st_mtime)}" if times else "")
                         + f" {digest}")
    st = root.lstat()
    lines.append("R ." + (f" {int(st.st_mtime)}" if times else ""))
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
