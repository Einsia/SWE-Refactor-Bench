"""Where does the core's dependency graph end up?

Advisory.  Nothing here can fail the audit gate; the gate is the seven prose
questions in evaluation.toml, answered by a model with both trees open.  These
checks answer the part a string can answer -- which specifiers appear under
`src/core/`, which Node globals are named there, which of the core's own imports
lead back out to the adapter -- so the reviewer spends its turns on the part a
string cannot: whether a facility named nowhere in the text arrives anyway.

That last case is why none of this is scored.  A core that imports nothing, and
receives `fs` through a module-level singleton the adapter assigns on startup,
passes every check in this file and has inverted no dependency.  Reading finds
it; grep does not.  The findings below are where to start reading.

The inverse mistake is just as easy and this file is arranged to avoid it.  A
submission whose doc comment says "replaced readFileSync with platform.read" is
describing the port correctly, so comments are blanked before matching and the
`*Sync` scan is reported as a pointer rather than a violation.  What is left is
still only text, which is the whole reason for the stage boundary.
"""

from __future__ import annotations

import re

import pytest
import srbscan

pytestmark = pytest.mark.scan


def _core_files():
    """The core, or nothing if it is not where the task says it is."""
    return srbscan.js_files(srbscan.CORE_ROOT)


def _need_core():
    if not _core_files():
        pytest.skip(
            f"no JavaScript under {srbscan.CORE_ROOT}; reported once by "
            "test_the_core_tree_exists rather than eighteen times here"
        )


# ---------------------------------------------------------------------------
# Is there a core to read?
# ---------------------------------------------------------------------------

def test_the_core_tree_exists(core):
    """Every other check in this module skips without this one, so it reports it.

    A skip is not visible to the reviewer.  `scan.digest` renders failures in full
    and passes as a count, and skips as a bare number -- "24 did not apply", with no
    reason attached.  So a submission that never created `src/core/` would produce
    eighteen silent skips in this module, and the prompt would carry no trace of
    why.  Absence has to be a finding to be legible at all.
    """
    files = _core_files()
    srbscan.fail_if(not files, (
        f"there is no JavaScript under {srbscan.CORE_ROOT}. §1.1 puts the compiler "
        "core there and stage 2 loads src/core/index.js into the realm, so this is "
        "also a submission stage 2 cannot run. Every other check in this module "
        "skipped for want of something to read.\n"
        f"src/core/: absent or empty"
    ))


# ---------------------------------------------------------------------------
# What the core names
# ---------------------------------------------------------------------------

def test_core_names_no_node_builtin_specifier(core):
    """`import 'fs'` under the core, with or without the `node:` prefix.

    The realm defines no module resolver, so a specifier like this cannot link
    there at all -- which makes a hit here either dead code or a file that is not
    really part of the core.  Both are worth the reviewer's attention, and neither
    is decidable from the string.
    """
    _need_core()
    hits = srbscan.specifier_hits(core, srbscan.is_node_builtin)
    srbscan.fail_if(hits, (
        "the core names Node built-in modules directly:\n  "
        + "\n  ".join(str(h) for h in hits[:20])
        + "\nThe realm has no resolver, so these cannot link there. Read whether "
        "the file is reachable from src/core/index.js or is a leftover.\n"
        + srbscan.cite_line(hits)
    ))


def test_core_names_no_bare_package_specifier(core):
    """A package import under the core: same argument, different resolver.

    `import glob from 'glob'` needs a `node_modules` lookup, and the realm has
    none.  Relative specifiers are fine and are what the core should be made of.
    """
    _need_core()
    hits = srbscan.specifier_hits(core, srbscan.is_bare)
    srbscan.fail_if(hits, (
        "the core imports packages by bare specifier:\n  "
        + "\n  ".join(str(h) for h in hits[:20])
        + "\nNothing resolves a package name inside the realm. If one of these is "
        "genuinely bundled, the reviewer can see how.\n"
        + srbscan.cite_line(hits)
    ))


@pytest.mark.parametrize("name", sorted(srbscan.NODE_GLOBALS))
def test_core_does_not_name_a_node_global(name, core):
    """One row per global, so the finding names which one rather than "some".

    Parametrised rather than looped because the reviewer reads a list of check
    names: "core_does_not_name_a_node_global[process]" says more at a glance than
    one check that failed with ten globals in its message.
    """
    _need_core()
    hits = srbscan.grep(core, re.compile(srbscan.NODE_GLOBALS[name]), f"uses {name}")
    srbscan.fail_if(hits, (
        f"the core names {name}, which the realm does not define:\n  "
        + "\n  ".join(str(h) for h in hits[:12])
        + f"\nIn a live branch this throws at run time. In a type guard "
        f"(`typeof {name.split('.')[0]} !== 'undefined'`) it is a portability "
        "check and is fine -- read which.\n"
        + srbscan.cite_line(hits)
    ))


def test_core_holds_no_fs_shaped_sync_calls(core):
    """`readFileSync` and its family, wherever they are implemented.

    A pointer, not a verdict, and the weakest check in this file: the platform
    object has to provide file access somehow, and a submission that calls the
    capability it was given `readSync` has done nothing wrong.  What the reviewer
    is being told is that the core still speaks in the shape of the API it was
    supposed to have stopped speaking, which is worth one look at the definition.
    """
    _need_core()
    hits = srbscan.shim_modules(core)
    srbscan.fail_if(hits, (
        "the core uses fs-shaped *Sync names:\n  "
        + "\n  ".join(str(h) for h in hits[:15])
        + "\nThese may well be methods on the injected platform object, which is "
        "correct. Follow one to its definition: a method on the capability is the "
        "port, a module-level function that opens a file is not.\n"
        + srbscan.cite_line(hits)
    ))


def test_core_has_no_computed_dynamic_import(core):
    """`import(expr)` with a non-literal specifier.

    Legitimate in a plugin loader and the one shape that defeats every other check
    in this file, since there is no string to match.  Reported so the reviewer
    knows a specifier exists that this scan could not read.
    """
    _need_core()
    hits = srbscan.grep(core, srbscan.IMPORT_COMPUTED,
                        "import() with a computed specifier")
    srbscan.fail_if(hits, (
        "the core has dynamic imports this scan cannot resolve:\n  "
        + "\n  ".join(str(h) for h in hits[:10])
        + "\nWhatever these load is invisible to every other check here. Read what "
        "the expression can evaluate to.\n"
        + srbscan.cite_line(hits)
    ))


# ---------------------------------------------------------------------------
# Which way the dependency points
# ---------------------------------------------------------------------------

def test_core_does_not_import_the_node_adapter(core):
    """The direction of the arrow, as text.

    The port's premise is that the adapter depends on the core and not the other
    way round.  A relative import from `src/core/` that climbs into `src/node/`
    inverts that, and unlike the checks above it is not a portability nit: it is
    the architecture the task asks for, reversed.
    """
    _need_core()
    hits = srbscan.specifier_hits(
        core, lambda s: srbscan.is_relative(s) and re.search(r"(?:^|/)node(?:/|$|\.)", s)
    )
    srbscan.fail_if(hits, (
        "the core imports from the Node adapter:\n  "
        + "\n  ".join(str(h) for h in hits[:12])
        + "\nThis is the dependency pointing the wrong way: the adapter is supposed "
        "to supply the core, not be reachable from it.\n"
        + srbscan.cite_line(hits)
    ))


def test_core_does_not_import_the_retired_lib_tree(core):
    """Does the new core still reach into the tree it replaced?

    `lib/` is State A's 137 CommonJS modules.  A core that imports from there has
    a new directory and the old implementation.
    """
    _need_core()
    hits = srbscan.imports_of(core, "lib/")
    srbscan.fail_if(hits, (
        "the core imports from lib/, the CommonJS tree it replaces:\n  "
        + "\n  ".join(str(h) for h in hits[:12])
        + "\nRead whether lib/ is still the implementation with src/core/ as a "
        "facade over it.\n"
        + srbscan.cite_line(hits)
    ))


def test_core_is_esm_not_commonjs(core):
    """`module.exports` or `require(` in a tree the task says is ESM.

    Distinct from the globals row for `require`: this one is about the module
    system the file is written in rather than a stray identifier, and it is the
    check most likely to be a whole-file finding rather than a line.
    """
    _need_core()
    cjs = srbscan.grep(
        core, re.compile(r"\bmodule\s*\.\s*exports\b|\brequire\s*\("), "CommonJS")
    by_file = sorted({h.where for h in cjs})
    srbscan.fail_if(cjs, (
        f"{len(by_file)} core file(s) are still CommonJS:\n  "
        + "\n  ".join(str(h) for h in cjs[:12])
        + "\nThe realm loads ESM. A CommonJS file here either does not load or is "
        "not reached.\n"
        + srbscan.cite_line(cjs)
    ))


# ---------------------------------------------------------------------------
# The adapter, from the other side
# ---------------------------------------------------------------------------

def test_the_node_adapter_exists_and_uses_node(node):
    """The one check here that fails when something is *absent*.

    Every other check in this file passes most easily on an empty directory, which
    is the failure mode a scan cannot see: a submission that deleted the Node side
    rather than porting it satisfies all of them.  The adapter is where Node is
    supposed to live now, so an adapter that names no Node facility at all has
    either not been written or is not the adapter.
    """
    if not srbscan.js_files(node):
        pytest.skip(f"no JavaScript under {node}; absence is the review's question")

    node_specs = srbscan.specifier_hits(node, srbscan.is_node_builtin)
    globals_used = srbscan.node_globals(node)
    srbscan.fail_if(not (node_specs or globals_used), (
        f"{node} exists but names no Node facility -- no built-in import, no "
        "process, no Buffer, no require. The adapter is where Node is supposed to "
        "have moved to; one that touches none of it is not supplying the core with "
        "anything. Check whether the file provides the platform object at all.\n"
        f"{len(srbscan.js_files(node))} file(s) under {node.name}/, 0 Node references"
    ))
