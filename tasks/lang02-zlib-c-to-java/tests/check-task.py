#!/usr/bin/env python3
"""Authoring-time checks that span files no single image can see.

Run from anywhere:

    python3 tasks/lang02-zlib-c-to-java/tests/check-task.py

Five groups of checks, each about a claim one file makes about another:

    gate-map     every guard stage 2 stopped measuring is answered by a gate
                 stage 1 actually declares
    vocabulary   the anti-cheat token lists agree with what the author was given
    figures      instruction.md's scoring numbers match the loaders
    requirements every requirement stage 2 enforces is stated where the agent
                 can read it
    collection   no artifact exclude glob deletes a path grading requires

Why any of this exists outside the images.  The task's instruments live in two
Docker build contexts -- stage 1's is `tests/audit/`, stage 2's is
`tests/behavioural/` -- and a Docker build cannot reach outside its context.  So no
image can read two of them at once, and `instruction.md` and `evaluation.toml` sit
outside both.  `swerefactor validate` does not close the gap either: it reads the
config files and the stage layout, never a task's own Python.

That leaves the cross-file claims unchecked by anything, and each group below is
here because one of them was false:

  * `driver.py` deferred two guards to a gate id `evaluation.toml` did not declare,
    so those guards were measured in neither stage while both stages reported a
    clean partition.
  * All three anti-cheat instruments said "instruction.md does not name these
    words" about a list containing `/opt/swerefactor` and `source-contract.json`,
    which instruction.md names as the normative API statement and tells the author
    to read.  A javadoc line citing the specification tripped an advisory scan, a
    scored case, and a required gate whose instruction was "Confirm the line and
    fail."
  * `evaluation.toml` said "Eleven gates, nine required" after a twelfth was added,
    and the section of instruction.md that tells the author how they are scored
    described a two-part scheme with "35 gates" when the ladder has three stages,
    twelve gates and the number 35 means something else entirely.
  * Three scored requirements were enforced and stated nowhere the solver could
    read them: `ctest -N` having to list `example`, the implicit public
    constructor `javac` supplies for a class the contract lists with none, and
    `ZLIB_BUILD_EXAMPLES` having to stay a declared `option()`.  Each was
    satisfiable, so each was a trap rather than a broken check -- points lost for
    a rule never given.  `environment/Dockerfile` asserts this cannot happen.

This is honest about being an authoring tool: no image build runs it and grading
does not run it.  What makes it worth having anyway is that every failure above is
silent -- the suites stay green, the counts stay self-consistent, and the thing
that is wrong is a sentence in a different directory.

`gate-map` and `figures` need the harness's config loader, found at $SRB_INFRA or
in `infra/` beside `tasks/`.  `vocabulary` needs no imports, so it still runs in a
tree that has only `tasks/`; pass `--only vocabulary` there.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import os
import re
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent

LIB = HERE / "behavioural" / "lib"
AUDIT = LIB / "audit.py"
SCAN = HERE / "audit" / "modules" / "contract" / "test_contract.py"
EVALUATION = HERE / "evaluation.toml"
INSTRUCTION = TASK / "instruction.md"
ENVIRONMENT = TASK / "environment"

#: Read out of the gate question rather than hardcoded: the sentence enumerates the
#: tokens a reviewer may fail on sight, and one check is about that enumeration.
#:
#: Matched against whitespace-flattened text, because the sentence is prose inside a
#: TOML block and the line breaks fall wherever the paragraph happens to wrap.  The
#: first version wanted a single space before "is addressing" and stopped matching
#: when a longer token list pushed those two words onto the next line -- reporting
#: the sentence as absent rather than as wrong, and firing the vacuity guard beside
#: the check that should have caught it.
FAIL_ON_SIGHT = re.compile(
    r"A source file that names (.+?) is addressing the harness")


def flatten(text: str) -> str:
    """Collapse whitespace runs, so a matcher cannot depend on where prose wraps."""
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
            f"that has one, or run --only vocabulary, which needs no imports.")
    return config


def _assignments(tree: ast.AST, anywhere: bool):
    """Every `name = value` in the tree, at module level or anywhere."""
    for node in (ast.walk(tree) if anywhere else tree.body):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if isinstance(target, ast.Name):
            yield target.id, node.value


def literal(path: Path, name: str, *, anywhere: bool = False):
    """One constant, read without importing the module that defines it.

    `driver.py` and `audit.py` import stage 2's lib on the way in, and that
    imports things that exist only inside the image.  The constants wanted here are
    literals, so parsing is both sufficient and the only thing that works.

    `anywhere` walks into class bodies, for `GUARD_CASES`.  Both assignment forms
    are handled because the annotated one has been missed here before.

    Module-level constants are substituted before evaluating, because
    `STRUCT_CASES` spells its config pair `BOTH` rather than the tuple.  Without
    that, reading the scored structure table fails as "malformed node", which is
    how it stayed unread here while `GUARD_CASES` beside it was checked.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    consts: dict[str, object] = {}
    for ident, value in _assignments(tree, False):
        try:
            consts[ident] = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            pass  # not a literal; nothing here needs it

    class Resolve(ast.NodeTransformer):
        def visit_Name(self, node):  # noqa: N802 - ast's own casing
            if node.id in consts:
                return ast.copy_location(ast.Constant(value=consts[node.id]), node)
            return node

    for ident, value in _assignments(tree, anywhere):
        if ident == name:
            resolved = Resolve().visit(ast.fix_missing_locations(value))
            return ast.literal_eval(resolved)
    raise SystemExit(f"{path.name}: no {name}")


# --------------------------------------------------------------------------- #
# gate-map
# --------------------------------------------------------------------------- #

def check_gate_map(config, say) -> list[str]:
    """Every guard stage 2 defers is answered by a gate stage 1 declares.

    `swerefactor validate` already checks the other direction for the prompt -- a
    declared gate the prompt never mentions -- so between the two, a gate id has to
    exist in all three places or something fails.
    """
    answers: dict[str, str] = literal(LIB / "driver.py", "STAGE1_ANSWERS")
    measured = set(literal(LIB / "driver.py", "PROVENANCE_GATES"))
    guards = list(literal(LIB / "catalog.py", "GUARD_CASES", anywhere=True))
    advisory = {check for _cid, check, mandatory, _note in guards
                if mandatory is False}
    declared_guards = {check for _cid, check, _m, _n in guards}

    evaluation = config.Evaluation.load(EVALUATION)
    gates = {g.id: g for g in evaluation.stage("audit").gates}
    problems: list[str] = []

    for gate in sorted({a for a in answers.values() if a not in gates}):
        deferred = sorted(k for k, v in answers.items() if v == gate)
        problems.append(
            f"STAGE1_ANSWERS defers {', '.join(deferred)} to gate {gate!r}, which "
            f"evaluation.toml does not declare -- "
            f"{'that guard is' if len(deferred) == 1 else 'those guards are'} "
            f"measured in neither stage")

    invented = sorted(set(answers) - declared_guards)
    if invented:
        problems.append(
            f"STAGE1_ANSWERS names guards the catalog does not declare: "
            f"{', '.join(invented)}")

    # An advisory observation deferred to a required gate becomes the opposite of
    # what the catalog says it is worth.
    for guard in sorted(advisory & set(answers)):
        gate = gates.get(answers[guard])
        if gate is not None and gate.required:
            problems.append(
                f"guard {guard!r} is advisory in the catalog but defers to gate "
                f"{gate.id!r}, which is required -- an observation the catalog says "
                f"should not void a submission would become the zero")

    say(f"{len(guards)} guards: {len(measured)} measured in stage 2, "
        f"{len(answers)} deferred to stage 1 "
        f"({len(set(answers) - measured)} of them there only)")
    say(f"{len(answers)} deferrals -> {len(set(answers.values()))} of "
        f"{len(gates)} declared gates")
    say(f"{len(advisory)} advisory guard(s) ({', '.join(sorted(advisory))}) -> "
        f"{', '.join(sorted({answers[g] for g in advisory if g in answers}))}")

    # Printed rather than checked, so it stays a deliberate fact: a gate nothing
    # defers to is a question stage 1 asks on its own account.  `drivers-are-ported`
    # is one; `default-path` is measured in stage 2 under its own name as well.
    unreferenced = sorted(set(gates) - set(answers.values()))
    if unreferenced:
        say(f"{len(unreferenced)} gate(s) answer no deferral: "
            f"{', '.join(unreferenced)}")
    return problems


# --------------------------------------------------------------------------- #
# vocabulary
# --------------------------------------------------------------------------- #

def author_documents() -> dict[str, str]:
    """Everything the author can read, as name -> text.

    State A comes out of the shipped tarball rather than a working copy, so this
    checks what is actually delivered.  Not `environment/Dockerfile`, which builds
    the image and is never inside it, and not `task.toml`, which is the harness's
    own configuration -- both legitimately name harness paths.
    """
    docs = {"instruction.md": INSTRUCTION.read_text(encoding="utf-8")}
    docs["environment/source-contract.json"] = (
        ENVIRONMENT / "source-contract.json").read_text(encoding="utf-8")
    with tarfile.open(ENVIRONMENT / "original.tar.gz", "r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is not None:
                docs[f"State A:{member.name}"] = handle.read().decode(
                    "utf-8", "replace")
    return docs


def where(docs: dict[str, str], needle: str, *, regex: bool) -> list[str]:
    """Which documents contain `needle`, with a line number for the first hit."""
    found = []
    for name, text in docs.items():
        if regex:
            match = re.search(needle, text, re.IGNORECASE)
            start = match.start() if match else -1
        else:
            start = text.find(needle)
        if start >= 0:
            found.append(f"{name}:{text.count(chr(10), 0, start) + 1}")
    return found


def check_vocabulary(say) -> list[str]:
    """The anti-cheat token lists against what the author was actually given."""
    strict_patterns = literal(AUDIT, "VERIFIER_AWARENESS_PATTERNS")
    exempt_patterns = literal(AUDIT, "CONTRACT_PATH_PATTERNS")
    strict_tokens = literal(SCAN, "VERIFIER_VOCABULARY")
    exempt_tokens = literal(SCAN, "CONTRACT_VOCABULARY")

    docs = author_documents()
    problems: list[str] = []

    # 1. Nothing in a strict list may appear in anything the author holds.
    for pattern in strict_patterns:
        hits = where(docs, pattern, regex=True)
        if hits:
            problems.append(
                f"audit.py VERIFIER_AWARENESS_PATTERNS has {pattern!r}, which "
                f"the author was given at {', '.join(hits[:3])} -- a submission "
                f"quoting what it was told to read would fail a scored case")
    for token in strict_tokens:
        hits = where(docs, token, regex=False)
        if hits:
            problems.append(
                f"test_contract.py VERIFIER_VOCABULARY has {token!r}, which the "
                f"author was given at {', '.join(hits[:3])} -- the reviewer would be "
                f"sent to find a leak the instruction handed over on purpose")

    # 2. An exemption for a token the author never saw weakens a check for nothing.
    for pattern in exempt_patterns:
        if not where(docs, pattern, regex=True):
            problems.append(
                f"audit.py CONTRACT_PATH_PATTERNS exempts {pattern!r} from the "
                f"comment rule, but no document the author holds names it -- there "
                f"is no citation to protect, so this belongs in "
                f"VERIFIER_AWARENESS_PATTERNS where a mention is the finding")
    for token in exempt_tokens:
        if not where(docs, token, regex=False):
            problems.append(
                f"test_contract.py CONTRACT_VOCABULARY exempts {token!r}, but no "
                f"document the author holds names it -- it belongs in "
                f"VERIFIER_VOCABULARY")

    # 3. A token in both lists: which one wins depends on the order two unrelated
    #    tuples happen to be written in.
    for name, strict, exempt in (("audit.py", strict_patterns, exempt_patterns),
                                 ("test_contract.py", strict_tokens, exempt_tokens)):
        both = sorted(set(strict) & set(exempt))
        if both:
            problems.append(f"{name} lists {', '.join(repr(b) for b in both)} as "
                            f"both strict and exempt")

    # 4. The gate's own enumeration, which is what a reviewer acts on.
    match = FAIL_ON_SIGHT.search(flatten(EVALUATION.read_text(encoding="utf-8")))
    if match is None:
        problems.append(
            "evaluation.toml has no 'A source file that names ... is addressing the "
            "harness' sentence -- if the gate no longer enumerates tokens to fail on "
            "sight, delete this check rather than leaving it passing vacuously")
    else:
        enumerated = re.findall(r"`([^`]+)`", match.group(1))
        if not enumerated:
            problems.append("the gate's fail-on-sight sentence enumerates no tokens")
        for token in enumerated:
            hits = where(docs, token, regex=False)
            if hits:
                problems.append(
                    f"the no-verifier-awareness gate tells the reviewer to fail on "
                    f"sight of {token!r}, which the author was given at "
                    f"{', '.join(hits[:3])} -- that is a required gate, so this is "
                    f"the whole task's zero for reading the specification")

    # 5. The scored instrument and the advisory one must agree about which mentions
    #    are expected.  Compared by what each matches, since one is regexes.
    for token in exempt_tokens:
        if not any(re.search(p, token, re.IGNORECASE) for p in exempt_patterns):
            problems.append(
                f"the scan exempts {token!r} but no pattern in audit.py's "
                f"CONTRACT_PATH_PATTERNS matches it -- the advisory instrument would "
                f"treat a citation as expected while the scored one fails it")

    say(f"{len(docs)} document(s) the author holds "
        f"({len(docs) - 2} of them State A files)")
    say(f"audit.py: {len(strict_patterns)} strict pattern(s), "
        f"{len(exempt_patterns)} exempt")
    say(f"test_contract.py: {len(strict_tokens)} strict token(s), "
        f"{len(exempt_tokens)} exempt")
    if match is not None:
        say(f"gate enumerates {len(re.findall(r'`([^`]+)`', match.group(1)))} "
            f"token(s) to fail on sight")
    return problems


# --------------------------------------------------------------------------- #
# figures
# --------------------------------------------------------------------------- #

#: Words instruction.md writes out, because prose does.  Only the values that
#: appear there: a bare digit lookup would silently pass on any number this does
#: not know, so an unknown word is a failure rather than a skip.
WORDS = {
    # "an hour" is the quantity, written the way prose writes it.
    "a": 1, "an": 1,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "seventeen": 17, "eighteen": 18,
}

#: The heading whose body is checked.  A rename should fail loudly rather than
#: leave every figure below unchecked.
SCORE_SECTION = "## What earns a score"


def score_section() -> str:
    """instruction.md's scoring section, flattened, up to the next heading."""
    text = INSTRUCTION.read_text(encoding="utf-8")
    start = text.find(SCORE_SECTION)
    if start < 0:
        raise SystemExit(
            f"instruction.md has no {SCORE_SECTION!r} heading -- every figure check "
            f"below reads that section, so a rename must fail here rather than "
            f"leave them all passing against an empty string")
    end = text.find("\n## ", start + len(SCORE_SECTION))
    return flatten(text[start:end if end > 0 else len(text)])


def number(section: str, pattern: str, label: str, problems: list[str]):
    """One figure out of the prose, as an int, or None with a problem recorded.

    `pattern` must have exactly one group, over a digit run or a written word.
    """
    match = re.search(pattern, section, re.IGNORECASE)
    if match is None:
        problems.append(
            f"instruction.md's scoring section states no {label} -- expected text "
            f"matching /{pattern}/")
        return None
    raw = match.group(1).replace(",", "").lower()
    if raw.isdigit():
        return int(raw)
    if raw in WORDS:
        return WORDS[raw]
    problems.append(f"instruction.md's {label} reads {raw!r}, which is neither a "
                    f"number nor a word this check knows")
    return None


def check_figures(config, say) -> list[str]:
    """instruction.md's scoring numbers against the files that implement them.

    The section is what the author is scored by, and nothing else reads it.  The
    environment Dockerfile machine-checks instruction.md's *compile-time* claims
    against the contract; the scoring figures had no checker at all, which is how a
    three-stage ladder came to be described as two parts worth "35 gates".
    """
    section = score_section()
    problems: list[str] = []

    evaluation = config.Evaluation.load(EVALUATION)
    scoring = evaluation.scoring
    audit = evaluation.stage("audit")
    gates = list(audit.gates)
    required = [g for g in gates if g.required]
    advisory = [g for g in gates if not g.required]

    # Through the loaders, not tomllib: these are the objects grading builds, so a
    # default that the TOML leaves out is read here the way the runner reads it.
    suite = config.Suite.load(HERE / "behavioural" / "suite.toml")
    modules = suite.modules
    probe = config.Probe.load(HERE / "verification" / "probe.toml")

    # --- stage 1 ---------------------------------------------------------- #
    # Four stage 1 figures are asserted *absent* from the prose: the gate count,
    # how many are required, how many advisory, and the per-gate sample count.
    # README.md's invariant is why -- "the agent gets the goal, not the rubric" --
    # and SCHEMA.md spells out the reason, since an instruction that enumerates
    # checks is an instruction to satisfy checks.  A gate count is not a fact
    # about what State B has to be; it is a fact about how many questions the
    # reviewer was handed, and a solver who knows ten of twelve are fatal knows
    # how much of the document is load-bearing.
    #
    # What stays checked is every consequence the author does have to be told
    # about.
    for pattern, what in (
        (r"(\w+) questions about whether", "the stage 1 gate count"),
        (r"(\w+) of the \w+ are required", "how many gates are required"),
        (r"and (\w+) [—-]+ the licence", "how many gates are advisory"),
        (r"(\w+) independent reviews", "how many reviews answer each gate"),
        (r"across ([\d,]+) cases", "the exact stage 2 case count"),
        (r"grouped into (\w+) modules", "the stage 2 module count"),
    ):
        found = re.search(pattern, section, re.IGNORECASE)
        if found is not None:
            problems.append(
                f"instruction.md's scoring section states {what} "
                f"({found.group(0)!r}), which is rubric rather than contract -- "
                f"README.md promises the agent the goal and not the rubric, and "
                f"this figure was removed once already")

    # samples, checked against the config alone.  The prose promises no number,
    # and a stage that sets none lets the runner's default decide how many reviews
    # answer a gate, which is not this task's to assume.
    # Not `.get("samples") is not None and ...`: samples lives in the stage's option
    # bag, which takes any key without complaint, so a typo there would leave this
    # comparison skipped and passing.  An absent key is the finding.
    if audit.options.get("samples") is None:
        problems.append(
            "[stages.audit] sets no samples -- the runner's default decides how "
            "many independent reviews answer each gate, which makes the strength of "
            "the whole stage a property of the harness version rather than of this "
            "task")

    # The claim a submission could act on: "there is no JDK and no CMake". Checked
    # against the assertion in the image that makes it true, since a stage 1 that
    # gained a toolchain would make this paragraph an invitation to build.
    if "no JDK and no CMake" in section:
        dockerfile = (HERE / "audit" / "Dockerfile").read_text(
            encoding="utf-8")
        if "javac java jar cmake" not in flatten(dockerfile):
            problems.append(
                "instruction.md tells the author stage 1 has no JDK and no CMake, "
                "but audit/Dockerfile no longer asserts their absence")

    # --- stage 2 ---------------------------------------------------------- #
    stated = number(section, r"behavioural compatibility\.\s*(\d+) points",
                    "behavioural points", problems)
    if stated is not None and stated != int(scoring.behavioural_points):
        problems.append(f"instruction.md says stage 2 is worth {stated}; "
                        f"[scoring] sets behavioural_points = "
                        f"{scoring.behavioural_points}")
    # The case count and the module count are asserted absent above.  suite.toml
    # still declares `cases`, and the stage 2 image's own `driver.py --self-check`
    # still compares that declaration against the catalog frozen inside it, so the
    # figure remains checked -- against the thing it describes rather than against a
    # sentence the agent reads.
    declared_cases = suite.metadata.get("cases")

    # `build` first and carrying weight.  A property of the suite, not of the prose:
    # the module that establishes there is something to measure has to run before the
    # modules that measure it, whatever any document says.  The second half is that
    # the build module counts for something, since a weight-0 build would run, be
    # depended on by every module after it, and contribute nothing to the number.
    if modules[0].id != "build":
        problems.append(
            f"suite.toml's first module is {modules[0].id!r}; the module that "
            f"establishes the build produced the installed artifacts has to run "
            f"first, or the modules after it measure an empty tree and call it a low "
            f"score")
    elif not modules[0].weight > 0:
        problems.append(
            f"suite.toml gives the build module weight {modules[0].weight!r}; every "
            f"later module depends on what it produces, so it has to count for "
            f"something")

    # What the author is told about the modules that carry a consequence worth
    # planning around -- stated as the consequence, never as the module id.  A module
    # id is an internal name; what a solver plans around is what happens.
    #
    # The table is the trigger: each entry names a module that must exist and a
    # sentence instruction.md must carry, and dropping either one is what fails here.
    # Keyed on the table rather than on the suite on purpose.  No module is required,
    # so a loop over `[m for m in modules if m.required]` would iterate an empty list
    # and check nothing -- a check that passes because it looked at nothing is the
    # failure mode this file exists to catch elsewhere.
    disclosure = {
        # "A default build that does not produce the installed artifacts leaves
        # nothing to measure."
        "build": r"default build that does not produce the installed artifacts",
        # "the `java.util.zip` prohibition at the top of this document holds here as
        # well as in stage 1 -- a cheat that no input can detect does not cost less
        # than one that every input catches."
        "provenance": r"cheat that no input can detect does not cost less",
    }
    declared = {m.id for m in modules}
    for module, pattern in sorted(disclosure.items()):
        if module not in declared:
            problems.append(
                f"`disclosure` has an entry for the {module!r} module and suite.toml "
                f"does not declare it -- either the module was renamed and this table "
                f"was not, or instruction.md is carrying a sentence about something "
                f"that no longer runs")
        elif not re.search(pattern, section, re.IGNORECASE):
            problems.append(
                f"instruction.md's scoring section no longer contains the disclosure "
                f"that covers the {module!r} module (/{pattern}/) -- that is a "
                f"consequence the author was not told about")
    # And the reverse: prose naming a module as required at all.  `required` is not a
    # module key, so such a sentence describes a distinction the stage does not draw,
    # on top of leaking an id the author is told nothing about.
    for claimed in re.findall(r"`([a-z-]+)` module[^.]*\brequired\b", section):
        problems.append(
            f"instruction.md's scoring section names the {claimed!r} module as "
            f"required; no module is -- the stage is all-or-nothing over every "
            f"weighted module, so a single failed check anywhere costs all 40 points "
            f"-- and this names an internal id the author is told nothing about")

    # driver.py routes the provenance guard cases to a module id declared one
    # directory up.  Call the driver's own function instead of restating the rule
    # here: it also runs at image
    # build time from --self-check, so both places test one implementation.  It
    # raises SystemExit, and this checker's contract is to collect problems.
    # dont_write_bytecode first: importing driver drags in six sibling modules, and
    # the default behaviour drops a __pycache__ into the directory that ships.  The
    # behavioural image deletes bytecode as its last layer, so the image stays clean
    # either way -- but a checker that dirties the tree it checks leaves .pyc files
    # for the packaging step to carry, and it wrote seven of them before this line.
    was_writing, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        sys.path.insert(0, str(LIB))
        import driver  # noqa: PLC0415 - deliberately late; needs LIB on the path
        driver.check_guard_module_declared(HERE / "behavioural" / "suite.toml")
    except SystemExit as exc:
        problems.append(f"{exc}")
    finally:
        sys.path.remove(str(LIB))
        sys.dont_write_bytecode = was_writing

    # --- stage 3 ---------------------------------------------------------- #
    stated = number(section, r"verification\.\s*(\d+) points", "verification points",
                    problems)
    if stated is not None and stated != int(scoring.verification_points):
        problems.append(f"instruction.md says stage 3 is worth {stated}; "
                        f"[scoring] sets verification_points = "
                        f"{scoring.verification_points}")
    stated = number(section, r"(\w+) independent attempts", "attempt count",
                    problems)
    adversaries = probe.adversaries
    if stated is not None and stated != scoring.verification_models:
        problems.append(f"instruction.md says {stated} attempts; [scoring] sets "
                        f"verification_models = {scoring.verification_models}")
    if stated is not None and stated != len(adversaries):
        problems.append(f"instruction.md says {stated} attempts; probe.toml defines "
                        f"{len(adversaries)} adversaries")
    stated = number(section, r"each with (\w+) hours? to write", "per-attempt budget",
                    problems)
    budgets = {a.budget_sec for a in adversaries}
    if stated is not None and budgets != {stated * 3600.0}:
        problems.append(f"instruction.md gives each attempt {stated} hour(s); "
                        f"probe.toml sets budget_sec = "
                        f"{', '.join(str(b) for b in sorted(budgets))}")
    stated = number(section, r"reproduces identically (\w+) times", "rerun count",
                    problems)
    if stated is not None and stated != probe.scope.reruns:
        problems.append(f"instruction.md says a candidate must reproduce {stated} "
                        f"times; probe.toml sets reruns = {probe.scope.reruns}")
    stated = number(section, r"finds nothing is worth (\d+) points",
                    "points per survival", problems)
    if stated is not None and stated != int(scoring.points_per_survived_model):
        problems.append(f"instruction.md says {stated} points per attempt that "
                        f"finds nothing; [scoring] sets points_per_survived_model = "
                        f"{scoring.points_per_survived_model}")

    # --- the ladder ------------------------------------------------------- #
    # The entry condition, which under an all-or-nothing stage 2 is a sentence
    # rather than a figure: there is no number between 0 and behavioural_points for
    # a threshold to be about, so what is checked is that the prose says so.  A
    # missing sentence is the drift that matters -- an instruction that leaves the
    # condition unstated lets an agent read stage 3 as reachable from a near miss.
    if not re.search(r"runs only on a stage 2 at full marks", section, re.I):
        problems.append("instruction.md's scoring section does not state the "
                        "stage 3 entry condition -- expected text matching "
                        "/runs only on a stage 2 at full marks/")
    stated = number(section, r"behavioural compatibility\. (\d+) points",
                    "stage 2 total", problems)
    if stated is not None and stated != int(scoring.behavioural_points):
        problems.append(f"instruction.md says stage 2 is worth {stated}; [scoring] "
                        f"sets behavioural_points = {scoring.behavioural_points:g}")
    stated = number(section, r"verification\. (\d+) points", "stage 3 total",
                    problems)
    if stated is not None and stated != int(scoring.verification_points):
        problems.append(f"instruction.md says stage 3 is worth {stated}; [scoring] "
                        f"sets verification_points = {scoring.verification_points:g}")
    stated = number(section, r"([\d,]+) points, all of them in the last two",
                    "maximum", problems)
    if stated is not None and stated != int(scoring.max_score):
        problems.append(f"instruction.md says {stated} points total; [scoring] sets "
                        f"max_score = {scoring.max_score}")

    # Two arithmetic identities are deliberately *not* checked here --
    # `behavioural_points + verification_points == max_score`, and
    # `verification_models x points_per_survived_model == verification_points`.
    # `ScoringPolicy.from_dict` raises on both, so a config that breaks either never
    # reaches this function: `Evaluation.load` above would have thrown.  They were
    # written here first, and three mutants that should have proved them instead died
    # in the loader -- which is how the duplication showed up.  What is left is the
    # half the harness cannot see: whether the prose agrees with the fields.

    # A promise of partial credit, which the ladder cannot keep.  "stops at N keeps
    # N" is the shape an instruction takes when a stage pays a rate, and any
    # sentence of that shape is now false whatever N is -- a submission one check
    # short is paid what an untouched repository is paid.  Checked as a prohibition
    # rather than as arithmetic, because there is no correct N.
    for match in re.finditer(r"stops at (\d+) keeps \1", section):
        problems.append(f"instruction.md promises partial credit -- {match.group(0)!r} "
                        f"-- but stage 2 pays {scoring.behavioural_points:g} only for "
                        f"a submission that passed every scored check, and nothing "
                        f"otherwise")

    say(f"stage 1: {len(gates)} gates, {len(required)} required, "
        f"{len(advisory)} advisory ({', '.join(g.id for g in advisory)})")
    # No "required:" tail, unlike the stage-1 line above: every weighted module has
    # to pass every scored check, so there is no subset to name.  The weights below
    # are the whole of what a module is worth.
    say(f"stage 2: {scoring.behavioural_points:g} pts, "
        f"{declared_cases} cases, {len(modules)} modules")
    say(f"stage 3: {scoring.verification_points:g} pts, {len(adversaries)} adversaries "
        f"x {scoring.points_per_survived_model:g} pts, reruns "
        f"{probe.scope.reruns}, entered only on a complete stage 2")
    return problems


# --------------------------------------------------------------------------- #
# requirements
# --------------------------------------------------------------------------- #

#: The source files holding scored delivery checks, in catalog dispatch order.
CHECK_SOURCES = ("structure.py", "classfile.py", "manifest_check.py")

#: Names belonging to this repository's interface -- the kind of thing the
#: contract exists to state.  Deliberately not a general word list: a check that
#: hardcodes `Test #` or `BOOL` is describing a tool's output format, which is not
#: a requirement on the submission, while one that hardcodes `example` or
#: `zlibstatic` is describing State A.
REPO_LITERALS = re.compile(
    r"\b(example|minigzip|zlib\.h|zconf\.h|zlib\.map|zlib\.pc|libz|"
    r"org[./]zlib|org\.zlib|zlib-1\.3\.1|zlib\.jar|share/java|"
    r"zlibstatic|module-info|ZLIB_BUILD_EXAMPLES|SKIP_INSTALL|"
    r"CMakeLists|LICENSE|ZStream|GzFile|GzHeader|Zlib)\b")

#: Where each hardcoded expectation is stated, for the agent to read.
#:
#: `contract:<dotted.path>` resolves against source-contract.json; anything else
#: is a literal phrase that must appear in instruction.md.  A locator is not
#: decoration: it is checked, so a requirement that gets reworded out of the
#: instruction fails here rather than becoming a surprise at grading time.
#:
#: An entry is needed only because the check hardcodes its expectation.  The way
#: off this list is to read the expectation from the contract instead, which is
#: strictly better -- the contract ships to the agent, so the requirement travels
#: with the thing that enforces it and cannot go unstated.
STATED_BY = {
    "api/stream-fields": "contract:api_contract",
    "build/cache-options": "contract:build_contract.cache_options",
    "build/ctest-registration": "contract:driver_contract.ctest_registration",
    "build/examples-flag": "ZLIB_BUILD_EXAMPLES",
    "build/out-of-source": "out-of-source",
    "build/version-reported": "contract:build_contract",
    "classfile/no-library-load": "System.load",
    "classfile/no-process-spawn": "no `Runtime.exec`, no `ProcessBuilder`",
    "install/no-headers": "No `include/zlib.h`",
    "module/descriptor-present": "`module-info.class` at its root",
    "source/cmake-no-dangling": "dangling reference",
    "source/cmake-targets": "contract:build_contract.target_names",
    "source/license": "LICENSE",
    "source/module-info-declares": "`module-info` replaces",
    "source/spec-header-kept": "zlib.h",
}


def check_bodies() -> dict[str, tuple[str, str]]:
    """check_* method name -> (file, source text with docstring and comments cut).

    Prose naming `example` is not a hardcoded expectation, and every check in this
    task carries a long docstring explaining itself, so the text scanned has to be
    code only or the scan flags all 71.
    """
    bodies: dict[str, tuple[str, str]] = {}
    for filename in CHECK_SOURCES:
        path = LIB / filename
        if not path.exists():
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not node.name.startswith("check_") or node.name in bodies:
                continue
            segment = ast.get_source_segment(source, node) or ""
            code = "\n".join(line for line in segment.splitlines()
                             if not line.strip().startswith("#"))
            code = re.sub(r'"""(?:.|\n)*?"""', "", code)
            bodies[node.name] = (filename, code)
    return bodies


def hardcoding_cases() -> dict[str, tuple[float, str, list[str]]]:
    """Scored structure cases whose check body hardcodes a State A name."""
    bodies = check_bodies()
    flagged: dict[str, tuple[float, str, list[str]]] = {}
    unresolved: list[str] = []

    for case_id, check, _configs, weight, _note in literal(
            LIB / "catalog.py", "STRUCT_CASES", anywhere=True):
        slug = check.replace("-", "_")
        wanted = ("check" + slug).replace("_", "")
        names = [name for name in bodies
                 if name == f"check_{slug}" or name.endswith(f"_{slug}")
                 or name.replace("_", "") == wanted]
        if not names:
            unresolved.append(f"{case_id} -> {check}")
            continue
        _filename, code = bodies[min(names, key=len)]
        hits = {text for pair in re.findall(r'"([^"\n]*)"|\'([^\'\n]*)\'', code)
                for text in pair if text and REPO_LITERALS.search(text)}
        if hits:
            flagged[case_id] = (weight, min(names, key=len), sorted(hits))

    if unresolved:
        raise SystemExit(
            "check-task.py: no method found for " + ", ".join(unresolved) +
            " -- the catalog's dispatch key stopped matching the method name, so "
            "this group would silently scan nothing")
    return flagged


def check_requirements(say) -> list[str]:
    """Every enforced requirement is stated where the agent can read it.

    `environment/Dockerfile` claims this invariant in as many words -- "every
    compile-time requirement in instruction.md is machine-checked against
    source-contract.json, and no requirement is enforced that the instruction does
    not state" -- and until this group existed, nothing checked it.  Three
    requirements had gone unstated: `ctest -N` listing `example`, the implicit
    public constructor `javac` supplies, and `ZLIB_BUILD_EXAMPLES` having to
    remain a declared `option()` so it lands in the cache as `:BOOL`.  All three
    were satisfiable, so all three were traps rather than broken checks, and each
    cost a submission points for a rule it was never given.

    Checking "is this documented" in general is not possible here and this does
    not try.  It checks the mechanism instead.  A check that reads its expectation
    from the contract cannot be unstated, because the contract is delivered to the
    agent.  A check that hardcodes a repository name can be, and two of the three
    did.  So the hardcoded ones are enumerated, and each has to say where it is
    stated.

    A cheaper design was measured first and rejected: scanning every scored case's
    note for vocabulary absent from instruction.md and the contract flags 61 of
    106, nearly all on prose ("succeed", "green", "carries").  A list that long is
    not read, and the one real finding in it would not have been seen.
    """
    contract = json.loads(
        (ENVIRONMENT / "source-contract.json").read_text(encoding="utf-8"))
    instruction = INSTRUCTION.read_text(encoding="utf-8")
    problems: list[str] = []

    def resolves(locator: str) -> str:
        if locator.startswith("contract:"):
            node = contract
            for part in locator[len("contract:"):].split("."):
                if not isinstance(node, dict) or part not in node:
                    return f"source-contract.json has no {locator[9:]}"
                node = node[part]
            if isinstance(node, str) and not node.strip():
                return f"source-contract.json {locator[9:]} is empty"
            return ""
        return "" if locator in instruction else (
            f"instruction.md does not contain {locator!r}")

    flagged = hardcoding_cases()

    # 1. A hardcoded expectation with nowhere stated is the defect this group is
    #    for.  Weight is reported because it is what the submission loses.
    for case_id, (weight, method, hits) in sorted(flagged.items()):
        if case_id not in STATED_BY:
            problems.append(
                f"{case_id} (weight {weight:g}) hardcodes {hits[:3]} in {method} "
                f"and STATED_BY does not say where the instruction states it -- "
                f"either read the expectation from source-contract.json or add "
                f"the locator")

    # 2. A locator that does not resolve is the same defect by the other door:
    #    the check stays and the sentence is reworded away.
    for case_id, locator in sorted(STATED_BY.items()):
        why = resolves(locator)
        if why:
            problems.append(f"{case_id} is recorded as stated by {locator!r}, but {why}")

    # 3. A stale entry means the map is describing a check that is gone, and the
    #    next reader trusts it.
    for case_id in sorted(set(STATED_BY) - set(flagged)):
        problems.append(
            f"STATED_BY has {case_id}, which no longer hardcodes anything -- "
            f"delete the entry, the check now reads its expectation")

    # 4. Finding B's invariant, which is conditional on the contract rather than
    #    on any check: if the contract lists a concrete public class with no
    #    constructors, the closed-world note has to disarm the implicit one, or
    #    the obvious way to write that class widens the surface by default and
    #    fails a required module.
    api = contract.get("api_contract", {})
    note = api.get("closed_world_note", "")
    classes = api.get("classes", [])
    if not isinstance(classes, list) or not classes:
        problems.append(
            "api_contract.classes is missing or not a list, so the "
            "implicit-constructor invariant below checked nothing")
    else:
        # `constructors: []` is the trap and a missing key is not: the empty list
        # says "this type declares no constructor", which is exactly the case
        # javac fills in with a public one.  `abstract` is exempt because an
        # abstract class's constructor is not callable as the listed type's, and a
        # declared `{"params": []}` is exempt because then the no-arg constructor
        # is part of the stated surface.
        traps = sorted(
            cls.get("name", "?") for cls in classes
            if isinstance(cls, dict)
            and cls.get("constructors") == []
            and "abstract" not in (cls.get("modifiers") or [])
            and str(cls.get("kind", "class")).lower() == "class")
        if traps and "implicit" not in note.lower():
            problems.append(
                f"api_contract lists {', '.join(traps)} with an empty "
                f"constructors list and closed_world_note does not mention the "
                f"implicit one -- javac supplies a public no-arg constructor, so "
                f"the obvious way to write those classes fails "
                f"api/no-extra-public (weight 3.0) and the required "
                f"guard/no-api-widening")
        say(f"{len(classes)} contract classes, {len(traps)} needing an explicit "
            f"private constructor ({', '.join(traps) or 'none'})")

    say(f"{len(flagged)} scored structure case(s) hardcode a State A name; "
        f"{len(STATED_BY)} recorded as stated")
    contract_read = sum(1 for loc in STATED_BY.values()
                        if loc.startswith("contract:"))
    say(f"{contract_read} stated in source-contract.json, "
        f"{len(STATED_BY) - contract_read} in instruction.md")
    return problems


# --------------------------------------------------------------------------- #

def check_collection(say) -> list[str]:
    """Does task.toml's artifact exclude list delete a file grading requires?

    `[[artifacts]].exclude` is applied by the harness when it collects the agent's
    tree and re-materialises it in the verifier container, so every glob there is a
    deletion performed between the submission being written and every check running
    against it.  A glob that reaches a preserved path removes a file stage 1 hashes
    and stage 2 reads, and the resulting low score describes the exclude list
    rather than the submission.

    This is checked here because it cannot be checked anywhere else.  The exclude
    list lives in `task.toml`, which is the harness's file and is inside neither
    image's build context; the preserved paths live in `source-contract.json`.  No
    image can read both.  Worse, the collection step only runs under the real
    harness -- an identity run, or any run driven by hand against a mounted tree,
    grades the *raw* submission and so scores clean no matter what this list says.
    Three sibling tasks shipped a glob that ate a graded fixture for exactly that
    reason, and in each case every local run looked fine.

    The widest plausible reading of the matching is used -- a glob matches if it
    matches the full relative path or any single component of it -- because the
    harness's own semantics are not available from here.  A clean answer under the
    widest reading is clean under every narrower one, which is the only form of
    this check that can be trusted without the collector in hand.

    Deliberately *not* asserted: that no forbidden extension is excluded.  The list
    drops `*.o` and `*.lo`, which are also forbidden extensions, and that is
    consistent with the rule task.toml states -- zlib compiles in-tree, so those
    are debris the published build writes at the repository root, and neither is an
    exploit route.  The compiled files that *are* one (`.a`, `.so`, `.class`,
    `.jar`) are kept, so the gate that names them keeps its evidence.  Which side
    of that line an extension falls on is a reading, not a mechanism, so it stays
    in the comment above the list rather than becoming a check that would have to
    guess.
    """
    problems: list[str] = []

    text = (TASK / "task.toml").read_text(encoding="utf-8")
    globs: list[str] = []
    for block in re.findall(r"^exclude\s*=\s*\[(.*?)\]", text, re.S | re.M):
        globs += re.findall(r'"([^"]+)"', block)
    if not globs:
        # Vacuity guard: an empty list would make every assertion below pass by
        # having nothing to match against, which is the one way this check could
        # report ok while the thing it is about is broken.
        return ["parsed no exclude globs from task.toml; either the [[artifacts]] "
                "block lost its exclude list or this parser stopped matching it, "
                "and both make the collection check pass vacuously"]

    contract = json.loads(
        (ENVIRONMENT / "source-contract.json").read_text(encoding="utf-8"))
    preserved = contract.get("preserved_paths", {}).get("paths", [])
    if not preserved:
        return ["source-contract.json states no preserved_paths.paths, so there "
                "is nothing to hold the exclude list against"]

    for relpath in preserved:
        components = relpath.split("/")
        for glob in globs:
            if (fnmatch.fnmatch(relpath, glob)
                    or any(fnmatch.fnmatch(c, glob) for c in components)):
                problems.append(
                    f"task.toml excludes {glob!r}, which removes the preserved "
                    f"path {relpath!r} during collection. Stage 1 hashes it and "
                    f"stage 2 reads it, so every submission would lose those "
                    f"checks and the score would describe this list rather than "
                    f"the submission -- and no local run would show it, because "
                    f"only the real harness collects.")

    say(f"{len(globs)} exclude glob(s) vs {len(preserved)} preserved path(s): "
        f"{'no collisions' if not problems else f'{len(problems)} collision(s)'}")
    return problems


# --------------------------------------------------------------------------- #

CHECKS = {
    "gate-map": (check_gate_map, True),
    "vocabulary": (check_vocabulary, False),
    "figures": (check_figures, True),
    "requirements": (check_requirements, False),
    "collection": (check_collection, False),
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", choices=sorted(CHECKS),
                        help="run one group; repeatable.  Default: all of them.")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="print only failures")
    args = parser.parse_args()

    groups = args.only or sorted(CHECKS)
    say = (lambda line: None) if args.quiet else (lambda line: print(f"  {line}"))
    if not args.quiet:
        print(f"  task {TASK.name}")

    config = None
    if any(CHECKS[g][1] for g in groups):
        config = load_harness()

    problems: list[str] = []
    for group in groups:
        check, needs_config = CHECKS[group]
        problems += check(config, say) if needs_config else check(say)

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    say(f"{', '.join(groups)}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

