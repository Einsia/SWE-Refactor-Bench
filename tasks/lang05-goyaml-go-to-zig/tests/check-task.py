#!/usr/bin/env python3
"""Authoring-time checks that span files no single image can see.

Run from anywhere:

    python3 tasks/lang05-goyaml-go-to-zig/tests/check-task.py

One group per kind of claim.  `--only` takes any of these, repeatably:

    bytecode     each stage Dockerfile's bytecode sweep against its own assertion
    contract     the figures in evaluation.toml's gate questions and scan.toml's
                 prose, against source-contract.json and the shipped tarball
    cross-image  claims one build context makes about a file in another
    drivers      graded_builds.drivers against state_a, state_b, the provenance
                 catalog and the resolver that reads it
    duplicates   files that exist twice because no Dockerfile can COPY ../
    figures      instruction.md's scoring numbers against the loaders
    hygiene      artefacts a host-side check leaves in the tree that ships
    screen       the verification screen against the prompt it has to accept
    vocabulary   the anti-cheat token lists against what the author was given, and
                 the words this task does not ship

The list above is checked against GROUPS at startup, because a reader trusts a list
like this in proportion to how complete it looks -- and a group missing from it is a
group nobody knows to run.

Why any of this exists outside the images.  This task has three Docker build
contexts -- `tests/audit/`, `tests/behavioural/`, `tests/verification/` --
and a build cannot reach outside its own.  So no image can read two of them at
once, and `instruction.md`, `task.toml`, `tests/evaluation.toml` and
`environment/source-contract.json` sit outside all three.  `swerefactor validate`
does not close the gap either: it reads the config files and the stage layout,
never a task's own Python.

That leaves the cross-file claims unchecked by anything, and the checks below exist
because this class of mistake is silent.  A count in a gate question, a suffix list
one scanner has and another does not, a pin shared by two Dockerfiles -- each stays
green in every suite, stays self-consistent inside its own file, and is wrong in a
directory nothing compares.

`tests/verification/check-probe.py` names this file as its host-side counterpart: it
records two numbers it cannot derive from inside its own build context
(`EXPECTED_ADVERSARIES`, `NOT_GRADED_SURFACES`), and the `cross-image` group here is
what compares them against the files they describe.

`contract`, `cross-image` and `figures` need the harness's config loader, found at
$SRB_INFRA or in `infra/` beside `tasks/`.  `vocabulary` and `hygiene` need no
imports, so they still run in a tree that has only `tasks/`.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent

LIB = HERE / "behavioural" / "lib"
SCAN_LIB = HERE / "audit" / "lib"
VERIFICATION = HERE / "verification"
EVALUATION = HERE / "evaluation.toml"
SUITE = HERE / "behavioural" / "suite.toml"
PROBE_TOML = VERIFICATION / "probe.toml"
SCAN_TOML = HERE / "audit" / "scan.toml"
INSTRUCTION = TASK / "instruction.md"
TASK_TOML = TASK / "task.toml"
ENVIRONMENT = TASK / "environment"
CONTRACT = ENVIRONMENT / "source-contract.json"
#: The declared case counts.  Verifier-side, and not in the contract: the contract
#: is handed to the agent, and how many cases the port is measured against is not
#: part of what State B has to be.  freeze.py asserts this file against the suite
#: it just froze; `check_counts` asserts the prose against this file.
CASE_COUNTS = HERE / "behavioural" / "data" / "case-counts.json"


def flatten(text: str) -> str:
    """Collapse whitespace runs, so a matcher cannot depend on where prose wraps.

    Every prose matcher below goes through this.  The reason is measured, not
    theoretical: on the sibling task 6 of 11 checker failures were the checker
    matching against a line break rather than against the text, which reports a
    sentence as absent when it is merely rewrapped -- and an absent sentence and a
    wrong one need opposite fixes.
    """
    return re.sub(r"\s+", " ", text)


def load_harness():
    """Import `swerefactor.config`, the same way score.py finds the harness."""
    if "swerefactor" not in sys.modules:
        for candidate in (Path(os.environ.get("SRB_INFRA", "/opt/swerefactor")),
                          TASK.parent.parent / "infra"):
            if (candidate / "swerefactor" / "__init__.py").exists():
                sys.path.insert(0, str(candidate))
                break
    try:
        from swerefactor import config
    except ImportError as exc:
        raise SystemExit(
            f"cannot import the swerefactor harness: {exc}\n"
            f"expected it at $SRB_INFRA or in infra/ beside tasks/.  In a tree "
            f"holding only tasks/, point SRB_INFRA at the infra/ of a checkout "
            f"that has one, or run --only vocabulary --only hygiene, which need "
            f"no imports.")
    return config


def literal(path: Path, name: str, *, anywhere: bool = False):
    """One constant, read without importing the module that defines it.

    Stage 2's lib imports siblings that exist only inside the image, and importing
    any of it also writes bytecode into a tree that ships.  The constants wanted
    here are literals, so parsing is both sufficient and the only thing that works.

    Both assignment forms are handled.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = ast.walk(tree) if anywhere else tree.body
    for node in nodes:
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name) and target.id == name:
            return ast.literal_eval(node.value)
    raise SystemExit(f"{path.name}: no {name}")


def literal_in_function(path: Path, function: str, name: str):
    """One constant assigned inside a function body, without importing the module.

    `literal` reads module scope.  This reads one level in, for the case where the
    authoritative copy of a small set lives in the function that uses it -- the
    alternative being a module constant duplicating it, which is the drift this
    file exists to catch.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != function:
            continue
        for statement in ast.walk(node):
            target = None
            if isinstance(statement, ast.AnnAssign):
                target = statement.target
            elif (isinstance(statement, ast.Assign)
                  and len(statement.targets) == 1):
                target = statement.targets[0]
            if isinstance(target, ast.Name) and target.id == name:
                return ast.literal_eval(statement.value)
        raise SystemExit(f"{path.name}: {function}() has no {name}")
    raise SystemExit(f"{path.name}: no def {function}")


def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def state_a_files() -> list[str]:
    """Every file in the shipped tarball, by archive name.

    Read from `environment/original.tar.gz` rather than from a working copy,
    because what the author gets is the archive.  A check against a checkout would
    pass while the shipped bytes said something else.
    """
    with tarfile.open(ENVIRONMENT / "original.tar.gz", "r:gz") as archive:
        return [m.name for m in archive.getmembers() if m.isfile()]


def go_populations() -> dict[str, list[str]]:
    """The tarball's .go files, split into the two populations that exist.

    State A ships Go for two different reasons and the prose counts them
    separately, so a checker that counts "the .go files" measures neither.

      library  the root of the module: `gopkg.in/yaml.v3` as upstream published it.
               This is what is being ported, and what instruction.md's opening
               sentence describes -- thirteen non-test files, 11,285 raw lines.
      harness  `probe/` and `cmd/yaml-probe/`: the NDJSON adapter State A builds
               its probe binary from, and which `freeze.py` also compiles into the
               reference so the rule turning a node into response bytes has one
               implementation.  Not upstream's, and not the library.

    Split on the archive path rather than on a list of names, because a list would
    need editing every time either population gained a file -- and the failure mode
    of a stale list here is a count that stops measuring what its sentence says.

    The distinction is load-bearing for the counts and for nothing else: both
    populations are `.go`, both are forbidden in a submission, and stage 1 judges a
    leftover from either the same way.
    """
    out: dict[str, list[str]] = {"library": [], "harness": []}
    for name in state_a_files():
        if not name.endswith(".go"):
            continue
        # Archive names are `repo/...`; the library is what sits directly in it.
        rel = name.split("/", 1)[1] if "/" in name else name
        out["library" if "/" not in rel else "harness"].append(rel)
    return out


def state_a_raw_go_lines() -> int:
    """Raw lines in the *library*'s non-test .go files -- `wc -l`, not logic lines.

    A second, different measure of the same tree, and the reason it needs saying:
    the contract's `go_logic_lines` is 7,609 over the twelve *graded* files with
    blanks and comments dropped, and instruction.md's opening says 11,285 over the
    thirteen *non-test* files counting everything.  Both are right.  Two plausible
    line counts for one repository is exactly the shape that gets one of them
    quietly replaced by the other, so each is checked against what it measures.

    Library only -- `go_populations()['library']`.  Counting the probe harness too
    would add 623 lines to a figure whose sentence is about what is being ported,
    and the harness is not: it is the adapter the protocol is spoken through.
    """
    library = set(go_populations()["library"])
    total = 0
    with tarfile.open(ENVIRONMENT / "original.tar.gz", "r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.split("/")[-1]
            if not member.isfile() or not name.endswith(".go"):
                continue
            if name.endswith("_test.go") or name not in library:
                continue
            handle = archive.extractfile(member)
            if handle is None:                      # pragma: no cover - not a file
                continue
            # splitlines() rather than count("\n"): a file with no trailing newline
            # has one fewer newline than lines, and `wc -l` and an editor disagree
            # about it.  The stated figure came from `cat | wc -l`, so a file
            # missing its final newline would make the two differ by one per file.
            total += len(handle.read().decode("utf-8", "replace").splitlines())
    return total


#: Numbers prose writes as words.  Only the values that actually appear: an unknown
#: word is a failure rather than a skip, because a bare digit lookup would pass
#: silently on any number this table does not know.
WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "twenty-one": 21, "twenty-two": 22, "twenty-four": 24,
}


def number(section: str, pattern: str, label: str, problems: list[str]):
    """One figure out of prose, as an int, or None with a problem recorded.

    `pattern` must have exactly one group, over a digit run or a written word.
    """
    match = re.search(pattern, section, re.IGNORECASE)
    if match is None:
        problems.append(
            f"no {label} found -- expected text matching /{pattern}/")
        return None
    raw = match.group(1).replace(",", "").lower()
    if raw.isdigit():
        return int(raw)
    if raw in WORDS:
        return WORDS[raw]
    problems.append(f"{label} reads {raw!r}, which is neither a number nor a word "
                    f"this check knows")
    return None


def compare(stated, actual, label: str, source: str, problems: list[str]) -> None:
    """Record a problem when a stated figure and a measured one disagree."""
    if stated is not None and stated != actual:
        problems.append(f"{label}: the text says {stated}, {source} says {actual}")


def check_contract(problems: list[str]) -> None:
    """The figures the stage-1 prose states, against the contract and the tarball.

    Stage 1 is a reading task, and the questions put in front of the reviewer carry
    numbers: twenty-four Go files in two populations -- seventeen sources, thirteen
    of them the library's and four the probe adapter's, and seven _test.go, six and
    one the same way -- twelve graded, 7,609 lines, a 4,000-line floor, twenty-four
    scanned suffixes, seven forbidden paths.  Each is a claim about a file the prose
    does not include.

    instruction.md's opening states the same tree a third way -- thirteen non-test
    files, 11,285 raw lines -- and those are checked here too, against the tarball
    rather than against the other two figures.  Three counts of one repository, all
    correct and all measuring something different, is a standing invitation to
    "fix" one by copying another.

    A wrong figure here is not cosmetic.  The reviewer is told what State A holds
    and answers a pass/fail gate against it, so a count that is off by one either
    invites a finding where there is nothing wrong, or -- the direction that
    actually loses information -- describes a smaller State A than shipped and
    leaves whatever is past the count unexamined.  Nothing else compares these:
    the numbers live in prose, the answers live in JSON and a tarball, and the
    stage that reads the prose has neither.
    """
    con = contract()
    state_a = con["state_a"]
    names = [n.split("/")[-1] for n in state_a_files()]
    populations = go_populations()
    # Every .go in the tarball, and the library's alone.  Both are used below, for
    # sentences that count different things: the gate question enumerates the whole
    # tree because that is what has to be gone, and instruction.md's opening
    # describes the library because that is what is being ported.  Using one figure
    # for both was the bug -- the harness landed in the tarball and four prose
    # figures about the library started failing against a population that had grown.
    go = [n for n in names if n.endswith(".go")]
    tests = [n for n in go if n.endswith("_test.go")]
    sources = [n for n in go if not n.endswith("_test.go")]
    lib_tests = [n for n in populations["library"] if n.endswith("_test.go")]
    lib_sources = [n for n in populations["library"]
                   if not n.endswith("_test.go")]
    harness_tests = [n for n in populations["harness"] if n.endswith("_test.go")]
    harness_sources = [n for n in populations["harness"]
                       if not n.endswith("_test.go")]
    graded = state_a["graded_sources"]
    anchors = state_a["anchor_files"]

    # The split itself, against the contract.  `graded_sources` is a subset of the
    # library by construction, and if the population function ever mis-sorted a file
    # the counts above would all still be self-consistent.
    stray = sorted(set(graded) - set(populations["library"]))
    if stray:
        problems.append(
            f"go_populations() does not put {stray} in the library, but "
            f"state_a.graded_sources does; the split is wrong and every figure "
            f"below is measured over the wrong set")
    if not populations["harness"]:
        problems.append(
            "go_populations() finds no probe harness in the tarball; State A has no "
            "protocol adapter to build its probe from, and freeze.py's "
            "build_instruments raises on it")

    # Two contract keys hold the same twelve names for different readers, and
    # `freeze.py` grades against one while the gate question counts the other.
    if sorted(graded) != sorted(anchors):
        problems.append(
            f"source-contract.json: state_a.graded_sources and state_a.anchor_files "
            f"are meant to be the same twelve files, but differ: "
            f"only in graded_sources {sorted(set(graded) - set(anchors))}, "
            f"only in anchor_files {sorted(set(anchors) - set(graded))}")

    per_file = state_a["go_logic_lines_per_file"]
    if sum(per_file.values()) != state_a["go_logic_lines"]:
        problems.append(
            f"source-contract.json: go_logic_lines_per_file sums to "
            f"{sum(per_file.values())}, go_logic_lines says "
            f"{state_a['go_logic_lines']}")
    if sorted(per_file) != sorted(graded):
        problems.append(
            f"source-contract.json: go_logic_lines_per_file covers "
            f"{sorted(per_file)}, which is not the graded set {sorted(graded)}")
    if state_a["file_count"] != len(names):
        problems.append(f"source-contract.json: state_a.file_count says "
                        f"{state_a['file_count']}, the tarball holds {len(names)} "
                        f"files")

    evaluation = flatten(EVALUATION.read_text(encoding="utf-8"))

    # `no-go-sources`: the one gate question that enumerates State A to the reviewer.
    closure = re.search(r"State A ships (\S+) `?\.?go`? files.{0,700}", evaluation,
                        re.IGNORECASE)
    if closure is None:
        problems.append("evaluation.toml: the no-go-sources question no longer opens "
                        "with 'State A ships N .go files', so its figures cannot be "
                        "located; update this check with it")
    else:
        text = closure.group(0)
        compare(number(text, r"State A ships (\S+) `?\.?go`? files", "go file count",
                       problems), len(go), "no-go-sources go files",
                "the tarball", problems)
        compare(number(text, r"(\w+) sources\b", "source count", problems),
                len(sources), "no-go-sources sources", "the tarball", problems)
        # Every figure in the sentence, not the two that a pattern happened to still
        # match.  The question describes both populations -- seventeen sources as the
        # library's thirteen plus four adapter files, seven tests as six plus one --
        # and each of those six numbers is a claim about the tarball.  A pattern that
        # goes stale reports "no count found", which is the right failure: a figure
        # that moved out from under its matcher is unchecked, and unchecked reads as
        # passing.
        compare(number(text, r"the library's (\w+) --", "library source count",
                       problems), len(lib_sources),
                "no-go-sources library sources", "the tarball", problems)
        compare(number(text, r"and (\w+) more under", "adapter source count",
                       problems), len(harness_sources),
                "no-go-sources adapter sources", "the tarball", problems)
        compare(number(text, r"(\w+) _test\.go sit beside", "test count", problems),
                len(tests), "no-go-sources _test.go", "the tarball", problems)
        compare(number(text, r"(\w+) of the library's own", "library test count",
                       problems), len(lib_tests),
                "no-go-sources library _test.go", "the tarball", problems)
        compare(number(text, r"and (\w+) of the adapter's", "adapter test count",
                       problems), len(harness_tests),
                "no-go-sources adapter _test.go", "the tarball", problems)
        compare(number(text, r"(\w+) of the library's \w+ are graded",
                       "graded count", problems), len(graded),
                "no-go-sources graded", "the contract", problems)
        compare(number(text, r"of the library's (\w+) are graded",
                       "library total", problems), len(lib_sources),
                "no-go-sources library total", "the tarball", problems)

    # instruction.md's own description of State A.  Separate from the gate question
    # above because it counts a different set with a different measure: the
    # *library*, thirteen non-test files and 11,285 raw lines, where the gate counts
    # every .go file in the tree and the contract counts 7,609 logic lines over the
    # twelve it grades.  Three figures for one tree; the risk is not that one is
    # wrong today but that a later edit reconciles them by making two of them agree.
    #
    # Measured against the library rather than the tarball, and that distinction is
    # the whole of a bug: when the probe harness was added to State A these four
    # comparisons started failing, and every one of them was right -- the sentences
    # are about the library and the harness is not in it.  Fixing the numbers would
    # have made the document describe a population it does not discuss.
    instruction = flatten(INSTRUCTION.read_text(encoding="utf-8"))
    compare(number(instruction, r"([\d,]+) lines of Go across", "raw Go lines",
                   problems), state_a_raw_go_lines(), "instruction.md raw Go lines",
            "the tarball's non-test library .go files", problems)
    compare(number(instruction, r"lines of Go across (\S+) non-test files",
                   "non-test file count", problems), len(lib_sources),
            "instruction.md non-test files", "the library", problems)
    # These two land on the "What must be gone" bullet, which is the one sentence in
    # the document counting the whole tree rather than the library: everything that
    # leaves, adapter included.  So they are the tarball's figures, unlike the two
    # above -- the opening paragraph describes what is being ported and this bullet
    # describes what is deleted, and those are different sets by five files.
    compare(number(instruction, r"all (\w+) `_test\.go` files", "test file count",
                   problems), len(tests), "instruction.md _test.go files",
            "the tarball", problems)
    compare(number(instruction, r"all (\w+) implementation files",
                   "implementation file count", problems), len(sources),
            "instruction.md implementation files", "the tarball", problems)
    # And the bullet's own split, which is what makes the two totals above legible
    # rather than a pair of numbers a reader has to trust.
    gone_bullet = re.search(r"all \w+ implementation files.{0,160}", instruction)
    if gone_bullet is None:
        problems.append(
            "instruction.md's 'What must be gone' no longer opens its `*.go` bullet "
            "with 'all N implementation files', so its split cannot be located")
    else:
        text = gone_bullet.group(0)
        compare(number(text, r"the library's (\w+) and", "library split",
                       problems), len(lib_sources),
                "instruction.md's gone-bullet library sources", "the tarball",
                problems)
        compare(number(text, r"the library's \w+ and (\w+)", "library test split",
                       problems), len(lib_tests),
                "instruction.md's gone-bullet library tests", "the tarball",
                problems)
        compare(number(text, r"adapter's (\w+) and", "adapter split", problems),
                len(harness_sources),
                "instruction.md's gone-bullet adapter sources", "the tarball",
                problems)
        compare(number(text, r"adapter's \w+ and (\w+)", "adapter test split",
                       problems), len(harness_tests),
                "instruction.md's gone-bullet adapter tests", "the tarball",
                problems)

    # instruction.md's own list of what must be gone, against the contract's.  The
    # document says it is the evaluation standard and the contract says it is
    # authoritative where they overlap, so a submission reading only one of them has
    # to get the same answer.  Both directions: a pattern the document omits is a
    # file deleted for a reason the author was never told, and one it adds is a
    # promise nothing enforces.
    gone = instruction_section(
        INSTRUCTION.read_text(encoding="utf-8"), "### What must be gone")
    if gone is None:
        problems.append(
            "instruction.md has no '### What must be gone' section, so the author is "
            "not told what leaves the tree; either add it or update this check with "
            "the heading that replaced it")
    else:
        # Scope is the bullet list, not the whole section: the prose after it names
        # `scannerc.go`, `docs/` and `zig-out/` in sentences about what does *not*
        # count as removal, and those are not claims about the forbidden set.
        #
        # Then every backticked token in the bullets is classified rather than
        # filtered.  Filtering makes the check vacuous: if `stated_names` is built
        # as "tokens that are in the contract", then `stated - contract` is empty by
        # construction and an invented name sails through.  A token has to land in
        # exactly one bucket and be wrong somewhere, or the direction it would have
        # failed in does not exist.
        # A bullet is its `- ` line plus any indented continuation lines.  Matching
        # `^\s*[-*] ` alone silently drops three extensions, because the artefact
        # bullet wraps and `*.obj`, `*.lib` and `*.wasm` sit on the continuation.
        # Reflowing that bullet to one line would "fix" the check by editing the
        # document it audits.
        bullets: list[str] = []
        for line in gone.splitlines():
            if re.match(r"\s*[-*] ", line):
                bullets.append(line.strip())
            elif bullets and line.startswith((" ", "\t")) and line.strip():
                bullets[-1] += " " + line.strip()
            elif not line.strip():
                bullets.append("")           # blank line closes the current bullet
        quoted = set(re.findall(r"`([^`]+)`", "\n".join(bullets)))
        contract_names = set(con["forbidden_paths"]["names"])
        contract_exts = set(con["forbidden_paths"]["extensions"])
        stated_exts, stated_names = set(), set()
        for token in quoted:
            bare = token.rstrip("/")
            if re.fullmatch(r"\*?\.[a-z0-9]+", bare):
                stated_exts.add(bare.lstrip("*"))
            elif bare in contract_names:
                # Before the suffix rule, or `Makefile.go` reads as an example of
                # the `.go` pattern and its own entry in the name list goes unread.
                stated_names.add(bare)
            elif any(bare.endswith(ext) for ext in contract_exts):
                # `_test.go`: an example of a pattern the extension list already
                # forbids, so the promise is enforced without a name entry.
                continue
            else:
                # Everything left is an unbacked promise.  This branch is the only
                # reason the `stated - actual` loop below can fire at all.
                stated_names.add(bare)
        for label, stated, actual in (("extensions", stated_exts, contract_exts),
                                      ("names", stated_names, contract_names)):
            for missing in sorted(actual - stated):
                problems.append(
                    f"the contract's forbidden_paths.{label} has {missing!r} and "
                    f"instruction.md's 'What must be gone' never names it, so a "
                    f"submission that keeps it fails a gate the document did not "
                    f"state")
            for extra in sorted(stated - actual):
                problems.append(
                    f"instruction.md says {extra!r} must be gone and the contract's "
                    f"forbidden_paths.{label} does not list it, so nothing enforces "
                    f"it -- either add it to the contract or stop promising it")

    # The floor, and the figure it is justified against.  Both stated in two files.
    #
    # Three spellings, so this one cannot go through `number`: that helper requires a
    # single group and reads group(1), while an alternation returns None for every
    # branch that did not match -- group(1) would be empty on exactly the input where
    # the second or third spelling is the one in use.  Hence the direct search below
    # and `next(g for g in match.groups() if g)` to take whichever branch fired.
    floor = con["native_code_policy"]["min_zig_logic_lines"]
    # Anchored on "Zig", because both files also state an unrelated floor -- the
    # scan's collection floor -- which a bare /floor of (\d+)/ would match instead.
    floor_pattern = (r"floor of ([\d,]+) non-blank"
                     r"|names a floor of ([\d,]+)[^.]*?of Zig"
                     r"|([\d,]+)-line Zig logic")
    for path, label in ((EVALUATION, "evaluation.toml"), (SCAN_TOML, "scan.toml")):
        text = flatten(path.read_text(encoding="utf-8"))
        match = re.search(floor_pattern, text, re.IGNORECASE)
        if match is None:
            problems.append(f"{label} states no Zig logic-line floor; the contract "
                            f"sets one at {floor} and both files explain it to a "
                            f"reader")
        else:
            stated = int(next(g for g in match.groups() if g).replace(",", ""))
            compare(stated, floor, f"{label} Zig floor", "the contract", problems)
        stated_go = number(text, r"([\d,]+) (?:non-blank non-comment )?lines of "
                                 r"(?:graded )?Go", f"{label} Go lines", problems)
        compare(stated_go, state_a["go_logic_lines"], f"{label} Go lines",
                "the contract", problems)

    # The same two figures in instruction.md, which the loop above cannot cover for
    # two reasons.  Its phrasings differ ("At least **4,000**", "7,609 such lines"),
    # and -- the one that matters -- a document-wide search for `([\d,]+) lines of
    # Go` finds the opening paragraph's 11,285 first, which is a raw-line count of
    # thirteen files and would be reported as disagreeing with a logic-line count of
    # twelve.  Two true numbers, so the scope is the section, not the file.
    # Re-read rather than reusing `text`: the loop above rebinds it once per file,
    # so by here it holds scan.toml.  And `instruction_section` needs real newlines
    # to find a heading, so this is the raw document, flattened after the cut.
    floor_section = instruction_section(
        INSTRUCTION.read_text(encoding="utf-8"), "### The floor")
    if floor_section is None:
        problems.append("instruction.md has no '### The floor' section, so neither "
                        "the Zig floor nor the Go figure it is justified against is "
                        "checked against the contract in the document a submitter "
                        "is told is the standard")
    else:
        floor_section = flatten(floor_section)
        compare(number(floor_section, r"At least \*{0,2}([\d,]+)\*{0,2} non-blank",
                       "instruction.md Zig floor", problems),
                floor, "instruction.md Zig floor", "the contract", problems)
        compare(number(floor_section, r"([\d,]+) such lines of graded Go",
                       "instruction.md Go lines", problems),
                state_a["go_logic_lines"], "instruction.md Go lines",
                "the contract", problems)
        # "graded Go" is a smaller population than the thirteen files this same
        # document counts in its opening paragraph, and instruction.md defines the
        # word nowhere else.  So the section has to name every non-test source the
        # contract does not grade, or a submitter reading the floor cannot tell
        # which twelve of the thirteen files it was measured over -- and the floor
        # is the one figure in the document that decides a gate.
        #
        # Not "so the reader can subtract": 11,285 is a total-line count including
        # blanks and 7,609 is a logic-line count, so their difference is 3,676 and
        # naming a 110-line file explains none of it.  The two figures are simply
        # different measures of different populations, which is exactly why the
        # smaller one needs its scope named.
        #
        # Derived from the tarball and the contract rather than written here: a
        # hard-coded "sorter.go" would still pass on the day the graded set
        # changed, which is the only day this matters.
        #
        # The library, not the tarball.  The probe harness is also ungraded and also
        # `.go`, and requiring the floor section to name `jenc.go` would be asking a
        # paragraph about how much Zig the port needs to enumerate files that are not
        # the port -- the reader's question here is which of the library's thirteen
        # the 7,609 covers.  That the harness exists at all is the opening
        # paragraph's job, and `check_figures` is what holds it to it.
        for ungraded in sorted(set(lib_sources) - set(graded)):
            if ungraded not in floor_section:
                problems.append(
                    f"instruction.md's floor is justified against 'graded Go', "
                    f"which excludes {ungraded} -- the section never names it, so "
                    f"the population that figure was measured over is undefined in "
                    f"the document a submitter is told is the standard")

    # scan.toml's own two counts, against the scanner it describes.
    scan = flatten(SCAN_TOML.read_text(encoding="utf-8"))
    suffixes = (literal(SCAN_LIB / "srbscan.py", "GO_SOURCE_SUFFIXES")
                + literal(SCAN_LIB / "srbscan.py", "C_SOURCE_SUFFIXES")
                + literal(SCAN_LIB / "srbscan.py", "BINARY_SUFFIXES"))
    compare(number(scan, r"(\S+) Go, C-family and binary suffixes", "suffix count",
                   problems), len(suffixes), "scan.toml suffixes",
            "srbscan.py's three suffix tuples", problems)
    compare(number(scan, r"the (\w+) forbidden paths", "forbidden path count",
                   problems), len(con["forbidden_paths"]["names"]),
            "scan.toml forbidden paths", "the contract", problems)

    # srbscan's forbidden names and retained paths are a second copy of the
    # contract's, in a build context that cannot read the contract.
    scan_names = literal(SCAN_LIB / "srbscan.py", "FORBIDDEN_NAMES")
    if sorted(scan_names) != sorted(con["forbidden_paths"]["names"]):
        problems.append(f"srbscan.py FORBIDDEN_NAMES {sorted(scan_names)} is not the "
                        f"contract's forbidden_paths.names "
                        f"{sorted(con['forbidden_paths']['names'])}")
    retained = literal(SCAN_LIB / "srbscan.py", "RETAINED_PATHS")
    required = con["retained_paths"]["required"]
    if sorted(retained) != sorted(required):
        problems.append(f"srbscan.py RETAINED_PATHS {sorted(retained)} is not the "
                        f"contract's retained_paths.required {sorted(required)}")

    # --- the scan's own three numbers ----------------------------------------
    # scan.toml explains the collection floor to a reader; the Dockerfile passes it
    # to collect-check.sh as an argument, and the script has a different default.
    # Three numbers, two files, one command line, and the failure is quiet in the
    # worst way: a floor that drops below what a broken scan still collects lets a
    # scan that produces no findings look like a clean tree.
    scan_docker = (HERE / "audit" / "Dockerfile").read_text(
        encoding="utf-8")
    invocation = re.search(r"collect-check\.sh\s+(\d+)", scan_docker)
    if invocation is None:
        problems.append(
            "the audit Dockerfile does not pass a floor to "
            "collect-check.sh, so the script's own default applies; the default is "
            "deliberately lower than this scan's measured count and a silent scan "
            "would clear it")
    else:
        passed = int(invocation.group(1))
        compare(number(scan, r"fails the build under a floor of (\d+)",
                       "scan.toml collection floor", problems), passed,
                "scan.toml collection floor",
                "the floor the Dockerfile passes", problems)
        full = number(scan, r"(\d+) checks against a real pair", "scan check count",
                      problems)
        build_time = number(scan, r"(\d+) at build time", "build-time check count",
                            problems)
        if build_time is not None and build_time < passed:
            problems.append(
                f"scan.toml says {build_time} checks collect at build time and the "
                f"Dockerfile fails under {passed}; the build cannot pass")
        if full is not None and build_time is not None and full < build_time:
            problems.append(
                f"scan.toml says {full} checks against real trees and {build_time} "
                f"at build time; the build-time count is the one with the mount "
                f"empty and cannot be the larger")
        # The Dockerfile explains the same three numbers in its own comment.
        # Matched by pattern on both sides rather than by asking whether the digits
        # appear anywhere in the file: a bare `str(n) in text` finds "99" inside a
        # sha256 digest, and this check passed a mutation because of it.
        flat_docker = flatten(scan_docker)
        for label, mine, pattern in (
            ("full check count", full, r"(\d+) file-reading checks"),
            ("build-time count", build_time, r"is (\d+): below the"),
            ("collection floor", passed, r"floor is (\d+)"),
        ):
            theirs = number(flat_docker, pattern,
                            f"the Dockerfile's {label}", problems)
            if mine is not None and theirs is not None and mine != theirs:
                problems.append(
                    f"the scan's {label}: scan.toml says {mine}, the "
                    f"audit Dockerfile's comment says {theirs}.  Both "
                    f"describe the same measured collection and a reader of either "
                    f"one is being told the wrong size")

    # Every extension the contract forbids must be one the scanner opens its eyes
    # to.  The scanner may know more (it does: 9 more, covered in the contract's
    # forbidden_build_actions prose); it may not know fewer.
    missing = sorted(set(con["forbidden_paths"]["extensions"]) - set(suffixes))
    if missing:
        problems.append(f"the contract forbids {missing} but srbscan.py scans for "
                        f"no such suffix, so stage 1 cannot see them")


#: Words this task does not use in prose the author reads.
#:
#: Not a style rule.  `corpus` and `fixture` name nothing in the world the author
#: is working in -- they are harness vocabulary, and a task document written in it
#: describes the grader instead of the job.  `fixture` additionally collides with
#: pytest's own meaning inside these suites, so the same word means two things
#: depending on which file it is in.  The replacements are concrete: the frozen
#: cases are cases, the YAML they parse are documents.
#:
#: Scope is prose the author is handed, not the suites' own Python: `@pytest.fixture`
#: is an API name and renaming it would be a different kind of mistake.
BANNED_WORDS = ("corpus", "corpora", "fixture", "fixtures")

#: The prose an author actually reads, in the order they meet it.
AUTHOR_DOCUMENTS = (
    ("instruction.md", INSTRUCTION),
    ("task.toml", TASK_TOML),
    ("README.swerefactor.md", None),   # read out of the tarball
)


def author_prose() -> list[tuple[str, str]]:
    """Each author-visible document as (label, text), tarball included."""
    out = []
    for label, path in AUTHOR_DOCUMENTS:
        if path is not None:
            out.append((label, path.read_text(encoding="utf-8")))
            continue
        with tarfile.open(ENVIRONMENT / "original.tar.gz", "r:gz") as archive:
            member = next((m for m in archive.getmembers()
                           if m.name.endswith(label)), None)
            if member is None:
                raise SystemExit(f"original.tar.gz no longer holds {label}; this "
                                 f"check and environment/Dockerfile disagree about "
                                 f"what the author is given")
            out.append((label, archive.extractfile(member).read().decode("utf-8")))
    return out


def where(text: str, needle: str) -> str:
    """Line numbers a word appears on, for a message that can be acted on."""
    lines = [str(i) for i, line in enumerate(text.splitlines(), 1)
             if re.search(rf"\b{needle}\b", line, re.IGNORECASE)]
    return ", ".join(lines[:8]) + (" ..." if len(lines) > 8 else "")


#: Files that exist twice because two build contexts each need them and neither can
#: read the other's directory.  Authoritative copy first.
#:
#: `environment/` is authoritative for the contract: it is what the author is handed
#: and what `verify_environment.py` checks the extracted tree against.  The copy in
#: `tests/behavioural/data/` exists because a Dockerfile cannot `COPY ../`.
DUPLICATED_FILES = (
    ("environment/source-contract.json", "tests/behavioural/data/source-contract.json"),
)


def check_duplicates(problems: list[str]) -> None:
    """Files with two homes, byte for byte.

    Found by making the mistake: a block added to the contract in `environment/`
    left the copy under `tests/behavioural/data/` a version behind, and nothing
    noticed.  That drift is the quiet kind -- the author reads one file and the
    verifier grades against the other, so the two disagree about the contract in
    exactly the direction that produces an unexplainable score.  (The block in
    question was `grading.case_counts`, which no longer lives in the contract at
    all: the counts moved to `tests/behavioural/data/case-counts.json`, because the
    contract is shipped to the agent and the size of the graded suite is not part
    of what State B has to be.  The check outlived its example, which is the
    ordinary fate of a good check.)

    Compared as bytes rather than as parsed JSON, deliberately.  Equal-after-parsing
    would let the two files differ in key order and formatting, and then a diff
    between them is unreadable for the next person who has to check by hand.
    """
    for first, second in DUPLICATED_FILES:
        left, right = TASK / first, TASK / second
        for path in (left, right):
            if not path.is_file():
                problems.append(f"{path.relative_to(TASK)} is one of a pair that "
                                f"must be identical, and it does not exist")
                break
        else:
            if left.read_bytes() != right.read_bytes():
                problems.append(
                    f"{first} and {second} are the same file in two build contexts "
                    f"and their bytes differ.  {first} is authoritative:\n"
                    f"    cp {first} {second}\n"
                    f"  The author reads the first and the verifier grades against "
                    f"the second, so a drift here is a submission scored on a "
                    f"contract it was never shown.")


def check_vocabulary(problems: list[str]) -> None:
    """Words the author reads, and words the author is given.

    Two unrelated failures, both about vocabulary.

    The banned list is the one worth explaining.  These documents are read by
    someone doing a YAML port, and a document that calls its inputs a corpus of
    fixtures is describing the machinery rather than the work.  The words also
    carry no information the concrete ones do not: cases are cases, documents are
    documents.  Removing them is easy and keeping them out is not, which is why it
    is a check rather than a pass over the files.

    The second failure is the opposite direction: the anti-cheat token lists name
    things the author is *not* given, and a name that was never in the author's
    tree is a token the screen can never fire on.  A list of those reads as
    coverage and is decoration.
    """
    for label, text in author_prose():
        for word in BANNED_WORDS:
            if re.search(rf"\b{word}\b", text, re.IGNORECASE):
                problems.append(
                    f"{label} says {word!r} (line {where(text, word)}); this task's "
                    f"prose does not use harness vocabulary -- frozen inputs are "
                    f"cases, the YAML they hold are documents")


def check_screen(problems: list[str]) -> None:
    """The candidate screen, against the prompt that tells models what to write.

    Stage 3 hands six models a prompt and screens what they hand back.  Those
    two files are the same rule written twice, and they are in the same directory
    but nothing compares them: `screen-candidate.py`'s build-time check writes its
    own two candidates inline, so it proves the screen runs, not that the screen
    agrees with the instructions.

    A screen stricter than the prompt is expensive and silent.  Every round comes
    back INVALID, the adjudicator scores six INVALID rounds as SURVIVED, and a
    submission collects all 60 verification points from a screen that was refusing
    the example its own prompt printed.  So the prompt's example is extracted and
    run through the screen here.

    Also checked: every `FORBIDDEN_CODE` pattern matches at least the spelling it
    was written for.  A pattern that matches nothing is a rule that reads enforced.
    """
    sys.path.insert(0, str(VERIFICATION))
    try:
        import importlib.machinery
        import importlib.util
        spec = importlib.util.spec_from_loader(
            "screen_candidate",
            importlib.machinery.SourceFileLoader(
                "screen_candidate", str(VERIFICATION / "screen-candidate.py")))
        screen = importlib.util.module_from_spec(spec)
        sys.dont_write_bytecode = True   # this tree ships; see check_hygiene
        spec.loader.exec_module(screen)
    except Exception as exc:                       # noqa: BLE001 -- reported
        problems.append(f"cannot load screen-candidate.py to check it against "
                        f"prompt.txt: {type(exc).__name__}: {exc}")
        return

    prompt = VERIFICATION / "prompt.txt"
    text = prompt.read_text(encoding="utf-8")
    marker = "Your file may import"
    if marker not in text:
        problems.append(f"prompt.txt no longer says {marker!r} before its example, "
                        f"so the example cannot be located; update this check")
        return
    tail = text.split(marker, 1)[1]
    block, started = [], False
    for line in tail.splitlines():
        if line.startswith("    "):
            block.append(line[4:])
            started = True
        elif started and not line.strip():
            block.append("")
        elif started:
            break
    example = "\n".join(block).strip() + "\n"
    if "import srbyaml" not in example:
        problems.append("prompt.txt's example no longer imports srbyaml; either the "
                        "prompt changed shape or this extraction is wrong, and both "
                        "mean the screen is unchecked against the prompt")
        return

    refusals = screen.screen(example, "prompt.txt example")
    if refusals:
        problems.append(
            "screen-candidate.py refuses the example printed in prompt.txt: "
            + "; ".join(refusals)
            + ".  A model that follows the instructions is refused, every round "
              "is INVALID, and six INVALID rounds report as SURVIVED -- the "
              "submission collects all 60 verification points.")

    # Blanked the way the screen blanks it, so a pattern that only matches inside
    # a string literal counts as matching nothing -- which is what it is, since
    # the screen never sees literals.
    probes = screen.blank_literals(SCREEN_PROBES)
    for pattern, reason in screen.FORBIDDEN_CODE:
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            problems.append(f"FORBIDDEN_CODE pattern {pattern!r} does not compile: "
                            f"{exc}")
            continue
        if not compiled.search(probes):
            problems.append(
                f"FORBIDDEN_CODE pattern {pattern!r} ({reason.split('.')[0]}) "
                f"matches nothing in this check's probe text.  Either the rule is "
                f"dead, or SCREEN_PROBES needs the spelling it was written for -- "
                f"an unexercised pattern reads enforced and is not.")


def anchors(text: str) -> list[str]:
    """Distinctive tokens from a surface description, for a can't-be-vacuous match.

    Two kinds, because the descriptions use both: a quoted phrase (the aliasing
    guard is named only by the error text it prints) and an identifier carrying a
    dot, an underscore or an interior capital.  A dotted `yaml.X` also yields `X`,
    since the two files disagree about whether the package prefix is written.

    Every caller must check that this returned something before concluding a match
    failed.  A surface that yields no anchors is a surface this check cannot speak
    about, and treating that as a pass is how a coverage check becomes decoration.
    """
    found = list(re.findall(r"'([^']{8,})'", text))
    for token in re.findall(r"[A-Za-z_][A-Za-z0-9_.]{4,}", text):
        if any(ch.isupper() for ch in token) or "." in token or "_" in token:
            found.append(token)
            if token.lower().startswith("yaml."):
                found.append(token[5:])
    return [f for f in found if f]


def check_cross_image(problems: list[str]) -> None:
    """Claims one build context makes about a file in another.

    Three build contexts, none able to read another, plus four config files outside
    all three.  Everything below is a value written down in one context that
    describes a file in a different one, which makes this the only place the two
    halves are ever in the same process.
    """
    con = contract()
    config = load_harness()
    evaluation = config.Evaluation.load(EVALUATION)
    suite = config.Suite.load(SUITE)
    probe = config.Probe.load(PROBE_TOML)
    scoring = evaluation.scoring

    # --- stage 3's two host-side numbers -------------------------------------
    # check-probe.py runs inside the verification image and cannot read
    # evaluation.toml or the contract, so it writes both figures down and names
    # this file as the thing that checks them.
    expected = literal(VERIFICATION / "check-probe.py", "EXPECTED_ADVERSARIES")
    if expected != scoring.verification_models:
        problems.append(
            f"check-probe.py EXPECTED_ADVERSARIES = {expected}, evaluation.toml "
            f"[scoring] verification_models = {scoring.verification_models}")
    if len(probe.adversaries) != scoring.verification_models:
        problems.append(
            f"probe.toml defines {len(probe.adversaries)} adversaries, the scoring "
            f"policy pays {scoring.points_per_survived_model} each for "
            f"{scoring.verification_models}")
    surfaces = con["graded_surface"]["not_graded"]
    stated = literal(VERIFICATION / "check-probe.py", "NOT_GRADED_SURFACES")
    if stated != len(surfaces):
        problems.append(
            f"check-probe.py NOT_GRADED_SURFACES = {stated}, the contract marks "
            f"{len(surfaces)} surfaces not graded")

    # The points have to add up to what the ladder claims, in the file that claims
    # it -- 60 paid five at a time over six models is the whole of stage 3.
    if scoring.verification_models * scoring.points_per_survived_model != \
            scoring.verification_points:
        problems.append(
            f"{scoring.verification_models} models x "
            f"{scoring.points_per_survived_model} points = "
            f"{scoring.verification_models * scoring.points_per_survived_model}, "
            f"but verification_points = {scoring.verification_points}")
    if scoring.behavioural_points + scoring.verification_points != scoring.max_score:
        problems.append(
            f"behavioural_points + verification_points = "
            f"{scoring.behavioural_points + scoring.verification_points}, "
            f"max_score = {scoring.max_score}")
    # No threshold to validate: stage 2 pays behavioural_points or nothing, and
    # stage 3 is entered on the same event that pays it.  A [scoring] table cannot
    # put the gate somewhere the payment is not, because there is only one figure.
    budgets = {a.budget_sec for a in probe.adversaries}
    if len(budgets) != 1:
        problems.append(f"the six adversaries do not share one budget: "
                        f"{sorted(budgets)}; the prose and evaluation.toml both "
                        f"describe 'an hour each'")

    # --- every excluded surface is out of the adversary's scope ---------------
    # A surface the contract does not grade, that stage 3 lets an adversary attack,
    # is a submission losing ten points for not implementing what it was told not
    # to implement.  Matched by anchor token, with the vacuity guard the anchors
    # exist for: a surface yielding no anchor is a surface unchecked, and it says so.
    haystack = "\n".join(probe.scope.deny).lower()
    for surface in surfaces:
        text = surface["surface"]
        tokens = anchors(text)
        if not tokens:
            problems.append(
                f"the contract's not-graded surface {text!r} yields no anchor "
                f"token, so this check cannot tell whether probe.toml's scope "
                f"covers it; give the surface a quoted phrase or an identifier")
            continue
        if not any(t.lower() in haystack for t in tokens):
            problems.append(
                f"the contract does not grade {text!r}, but no [scope] deny entry "
                f"in probe.toml mentions any of {tokens[:4]}; an adversary may "
                f"attack it and the submission loses "
                f"{scoring.points_per_survived_model} points for obeying the "
                f"contract")

    # --- the protocol, on both sides of it ------------------------------------
    # srbyaml is the adversary's only way to reach the binary, in a context that
    # cannot read the contract that defines the wire.  An op in one and not the
    # other is either a ProtocolError that reads as a finding about the submission,
    # or a graded operation no adversary can send.
    ops = literal(VERIFICATION / "lib" / "srbyaml.py", "OPERATIONS")
    wire = tuple(con["probe_protocol"]["operations"])
    handshake = con["probe_protocol"]["handshake_op"]
    if set(ops) != set(wire) | {handshake}:
        problems.append(
            f"srbyaml.OPERATIONS is {sorted(ops)}; the contract's protocol is "
            f"{sorted(wire)} plus the handshake {handshake!r}.  "
            f"Only in srbyaml: {sorted(set(ops) - set(wire) - {handshake})}; "
            f"only in the contract: {sorted((set(wire) | {handshake}) - set(ops))}")

    # The third side of the same wire: instruction.md is the author's only spec for
    # it, and an operation that is graded without a heading here is a case nobody
    # could have answered.  The reverse -- a heading for an op the contract does not
    # list -- is work asked for and never scored.
    instruction_text = INSTRUCTION.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\*\*`([a-z_]+)`\*\*", instruction_text,
                                re.MULTILINE))
    if documented != set(wire) | {handshake}:
        problems.append(
            f"instruction.md documents the operations {sorted(documented)}; the "
            f"contract grades {sorted(wire)} plus the handshake {handshake!r}.\n"
            f"  graded with no heading in instruction.md: "
            f"{sorted((set(wire) | {handshake}) - documented)}\n"
            f"  documented and not graded: {sorted(documented - set(wire) - {handshake})}")
    compare(number(flatten(instruction_text), r"(\w+) operations are graded",
                   "graded operation count", problems), len(wire),
            "instruction.md graded operations", "the contract", problems)

    # --- stage 2's manifest, read by two files in one context but two layers ---
    modules = [m["id"] for m in literal(LIB / "catalog.py", "MODULES")]
    declared = [m.id for m in suite.modules]
    if modules != declared:
        problems.append(
            f"suite.toml lists {len(declared)} modules and catalog.py holds "
            f"{len(modules)}, in different order or with different names.\n"
            f"  only in suite.toml: {[m for m in declared if m not in modules]}\n"
            f"  only in catalog.py: {[m for m in modules if m not in declared]}\n"
            f"  first disagreement at index "
            f"{next((i for i, (a, b) in enumerate(zip(declared, modules)) if a != b), len(modules))}")
    assets_env = suite.env.get("SWEREFACTOR_ASSETS")
    dockerfile = (HERE / "behavioural" / "Dockerfile").read_text(encoding="utf-8")
    baked = re.search(r"SWEREFACTOR_ASSETS=(\S+)", dockerfile)
    if assets_env and baked and baked.group(1) != assets_env:
        problems.append(
            f"suite.toml sets SWEREFACTOR_ASSETS={assets_env!r}, the behavioural "
            f"Dockerfile bakes {baked.group(1)!r}; the suite value wins at run "
            f"time and the digest-sealed tree is at the Dockerfile's path")

    # --- the two counters of Zig logic lines ---------------------------------
    # srbscan.py:181 says this file runs both implementations over the same awkward
    # inputs and fails if they disagree.  This is that.  Compared on behaviour, not
    # by diffing the source: the copies are allowed to be written differently, they
    # are not allowed to count differently.
    scan_count = load_function(SCAN_LIB / "srbscan.py", "zig_logic_lines")
    vlib_count = load_function(LIB / "vlib.py", "zig_logic_lines")
    for label, sample in LINE_COUNT_SAMPLES:
        a, b = scan_count(sample), vlib_count(sample)
        if a != b:
            problems.append(
                f"zig_logic_lines disagrees on {label}: srbscan says {a}, vlib "
                f"says {b}.  Stage 1 states the 4,000-line floor to the reviewer "
                f"and stage 2 counted the Go the floor was derived from; two "
                f"counters means the floor and the figure justifying it are in "
                f"different units")

    # --- pins shared by two Dockerfiles --------------------------------------
    # The verification Dockerfile's own comment promises these match stage 2's.
    # They are five values in two files that no build sees together.
    behavioural_pins = docker_pins(HERE / "behavioural" / "Dockerfile")
    verification_pins = docker_pins(VERIFICATION / "Dockerfile")
    for key, value in behavioural_pins.items():
        other = verification_pins.get(key)
        if other is None:
            problems.append(f"the behavioural Dockerfile pins {key}={value} and the "
                            f"verification one pins no {key}; stage 3 copies stage "
                            f"2's binary and has to be the same platform")
        elif other != value:
            problems.append(f"{key}: behavioural pins {value}, verification pins "
                            f"{other}")
    if con["state_b"]["zig_version"] != behavioural_pins.get("ZIG_VERSION"):
        problems.append(f"the contract requires Zig "
                        f"{con['state_b']['zig_version']}, the behavioural "
                        f"Dockerfile installs {behavioural_pins.get('ZIG_VERSION')}")

    # --- the Zig cache directories, set in four files -------------------------
    # Every image that runs `zig build` sets these and pre-creates the directories
    # 0777; task.toml sets them again for the agent and verifier containers, where
    # a value overrides the image's ENV.  So a disagreement means building into a
    # directory nothing prepared.  It is not a hard failure at run time -- /tmp is
    # 1777 and Zig creates its own cache -- which is why this drifted unnoticed
    # until it was compared: task.toml said /tmp/zig-global-cache where all three
    # images said /tmp/zig-cache-global.
    cache_env: dict[str, dict[str, str]] = {}
    for label in ("environment/Dockerfile", "tests/behavioural/Dockerfile",
                  "tests/verification/Dockerfile", "task.toml"):
        found = dict(re.findall(
            # The quote classes matter: environment/Dockerfile writes each path
            # twice, once as an ENV and once inside a single-quoted `export` line
            # for /etc/profile.d, and a class that admits `'` captures the closing
            # quote -- which then reads as a fourth spelling of the same path.
            r"""ZIG_(GLOBAL|LOCAL)_CACHE_DIR\s*[= ]\s*["']?(/[^\s"'\\]+)""",
            (TASK / label).read_text(encoding="utf-8")))
        if found:
            cache_env[label] = found
    if "task.toml" not in cache_env:
        problems.append(
            "task.toml sets no ZIG_*_CACHE_DIR; the agent then inherits the image's, "
            "which is fine, but this check no longer compares anything -- either "
            "restore them or delete this block")
    for which in ("GLOBAL", "LOCAL"):
        spellings = {label: env[which] for label, env in cache_env.items()
                     if which in env}
        if len(set(spellings.values())) > 1:
            problems.append(
                f"ZIG_{which}_CACHE_DIR is spelled more than one way: "
                + "; ".join(f"{label} says {value}"
                            for label, value in sorted(spellings.items()))
                + ". The images pre-create their spelling 0777 and task.toml's "
                  "overrides it at run time, so these have to be one path.")

    # Each image that names a cache directory has to create it: the point of the
    # ENV is that the directory exists and is writable before `zig build` runs.
    for label, env in cache_env.items():
        if label == "task.toml":
            continue
        text = (TASK / label).read_text(encoding="utf-8")
        for which, directory in sorted(env.items()):
            if not re.search(rf"install -d[^\n]*{re.escape(directory)}", text):
                problems.append(
                    f"{label} sets ZIG_{which}_CACHE_DIR={directory} and never "
                    f"creates it; `install -d -m 0777 {directory}` is what makes "
                    f"it writable for a non-root build")


def load_function(path: Path, name: str):
    """Compile one top-level function out of a module, without importing it.

    Neither module is importable here: `srbscan` imports pytest and reads
    /opt/workspace at import time, `vlib` is stage 2's and imports siblings that
    exist only in the image.  The two functions wanted are pure text functions, so
    the function definition is lifted out and compiled on its own.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            module = ast.Module(body=[node], type_ignores=[])
            namespace: dict = {"re": re}
            exec(compile(module, str(path), "exec"), namespace)   # noqa: S102
            return namespace[name]
    raise SystemExit(f"{path.name}: no top-level def {name}")


#: Inputs where a line counter can plausibly differ from another line counter.
#: Chosen for the disagreements that matter -- doc comments, a `//` that is not a
#: comment, CRLF, an unterminated last line -- rather than for coverage.
LINE_COUNT_SAMPLES = (
    ("empty", ""),
    ("blank lines only", "\n\n   \n\t\n"),
    ("no trailing newline", "const a = 1;"),
    ("CRLF", "const a = 1;\r\n// comment\r\nconst b = 2;\r\n"),
    ("doc and module comments", "//! module\n/// doc\n// plain\nconst a = 1;\n"),
    ("indented comment", "    // indented\n    const a = 1;\n"),
    ("// inside a string", 'const s = "http://x";\n'),
    ("trailing comment on code", "const a = 1; // why\n"),
    ("tabs and form feed", "\tconst a = 1;\n\x0c\nconst b = 2;\n"),
    ("only comments", "// a\n// b\n//! c\n"),
)

#: The values two Dockerfiles must agree on, and how each is spelled.  Stage 3
#: copies stage 2's reference binary, so a different base image or a different Zig
#: is a different oracle.
PIN_PATTERNS = {
    "base image": r"FROM debian@(sha256:[0-9a-f]{64})",
    "DEBIAN_SNAPSHOT": r"ARG DEBIAN_SNAPSHOT=(\S+)",
    "ZIG_VERSION": r"ARG ZIG_VERSION=(\S+)",
    "ZIG_SHA256": r"ARG ZIG_SHA256=([0-9a-f]{64})",
    "python3": r"python3=(\S+?)\s*\\?$",
}


def docker_pins(path: Path) -> dict[str, str]:
    """Each shared pin found in one Dockerfile, and a problem if it repeats itself.

    A file that writes the same pin twice with two values is the failure this is
    shaped around: `first_match` would pick one, and the build would use the other.
    """
    text = path.read_text(encoding="utf-8")
    out: dict[str, str] = {}
    for key, pattern in PIN_PATTERNS.items():
        found = {m.group(1) for m in re.finditer(pattern, text, re.MULTILINE)}
        if len(found) == 1:
            out[key] = found.pop()
        elif len(found) > 1:
            out[key] = f"<{len(found)} different values in one file: {sorted(found)}>"
    return out


#: One line per `FORBIDDEN_CODE` rule, spelling the thing it exists to catch.
#: Kept as one blob rather than paired with the patterns, because pairing them
#: means writing the pattern twice and a rule can then match only its own twin.
SCREEN_PROBES = "\n".join((
    "x = SRB_TARGET; y = os.environ['SRB_ORIGINAL']",
    "__import__('os'); importlib.import_module('os')",
    "eval('1'); exec('pass'); compile('1', '<s>', 'eval')",
    "__builtins__; builtins.open; import builtins",
    "__loader__; __spec__; __file__",
    "globals()['probe']; locals()",
    "open('/etc/passwd'); io.open('x')",
    "sys.modules['srbyaml']; sys.path.append('.')",
    "sys.executable; sys.prefix; sys.base_prefix; sys.argv",
    "sys._getframe(1)",
    "srbyaml.os.getcwd(); srbyaml.sys.argv; srbyaml.subprocess.run",
    "srbyaml.select.select; srbyaml.json.dumps; srbyaml.shutil.which",
    "os.getcwd(); subprocess.run(['ls']); socket.gethostname(); pathlib.Path('.')",
    "shutil.which('zig'); glob.glob('*'); tempfile.mkdtemp()",
    "inspect.stack(); ctypes.CDLL('x'); resource.getrlimit(0)",
    "setattr(srbyaml, 'x', 1); getattr(srbyaml, 'os'); delattr(x, 'y')",
    "vars(srbyaml); dir(srbyaml); type(x).__mro__; x.__class__.__bases__",
    "x.__dict__; x.__globals__; x.__code__; x.__subclasses__()",
    "breakpoint(); input(); memoryview(b''); id(x)",
    # pytest's own path- and process-shaped fixtures.  Requested by name in a
    # signature, so they never appear as an import and the import allow list does
    # not see them: `def test_x(tmp_path, pytestconfig, monkeypatch)`.
    "def test_x(tmp_path, tmpdir, tmp_path_factory, pytestconfig, monkeypatch):",
    "def test_y(request): request.config.rootdir; request.config.rootpath",
    "request.config.invocation_dir; request.config.invocation_params",
    "request.config.startpath; request.session.startpath",
    # Repointing the probe at a binary the harness did not build.
    "p = srbyaml.Probe(); p.binary = '/opt/original/zig-out/bin/yaml-probe'",
))

#: The heading instruction.md states the ladder under.  Matched as a heading rather
#: than searched for across the file, because a number that appears somewhere in a
#: 400-line document is not the same as the number the author is scored by.
SCORE_SECTION = "## How this is scored"

#: Sentences that are wrong the moment they appear, whatever surrounds them.
#: `0.8 * behavioural + 0.2 * structural` was the ladder before this task had three
#: stages, and it is the specific formula an author would compute their own score
#: from and get a different answer than the harness.
FAIL_ON_SIGHT = (
    (r"0\.8\s*\*\s*behavioural", "the two-term score formula predates the three-stage "
                                "ladder; an author computing this gets a number the "
                                "harness never produces"),
    # Narrowed from a bare /\bstructural\b/, which fired on "Comments are
    # structural" -- a sentence about YAML, in a document about YAML.  A rule broad
    # enough to catch the word wherever it appears reports the prose as broken for
    # using an ordinary English adjective, and the fix for that is to weaken the
    # rule rather than the sentence.  What is actually forbidden is *structural as a
    # score component*.
    (r"structural\s*\(0?\.\d+\)|\bstructural score\b|\*\s*structural\b"
     r"|\bstructural\s*[:=]\s*0?\.\d", "there is no structural score component; "
                                       "stage 1 is a pass/fail gate and stage 3 is "
                                       "verification"),
    (r"partial credit for the gates?\b", "the gates are pass/fail: a failure scores "
                                         "zero and stops the run"),
)


def instruction_section(text: str, heading: str) -> str | None:
    """One section of a Markdown document: the heading to the next of its level or above.

    `heading` is written with its hashes, and the level is taken from them.  Stopping
    at the same level *or above* is the part worth getting right: a `###` section that
    ran to the next `###` would swallow the rest of the document whenever it happened
    to be the last subsection under its `##`.
    """
    hashes = re.match(r"#+", heading)
    if hashes is None:
        raise SystemExit(f"instruction_section: {heading!r} has no leading hashes")
    stop = "|".join(f"^{'#' * n} " for n in range(1, len(hashes.group(0)) + 1))
    match = re.search(rf"^{re.escape(heading)}\s*$(.*?)(?={stop}|\Z)",
                      text, re.MULTILINE | re.DOTALL)
    return match.group(1) if match else None


def score_section(text: str) -> str | None:
    """The scoring section of instruction.md, heading to next heading of its level."""
    return instruction_section(text, SCORE_SECTION)


def check_figures(problems: list[str]) -> None:
    """instruction.md's numbers, against the files that produce them.

    instruction.md is the task's final word: the author reads it, and it is what a
    disagreement between the document and the harness is resolved against.  So every
    figure in it is a claim about a loader, and every one of them is checked here
    rather than trusted.

    The case counts are the ones that move.  They are produced at image build time
    by freeze.py -- 8,170 frozen answers, 2,000 fresh, 72 document, 13 protocol,
    10,255 graded in total -- and they change whenever a family is added or a
    generator's seed range moves.
    """
    config = load_harness()
    evaluation = config.Evaluation.load(EVALUATION)
    scoring = evaluation.scoring
    text = INSTRUCTION.read_text(encoding="utf-8")
    flat = flatten(text)

    # The counts first, and unconditionally.  An early return for a missing scoring
    # section would leave a document that has lost its heading with its counts
    # unchecked -- one missing heading suppressing eight checks about five other
    # files, while the group still prints the three findings that make it look like
    # it has done its work.
    check_counts(problems)

    for pattern, why in FAIL_ON_SIGHT:
        if re.search(pattern, flat, re.IGNORECASE):
            problems.append(f"instruction.md matches /{pattern}/ "
                            f"(line {where(text, pattern.replace(chr(92) + 'b', ''))}"
                            f"): {why}")

    section = score_section(text)
    if section is None:
        problems.append(
            f"instruction.md has no {SCORE_SECTION!r} section, so the author is not "
            f"told the ladder they are scored by; either add it or update this "
            f"check with the heading that replaced it")
        return
    section = flatten(section)

    # Anchored on the table row and the sentences the section actually uses.  Each
    # pattern is deliberately specific: a loose one matches an unrelated number
    # further down and reports the wrong figure as wrong, which is worse than not
    # checking, and /(\w+) models/ did exactly that -- it matched "the models".
    compare(number(section, r"2 behavioural \| \*\*([\d.]+) points, or none\*\*",
                   "behavioural points", problems),
            int(scoring.behavioural_points), "instruction.md behavioural points",
            "evaluation.toml", problems)
    # The entry condition is a sentence, not a figure -- there is no number between
    # 0 and behavioural_points for one to name -- so what is checked is that the
    # sentence is present.  An instruction that leaves it out lets an agent read
    # stage 3 as reachable from a near miss, which is the drift that costs it work.
    if not re.search(r"runs only on a stage 2 at full marks", section, re.I):
        problems.append("instruction.md does not state the stage 3 entry condition "
                        "-- expected text matching /runs only on a stage 2 at full "
                        "marks/")
    compare(number(section, r"(\w+) models each get", "verification model count",
                   problems), scoring.verification_models,
            "instruction.md model count", "evaluation.toml", problems)
    compare(number(section, r"worth ([\d.]+) points? to you", "per-model points",
                   problems), int(scoring.points_per_survived_model),
            "instruction.md per-model points", "evaluation.toml", problems)
    compare(number(section, r"3 verification \| \*\*([\d.]+) points?\*\*",
                   "verification points", problems),
            int(scoring.verification_points), "instruction.md verification points",
            "evaluation.toml", problems)

    # How many gate questions there are, and how many can fail the run.  These two
    # figures are now the same number: the two advisory gates are deleted, every
    # gate declared here is required, and the prose says so as "all of them
    # required" rather than repeating the count.
    #
    # They are still compared separately, because the sentence is only *currently*
    # redundant.  "all" resolves to the declared count and is then compared against
    # the required count like any other figure, so a gate made advisory again while
    # this sentence still says "all" fails here -- which is the whole reason the
    # second comparison is worth keeping now that it cannot disagree by wording
    # alone.  Both come from the gate table rather than from scan.toml's prose about
    # it, because the table is what grade_audit reads.
    gates = evaluation.stages["audit"].gates
    compare(number(section, r"answer (\w+) written questions", "gate count",
                   problems), len(gates), "instruction.md gate count",
            "evaluation.toml", problems)
    written = re.search(r"(\w+) of them required", section, re.IGNORECASE)
    if written is not None and written.group(1).lower() == "all":
        stated_required = len(gates)
    else:
        stated_required = number(section, r"(\w+) of them required",
                                 "required gate count", problems)
    compare(stated_required, sum(1 for gate in gates if gate.required),
            "instruction.md required gate count", "evaluation.toml", problems)


#: Every host-side file that writes a case count in prose, and the pattern that
#: finds it.  Derived from a sweep for the figures themselves, not from memory:
#: each entry is a place a reader is told a number the file cannot compute.
#:
#: The point of listing files rather than grepping the tree for digits is that a
#: file appearing here is a file whose figure is checked, and a file that starts
#: stating a count without being added here is exactly the drift this misses.  So
#: `check_counts` also sweeps for the count spellings anywhere else and reports
#: what it finds -- the list decides what is *checked*, the sweep decides what is
#: *unchecked*, and the second one is the honest half.
COUNT_CLAIMS = (
    ("instruction.md", INSTRUCTION, "frozen_cases",
     r"\*\*([\d,]+)\*\* frozen cases in \w+ families"),
    ("instruction.md", INSTRUCTION, "graded_total",
     r"([\d,]+) cases in total"),
    ("instruction.md", INSTRUCTION, "document_cases", r"digest\. ([\d,]+) cases"),
    ("instruction.md", INSTRUCTION, "protocol_cases", r"([\d,]+) protocol cases"),
    ("instruction.md", INSTRUCTION, "fresh_cases",
     r"([\d,]+) cases generated at grading time"),
    # Two digits, so the sweep below will not see it -- 41 is a number a file can
    # hold for any reason.  It is claimed here because the sentence promises a
    # reader that the graded error strings are a closed set of that size, and the
    # figure only holds while the frozen suite does.
    ("instruction.md", INSTRUCTION, "error_texts",
     r"frozen cases reach ([\d,]+) distinct problem texts"),
    # The weighting sentence.  Both extremes are claimed because the pair is the
    # point: either figure alone reads as one family's size rather than as the span
    # the weighting exists to flatten.  The smaller extreme is two digits, so the
    # sweep below cannot see it and this entry is the only thing holding it -- which
    # is why it is claimed even though its file is the one that defines it.  It was
    # stated as a different family's size for four sessions until freeze.py's
    # build-time assertion, added at the same time as this entry, disagreed.
    # freeze.py quotes the same sentence in a comment and catalog.py explains the
    # larger figure on its own; both are claimed against the contract too, so a
    # re-freeze that moves an extreme cannot leave three files behind.
    ("instruction.md", INSTRUCTION, "smallest_family_cases",
     r"a family with ([\d,]+) cases and one with"),
    ("instruction.md", INSTRUCTION, "largest_family_cases",
     r"cases and one with ([\d,]+) carry comparable weight"),
    ("tests/behavioural/lib/freeze.py", None, "largest_family_cases",
     r"cases and one with ([\d,]+) carry comparable weight"),
    ("tests/behavioural/lib/catalog.py", None, "largest_family_cases",
     r"`compose` has ([\d,]+) cases"),
    ("tests/evaluation.toml", EVALUATION, "graded_total",
     r"froze ([\d,]+) answers"),
    ("tests/behavioural/lib/catalog.py", None, "frozen_cases",
     r"submission that embedded the ([\d,]+)"),
    ("tests/behavioural/lib/freeze.py", None, "graded_total",
     r"2,000 of the ([\d,]+) cases"),
    ("tests/behavioural/lib/probe.py", None, "frozen_cases",
     r"answers all ([\d,]+) requests"),
)


def check_counts(problems: list[str]) -> None:
    """The case counts, in every host-side file that states one.

    Five files tell a reader a number that only the image can compute.  Each is
    checked against the contract's declaration, which freeze.py checks against the
    frozen suite -- so the chain is: frozen cases -> contract -> prose, with an
    assertion at each arrow and no arrow left to a human.
    """
    counts = case_counts()

    # `fresh_cases` has a definition rather than only a prose mention: freeze.py's
    # FRESH_COUNT is the number the generator is asked for, and the contract
    # declares what was frozen.  Checked against the constant, which is stronger
    # than finding the digits in a sentence.
    fresh_count = literal(LIB / "freeze.py", "FRESH_COUNT")
    if "fresh_cases" in counts and fresh_count != counts["fresh_cases"]:
        problems.append(
            f"freeze.py FRESH_COUNT = {fresh_count}, the contract declares "
            f"fresh_cases = {counts['fresh_cases']}; the generator is asked for one "
            f"number and the task publishes another")

    # The sweep covers counts a file cannot plausibly contain for another reason.
    # A round number can: 2,000 is FRESH_COUNT, a budget, a byte ceiling, and it
    # appeared in eight files that were all legitimate -- reporting those as
    # unchecked trains the reader to skip this section, which costs the two counts
    # that are worth watching.  8,170 and 10,255 are not numbers a file holds by
    # coincidence.
    distinctive = {key: value for key, value in counts.items()
                   if isinstance(value, int) and value >= 1000 and value % 100}
    spellings = {f"{value:,}": key for key, value in distinctive.items()}
    spellings.update({str(value): key for key, value in distinctive.items()})

    checked: dict[Path, set[str]] = {}
    for label, path, key, pattern in COUNT_CLAIMS:
        path = path or (TASK / label)
        if not path.exists():
            problems.append(f"{label} is named as stating the {key} count and does "
                            f"not exist; either it moved or COUNT_CLAIMS is stale")
            continue
        text = path.read_text(encoding="utf-8")
        found = re.search(pattern, flatten(text))
        if found is None:
            problems.append(
                f"{label} no longer states its {key} count as /{pattern}/.  Either "
                f"the sentence was reworded -- update the pattern -- or the figure "
                f"was dropped, in which case remove the entry.  A pattern that "
                f"matches nothing checks nothing.")
            continue
        stated = int(found.group(1).replace(",", ""))
        if stated != counts.get(key):
            problems.append(
                f"{label} says {found.group(1)} where case-counts.json declares "
                f"{key} = {counts.get(key)}")
        checked.setdefault(path, set()).add(found.group(1))

    # The other half: any count spelling in a host-side file that no entry above
    # claims.  Reported, not failed -- a legitimate second mention is possible and
    # the useful output is "here is a number nothing checks".
    unchecked = []
    for path in sorted(TASK.rglob("*")):
        if path.is_dir() or "agent_run" in path.parts:
            continue
        if path.suffix not in (".md", ".toml", ".py", ".json", ".txt"):
            continue
        # case-counts.json is the declaration every claim above is checked
        # against, so its own figures are not a second mention of anything.
        if path.name in ("source-contract.json", "case-counts.json"):
            continue
        if path == Path(__file__):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for spelling, key in spellings.items():
            if spelling in text and spelling not in checked.get(path, set()):
                unchecked.append(f"{path.relative_to(TASK)} contains {spelling} "
                                 f"({key}), which no COUNT_CLAIMS entry checks")
    if unchecked:
        problems.append("case counts stated where nothing checks them:\n  "
                        + "\n  ".join(sorted(set(unchecked))))


def case_counts() -> dict[str, int]:
    """The declared case counts, and a failure rather than a skip if they are gone.

    The counts exist for real only inside the verifier image, where freeze.py
    computes them.  `case_counts` in tests/behavioural/data/case-counts.json is the
    declaration, and freeze.py asserts it against the frozen suite on every image
    build, so by the time this reads it the number has been checked against the
    thing it describes.

    A missing block raises.  Returning an empty dict would make every count check
    below pass by having nothing to compare, and a document stating a five-year-old
    total would ship green -- which is the failure this file exists to catch, in the
    file that exists to catch it.
    """
    if not CASE_COUNTS.is_file():
        raise SystemExit(
            f"no {CASE_COUNTS.relative_to(TASK)}, so the counts in instruction.md "
            f"are unchecked; freeze.py writes the real figures to the manifest and "
            f"asserts that file against them -- restore it from a build log")
    declared = json.loads(
        CASE_COUNTS.read_text(encoding="utf-8")).get("case_counts")
    if not declared:
        raise SystemExit(
            f"{CASE_COUNTS.relative_to(TASK)} has no case_counts block, so the "
            f"counts in instruction.md are unchecked; freeze.py writes the real "
            f"figures to the manifest and asserts this block against them -- "
            f"restore it from a build log")
    counts = {k: v for k, v in declared.items() if not k.startswith("_")}
    # The catalog's floors are separate, and the declaration has to clear them:
    # a declared count below a floor means one of the two is stale.
    for key, floor_name in (("frozen_cases", "MIN_FROZEN_CASES"),
                            ("fresh_cases", "MIN_FRESH_CASES"),
                            ("document_cases", "MIN_DOCUMENT_CASES"),
                            ("protocol_cases", "MIN_PROTOCOL_CASES"),
                            ("error_texts", "MIN_ERROR_TEXTS")):
        floor = literal(LIB / "catalog.py", floor_name)
        if key in counts and counts[key] < floor:
            raise SystemExit(
                f"case-counts.json declares {key} = {counts[key]}, below "
                f"catalog.py's {floor_name} = {floor}; the image build would fail "
                f"on this, so one of the two was edited without the other")
    families = literal(LIB / "catalog.py", "FAMILY_BUDGETS")
    if counts.get("frozen_families") not in (None, len(families)):
        raise SystemExit(
            f"case-counts.json declares {counts['frozen_families']} frozen "
            f"families, catalog.py weights {len(families)}")
    parts = ("frozen_cases", "fresh_cases", "document_cases", "protocol_cases")
    if all(p in counts for p in parts) and "graded_total" in counts:
        total = sum(counts[p] for p in parts)
        if total != counts["graded_total"]:
            raise SystemExit(
                f"case-counts.json's declared parts sum to {total} and it "
                f"declares graded_total = {counts['graded_total']}")
    return counts


#: Artefacts a host-side run drops into a tree that ships.
STRAY = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".DS_Store")


#: Each stage Dockerfile's last RUN sweeps bytecode and then asserts there is
#: none.  A `find` invocation takes its roots before its first `-name`, so both
#: halves of the sweep are parseable and comparable.
SWEEP_ROOTS = re.compile(r"find\s+(?P<roots>(?:/\S+\s+)+)-name\s+(?P<what>\S+)")

#: A RUN that could write bytecode after the sweep has run.  `python3 -m`, a bare
#: `python3 x.py`, pip, and pytest all import, and all write .pyc unless
#: PYTHONDONTWRITEBYTECODE is in force -- which each stage sets, making this the
#: second line of defence rather than the first.
IMPORTS_PYTHON = re.compile(r"\b(python3?|pip3?|pytest)\b")


def check_image_bytecode(problems: list[str]) -> None:
    """Each stage's bytecode sweep, against its own assertion.

    All three stage Dockerfiles end with `find <roots> -name __pycache__ ... &&
    test -z "$(find <roots> -name '*.pyc')"`, and in all three the assertion named
    fewer roots than the sweep.  That passes on whatever the sweep happens to
    reach, which is not the same as passing because the image is clean: stage 2
    swept and asserted `/tests/behavioural` alone and shipped twenty .pyc files
    under `/opt/swerefactor`, written when an earlier step imported the harness
    through PYTHONPATH.  Found by running `find / -name '*.pyc'` in the built
    image; none of these lines would ever have said so.

    So the check is an equality between the two root lists, plus the ordering
    claim each sweep's comment makes -- that nothing after it imports anything.
    An assertion placed before the last thing that can violate it reports on a
    state that no longer exists by the time the image is tagged.
    """
    for stage in ("audit", "behavioural", "verification"):
        path = HERE / stage / "Dockerfile"
        if not path.exists():
            problems.append(f"tests/{stage}/Dockerfile does not exist")
            continue
        text = path.read_text(encoding="utf-8")
        found = {m.group("what").strip("'\""): (m.start(),
                                               tuple(m.group("roots").split()))
                 for m in SWEEP_ROOTS.finditer(flatten(text))}
        sweep = found.get("__pycache__")
        assertion = found.get("*.pyc")
        if sweep is None or assertion is None:
            problems.append(
                f"tests/{stage}/Dockerfile has no bytecode sweep -- expected a "
                f"final RUN with `find <roots> -name __pycache__` and a `find "
                f"<roots> -name '*.pyc'` beside it.  Without one the image ships "
                f"whatever the build's imports wrote.")
            continue
        if set(sweep[1]) != set(assertion[1]):
            problems.append(
                f"tests/{stage}/Dockerfile sweeps {' '.join(sweep[1])} and asserts "
                f"over {' '.join(assertion[1])}; the difference is swept and never "
                f"checked, so a sweep that silently stopped reaching it would pass")

        # Nothing after the sweep may import.  Measured on the raw text, since a
        # line offset in flattened prose does not map back to a RUN boundary.
        #
        # No [1:] here.  The slice starts at `-name __pycache__`, which is past the
        # sweep's own `RUN` keyword, and its continuation lines begin with `&&`, so
        # the first ^RUN in `tail` is already the next instruction.  Dropping one
        # made the check blind to exactly the step most likely to be appended --
        # the first one after the sweep -- and it passed a mutation that added
        # `RUN python3 -c ...` at the end of the file.
        tail = text[text.index("-name __pycache__"):]
        for block in re.findall(r"^RUN\s(?:.*\\\n)*.*$", tail, re.MULTILINE):
            if IMPORTS_PYTHON.search(block):
                problems.append(
                    f"tests/{stage}/Dockerfile runs Python after its bytecode "
                    f"sweep:\n    {flatten(block)[:120]}\n  Move the step above the "
                    f"sweep, or the sweep below it -- an assertion before the last "
                    f"thing that can break it describes a state the image no "
                    f"longer has when it is tagged.")


def check_hygiene(problems: list[str]) -> None:
    """Nothing in this tree that a host-side run wrote.

    Written after doing it: running one of these checks on the host left a
    `__pycache__` in `tests/verification/` and `tests/behavioural/lib/`.  The
    `.dockerignore` files caught it, so no image shipped it, but the tree that gets
    committed is not filtered by a `.dockerignore`, and the author's bytecode in a
    published task is somebody else's confusing diff.

    Detecting the artefact rather than enumerating what writes it, because the
    causes are open-ended -- an import, a pytest run, a python3 -c -- and the
    artefact is one thing.  `sys.dont_write_bytecode` is set before this file
    imports anything, which is the fix; this is the check that the fix held.
    """
    for path in sorted(TASK.rglob("*")):
        if "agent_run" in path.parts:
            continue          # not shipped, not this file's business
        if path.name in STRAY or path.suffix in (".pyc", ".pyo"):
            problems.append(
                f"{path.relative_to(TASK)} is an artefact of running something on "
                f"the host, not part of the task; remove it and keep "
                f"sys.dont_write_bytecode set in anything that imports the suites")


def check_drivers(problems: list[str]) -> None:
    """The two build drivers, against the contract and against each other.

    `graded_builds.drivers` is the one part of the contract that decides how a
    submission is built rather than describing it.  The resolved entry fixes the
    compiler, the argv, the artefact path, the permitted build outputs and which
    provenance cases are scored -- and every module downstream reads the resolved
    answer instead of the tree, so a misroute is not recoverable later in the run.

    The suite checks the resolution table at image build time, in `driver.py`'s
    self-check.  What that cannot reach is the rest of the block: `state_b`'s
    published build steps and the Zig driver's are the same commands written twice,
    `state_a.build_toolchain.commands` and the Go driver's are a third and fourth
    copy, and the two `source-contract.json` copies are a fifth pair.  An agent
    promised one argv and graded on another has no way to see the difference, and
    nothing else compares these files.

    Both directions of the fallback rule, because each fails differently: an entry
    with no `detect` that is not last makes every driver after it unreachable, and
    a last entry that *does* declare `detect` leaves a tree matching nothing to
    fall through to `drivers[-1]` anyway -- reached by a line that reads like a
    default but is the Zig driver in particular.
    """
    con = contract()
    graded = con.get("graded_builds") or {}
    drivers = graded.get("drivers") or []
    if not drivers:
        problems.append("source-contract.json: graded_builds.drivers is empty; "
                        "resolve_driver raises on this and no module can build")
        return

    ids = [d.get("id") for d in drivers]
    if len(set(ids)) != len(ids):
        problems.append(f"source-contract.json: duplicate driver id in {ids}; "
                        f"resolution returns the first and the second is dead")
    for entry in drivers[:-1]:
        if not (entry.get("detect") or {}):
            problems.append(
                f"driver {entry.get('id')!r} declares no detect but is not last; "
                f"every driver after it is unreachable")
    if drivers[-1].get("detect"):
        problems.append(
            f"the last driver {drivers[-1].get('id')!r} declares detect; a tree "
            f"matching no driver still falls through to it, so it is the fallback "
            f"whether or not it says so -- drop the detect or add a fallback after "
            f"it")

    # The predicates in the file, against the ones the shipped resolver knows.  An
    # unimplemented predicate is raised on at run time, which is right and late:
    # it would take a grading run to see it.
    resolve = load_function(LIB / "build.py", "resolve_driver")
    known = literal_in_function(LIB / "build.py", "resolve_driver", "known")
    for entry in drivers:
        unknown = sorted(set(entry.get("detect") or {}) - known)
        if unknown:
            problems.append(
                f"driver {entry.get('id')!r} declares detect predicate(s) "
                f"{unknown}; resolve_driver implements {sorted(known)} and raises "
                f"on anything else")

    # The resolution prose, against the predicates the drivers actually use.  It
    # named `detect.any_path_exists` alone while the Go driver had grown a second
    # predicate, so the sentence describing how detection works described a rule
    # that had stopped being the rule.
    used = sorted({p for d in drivers for p in (d.get("detect") or {})})
    resolution = flatten(graded.get("resolution") or "")
    for predicate in used:
        if predicate not in resolution:
            problems.append(
                f"graded_builds.resolution does not mention {predicate!r}, which "
                f"driver detection uses; the prose describes a narrower rule than "
                f"the one that runs")

    # The same commands, written twice.  `plain` drops mapping key order: the
    # shipped file is key-sorted and a direct comparison would report a difference
    # that is not one.
    def plain(value):
        if isinstance(value, dict):
            return {k: plain(v) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [plain(v) for v in value]
        return value

    by_id = {d.get("id"): d for d in drivers}
    zig = by_id.get("state-b-zig")
    state_b = con.get("state_b") or {}
    if zig is None:
        problems.append(f"source-contract.json: no state-b-zig driver, in {ids}; "
                        f"the published State B build has no entry that runs it")
    else:
        for key in ("build_steps", "binaries"):
            if plain(zig.get(key)) != plain(state_b.get(key)):
                problems.append(
                    f"the state-b-zig driver's {key} differ from state_b.{key}; "
                    f"the agent is promised one and graded on the other\n"
                    f"  driver:  {json.dumps(plain(zig.get(key)), sort_keys=True)}\n"
                    f"  state_b: {json.dumps(plain(state_b.get(key)), sort_keys=True)}")
        if zig.get("toolchain_version") != state_b.get("zig_version"):
            problems.append(
                f"the state-b-zig driver builds with Zig "
                f"{zig.get('toolchain_version')!r}, state_b.zig_version says "
                f"{state_b.get('zig_version')!r}")

    go = by_id.get("state-a-go")
    declared = [c[:2] for c in
                ((con.get("state_a") or {}).get("build_toolchain") or {})
                .get("commands", [])]
    if go is None:
        problems.append(f"source-contract.json: no state-a-go driver, in {ids}; "
                        f"State A cannot be built by this stage")
    else:
        # Head of the argv only.  The driver's install step adds `-o bin/yaml-probe`
        # so the artefact lands at a fixed path, which `state_a.build_toolchain`
        # deliberately does not state -- it describes how the repository builds, not
        # where this stage wants the output.
        invoked = [(s.get("invoked_as") or [])[:2] for s in go.get("build_steps") or []]
        if invoked != declared:
            problems.append(
                f"the state-a-go driver runs {invoked}, state_a.build_toolchain "
                f"declares {declared}")

    # Every driver's unscored list, against the provenance cases that exist.  A name
    # no case answers to unscores nothing while reading as though it had; a list
    # covering every case rates the module 0.0, because `result.rate` returns 0.0
    # for an all-skipped pool.  Both are raised on at run time, both are cheap here.
    cases = {c[1] for c in literal(LIB / "catalog.py", "PROVENANCE_CASES")}
    for entry in drivers:
        unscored = set(entry.get("provenance_checks_not_scored") or [])
        stray = sorted(unscored - cases)
        if stray:
            problems.append(
                f"driver {entry.get('id')!r} unscores {stray}, which are not "
                f"declared provenance checks: {sorted(cases)}")
        if unscored and not cases - unscored:
            problems.append(
                f"driver {entry.get('id')!r} unscores every provenance check; an "
                f"all-skipped pool rates 0.0, so this would zero the module it "
                f"means to protect")
        if unscored and not flatten(entry.get("provenance_note") or ""):
            problems.append(
                f"driver {entry.get('id')!r} unscores {sorted(unscored)} with no "
                f"provenance_note; the reason a check is unscored is what the "
                f"detail line quotes to whoever reads the result")

    # And the table the suite runs, run here too.  It is the same function and the
    # same rows, and the difference is when: this is the authoring-time run, before
    # a 25-minute image build, and it is where a contract edit gets caught.
    rows = literal(LIB / "build.py", "DRIVER_RESOLUTION_CASES")
    reached = set()
    for label, files, expected in rows:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for rel in files:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("x", encoding="utf-8")
            try:
                got = resolve(root, con).get("id")
            except Exception as exc:                             # noqa: BLE001
                problems.append(f"resolving {label!r} raised "
                                f"{type(exc).__name__}: {exc}")
                continue
        reached.add(got)
        if got != expected:
            problems.append(f"{label!r} resolves to {got!r}, expected {expected!r}")
    unreached = [i for i in ids if i not in reached]
    if unreached:
        problems.append(
            f"no row of DRIVER_RESOLUTION_CASES resolves to {unreached}; a table "
            f"that exercises one driver passes while the other is unreachable")


GROUPS = {
    "contract": check_contract,
    "drivers": check_drivers,
    "duplicates": check_duplicates,
    "vocabulary": check_vocabulary,
    "screen": check_screen,
    "cross-image": check_cross_image,
    "figures": check_figures,
    "hygiene": check_hygiene,
    "bytecode": check_image_bytecode,
}


def check_own_docstring() -> None:
    """The header's group list against GROUPS, in both directions.

    Derived from the mapping rather than typed beside it.  Both directions
    matter: a group missing from the list is undocumented, and a name in the
    list that GROUPS has dropped sends the reader to a `--only` value argparse
    rejects.
    """
    documented = set(re.findall(r"^    ([a-z-]+) {2,}\S", __doc__, re.MULTILINE))
    if documented != set(GROUPS):
        missing = sorted(set(GROUPS) - documented)
        extra = sorted(documented - set(GROUPS))
        raise SystemExit(
            "this file's header does not describe its own groups"
            + (f"\n  in GROUPS, not in the header: {', '.join(missing)}" if missing
               else "")
            + (f"\n  in the header, not in GROUPS: {', '.join(extra)}" if extra
               else ""))


def main(argv: list[str]) -> int:
    check_own_docstring()
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", choices=sorted(GROUPS),
                        metavar="GROUP", help=f"run one group; repeatable. "
                                              f"one of {', '.join(sorted(GROUPS))}")
    args = parser.parse_args(argv[1:])
    chosen = args.only or sorted(GROUPS)

    total = 0
    for name in chosen:
        problems: list[str] = []
        try:
            GROUPS[name](problems)
        except SystemExit as exc:
            problems.append(f"the group could not run: {exc}")
        except Exception as exc:                            # noqa: BLE001
            problems.append(f"the group raised {type(exc).__name__}: {exc}")
        if problems:
            print(f"\n{name}: {len(problems)} problem"
                  f"{'s' if len(problems) > 1 else ''}")
            for problem in problems:
                print("  - " + problem.replace("\n", "\n    "))
        else:
            print(f"{name}: ok")
        total += len(problems)

    if total:
        print(f"\n{total} problem{'s' if total > 1 else ''} across "
              f"{len(chosen)} group{'s' if len(chosen) > 1 else ''}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main(sys.argv))
