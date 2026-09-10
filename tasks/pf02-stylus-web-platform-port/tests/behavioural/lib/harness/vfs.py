"""Builds the in-memory filesystem the core is given inside the realm.

Contents come from State A's `test/` tree plus the submission's own built-in
`.styl` library.  Bytes are base64'd because that tree includes real PNG, GIF and
JPEG files that `image-size()` has to parse -- the port has to handle binary
reads, so the harness must be able to deliver them.
"""
from __future__ import annotations

import base64
import functools
from pathlib import Path

from . import layout

#: Extensions worth mounting.  Everything a case reads, nothing else.
MOUNT_SUFFIXES = frozenset(
    {".styl", ".css", ".json", ".png", ".gif", ".jpeg", ".jpg", ".svg", ".map", ".deps", ".txt"}
)


def text(files: dict[str, str], *, runtime: bool = True) -> dict[str, str]:
    """Encode a literal `{path: source}` map into a mountable payload.

    Every value in the payload is base64 -- `js/sandbox.mjs` decodes strings
    unconditionally -- so a suite that writes its sources out by hand has to come
    through here. Passing raw text instead produces `atob: invalid character` in
    the load phase, which reads as a submission fault and is not one.

    The submission's built-in library is mounted alongside by default, matching
    `subset()`. Every compile loads it -- `Renderer` imports `index.styl` before it
    looks at the input -- so a payload of just the test's own sources fails for a
    reason that has nothing to do with what the test is measuring. Pass
    `runtime=False` only when the empty runtime root *is* the measurement.
    """
    out = {p: base64.b64encode(s.encode("utf8")).decode("ascii") for p, s in files.items()}
    if runtime:
        root = find_runtime_root()
        if root is not None:
            # Test-supplied paths win: a case may deliberately shadow a builtin.
            out = {**runtime_files(root), **out}
    return out


def _walk(real_root: Path, virt_root: str, out: dict[str, str]) -> None:
    for p in sorted(real_root.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in MOUNT_SUFFIXES:
            continue
        rel = p.relative_to(real_root).as_posix()
        out[f"{virt_root.rstrip('/')}/{rel}"] = base64.b64encode(p.read_bytes()).decode("ascii")


@functools.lru_cache(maxsize=1)
def project_files() -> dict[str, str]:
    """State A's `test/` tree, mounted at /proj."""
    out: dict[str, str] = {}
    _walk(layout.INPUTS, f"{layout.VPROJ}/test", out)
    return out


def runtime_files(runtime_real: Path) -> dict[str, str]:
    """The built-in `.styl` library, mounted at /runtime.

    Read from the submission, because §1.4 makes shipping it part of the port.
    A submission that never ships it gets an empty runtime root and will fail to
    compile anything that uses a built-in -- which is the honest outcome.
    """
    out: dict[str, str] = {}
    if runtime_real.is_dir():
        _walk(runtime_real, layout.VRUNTIME, out)
    return out


def find_runtime_root() -> Path | None:
    """Locate the tree's own built-in `.styl` library.

    §1.4 says to ship it under `src/core`; it does not dictate the directory
    name.  The marker is `index.styl`, the file State A loads via __dirname.

    In a tree that has not been ported (`layout.face()`) the library is still
    where upstream keeps it, so that is what is returned.  It matters for the same
    reason the mount exists at all: `linenos` and `firebug` write the path of every
    file they compile into the CSS, the built-in library is one of those files, and
    a comparison that saw `/runtime/index.styl` on one side and a real path on the
    other would fail on the prefix rather than on the port.
    """
    if layout.is_pre_migration():
        return layout.LEGACY_RUNTIME if layout.LEGACY_RUNTIME.is_dir() else None
    if not layout.CORE_ROOT.is_dir():
        return None
    candidates = sorted(layout.CORE_ROOT.rglob("index.styl"))
    if not candidates:
        return None
    # Shallowest wins, so a copy nested deeper cannot shadow the real one.
    candidates.sort(key=lambda p: (len(p.parts), str(p)))
    return candidates[0].parent


@functools.lru_cache(maxsize=1)
def full() -> dict[str, str]:
    """The complete VFS: State A's inputs at /proj, the submission's built-in
    `.styl` library at /runtime."""
    out = dict(project_files())
    root = find_runtime_root()
    if root is not None:
        out.update(runtime_files(root))
    return out


@functools.lru_cache(maxsize=1)
def runtime_root_virtual() -> str:
    return layout.VRUNTIME


def subset(prefixes: tuple[str, ...]) -> dict[str, str]:
    """A slice of the VFS, for tests that want to assert on read sequences."""
    everything = full()
    keep = {}
    for path, data in everything.items():
        if path.startswith(layout.VRUNTIME) or any(path.startswith(p) for p in prefixes):
            keep[path] = data
    return keep
