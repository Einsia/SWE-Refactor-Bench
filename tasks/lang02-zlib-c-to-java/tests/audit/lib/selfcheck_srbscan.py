#!/usr/bin/env python3
"""Build-time self-check for srbscan.find_name.

Not collected by the scan: run-scan.sh points pytest at `modules/<id>`, so a file
in lib/ is never a graded check.  This runs from the Dockerfile instead, beside the
prompt and contract checks, for the reason collect-check.sh gives about itself --
the failure it guards against is silent in the rendered prompt.

What it guards is narrower than "does the matcher work".  `find_name` decides
whether an authored file *mentions* one of nineteen third-party deflate
implementations, and a hit there is a finding of vendoring that feeds the required
`no-foreign-port` gate.  The predicate it replaced was `name.lower() in text`, and
that predicate said `Example.java` mentions `tinflate` -- because "tes|tinflate"
contains it -- so a faithful port of zlib's own `test_inflate` driver, spelled
`testInflate` as Java spells it, was reported as a vendored third-party decoder.

The reason that survived to a live submission is the reason this file exists: no
run before one could show it up.  State A is C and spells the name `test_inflate`,
where the underscore breaks the accidental match, so the identity run passes and
every correct Java port fails.  A case table is the only instrument that sees it,
and a case table I rebuild by hand each time I touch the matcher is one that stops
being run.

Usage:  python3 selfcheck_srbscan.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import srbscan  # noqa: E402


#: (text, name, must_match, why).  Each text is written to a file and searched for
#: `name`.  The `why` is printed on failure, because a bare "case 7 failed" sends
#: the next reader back to reconstruct the argument this table is made of.
CASES: tuple[tuple[str, str, bool, str], ...] = (
    # --- The false positives.  Every one of these is a line a faithful port
    # --- writes, and the old substring predicate reported all of them.
    ("    private static void testInflate(byte[] compr, int comprLen) {",
     "tinflate", False,
     "zlib's own test_inflate, spelled as Java spells it; 'tes|tinflate' contains "
     "the name but does not mention it"),
    ("        testDictInflate(compr, comprLen, uncompr, uncomprLen);",
     "tinflate", False, "the same accident inside a longer camelCase name"),
    ("static void test_inflate(Byte *compr, uLong comprLen)",
     "tinflate", False,
     "State A's spelling: the underscore already broke the match, which is why "
     "no identity run could ever see this"),
    ("    int latestinflateState = 0;",
     "tinflate", False,
     "all-lowercase, no camel hump to break on; the left-boundary lookbehind is "
     "the only thing that rejects it"),
    ("  // fastest inflate path we could manage",
     "tinflate", False, "a comment, matched across the space by neither rule"),

    # --- The true positives.  A matcher tuned until the cases above pass is
    # --- worthless if it stopped seeing these, which is what the gate is for.
    ("import com.jcraft.jzlib.ZStream;", "jzlib", True,
     "the real thing: a jzlib import"),
    ("import com.jcraft.jzlib.Deflater;", "com.jcraft", True,
     "a dotted name, whose parts must not be treated as word breaks to skip"),
    ("class JZlibDeflater implements Deflater {", "jzlib", True,
     "vendoring appears as a prefix, so the right-hand side stays open"),
    ("        return pakoInflateRaw(input);", "pako", True,
     "same, lowercase prefix of a camelCase name"),
    ("    static int tinflate(byte[] in, byte[] out) {", "tinflate", True,
     "the name the false positives are about, standing as a name"),
    ("  n = tinfl_decompress(&decomp, ...);", "tinflate", False,
     "miniz's actual symbol is not this name; `tinfl` is not in the list and "
     "must not be reached by a prefix of a longer entry"),
    ("#define MINIZ_EXPORT", "miniz", True, "case-insensitive"),
    ("  x = obj.zopfliCompress(buf);", "zopfli", True,
     "a method call: `.` is a word break on the left"),
    ("  use_libdeflate = 1;", "libdeflate", True,
     "an underscore is a word break in snake_case, so this is a mention"),
    ("  int useTinflate = 1;", "tinflate", True,
     "the camelCase break is what makes this a mention rather than a fragment; "
     "without it the lookbehind would reject a real hit"),
)


def check_cases(tmp: Path) -> list[str]:
    bad: list[str] = []
    for index, (text, name, want, why) in enumerate(CASES, 1):
        path = tmp / f"case{index}.java"
        path.write_text(text + "\n", encoding="utf-8")
        got = srbscan.find_name(path, name)
        if bool(got) is not want:
            verb = "did not match" if want else "matched"
            bad.append(f"case {index}: find_name(..., {name!r}) {verb}\n"
                       f"    text: {text.strip()}\n"
                       f"    why:  {why}")
            continue
        if got and got[0] != 1:
            bad.append(f"case {index}: matched at line {got[0]}, not 1")
        if got and not got[1]:
            bad.append(f"case {index}: matched but quoted nothing; a finding that "
                       f"cites no line is what rendered as line 0")
    return bad


#: (text, needle, want_line, want_quote_contains).  `find_text`'s contract is that
#: a hit carries the *line*, never the needle: the finding it feeds is read by a
#: reviewer deciding no-jdk-deflate, and `('PORTING.md', 4, 'java.util.zip', ...)`
#: is the same rendering for an import and for a comment saying the port avoids the
#: import.  Both are shapes a correct submission for this task writes.
TEXT_CASES: tuple[tuple[str, str, int, str], ...] = (
    ("import java.util.zip.Deflater;", "java.util.zip", 1, "import"),
    ("// java.util.zip.Deflater is deliberately not used here",
     "java.util.zip", 1, "deliberately not used"),
    ("line one\nline two\n  path = /logs/verifier/out\n", "/logs/verifier", 3,
     "path ="),
    ("no mention at all\n", "java.util.zip", 0, ""),
    # Case-sensitive, because the predicates this replaced were.  Changing which
    # findings there are was never the point; only what they show.
    ("import JAVA.UTIL.ZIP.Deflater;", "java.util.zip", 0, ""),
)


def check_text_cases(tmp: Path) -> list[str]:
    bad: list[str] = []
    for index, (text, needle, want_line, want_in) in enumerate(TEXT_CASES, 1):
        path = tmp / f"text{index}.txt"
        path.write_text(text if text.endswith("\n") else text + "\n",
                        encoding="utf-8")
        got = srbscan.find_text(path, needle)
        if want_line == 0:
            if got is not None:
                bad.append(f"text case {index}: find_text(..., {needle!r}) matched "
                           f"{got!r} in {text.strip()!r}, expected no hit")
            continue
        if got is None:
            bad.append(f"text case {index}: find_text(..., {needle!r}) found nothing "
                       f"in {text.strip()!r}")
            continue
        line, quote = got
        if line != want_line:
            bad.append(f"text case {index}: line {line}, expected {want_line}")
        if quote == needle:
            bad.append(f"text case {index}: the quote is the needle itself "
                       f"({needle!r}); a reviewer cannot tell an import from a "
                       f"comment saying the import was avoided")
        if want_in and want_in not in quote:
            bad.append(f"text case {index}: quote {quote!r} does not contain "
                       f"{want_in!r}, so it is not the line that matched")
    return bad


def check_authored(tmp: Path) -> list[str]:
    """`authored`'s three behaviours, including the one that is deliberately open.

    The filter every text search runs through, so what it drops decides which findings
    exist.  Two of these are the contract; the third is a documented fail-open, and it
    is here precisely because it is the kind of thing a later reader tidies into a
    guard.  All three ways of guarding it damage the reviewer's prompt more than the
    noise does -- a skip becomes 42 identical converted failures, a refusal before
    pytest renders as "(the scan produced no findings)", and returning nothing makes
    the searches pass having looked at nothing.  What covers it instead is
    `closure.test_both_trees_are_mounted`, which fails on an empty mount and names the
    cause, and that is the whole of the protection: a review answers pass or fail, so
    an empty mount produces a scored run rather than a refused one.  This case stays
    as it is, and stays here, because it is the tripwire for a later reader who tidies
    the fail-open away without knowing which layer is load-bearing.
    """
    bad: list[str] = []
    repo, orig = tmp / "repo", tmp / "orig"
    for d in (repo, orig):
        (d / "sub").mkdir(parents=True, exist_ok=True)
    (repo / "README").write_text("upstream text, names miniz\n", encoding="utf-8")
    (orig / "README").write_text("upstream text, names miniz\n", encoding="utf-8")
    (repo / "ChangeLog").write_text("edited by the port\n", encoding="utf-8")
    (orig / "ChangeLog").write_text("upstream ChangeLog\n", encoding="utf-8")
    (repo / "sub" / "New.java").write_text("class New {}\n", encoding="utf-8")

    paths = [repo / "README", repo / "ChangeLog", repo / "sub" / "New.java"]
    saved = (srbscan.REPO, srbscan.ORIGINAL)
    try:
        srbscan.REPO, srbscan.ORIGINAL = repo, orig
        got = {p.name for p in srbscan.authored(paths)}
        if "README" in got:
            bad.append("authored kept a file byte-identical to State A's copy; "
                       "every preserved README/ChangeLog/RFC then produces the "
                       "same finding on every honest submission")
        for name in ("ChangeLog", "New.java"):
            if name not in got:
                bad.append(f"authored dropped {name!r}, which differs from State A "
                           f"(or has no counterpart there); an edit to a preserved "
                           f"file is authored text and must stay in scope")

        srbscan.ORIGINAL = tmp / "absent"
        (tmp / "absent").mkdir(exist_ok=True)
        if len(srbscan.authored(paths)) != len(paths):
            bad.append(
                "authored no longer returns every path when ORIGINAL is empty. "
                "That is a deliberate fail-open, not an oversight -- see its "
                "docstring for the three guards that were measured and rejected. "
                "If you are replacing it, the thing to verify is what the "
                "reviewer's {{findings}} section says, not what this function "
                "returns.")
    finally:
        srbscan.REPO, srbscan.ORIGINAL = saved
    return bad


def check_every_name_matches_itself(tmp: Path) -> tuple[list[str], tuple[str, ...]]:
    """Every graded name must be findable when it stands alone.

    Read from the module that grades, not re-listed here, so the two cannot drift.
    This is the guard against the next name added to that tuple: `find_name`
    treats a camelCase boundary in the *text* as a word break, and a name
    carrying one of its own -- `jZlib`, say -- would be split by the same
    substitution and never match anything.  Reasoning about that at review time is
    how it gets missed; failing the build is not.
    """
    bad: list[str] = []
    sys.path.insert(0, str(Path(srbscan.__file__).resolve().parent.parent))
    try:
        from modules.provenance.test_provenance import FOREIGN_IMPLEMENTATIONS
    except Exception as exc:  # pragma: no cover - the build reports it
        # Returning an empty name list beside the failure, rather than the failure
        # alone: this function's two return paths have to have the same shape, or
        # the caller unpacking them raises and the build fails with a TypeError
        # naming this file instead of the problem it found.
        return ([f"cannot read FOREIGN_IMPLEMENTATIONS from the provenance module, "
                 f"so this check would silently verify nothing: {exc!r}"], ())
    if len(FOREIGN_IMPLEMENTATIONS) < 10:
        bad.append(f"FOREIGN_IMPLEMENTATIONS has only "
                   f"{len(FOREIGN_IMPLEMENTATIONS)} entries; the list this check "
                   f"reasons over looks truncated")
    for name in FOREIGN_IMPLEMENTATIONS:
        path = tmp / "alone.txt"
        path.write_text(f"a mention of {name} here\n", encoding="utf-8")
        if not srbscan.find_name(path, name):
            bad.append(f"{name!r} does not match itself standing alone; the check "
                       f"that greps for it can never fire")
    return bad, tuple(FOREIGN_IMPLEMENTATIONS)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="selfcheck-srbscan-") as raw:
        tmp = Path(raw)
        bad = check_cases(tmp)
        bad += check_text_cases(tmp)
        bad += check_authored(tmp)
        more, names = check_every_name_matches_itself(tmp)
        bad += more

    if bad:
        print(f"selfcheck_srbscan: {len(bad)} case(s) wrong:", file=sys.stderr)
        for line in bad:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print(f"selfcheck_srbscan: ok, {len(CASES)} find_name + {len(TEXT_CASES)} "
          f"find_text cases, authored's 3 (drop identical, keep edited, stay open "
          f"on an empty mount), and {len(names)} graded names each match themselves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
