"""Compile the submission and write the build ledger.

Three checks, and they are deliberately about the *contract* rather than about the
port's internals:

``script-present``   the entry point instruction.md names exists and is runnable
``build``            running it exits 0, from a tree with build products removed
``artifact``         it produced a file that is a wasm module at all

Two things this file is careful about.

**It deletes build products first.**  instruction.md says the verifier does, and
says why: a checked-in ``sqlite3.c`` would let a submission ship a pre-generated
amalgamation and never exercise the generator work, which is a large part of the
task.  The deletion list is by name, taken from what SQLite's own ``make clean``
removes plus the wasm artefact, and it is applied to a *copy* -- the submission
tree stays as it was handed in, so the later stages and the report see the same
bytes the agent left.

**It does not read the build log for keywords.**  The log is captured, tailed into
the check detail on failure, and never grepped.  An earlier design scored "the
build did not emit warnings", which rewards a submission that adds ``-w``.

The pre-migration path
----------------------
A tree with no ``build-wasi.sh`` that still carries ``src/os_unix.c``, ``configure``
and ``Makefile.in`` has not been ported.  Reporting that as three failed required
checks makes the stage say 0.0, and 0.0 is the wrong number -- not because the
tree deserves credit for the migration, which it plainly has not done, but because
it says the tree has none of 3.31.1's behaviour when it has all of it.  Stage 2
does not decide whether the port happened; six required gates in stage 1 do, and
every one of them fails on such a tree.  What stage 2 measures is how much
behaviour survived, and that question has an answer here.

So this module builds that tree with the documented native recipe and records
``kind = "native"`` in the ledger.  The later modules then compare two native
builds and the score comes out as what it should be for a tree that changed
nothing: full preservation, and a stage-1 zero.

This is not a way in, and the reason is worth being precise about rather than
assuming.  The path is taken only when ``src/os_unix.c`` is present -- the exact
file ``posix_layer_retired`` requires to be gone -- and only when there is no
``build-wasi.sh``, which ``build_from_this_repository`` requires to exist and
work.  A submission cannot satisfy the native path's conditions and stage 1's at
the same time; the two are complements.  What the path buys is that a *partial*
port lands between the baseline and a finished one instead of alongside a tree
nobody touched, which is the whole reason the behavioural stage publishes a rate
and not only a verdict.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import builds
import report
import wasmfmt

#: Removed from the build copy before the submission's script runs.  Generated
#: sources and build output, by name.  Nothing here is a source file: `manifest`,
#: `src/`, `ext/`, `tool/` and the build files are all left exactly as submitted.
GENERATED = (
    "sqlite3.c", "sqlite3.h", "shell.c", "sqlite3ext.h",
    "sqlite3.wasm", "sqlite3", "sqlite3.o", "libsqlite3.a", "libsqlite3.so",
    "parse.c", "parse.h", "parse.out", "opcodes.c", "opcodes.h",
    "keywordhash.h", "fts5.c", "fts5.h", "lemon", "mkkeywordhash",
    "config.log", "config.status", "Makefile", "sqlite3.pc",
    "tsrc", "bld", "build", ".build",
)

#: How long the submission's build script gets.  Generous: the amalgamation is
#: assembled by tclsh scripts and then compiled as one 240k-line translation unit,
#: which is minutes on a cold cache even when nothing is wrong.
BUILD_TIMEOUT = float(os.environ.get("PF02_BUILD_TIMEOUT", "2400"))

TAIL = 4000

#: The documented pre-migration recipe, shipped in this image's data/ directory.
#: The same file the Dockerfile used to build the reference, which is the point:
#: on the native path both sides of every comparison come from one recipe.
NATIVE_RECIPE = Path("/tests/behavioural/data/scripts/build-native-oracle.sh")

#: What a tree must still have for the native path to apply.  ``src/os_unix.c`` is
#: the POSIX VFS the task exists to retire and the recipe's own hard requirement;
#: ``configure`` and ``Makefile.in`` are the pre-migration build system the recipe
#: drives.  A tree with all three has not been ported.
NATIVE_MARKERS = ("src/os_unix.c", "configure", "Makefile.in")


def tail(text: str, limit: int = TAIL) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


def stage(repo: Path, tree: Path) -> list[str]:
    """Copy the submission to ``tree`` and remove build products from the copy.

    Both paths build from a copy, and both delete the same list.  The copy is why
    the submitted tree is never modified by the act of grading it; the deletion is
    instruction.md's rule, which exists so a checked-in ``sqlite3.c`` cannot stand
    in for the generator work.

    Symlinks are followed as data rather than preserved: the tree came out of a
    tarball of the agent's container, and a dangling link would abort the copy for
    a reason that has nothing to do with the port.
    """
    if tree.exists():
        shutil.rmtree(tree)
    shutil.copytree(repo, tree, symlinks=False, ignore_dangling_symlinks=True)
    removed: list[str] = []
    for name in GENERATED:
        victim = tree / name
        if victim.is_dir() and not victim.is_symlink():
            shutil.rmtree(victim, ignore_errors=True)
            removed.append(name + "/")
        elif victim.exists() or victim.is_symlink():
            victim.unlink(missing_ok=True)
            removed.append(name)
    return removed


def build_native(repo: Path, work: Path, shared: Path,
                 provenance: dict[str, object]) -> tuple[list[dict], list[str], dict]:
    """Build a pre-migration tree with the documented recipe.

    Three required checks, chosen so that each one can fail on a tree that reaches
    this path -- a check that cannot fail is a check that pays a free mark:

    ``native-build``      the recipe exits 0 on this tree.  It fails on a tree
                          whose ``configure``, ``Makefile.in``, ``tool/`` scripts
                          or ``src/`` no longer generate an amalgamation, which is
                          most of what a half-finished port breaks.
    ``native-artifact``   the binary runs and reports 3.31.1.  The recipe already
                          refuses a source id that is neither the canonical nor
                          the ``alt1`` tail, so this is the version the cases are
                          about rather than some other checkout.
    ``native-features``   its ``pragma compile_options`` equals the reference's.
                          This is the one worth having.  Four of the recipe's
                          thirteen flags are invisible to that pragma, but the
                          eight that are visible cover the feature set the corpus
                          asks about, and a tree that edited ``src/ctime.c`` or a
                          default would land here rather than as 300 mystery case
                          failures downstream.
    """
    checks: list[dict[str, object]] = []
    notes = [
        "graded on the pre-migration path: this tree has no "
        f"{builds.BUILD_SCRIPT} and still carries "
        + ", ".join(NATIVE_MARKERS)
        + ".  The submission side is a native build of it, by the same recipe the "
        "image built the reference with, so the cases measure how much of 3.31.1's "
        "behaviour is still here.  Whether the port happened is stage 1's question "
        "and its six required gates all fail on such a tree."
    ]

    tree = work / "repo"
    removed = stage(repo, tree)
    if removed:
        notes.append("removed before building: " + ", ".join(sorted(removed)))

    out = work / "sqlite3-native"
    started = time.time()
    try:
        proc = subprocess.run(
            ["bash", str(NATIVE_RECIPE), str(tree), str(out)],
            capture_output=True, timeout=BUILD_TIMEOUT,
            env=dict(os.environ, LC_ALL="C.UTF-8", TZ="UTC"),
        )
        log = (proc.stdout + proc.stderr).decode("utf-8", "replace")
        code: int | None = proc.returncode
    except subprocess.TimeoutExpired as exc:
        log = b"".join(p for p in (exc.stdout, exc.stderr) if p).decode("utf-8", "replace")
        code = None
    except OSError as exc:
        log, code = f"could not start the recipe: {exc}", None
    elapsed = time.time() - started
    (work / "build.log").write_text(log)
    print(f"native build: exit {code} in {elapsed:.0f}s")

    checks.append({
        "id": "native-build",
        "title": "the documented pre-migration recipe builds this tree",
        "ok": code == 0 and out.is_file(),
        "required": True,
        "weight": 1,
        "detail": (
            f"{NATIVE_RECIPE.name} exited 0 after {elapsed:.0f}s"
            if code == 0 and out.is_file() else
            (f"timed out after {BUILD_TIMEOUT:.0f}s" if code is None
             else f"exit {code} after {elapsed:.0f}s")
            + f"; log tail:\n{tail(log)}"
        ),
    })

    described: dict[str, object] = {}
    if out.is_file():
        try:
            described = builds.describe(str(out))
        except (OSError, subprocess.SubprocessError) as exc:
            notes.append(f"the native build would not answer for itself: {exc}")

    want_version = str(provenance.get("version") or "")
    got_version = str(described.get("version") or "")
    checks.append({
        "id": "native-artifact",
        "title": f"the built binary runs and reports SQLite {want_version}",
        "ok": bool(got_version) and got_version == want_version,
        "required": True,
        "weight": 1,
        "detail": (
            f"reports {got_version}, source id {described.get('source_id')}"
            if got_version == want_version else
            f"reports {got_version!r}, reference reports {want_version!r}"
            if got_version else
            "not attempted: there is no binary to ask"
        ),
    })

    want_opts = list(provenance.get("compile_options") or [])
    got_opts = list(described.get("compile_options") or [])
    missing = [o for o in want_opts if o not in got_opts]
    extra = [o for o in got_opts if o not in want_opts]
    checks.append({
        "id": "native-features",
        "title": "its compile options match the reference's",
        "ok": bool(got_opts) and not missing and not extra,
        "required": True,
        "weight": 1,
        "detail": (
            f"{len(got_opts)} options, identical to the reference's"
            if got_opts and not missing and not extra else
            f"missing {missing or '-'}, unexpected {extra or '-'}"
            if got_opts else
            "not attempted: there is no binary to ask"
        ),
    })

    metadata = {
        "module": "build",
        "path": "native",
        "reference_source_id": provenance.get("source_id"),
        "build_seconds": round(elapsed, 1) if code is not None else None,
        "artifact_bytes": out.stat().st_size if out.is_file() else 0,
    }

    if not all(c["ok"] for c in checks):
        notes.append(
            "no ledger was written: the pre-migration build did not produce a "
            "binary this suite can compare, so every later module will report "
            "that it could not measure the submission"
        )
        return checks, notes, metadata

    stable = shared / "build"
    stable.mkdir(parents=True, exist_ok=True)
    binary = stable / "sqlite3-native"
    shutil.copy2(out, binary)
    ledger = builds.Ledger(
        wasm=str(binary),
        wasmtime="",
        reference=str(builds.REFERENCE_BINARY),
        kind=builds.KIND_NATIVE,
        reference_provenance=provenance,
        submission_provenance={
            "kind": builds.KIND_NATIVE,
            "size": binary.stat().st_size,
            "version": described.get("version"),
            "source_id": described.get("source_id"),
            "compile_options": got_opts,
            "recipe": str(NATIVE_RECIPE),
            "build_seconds": round(elapsed, 1),
        },
        notes=notes,
    )
    print(f"ledger: {ledger.write(shared)}")
    return checks, notes, metadata


def is_pre_migration(repo: Path) -> bool:
    """Whether this tree is still the POSIX source, with nothing ported.

    Both halves are required, and they are not the same claim.  No
    ``build-wasi.sh`` says nothing was delivered; all three ``NATIVE_MARKERS``
    present says the thing that was supposed to be retired is still here.  A tree
    that fails either half is graded on the wasm path, where a missing artefact is
    a failed required check exactly as before.
    """
    if (repo / builds.BUILD_SCRIPT).is_file():
        return False
    return all((repo / marker).exists() for marker in NATIVE_MARKERS)


def main() -> int:
    repo = Path(os.environ["SRB_REPO"])
    work = Path(os.environ["SRB_WORK"])
    shared = Path(os.environ["SRB_SUITE_WORK"])
    checks: list[dict[str, object]] = []
    notes: list[str] = []

    # -- the reference, which the image already built -------------------------
    try:
        provenance = builds.read_provenance()
    except builds.LedgerError as exc:
        # There is no submission-shaped reading of this.  Erroring the module is
        # correct even though an errored module closes the stage: a stage that
        # cannot say what it compared against should not publish a number at all.
        report.write({
            "status": "error",
            "summary": f"the image's reference build is unaccounted for: {exc}",
            "checks": [],
        })
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    print(f"reference: {provenance.get('version')} "
          f"({provenance.get('source_id')}) at {builds.REFERENCE_BINARY}")

    # -- which of the two submissions is this? --------------------------------
    # Decided once, here, and recorded in the ledger.  No later module asks the
    # tree anything; they read the kind.  See the module docstring for why the two
    # paths cannot both be reachable by one submission.
    if is_pre_migration(repo):
        print(f"no {builds.BUILD_SCRIPT} and the POSIX layer is intact: "
              f"grading the pre-migration tree")
        checks, notes, metadata = build_native(repo, work, shared, provenance)
        report.write({"checks": checks, "notes": notes, "metadata": metadata})
        return 0 if all(c["ok"] for c in checks) else 1

    # -- the submission ------------------------------------------------------
    script = repo / builds.BUILD_SCRIPT
    checks.append({
        "id": "script-present",
        "title": f"{builds.BUILD_SCRIPT} exists in the repository root",
        "ok": script.is_file(),
        "required": True,
        "weight": 1,
        "detail": (
            f"{script} is present" if script.is_file() else
            f"{script} does not exist.  instruction.md names it as the build "
            f"contract: `cd /workspace/repo && ./{builds.BUILD_SCRIPT}` must "
            f"exit 0 and produce ./{builds.WASM_NAME}."
        ),
    })

    tree = work / "repo"
    if tree.exists():
        shutil.rmtree(tree)
    elapsed = 0.0
    code: int | None = None
    if script.is_file():
        removed = stage(repo, tree)
        if removed:
            notes.append("removed before building: " + ", ".join(sorted(removed)))
            print(f"removed {len(removed)} build product(s) from the copy")

        started = time.time()
        try:
            proc = subprocess.run(
                ["bash", f"./{builds.BUILD_SCRIPT}"],
                cwd=str(tree), capture_output=True, timeout=BUILD_TIMEOUT,
                env=dict(os.environ, LC_ALL="C.UTF-8", TZ="UTC"),
            )
            log = (proc.stdout + proc.stderr).decode("utf-8", "replace")
            code = proc.returncode
        except subprocess.TimeoutExpired as exc:
            captured = b""
            for part in (exc.stdout, exc.stderr):
                if part:
                    captured += part
            log = captured.decode("utf-8", "replace")
            code = None
        except OSError as exc:
            log, code = f"could not start the build: {exc}", None
        elapsed = time.time() - started
        (work / "build.log").write_text(log)

        checks.append({
            "id": "build",
            "title": f"./{builds.BUILD_SCRIPT} exits 0 with build products removed",
            "ok": code == 0,
            "required": True,
            "weight": 1,
            "detail": (
                f"exit 0 after {elapsed:.0f}s" if code == 0 else
                (f"timed out after {BUILD_TIMEOUT:.0f}s" if code is None
                 else f"exit {code} after {elapsed:.0f}s")
                + f"; log tail:\n{tail(log)}"
            ),
        })
        print(f"build: exit {code} in {elapsed:.0f}s")
    else:
        checks.append({
            "id": "build",
            "title": f"./{builds.BUILD_SCRIPT} exits 0 with build products removed",
            "ok": False, "required": True, "weight": 1,
            "detail": "not attempted: there is no build script to run",
        })

    # -- the artefact --------------------------------------------------------
    artefact = tree / builds.WASM_NAME
    module = None
    if artefact.is_file():
        try:
            module = wasmfmt.parse(str(artefact))
        except wasmfmt.WasmError as exc:
            notes.append(f"{builds.WASM_NAME} is not readable as wasm: {exc}")
    checks.append({
        "id": "artifact",
        "title": f"the build produced ./{builds.WASM_NAME}, and it is a wasm module",
        "ok": module is not None,
        "required": True,
        "weight": 1,
        "detail": (
            f"{artefact} is {module.size} bytes, {len(module.imports)} imports, "
            f"{len(module.exports)} exports"
            if module is not None else
            f"{artefact} " + ("is not a wasm module" if artefact.is_file()
                              else "was not produced")
        ),
    })

    # -- publish -------------------------------------------------------------
    # Written whenever there is something to name, even if a check failed: the
    # later modules will fail their own case-count check against a broken
    # artefact, which reports "the port disagrees here" rather than "the suite
    # could not run", and that is the more useful failure.
    if module is not None:
        stable = shared / "build"
        stable.mkdir(parents=True, exist_ok=True)
        shutil.copy2(artefact, stable / builds.WASM_NAME)
        ledger = builds.Ledger(
            wasm=str(stable / builds.WASM_NAME),
            wasmtime=shutil.which("wasmtime") or "wasmtime",
            reference=str(builds.REFERENCE_BINARY),
            kind=builds.KIND_WASM,
            reference_provenance=provenance,
            submission_provenance={
                "kind": builds.KIND_WASM,
                "size": module.size,
                "sections": [name for name, _ in module.sections],
                "wasi_functions": list(module.wasi_functions()),
                "foreign_imports": [f"{i.module}.{i.field}"
                                    for i in module.foreign_imports()],
                "exports": [e.name for e in module.exports][:40],
                "is_command": module.is_command,
                "is_wasm32": module.is_wasm32,
                "is_threaded": module.is_threaded,
                "build_seconds": round(elapsed, 1) if code is not None else None,
            },
            notes=notes,
        )
        path = ledger.write(shared)
        print(f"ledger: {path}")
    else:
        notes.append(
            "no ledger was written: there is no artefact to compare, so every "
            "later module will report that it could not measure the submission"
        )

    report.write({
        "checks": checks,
        "notes": notes,
        "metadata": {
            "module": "build",
            "path": "wasm",
            "reference_source_id": provenance.get("source_id"),
            "build_seconds": round(elapsed, 1) if code is not None else None,
            "artifact_bytes": module.size if module else 0,
        },
    })
    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
