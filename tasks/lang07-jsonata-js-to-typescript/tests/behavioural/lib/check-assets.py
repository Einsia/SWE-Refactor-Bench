#!/usr/bin/env python3
"""Assert every asset the catalog names exists, and every asset present is named.

Run by the verifier Dockerfile, and runnable from the host:

    python3 lib/check-assets.py [suite-dir]

`catalog.py --check` proves the inventory is internally consistent: budgets sum,
families are owned once, percentages match.  It says nothing about whether the
*files* those numbers describe are in the image.  A missing consumer file is not a
crash -- the type module reports the row as failed, and the report shows a
submission that got its declarations wrong.  For all 24 rows at once, if the
directory did not make it into the build context.  Nothing distinguishes that from
a submission with no `.d.ts` at all.

Four things, each of which has a way of going missing that is not a typo:

1.  A `TYPE_CASES` id with no `data/consumers/<id>.ts`.  The id list and the
    directory are edited at different times, and a row added to the catalog
    without its file grades as a failure the submission did not cause.

2.  A consumer file no row names.  Harmless at grade time -- nothing reads it --
    which is why it is worth reporting: it is either a row that was dropped from
    the catalog and left its file behind, or a row whose id was renamed on one
    side only.  Both mean the visible file count stops matching the graded row
    count, and the second means a graded row is failing for a missing file while
    its content sits right there under the old name.

3.  A negative consumer with no marker comment.  The type module needs the line a
    diagnostic is expected on; `types-surface.py` finds it by marker.  A negative
    row whose file lost its marker cannot be graded as "rejected at the right
    place" and would either fail or, worse, pass on a diagnostic from somewhere
    else in the file.

4.  A corpus stem missing one of its two halves.  `<stem>-cases.jsonl` without
    `<stem>-expected.jsonl` is a stem the runner would grade against nothing.
    Only checked when a corpus directory is present, because the corpus is frozen
    at image build and is legitimately absent from a source tree.

Nothing here reads a consumer's *content* beyond the marker: what those files
assert is the type surface's business, and duplicating it here would give two
places to update.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

STEMS = ("main", "fresh", "protocol", "fixture")


def main(argv: list[str]) -> int:
    """`check-assets.py [assets-root]`.

    The argument is the directory holding `consumers/` and `corpus/` -- the source
    tree when run from the host, `/opt/assets` when run against what the oracle
    stage assembled.  Those are the two things worth checking and they are not the
    same directory: the source tree has consumers and no corpus, the assembled one
    has both, and it is the assembled one the driver reads at grade time.

    The engine is imported from beside *this file* rather than from under the
    argument.  `/opt/assets` holds data, not code.
    """
    here = Path(__file__).resolve().parent
    root = Path(argv[1] if len(argv) > 1 else here.parent).resolve()
    sys.path.insert(0, str(here))
    try:
        import catalog
        # `types-surface.py` is not an identifier; the same import the driver uses.
        # Imported for its `MARKER` and `_marker_line` rather than for a second
        # spelling of them here: the marker is that module's contract with these
        # files, and a copy of the string in this file would let the two disagree
        # while both looked right.
        types_surface = importlib.import_module("types-surface")
    except Exception as exc:
        print(f"check-assets: cannot import the engine from {root/'lib'}: {exc}",
              file=sys.stderr)
        return 1

    problems: list[str] = []
    notes: list[str] = []

    # Either layout: `<root>/consumers` (the assembled assets directory, which is
    # what `SWEREFACTOR_ASSETS` points at) or `<root>/data/consumers` (the source
    # tree). Accepting both rather than taking a flag, because the two callers are
    # the build and a person, and a flag is the thing a person gets wrong.
    #
    # `data/` rather than a directory of its own: the read-only inputs a stage ships
    # live in `data/`, `lib/` and `modules/` and nowhere else -- `swerefactor validate`
    # rejects a fourth name, and this suite had one until it was told so. The
    # coincidence is useful. `data/` is the directory the final stage deletes, so the
    # consumers and State A's tarball leave the grading image by the same `rm`.
    def locate(name: str) -> Path:
        direct = root / name
        return direct if direct.is_dir() else root / "data" / name

    consumers = locate("consumers")
    declared = {case[0]: case[1] for case in catalog.TYPE_CASES}
    if not consumers.is_dir():
        problems.append(
            f"no consumers directory under {root}, so all {len(declared)} type "
            f"rows would be graded as failures whatever the submission declared")
    else:
        present = {path.stem for path in consumers.glob("*.ts")}
        for case_id, kind in sorted(declared.items()):
            path = consumers / f"{case_id}.ts"
            if not path.is_file():
                problems.append(
                    f"type row {case_id!r} has no data/consumers/{case_id}.ts")
                continue
            if kind == "negative":
                # `_marker_line` raises on none, on more than one, and on a marker
                # with no line after it. Calling it is a stronger check than
                # searching for the string, and it is the same code that will run
                # at grade time -- so a file that passes here cannot fail there for
                # a reason about the marker.
                try:
                    types_surface._marker_line(path.read_text(encoding="utf-8"))
                except Exception as exc:
                    problems.append(
                        f"negative row {case_id!r}: {exc}; there is no single line "
                        f"to require the diagnostic on")
        for extra in sorted(present - set(declared)):
            notes.append(
                f"data/consumers/{extra}.ts is not named by any type row, so "
                f"nothing grades it")
        tsconfig = consumers / "tsconfig.json"
        if not tsconfig.is_file():
            problems.append(
                "data/consumers/tsconfig.json is missing; the type rows compile "
                "under it and would otherwise inherit whatever the submission "
                "chose, which is the thing being graded")

    # The corpus, when it is here.  Frozen at image build, so a source tree
    # legitimately has none -- but a tree with *some* of it is a tree where one
    # stem would be graded against nothing.
    corpus = locate("corpus")
    if corpus.is_dir():
        for stem in STEMS:
            cases = corpus / f"{stem}-cases.jsonl"
            expected = corpus / f"{stem}-expected.jsonl"
            if cases.is_file() != expected.is_file():
                have, want = ((cases, expected) if cases.is_file()
                              else (expected, cases))
                problems.append(
                    f"the corpus has {have.name} but not {want.name}; that stem "
                    f"cannot be graded")
            elif not cases.is_file():
                problems.append(f"the corpus has no {stem} stem")
        loose = sorted(p.name for p in corpus.glob("*.jsonl")
                       if p.stem.rsplit("-", 1)[0] not in STEMS)
        for name in loose:
            notes.append(f"corpus/{name} belongs to no known stem")

    # The fixture tarball is a property of the suite tree, not of the assets, so it
    # is resolved from this file rather than from the argument. Only required when
    # there is no frozen corpus: once the freeze has run, the 1,675 fixture cases
    # are in `fixture-cases.jsonl` and the tarball they came from has done its job.
    import gen  # noqa: PLC0415  -- for the tarball's name, which it owns
    fixtures = here.parent / "data" / gen.UPSTREAM_TARBALL
    if not fixtures.is_file() and not corpus.is_dir():
        problems.append(
            f"neither data/{fixtures.name} nor a frozen corpus is present, so the "
            f"`suite` module has no cases to grade")

    for note in notes:
        print(f"check-assets: note: {note}")
    if problems:
        print(f"check-assets: {len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"check-assets: {len(declared)} type row(s) paired with consumer files"
          + (", corpus stems complete" if corpus.is_dir() else ", no frozen corpus"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
