"""The build ledger: which two things are being compared, and where they are.

Every module in this suite compares a reference against a submission, and neither
one is cheap to produce.  So each is produced exactly once and named here.

**The reference is frozen into the image.**  ``Dockerfile`` unpacks the State A
payload, builds native ``sqlite3`` from it with ``scripts/build-native-oracle.sh``,
and records what it got in ``/opt/reference/provenance.json``.  That is deliberate
and it is the more important half of this file: a reference built at *run* time
would mean that a broken compiler, a full disk or a bad mirror on grading day
produces a stage where every comparison fails -- which is indistinguishable, in
the result file, from a submission that ported nothing.  Building it at image
build time turns that class of accident into a ``docker build`` failure, before
any submission is involved and before any number is published.

**The submission is built once, at run time, by the ``build`` module.**  It has to
be run time: the whole point is to compile the tree that was handed in.  The
module writes ``builds.json`` into ``$SRB_SUITE_WORK`` and every later module
reads it.

**There are two kinds of submission, and the ledger says which.**  Normally the
tree ships ``build-wasi.sh`` and the artefact is a wasm module -- that is the
task.  A tree that is still the pre-migration POSIX source has no module to run,
and the honest reading of that is not "every comparison fails": it is "this tree
has not been ported yet", which is stage 1's verdict to give.  Stage 2's question
is narrower and it has an answer for such a tree -- *how much of 3.31.1's
behaviour is still here* -- so the ledger records ``kind = "native"``, the suite
builds the tree with the same documented native recipe the reference came from,
and the cases compare two native builds.  That keeps the behavioural score a
measure of preserved behaviour for every tree the stage is handed, which is what
lets a partial port score between the baseline and a finished one instead of
alongside a tree that was never touched.

A module that cannot find the ledger fails, loudly, naming the file and the module
that should have written it.  It does *not* build the submission itself.  That
rule is worth stating because the convenient alternative is genuinely tempting and
genuinely wrong: a fallback build means six modules each silently compiling their
own wasm, six chances to disagree about the flags, and a suite whose runtime
depends on which modules happened to run.  One build, one artefact, one failure
site.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import execute

#: Where the image put the reference build.  Fixed rather than discovered: the
#: Dockerfile writes here and this is the only thing that reads it.
REFERENCE_DIR = Path("/opt/reference")
REFERENCE_BINARY = REFERENCE_DIR / "sqlite3"
REFERENCE_PROVENANCE = REFERENCE_DIR / "provenance.json"

#: The ledger's name inside $SRB_SUITE_WORK.
LEDGER_NAME = "builds.json"

#: The artefact instruction.md names as the deliverable, relative to the repo.
WASM_NAME = "sqlite3.wasm"

#: The build entry point instruction.md names, relative to the repo.
BUILD_SCRIPT = "build-wasi.sh"


class LedgerError(RuntimeError):
    """The ledger is missing, unreadable, or does not describe what was asked."""


#: The two shapes a submission can take, and the only two.
#:
#: ``wasm``    the delivered artefact is a wasm32-wasi module run under wasmtime.
#:            This is the task, it is what instruction.md contracts for, and it is
#:            the only kind a submission that passes stage 1 can produce.
#: ``native``  the delivered tree is still the *pre-migration* POSIX tree, so
#:            there is no module to run and the suite builds it with the same
#:            documented native recipe the reference came from.
#:
#: The second exists so that stage 2 answers its question -- how much of 3.31.1's
#: behaviour does this tree still have -- for any tree it is handed, including the
#: baseline.  See ``KIND_NATIVE`` in do-build.py for why that is not a way in.
KIND_WASM = "wasm"
KIND_NATIVE = "native"
KINDS = (KIND_WASM, KIND_NATIVE)


@dataclass
class Ledger:
    """Where the two artefacts are, and what they say about themselves."""

    wasm: str
    wasmtime: str
    reference: str
    kind: str = KIND_WASM
    reference_provenance: dict[str, Any] = field(default_factory=dict)
    submission_provenance: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # -- persistence ---------------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "wasm": self.wasm,
            "wasmtime": self.wasmtime,
            "reference": self.reference,
            "kind": self.kind,
            "reference_provenance": self.reference_provenance,
            "submission_provenance": self.submission_provenance,
            "notes": self.notes,
        }

    def write(self, directory: str | os.PathLike[str]) -> Path:
        path = Path(directory) / LEDGER_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(self.to_json(), indent=2, sort_keys=True) + "\n")
        # Atomic, so a module that reads the ledger while the build module is
        # still writing it sees either the old file or the whole new one, never
        # half of a JSON object.
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, directory: str | os.PathLike[str] | None = None) -> Ledger:
        directory = Path(directory or os.environ.get("SRB_SUITE_WORK", ""))
        path = directory / LEDGER_NAME
        if not path.is_file():
            raise LedgerError(
                f"no build ledger at {path}.  The `build` module writes it and "
                f"runs first; if it failed, this module has nothing to measure. "
                f"This module does not build the submission itself -- see "
                f"builds.py on why."
            )
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise LedgerError(f"the build ledger at {path} is unreadable: {exc}") from None
        kind = str(raw.get("kind") or KIND_WASM)
        if kind not in KINDS:
            raise LedgerError(
                f"the build ledger at {path} declares kind {kind!r}; the suite "
                f"knows {', '.join(KINDS)}"
            )
        # wasmtime is required for a wasm submission and meaningless for a native
        # one, so the required set depends on the kind rather than being fixed.
        needed = ["wasm", "reference"] + (["wasmtime"] if kind == KIND_WASM else [])
        missing = [k for k in needed if not raw.get(k)]
        if missing:
            raise LedgerError(
                f"the build ledger at {path} names no {', '.join(missing)}"
            )
        return cls(
            wasm=raw["wasm"],
            wasmtime=raw.get("wasmtime") or "",
            reference=raw["reference"],
            kind=kind,
            reference_provenance=raw.get("reference_provenance") or {},
            submission_provenance=raw.get("submission_provenance") or {},
            notes=list(raw.get("notes") or []),
        )

    # -- what the modules actually want --------------------------------------

    def targets(self) -> tuple[execute.Target, execute.Target]:
        """(reference, submission), ready to run.

        Checked here rather than at each call site: a path in the ledger that no
        longer exists on disk is worth one clear error, not one per case.

        The reference is native either way -- it is the pre-migration binary the
        image froze.  What the kind decides is the *submission* side: a wasm
        module under wasmtime, or, for a tree that is still pre-migration, a
        second native build of that tree.  Both go through the same executor and
        the same cases, which is what makes the two paths comparable at all.
        """
        for label, path in (("reference", self.reference), ("submission", self.wasm)):
            if not Path(path).is_file():
                raise LedgerError(
                    f"the ledger names the {label} at {path}, which does not exist"
                )
        reference = execute.NativeTarget(self.reference)
        if self.kind == KIND_NATIVE:
            return (reference, execute.NativeTarget(self.wasm))
        return (reference, execute.WasmTarget(self.wasmtime, self.wasm))

    @property
    def is_native(self) -> bool:
        """Whether the submission side is a native build of a pre-migration tree."""
        return self.kind == KIND_NATIVE

    @property
    def oracle(self) -> str:
        """The binary that ``native_read`` cases hand a written database to.

        The same reference build.  Naming it separately is not redundancy: the
        two uses are different claims.  As a *target* it answers "what should
        this print"; as an *oracle* it answers "is what the port wrote a
        database at all", by opening bytes it did not write.
        """
        return self.reference


def read_provenance() -> dict[str, Any]:
    """What the image recorded about the reference it built."""
    if not REFERENCE_PROVENANCE.is_file():
        raise LedgerError(
            f"the image did not record {REFERENCE_PROVENANCE}; the reference "
            f"build is unaccounted for and no comparison against it can be "
            f"trusted"
        )
    try:
        return json.loads(REFERENCE_PROVENANCE.read_text())
    except (OSError, ValueError) as exc:
        raise LedgerError(f"{REFERENCE_PROVENANCE} is unreadable: {exc}") from None


def describe(binary: str) -> dict[str, Any]:
    """Ask a native sqlite3 binary what it is.  Used for the reference only."""
    def ask(sql: str) -> str:
        proc = subprocess.run(
            [binary, ":memory:", sql], capture_output=True, timeout=60,
            env=dict(execute.BASE_ENV),
        )
        return execute.decode(proc.stdout).strip()

    return {
        "path": binary,
        "version": ask("select sqlite_version();"),
        "source_id": ask("select sqlite_source_id();"),
        "compile_options": sorted(
            ask("select * from pragma_compile_options;").splitlines()
        ),
    }
