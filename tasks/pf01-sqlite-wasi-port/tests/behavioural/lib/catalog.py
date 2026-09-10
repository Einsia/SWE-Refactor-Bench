"""Which cases belong to which module, and how many there should be.

One place decides the split, so ``suite.toml``'s weights and the modules'
``run.sh`` scripts cannot drift apart: a module names itself here and gets its
cases, or fails loudly.

On the counts
-------------
``EXPECTED`` pins an exact integer per module.  It is not a checksum and it is not
trying to be one.  A sha256 over the case list would look like the stronger seal
and be the weaker check: nothing about the authoring host can change what
``cases_engine.build()`` returns, because it returns literals, so such a digest
only ever fires when somebody edits a case on purpose -- at which point the fix is
to update the digest, which is not a check.

What can actually go wrong is *silent shrinkage*: an exception swallowed inside a
builder, a loop whose input tuple lost an entry in an edit, a module pointed at
the wrong builder.  All three show up as a count that moved.  So one integer per
module, asserted at image build time, and the failure message says which module
and by how much.  A deliberate change to a case set is a one-line edit here in
the same commit, and the diff shows the intent.

The totals are also the denominator the score is computed against, and they are
fixed here rather than counted at run time because a suite that divides by however
many cases it managed to build rewards a submission that makes case construction
fail.  ``differential.py`` reports ``expected`` alongside ``ran``, and a module
whose two disagree fails its own required check instead of quietly scoring out of a
smaller denominator.
"""

from __future__ import annotations

from typing import Callable

import cases_engine
import cases_extensions
import cases_platform
import cases_shell
import cases_storage
from case import Case, CaseError

#: module id -> the builder that owns it.  The ids match ``suite.toml``.
BUILDERS: dict[str, Callable[[], list[Case]]] = {
    "engine": cases_engine.build,
    "shell": cases_shell.build,
    "extensions": cases_extensions.build,
    "storage": cases_storage.build,
    "platform": cases_platform.build,
}

#: module id -> exact case count.  See the module docstring.
EXPECTED: dict[str, int] = {
    "engine": 1616,
    "shell": 481,
    "extensions": 348,
    "storage": 130,
    "platform": 78,
}

#: module id -> exact number of distinct ``operation`` values.  A case's operation
#: is the behaviour it exercises, and several cases usually share one.  Counting
#: them separately catches the edit that duplicates a case forty times without
#: covering anything new -- the count of cases would rise and the count of
#: operations would not.
OPERATIONS: dict[str, int] = {
    "engine": 328,
    "shell": 167,
    "extensions": 119,
    "storage": 71,
    "platform": 32,
}

#: Every case in the suite, for the totals in the image's build-time check.
TOTAL_CASES = sum(EXPECTED.values())
TOTAL_OPERATIONS = sum(OPERATIONS.values())


def module_ids() -> tuple[str, ...]:
    return tuple(BUILDERS)


def build(module: str) -> list[Case]:
    """The cases for one module, count-checked.

    Raises rather than returning a short list: a module that runs 1,400 of its
    1,616 cases and scores 100% of them is worse than a module that fails.
    """
    try:
        builder = BUILDERS[module]
    except KeyError:
        raise CaseError(
            f"unknown module {module!r}; suite.toml and catalog.BUILDERS "
            f"disagree.  Known: {', '.join(sorted(BUILDERS))}"
        ) from None

    cases = builder()
    want = EXPECTED[module]
    if len(cases) != want:
        raise CaseError(
            f"module {module!r} built {len(cases)} cases, expected {want} "
            f"({len(cases) - want:+d}).  If the change was deliberate, update "
            f"catalog.EXPECTED in the same commit; if not, a builder is losing "
            f"cases."
        )

    ops = {c.operation for c in cases}
    want_ops = OPERATIONS[module]
    if len(ops) != want_ops:
        raise CaseError(
            f"module {module!r} covers {len(ops)} operations, expected "
            f"{want_ops} ({len(ops) - want_ops:+d}).  Cases were added or "
            f"removed without the coverage moving as expected."
        )

    keys = [c.key for c in cases]
    if len(set(keys)) != len(keys):
        seen: set[str] = set()
        dupes = sorted({k for k in keys if k in seen or seen.add(k)})  # type: ignore[func-returns-value]
        raise CaseError(
            f"module {module!r} has duplicate case keys, which would collide in "
            f"the report and hide one of each pair: {', '.join(dupes[:5])}"
        )
    return cases


def build_all() -> dict[str, list[Case]]:
    """Every module's cases.  Used by the image's build-time self-check."""
    return {name: build(name) for name in BUILDERS}
