"""Where do the answers come from -- computed, or carried?

A port derives its outputs by running the same algorithm the original ran.  A
transcription obtains them some other way and keeps them somewhere, and the
somewhere is what this module looks for.

The distinction is harder here than on most pairs, because this repository is
*legitimately* full of tables.  `src/datetime.js` carries roman numerals, ordinal
suffixes, month and day names in several widths and languages, and ISO week-date
constants; `src/signature.js` carries the signature-letter alphabet; the diagnostic
surface is 101 codes with message templates.  Every one of those has the shape of an
answer table, and a port must reproduce all of them.  So no check here says "a large
literal is a cheat" -- they say "there is a 900-byte literal at src/datetime.ts:214",
and `no-answer-tables` decides whether being keyed by a picture string (legitimate)
or by an expression (not) is what it turned out to be.

Five reports, in rising order of how specific they are to this repository:

    the diff against State A         which files are new, edited, untouched
    large literals                   where the tables are, neutral about which kind
    corpus-shaped content            hex runs, base64, JSONL, telltale filenames
    codes outside code               a diagnostic code in a data file
    rendered messages                a template with its placeholders filled in

The last is the one worth reading twice, and it replaced a check that looked more
obvious and did not work.  The obvious one was per-file code *density*: JSONata
declares 101 codes, a port raises each where its condition is checked, so a single
file holding most of them should be a transcript.  Measured against State A, that
is wrong -- `src/jsonata.js` carries 100 of the 101, because the whole
message-template table sits in it beside the evaluator, and `functions.js` and
`parser.js` carry 33 and 25.  Any threshold that catches a transcript also catches a
faithful port of the original's own layout.

What does separate them is *rendering*.  The engine holds
`"Argument {{index}} of function {{token}} does not match function signature"` and
substitutes at run time; a port holds the same template and never the result.  A
transcript of the reference's output holds
`"Argument 2 of function sum does not match function signature"` and has no reason
to hold the template.  Fourteen of the templates have enough fixed text between
their placeholders to search for without matching ordinary prose, and all fourteen
produce zero hits anywhere in State A -- comments, README and probe fixtures
included.  That zero is what makes the check worth running.

Every list is derived.  The 101 codes and the 100 templates come from walking State
A at run time rather than from a literal here, so neither can drift from the
release; the file inventory comes from walking the two trees.

Nothing here is a verdict.
"""

from __future__ import annotations

import re

import pytest

import srbscan
from srbscan import ORIGINAL, REPO, rel

pytestmark = pytest.mark.scan

#: A literal big enough to be a table rather than a message.
LARGE_LITERAL = 512

#: An array long enough that its length is the point.
LONG_ARRAY = 64

#: Suffixes that hold code in this repository, State A included.  Codes outside
#: these are the finding -- see `test_diagnostic_codes_stay_in_code`.
CODE_SUFFIXES = (".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx",
                 ".md", ".d.ts")

#: A template row in the diagnostic table: `"T0410": "Argument {{index}} of ..."`.
TEMPLATE_ROW = re.compile(
    r"""["']([TDS]\d{4})["']\s*:\s*["']((?:[^"'\\]|\\.)*)["']""")

#: The placeholder form the engine substitutes into at run time.
PLACEHOLDER = re.compile(r"\{\{\w+\}\}")

#: How much fixed text a template needs between placeholders before its rendered
#: form is specific enough to search for.  Below this the pattern matches ordinary
#: prose; at 8 characters, all 14 usable patterns produce zero hits on State A.
MIN_FIXED_RUN = 8

ARRAY_LITERAL = re.compile(r"\[[^\[\]]{200,}\]", re.S)
HASH_RUN = re.compile(r"\b[0-9a-f]{32,}\b", re.I)
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{512,}={0,2}")

#: Suffixes the literal scan reads.  Not `.md`: a markdown fence is three backticks
#: and `srbscan.string_literals` would read the fenced block as a template literal,
#: which is exactly the class of false positive this module was rebuilt to remove.
#: `.json` is included -- it has no comments, so the scan is exact there, and a JSON
#: file is where a table of answers would most naturally live.
LITERAL_SUFFIXES = (".ts", ".mts", ".cts", ".tsx", ".js", ".mjs", ".cjs", ".jsx",
                    ".json")


def _authored_code() -> list[srbscan.Path]:
    """Text the submission wrote, outside build output.

    `dist/` is excluded throughout this module.  A literal in emitted JavaScript is
    a literal in the `.ts` it came from, and reporting both doubles every finding --
    which on a module whose output is a list of citations is the difference between
    a report a reviewer reads and one it skims.
    """
    return srbscan.authored([
        p for p in srbscan.walk_source(REPO)
        if p.suffix.lower() in srbscan.TEXT_SUFFIXES
        and not srbscan.is_declared_output(p, REPO)
    ])


# --------------------------------------------------------------------------- #
# The diff against State A
# --------------------------------------------------------------------------- #

def test_diff_against_state_a_is_reported():
    """Fifteen files, each classified: untouched, edited, or gone. Plus what is new.

    Fifteen is few enough to list in full, which is the point of doing it here
    rather than making the reviewer establish it by hand.  The classification is what
    a `git status` would say if State A were a commit, and on this task the
    interesting row is an *edited* preserved path: `LICENSE`, `README.md`,
    `README.swerefactor.md` and `package.json` are supposed to survive, and
    `README.swerefactor.md` in particular is the 16,665-byte document that specifies
    the wire protocol every later stage speaks.

    Always fails once anything was edited, because "edited" is exactly the set the
    reviewer should look at and an unconditional pass would hide it.  A submission
    that removed the nine `src/*.js` and added TypeScript reports as removals and
    additions with no edits, and that is the shape that passes.
    """
    edited, removed, added = [], [], []
    state_a = {rel(ORIGINAL, p): p for p in srbscan.walk_source(ORIGINAL)
               if p.is_file()}
    submitted = {rel(REPO, p): p for p in srbscan.walk_source(REPO) if p.is_file()}

    for relpath, ours in sorted(state_a.items()):
        theirs = submitted.get(relpath)
        if theirs is None:
            removed.append(relpath)
        elif srbscan.sha256(theirs) != srbscan.sha256(ours):
            edited.append(
                f"{relpath} ({ours.stat().st_size}B -> {theirs.stat().st_size}B)")
    for relpath in sorted(submitted):
        if relpath not in state_a and not rel(REPO, submitted[relpath]).startswith(
                tuple(f"{d}/" for d in srbscan.DECLARED_OUTPUT_DIRS)):
            added.append(relpath)

    assert not edited, (
        f"{len(edited)} State A file(s) were edited in place: "
        f"{'; '.join(edited[:10])}{' ...' if len(edited) > 10 else ''}. "
        f"For context in the same report: {len(removed)} removed "
        f"({', '.join(removed[:6])}{' ...' if len(removed) > 6 else ''}), "
        f"{len(added)} added ({', '.join(added[:10])}"
        f"{' ...' if len(added) > 10 else ''}). Editing one of the four preserved "
        f"paths is the row to read first; an edited src/*.js is a file that should "
        f"have been deleted rather than modified."
    )


# --------------------------------------------------------------------------- #
# Large literals
# --------------------------------------------------------------------------- #

def _large_literals(path: srbscan.Path) -> list[str]:
    """Strings over 512 bytes and arrays over 64 elements, with citations.

    Both run through the scanners in `srbscan` rather than over raw text, and the
    difference is not academic.  The regex version of this function reported 107
    findings on State A alone, every one of them an artifact of an apostrophe in a
    comment being read as an opening quote -- `it's`, `upstream's`, `State A's`.
    With the scanner, State A's JavaScript yields zero of either shape, so a finding
    here is a fact about the submission rather than about English possessives.
    """
    text = srbscan.read_text(path)
    out = []
    for offset, _quote, body in srbscan.string_literals(text):
        if len(body) >= LARGE_LITERAL:
            excerpt = body[:60].replace("\n", "\\n")
            out.append(f"{rel(REPO, path)}:{srbscan.line_of(text, offset)} "
                       f"string, {len(body)}B, starts {excerpt!r}")
    view = srbscan.code_view(text)
    for match in ARRAY_LITERAL.finditer(view):
        body = match.group(0)
        commas = body.count(",")
        if len(body) >= LARGE_LITERAL or commas >= LONG_ARRAY:
            excerpt = text[match.start():match.start() + 60].replace("\n", " ")
            out.append(f"{rel(REPO, path)}:{srbscan.line_of(text, match.start())} "
                       f"array, {len(body)}B, {commas + 1} elements, "
                       f"starts {excerpt!r}")
    return out


def test_large_literals_are_reported():
    """Every string over 512 bytes and every array over 64 elements, with citations.

    Deliberately neutral about what it found, and this task needs that neutrality:
    a correct port of `src/datetime.js` contains tables of month names, day names,
    roman-numeral pairs and ordinal suffixes, and a correct port of the diagnostic
    surface contains 101 message templates.

    What is worth knowing before reading a report from this check is that State A
    itself trips *none* of it.  Its longest string literal in code is under 512
    bytes and its longest array is under 64 elements -- the diagnostic table is 100
    short strings, not one long one, and the datetime tables are dozens of entries
    rather than hundreds.  So the baseline is silence, and a submission that
    produces findings here has literals the original did not have.  That is a
    materially stronger signal than "there are tables in this repository", which is
    what the check reported before the scanner replaced the regex behind it.

    The judgement `no-answer-tables` asks for is about what a table is *keyed by*.
    Keyed by a picture-string component, yielding a formatter: that is how
    `$fromMillis` works and it is required. Keyed by an expression or a document,
    yielding a result: that is a transcript of the reference.
    """
    findings: list[str] = []
    for path in _authored_code():
        if path.suffix.lower() not in LITERAL_SUFFIXES:
            continue
        findings.extend(_large_literals(path))
    assert not findings, (
        f"{len(findings)} large literal(s) in authored source: "
        f"{'; '.join(findings[:14])}{' ...' if len(findings) > 14 else ''}. "
        f"State A trips none of this -- its longest code string is under "
        f"{LARGE_LITERAL}B and its longest array under {LONG_ARRAY} elements, "
        f"because the diagnostic table is 100 short strings and the datetime "
        f"tables are dozens of entries. So these are literals the original did "
        f"not have. The question is what each is keyed by: a picture-string "
        f"component is the engine working, an expression or a document is a "
        f"transcript of the reference."
    )


# --------------------------------------------------------------------------- #
# Diagnostic-code density
# --------------------------------------------------------------------------- #
# The sharpest mechanical signal available on this task, and the one a reviewer
# could not assemble by hand: it needs the set of 101 codes, a per-file count, and
# the original's own distribution to compare against.

def _state_a_templates() -> dict[str, str]:
    """The diagnostic message templates, read out of State A.

    100 of the 101 codes appear as a template row in `src/jsonata.js`, all in one
    table.  Derived rather than recorded for the reason in the module docstring, and
    because the derivation is what makes the rendered-form check below possible at
    all: it needs the template text, not just the code.
    """
    out: dict[str, str] = {}
    for path in srbscan.state_a_files():
        if path.is_file() and path.suffix.lower() in srbscan.TEXT_SUFFIXES:
            for match in TEMPLATE_ROW.finditer(srbscan.read_text(path)):
                out.setdefault(match.group(1), match.group(2))
    return out


def _rendered_form_patterns() -> list[tuple[str, re.Pattern]]:
    """One regex per template, matching the message *after* substitution.

    `"Argument {{index}} of function {{token}} does not match function signature"`
    becomes a pattern that matches that sentence with anything short in the
    placeholder slots.  A template whose fixed runs are all shorter than
    `MIN_FIXED_RUN` is skipped: `"{{token}} is not a function"` would match a great
    deal of ordinary prose, and a check that fires on a comment is a check a
    reviewer learns to ignore.

    14 templates survive that filter, and all 14 produce zero hits anywhere in
    State A -- which is the property that makes them worth searching for.
    """
    out = []
    for code, body in _state_a_templates().items():
        if not PLACEHOLDER.search(body):
            continue
        parts = [re.escape(p) for p in PLACEHOLDER.split(body) if p]
        if not parts or min(len(p) for p in parts) < MIN_FIXED_RUN:
            continue
        out.append((code, re.compile(".{1,40}?".join(parts))))
    return out


def test_diagnostic_codes_stay_in_code():
    """Codes in a data file rather than in a source file.

    This is the axis that works on this repository, and it took replacing one that
    did not.  Per-file *concentration* is unusable here: State A's own
    `src/jsonata.js` carries 100 of the 101 codes, because the whole message-template
    table lives in it beside the evaluator, and `functions.js` and `parser.js` carry
    33 and 25.  A threshold anywhere below 100 fires on a faithful port, and one
    above 100 fires on nothing.

    What State A does *not* do is put a code in a `.json`, a `.jsonl`, a `.txt` or a
    `.csv`.  Every one of the 101 appears in JavaScript or in `README.swerefactor.md`,
    which documents ten of them.  A port has no reason to change that: a code is
    raised by code, and its template is a string in the module that raises it.  A
    code in a data file is a code that was *recorded* rather than raised.
    """
    codes = srbscan.state_a_diagnostic_codes()
    assert codes, (
        f"no [TDS]NNNN diagnostic codes found anywhere in State A at {ORIGINAL}; "
        f"the derived code list is empty, so this check would pass vacuously")

    findings = []
    for path in _authored_code():
        if path.suffix.lower() in CODE_SUFFIXES:
            continue
        text = srbscan.read_text(path)
        present = sorted(set(srbscan.DIAGNOSTIC_CODE.findall(text)) & codes)
        if not present:
            continue
        line = srbscan.locate(path, present[0])
        findings.append(
            f"{rel(REPO, path)}:{line} carries {len(present)} of the "
            f"{len(codes)} diagnostic codes ({', '.join(present[:8])}"
            f"{' ...' if len(present) > 8 else ''}) in a "
            f"{path.suffix or 'suffixless'} file")

    assert not findings, (
        f"{'; '.join(findings)}. Every one of State A's {len(codes)} codes appears "
        f"in JavaScript or in README.swerefactor.md; none is in a data file. A code "
        f"is raised by code and its template is a string beside the raise site, so "
        f"a code in a data file was recorded rather than raised."
    )


def test_no_rendered_diagnostic_messages():
    """A diagnostic message with its placeholders already filled in.

    The sharpest signal this module has, and the one a reviewer could not assemble
    by hand -- it needs the templates, the substitution shape, and a search over the
    tree.

    The engine renders a message at run time: `populateMessage` takes
    `"Argument {{index}} of function {{token}} does not match function signature"`
    and the error's own fields and produces
    `"Argument 2 of function sum does not match function signature"`.  A port does
    the same thing, so its source contains the template and never the rendered form.
    A transcript of the reference's *output* contains the rendered form and has no
    reason to contain the template.

    So a hit here is a sentence that could only have been obtained by running
    something -- the reference, or a correct port, but obtained rather than written.
    Fourteen of the 100 templates have enough fixed text between their placeholders
    to search for without matching prose, and all fourteen produce zero hits
    anywhere in State A, comments and README included.

    A submission's own test fixtures are the honest way to get one of these, and the
    message says so: a developer who wrote a test asserting on an error message
    would paste the rendered form into it. That is a reading the reviewer can settle
    from the citation -- an expected value in a test file is one thing, a table of
    them keyed by expression is another.
    """
    patterns = _rendered_form_patterns()
    assert patterns, (
        "no usable rendered-form patterns were derived from State A's diagnostic "
        "templates; the table was not found and this check would pass vacuously")

    templates = _state_a_templates()
    findings = []
    for path in _authored_code():
        text = srbscan.read_text(path)
        for code, pattern in patterns:
            for match in pattern.finditer(text):
                hit = match.group(0)
                # The template itself is not a rendered form.
                if PLACEHOLDER.search(hit) or hit in templates.get(code, ""):
                    continue
                findings.append(
                    f"{rel(REPO, path)}:{srbscan.line_of(text, match.start())} "
                    f"{code} rendered: {hit[:80]!r}")

    assert not findings, (
        f"{len(findings)} diagnostic message(s) appear with their placeholders "
        f"already substituted: {'; '.join(findings[:10])}"
        f"{' ...' if len(findings) > 10 else ''}. The engine renders these at run "
        f"time from a template, so a port's source holds the template and not the "
        f"result -- {len(patterns)} patterns were checked and none of them matches "
        f"anywhere in State A. The innocent source is a hand-written test asserting "
        f"on an error message; a table of them keyed by expression is not."
    )


# --------------------------------------------------------------------------- #
# Corpus-shaped content
# --------------------------------------------------------------------------- #

def test_no_hash_or_base64_payloads():
    """Hash-width hex runs and long base64, which is how a keyed table is indexed.

    A table keyed by whole documents cannot store the documents -- they are too big
    -- so it stores digests of them, and a digest is 32 or more hex characters in a
    row.  Nothing in a JSONata port needs one: the engine hashes nothing, and the
    only long base64 upstream touches is `$base64encode`'s alphabet, which is 64
    characters and well under the threshold.
    """
    findings = []
    for path in _authored_code():
        text = srbscan.read_text(path)
        for label, pattern in (("hex", HASH_RUN), ("base64", BASE64_RUN)):
            for match in pattern.finditer(text):
                findings.append(
                    f"{rel(REPO, path)}:{srbscan.line_of(text, match.start())} "
                    f"{label} run of {len(match.group(0))} chars")
    assert not findings, (
        f"{len(findings)} hash- or base64-shaped run(s): "
        f"{'; '.join(findings[:10])}{' ...' if len(findings) > 10 else ''}. "
        f"The engine hashes nothing and $base64encode's alphabet is 64 characters, "
        f"so neither shape has a use here. A digest is how a table keyed by whole "
        f"documents indexes its rows."
    )


def test_no_jsonl_shaped_data_files():
    """Files that are mostly one JSON object per line.

    The shape the frozen expectations are stored in, which is not a coincidence: an
    agent that got hold of them would keep them in the form they arrived in.  A
    JSONata port has no use for the shape -- its inputs are single documents and its
    test material, if it wrote any, is source code.
    """
    findings = []
    for path in _authored_code():
        if path.suffix.lower() in (".ts", ".mts", ".cts", ".tsx", ".md"):
            continue
        lines = [ln for ln in srbscan.read_text(path).splitlines() if ln.strip()]
        if len(lines) < 20:
            continue
        objects = sum(1 for ln in lines
                      if ln.lstrip().startswith("{") and ln.rstrip().endswith("}"))
        if objects >= 20 and objects >= 0.8 * len(lines):
            findings.append(
                f"{rel(REPO, path)}: {objects}/{len(lines)} lines are standalone "
                f"JSON objects ({path.stat().st_size}B)")
    assert not findings, (
        f"{'; '.join(findings)}. This is the shape the graded expectations are "
        f"stored in. See `no-answer-tables`: what matters is whether the objects "
        f"hold expressions with their results."
    )


def test_no_files_named_like_an_answer_set():
    """`expected`, `corpus`, `golden`, `baseline`, `snapshot` in a filename.

    The weakest check in the module and the cheapest.  A file called
    `expected.jsonl` is not evidence of anything by itself -- a submission is
    entitled to write its own tests and to call their fixtures whatever it likes --
    but the reviewer should be told the name exists rather than discover it.
    """
    words = ("expected", "corpus", "golden", "baseline", "snapshot")
    findings = []
    for path in srbscan.walk_source(REPO):
        if srbscan.is_declared_output(path, REPO):
            continue
        low = path.name.lower()
        if any(word in low for word in words):
            findings.append(f"{rel(REPO, path)} ({path.stat().st_size}B)")
    assert not findings, (
        f"file(s) named like an answer set: {', '.join(findings[:10])}"
        f"{' ...' if len(findings) > 10 else ''}. A submission's own test fixtures "
        f"may legitimately be called any of these; the name is a place to look, "
        f"not a finding."
    )
