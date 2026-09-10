"""Where everything lives, and the two virtual roots the sandbox realm sees.

The submission and State A address the same logical files through different
namespaces.  Fixing the mapping here -- once -- is what lets a stylesheet be
described one time and compiled twice.

`face()` is the other thing decided here: which artifact in the submitted tree
this stage drives.  See its docstring -- it is the resolution the whole suite
depends on, and no module makes that decision for itself.

Everything is read from the module contract's environment, so a module can be run
by hand with three exports and no image::

    SRB_REPO        the submission as collected from the agent container
    SRB_SUITE_WORK  scratch shared by every module in this run
    SRB_WORK        this module's own scratch directory
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# --- real locations ----------------------------------------------------------

#: Scratch shared by every module in this run.  ``prepare`` publishes the tree it
#: built here and the other modules read it.
SUITE_WORK = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/pf02-suite"))

#: This module's own scratch directory.
WORK = Path(os.environ.get("SRB_WORK", "/tmp/pf02-work"))

#: The submission exactly as collected from the agent container: no
#: ``node_modules``, no install, and not a place a module may write.  Read this
#: only in order to copy it; measure the copy.
SUBMITTED = Path(os.environ.get("SRB_REPO", "/workspace/repo"))

#: The tree under measurement: the submission copied out of the graded directory
#: with its dependencies reinstalled offline from its own ``package-lock.json``.
#: ``prepare`` builds it.  Modules address the submission through here so that a
#: run cannot write inside the artifact it is grading, and so that nothing the
#: agent may have left inside ``node_modules`` is ever loaded.
REPO = Path(os.environ.get("PF03_TREE", str(SUITE_WORK / "tree")))

#: What ``prepare`` recorded about that build.
LEDGER = SUITE_WORK / "prepare.json"

#: State A: Stylus 0.63.0 with its dependencies installed, built into this image
#: and read-only.  The agent's container did not exist when this was created, so
#: nothing in a submission can have influenced it.  It answers every question of
#: the form "what did the original do with this?" in the same run, on the same
#: machine, which is why this stage ships no stored expected output.
#:
#: ``$SRB_ORIGINAL`` is the same snapshot mounted by the harness, without the
#: dependency install.  This stage uses the built copy because it has to run.
STATE_A = Path(os.environ.get("PF03_STATE_A", "/opt/state-a"))

#: The upstream stylesheets and their expected CSS, read from State A rather than
#: from the submission.  Editing them in a submission therefore changes nothing
#: about what this stage measures; whether they were edited is stage 1's
#: question, and stage 1 has the digests.
INPUTS = STATE_A / "test"

#: Harness JavaScript: the three drivers and the two bootstraps.
JS = Path(__file__).resolve().parent.parent / "js"

# --- the shape the task requires of a submission -----------------------------
# These are locations of artifacts to run, not text to read.  A module opens
# CORE_ENTRY to hand it to the realm loader and executes CLI as a program; no
# module in this stage reads either as source.  What a source file *says* is
# stage 1's question, and stage 1 has both trees and a reviewer for it.

CORE_ROOT = REPO / "src" / "core"
CORE_ENTRY = CORE_ROOT / "index.js"
NODE_ROOT = REPO / "src" / "node"
NODE_ENTRY = NODE_ROOT / "index.js"
CLI = REPO / "bin" / "stylus"

# --- and the shape it had before ----------------------------------------------
# Where the same two faces live in a tree that has not been ported yet: one
# CommonJS package whose entry resolves into ``lib/``, with the built-in `.styl`
# library beside its JavaScript built-ins.  Named here because the face resolution
# below is the only thing in this suite that may consult them.

LEGACY_ENTRY = REPO / "lib" / "stylus.js"
LEGACY_RUNTIME = REPO / "lib" / "functions"

#: A tree that presents the shape §1.1 and §1.5 ask for.
PORTED = "ported"

#: A tree that still presents the shape State A had: one CommonJS package.
PRE_MIGRATION = "pre-migration"

#: Neither.  Nothing to drive, and every check that needed an artifact fails.
ABSENT = "absent"


def face() -> str:
    """Which of the two faces this tree presents, decided by what it *is*.

    Every module in this stage drives an artifact and compares bytes.  Which
    artifact to drive is a question about the tree, and it is answered here once
    rather than by each module guessing -- the same arrangement `lang03` uses for
    its second builder, where the tree's own shape selects the path and no module
    grows a second one.

    The order is not a preference and not a fallback.  ``src/core/index.js`` is
    what §1.1 makes the deliverable, so a tree that has it is measured there and
    nowhere else: a broken core fails its checks, and there is no second face
    behind it to soften that.  ``lib/stylus.js`` is the pre-migration shape, and a
    tree that presents *only* that one has preserved its behaviour without moving
    it -- which stage 2 can measure in full, and which stage 1 fails on
    ``node_platform_retired`` and ``default_path_is_the_port``, both required.

    That division is what keeps the ranking the right way up.  The two faces are
    not two difficulties: an unported tree answers every behavioural question in
    this stage and still scores zero overall, because the stage that asks whether
    the port happened is not this one.  What a submission cannot do is present the
    ported face, fail it, and be re-measured on the other -- deleting
    ``src/core/`` to reach the pre-migration face is the deliberate act stage 1
    reports, not a cheaper route through stage 2.
    """
    if CORE_ENTRY.is_file():
        return PORTED
    if LEGACY_ENTRY.is_file():
        return PRE_MIGRATION
    return ABSENT


def is_pre_migration() -> bool:
    return face() == PRE_MIGRATION


def core_face_available() -> bool:
    """True when there is a compiler core to drive, in either shape."""
    return face() in (PORTED, PRE_MIGRATION)


def node_face_available() -> bool:
    """True when there is a Node-facing entry point to drive, in either shape.

    §1.5's ``src/node/index.js`` in a ported tree; the package's own CommonJS
    entry in one that has not been ported, which is the same API by definition --
    it is the API §1.5 exists to preserve.
    """
    return NODE_ENTRY.is_file() or is_pre_migration()


# Where the built-in `.styl` library lives is asked in one place too, and it is
# `vfs.find_runtime_root()`: §1.4 pins the mount rather than the directory name, so
# in a ported tree it has to be searched for, and searching is that module's job.

# --- virtual roots -----------------------------------------------------------

#: Virtual mount for the project tree inside the sandbox realm.
VPROJ = "/proj"

#: Virtual mount for the built-in `.styl` library (``platform.runtimeRoot``).
VRUNTIME = "/runtime"


def mounts(proj_real: Path) -> dict[str, str]:
    """What State A should substitute for each virtual prefix.

    Filled in per call, because the stylesheets live under ``INPUTS`` while the
    built-in library lives inside whichever tree is being exercised.
    """
    return {VPROJ: str(proj_real)}


# --- what prepare left behind ------------------------------------------------


def build_ledger() -> dict:
    """Read ``prepare``'s record of the tree it built.

    Missing means ``prepare`` did not run, which is a harness fault rather than a
    submission's: a module raises here instead of quietly measuring the wrong
    tree, or -- worse -- rebuilding one itself and reporting on a second install
    nobody declared.
    """
    if not LEDGER.is_file():
        raise RuntimeError(
            f"{LEDGER} does not exist: the `prepare` module did not run, so there "
            f"is no built tree to measure. Run the suite through "
            f"`swerefactor behavioural`, which runs prepare first."
        )
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def require_dependencies() -> dict:
    """The ledger, or a hard failure naming why the install did not happen.

    Modules that drive the Node adapter or the command line need the submission's
    dependency tree.  When ``npm ci`` failed, they fail with npm's own reason
    rather than with a confusing ``Cannot find module``.  That is a real defect in
    a submission -- a lockfile that does not install is not a working repository --
    so it is a failure and not a skip.
    """
    ledger = build_ledger()
    if not ledger.get("dependencies_installed"):
        raise AssertionError(
            "the submission's dependencies could not be installed offline from "
            "its own package-lock.json, so nothing here can be driven:\n"
            + str(ledger.get("install_error") or "no reason recorded")
        )
    return ledger


# --- node invocation ---------------------------------------------------------

NODE = os.environ.get("PF03_NODE", "node")

#: ``vm.SourceTextModule`` is still behind a flag on Node 22.
NODE_VM_FLAGS = ["--experimental-vm-modules", "--no-warnings"]

#: Ceiling for a single driver invocation.  Generous, because one call may carry
#: a few hundred compilations; a submission that hangs should cost its batch
#: rather than the module's whole timeout.
DRIVER_TIMEOUT_SEC = float(os.environ.get("PF03_DRIVER_TIMEOUT", "900"))


def ensure_dirs() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    SUITE_WORK.mkdir(parents=True, exist_ok=True)
