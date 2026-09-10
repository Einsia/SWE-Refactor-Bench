#!/usr/bin/env python3
"""The vocabulary shared by both halves of the probe pair.

The reference probe is Python and the submission probe is Go.  Anything both of
them need to agree on -- which format options a preset means, which filter a
name refers to, what a "stack" is -- lives here and is serialized into the frozen
assets as spec.json.  The Go probes read that file at run time.

The alternative would be to write these tables twice, once per language.  They
would then drift, and the failure mode of drift is the worst kind: the Go side
would be graded against options the Python side never applied, and the diff
would look like a porting bug in the submission.

Option values carry an explicit type tag.  Python distinguishes int from bool
from str, JSON does not distinguish int from float, and Go's decoder turns every
JSON number into float64.  A tagged encoding means both sides reconstruct exactly
the value the reference's validate_options was written against -- which matters,
because several of its error messages exist only to complain about a type.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Option encoding
# --------------------------------------------------------------------------
# ("i", 4) -> int 4        ("s", "upper") -> "upper"
# ("b", True) -> True      ("n", None) -> None / nil
# ("f", 1.5) -> float 1.5


def I(v: int):  # noqa: E743
    return ["i", v]


def S(v: str):
    return ["s", v]


def B(v: bool):
    return ["b", v]


def N():
    return ["n", None]


def F(v: float):
    return ["f", v]


def decode_option(tagged) -> object:
    """The Python side of the tagged encoding."""
    tag, value = tagged
    if tag == "i":
        return int(value)
    if tag == "s":
        return str(value)
    if tag == "b":
        return bool(value)
    if tag == "n":
        return None
    if tag == "f":
        return float(value)
    raise ValueError(f"unknown option tag {tag!r}")


def decode_options(mapping: dict) -> dict:
    return {k: decode_option(v) for k, v in mapping.items()}


# --------------------------------------------------------------------------
# Format presets
# --------------------------------------------------------------------------
# Each preset is one option map.  The format family applies every preset to
# every format-target document, so this list is the multiplier: 30 presets over
# 36 documents is 1080 cases, and each preset was chosen because it reaches a
# different branch of the formatter or of the reindent filter.

FORMAT_PRESETS: dict[str, dict] = {
    # identity: no options at all.  A submission that ignores options entirely
    # still has to pass this one, and it is the cheapest possible smoke test.
    "identity": {},

    # single-option cases: each isolates one filter.
    "kw-upper": {"keyword_case": S("upper")},
    "kw-lower": {"keyword_case": S("lower")},
    "kw-capitalize": {"keyword_case": S("capitalize")},
    "id-upper": {"identifier_case": S("upper")},
    "id-lower": {"identifier_case": S("lower")},
    "id-capitalize": {"identifier_case": S("capitalize")},
    "strip-comments": {"strip_comments": B(True)},
    "strip-ws": {"strip_whitespace": B(True)},
    "use-space-around-operators": {"use_space_around_operators": B(True)},
    "truncate-strings": {"truncate_strings": I(6)},
    "truncate-strings-char": {"truncate_strings": I(4), "truncate_char": S("*")},
    "right-margin": {"right_margin": I(30)},
    "out-python": {"output_format": S("python")},
    "out-python-var": {"output_format": S("python"), "reindent": B(True)},
    "out-php": {"output_format": S("php")},

    # the reindent filter, which is where most of the formatter's complexity is.
    "reindent": {"reindent": B(True)},
    "reindent-w4": {"reindent": B(True), "indent_width": I(4)},
    "reindent-w8": {"reindent": B(True), "indent_width": I(8)},
    "reindent-w1": {"reindent": B(True), "indent_width": I(1)},
    "reindent-tabs": {"reindent": B(True), "indent_tabs": B(True)},
    "reindent-char": {"reindent": B(True), "indent_char": S(".")},
    "reindent-comma-first": {"reindent": B(True), "comma_first": B(True)},
    "reindent-after-first": {"reindent": B(True), "indent_after_first": B(True)},
    "reindent-columns": {"reindent": B(True), "indent_columns": B(True)},
    "reindent-compact": {"reindent": B(True), "compact": B(True)},
    "reindent-wrap2": {"reindent": B(True), "wrap_after": I(2)},
    "reindent-wrap40": {"reindent": B(True), "wrap_after": I(40)},
    "reindent-strip-comments": {"reindent": B(True), "strip_comments": B(True)},
    "reindent-kw-upper": {"reindent": B(True), "keyword_case": S("upper")},
    "reindent-margin": {"reindent": B(True), "right_margin": I(40)},

    # the aligned-indent filter, a separate code path from reindent.
    "aligned": {"reindent_aligned": B(True)},
    "aligned-kw-upper": {"reindent_aligned": B(True), "keyword_case": S("upper")},
    "aligned-strip-comments": {"reindent_aligned": B(True), "strip_comments": B(True)},

    # combinations, because the filters compose in a fixed order and the order
    # is observable.
    "pretty": {
        "reindent": B(True),
        "keyword_case": S("upper"),
        "identifier_case": S("lower"),
        "strip_comments": B(True),
        "indent_width": I(4),
    },
    "everything": {
        "reindent": B(True),
        "keyword_case": S("upper"),
        "identifier_case": S("lower"),
        "strip_comments": B(True),
        "strip_whitespace": B(True),
        "use_space_around_operators": B(True),
        "indent_width": I(2),
        "wrap_after": I(60),
        "comma_first": B(True),
        "truncate_strings": I(10),
    },
    "explicit-false": {
        # Every boolean explicitly off.  The reference treats absent and False
        # the same here, and a submission that conflates "present" with "true"
        # fails this and nothing else.
        "reindent": B(False),
        "strip_comments": B(False),
        "strip_whitespace": B(False),
        "use_space_around_operators": B(False),
        "comma_first": B(False),
        "indent_tabs": B(False),
        "compact": B(False),
    },
    # Truncation with a multi-byte replacement, over inputs that contain
    # multi-byte text.  Python counts code points and Go indexes bytes, so this
    # is the preset a port that reached for len(s) on a Go string fails.
    "truncate-unicode": {"truncate_strings": I(3), "truncate_char": S("…")},
}

# --------------------------------------------------------------------------
# validate_options cases
# --------------------------------------------------------------------------
# Two halves.  The invalid ones are graded on the error message, verbatim: those
# strings are the reference's API and a caller catches them.  The valid ones are
# graded on the fact that no error was raised -- the normalized map itself is
# full of Python-side objects that have no portable rendering.

VALIDATE_CASES: dict[str, dict] = {
    "ok-empty": {},
    "ok-kw-upper": {"keyword_case": S("upper")},
    "ok-kw-lower": {"keyword_case": S("lower")},
    "ok-kw-capitalize": {"keyword_case": S("capitalize")},
    "ok-id-upper": {"identifier_case": S("upper")},
    "ok-reindent": {"reindent": B(True)},
    "ok-reindent-width": {"reindent": B(True), "indent_width": I(4)},
    "ok-wrap-after": {"reindent": B(True), "wrap_after": I(10)},
    "ok-right-margin": {"right_margin": I(20)},
    "ok-out-python": {"output_format": S("python")},
    "ok-out-php": {"output_format": S("php")},
    "ok-truncate": {"truncate_strings": I(5)},
    "ok-truncate-char": {"truncate_strings": I(5), "truncate_char": S("~")},
    "ok-aligned": {"reindent_aligned": B(True)},
    "ok-indent-tabs": {"reindent": B(True), "indent_tabs": B(True)},
    "ok-indent-char": {"reindent": B(True), "indent_char": S("_")},
    "ok-compact": {"reindent": B(True), "compact": B(True)},
    "ok-columns": {"reindent": B(True), "indent_columns": B(True)},
    "ok-none-values": {"keyword_case": N(), "identifier_case": N()},
    "ok-strip-both": {"strip_comments": B(True), "strip_whitespace": B(True)},
    "ok-spaces-operators": {"use_space_around_operators": B(True)},
    "ok-comma-first": {"reindent": B(True), "comma_first": B(True)},
    "ok-after-first": {"reindent": B(True), "indent_after_first": B(True)},

    "bad-kw-case": {"keyword_case": S("bogus")},
    "bad-kw-case-empty": {"keyword_case": S("")},
    "bad-kw-case-upper-caps": {"keyword_case": S("UPPER")},
    "bad-id-case": {"identifier_case": S("bogus")},
    "bad-out-format": {"output_format": S("ruby")},
    "bad-out-format-caps": {"output_format": S("PYTHON")},
    "bad-truncate-int": {"truncate_strings": I(1)},
    "bad-truncate-str": {"truncate_strings": S("5")},
    "bad-truncate-zero": {"truncate_strings": I(0)},
    "bad-truncate-neg": {"truncate_strings": I(-1)},
    "bad-indent-width-str": {"reindent": B(True), "indent_width": S("x")},
    "bad-indent-width-zero": {"reindent": B(True), "indent_width": I(0)},
    "bad-indent-width-neg": {"reindent": B(True), "indent_width": I(-2)},
    "bad-wrap-after-neg": {"reindent": B(True), "wrap_after": I(-1)},
    "bad-wrap-after-str": {"reindent": B(True), "wrap_after": S("10")},
    "bad-right-margin-small": {"right_margin": I(5)},
    "bad-right-margin-ten": {"right_margin": I(10)},
    "bad-right-margin-str": {"right_margin": S("80")},
    "bad-strip-comments-str": {"strip_comments": S("yes")},
    "bad-strip-ws-str": {"strip_whitespace": S("yes")},
    "bad-comma-first-str": {"reindent": B(True), "comma_first": S("yes")},
    "bad-compact-str": {"reindent": B(True), "compact": S("yes")},
    "bad-columns-str": {"reindent": B(True), "indent_columns": S("yes")},
    "bad-after-first-str": {"reindent": B(True), "indent_after_first": S("yes")},
    "bad-spaces-operators-str": {"use_space_around_operators": S("yes")},
    "bad-indent-tabs-str": {"reindent": B(True), "indent_tabs": S("yes")},
    "bad-truncate-char-int": {"truncate_strings": I(5), "truncate_char": I(1)},
}

# --------------------------------------------------------------------------
# Individual filters
# --------------------------------------------------------------------------
# name -> (constructor, args, stage).  The stage matters: the reference's
# FilterStack runs preprocess on the raw stream, stmtprocess on each grouped
# statement and postprocess after, and a filter installed in the wrong stage
# produces different output rather than an error.

# Each entry names the filter twice, because the two languages construct it
# differently and neither naming is wrong:
#
#   py:   the reference class in sqlparse.filters, plus keyword arguments
#   go:   the contracted constructor in the submission's filters package
#
# The `params` map is the single source of the values; each side reads the ones
# its own constructor takes.  Python's ReindentFilter takes eight keyword
# arguments and Go's NewReindent takes one ReindentConfig struct, so a shared
# "args" list could not have described both.

FILTERS: dict[str, dict] = {
    "kwcase-upper": {
        "py": "KeywordCaseFilter", "go": "NewKeywordCase",
        "params": {"case": S("upper")}, "stage": "pre"},
    "kwcase-lower": {
        "py": "KeywordCaseFilter", "go": "NewKeywordCase",
        "params": {"case": S("lower")}, "stage": "pre"},
    "kwcase-capitalize": {
        "py": "KeywordCaseFilter", "go": "NewKeywordCase",
        "params": {"case": S("capitalize")}, "stage": "pre"},
    "idcase-upper": {
        "py": "IdentifierCaseFilter", "go": "NewIdentifierCase",
        "params": {"case": S("upper")}, "stage": "pre"},
    "idcase-lower": {
        "py": "IdentifierCaseFilter", "go": "NewIdentifierCase",
        "params": {"case": S("lower")}, "stage": "pre"},
    "idcase-capitalize": {
        "py": "IdentifierCaseFilter", "go": "NewIdentifierCase",
        "params": {"case": S("capitalize")}, "stage": "pre"},
    "truncate-4": {
        "py": "TruncateStringFilter", "go": "NewTruncateStrings",
        "params": {"width": I(4), "char": S("[...]")}, "stage": "pre"},
    "truncate-2-star": {
        "py": "TruncateStringFilter", "go": "NewTruncateStrings",
        "params": {"width": I(2), "char": S("*")}, "stage": "pre"},
    "truncate-3-unicode": {
        "py": "TruncateStringFilter", "go": "NewTruncateStrings",
        "params": {"width": I(3), "char": S("…")}, "stage": "pre"},
    "strip-comments": {
        "py": "StripCommentsFilter", "go": "NewStripComments",
        "params": {}, "stage": "stmt"},
    "strip-whitespace": {
        "py": "StripWhitespaceFilter", "go": "NewStripWhitespace",
        "params": {}, "stage": "stmt"},
    "strip-semicolon": {
        "py": "StripTrailingSemicolonFilter", "go": "NewStripTrailingSemicolon",
        "params": {}, "stage": "stmt"},
    "spaces-operators": {
        "py": "SpacesAroundOperatorsFilter", "go": "NewSpacesAroundOperators",
        "params": {}, "stage": "stmt"},
    "reindent-default": {
        "py": "ReindentFilter", "go": "NewReindent", "params": {}, "stage": "stmt"},
    "reindent-w4": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"width": I(4)}, "stage": "stmt"},
    "reindent-w8": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"width": I(8)}, "stage": "stmt"},
    "reindent-tab": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"char": S("\t"), "width": I(1)}, "stage": "stmt"},
    "reindent-comma-first": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"comma_first": B(True)}, "stage": "stmt"},
    "reindent-compact": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"compact": B(True)}, "stage": "stmt"},
    "reindent-wrap5": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"wrap_after": I(5)}, "stage": "stmt"},
    "reindent-columns": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"indent_columns": B(True)}, "stage": "stmt"},
    "reindent-after-first": {
        "py": "ReindentFilter", "go": "NewReindent",
        "params": {"indent_after_first": B(True)}, "stage": "stmt"},
    "aligned-default": {
        "py": "AlignedIndentFilter", "go": "NewAlignedIndent",
        "params": {"char": S(" "), "n": S("\n")}, "stage": "stmt"},
    "aligned-dot": {
        "py": "AlignedIndentFilter", "go": "NewAlignedIndent",
        "params": {"char": S("."), "n": S("\n")}, "stage": "stmt"},
    "right-margin-20": {
        "py": "RightMarginFilter", "go": "NewRightMargin",
        "params": {"width": I(20)}, "stage": "stmt"},
    "right-margin-40": {
        "py": "RightMarginFilter", "go": "NewRightMargin",
        "params": {"width": I(40)}, "stage": "stmt"},
    "serializer": {
        "py": "SerializerUnicode", "go": "NewSerializerUnicode",
        "params": {}, "stage": "post"},
    "out-python": {
        "py": "OutputPythonFilter", "go": "NewOutputPython",
        "params": {"varname": S("sql")}, "stage": "post"},
    "out-python-var": {
        "py": "OutputPythonFilter", "go": "NewOutputPython",
        "params": {"varname": S("query")}, "stage": "post"},
    "out-php": {
        "py": "OutputPHPFilter", "go": "NewOutputPHP",
        "params": {"varname": S("sql")}, "stage": "post"},
    "out-php-var": {
        "py": "OutputPHPFilter", "go": "NewOutputPHP",
        "params": {"varname": S("q")}, "stage": "post"},
}

# --------------------------------------------------------------------------
# Filter-stack shapes
# --------------------------------------------------------------------------
# The stack is the thing sqlparse.parse and sqlparse.format both go through, and
# a submission can get every individual filter right and still assemble them
# wrongly.  Each shape names its grouping setting, its strip_semicolon setting
# and its filter list.

STACKS: dict[str, dict] = {
    "bare": {"grouping": False, "strip_semicolon": False, "filters": []},
    "grouped": {"grouping": True, "strip_semicolon": False, "filters": []},
    "grouped-strip-semi": {"grouping": True, "strip_semicolon": True,
                           "filters": []},
    "bare-strip-semi": {"grouping": False, "strip_semicolon": True,
                        "filters": []},
    "grouped-serializer": {"grouping": True, "strip_semicolon": False,
                           "filters": ["serializer"]},
    "grouped-reindent": {"grouping": True, "strip_semicolon": False,
                         "filters": ["reindent-default", "serializer"]},
    "grouped-kwcase-reindent": {
        "grouping": True, "strip_semicolon": False,
        "filters": ["kwcase-upper", "reindent-default", "serializer"],
    },
    "grouped-strip-comments-reindent": {
        "grouping": True, "strip_semicolon": False,
        "filters": ["strip-comments", "reindent-default", "serializer"],
    },
    "bare-kwcase": {"grouping": False, "strip_semicolon": False,
                    "filters": ["kwcase-lower", "serializer"]},
    "grouped-aligned": {"grouping": True, "strip_semicolon": False,
                        "filters": ["aligned-default", "serializer"]},
    "grouped-out-python": {"grouping": True, "strip_semicolon": False,
                           "filters": ["out-python"]},
    "grouped-full": {
        "grouping": True, "strip_semicolon": True,
        "filters": ["kwcase-upper", "idcase-lower", "strip-comments",
                    "reindent-default", "serializer"],
    },
}

# --------------------------------------------------------------------------
# utils inputs
# --------------------------------------------------------------------------

# The empty string is deliberately absent: remove_quotes("") raises IndexError
# in the reference, and the contracted Go signature -- RemoveQuotes(string) string
# -- has no way to raise.  Grading it would be grading an impossibility rather
# than a port.  Recorded under not_graded in the behavioral contract.  Note also
# that [a] is *not* unquoted: the reference strips only ' " and `.
REMOVE_QUOTES_INPUTS = [
    "a", "'a'", '"a"', "`a`", "[a]", "'a", "a'", "''", '""', "'*'",
    "'it''s'", "'a\"b'", '"a\'b"', "'a b'", "'  '", "'a'b'", "\"'a'\"",
    "'ünïcödé'", "'中文'", "'a\\'b'", "'\n'", "'\t'", "[a b]", "`a b`",
    "'multi\nline'", "not quoted", "'unclosed", "$$a$$", "'a'::text",
    "'", '"', "`", "'''", "`a'", "'a`", "'中'", "中", "'\x00'",
]
# The separator inputs stop here.  A family isolating ReindentFilter's column
# arithmetic -- Python's \v \f \x1c \x1d \x1e \x85 \u2028 \u2029 against
# strings.Split's \n, and code points against bytes -- would need the contract
# to export utils.LastLineWidth, and the reference has no such export:
# _get_offset is a private method on the filter.  Nor is the behavior reachable
# through one that exists: neither shortcut changes a byte of format() output on
# any input found, because reindent inserts its newline before measuring, so the
# measured line is always indentation whitespace or a short ASCII prefix.

SPLIT_UNQUOTED_NEWLINES_INPUTS = [
    "", "a", "a\nb", "a\nb\nc", "\n", "\n\n", "a\n", "\na",
    "'a\nb'", '"a\nb"', "a'b\nc'd", "select 'a\nb' from t",
    "select a\nfrom 'x\ny'", "'unclosed\nnewline", "a\r\nb",
    "select '\n' from t", "$$a\nb$$", "a\n'b\nc'\nd",
    "'a''b\nc'", "select 1\n; select 2", "-- c\nselect 1",
    "/* a\nb */ select 1",
    # LINE_MATCH is `(\r\n|\r|\n)` and the list above exercised two of its three
    # alternatives: "a\r\nb" covers CRLF and eleven inputs cover LF.  Deleting the
    # bare `\r` alternative changed nothing here, because CRLF still matched -- so
    # the family that probes this function directly could not see the one rule only
    # it can see.  A bare CR is a real line ending (classic Mac, and what a lone
    # \r in a heredoc produces), and a port splitting on "\n" alone or using
    # bufio.ScanLines gets it wrong in exactly this way.
    "a\rb",              # the alternative itself
    "a\r\nb\rc\nd",      # all three in one input, so order of alternation matters
    "'a\rb'",            # inside quotes: not a break, same as \n
    "a\r",               # trailing
    "\ra",               # leading
]

# --------------------------------------------------------------------------
# Encoding gate
# --------------------------------------------------------------------------
# The reference accepts every codec CPython registers.  The port cannot: Go's
# standard library has UTF-8 and UTF-16 and nothing else, and golang.org/x/text
# is outside the stdlib-only closure.  So the supported set is declared, and the
# reference is driven through the same gate as the submission -- otherwise the
# differential would be grading CPython's codec registry rather than a port.
#
# ENCODING_ALIASES is the whole normalizer: strip, lower, then this table.  It is
# a fixed enumeration rather than a reimplementation of CPython's alias
# machinery, so both sides can agree exactly.  A name that is not a key is
# unsupported, and both sides must error.

ENCODING_ALIASES: dict[str, str] = {
    "utf-8": "utf-8", "utf8": "utf-8", "utf_8": "utf-8", "u8": "utf-8",
    "utf": "utf-8", "utf-8-sig": "utf-8-sig", "utf_8_sig": "utf-8-sig",

    "ascii": "ascii", "us-ascii": "ascii", "us_ascii": "ascii",
    "646": "ascii", "ansi_x3.4-1968": "ascii",

    "latin-1": "latin-1", "latin1": "latin-1", "latin": "latin-1",
    "iso8859-1": "latin-1", "iso-8859-1": "latin-1", "iso_8859-1": "latin-1",
    "8859": "latin-1", "cp819": "latin-1", "l1": "latin-1",

    "cp1251": "cp1251", "windows-1251": "cp1251", "windows_1251": "cp1251",
    "1251": "cp1251",

    "utf-16": "utf-16", "utf16": "utf-16", "utf_16": "utf-16", "u16": "utf-16",
    "utf-16-le": "utf-16-le", "utf-16le": "utf-16-le",
    "utf_16_le": "utf-16-le", "utf16-le": "utf-16-le",
    "utf-16-be": "utf-16-be", "utf-16be": "utf-16-be",
    "utf_16_be": "utf-16-be", "utf16-be": "utf-16-be",
}

# The canonical names the aliases resolve to, and the CPython codec each one is
# implemented by in the reference.  utf-8-sig is in the table because a caller
# can ask for it by name; the BOM-stripping behavior differs from utf-8 and is
# graded.
ENCODING_CANONICAL: dict[str, str] = {
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8-sig",
    "ascii": "ascii",
    "latin-1": "iso8859-1",
    "cp1251": "cp1251",
    "utf-16": "utf-16",
    "utf-16-le": "utf-16-le",
    "utf-16-be": "utf-16-be",
}


def normalize_encoding(name: str) -> str:
    """The whole gate.  Returns the canonical name, or "" if unsupported."""
    return ENCODING_ALIASES.get(name.strip().lower(), "")


# --------------------------------------------------------------------------
# Lexer state scripts
# --------------------------------------------------------------------------
# The reference's Lexer is a process-wide singleton with a mutable keyword set
# and a documented add / clear / default_initialization protocol.  Each script
# below is a sequence of steps; the probe reports the result of every observing
# step, so the whole trajectory is graded rather than just the end state.
#
# Steps:
#   ["clear"]                        drop every keyword table and the regex set
#   ["default-init"]                 restore the shipped tables
#   ["add", {"WORD": "Token.Type"}]  add_keywords
#   ["lex", "sql"]                   observe: the flat token stream
#   ["is-keyword", "word"]           observe: the (ttype, value) lookup
#
# Every script must leave the singleton in its default state; the probe
# re-initializes after each script regardless, because a script that leaked
# state would silently corrupt every case that ran after it in the same batch.
#
# Two behaviors these scripts pin down, both surprising and both real:
#   - after clear(), the lexer has no SQL_REGEX at all, so every character comes
#     back as Token.Error individually -- not one Error token for the whole input
#   - after clear(), is_keyword() still answers, falling back to Token.Name
#   - add_keywords({"SELECT": ...}) does NOT change how `select` tokenizes,
#     because SQL_REGEX matches it before the keyword tables are consulted; the
#     keyword tables only decide words the regex resolved to Name

LEXSTATE_SCRIPTS: dict[str, list] = {
    "default": [
        ["lex", "select a from t"],
    ],
    "clear-then-lex": [
        ["clear"],
        ["lex", "select a"],
    ],
    "clear-then-default": [
        ["clear"],
        ["lex", "select a"],
        ["default-init"],
        ["lex", "select a"],
    ],
    "add-custom": [
        ["add", {"MYVERB": "Token.Keyword.DML"}],
        ["lex", "myverb a from t"],
        ["is-keyword", "myverb"],
        ["is-keyword", "MYVERB"],
    ],
    "add-then-clear": [
        ["add", {"MYVERB": "Token.Keyword.DML"}],
        ["lex", "myverb a"],
        ["clear"],
        ["lex", "myverb a"],
        ["is-keyword", "myverb"],
    ],
    "add-twice": [
        ["add", {"MYVERB": "Token.Keyword.DML"}],
        ["add", {"MYVERB": "Token.Name.Builtin"}],
        ["lex", "myverb a"],
        ["is-keyword", "myverb"],
    ],
    "add-overrides-builtin": [
        # The quirk: SELECT is matched by SQL_REGEX before any table lookup, so
        # this add is invisible to the lexer while being visible to is_keyword.
        ["add", {"SELECT": "Token.Name.Builtin"}],
        ["lex", "select a"],
        ["is-keyword", "select"],
    ],
    "add-lowercase-key": [
        # The tables are keyed by upper-case; a lower-case key never matches.
        ["add", {"myverb": "Token.Keyword.DML"}],
        ["lex", "myverb a"],
        ["is-keyword", "myverb"],
    ],
    "default-idempotent": [
        ["default-init"],
        ["default-init"],
        ["lex", "select a"],
    ],
    "clear-idempotent": [
        ["clear"],
        ["clear"],
        ["lex", "select a"],
        ["default-init"],
        ["lex", "select a"],
    ],
    "add-empty-table": [
        ["add", {}],
        ["lex", "select a"],
    ],
    "add-then-reinit": [
        ["add", {"MYVERB": "Token.Keyword.DML"}],
        ["lex", "myverb a"],
        ["default-init"],
        ["lex", "myverb a"],
        ["is-keyword", "myverb"],
    ],
    "is-keyword-after-clear": [
        ["clear"],
        ["is-keyword", "select"],
        ["is-keyword", "zzz"],
        ["is-keyword", ""],
        ["default-init"],
        ["is-keyword", "select"],
    ],
    "add-many": [
        ["add", {"ALPHA": "Token.Keyword", "BETA": "Token.Keyword.DDL",
                 "GAMMA": "Token.Name.Builtin", "DELTA": "Token.Operator"}],
        ["lex", "alpha beta gamma delta"],
        ["is-keyword", "alpha"],
        ["is-keyword", "beta"],
        ["is-keyword", "gamma"],
        ["is-keyword", "delta"],
    ],
}

# --------------------------------------------------------------------------
# token-type probes
# --------------------------------------------------------------------------
# Rendered names.  The lattice cases resolve each of these and report its
# rendering, its parent, and its containment relation against every other name
# in the list -- which is a 40x40 relation per case and exactly what a hand-
# written Go type hierarchy gets wrong.

TTYPE_NAMES = [
    "Token",
    "Token.Text", "Token.Text.Whitespace", "Token.Text.Whitespace.Newline",
    "Token.Error",
    "Token.Other",
    "Token.Punctuation",
    "Token.Operator", "Token.Operator.Comparison",
    "Token.Wildcard",
    "Token.Comment", "Token.Comment.Single", "Token.Comment.Multiline",
    # The hint types live one level deeper than a reader would guess: the
    # reference has no Token.Comment.Hint, it has a Hint under each of the two
    # comment flavours, because a hint is recognized by its comment syntax.
    "Token.Comment.Single.Hint", "Token.Comment.Multiline.Hint",
    "Token.Keyword", "Token.Keyword.DML", "Token.Keyword.DDL",
    "Token.Keyword.CTE", "Token.Keyword.DCL", "Token.Keyword.TZCast",
    "Token.Keyword.Order",
    "Token.Name", "Token.Name.Builtin", "Token.Name.Placeholder",
    "Token.Literal", "Token.Literal.String", "Token.Literal.String.Single",
    "Token.Literal.String.Symbol", "Token.Literal.Number",
    "Token.Literal.Number.Integer", "Token.Literal.Number.Float",
    "Token.Literal.Number.Hexadecimal",
    "Token.Generic", "Token.Generic.Command",
    "Token.Assignment",
    # Aliases: the reference publishes Token.String and Token.Number as second
    # names for types that live under Literal.  They render as their canonical
    # name, so a port that treats them as distinct types fails these two cases
    # and nothing else.
    "Token.String", "Token.Number",
    "Token.Nonexistent", "Token.Keyword.Nonexistent",
]

# The two names the reference never emits.  They are probed anyway, and they are
# not a trick: attribute access on the reference's token type CREATES the child,
# so `Token.Nonexistent` is a real type the moment anyone names it, contained in
# Token and containing only itself.  The contracted Go equivalent is
# TokenType.Sub, which interns the same way, so both halves must answer that a
# well-formed name always resolves.  Naming them here keeps that deliberate:
# a reader who assumes these should fail to resolve is looking at the one place
# that says otherwise.
TTYPE_NONEXISTENT = ["Token.Nonexistent", "Token.Keyword.Nonexistent"]

# --------------------------------------------------------------------------
# node accessor scope
# --------------------------------------------------------------------------
# Which accessors a node answers, as a function of its kind alone.
#
# The reference spreads its read surface over a class hierarchy: `get_type` is
# defined on Statement, `left`/`right` on Comparison, `get_parameters` on
# Function, and eleven more on TokenList, which Token does not inherit.  Ask a
# node for an accessor its class does not define and Python raises
# AttributeError.  That is not an implementation detail -- it is the published
# shape of the API, and a port that answers everything everywhere has not
# reproduced it.
#
# The contracted Go type is a single *Node on which every method is always
# callable, so the Go half cannot discover this by asking.  It has to be told.
# This table is the telling: it is exported to spec.json, both halves read it,
# and each renders `no-method` for a (kind, accessor) pair it marks out of
# scope.  Neither half is guessing, and the fact stays graded rather than
# quietly dropped -- the same arrangement ENCODING_ALIASES uses for the encoding
# gate.
#
# Scope values:
#   "any"     every node answers
#   "group"   only a group answers -- exactly the nodes whose `is_group` is
#             true, which is every kind below except Token (Token.__init__ sets
#             it false at sql.py:55, TokenList.__init__ sets it true at 163, and
#             nothing else touches it, so `is_group` and "is a TokenList" are
#             the same predicate)
#   <Kind>    only that one kind answers
#
# _self_check asserts the whole table against hasattr over all 22 classes, so it
# cannot drift from the reference without failing the build.

NODE_KINDS = [
    "Assignment", "Begin", "Case", "Command", "Comment", "Comparison", "For",
    "Function", "Having", "Identifier", "IdentifierList", "If", "Operation",
    "Over", "Parenthesis", "SquareBrackets", "Statement", "Token", "TokenList",
    "TypedLiteral", "Values", "Where",
]

# The one kind that is not a group.  Everything else in NODE_KINDS derives from
# TokenList.
NON_GROUP_KINDS = ["Token"]

ACCESSOR_SCOPE: dict[str, str] = {
    # Token's own surface -- inherited by every kind.
    "kind": "any",
    "ttype": "any",
    "value": "any",
    "normalized": "any",
    "is_group": "any",
    "is_keyword": "any",
    "is_whitespace": "any",
    "is_newline": "any",
    "str": "any",
    "flatten_count": "any",
    "within_function": "any",
    "within_parenthesis": "any",
    "parent_kind": "any",
    "has_ancestor_stmt": "any",
    "is_child_of_root": "any",
    # Reads token_index on the *parent*, so the precondition is having a parent,
    # not being a group.  A parent is always a group by construction, so every
    # kind can answer this whenever it is not the root.
    "token_index_self": "any",
    "match_kw_lower": "any",
    "match_kw_subtype": "any",
    "match_punct_regex": "any",
    "multiline_str": "any",
    # TokenList's surface -- absent on Token.
    "token_count": "group",
    "get_real_name": "group",
    "get_name": "group",
    "get_parent_name": "group",
    "get_alias": "group",
    "has_alias": "group",
    "get_sublists": "group",
    "token_first": "group",
    "token_first_ws": "group",
    "token_next_0": "group",
    "token_prev_last": "group",
    "token_at_offset_0": "group",
    "token_at_offset_mid": "group",
    # One class each.
    "get_type": "Statement",
    "get_ordering": "Identifier",
    "get_typecast": "Identifier",
    "get_array_indices": "Identifier",
    "is_wildcard": "Identifier",
    "get_identifiers": "IdentifierList",
    "get_parameters": "Function",
    "get_window": "Function",
    "get_cases": "Case",
    "get_cases_skip": "Case",
    "left": "Comparison",
    "right": "Comparison",
    "comment_is_multiline": "Comment",
}


def accessor_applies(scope: str, kind: str) -> bool:
    """Whether a node of `kind` answers an accessor with this scope."""
    if scope == "any":
        return True
    if scope == "group":
        return kind not in NON_GROUP_KINDS
    return scope == kind


# --------------------------------------------------------------------------
# CLI invocations
# --------------------------------------------------------------------------
# argv (after the program name) plus the document to feed as stdin or write to a
# file.  "-" means stdin.  {doc} in an argv element is replaced with the path of
# the document written to a temporary file.

CLI_CASES: dict[str, dict] = {
    "version": {"argv": ["--version"], "doc": None},
    "help": {"argv": ["--help"], "doc": None, "grade": "status-and-shape"},
    "no-args": {"argv": [], "doc": None, "grade": "status-and-diag"},
    "stdin-plain": {"argv": ["-"], "doc": "ft-simple"},
    "stdin-reindent": {"argv": ["-", "-r"], "doc": "ft-many-cols"},
    "stdin-reindent-long": {"argv": ["-", "--reindent"], "doc": "ft-join-where"},
    "stdin-kw-upper": {"argv": ["-", "-k", "upper"], "doc": "ft-simple"},
    "stdin-kw-lower": {"argv": ["-", "--keywords", "lower"], "doc": "ft-mixed-case"},
    "stdin-kw-capitalize": {"argv": ["-", "-k", "capitalize"], "doc": "ft-simple"},
    "stdin-id-upper": {"argv": ["-", "-i", "upper"], "doc": "ft-simple"},
    "stdin-id-lower": {"argv": ["-", "--identifiers", "lower"],
                       "doc": "ft-mixed-case"},
    "stdin-strip-comments": {"argv": ["-", "--strip-comments"],
                             "doc": "ft-comment-inline"},
    "stdin-indent-width-4": {"argv": ["-", "-r", "--indent_width", "4"],
                             "doc": "ft-many-cols"},
    "stdin-indent-width-8": {"argv": ["-", "-r", "--indent_width", "8"],
                             "doc": "ft-subquery"},
    "stdin-indent-tabs": {"argv": ["-", "-r", "--indent_tabs"],
                          "doc": "ft-many-cols"},
    "stdin-indent-after-first": {
        "argv": ["-", "-r", "--indent_after_first"], "doc": "ft-many-cols"},
    "stdin-indent-columns": {"argv": ["-", "-r", "--indent_columns"],
                             "doc": "ft-many-cols"},
    "stdin-wrap-after": {"argv": ["-", "-r", "--wrap_after", "20"],
                         "doc": "ft-in-list-long"},
    "stdin-comma-first": {"argv": ["-", "-r", "--comma_first"],
                          "doc": "ft-many-cols"},
    "stdin-compact": {"argv": ["-", "-r", "--compact"], "doc": "ft-cte"},
    "stdin-aligned": {"argv": ["-", "-a"], "doc": "ft-join-where"},
    "stdin-aligned-long": {"argv": ["-", "--reindent_aligned"],
                           "doc": "ft-subquery"},
    "stdin-out-python": {"argv": ["-", "-l", "python"], "doc": "ft-simple"},
    "stdin-out-php": {"argv": ["-", "-l", "php"], "doc": "ft-simple"},
    "stdin-strip-ws": {"argv": ["-", "--strip_whitespace"], "doc": "ft-tabs"},
    "stdin-spaces-operators": {"argv": ["-", "--use_space_around_operators"],
                               "doc": "ft-no-space-ops"},
    "stdin-encoding-cp1251": {"argv": ["-", "--encoding", "cp1251"],
                              "doc": "by-cp1251"},
    "stdin-encoding-gbk": {"argv": ["-", "--encoding", "gbk"], "doc": "by-gbk"},
    "stdin-encoding-utf8": {"argv": ["-", "--encoding", "utf-8"],
                            "doc": "ft-unicode"},
    # Undecodable input, not a bad codec name: `ascii` is a real codec and the
    # document is UTF-8.  The reference does not handle it -- nothing in cli.py
    # catches UnicodeDecodeError -- so CPython prints a traceback and exits 1, and
    # the traceback names /tmp/freeze-work/reference/sqlparse/cli.py and
    # /usr/lib/python3.11/encodings/ascii.py.  Graded `full`, that made the
    # expected answer a scratch path from the verifier's own build container: no
    # port could produce it, and the case was unpassable rather than hard.
    #
    # What is gradable is the same three bits as its sibling bad-encoding-name:
    # exits non-zero, says nothing on stdout, says something on stderr, and does
    # not open with usage (this is not an argument error).  That is exactly the
    # difference between a port that reports the failed decode and one that
    # substitutes replacement characters and prints SQL.
    "stdin-encoding-wrong": {"argv": ["-", "--encoding", "ascii"],
                             "doc": "ft-unicode", "grade": "status-and-diag"},
    "stdin-multi-stmt": {"argv": ["-", "-r"], "doc": "ft-multi-stmt"},
    "stdin-empty": {"argv": ["-"], "doc": "ms-empty"},
    "stdin-ws-only": {"argv": ["-"], "doc": "ms-ws-only"},
    "stdin-unicode": {"argv": ["-", "-r"], "doc": "ft-unicode"},
    "stdin-pathological": {"argv": ["-", "-r"], "doc": "pa-wide-cols-500"},
    "stdin-error-doc": {"argv": ["-", "-r"], "doc": "er-lone-single-quote"},
    "stdin-combined": {
        "argv": ["-", "-r", "-k", "upper", "-i", "lower", "--strip-comments",
                 "--indent_width", "3"],
        "doc": "ft-comment-block"},
    "file-plain": {"argv": ["{doc}"], "doc": "ft-simple"},
    "file-reindent": {"argv": ["{doc}", "-r"], "doc": "ft-cte"},
    # Two positionals where the parser declares one.  Graded on status alone for
    # the same reason as bad-unknown-flag: the rejection is contractual, the
    # argument parser's prose about it is not.
    "file-two": {"argv": ["{doc}", "{doc}"], "doc": "ft-simple",
                 "grade": "status-and-diag"},

    # -o/--outfile: the only flag with a filesystem side effect, so the graded
    # signature includes the file's bytes as well as the streams.  A port that
    # writes the formatted text to stdout and an empty file passes every other
    # CLI case and fails these.
    "out-plain": {"argv": ["-", "-o", "{out}"], "doc": "ft-simple"},
    "out-reindent": {"argv": ["{doc}", "-r", "-o", "{out}"], "doc": "ft-many-cols"},
    # --encoding governs the output stream as well as the input one.
    "out-encoding": {"argv": ["-", "--encoding", "cp1251", "-o", "{out}"],
                     "doc": "by-cp1251"},
    "out-unwritable": {"argv": ["-", "-o", "/nonexistent-dir/out.sql"],
                       "doc": "ft-simple", "grade": "status-and-prefix"},
    "file-missing": {"argv": ["/nonexistent/definitely-not-here.sql"],
                     "doc": None, "grade": "status-and-prefix"},
    "file-is-dir": {"argv": ["/tmp"], "doc": None, "grade": "status-and-prefix"},
    "bad-kw-choice": {"argv": ["-", "-k", "bogus"], "doc": "ft-simple",
                      "grade": "status-and-diag"},
    "bad-id-choice": {"argv": ["-", "-i", "bogus"], "doc": "ft-simple",
                      "grade": "status-and-diag"},
    "bad-lang-choice": {"argv": ["-", "-l", "ruby"], "doc": "ft-simple",
                        "grade": "status-and-diag"},
    "bad-indent-width-value": {"argv": ["-", "--indent_width", "abc"],
                               "doc": "ft-simple", "grade": "status-and-diag"},
    "bad-indent-width-zero": {"argv": ["-", "-r", "--indent_width", "0"],
                              "doc": "ft-simple",
                              "grade": "status-and-stderr"},
    # The library's own message, not the parser's: formatter.validate_options
    # raises SQLParseError and cli._error writes "[ERROR] Invalid options: ...".
    # That string is contractual -- the same text the port's Error.Error() must
    # produce -- so this one is graded byte for byte, unlike its argparse siblings.
    # It was graded on status alone, which put a library message no port can guess
    # in the same bucket as prose no port should have to.
    "bad-indent-width-neg": {"argv": ["-", "-r", "--indent_width", "-1"],
                             "doc": "ft-simple", "grade": "status-and-stderr"},
    "bad-wrap-after-neg": {"argv": ["-", "-r", "--wrap_after", "-5"],
                           "doc": "ft-simple", "grade": "status-and-stderr"},
    "bad-unknown-flag": {"argv": ["-", "--not-a-real-flag"], "doc": "ft-simple",
                         "grade": "status-and-diag"},
    # The reference does not handle this one: TextIOWrapper raises LookupError and
    # nothing catches it, so CPython prints a traceback and exits 1.  The traceback
    # is the interpreter's and is not gradable, but "exits 1, says something on
    # stderr, says nothing on stdout, and does not open with usage" is, and it is
    # what separates a port that reports the bad codec from one that accepts it.
    "bad-encoding-name": {"argv": ["-", "--encoding", "not-an-encoding"],
                          "doc": "ft-simple", "grade": "status-and-diag"},
}


def _self_check() -> None:
    """Table hygiene, asserted at import so a slip cannot reach the image.

    Two presets with the same option map are one preset counted twice.  The
    format family is a preset x document matrix, so a duplicate silently inflates
    the case count by a document's worth of cases without adding coverage -- and
    it does it in the family that already carries the most weight.
    """
    for label, table in (("format preset", FORMAT_PRESETS),
                         ("validate case", VALIDATE_CASES)):
        seen: dict[str, str] = {}
        for name, options in table.items():
            key = repr(sorted((k, tuple(v)) for k, v in options.items()))
            if key in seen:
                raise SystemExit(
                    f"{label}s {seen[key]!r} and {name!r} are the same options"
                )
            seen[key] = name

    # Every option name a preset or validate case mentions has to be an option
    # the reference actually knows, or the case grades a typo.
    known = {
        "keyword_case", "identifier_case", "strip_comments", "strip_whitespace",
        "truncate_strings", "truncate_char", "reindent", "reindent_aligned",
        "indent_tabs", "indent_width", "indent_char", "indent_after_first",
        "indent_columns", "wrap_after", "comma_first", "compact",
        "right_margin", "output_format", "use_space_around_operators",
    }
    for label, table in (("format preset", FORMAT_PRESETS),
                         ("validate case", VALIDATE_CASES)):
        for name, options in table.items():
            unknown = sorted(set(options) - known)
            if unknown:
                raise SystemExit(f"{label} {name!r} names unknown options: {unknown}")

    # Every filter a stack references must exist.
    for name, shape in STACKS.items():
        missing = [f for f in shape["filters"] if f not in FILTERS]
        if missing:
            raise SystemExit(f"stack {name!r} references unknown filters: {missing}")

    for name, entry in FILTERS.items():
        if entry["stage"] not in ("pre", "stmt", "post"):
            raise SystemExit(f"filter {name!r} has an unknown stage")
        for field in ("py", "go", "params"):
            if field not in entry:
                raise SystemExit(f"filter {name!r} is missing {field!r}")

    # remove_quotes("") raises in the reference and cannot raise in Go.  Assert
    # it stays out rather than trusting the comment above the list.
    if "" in REMOVE_QUOTES_INPUTS:
        raise SystemExit(
            "remove_quotes('') raises IndexError in the reference and the "
            "contracted Go signature cannot raise; it must not be graded"
        )

    for alias, canonical in ENCODING_ALIASES.items():
        if canonical not in ENCODING_CANONICAL:
            raise SystemExit(
                f"encoding alias {alias!r} resolves to unknown {canonical!r}")
        if alias != alias.strip().lower():
            raise SystemExit(
                f"encoding alias {alias!r} is not already normalized, so the "
                f"table can never match it"
            )

    # The grade name selects which streams the executor compares.  An unknown one
    # would silently reach the grader, which has no handler for it -- so the
    # vocabulary is closed here rather than discovered at grading time.
    #
    # There is deliberately no status-only grade.  On a case whose expected
    # status is a *failure*, comparing the status alone asserts nothing, because
    # a binary that panics on startup also exits non-zero; `status-and-diag`
    # covers those and asserts which stream the diagnostic went to.
    cli_grades = {"full", "status-and-diag", "status-and-stderr",
                  "status-and-prefix", "status-and-shape"}
    for name, entry in CLI_CASES.items():
        grade = entry.get("grade", "full")
        if grade not in cli_grades:
            raise SystemExit(
                f"cli case {name!r} has unknown grade {grade!r}; "
                f"known: {sorted(cli_grades)}"
            )
        if not isinstance(entry.get("argv"), list):
            raise SystemExit(f"cli case {name!r} has no argv list")
        placeholders = [a for a in entry["argv"] if "{doc}" in a or "{out}" in a]
        if any("{doc}" in a for a in placeholders) and entry.get("doc") is None:
            raise SystemExit(
                f"cli case {name!r} substitutes {{doc}} but names no document")
        # An {out} case whose signature ignored the file would grade nothing that
        # the equivalent stdout case does not already cover.
        # Stated as the grade that does read it rather than as the grades that do
        # not: `full` is the only branch of CliRunner.signature that opens the
        # outfile, so naming the others would be a list to keep in step with a
        # vocabulary that grows.
        if any("{out}" in a for a in entry["argv"]) and grade != "full":
            raise SystemExit(
                f"cli case {name!r} writes an output file but is graded "
                f"{grade!r}, and only 'full' reads the file back, so the bytes it "
                f"wrote would never be compared"
            )

    # Cases the reference answers with an uncaught exception.  Every one of them
    # must be graded on the diagnostic's shape and not its text, because the text
    # is a CPython traceback: it carries interpreter frames, caret markers, the
    # stdlib's own file paths, and the absolute path of the reference inside the
    # verifier's build container.  Graded `full`, such a case is not difficult, it
    # is unpassable -- and it looks exactly like a case a port simply got wrong.
    #
    # Listed by name rather than detected, because detection needs the frozen
    # answer and this check runs before anything is frozen.  freeze.py re-checks it
    # from the other direction: portable_answers() scans every frozen payload for a
    # traceback marker, so a case added here without a grade, or an unhandled path
    # nobody knew about, fails the image build rather than the submission.
    unhandled_in_reference = {
        # LookupError from TextIOWrapper: the codec name does not exist.
        "bad-encoding-name",
        # UnicodeDecodeError from wrapper.read(): the codec exists, the bytes are
        # not valid in it.
        "stdin-encoding-wrong",
    }
    for name in sorted(unhandled_in_reference):
        if name not in CLI_CASES:
            raise SystemExit(
                f"cli case {name!r} is listed as an unhandled reference path but "
                f"no such case exists")
        grade = CLI_CASES[name].get("grade", "full")
        if grade != "status-and-diag":
            raise SystemExit(
                f"cli case {name!r} is an unhandled path in the reference, so its "
                f"stderr is a CPython traceback, but it is graded {grade!r}. Only "
                f"'status-and-diag' compares the shape of the diagnostic rather "
                f"than its text; any other grade makes the case unpassable.")

    if len(set(TTYPE_NAMES)) != len(TTYPE_NAMES):
        raise SystemExit("TTYPE_NAMES has a duplicate")
    for name in TTYPE_NONEXISTENT:
        if name not in TTYPE_NAMES:
            raise SystemExit(f"nonexistent ttype {name!r} is not probed at all")
    for name in TTYPE_NAMES:
        if not name.startswith("Token"):
            raise SystemExit(f"ttype {name!r} is not rooted at Token")

    steps = {"clear", "default-init", "add", "lex", "is-keyword"}
    for name, script in LEXSTATE_SCRIPTS.items():
        if not any(s[0] in ("lex", "is-keyword") for s in script):
            raise SystemExit(f"lexstate script {name!r} observes nothing")
        for step in script:
            if step[0] not in steps:
                raise SystemExit(
                    f"lexstate script {name!r} has unknown step {step[0]!r}")
            if step[0] == "add" and not isinstance(step[1], dict):
                raise SystemExit(f"lexstate script {name!r}: add wants a table")
            if step[0] in ("lex", "is-keyword") and not isinstance(step[1], str):
                raise SystemExit(f"lexstate script {name!r}: {step[0]} wants a string")

    # Table-internal hygiene only.  Comparing the scope table against the
    # reference needs sqlparse imported, and spec.py is imported by tools that
    # run without it, so that check is verify_accessor_scope() below and the
    # build step calls it where the reference exists.
    for label, scope in ACCESSOR_SCOPE.items():
        if scope in ("any", "group"):
            continue
        if scope not in NODE_KINDS:
            raise SystemExit(
                f"accessor {label!r} is scoped to unknown kind {scope!r}")
    for kind in NON_GROUP_KINDS:
        if kind not in NODE_KINDS:
            raise SystemExit(f"non-group kind {kind!r} is not in NODE_KINDS")
    if len(set(NODE_KINDS)) != len(NODE_KINDS):
        raise SystemExit("NODE_KINDS has a duplicate")


# label -> the node attribute it reads.  Labels the probe computes from the node
# rather than reading off it map to None: there is nothing for hasattr to
# confirm, and their scope is a decision about what the probe can compute, not
# about the reference's hierarchy.  token_index_self is one of them because it
# reads token_index on the *parent*.
ACCESSOR_ATTR: dict[str, str | None] = {
    "kind": None, "str": None, "flatten_count": None, "multiline_str": None,
    "token_index_self": None,
    "ttype": "ttype", "value": "value", "normalized": "normalized",
    "is_group": "is_group", "is_keyword": "is_keyword",
    "is_whitespace": "is_whitespace", "is_newline": "is_newline",
    "token_count": "tokens", "get_real_name": "get_real_name",
    "get_name": "get_name", "get_parent_name": "get_parent_name",
    "get_alias": "get_alias", "has_alias": "has_alias",
    "get_sublists": "get_sublists", "token_first": "token_first",
    "token_first_ws": "token_first", "token_next_0": "token_next",
    "token_prev_last": "token_prev",
    "token_at_offset_0": "get_token_at_offset",
    "token_at_offset_mid": "get_token_at_offset",
    "get_type": "get_type", "get_ordering": "get_ordering",
    "get_typecast": "get_typecast",
    "get_array_indices": "get_array_indices",
    "is_wildcard": "is_wildcard", "get_identifiers": "get_identifiers",
    "get_parameters": "get_parameters", "get_window": "get_window",
    "get_cases": "get_cases", "get_cases_skip": "get_cases",
    "left": "left", "right": "right",
    "comment_is_multiline": "is_multiline",
    "has_ancestor_stmt": "has_ancestor",
    "is_child_of_root": "is_child_of",
    "match_kw_lower": "match", "match_kw_subtype": "match",
    "match_punct_regex": "match",
    "within_function": "within", "within_parenthesis": "within",
    "parent_kind": "parent",
}


def verify_accessor_scope() -> None:
    """ACCESSOR_SCOPE against the reference's actual class hierarchy.

    The table is what the Go half is told, so it has to be what the Python half
    would have discovered by asking.  Every entry is checked against hasattr over
    every kind: an accessor the table calls out of scope must genuinely be
    missing, and one it calls in scope must genuinely be there.  A sqlparse that
    moved a method between classes fails here rather than silently grading the
    two halves against different tables.

    Not called at import, because spec.py is imported by tools that run without
    the reference on the path.  The freeze step calls it, so a mismatch stops the
    verifier image build before any expectation is recorded.
    """
    import inspect

    from sqlparse import sql as _sql

    missing = sorted(set(ACCESSOR_SCOPE) - set(ACCESSOR_ATTR))
    if missing:
        raise SystemExit(
            f"accessors {missing} have no ACCESSOR_ATTR entry, so their scope "
            f"cannot be verified"
        )
    extra = sorted(set(ACCESSOR_ATTR) - set(ACCESSOR_SCOPE))
    if extra:
        raise SystemExit(f"ACCESSOR_ATTR names unscoped accessors {extra}")

    classes = {
        name: obj for name, obj in vars(_sql).items()
        if inspect.isclass(obj) and issubclass(obj, _sql.Token)
    }
    if sorted(classes) != sorted(NODE_KINDS):
        raise SystemExit(
            "NODE_KINDS does not match sqlparse.sql: missing "
            f"{sorted(set(classes) - set(NODE_KINDS))}, extra "
            f"{sorted(set(NODE_KINDS) - set(classes))}"
        )
    non_group = sorted(
        n for n, c in classes.items() if not issubclass(c, _sql.TokenList)
    )
    if non_group != sorted(NON_GROUP_KINDS):
        raise SystemExit(
            f"NON_GROUP_KINDS says {sorted(NON_GROUP_KINDS)} but the "
            f"non-TokenList kinds are {non_group}"
        )

    for label, scope in ACCESSOR_SCOPE.items():
        attr = ACCESSOR_ATTR[label]
        if attr is None:  # computed by the probe; nothing to compare against
            continue
        if scope not in ("any", "group") and scope not in classes:
            raise SystemExit(
                f"accessor {label!r} is scoped to unknown kind {scope!r}")
        for kind, cls in classes.items():
            want = accessor_applies(scope, kind)
            got = hasattr(cls, attr)
            if want != got:
                raise SystemExit(
                    f"accessor {label!r} (scope {scope!r}) reads {attr!r}: the "
                    f"table says {kind} {'answers' if want else 'does not'}, "
                    f"but hasattr says {got}"
                )


_self_check()


def as_json() -> dict:
    """Everything the Go probes need, in one serializable object."""
    return {
        "schema": "swerefactor-spec-v1",
        "format_presets": FORMAT_PRESETS,
        "validate_cases": VALIDATE_CASES,
        "filters": FILTERS,
        "stacks": STACKS,
        "remove_quotes_inputs": REMOVE_QUOTES_INPUTS,
        "split_unquoted_newlines_inputs": SPLIT_UNQUOTED_NEWLINES_INPUTS,
        "ttype_names": TTYPE_NAMES,
        "ttype_nonexistent": TTYPE_NONEXISTENT,
        "node_kinds": NODE_KINDS,
        "non_group_kinds": NON_GROUP_KINDS,
        "accessor_scope": ACCESSOR_SCOPE,
        "lexstate_scripts": LEXSTATE_SCRIPTS,
        "encoding_aliases": ENCODING_ALIASES,
        "encoding_canonical": sorted(ENCODING_CANONICAL),
        "cli_cases": CLI_CASES,
    }


if __name__ == "__main__":
    import json

    payload = as_json()
    print(json.dumps(
        {k: (len(v) if isinstance(v, (list, dict)) else v)
         for k, v in payload.items()},
        indent=1, sort_keys=True,
    ))
