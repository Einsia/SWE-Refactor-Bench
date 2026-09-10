#!/usr/bin/env python3
"""Authoring-time checks that span files no single image can see.

Run from anywhere:

    python3 tasks/lang07-jsonata-js-to-typescript/tests/check-task.py

One group per kind of claim.  `--only` takes any of these, repeatably:

    bytecode     no .pyc or __pycache__ under the task; three of the four
                 contexts COPY themselves whole, and `duplicates` skips these
    contract     instruction.md's figures and tables against
                 environment/source-contract.json, and the contract's own
                 internal arithmetic
    counter      srbscan.count_logic_lines re-run over the shipped tarball,
                 against the contract's per-file logic_lines -- the identity
                 the closure gate's floor rests on
    duplicates   files under tests/ that exist twice because no Dockerfile can
                 COPY ../, held byte-identical -- including the ones in lib/,
                 which swerefactor validate cannot reach
    gates        gate ids named in prose, against the gates evaluation.toml
                 declares
    pins         the digests, URLs, sha256s, apt versions and asserted versions
                 the four Dockerfiles each spell for themselves, against each
                 other -- plus the .dockerignore each of the four contexts ships
    reference    the contract's protocol description against State A's own
                 source: the ten codes, the error field order, and the `hello`
                 vocabulary the environment image asserts byte-for-byte
    structure    the eight release-case sentences structure.py claims are
                 published, required to appear in instruction.md verbatim
    tree         the measured facts about State A against the shipped archive

`contract` exists because two copies of source-contract.json each say, in their
own `note`, that "tests/check-task.py round-trips the numbers in instruction.md
against the ones here".  A sentence like that describes nothing until something
reads it, which is the whole failure mode this file is against: a claim that reads
as a guarantee and is enforced by nobody.  Four other files make a promise about
this one, in five places, and each
is honoured by a named group:

    srbscan.py:36        "tests/check-task.py re-runs it over the tarball to hold
                         it to the contract's per-file rows"          -> counter
    srbscan.py:142       BINARY_SUFFIXES "kept in step with the contract's
                         forbidden_paths.extensions"                  -> contract
    catalog.py:447       the gate names are not copied here, "tests/check-task.py
                         reads the real file"                         -> gates
    structure.py:1065    STATED_IN_INSTRUCTION, "the host-side checker reads
                         it"                                          -> structure
    verification/
      Dockerfile:83      "holds this list byte-identical to
                         tests/behavioural/Dockerfile's"               -> pins

Why any of this lives outside the images.  This task has four Docker build
contexts -- `environment/` and the three under `tests/` -- and a build cannot
reach outside its own.  So no image can read two of them at once, and
`instruction.md` and `task.toml` sit outside all four.  `swerefactor validate` does
not close the gap: it reads the config files and the stage layout, never a task's
own prose or Python.

That leaves the cross-file claims unchecked by anything.  A figure in
instruction.md, a message string two files spell independently, a pin shared by
two Dockerfiles -- each stays green in every suite, stays self-consistent inside
its own file, and is wrong in a directory nothing compares.

No group here needs the harness or a running image: everything is read as text, as
JSON, or parsed out of the tarball.  Four modules are imported from inside the task
-- `srbscan` from `tests/audit/lib`, and `catalog`, `gen` and
`structure` from `tests/behavioural/lib` -- and each is pure Python over paths and
tables, needing nothing built and nothing generated.  What is read out of them is
only ever a declared table, never a result.

WHAT DOES NOT PROVE THIS FILE WORKS

`all groups ok` is the same output a checker whose patterns have stopped matching
produces.  Every group below therefore fails loudly rather than vacuously when it
finds nothing to check: a pattern that matches zero times is reported as a
problem, not passed over.  That is weaker than a mutation harness and it is what
is here; where a group's emptiness check is the only thing standing between it and
a silent pass, its docstring says so.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent

INSTRUCTION = TASK / "instruction.md"
TASK_TOML = TASK / "task.toml"
EVALUATION = HERE / "evaluation.toml"
ENVIRONMENT = TASK / "environment"
CONTRACT = ENVIRONMENT / "source-contract.json"
ARCHIVE = ENVIRONMENT / "original.tar.gz"

#: Each set of files under `tests/` that is one file copied, as a glob relative to
#: `tests/`.  Every real file matching a glob must hold the same bytes.
#:
#: Globs rather than a list of paths, because a copy comes into existence when
#: someone adds a file, which is the moment nobody is rereading a hand-kept list.
#:
#: Globs rather than basenames, because a basename is not a role.  `run.sh` is two
#: unrelated scripts here: the scan's four module runners, which are copies, and
#: the behavioural suite's sixteen, which are symlinks to one script and whose own
#: comment invites a module to replace its link with a real script.  Keyed by
#: basename, that legitimate script would be reported as drift against a scan
#: runner it has nothing to do with.
#:
#: `*` does not cross a `/` here, so the depth in each pattern is part of the claim.
SAME_BYTES = (
    "*/lib/vlib.py",
    "*/lib/executor.py",
    # The behavioural and verification stages sweep their finished images for an
    # archive holding `.js`/`.ts`, with the same code, because they acquired the
    # same problem the same way: `COPY . /tests/…` brought `data/original.tar.gz`
    # in, and every other sweep in both images reads loose filenames.  Held
    # identical rather than left as two similar files -- the two images differ in
    # what they are allowed to contain, and the day that difference gets written
    # into one copy is the day the other stops being the same check.
    "*/lib/check-archives.py",
    "*/data/original.tar.gz",
    "*/data/original.sha256",
    "*/data/source-contract.json",
    "audit/modules/*/run.sh",
    # Three here, four in the tree: `environment/` is a build context too and
    # ships the same list.  `pins` compares all four and asserts the `**/` prefix
    # each pattern needs; this entry keeps the three under `tests/` from being a
    # shared basename in neither table.  If the two counts ever disagree by more
    # than that one, one of the groups has stopped looking.
    "*/.dockerignore",
)

#: Files that share a name and are *not* copies, with the reason.  These exist so
#: that a shared name is either a compared group or an explained one, never an
#: unexamined one.
MAY_DIFFER = {
    "*/Dockerfile": "each stage builds a different image from a different context",
    # Four Dockerfiles under tests/, and this is the one `*/Dockerfile` cannot
    # reach: it sits at the top of the build context rather than in a stage
    # directory.  Named separately rather than by widening the glob above, because
    # the reason it differs is different -- the other three are three stages, and
    # this one is not a stage at all.
    "Dockerfile": ("the Harbor verifier image: it ships no toolchain and never "
                   "runs the submission, it drives the three stage images"),
    "*/prompt.txt": ("stage 1 briefs a reviewer about a submission, stage 3 "
                     "briefs an adversary about a defect to plant"),
    "behavioural/modules/*/run.sh": ("symlinks to one script today; a module that "
                                    "outgrows the shared driver is documented as "
                                    "free to replace its link with a real one"),
    "*/data/README.md": ("the behavioural stage's corpus provenance note; nothing "
                         "else ships a data README"),
    "*/requirements-candidate.txt": ("one file, not a copy -- listed so its name "
                                     "is accounted for beside requirements-scan"),
    "*/requirements-scan.txt": ("stage 1's scan stack; a different closure from "
                                "the candidate stack"),
}

#: The build contexts, in the order the docstring names them.
CONTEXTS = (
    ENVIRONMENT,
    HERE / "behavioural",
    HERE / "audit",
    HERE / "verification",
)


def _glob(pattern: str) -> re.Pattern[str]:
    """A `tests/`-relative glob where `*` stops at a `/` and `**` does not.

    `fnmatch` and `Path.match` both fail this differently -- fnmatch's `*` crosses
    separators, and `Path.match` anchors at the right, so `*/lib/vlib.py` would
    match a `vlib.py` at any depth.  Either way the pattern stops saying where the
    file is, which is half of what distinguishes two roles sharing a name.
    """
    body = (re.escape(pattern)
            .replace(r"\*\*", "\x00")
            .replace(r"\*", "[^/]*")
            .replace("\x00", ".*"))
    return re.compile(body + r"\Z")


def flatten(text: str) -> str:
    """Collapse whitespace runs, so a matcher cannot depend on where prose wraps.

    Every prose matcher below goes through this.  The reason is measured on a
    sibling task, not theoretical: 6 of 11 checker failures there were the checker
    matching against a line break rather than against the text, which reports a
    sentence as absent when it is merely rewrapped -- and an absent sentence and a
    wrong one need opposite fixes.
    """
    return re.sub(r"\s+", " ", text)


#: Number words.  Prose here spells a small count as a word -- "Fifteen files, 9,706
#: lines" -- so a checker comparing prose with a contract's integers has to read both
#: spellings, and a checker comparing prose with the size of a table has to write
#: one.  Both groups below use this, which is why it sits with the helpers.
#:
#: Only as far as the figures here go.  A number past the end is reported rather than
#: skipped, in both directions.
_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty",
)


def _word(number: int) -> str | None:
    """The English word for `number`, or None if it is past the list."""
    return _NUMBER_WORDS[number] if 0 <= number < len(_NUMBER_WORDS) else None


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def instruction() -> str:
    if not INSTRUCTION.is_file():
        raise SystemExit(
            f"{INSTRUCTION} does not exist. It is written last, and every group "
            f"that reads it fails until it is -- deliberately, because a checker "
            f"that skipped the missing file would report `ok` on a task with no "
            f"instructions in it.")
    return INSTRUCTION.read_text(encoding="utf-8")


def archive_member(name: str) -> str:
    """One file's text out of the shipped tarball.

    Read from the archive rather than from a working tree, because the archive is
    what ships: a curated tree that has drifted from it would satisfy a check
    against itself and still hand the agent something else.
    """
    with tarfile.open(ARCHIVE, "r:gz") as tf:
        for member in tf.getmembers():
            if member.isfile() and member.name.split("/", 1)[-1] == name:
                fh = tf.extractfile(member)
                if fh is None:
                    break
                return io.TextIOWrapper(fh, encoding="utf-8").read()
    raise SystemExit(f"{ARCHIVE.name}: no member named {name}")


def archive_files() -> dict[str, bytes]:
    """Every member's bytes, keyed by path with the top directory stripped."""
    out: dict[str, bytes] = {}
    with tarfile.open(ARCHIVE, "r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            fh = tf.extractfile(member)
            if fh is None:
                continue
            out[member.name.split("/", 1)[-1]] = fh.read()
    return out


def load_module(name: str, where: Path):
    """Import a module out of a stage's `lib/`, without leaving bytecode.

    `where` may be the directory holding the module or the module file itself;
    both readings are natural at a call site and passing the wrong one would fail
    as `ModuleNotFoundError`, which reads as "the file is missing" rather than as
    "this call spelled the path the other way".

    `sys.dont_write_bytecode` is set for the duration.  Not fussiness: the
    `bytecode` group below reports `__pycache__` under the task, and the way it
    came to exist in the first place was a tool importing `catalog` out of
    `tests/behavioural/lib/` to count cases.  A checker that created what it then
    reported would be a checker nobody could get to green.
    """
    import importlib

    directory = where.parent if where.suffix == ".py" else where
    if not directory.is_dir():
        raise SystemExit(
            f"check-task.py: cannot import {name}: {directory} is not a directory")

    saved_path = list(sys.path)
    saved_flag = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(directory))
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"check-task.py: cannot import {name} from {directory}: {exc}. The "
            f"group that needs it checks a promise another file makes about this "
            f"one, so skipping it would report agreement that was never tested."
        ) from exc
    finally:
        sys.path[:] = saved_path
        sys.dont_write_bytecode = saved_flag


# ---------------------------------------------------------------------------
# tree: the measured facts, against the archive they were measured from
# ---------------------------------------------------------------------------

def check_tree(problems: list[str]) -> None:
    """`source_tree_facts` re-measured from `original.tar.gz`.

    Every figure in the contract's tree section is a measurement, and the whole
    point of writing it down is that the graders read the number instead of the
    tree.  Three stages and three prose documents then restate it.  So if the
    archive is ever repacked -- a file added, a line changed -- the contract keeps
    describing the tree it was written against and everything downstream agrees
    with it, because everything downstream is a copy of it.

    Read out of the archive rather than off disk.  A curated working tree is not
    what ships, and a check against the working tree would go green on exactly the
    drift that matters.

    The counter is not re-run here: that is the `counter` group, which needs the
    tarball unpacked, and this group is bytes and newlines only.
    """
    facts = contract()["source_tree_facts"]
    files = archive_files()

    if not files:
        problems.append(
            f"{ARCHIVE.name} holds no regular files, so every comparison in this "
            f"group is vacuous")
        return

    if len(files) != facts["file_count"]:
        problems.append(
            f"{ARCHIVE.name} holds {len(files)} files and "
            f"source_tree_facts.file_count says {facts['file_count']}. Six "
            f"places restate this figure; all of them are copies of the contract, "
            f"so the archive is the only thing that can disagree with it.")

    # `wc -l`, not a line split.  The contract's own note says which counter it
    # used and why -- LICENSE has no trailing newline, so the two disagree by one
    # on that file.  A checker that picked the other counter would report a
    # correct contract as wrong, which is worse than not checking.
    newlines = sum(blob.count(b"\n") for blob in files.values())
    if newlines != facts["total_lines"]:
        problems.append(
            f"the archive's members carry {newlines:,} newlines and "
            f"source_tree_facts.total_lines says {facts['total_lines']:,}. This "
            f"is `wc -l` as total_lines_note specifies; if the intended counter "
            f"changed, the note has to change with it.")

    src = {path: blob for path, blob in files.items() if path.startswith("src/")}
    for key, measured, what in (
        ("src_bytes", sum(len(b) for b in src.values()), "bytes under src/"),
        ("src_lines", sum(b.count(b"\n") for b in src.values()),
         "newlines under src/"),
    ):
        if facts[key] != measured:
            problems.append(
                f"the archive has {measured:,} {what} and source_tree_facts."
                f"{key} says {facts[key]:,}")

    # The per-file rows, both ways.  A row for a file that is not in the archive
    # is a fact about nothing; a member with no row is a file the contract does
    # not describe, and the reviewer is told to inventory the tree against it.
    rows = {row["path"]: row for row in facts["files"]}
    for path in sorted(set(rows) - set(files)):
        problems.append(
            f"source_tree_facts.files has a row for {path}, which is not in "
            f"{ARCHIVE.name}")
    for path in sorted(set(files) - set(rows)):
        problems.append(
            f"{ARCHIVE.name} contains {path} and source_tree_facts.files has no "
            f"row for it. Stage 1 asks a reviewer to inventory the tree against "
            f"this table, so an undescribed file is one they have no statement "
            f"about.")

    for path, row in sorted(rows.items()):
        blob = files.get(path)
        if blob is None:
            continue
        if len(blob) != row["bytes"]:
            problems.append(
                f"{path} is {len(blob):,} bytes in the archive and the contract's "
                f"row says {row['bytes']:,}")
        if blob.count(b"\n") != row["lines"]:
            problems.append(
                f"{path} carries {blob.count(chr(10).encode()):,} newlines in the "
                f"archive and the contract's row says {row['lines']:,}")

    # `javascript_file_count` is the one figure with a definition rather than a
    # measurement behind it -- the note says which ten files it means and which
    # two it excludes -- so it is checked against that definition.
    js = sorted(p for p in files if p.endswith(".js"))
    if len(js) != facts["javascript_file_count"]:
        problems.append(
            f"the archive holds {len(js)} .js files {js} and "
            f"source_tree_facts.javascript_file_count says "
            f"{facts['javascript_file_count']}. Its note names the nine under "
            f"src/ plus tools/build.js; if that set changed, the note is what "
            f"has to say so.")

    # The digest beside the archive, which every stage verifies after unpacking.
    # Checked here because the three copies of it under `tests/` are compared to
    # each other by `duplicates` and to nothing else: four identical copies of a
    # stale digest pass that group and fail every build.
    digest_file = ENVIRONMENT / "original.sha256"
    if not digest_file.is_file():
        problems.append(f"{digest_file.name} is missing beside the archive")
    else:
        import hashlib
        stated = digest_file.read_text(encoding="utf-8").split()
        actual = hashlib.sha256(ARCHIVE.read_bytes()).hexdigest()
        if not stated or stated[0] != actual:
            problems.append(
                f"original.sha256 says {stated[0] if stated else '(nothing)'} and "
                f"the archive hashes to {actual}. Every stage unpacks and verifies "
                f"against this, so a stale digest fails all three builds -- and "
                f"the three copies under tests/ are only compared to each other.")

    print(f"  archive: {len(files)} files, {newlines:,} newlines, "
          f"{sum(len(b) for b in files.values()):,} bytes, {len(js)} .js, "
          f"{len(rows)} row(s) matched")


# ---------------------------------------------------------------------------
# counter: the contract's logic-line figures, re-measured by the shipped counter
# ---------------------------------------------------------------------------

def test_logic_line_counter_reproduces_contract() -> list[str]:
    """`srbscan.count_logic_lines` over the archive, against the contract's rows.

    Named as `srbscan.py:467` cites it.  It is not collected by pytest -- this
    file is not a `test_*.py` and its name is not an importable module -- and the
    name is kept anyway, because a docstring that points at
    ``tests/check-task.py::test_logic_line_counter_reproduces_contract`` should
    find something when someone greps for it.  `check_counter` calls it.

    Why this identity matters more than it looks.  `ts_code_policy.
    min_ts_logic_lines` is 2,600, and the closure gate compares a submission's
    TypeScript against it with this function.  The floor is only meaningful
    relative to State A's 6,376 -- "a low floor, well under half" -- and that
    relation holds only if one counter produced both numbers.  If the counter
    drifts, the floor silently becomes a different fraction of the tree, and the
    direction is the bad one: a counter that mishandles a block comment swallows
    spans of a submission's source and reports a finished port as a stub.

    Returns problems rather than asserting, so a caller can report all of them.
    """
    problems: list[str] = []
    srbscan = load_module(
        "srbscan", HERE / "audit" / "lib")

    facts = contract()["source_tree_facts"]
    rows = [row for row in facts["files"] if row.get("logic_lines") is not None]
    if not rows:
        return ["the contract states no per-file logic_lines, so this group "
                "measures nothing"]

    # The alias is what the images call, and the docstring at srbscan.py:36 names
    # the underlying function.  Both are checked, because a rename that left only
    # one of them would break either the images or the citation.
    for name in ("count_logic_lines", "count_ts_logic_lines"):
        if not hasattr(srbscan, name):
            problems.append(
                f"srbscan exposes no {name}; tests/audit/Dockerfile "
                f"calls count_ts_logic_lines at build time and srbscan.py's own "
                f"docstring names count_logic_lines")
    if problems:
        return problems
    if srbscan.count_ts_logic_lines is not srbscan.count_logic_lines:
        problems.append(
            "srbscan.count_ts_logic_lines is not the same object as "
            "count_logic_lines. The contract's figures and the floor a submission "
            "is measured against have to be one measurement; two functions with "
            "these names is how they stop being one.")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(ARCHIVE, "r:gz") as tf:
            tf.extractall(tmp)
        tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
        if len(tops) != 1:
            return [f"{ARCHIVE.name} unpacks to {len(tops)} top-level "
                    f"directories; expected exactly one"]
        root = tops[0]

        for row in rows:
            path = root / row["path"]
            if not path.is_file():
                problems.append(f"{row['path']} is not in the archive")
                continue
            got = srbscan.count_ts_logic_lines([path])
            if got != row["logic_lines"]:
                problems.append(
                    f"{row['path']}: the counter says {got:,} logic lines and the "
                    f"contract's row says {row['logic_lines']:,}. One of them is "
                    f"what min_ts_logic_lines is a fraction of.")

        # The three aggregates, each recomputed from the rows the contract's own
        # notes define them over.  Checked because they are what the prose
        # quotes: "6,376 logic lines -- 5,665 library and 711 probe" appears in
        # instruction.md, task.toml, evaluation.toml and two Dockerfiles.
        under_src = [root / r["path"] for r in rows
                     if r["path"].startswith("src/")]
        probe = [root / r["path"] for r in rows
                 if r["path"].startswith("src/probe")]
        library = [p for p in under_src if p not in probe]
        for key, paths, what in (
            ("js_logic_lines", under_src, "every row under src/"),
            ("library_logic_lines", library, "the six library modules"),
            ("probe_logic_lines", probe, "the three probe files"),
        ):
            got = srbscan.count_ts_logic_lines(paths)
            if got != facts[key]:
                problems.append(
                    f"source_tree_facts.{key} is {facts[key]:,} and the counter "
                    f"over {what} gives {got:,}")

        # The per-file rows must add up to the aggregate.  A row and an aggregate
        # can both match the counter and still disagree with each other if a row
        # is missing -- which is the shape the `tree` group's set comparison
        # catches from the other side, and cheap to close here too.
        summed = sum(r["logic_lines"] for r in rows
                     if r["path"].startswith("src/"))
        if summed != facts["js_logic_lines"]:
            problems.append(
                f"the per-file logic_lines under src/ sum to {summed:,} and "
                f"js_logic_lines says {facts['js_logic_lines']:,}")

    print(f"  counter: {len(rows)} per-file row(s) reproduced, "
          f"{facts['js_logic_lines']:,} = {facts['library_logic_lines']:,} + "
          f"{facts['probe_logic_lines']:,}")
    return problems


def check_counter(problems: list[str]) -> None:
    """The counter identity, plus the floor's stated relation to State A.

    The stage-1 image runs the same identity at build time, and prints that it
    was skipped when State A is not mounted -- which is the usual case, because
    the mount is a grading-time thing.  So on a normal build that check reports
    `unchecked here` and names this file.  This is the half that always runs.
    """
    problems.extend(test_logic_line_counter_reproduces_contract())

    # The floor and what it is a fraction of.  `min_ts_logic_note` claims the
    # floor is "well under half" of State A; a floor that crept up past that
    # would make the note wrong, and the note is the argument for the number.
    facts = contract()["source_tree_facts"]
    policy = contract()["ts_code_policy"]
    floor = policy["min_ts_logic_lines"]
    state_a = facts["js_logic_lines"]
    if not isinstance(floor, int) or floor <= 0:
        problems.append(
            f"ts_code_policy.min_ts_logic_lines is {floor!r}, which is not a "
            f"line count")
    elif floor >= state_a / 2:
        problems.append(
            f"min_ts_logic_lines is {floor:,} against State A's {state_a:,} "
            f"logic lines, which is not 'well under half' as min_ts_logic_note "
            f"says. The floor exists to catch a stub, not to set a volume "
            f"target: raise it and a terser port that is correct starts failing "
            f"a required gate.")
    else:
        print(f"  floor: {floor:,} is {100.0 * floor / state_a:.0f}% of State A's "
              f"{state_a:,}")


# ---------------------------------------------------------------------------
# duplicates: files that exist twice because no build context can COPY ../
# ---------------------------------------------------------------------------

def check_duplicates(problems: list[str]) -> None:
    """Byte-identical or explained, for every file under `tests/` that repeats.

    Each copy is read by a different image, so a drift shows up as one stage
    disagreeing with another about State A or about how a case is executed -- and
    the disagreement is then reported as the submission's defect, which is the
    worst way for it to surface.

    Scoped to `tests/`, and the scope is the point.  `swerefactor validate` already
    compares each stage's top level and `data/` against `environment/`, matching on
    basename, which covers the tarball and the contract in the direction that
    decides whether a stage grades the tree the author was handed.  What it cannot
    do is descend into a stage's `lib/`, where two modules sharing a name need not
    be copies -- so `vlib.py` and `executor.py`, which are copies and have no
    counterpart in `environment/` at all, are seen by nothing else.

    The fourth `.dockerignore` lives in `environment/` and is handled by `pins`,
    which roots at the task rather than at `tests/`.
    """
    files: list[str] = []
    links: list[str] = []
    for path in sorted(HERE.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        # A symlink is not a copy: it cannot drift from what it points at, which
        # is why the behavioural suite uses sixteen of them for one `run.sh`.  They
        # are still collected, because a MAY_DIFFER entry claims a path rather than
        # a file -- the day a module does what `run-module.sh`'s own header invites
        # and replaces its link with a real script, that entry is what keeps the
        # script from being reported as drift against the scan's runner.
        if path.is_symlink():
            links.append(str(path.relative_to(HERE)))
            continue
        if not path.is_file():
            continue
        files.append(str(path.relative_to(HERE)))

    same = {pattern: _glob(pattern) for pattern in SAME_BYTES}
    may = {pattern: _glob(pattern) for pattern in MAY_DIFFER}

    # Derived, not enumerated: each group's members come from the tree.
    grouped: dict[str, list[str]] = {p: [] for p in SAME_BYTES}
    excused: dict[str, list[str]] = {p: [] for p in MAY_DIFFER}
    for rel in files:
        for pattern, matcher in same.items():
            if matcher.match(rel):
                grouped[pattern].append(rel)
    for rel in files + links:
        for pattern, matcher in may.items():
            if matcher.match(rel):
                excused[pattern].append(rel)

    # A file two SAME_BYTES globs both claim would be compared against two groups,
    # and which one reported a drift would depend on dict order.
    for rel in files:
        owners = [p for p, m in same.items() if m.match(rel)]
        if len(owners) > 1:
            problems.append(
                f"{rel} matches {len(owners)} SAME_BYTES patterns "
                f"({', '.join(owners)}); make them disjoint, or which group a "
                f"drift is reported against depends on iteration order")

    # Anything sharing a basename is in one table or the other.  This is what keeps
    # the tables from going stale the way a hand-written path list does.
    by_name: dict[str, list[str]] = {}
    for rel in files + links:
        by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    for name, rels in sorted(by_name.items()):
        if len(rels) < 2:
            continue
        unclaimed = [r for r in rels
                     if r in files
                     and not any(m.match(r) for m in same.values())
                     and not any(m.match(r) for m in may.values())]
        if unclaimed:
            problems.append(
                f"{name} occurs {len(rels)} times under tests/ and "
                f"{len(unclaimed)} of them ({', '.join(unclaimed)}) is in neither "
                f"SAME_BYTES nor MAY_DIFFER. Add a glob for whichever it is: a "
                f"copy that must match, or a shared name with a reason")

    # A pattern matching nothing, or one file, compares nothing while reporting the
    # same `ok` as a pattern that compared four.
    for pattern, members in sorted(grouped.items()):
        if len(members) < 2:
            problems.append(
                f"SAME_BYTES pattern {pattern!r} matches {len(members)} file(s) "
                f"under tests/, so it compares nothing. Either the copies are gone "
                f"and the pattern should go, or something moved and the pattern no "
                f"longer describes where the file lives")
            continue
        first, rest = members[0], members[1:]
        reference = (HERE / first).read_bytes()
        for other in rest:
            if (HERE / other).read_bytes() != reference:
                problems.append(
                    f"tests/{other} differs from tests/{first}. These are copies of "
                    f"one file; whichever is stale, the image that reads it grades "
                    f"against something no other stage agrees with, and the "
                    f"disagreement is reported as the submission's defect")

    for pattern in sorted(MAY_DIFFER):
        if not excused[pattern]:
            problems.append(
                f"MAY_DIFFER pattern {pattern!r} matches nothing under tests/, so "
                f"it excuses nothing and hides nothing -- but a file it was written "
                f"for may now be unexamined under a new path")

    if not any(len(m) > 1 for m in grouped.values()):
        problems.append(
            f"not one SAME_BYTES pattern matched two files among the {len(files)} "
            f"under tests/, so this group compared nothing at all")

    # Printed rather than merely asserted.  `duplicates: ok` is the same line
    # whether this compared twenty files or none, and the assertions above only
    # catch a pattern that has stopped matching -- not a table that never claimed
    # the file in the first place.  A reader who sees the counts can tell.
    for pattern in SAME_BYTES:
        print(f"  {pattern}: {len(grouped[pattern])} copies, identical")
    for pattern, reason in sorted(MAY_DIFFER.items()):
        print(f"  {pattern}: {len(excused[pattern])} share a name, may differ "
              f"({reason})")
    print(f"  {sum(len(m) for m in grouped.values())} file(s) in "
          f"{len(SAME_BYTES)} compared group(s)")


# ---------------------------------------------------------------------------
# bytecode: compiled Python that gets committed and COPY'd
# ---------------------------------------------------------------------------

def check_bytecode(problems: list[str]) -> None:
    """No `.pyc` or `__pycache__` anywhere under the task.

    Three separate reasons, and no one of them is the whole argument:

    * The tree is what gets committed.  Bytecode is host-specific -- the filename
      carries the interpreter version and the header carries a timestamp -- so it
      is noise in a diff at best, and at worst it is the author's Python answering
      for the image's.
    * Three of the four contexts do `COPY . /tests/…`, so anything lying in one is
      a candidate for a graded image.  `.dockerignore` is what stops that, and
      `pins` asserts all four copies of it, but a `.dockerignore` is a filter over
      what exists: the two checks answer different questions.
    * `check_duplicates` walks the same tree and *skips* `__pycache__` (the
      `continue` at the top of its loop).  That skip is correct -- bytecode is not
      a copy of anything and holding two `.pyc` byte-identical is not a property
      worth having -- but it means the one group that already sees these files is
      the group that has decided to look away.  The skip and this check are a pair.

    Recurring by construction: measuring a shipped module is done by importing it,
    and importing it writes bytecode beside it.  `load_module` above sets
    `sys.dont_write_bytecode` for exactly that reason, and this group is what
    reports the times something else did not.

    Scoped to the whole task rather than to the build contexts, because the reason
    spans both: `environment/` filters nothing today (its Dockerfile names each
    file it copies), and `tests/` holds this file, which is in no context at all
    and still gets committed.
    """
    found = sorted(
        str(p.relative_to(TASK))
        for p in TASK.rglob("*")
        if p.name == "__pycache__" or p.suffix in (".pyc", ".pyo", ".pyd"))

    if found:
        # The directory and the files inside it are both listed, deliberately:
        # deleting the files and leaving an empty `__pycache__` is a thing git will
        # not record but `COPY .` will still carry, and a message naming only the
        # `.pyc` invites exactly that half-fix.
        shown = ", ".join(found[:6])
        if len(found) > 6:
            shown += f", … ({len(found) - 6} more)"
        problems.append(
            f"{len(found)} compiled-Python path(s) under the task: {shown}. These "
            f"are the author's interpreter, not the image's, and three of the four "
            f"build contexts COPY their whole context. Remove them (`find … -name "
            f"__pycache__ -prune -exec rm -rf {{}} +`) and run whatever produced "
            f"them with PYTHONDONTWRITEBYTECODE=1.")

    # A count, because `bytecode: ok` reads the same whether this walked the tree
    # or walked nothing -- and the figure that makes it checkable is how many paths
    # were considered, not how many were rejected.
    considered = sum(1 for _ in TASK.rglob("*"))
    print(f"  bytecode: {len(found)} compiled path(s) among {considered} path(s) "
          f"under {TASK.name}/")


# ---------------------------------------------------------------------------
# gates: ids named in prose, against the gates that exist
# ---------------------------------------------------------------------------

#: Hyphenated tokens that begin like a gate id and are not one, with the reason.
#: Every entry must still be found in the prose, so a stale exclusion is reported
#: rather than left as a standing licence.  Kept short deliberately: this is the
#: table through which a genuinely invented gate name would get waved past, so
#: anything that could be resolved by rewording the prose instead belongs there.
NOT_GATE_TOKENS = {
    "strict-looking": ("an adjective. `a strict-looking tsconfig` is the phrase "
                       "for the central cheat on this pair, and `strict-typing` "
                       "is the gate that reads it"),
    "no-redef": "a mypy error code, in a `# type: ignore[...]` pragma",
}

#: Files that talk about gates by name and are read by somebody who acts on them.
#: A file that starts naming gates and is not here is checked by nothing -- which
#: is why the second half of this group does not depend on remembering to add it.
GATE_PROSE = {
    "environment/source-contract.json": lambda: CONTRACT,
    "tests/audit/scan.toml":
        lambda: HERE / "audit" / "scan.toml",
    "tests/audit/prompt.txt":
        lambda: HERE / "audit" / "prompt.txt",
    "tests/behavioural/lib/catalog.py":
        lambda: HERE / "behavioural" / "lib" / "catalog.py",
    "tests/audit/modules/contract/test_contract.py":
        lambda: (HERE / "audit" / "modules" / "contract"
                 / "test_contract.py"),
}


def check_gates(problems: list[str]) -> None:
    """Every gate id the prose names, against the gates evaluation.toml declares.

    `catalog.py:447` records why this is read rather than copied.  The table and the
    checks agreed with each other and neither had read `evaluation.toml`.

    Both directions.  An id in prose that no gate declares is a pointer at nothing.
    A *required* gate that no prose anywhere explains is a question that can end a
    run, met by a reviewer with no statement of what it is for -- which means they
    invent the standard, and two runs of the same submission get different verdicts.

    What this cannot check: whether the explanation is any good.  It checks that one
    exists and that it names the gate it is about.
    """
    evaluation = EVALUATION.read_text(encoding="utf-8")
    declared = set(re.findall(r'^id = "([^"]+)"', evaluation, re.M))
    if not declared:
        problems.append(
            "tests/evaluation.toml declares no gate ids, or no longer declares "
            "them as `id = \"...\"` at the start of a line, so both halves of this "
            "group are vacuous")
        return

    mentioned: dict[str, set[str]] = {}
    texts: dict[str, str] = {}
    for label, resolve in GATE_PROSE.items():
        path = resolve()
        if not path.exists():
            problems.append(f"{label}: missing")
            continue
        texts[label] = path.read_text(encoding="utf-8")

    # Plain occurrence, not backtick-delimited.  On this task the contract names
    # its gates in running prose ("no-foreign-jsonata is where that is weighed")
    # more often than in backticks, and a backtick-only pattern found two tokens
    # in the whole contract -- neither of them a gate -- while ten gates were
    # named in it.  A pattern that strict reports every required gate as
    # unexplained, which is a checker failing loudly about the wrong thing.
    for label, text in texts.items():
        for gate in declared:
            if gate in text:
                mentioned.setdefault(gate, set()).add(label)

    # The other direction needs a way to tell a gate id from an ordinary
    # hyphenated token, and the ids have no shared shape -- `strict-typing`,
    # `default-path`, `sources-ported`.  So: anything that *looks* like an id and
    # begins with a word one of the real ids begins with.
    gate_like = tuple(sorted({d.split("-", 1)[0] + "-" for d in declared}))

    # Names the behavioural suite declares for something else.  The prefix test is a
    # prefix test: `build-is-real` is a gate, so `build-` is gate-like, so the
    # release case `build-idempotent` reads as an invented gate.  Read from the
    # tables rather than listed here -- a hand-written exception list beside a
    # generated one is how a real invented name gets waved through later.
    #
    # `STRUCT_DROPS` as well as `STRUCT_CASES`, and it is the bigger half: the drop
    # ledger is twelve rows of "this row was dropped, stage 1 gate X instead", so
    # it names more gate-shaped case ids than the release table does and every one
    # of them sits next to a real gate id in the same sentence.
    not_gates: set[str] = set()
    try:
        catalog = load_module("catalog", HERE / "behavioural" / "lib")
        not_gates |= {check for check, _w, _note in catalog.STRUCT_CASES}
        not_gates |= {check for check, _why in catalog.STRUCT_DROPS}
    except Exception as exc:                                   # pragma: no cover
        problems.append(
            f"cannot read catalog.STRUCT_CASES and STRUCT_DROPS to tell "
            f"release-case names from gate ids: {exc!r}. Without them every case "
            f"id that shares a first word with a gate is reported as invented.")

    # The excused tokens, each of which must still be there.
    for token, reason in sorted(NOT_GATE_TOKENS.items()):
        if not any(token in text for text in texts.values()):
            problems.append(
                f"NOT_GATE_TOKENS excuses `{token}` ({reason}) and no file in "
                f"GATE_PROSE contains it any more. A stale excuse is a standing "
                f"licence for that exact name to be introduced as an invented "
                f"gate later; drop the entry.")

    for label, text in sorted(texts.items()):
        for token in sorted(set(re.findall(
                r"\b([a-z][a-z0-9]*(?:-[a-z0-9]+)+)\b", text))):
            if token in declared or token in not_gates:
                continue
            if token in NOT_GATE_TOKENS:
                continue
            if not token.startswith(gate_like):
                continue
            problems.append(
                f"{label} names `{token}` where a gate id belongs, and "
                f"tests/evaluation.toml declares no such id. Either a gate was "
                f"renamed and the prose still points at the old name, or the prose "
                f"invented one.")

    required = set(re.findall(
        r'id = "([^"]+)"(?:(?!\n\[)[\s\S])*?required = true', evaluation))
    if not required:
        problems.append(
            "no gate in tests/evaluation.toml is `required = true`, so a run can "
            "produce no zero and the whole first stage is advisory")
    unexplained = sorted(required - set(mentioned))
    if unexplained:
        problems.append(
            f"these gates are required and named in no prose the reviewer reads: "
            f"{unexplained}. Each can end a run at reward 0, so a reviewer meeting "
            f"one with nothing in the contract, scan.toml or the prompt explaining "
            f"what it is for is being asked to invent the standard. Name each "
            f"where its rule lives -- source-contract.json for a contract clause, "
            f"scan.toml for the mechanical half.")

    # Each stage image has to contain the grader it is driven by.  The three stage
    # Dockerfiles take it from the donor image, and a stage without it does not
    # score zero -- `import swerefactor` fails and the stage produces no result at
    # all.  Checked as text rather than by building, because this group runs on a
    # host where the images may never have been built.
    for rel in ("tests/behavioural/Dockerfile", "tests/verification/Dockerfile",
                "tests/audit/Dockerfile"):
        path = TASK / rel
        if not path.is_file():
            problems.append(f"{rel} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        if "COPY --from=infra /opt/swerefactor" not in text:
            problems.append(
                f"{rel} never copies the shared grader (`COPY --from=infra "
                f"/opt/swerefactor /opt/swerefactor`), so `import swerefactor` fails "
                f"inside that image and the stage cannot run")
        if not re.search(r"^FROM\s+\$\{INFRA_IMAGE\}\s+AS\s+infra", text, re.M):
            problems.append(
                f"{rel} copies from a stage named `infra` that it never declares "
                f"with `FROM ${{INFRA_IMAGE}} AS infra`; the build fails on an "
                f"unknown stage, which at least fails loudly, but the ARG is how "
                f"the donor tag is overridden")

    advisory = sorted(declared - required)
    print(f"  gates: {len(declared)} declared, {len(required)} required, "
          f"{len(advisory)} advisory; {len(mentioned)} named across "
          f"{len(texts)} file(s)")
    for gate in sorted(mentioned):
        print(f"    {gate}: {len(mentioned[gate])} file(s)")


# ---------------------------------------------------------------------------
# pins: the literals four Dockerfiles each spell for themselves
# ---------------------------------------------------------------------------

#: Every Dockerfile in the task, by the path a report should name.
DOCKERFILES = (
    "environment/Dockerfile",
    "tests/behavioural/Dockerfile",
    "tests/verification/Dockerfile",
    "tests/audit/Dockerfile",
)


#: One extractor per shape of pin.  Each returns {key: value} for one file, where
#: the key says *what* is pinned and the value is the pin.  The same key in two
#: files with two different values is the drift this group is for.
#:
#: Keys are deliberately version-free -- `nodejs.org/node-*-linux-x64.tar.xz`
#: rather than the URL as written.  A key carrying the version would make a
#: half-applied bump look like two unrelated downloads, which is the case that
#: needs to fail loudest.
def _digest_pins(text: str) -> dict[str, str]:
    """`FROM repo@sha256:...` -- the base image, by repository."""
    return {repo: digest for repo, digest
            in re.findall(r"^FROM\s+([\w./-]+)@sha256:([0-9a-f]{64})", text, re.M)}


def _arg_pins(text: str) -> dict[str, str]:
    """`ARG NAME=value`.  A bare `ARG NAME` re-declaration carries no pin."""
    return {f"ARG {name}": value for name, value
            in re.findall(r"^ARG\s+([A-Z_][A-Z0-9_]*)=(\S+)", text, re.M)}


def _url_pins(text: str) -> dict[str, str]:
    """Each pinned download, keyed by host plus filename with versions removed."""
    out: dict[str, str] = {}
    for url in re.findall(r"https://[^\s;'\"\\]+", text):
        if "/" not in url.split("://", 1)[1]:
            continue
        host, _, path = url.split("://", 1)[1].partition("/")
        name = path.rsplit("/", 1)[-1]
        if "." not in name:            # an API root, not a file
            continue
        stem = re.sub(r"\d+(?:\.\d+)+", "*", name)
        out[f"{host}/{stem}"] = url
    return out


def _sha_pins(text: str) -> dict[str, str]:
    """`echo '<sha256>  <file>' | sha256sum -c`, keyed by the file it checks."""
    return {f"sha256 of {name}": digest for digest, name
            in re.findall(r"'([0-9a-f]{64})\s+(\S+)'", text)}


def _apt_pins(text: str) -> dict[str, str]:
    """`apt-get install pkg=version`, keyed by package.

    Included because it is the largest block of duplicated pins in the task and
    the one most likely to be edited in one file: `python3-venv` exists only in
    the verification image, so the lists are near-copies rather than copies, and a
    near-copy is what a reader skims.

    Scoped to the lines between `apt-get install` and the `;` that ends it, rather
    than swept from the whole file.  A file-wide `name=version` pattern also
    matches `--strip-components=1` and `--max-old-space-size=2048`, and reporting
    those as apt packages would make this group describe a real disagreement in
    terms that send the reader to the wrong place.  Those two are pins as well and
    are extracted below, under their own name.
    """
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if "apt-get install" in stripped:
            inside = True
            continue
        if not inside:
            continue
        found = re.match(r"([a-z][a-z0-9.+-]*[a-z0-9])=(\S+?)\s*\\?$", stripped)
        if found is None:              # the `;` line, or anything else: block over
            inside = False
            continue
        out[f"apt {found.group(1)}"] = found.group(2)
    return out


#: Command flags that mean the same thing in every image that spells them, so a
#: difference is drift rather than intent.  An allowlist rather than a sweep,
#: because most flags here legitimately differ: `--from=` names a build stage,
#: `--reuid=` names whichever unprivileged user that image runs as.
#:
#: `--chown` is deliberately not here.  It appears in one file, on the COPY of the
#: archive and its digest into /run/state-a, so there is no second spelling for it
#: to drift from -- and the ownership that does matter across stages is set with
#: `chown -R`, a command rather than a flag, and asserted from inside the finished
#: image by the six setpriv checks at the end of tests/verification/Dockerfile.
FLAG_PINS = {
    "strip-components": "how deep the tarball is unpacked; a wrong value silently "
                        "produces a tree one level off rather than an error",
    "max-old-space-size": "node's heap ceiling. Two stages with two ceilings means "
                          "a submission that OOMs in one and not the other, which "
                          "reads as a flaky verdict rather than as a limit",
}


def _flag_pins(text: str) -> dict[str, str]:
    """The FLAG_PINS flags, keyed by flag name.

    A flag spelled twice in one file with two values is caught here too: the value
    is joined, so the key carries both and any other file disagrees with it.
    """
    out: dict[str, set[str]] = {}
    for flag, value in re.findall(
            r"--([a-z][a-z0-9-]*)=([^\s;'\"`\\]+)", text):
        if flag in FLAG_PINS:
            out.setdefault(flag, set()).add(value)
    return {f"--{flag}": " and ".join(sorted(values))
            for flag, values in out.items()}


def _version_pins(text: str) -> dict[str, str]:
    """`test "$(tool --version)" = "x"` -- what the image asserts it installed."""
    return {f"{tool} --version": want for tool, want
            in re.findall(r'test "\$\((\w+) --version\)" = "([^"]+)"', text)}


PIN_KINDS = {
    "base image digest": _digest_pins,
    "build argument": _arg_pins,
    "download URL": _url_pins,
    "artifact sha256": _sha_pins,
    "apt package version": _apt_pins,
    "command flag": _flag_pins,
    "installed version": _version_pins,
}


def check_pins(problems: list[str]) -> None:
    """Literals more than one Dockerfile spells, against each other.

    No Docker build can reach outside its own context, so the toolchain is
    installed three times from three copies of the same pins: a debian digest, a
    snapshot timestamp, six or seven apt versions, three download URLs with a
    sha256 each, and the two versions each image asserts it got.  Nothing in any
    build compares two of them -- each file is internally consistent by
    construction, because the sha256 it checks is the one written beside the URL it
    fetched.

    So a half-applied bump does not fail a build.  It produces stage 2 grading a
    submission on one node and stage 3 breaking it on another, which surfaces as an
    inconsistent verdict rather than as a broken image.  This group is the only
    thing that looks.

    `tests/audit/Dockerfile` is the exception and is expected to hold
    almost none of these: it is `FROM python@sha256:...` and deliberately carries
    no node and no tsc, because a scan module that could build or run either tree
    would stop being a scan.  It is still in the list -- its base digest and its
    INFRA_IMAGE are pins like any other, and its *absence* of node is asserted in
    the file itself rather than here.

    The keys are derived from the files, not listed here: a pin this checker has no
    pattern for is reported as unseen rather than passing silently, and a fifth
    Dockerfile is picked up by adding its path.
    """
    texts: dict[str, str] = {}
    for rel in DOCKERFILES:
        path = TASK / rel
        if not path.exists():
            problems.append(f"{rel}: missing")
            continue
        texts[rel] = path.read_text(encoding="utf-8")
    if len(texts) < 2:
        problems.append(
            "fewer than two Dockerfiles were read, so every comparison below is "
            "vacuous")
        return

    # kind -> key -> value -> [files]
    seen: dict[str, dict[str, dict[str, list[str]]]] = {
        kind: {} for kind in PIN_KINDS}
    for rel, text in texts.items():
        for kind, extract in PIN_KINDS.items():
            for key, value in extract(text).items():
                seen[kind].setdefault(key, {}).setdefault(value, []).append(rel)

    # A pattern that has stopped matching reports every file as agreeing, which is
    # the same green as agreement.  Each kind must be found somewhere.
    for kind, found in seen.items():
        if not found:
            problems.append(
                f"no {kind} was found in any of the {len(texts)} Dockerfiles. "
                f"Either the pins moved to another form, or PIN_KINDS['{kind}'] "
                f"stopped matching -- and an extractor that matches nothing passes "
                f"every comparison in this group.")

    shared = 0
    for kind, found in sorted(seen.items()):
        for key, values in sorted(found.items()):
            holders = {f for files in values.values() for f in files}
            if len(holders) < 2:
                continue
            shared += 1
            if len(values) == 1:
                continue
            spelled = "; ".join(
                f"{value} in {', '.join(sorted(files))}"
                for value, files in sorted(values.items()))
            problems.append(
                f"{kind} '{key}' is pinned to {len(values)} different values across "
                f"the Dockerfiles: {spelled}. These are copies of one pin, so "
                f"whichever is stale, that image builds a toolchain no other stage "
                f"has and its verdict does not compare.")

    # The count is asserted for the same reason the extractors are: if a refactor
    # left each pin in one file only, every comparison above would be skipped and
    # this group would report nothing while checking nothing.  Measured at 17 when
    # this was written; the floor is set below that so an ordinary edit does not
    # trip it, and far enough above zero to catch a collapse.
    if shared < 12:
        problems.append(
            f"only {shared} pins are held by more than one Dockerfile, and this "
            f"group compares nothing else. The debian digest, the snapshot, the "
            f"apt versions, three downloads with a sha256 each and the two "
            f"asserted versions were all shared when this was written; a number "
            f"this low means the extractors stopped lining up, not that the "
            f"duplication is gone.")

    # -- the two claims the stage images make about node's own tree ---------
    #
    # These are not version pins, they are sweeps, and they belong here for the
    # same reason: one claim written out twice in two files no build reads
    # together.  A drift is a *hole* rather than a mismatch, which is the worse
    # direction -- and on this pair they are what makes State A inert, since node
    # cannot be withheld from either grading image.
    packaged: dict[str, bool] = {}
    baseline: dict[str, bool] = {}
    for rel, text in texts.items():
        if not re.search(r"^FROM\s+toolchain\s+AS\s+baseline", text, re.M):
            continue
        # Both spellings of the same find, since one file quotes the name and the
        # other does not.  Matched on the parts rather than the line.
        packaged[rel] = bool(re.search(
            r"find\s+/\s+-xdev[\s\S]{0,200}?-type d\s+-name\s+'?jsonata'?", text))
        baseline[rel] = "/opt/baseline" in text and bool(re.search(
            r"test\s+!\s+-[de]\s+/opt/baseline", text))

    if len(packaged) < 2:
        problems.append(
            f"{len(packaged)} Dockerfile(s) build a `baseline` stage; the "
            f"behavioural and verification images both unpack State A into "
            f"/opt/baseline, so a number below two means this block stopped "
            f"finding the files it compares")
    for rel, found in sorted(packaged.items()):
        if not found:
            problems.append(
                f"{rel} never sweeps for a packaged jsonata (`find / -xdev -type d "
                f"-name jsonata`). The submission's build runs in this image with "
                f"`require()` available and the whole filesystem readable, so a "
                f"jsonata inside any node_modules is reachable -- and "
                f"npm_config_offline stops a fetch, not a read of something "
                f"already here.")
    for rel, found in sorted(baseline.items()):
        if not found:
            problems.append(
                f"{rel} unpacks State A into /opt/baseline and never asserts it is "
                f"gone from the final image. An empty /opt/baseline holds no .js "
                f"and would pass both content sweeps while still meaning a stage "
                f"leaked the reference.")

    # -- one .dockerignore per context, and the `**/` that makes it work ----
    #
    # The same shape of claim as the pins above, which is why it lives here: one
    # list copied into every build context, and no build able to read two of them.
    # It is here rather than in `duplicates` because that group roots at `tests/`
    # for a documented reason and the fourth copy is in `environment/`.
    #
    # The identity comparison alone is not enough: four identical copies of a bare
    # two-line form pass it.  So the prefix is asserted per pattern line.  Docker
    # matches a .dockerignore pattern against the whole context-relative path and
    # does not re-apply it at each level, so a bare `__pycache__/` guards the
    # context root and nothing below it -- and `lib/__pycache__` is the one that
    # actually appears here, because every check in the behavioural suite imports
    # `catalog` and `gen`.
    contexts = sorted({rel.rsplit("/", 1)[0] for rel in DOCKERFILES})
    ignores: dict[str, str] = {}
    for context in contexts:
        path = TASK / context / ".dockerignore"
        if not path.exists():
            problems.append(
                f"{context}/ is a Docker build context with no .dockerignore, so "
                f"whatever a local run left in it is a candidate for the image. "
                f"The other {len(contexts) - 1} contexts ship one.")
            continue
        ignores[context] = path.read_text(encoding="utf-8")

    def _patterns(text: str) -> list[str]:
        """The pattern lines, with comments and blanks dropped."""
        return [stripped for line in text.splitlines()
                if (stripped := line.strip()) and not stripped.startswith("#")]

    # Grouped by body rather than compared against whichever context sorts first.
    # Which copy is stale is not something this checker can know, and picking a
    # reference makes the odd one out decide how many problems get reported.
    if len(ignores) > 1:
        by_body: dict[str, list[str]] = {}
        for context, text in sorted(ignores.items()):
            by_body.setdefault(text, []).append(context)
        if len(by_body) > 1:
            spelled = "; ".join(
                f"{{{', '.join(group)}}} ({len(_patterns(body))} patterns)"
                for body, group in sorted(by_body.items(), key=lambda kv: kv[1]))
            problems.append(
                f"the {len(ignores)} build contexts ship {len(by_body)} distinct "
                f".dockerignore bodies: {spelled}. These are copies of one list; a "
                f"context that excluded something the others kept produces an image "
                f"nobody can reproduce from the tree.")

    for context, text in sorted(ignores.items()):
        patterns = _patterns(text)
        if not patterns:
            problems.append(
                f"{context}/.dockerignore has no pattern lines at all, so it "
                f"excludes nothing while looking like it does")
            continue
        bare = [p for p in patterns if not p.startswith(("**/", "!"))]
        if bare:
            problems.append(
                f"{context}/.dockerignore has {len(bare)} pattern(s) without a "
                f"`**/` prefix ({', '.join(bare[:4])}). Docker does not re-apply a "
                f"pattern at each directory level, so each of these guards the "
                f"context root and nothing under it.")
        # This pair is specific to this language pair and is why the list matters
        # more here than on the tasks before it: a stray `dist/` in a context is a
        # working JavaScript jsonata inside the image whose whole design is that no
        # such thing is present, and a stray `node_modules` is either a
        # hand-installed tsc or a hand-installed jsonata.
        for needed in ("**/dist", "**/node_modules"):
            if needed not in patterns:
                problems.append(
                    f"{context}/.dockerignore does not exclude `{needed}`. node is "
                    f"both runtimes on this task, so build output left in a context "
                    f"is a reference implementation being COPY'd into a grading "
                    f"image -- the behavioural image's own sweep would fail the "
                    f"build, which is the intended outcome and not the cheap one.")

    # An allowlist entry that matches nothing any more is not dangerous the way a
    # stale exclusion is -- it lets nothing through, it just claims a coverage this
    # group does not have.  Reported so the table stays a description of the files.
    for flag in sorted(FLAG_PINS):
        if not any(f"--{flag}=" in text for text in texts.values()):
            problems.append(
                f"FLAG_PINS lists `--{flag}` and no Dockerfile spells it any more, "
                f"so the entry describes a comparison that is not happening. Drop "
                f"it, or find out where the flag went.")

    sizes = sorted({len(_patterns(t)) for t in ignores.values()})
    bodies = len(set(ignores.values()))
    print(f"  pins: {shared} shared across {len(texts)} Dockerfile(s); "
          f"{len(packaged)} sweep for a packaged jsonata")
    print(f"  .dockerignore: {len(ignores)} of {len(contexts)} context(s), "
          f"{sizes} pattern(s) each, {bodies} distinct "
          f"{'body' if bodies == 1 else 'bodies'}")


# ---------------------------------------------------------------------------
# contract: instruction.md against the machine-readable half of itself
# ---------------------------------------------------------------------------

#: Figures instruction.md must state, as `contract path -> what it is`.  The value
#: is only for the report; the number comes from the contract, so a corrected
#: measurement is corrected in one place and this group finds the prose that still
#: carries the old one.
FIGURES = {
    "source_tree_facts.file_count": "files in the tree",
    "source_tree_facts.total_lines": "lines across the tree",
    "source_tree_facts.src_bytes": "bytes under src/",
    "source_tree_facts.src_lines": "lines under src/",
    "source_tree_facts.js_logic_lines": "JavaScript logic lines",
    "source_tree_facts.library_logic_lines": "logic lines in the six library modules",
    "source_tree_facts.probe_logic_lines": "logic lines in the three probe files",
    "ts_code_policy.min_ts_logic_lines": "the TypeScript logic-line floor",
}

#: Sums the contract asserts about itself.  A tree measured twice by two scripts is
#: how one of these stops holding.
FIGURE_SUMS = (
    ("source_tree_facts.js_logic_lines",
     ("source_tree_facts.library_logic_lines",
      "source_tree_facts.probe_logic_lines")),
)


def _dig(data: dict, path: str):
    """`a.b.c` into nested dicts.  Raises rather than returning None: a typo in
    FIGURES would otherwise read as a figure the contract does not state."""
    node = data
    for key in path.split("."):
        node = node[key]
    return node


def _numbers(text: str) -> set[int]:
    """Every integer `text` states, in digits or as a word.

    Digit grouping is why this is not `re.findall(r"\\d+")`: instruction.md writes
    `6,376` and the contract holds `6376`, and a substring search for the contract's
    spelling finds neither that nor `6376` inside `16376`.

    Number words are read because the prose spells a small count as a word where a
    sentence starts with it -- "Fifteen files, 9,706 lines" is the house style, and
    the contract's file_count is 15.  Only words up to twenty are known, which is
    all this costs: every figure above that appears in digits, so a word cannot
    satisfy one by accident.
    """
    out: set[int] = set()
    for found in re.findall(r"\d[\d,]*", text):
        digits = found.replace(",", "")
        if digits.isdigit():
            out.add(int(digits))
    lowered = text.lower()
    for value, word in enumerate(_NUMBER_WORDS):
        if re.search(rf"\b{word}\b", lowered):
            out.add(value)
    return out


def check_contract(problems: list[str]) -> None:
    """instruction.md, task.toml and environment/source-contract.json, on the
    claims all three make.

    The contract opens by calling itself "the machine-readable half of
    instruction.md. Where the two disagree, this file is what the graders read."
    That sentence is the reason this group exists: it tells a reader the prose is
    redundant, and redundant prose is what drifts. Nothing in any Docker build
    compares them -- instruction.md is not in any build context, so no image can
    even see both files.

    The failure this catches is not a typo. instruction.md is what the agent plans
    against for twelve hours; the contract is what the graders apply. A figure that
    differs between them is a submission built to the wrong floor, or a `dist/`
    path nobody was told about, discovered at grading time.

    Numbers are compared as numbers. A substring check would let `6,376` in the
    prose satisfy a contract holding `6376`, and would let `24` be satisfied by the
    `24` inside `24.18.1`.
    """
    data = contract()
    text = instruction()
    flat = flatten(text)
    numbers = _numbers(text)

    # -- the figures, and the sums the contract asserts about them ----------
    for path, what in sorted(FIGURES.items()):
        value = _dig(data, path)
        if not isinstance(value, int):
            problems.append(
                f"the contract's {path} is {value!r}, not an integer, so it cannot "
                f"be compared with the prose")
            continue
        if value not in numbers:
            problems.append(
                f"instruction.md never states {value:,} ({what}, the contract's "
                f"{path}). The agent plans against the prose and is graded against "
                f"the contract; a figure in only one of them is a plan built to a "
                f"number nobody applies.")

    for total_path, part_paths in FIGURE_SUMS:
        total = _dig(data, total_path)
        parts = [_dig(data, p) for p in part_paths]
        if sum(parts) != total:
            spelled = " + ".join(f"{p} ({v:,})" for p, v in zip(part_paths, parts))
            problems.append(
                f"the contract's own arithmetic does not hold: {spelled} is "
                f"{sum(parts):,}, and {total_path} is {total:,}. These are one "
                f"measurement of one tree reported three ways.")

    # -- [metadata], in three files ----------------------------------------
    #
    # task.toml and evaluation.toml carry the same three long strings, and those
    # strings carry figures.  Compared as whole strings first (they are copies), and
    # then the figures inside them against the contract's integers -- because the
    # copies agreeing with each other and both being stale is the case that a
    # string comparison alone reports as fine.
    meta_keys = ("state_a", "state_b", "scale")
    metas: dict[str, dict[str, str]] = {}
    for rel in ("task.toml", "tests/evaluation.toml"):
        path = TASK / rel
        if not path.exists():
            problems.append(f"{rel}: missing")
            continue
        body = path.read_text(encoding="utf-8")
        block = re.search(r"^\[metadata\]$(.*?)(?=^\[)", body, re.M | re.S)
        if block is None:
            problems.append(
                f"{rel} has no [metadata] section, or none this pattern can find")
            continue
        found: dict[str, str] = {}
        for key in meta_keys:
            value = re.search(rf'^{key}\s*=\s*"(.*)"$', block.group(1), re.M)
            if value is None:
                problems.append(f"{rel}: [metadata] has no {key}")
            else:
                found[key] = value.group(1)
        metas[rel] = found

    if len(metas) == 2:
        left, right = sorted(metas)
        for key in meta_keys:
            if key not in metas[left] or key not in metas[right]:
                continue
            if metas[left][key] != metas[right][key]:
                problems.append(
                    f"[metadata] {key} differs between {left} and "
                    f"{right}. These are the two files a reviewer reads to learn "
                    f"what the task is; when they disagree, one of them is "
                    f"describing a task that no longer exists.")

    # The figures inside `scale`, against the contract.  This is the string that
    # carries the most of them and the one most likely to survive a re-measurement
    # unedited, because nothing reads it at grading time.
    for rel, found in sorted(metas.items()):
        if "scale" not in found:
            continue
        stated = _numbers(found["scale"])
        for path, what in (("source_tree_facts.file_count", "files"),
                           ("source_tree_facts.src_bytes", "bytes under src/"),
                           ("source_tree_facts.js_logic_lines", "logic lines")):
            value = _dig(data, path)
            if value not in stated:
                problems.append(
                    f"{rel}: [metadata] scale does not state {value:,} ({what}, "
                    f"the contract's {path}). Its figures are quoted into "
                    f"task.toml's header comment and into the summary doc, so a "
                    f"stale one propagates.")
        for count, what in ((len(_dig(data, "probe_contract.ops")), "probe ops"),
                            (len(_dig(data, "probe_contract.protocol_codes")),
                             "protocol codes"),
                            (len(_dig(data, "probe_contract.fixtures.impls")),
                             "host callables"),
                            (len(_dig(data, "probe_contract.fixtures.engines")),
                             "regex engines")):
            if count not in stated:
                problems.append(
                    f"{rel}: [metadata] scale does not state {count} ({what}); the "
                    f"contract declares that many")

    # -- the interface, named in the prose ---------------------------------
    #
    # Every one of these is something a submission has to produce and would have no
    # way to guess.  A contract entry the prose never mentions is a requirement
    # discovered by failing a gate.
    for op in _dig(data, "probe_contract.ops"):
        if f'"{op}"' not in flat and f"`{op}`" not in flat:
            problems.append(
                f"instruction.md never names the probe op `{op}`. All six are "
                f"graded and the protocol has no discovery mechanism.")

    for code in sorted(_dig(data, "probe_contract.protocol_codes")):
        if code not in flat:
            problems.append(
                f"instruction.md never names protocol code {code}. A port that "
                f"emits a different code for that condition is byte-wrong on every "
                f"case that reaches it.")

    for entrypoint in _dig(data, "build_contract.entrypoints"):
        if entrypoint not in flat:
            problems.append(
                f"instruction.md never names the required path `{entrypoint}`. The "
                f"three dist/ paths are fixed and the rest of dist/ is the port's "
                f"own business; a submission cannot infer which three.")

    for flag in _dig(data, "tsconfig_contract.required_true"):
        if flag not in flat:
            problems.append(
                f"instruction.md never names the tsconfig flag `{flag}`, which the "
                f"contract requires be true. The submission authors tsconfig.json "
                f"from nothing -- State A has none -- so every required flag has to "
                f"be published.")

    for key, want in sorted(_dig(data, "tsconfig_contract.required_values").items()):
        # The key and its value within a short window of each other, in either
        # order, so `"rootDir": "src"` and `rootDir is src` both satisfy it without
        # `src` appearing anywhere in the file counting as an answer.
        near = (rf'\b{re.escape(key)}\b.{{0,40}}?\b{re.escape(str(want))}\b'
                rf'|\b{re.escape(str(want))}\b.{{0,40}}?\b{re.escape(key)}\b')
        if not re.search(near, flat):
            problems.append(
                f"instruction.md does not state `{key}` as `{want}`. rootDir and "
                f"outDir are what put the three fixed entrypoints at their fixed "
                f"paths; module and target are what make the emitted JavaScript "
                f"requirable by the harness.")

    if _dig(data, "tsconfig_contract.types_must_stay_empty"):
        if '"types"' not in flat:
            problems.append(
                'instruction.md never mentions `"types"`, which the contract '
                'requires stay empty. It is the one tsconfig key whose default is '
                'wrong here: unset, tsc pulls in every @types package it can find, '
                'and the image has none, so a submission that relies on the default '
                'builds here and not on the grader.')

    for entry in _dig(data, "submission_must_author")["paths"]:
        if entry["path"] not in flat:
            problems.append(
                f"instruction.md never names `{entry['path']}`, which the contract "
                f"says the submission must author from nothing")

    for entry in _dig(data, "preserved_paths")["paths"]:
        if entry["path"] not in flat:
            problems.append(
                f"instruction.md never names the preserved path `{entry['path']}`. "
                f"Removing one is a gate failure, and the four are not guessable "
                f"from the rest of the task -- README.swerefactor.md in particular "
                f"is the protocol's own spec document.")

    # -- what is forbidden, and what is deliberately not ------------------
    for extension in _dig(data, "forbidden_paths")["extensions"]:
        if extension not in flat:
            problems.append(
                f"instruction.md never names the forbidden extension `{extension}`")

    for directory in _dig(data, "forbidden_paths")["directories"]:
        if directory not in flat:
            problems.append(
                f"instruction.md never names the forbidden directory "
                f"`{directory}`")

    # The exception is the single most load-bearing sentence in the contract for
    # this language pair, and the one a reader coming from another task in the
    # category will assume is a mistake.  It has to be in the prose too.
    if not re.search(r"dist/.{0,200}?JavaScript|JavaScript.{0,200}?dist/", flat):
        problems.append(
            "instruction.md never puts `dist/` and `JavaScript` in the same "
            "sentence. tsc emits .js there and the contract's forbidden_paths "
            "deliberately does not list .js; an agent that reads `no JavaScript in "
            "the delivered tree` as covering dist/ deletes its own build output. "
            "This is the sentence a reader coming from another task in this "
            "category will assume is a mistake, so the prose has to be explicit.")

    # -- the toolchain, in prose and in the image -------------------------
    node_want = re.search(r"v?(\d+\.\d+\.\d+)",
                          _dig(data, "ts_code_policy.runtime"))
    tsc_want = re.search(r"(\d+\.\d+\.\d+)",
                         _dig(data, "ts_code_policy.compiled_by"))
    dockerfile = (ENVIRONMENT / "Dockerfile").read_text(encoding="utf-8")
    for label, found, asserted in (
            ("node", node_want, f'test "$(node --version)" = "v{node_want.group(1)}"'
                                if node_want else ""),
            ("tsc", tsc_want, f'test "$(tsc --version)" = "Version '
                              f'{tsc_want.group(1)}"' if tsc_want else "")):
        if found is None:
            problems.append(
                f"the contract's ts_code_policy does not state a {label} version in "
                f"a form this checker can read")
            continue
        version = found.group(1)
        if version not in flat:
            problems.append(
                f"instruction.md never states the {label} version {version} that "
                f"the contract pins. A port targeting another one is a port that "
                f"compiles somewhere else.")
        if asserted not in dockerfile:
            problems.append(
                f"environment/Dockerfile does not assert `{asserted}`, so the "
                f"contract's {label} {version} is a claim about an image nobody "
                f"checked")

    # -- srbscan.py:142's promise ----------------------------------------
    #
    # The one cross-file claim in this group that is not about prose.  srbscan's
    # BINARY_SUFFIXES says it is "kept in step with the contract's
    # forbidden_paths.extensions"; if the contract gains one, the scan stops reading
    # a file it was supposed to refuse to read as text.
    srbscan = load_module("srbscan", HERE / "audit" / "lib")
    suffixes = {s.lower() for s in getattr(srbscan, "BINARY_SUFFIXES", ())}
    if not suffixes:
        problems.append(
            "srbscan.BINARY_SUFFIXES is empty or absent, and its comment says it is "
            "kept in step with the contract's forbidden_paths.extensions")
    missing = sorted({e.lower() for e in _dig(data, "forbidden_paths")["extensions"]}
                     - suffixes)
    if missing:
        problems.append(
            f"the contract forbids {', '.join(missing)} and srbscan.BINARY_SUFFIXES "
            f"does not list {'it' if len(missing) == 1 else 'them'}. That list is "
            f"what stops the scan decoding a binary as source: a native addon "
            f"outside it gets read as text, and the reviewer is shown mojibake "
            f"instead of `a compiled artifact is in the tree`.")

    # -- what is not graded ----------------------------------------------
    #
    # Asked for because its absence costs the agent time rather than points: three
    # of the contract's `not_graded` entries are things a careful porter will
    # otherwise try to preserve exactly, and one of them (performance) cannot be.
    if not re.search(r"not\s+graded|no[t]?\s+(?:being\s+)?measured|"
                     r"is\s+not\s+(?:what\s+is\s+)?graded", flat, re.I):
        problems.append(
            "instruction.md never says what is *not* graded. The contract lists "
            "three, and each is a place a careful porter spends hours for no "
            "points: internal idioms, performance, and the wall clock.")

    print(f"  contract: {len(FIGURES)} figures, "
          f"{len(_dig(data, 'probe_contract.ops'))} ops, "
          f"{len(_dig(data, 'probe_contract.protocol_codes'))} protocol codes, "
          f"{len(_dig(data, 'tsconfig_contract.required_true'))} tsconfig flags, "
          f"{len(_dig(data, 'preserved_paths')['paths'])} preserved paths")
    print(f"  instruction.md: {len(text):,} bytes, {len(numbers)} distinct "
          f"integers, {len(metas)} [metadata] block(s) compared")


# ---------------------------------------------------------------------------
# reference: the contract against State A's own source
# ---------------------------------------------------------------------------

def check_reference(problems: list[str]) -> None:
    """The protocol, as the contract describes it and as State A implements it.

    Everything above compares a document with a document. This group compares the
    documents with the code, which on this task is a different kind of claim: the
    probe protocol has no external definition. It is whatever `src/probe.js` and
    `src/probe-wire.js` do, and the contract is a description of that -- so a
    disagreement here means the specification a submission is handed describes an
    engine other than the one it is graded against.

    That is worse than a stale figure. The corpus is generated *from* State A, so a
    port built to the contract's spelling of a code, and graded against State A's,
    fails cases it implemented correctly as documented.

    The three things checked are the three where the contract restates the source
    rather than pointing at it: the ten protocol codes, the error field order, and
    the `hello` response -- which publishes the closed vocabulary (ops, host
    callables, regex engines) that every other op is validated against.

    The archive is the authority, not the unpacked tree: `environment/` is a build
    context, the tarball is what it ships, and a local unpacked copy is not
    something any stage reads.
    """
    data = contract()
    files = archive_files()

    def source(name: str) -> str | None:
        for member, body in files.items():
            if member == name or member.endswith(f"/{name}"):
                return body.decode("utf-8")
        problems.append(
            f"{name} is not in environment/original.tar.gz, and it is where the "
            f"protocol is defined")
        return None

    # -- the ten protocol codes -------------------------------------------
    #
    # The contract strips the backticks the source puts around `id` and `op`, so the
    # descriptions are compared with backticks removed on both sides rather than
    # with the contract's spelling patched into the source's.
    probe = source("src/probe.js")
    declared = _dig(data, "probe_contract.protocol_codes")
    if probe is not None:
        block = re.search(r"PROTOCOL_CODES\s*=\s*\{(.*?)\n\};", probe, re.S)
        if block is None:
            problems.append(
                "src/probe.js has no `PROTOCOL_CODES = {...};` this checker can "
                "find. The contract publishes ten codes with descriptions and "
                "claims they are this table.")
        else:
            found = dict(re.findall(r"(P\d{4}):\s*'([^']*)'", block.group(1)))
            if not found:
                problems.append(
                    "src/probe.js's PROTOCOL_CODES table was found but no `PNNNN: "
                    "'...'` entries were read out of it, so the comparison below "
                    "checked nothing")
            for code in sorted(set(declared) | set(found)):
                if code not in found:
                    problems.append(
                        f"the contract declares protocol code {code} and "
                        f"src/probe.js's PROTOCOL_CODES does not define it. A "
                        f"submission that implements it as documented emits a code "
                        f"the reference never emits.")
                elif code not in declared:
                    problems.append(
                        f"src/probe.js defines protocol code {code} and the "
                        f"contract does not declare it. The corpus is generated "
                        f"from this table, so a case can reach a code the "
                        f"submission was never told about.")
                elif found[code].replace("`", "") != declared[code].replace("`", ""):
                    problems.append(
                        f"protocol code {code} is described as "
                        f"{declared[code]!r} in the contract and {found[code]!r} in "
                        f"src/probe.js. The message is compared byte-for-byte on "
                        f"every case that reaches this code.")

    # -- the error field order -------------------------------------------
    wire = source("src/probe-wire.js")
    if wire is not None:
        block = re.search(r"ERROR_FIELDS\s*=\s*\[([^\]]*)\]", wire)
        if block is None:
            problems.append(
                "src/probe-wire.js has no `ERROR_FIELDS = [...]`, and the "
                "contract's error_field_order describes it")
        else:
            fields = re.findall(r"'([^']+)'", block.group(1))
            stated = _dig(data, "probe_contract.error_field_order")
            # The contract states the rule in prose: "kind first, message last, and
            # in between only the fields that are present, in this fixed order:
            # code, position, token, value, index, type."  The list is read out of
            # that clause rather than by picking known field names out of the whole
            # sentence -- `value` is named three times in it, once in the list and
            # twice in the note after, so a sweep over the prose yields a seventh
            # entry and the comparison below could never pass.
            clause = re.search(r"order:\s*([^.]+)\.", stated)
            order = ([name.strip() for name in clause.group(1).split(",")]
                     if clause else None)
            if order is None:
                problems.append(
                    "the contract's error_field_order no longer contains an "
                    "`order: a, b, c.` clause, so the field list this checker "
                    "compares with src/probe-wire.js cannot be read out of it")
            elif order != fields:
                problems.append(
                    f"the contract's error_field_order lists {order} between kind "
                    f"and message; src/probe-wire.js emits {fields}. Field order is "
                    f"bytes on the wire, and bytes are what stage 2 compares -- "
                    f"every erroring case in the corpus disagrees.")
            for anchor, where in (("kind", "first"), ("message", "last")):
                if f"{anchor} {where}" not in stated:
                    problems.append(
                        f"the contract's error_field_order no longer says `{anchor} "
                        f"{where}`, which is half of what makes the order "
                        f"reproducible")
                if anchor in fields:
                    problems.append(
                        f"src/probe-wire.js's ERROR_FIELDS contains `{anchor}`, "
                        f"which the contract says is emitted {where} and outside "
                        f"this list. Two places deciding where it goes is how it "
                        f"ends up in two places.")

    # -- the hello response, which publishes the closed vocabulary --------
    #
    # environment/Dockerfile asserts this line byte-for-byte against the built
    # reference, and its comment says so: "A port has to reproduce this line for
    # id 1, so if this string is wrong then the task is wrong".  That makes the
    # Dockerfile the one place where the vocabulary is checked against the running
    # engine -- and this group is the only place it is checked against the contract.
    dockerfile = (ENVIRONMENT / "Dockerfile").read_text(encoding="utf-8")
    want = re.search(r"want='(\{\"id\":1,.*?\})';", dockerfile)
    if want is None:
        problems.append(
            "environment/Dockerfile no longer asserts a `hello` response this "
            "checker can find (`want='{\"id\":1,...}';`). That assertion is what "
            "ties the contract's vocabulary to the engine that produces it.")
    else:
        try:
            hello = json.loads(want.group(1))
        except json.JSONDecodeError as exc:
            problems.append(
                f"environment/Dockerfile's asserted `hello` response is not valid "
                f"JSON ({exc}); the probe emits JSON, so this assertion can never "
                f"pass")
        else:
            result = hello.get("result", {})
            for key, path in (("ops", "probe_contract.ops"),
                              ("impls", "probe_contract.fixtures.impls"),
                              ("engines", "probe_contract.fixtures.engines")):
                stated = sorted(_dig(data, path))
                # `hello` publishes them sorted; the contract lists ops in the order
                # a reader meets them.  Compared as sets, reported as sorted lists.
                if sorted(result.get(key, [])) != stated:
                    problems.append(
                        f"`hello` publishes {key} = {result.get(key)!r} and the "
                        f"contract declares {stated!r}. This is the vocabulary "
                        f"every other op validates against: a name in one and not "
                        f"the other is either a P0005/P0009 the corpus can reach "
                        f"and the submission was not told about, or a documented "
                        f"name that does not exist.")
            declared_hello = _dig(data, "probe_contract.hello_result")
            if sorted(result) != sorted(declared_hello["keys"]):
                problems.append(
                    f"`hello`'s result carries {sorted(result)} and the contract's "
                    f"hello_result.keys declares {sorted(declared_hello['keys'])}. "
                    f"An extra key is a byte on the first line of every session; a "
                    f"missing one is a vocabulary the submission cannot read back.")
            if result.get("protocol") != declared_hello["protocol"]:
                problems.append(
                    f"`hello` publishes protocol {result.get('protocol')!r} and the "
                    f"contract declares {declared_hello['protocol']!r}. It is the "
                    f"only version marker on the wire, so a submission built to the "
                    f"contract's spelling is wrong on line one of every session.")
            for key in ("ops", "impls", "engines"):
                values = result.get(key)
                if isinstance(values, list) and values != sorted(values):
                    problems.append(
                        f"`hello` publishes {key} unsorted ({values!r}) and the "
                        f"contract says each list is sorted. Sort order is bytes "
                        f"here, and the ordering rule is the only thing telling a "
                        f"port which of the permutations to emit.")
            if not _dig(data, "probe_contract.transport"):
                problems.append(
                    "the contract's probe_contract.transport is empty, and it is "
                    "what tells a submission the framing is one JSON object per "
                    "line")

    print(f"  reference: {len(declared)} protocol codes, "
          f"{len(_dig(data, 'probe_contract.fixtures.impls'))} host callables, "
          f"{len(_dig(data, 'probe_contract.fixtures.engines'))} regex engines, "
          f"from {len(files)} archive member(s)")


# ---------------------------------------------------------------------------
# structure: the sentences the release cases say they are graded from
# ---------------------------------------------------------------------------

def check_structure(problems: list[str]) -> None:
    """`structure.py`'s `STATED_IN_INSTRUCTION`, against instruction.md.

    The behavioural suite's structure module grades eight things a consumer of the
    built package can observe. Each is a rule, and structure.py:1061 states the
    third condition for a rule being gradable at all: that it is *published*, since
    "grading a rule the submission was never told is unfair however reasonable the
    rule is."

    So each case cites the sentence in instruction.md that publishes it, and that
    comment ends by naming this group as what enforces the citation. It has to be
    here rather than in the module: instruction.md is outside all four Docker build
    contexts, so no grading image has ever seen the file its cases quote.

    What is compared is the citation as a substring of the flattened prose --
    flattened because the prose wraps and the citation does not, and case-folded
    because a rule that opens a bullet is capitalised there and quoted lowercase
    here. Neither is a fact about whether the rule is published. What is *not*
    compared is the direction that would be nice to have: that the sentence
    instruction.md contains actually publishes the rule. A reader does that.

    One case cites `in order`, which is two words that occur in ordinary prose. A
    citation that short can be satisfied by a sentence about something else
    entirely, so short citations are checked for being anchored by a longer one
    rather than trusted on their own.
    """
    text = instruction()
    flat = flatten(text).lower()

    structure = load_module("structure", HERE / "behavioural" / "lib" / "structure.py")
    catalog = load_module("catalog", HERE / "behavioural" / "lib" / "catalog.py")
    stated = getattr(structure, "STATED_IN_INSTRUCTION", None)
    if not stated:
        problems.append(
            "tests/behavioural/lib/structure.py has no non-empty "
            "STATED_IN_INSTRUCTION, and its comment says this group requires every "
            "entry to appear in instruction.md verbatim. With the table gone, this "
            "group checks nothing.")
        return

    cases = {row[0] for row in getattr(catalog, "STRUCT_CASES", ())}
    # structure.py already checks STRUCT_CASES against HANDLERS both ways, so the
    # only key-set claim left is the one that spans the two files this group reads.
    for case in sorted(cases - set(stated)):
        problems.append(
            f"release case `{case}` is graded and cites no sentence in "
            f"instruction.md. Either the rule is published somewhere and the "
            f"citation is missing, or the case grades a rule the submission was "
            f"never told -- which is the thing structure.py:1061 says disqualifies "
            f"a rule from being in the table.")
    for case in sorted(set(stated) - cases):
        problems.append(
            f"STATED_IN_INSTRUCTION cites a sentence for `{case}` and no release "
            f"case has that id, so the citation is guarding nothing")

    for case, citation in sorted(stated.items()):
        needle = flatten(citation).lower()
        if needle not in flat:
            problems.append(
                f"release case `{case}` cites {citation!r} and instruction.md does "
                f"not contain it. The case is worth points against a rule the "
                f"prose does not state.")

    # -- the short ones ---------------------------------------------------
    #
    # `in order` is three points on whether responses come back in request order.
    # As a substring it is satisfied by any sentence in the file that happens to use
    # the phrase, including one about something else, so on its own it is not
    # evidence the rule was published.  What makes it evidence is that a longer
    # citation containing it is also present -- here `... one JSON response per line
    # on stdout, in order, until stdin reaches EOF ...`, which does publish it.
    SHORT = 24
    for case, citation in sorted(stated.items()):
        if len(citation) >= SHORT:
            continue
        longer = sorted(
            other for other, text_ in stated.items()
            if other != case and flatten(citation) in flatten(text_))
        if not longer:
            problems.append(
                f"release case `{case}` cites {citation!r}, which is {len(citation)} "
                f"characters and is not contained in any other citation. A phrase "
                f"that short is satisfied by prose about anything, so this citation "
                f"is not evidence the rule is published. Quote the whole sentence.")
        elif not any(flatten(stated[other]).lower() in flat for other in longer):
            problems.append(
                f"release case `{case}` cites {citation!r} and relies on "
                f"{longer[0]}'s longer citation to anchor it, and that one is not in "
                f"instruction.md either")

    # -- the count words -------------------------------------------------
    #
    # Prose in three files states how many release cases there are.  Two of the
    # three said "seven" or "eight" while the table held eight, which is the whole
    # class of defect this file is for: a figure in a sentence that nothing reads.
    # catalog.py's own `_FAMILY_NUM_RE` already checks suite.toml's *points* against
    # the weights; the count of cases is checked here.
    count = len(cases)
    word = _word(count)
    if word is None:
        problems.append(
            f"there are {count} release cases and _NUMBER_WORDS stops at "
            f"{len(_NUMBER_WORDS) - 1}, so the count words below were not checked")
    else:
        # Anchored to the two phrasings that assert *this table's* size, rather than
        # swept for `N checks`.  A sweep over structure.py finds "seven checks" and
        # "eight checks" in prose about other things, plus "pipe checks" and
        # "relocation checks" -- all legitimate, all reported, and a group that
        # cries wolf four times is a group nobody reads the fifth time.
        # The capture is a count, not any word: `(\w+) release cases` also matches
        # the article in "the release cases", and reporting that as a wrong number
        # is the same false alarm one level down.
        counts = r"\b(?:" + "|".join(_NUMBER_WORDS) + r"|\d+)\b"
        claims = (rf"the\s+same\s+({counts})\s+checks",
                  rf"({counts})\s+release\s+cases")
        found = 0
        for rel in ("behavioural/suite.toml", "behavioural/lib/structure.py"):
            path = HERE / rel
            if not path.exists():
                problems.append(f"{rel}: missing")
                continue
            body = path.read_text(encoding="utf-8")
            for pattern in claims:
                for match in re.finditer(pattern, body, re.I):
                    found += 1
                    if match.group(1).lower() != word:
                        problems.append(
                            f"{rel} says {match.group(0)!r} where the table holds "
                            f"{count} ({word}). Nothing reads this count at grading "
                            f"time, which is exactly why it survives the table "
                            f"changing size.")
        if not found:
            problems.append(
                f"neither behavioural/suite.toml nor structure.py states how many "
                f"release cases there are, in either phrasing this checks (`the "
                f"same N checks`, `N release cases`). The module's `about` is what a "
                f"reviewer reads to learn the module's shape; with no count in it, "
                f"a wrong one cannot be found here either.")

    lengths = sorted(len(c) for c in stated.values())
    print(f"  structure: {len(stated)} citations for {count} release case(s), "
          f"{lengths[0]}-{lengths[-1]} characters, "
          f"{sum(1 for c in stated.values() if len(c) < SHORT)} short")


# ---------------------------------------------------------------------------
# the groups, and running them
# ---------------------------------------------------------------------------

GROUPS = {
    "bytecode": check_bytecode,
    "contract": check_contract,
    "counter": check_counter,
    "duplicates": check_duplicates,
    "gates": check_gates,
    "pins": check_pins,
    "reference": check_reference,
    "structure": check_structure,
    "tree": check_tree,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="groups: " + " ".join(sorted(GROUPS)))
    parser.add_argument(
        "--only", action="append", choices=sorted(GROUPS), metavar="GROUP",
        help="run only this group; repeatable")
    args = parser.parse_args(argv)

    # The docstring lists the groups, and a reader trusts that list in proportion
    # to how complete it looks.  A sibling task shipped one that named five while
    # the code held eight, so the list is compared with the code rather than
    # maintained beside it.  Checked before any group runs: a `--only` naming a
    # group the docstring omits should still report the omission.
    documented = set(re.findall(r"^    ([a-z-]+)\s{2,}\S", __doc__, re.M))
    if documented != set(GROUPS):
        print(f"check-task.py: the module docstring documents {sorted(documented)} "
              f"and GROUPS holds {sorted(GROUPS)}", file=sys.stderr)
        return 2

    selected = sorted(set(args.only)) if args.only else sorted(GROUPS)
    failed: dict[str, list[str]] = {}
    for name in selected:
        print(f"{name}:")
        problems: list[str] = []
        GROUPS[name](problems)
        if problems:
            failed[name] = problems

    if not failed:
        print(f"\nall groups ok ({', '.join(selected)})")
        return 0

    total = sum(len(p) for p in failed.values())
    print(f"\n{total} problem(s) in {len(failed)} of {len(selected)} group(s):",
          file=sys.stderr)
    for name, problems in sorted(failed.items()):
        for problem in problems:
            print(f"  {name}: {problem}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
