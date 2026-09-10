"""Three ways to ask the same question, plus the oracle that answers it right.

Every runner takes a list of ops in the shared vocabulary and returns
`{op_id: Result}`.  Batching matters: a corpus sweep is one process, not 356.

Results are cached per batch signature so a suite can ask for the same sweep in
several tests without paying for it twice.

**Which artifact a runner drives is decided here, not by the module calling it.**
`layout.face()` reads the submitted tree and says whether it presents the ported
shape or the shape it had before, and `sandbox()` and `node_adapter()` dispatch on
that answer.  A module asks its question once and gets it answered by whichever
face the tree actually has; none of them grows a second code path, and none of
them may decide for itself which one to ask.

The pre-migration face is answered by `js/oracle.cjs` with `instrument: true` --
the same driver, the same op vocabulary, the same virtual mounts, pointed at the
staged tree instead of at State A.  That is what makes the comparison mean
something in either direction: a behavioural row is the identical question either
way, and the only difference is which package answered it.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import layout, vfs


@dataclass(frozen=True)
class Result:
    ok: bool
    value: dict | None = None
    error: dict | None = None
    phase: str | None = None
    #: Console lines emitted by this op alone, as `"<level> <formatted text>"`.
    #: The only observable output of `p()` and `warn()`, which return null.
    console: tuple[str, ...] = ()

    @property
    def css(self) -> str | None:
        return None if self.value is None else self.value.get("css")


@dataclass(frozen=True)
class Batch:
    results: dict[str, Result]
    load_error: dict | None = None
    module_count: int = 0
    access_log: tuple[str, ...] = ()
    console_log: tuple[str, ...] = ()
    driver_stderr: str = ""

    def get(self, op_id: str) -> Result:
        return self.results.get(
            op_id, Result(ok=False, error={"message": f"driver returned no result for {op_id}"})
        )


class DriverFailure(RuntimeError):
    """The driver process itself did not produce readable output."""


def _signature(kind: str, payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return f"{kind}-{hashlib.sha256(blob).hexdigest()[:16]}"


def _run(argv: list[str], sig: str, payload: dict, cwd: Path | None = None) -> tuple[dict, str]:
    layout.ensure_dirs()
    job_path = layout.WORK / f"{sig}.job.json"
    out_path = layout.WORK / f"{sig}.out.json"
    job_path.write_text(json.dumps(payload), encoding="utf8")
    if out_path.exists():
        out_path.unlink()

    proc = subprocess.run(
        argv + [str(job_path), str(out_path)],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=layout.DRIVER_TIMEOUT_SEC,
    )
    if not out_path.exists():
        raise DriverFailure(
            f"driver produced no output\nargv: {' '.join(argv)}\n"
            f"exit: {proc.returncode}\nstdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    try:
        data = json.loads(out_path.read_text(encoding="utf8"))
    except json.JSONDecodeError as exc:
        raise DriverFailure(f"driver output was not JSON: {exc}\nstderr:\n{proc.stderr[-4000:]}") from exc
    return data, proc.stderr


def _to_batch(data: dict, stderr: str) -> Batch:
    def _console(r: dict) -> tuple[str, ...]:
        # Drivers attach it inside `value` on success and beside `error` on
        # failure, since a probe that threw may still have printed first.
        lines = (r.get("value") or {}).get("console") if r.get("ok") else r.get("console")
        return tuple(lines or ())

    results = {
        r["id"]: Result(
            ok=bool(r.get("ok")),
            value=r.get("value"),
            error=r.get("error"),
            phase=r.get("phase"),
            console=_console(r),
        )
        for r in data.get("results", [])
    }
    return Batch(
        results=results,
        load_error=data.get("loadError"),
        module_count=int(data.get("moduleCount") or 0),
        access_log=tuple(data.get("accessLog") or ()),
        console_log=tuple(data.get("consoleLog") or ()),
        driver_stderr=stderr,
    )


# --------------------------------------------------------------------- oracle

_oracle_cache: dict[str, Batch] = {}


def default_mounts() -> dict[str, str]:
    """The two virtual roots, mapped onto the oracle's real directories.

    `/runtime` is listed because upstream loads its built-in `.styl` library from
    `lib/functions` via `__dirname`, and the port loads the same library from
    `platform.runtimeRoot`. Options that write source paths into the output
    (`linenos`, `firebug`) therefore name that file, and without this mount the
    oracle would say `/proj/lib/functions/index.styl` where the submission
    correctly says `/runtime/index.styl` -- a mismatch caused by the harness, not
    by the port.
    """
    return {
        layout.VRUNTIME: str(layout.STATE_A / "lib" / "functions"),
        layout.VPROJ: str(layout.STATE_A),
    }


def oracle(ops: list[dict], *, mounts: dict[str, str] | None = None) -> Batch:
    """Expectations from pristine State A.

    Runs with cwd set to the immutable oracle tree, so relative paths behave the
    way upstream's own suite expects.
    """
    payload = {
        "stateARoot": str(layout.STATE_A),
        "mounts": mounts if mounts is not None else default_mounts(),
        "ops": ops,
    }
    sig = _signature("oracle", payload)
    if sig not in _oracle_cache:
        data, stderr = _run([layout.NODE, str(layout.JS / "oracle.cjs")], sig, payload, cwd=layout.STATE_A)
        _oracle_cache[sig] = _to_batch(data, stderr)
    return _oracle_cache[sig]


# ------------------------------------------------------- the pre-migration face
#
# A tree that has not been ported presents one CommonJS package, which is what
# `js/oracle.cjs` drives.  Two things have to be arranged before it can answer a
# question posed in virtual paths: the payload the realm would have been handed has
# to exist as real files, and the mounts have to name them.

_premigration_cache: dict[str, Batch] = {}


def _runtime_mount() -> dict[str, str]:
    """`/runtime` onto the tree's own built-in `.styl` library, when it has one.

    Always the real directory, never a materialised copy, and this is the subtle
    part.  Upstream finds that library with `__dirname` rather than through
    anything the harness controls, so the mount is not how the bytes are reached --
    it is how the path is *reported*. `linenos` and `firebug` write the path of
    every file they compile into the CSS, the library is one of those files, and a
    mount pointing anywhere else would leave a real path in the output where the
    oracle wrote `/runtime/index.styl`.
    """
    root = vfs.find_runtime_root()
    return {layout.VRUNTIME: str(root)} if root is not None else {}


def _materialise(files: dict[str, str]) -> dict[str, str]:
    """Write a VFS payload out as real files; return the mounts that name them.

    Keyed by the payload's own signature and rebuilt when absent, so the several
    batches a module runs over the same trees pay for one directory.  Entries under
    `/runtime` are dropped: `_runtime_mount` supplies that root from the tree
    itself, and a second copy of the library on disk would be the one the mount did
    not point at.
    """
    payload = {p: d for p, d in files.items() if not p.startswith(layout.VRUNTIME)}
    root = layout.WORK / "premigration-vfs" / _signature("vfs", payload)
    prefixes = sorted({p.split("/")[1] for p in payload if p.startswith("/") and "/" in p[1:]})

    if not root.is_dir():
        staging = root.with_name(f"{root.name}.partial")
        if staging.exists():
            shutil.rmtree(staging)
        for vpath, b64 in payload.items():
            dest = staging / vpath.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(b64))
        # Renamed into place only once complete, so a crashed materialisation
        # cannot be reused as though it were the whole tree.
        staging.rename(root)

    mounts = {f"/{name}": str(root / name) for name in prefixes}
    mounts.update(_runtime_mount())
    return mounts


def _premigration_mounts(files: dict[str, str] | None) -> dict[str, str]:
    """What the pre-migration face should substitute for each virtual prefix.

    The corpus payload is `vfs.full()`: State A's `test/` tree at `/proj/test` plus
    the tree's own built-in library at `/runtime`.  Both already exist as real
    directories, so that case is mounted rather than copied -- 356 cases and the
    binary assets `image-size()` parses, written out per batch, would be the
    slowest thing in the stage for no gain.  Anything else is a payload some module
    built by hand, and gets written out.
    """
    if files is None or files == vfs.full():
        return {**_runtime_mount(), layout.VPROJ: str(layout.STATE_A)}
    return _materialise(files)


def _premigration(
    ops: list[dict],
    *,
    files: dict[str, str] | None = None,
    read_dir_order: str = "natural",
    cwd_virtual: str = layout.VPROJ,
) -> Batch:
    """The submitted tree's own CommonJS package, over the shared op vocabulary."""
    mounts = _premigration_mounts(files)
    payload = {
        "stateARoot": str(layout.REPO),
        "mounts": mounts,
        "instrument": True,
        "readDirOrder": read_dir_order,
        "ops": ops,
    }
    sig = _signature("premigration", payload)
    if sig not in _premigration_cache:
        # cwd is whatever `cwd_virtual` names, devirtualised.  The realm gives the
        # core a virtual cwd and the oracle runs in the real directory that mount
        # stands for; a relative path in an op has to mean the same thing on both
        # sides, so this face resolves the same name the same way.
        cwd = mounts.get(cwd_virtual) or str(layout.REPO)
        data, stderr = _run([layout.NODE, str(layout.JS / "oracle.cjs")], sig, payload, cwd=Path(cwd))
        _premigration_cache[sig] = _to_batch(data, stderr)
    return _premigration_cache[sig]


# --------------------------------------------------------------- sandboxed core

_sandbox_cache: dict[str, Batch] = {}


def _check_encoded(files: dict[str, str]) -> None:
    """Reject a raw-text VFS payload here rather than in the load phase.

    `js/sandbox.mjs` decodes every string value unconditionally, so a suite that
    hands it plain source gets `atob: invalid character` while the core is being
    loaded -- which looks exactly like a submission that cannot be imported, and
    fails an audit row for a fault in the harness. Three suites had this bug.

    Raised as an error, not asserted: it is a mistake in the test, and it should
    stop the suite that made it instead of scoring a submission on it.
    """
    for path, value in files.items():
        if not isinstance(value, str):
            raise TypeError(
                f"VFS payload for {path!r} is {type(value).__name__}, not a base64 str. "
                "Build literal sources with vfs.text()."
            )
        try:
            base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"VFS payload for {path!r} is not valid base64 ({exc}). Literal sources "
                f"must go through vfs.text(); got {value[:60]!r}"
            ) from None


def sandbox(
    ops: list[dict],
    *,
    sync: bool = False,
    files: dict[str, str] | None = None,
    read_dir_order: str = "natural",
    cwd_virtual: str = layout.VPROJ,
) -> Batch:
    """The submission's compiler core: inside the web realm once it has one.

    `sync` is meaningful only to the realm.  It says which shape of platform the
    core was handed, and the pre-migration package has no platform to hand -- it
    reads the disk synchronously and always did, which is the behaviour §1.3's
    `renderSync()` has to keep.  So the flag is accepted and inert on that face,
    and a check about the *refusal* is not a behavioural row there; it is charged
    on that face regardless, for the reason in `srbstylus`'s module docstring.
    """
    if files is not None:
        _check_encoded(files)
    if layout.is_pre_migration():
        return _premigration(
            ops, files=files, read_dir_order=read_dir_order, cwd_virtual=cwd_virtual
        )
    payload = {
        "coreRoot": str(layout.CORE_ROOT),
        "platform": {
            "sync": sync,
            "cwd": cwd_virtual,
            "runtimeRoot": layout.VRUNTIME,
            "readDirOrder": read_dir_order,
        },
        "vfs": files if files is not None else vfs.full(),
        "ops": ops,
    }
    sig = _signature("sandbox", payload)
    if sig not in _sandbox_cache:
        data, stderr = _run(
            [layout.NODE, *layout.NODE_VM_FLAGS, str(layout.JS / "sandbox.mjs")], sig, payload
        )
        _sandbox_cache[sig] = _to_batch(data, stderr)
    return _sandbox_cache[sig]


# ------------------------------------------------------------- node adapter

_node_cache: dict[str, Batch] = {}


def node_adapter(ops: list[dict]) -> Batch:
    """The submission's Node-facing API: `src/node` once it has one.

    Before the port that API is the package's own CommonJS entry, which is not an
    approximation of what §1.5 asks for -- it is the thing §1.5 asks to be
    preserved.  Every row here is `render`, `deps`, `convertCSS` or the surface
    they hang off, and those are the same questions either way.
    """
    if layout.is_pre_migration():
        return _premigration(ops)
    payload = {
        "repoRoot": str(layout.REPO),
        "entry": str(layout.NODE_ENTRY),
        "mounts": {layout.VPROJ: str(layout.STATE_A)},
        "ops": ops,
    }
    sig = _signature("nodeapi", payload)
    if sig not in _node_cache:
        data, stderr = _run(
            [layout.NODE, *layout.NODE_VM_FLAGS, str(layout.JS / "nodeapi.mjs")],
            sig,
            payload,
            cwd=layout.REPO,
        )
        _node_cache[sig] = _to_batch(data, stderr)
    return _node_cache[sig]
