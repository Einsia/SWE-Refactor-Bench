#!/usr/bin/env python3
"""Refuse a candidate that would learn which tree it is on without asking it.

Run by run-candidate.sh before pytest, on both trees, on every candidate.  Exit 0
lets the candidate run; exit 65 refuses it, with the reason on stderr.

The problem this exists for
---------------------------
A candidate that reads `$SRB_PROBE`'s bytes, or lists a directory, or looks at an
environment variable, can find out whether it is running against the Go original
or the Zig submission -- and then pass on one and fail on the other by saying so.
That satisfies every mechanical condition the adjudicator checks: it passes on the
original, fails on the submission, and reproduces exactly.  It establishes nothing
whatever about the migration, and it is worth ten points if nobody notices.

probe.toml's deny list says such a candidate is out of scope, and the adjudicator
would very likely reject it.  "Very likely" is not the right guarantee for a rule
with no honest exception, so this is the mechanical half: an import the candidate
has no legitimate use for is refused before it runs, on both trees, so the
adversary sees the refusal in the same turn and can spend the rest of its budget
on a real question.

Why an allow list of imports
----------------------------
Because the deny list is unbounded and the allow list is not.  A stage-3 candidate
for this task needs one thing: to send YAML through `srbyaml` and assert on what
comes back.  Everything a candidate legitimately does with a YAML library is
string, bytes, JSON and arithmetic work.  Nothing it legitimately does touches the
filesystem, another process, the network, or this process's environment -- the
source it wants to send is a string it wrote, and the answer arrives as bytes.

So the rule is: standard-library modules on the list below, plus `srbyaml` and
`pytest`.  Anything else is refused by name, and the name is printed, which makes
the refusal a fact about one line rather than a verdict on the attempt.

Why the regexes do not see string literals
------------------------------------------
Because a candidate's string literals are its test data.  The YAML it sends is a
string it wrote, and YAML scalars are arbitrary text: `"path: /opt/data"` and
`"cmd: eval(x)"` and `"$SRB_HOME"` are ordinary things to feed a YAML parser, and
refusing them would refuse honest work for looking like an evasion.  So every
regex below runs over the source with string literals and comments blanked out,
and what survives blanking is code.

That is safe in the direction that matters: a path in a string is inert without
something to open it, and nothing that opens anything is importable.  The one way
a blanked literal could still bite -- feeding it to something that evaluates it --
needs `eval`, `exec`, `compile` or `__import__` at a call site, and a call site is
code.

This is not a sandbox
---------------------
It is a screen, and it says so.  `getattr(__builtins__, ...)`, an import buried in
a lambda, a name assembled from two halves -- a determined evasion gets through,
and the regexes below only cover the obvious spellings of the obvious tricks.
What makes that acceptable is who is being screened: an adversary that evades a
stated rule in order to identify the tree has not found a defect, and the
transcript showing it trying is what the adjudicator reads.  The screen is here so
that the common case never reaches the adjudicator, not because the adjudicator
can be removed.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import token as token_mod
import tokenize
from pathlib import Path

#: Standard-library modules a candidate may import.  Anything that manipulates
#: text, bytes, numbers, JSON or Unicode, plus the two testing names.  Extending
#: this list is a decision about what a candidate can see, so it is one list in
#: one file rather than a rule spread across a prompt.
ALLOWED_IMPORTS = frozenset({
    # the probe, and the runner
    "srbyaml", "pytest",
    # text and bytes
    "base64", "binascii", "codecs", "difflib", "re", "string", "textwrap",
    "unicodedata",
    # data
    "array", "bisect", "collections", "copy", "dataclasses", "decimal",
    "enum", "fractions", "functools", "heapq", "itertools", "json", "math",
    "numbers", "operator", "pprint", "random", "statistics", "struct", "types",
    "typing", "uuid",
    # dates, which YAML has a tag for
    "calendar", "datetime", "zoneinfo",
    # allowed with a caveat, both stated in the prompt: `sys` because a pytest
    # file reaches for it reflexively and the two things it could leak (the
    # interpreter path, argv) name the token directory rather than the role;
    # `time` because bounding a loop is reasonable even though a timing
    # assertion is out of scope and will be rejected on review.
    "sys", "time",
    # hashing a large response instead of pasting it into an assertion
    "hashlib", "hmac", "zlib",
    # traceback, so a candidate can print one it caught
    "traceback", "warnings", "contextlib", "io",
})

#: Spellings that answer "which tree am I on?" without asking the library, or
#: that reach around the import check.  Each is a regex over the *code*, with
#: string literals and comments blanked (see `blank_literals`), and each names
#: what it is for: a refusal that does not explain itself reads like a broken
#: harness.
FORBIDDEN_CODE: tuple[tuple[str, str], ...] = (
    (r"\bSRB_[A-Z_]+\b", "names a harness variable. The path a tree was staged "
                         "at, and every other value the harness holds, is not "
                         "evidence about the migration"),
    (r"\b__import__\b", "imports by string, which is the import check spelled "
                        "differently"),
    (r"\bimportlib\b", "imports by string, which is the import check spelled "
                       "differently"),
    (r"\beval\s*\(", "evaluates a string as code"),
    (r"\bexec\s*\(", "executes a string as code"),
    (r"\bcompile\s*\(", "compiles a string as code"),
    (r"__builtins__|\bbuiltins\b", "reaches for the builtins table, which is how "
                                   "the import check gets bypassed"),
    (r"__loader__|__spec__|\b__file__\b", "reaches for the import machinery, "
                                          "which knows where this file is"),
    (r"\bglobals\s*\(", "indexes the module globals by name"),
    (r"\bopen\s*\(", "opens a file. Nothing a candidate needs is on disk: the "
                     "YAML you want to send is a string you wrote, and the "
                     "answer arrives as bytes"),
    # `sys` and `time` are importable for ergonomics; these are the parts of them
    # that are not ergonomics.
    (r"\bsys\s*\.\s*modules\b", "sys.modules is the import table"),
    (r"\bsys\s*\.\s*path\b", "sys.path is where imports come from"),
    (r"\bsys\s*\.\s*(executable|prefix|base_prefix|argv)\b",
     "names a path on this machine rather than a property of the library"),
    (r"\bsys\s*\.\s*_getframe\b", "reaches into the call stack"),
    # Reaching a denied module through an allowed one's namespace.  srbyaml
    # imports os, select, subprocess and sys to do its job; they are not the
    # candidate's to use.
    (r"\bsrbyaml\s*\.\s*(os|sys|select|subprocess|shutil|json)\b",
     "reaches a module through srbyaml's namespace, which is the import check "
     "spelled differently"),
    (r"\bos\s*\.|\bsubprocess\s*\.|\bsocket\s*\.|\bpathlib\s*\.",
     "uses a module that is not on the allow list"),
    (r"\bgetenv\b|\benviron\b|\bputenv\b",
     "reads this process's environment, which describes the harness rather than "
     "the library"),
    # pytest's own accessors for where it is running.
    (r"\b(rootdir|rootpath|invocation_dir|invocation_params|startpath)\b",
     "asks pytest where it was started, which is a path on this machine"),
    (r"\b(pytestconfig|tmp_path|tmpdir|tmp_path_factory)\b",
     "asks pytest for a path; a candidate needs no files, and pytest's config "
     "knows where it was started"),
    (r"\bmonkeypatch\b", "patches the process rather than testing the library"),
    # `Probe.__init__` takes no arguments so that a candidate cannot point it at
    # another executable.  This is the same reach by assignment.
    (r"\.\s*binary\s*=[^=]", "points a Probe at an executable other than the one "
                             "the harness built. Probe() takes no argument for "
                             "the same reason"),
)

REFUSED = 65


def blank_literals(source: str) -> str:
    """`source` with every string literal and comment replaced by spaces.

    Offsets and line numbers are preserved, so a match's position still points at
    the right line of the original.  A tokenizer failure means the file does not
    tokenize; `screen` reports the parse error instead and never gets here.
    """
    lines = source.splitlines(keepends=True)
    out = list(lines)
    kill = {token_mod.STRING, token_mod.COMMENT}
    # FSTRING_MIDDLE is 3.12+; the interior of an f-string is a literal too.
    for name in ("FSTRING_MIDDLE",):
        if hasattr(token_mod, name):
            kill.add(getattr(token_mod, name))

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source  # `screen` has already reported why; fall back to strict

    for tok in tokens:
        if tok.type not in kill:
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        for row in range(r1, r2 + 1):
            line = out[row - 1]
            start = c1 if row == r1 else 0
            end = c2 if row == r2 else len(line)
            # Keep the trailing newline so line numbering survives.
            body = "".join(" " if ch != "\n" else "\n" for ch in line[start:end])
            out[row - 1] = line[:start] + body + line[end:]
    return "".join(out)


def imported_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module a file imports, top-level name only, with its line.

    `import a.b.c` and `from a.b import c` both count as `a`: the allow list is
    about which library a candidate reaches for, and `os.path` is `os`.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name.split(".")[0], node.lineno))
        elif isinstance(node, ast.ImportFrom):
            # `from . import x` has no module; a candidate is a single file with
            # nothing beside it, so a relative import is refused as unresolvable
            # rather than allowed as empty.
            name = (node.module or "").split(".")[0]
            found.append((name or ".", node.lineno))
    return found


def screen(source: str, path: str) -> list[str]:
    """Every reason this candidate is refused.  Empty means it may run."""
    problems: list[str] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        # Not a refusal on scope grounds, but there is no point running it: pytest
        # would report a collection error on both trees, which reads as "fails on
        # the original" and costs the adversary a turn to understand.
        return [f"line {exc.lineno}: the file does not parse: {exc.msg}"]

    for name, line in imported_names(tree):
        if name not in ALLOWED_IMPORTS:
            problems.append(
                f"line {line}: imports {name!r}, which is not on the allow list. "
                f"A candidate sends YAML through srbyaml and asserts on the "
                f"answer; it has no use for the filesystem, another process, the "
                f"network or this process's environment, and each of those is a "
                f"way to learn which tree is running rather than what it does.")

    code = blank_literals(source)
    for pattern, why in FORBIDDEN_CODE:
        match = re.search(pattern, code)
        if match is None:
            continue  # one report per pattern; twenty copies of a rule is noise
        line = code.count("\n", 0, match.start()) + 1
        problems.append(f"line {line}: {match.group(0)!r} {why}.")

    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} CANDIDATE.py", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"cannot read the candidate at {path}: {exc}", file=sys.stderr)
        return 2

    problems = screen(source, str(path))
    if not problems:
        return 0

    print(f"This candidate was not run. It is refused on both trees, so the "
          f"refusal is not evidence about either one.\n", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        f"\nThe rule: a candidate may import {len(ALLOWED_IMPORTS)} standard "
        f"library modules plus srbyaml and pytest, and may not name a harness "
        f"path or variable. It exists because a candidate that identifies the "
        f"tree by reading its surroundings passes on one and fails on the other "
        f"while establishing nothing about the migration. Ask the library a "
        f"question instead -- that is the only thing here that is evidence.",
        file=sys.stderr)
    return REFUSED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
