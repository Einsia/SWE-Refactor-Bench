"""What the reference does, stated once, in the only place that states it.

This module is the single source of truth for the graded surface: the two CLI
flag matrices, the number-rendering rule, the closed parseYaml input set, the
error vocabulary, and the stdlib inventory.  The case list generator reads it, the
freeze step reads it, the verifier reads it, and it is exported to spec.json so
the migration contract shipped to the submission is generated from the same data
rather than written alongside it.  lang03 learned that the hard way: a
requirement stated in prose in one file and in a table in another will drift, and
the drift is invisible until a submission fails something the contract never
said.

Nothing here imports the reference.  The freeze step, which runs where the
reference exists, calls the verify_* functions below to bind every table in this
file to actual reference behavior; a mismatch stops the verifier image build
before any expectation is recorded.  That split is deliberate -- spec.py is
imported by authoring tools that run with no jsonnet binary on the path.
"""
from __future__ import annotations

SCHEMA_VERSION = "1.0"
UPSTREAM = "google/jsonnet"
UPSTREAM_VERSION = "0.20.0"

# stdlib/std.jsonnet survives the migration byte for byte: of the 129 public std
# fields, 96 are written in Jsonnet in this file and embedded at build time, and
# 33 are native.  So State B still carries this exact file and must be able to
# evaluate it -- the stdlib is preserved *data*, not code to be ported.
# Measured on the shipped State A tree.
STDLIB_SHA256 = \
    "006e2051f3db3bc8311dbd98b421b01740cfc078f7432bfda715669e664fff5f"

# --------------------------------------------------------------------------
# the two command-line surfaces
# --------------------------------------------------------------------------
# Every flag the reference documents, with the shape of its argument.  These are
# the public interface being preserved, so they are graded as behavior: a port
# that renames one, drops one, or accepts one it should reject has changed the
# interface even if its evaluator is perfect.
#
# arg kinds:
#   "none"    a bare switch
#   "int"     a decimal count
#   "dir"     a directory path
#   "file"    a file path
#   "var"     <name>[=<value>], value optional (read from the environment)
#   "varfile" <name>=<path>
#   "choice"  one of a fixed set, given in `choices`

EVAL_FLAGS: dict[str, dict] = {
    "--help": {"short": "-h", "arg": "none"},
    "--exec": {"short": "-e", "arg": "none"},
    "--jpath": {"short": "-J", "arg": "dir"},
    "--output-file": {"short": "-o", "arg": "file"},
    "--multi": {"short": "-m", "arg": "dir"},
    "--yaml-stream": {"short": "-y", "arg": "none"},
    "--string": {"short": "-S", "arg": "none"},
    "--max-stack": {"short": "-s", "arg": "int"},
    "--max-trace": {"short": "-t", "arg": "int"},
    "--gc-min-objects": {"short": None, "arg": "int"},
    "--gc-growth-trigger": {"short": None, "arg": "int"},
    "--version": {"short": None, "arg": "none"},
    "--ext-str": {"short": "-V", "arg": "var"},
    "--ext-str-file": {"short": None, "arg": "varfile"},
    "--ext-code": {"short": None, "arg": "var"},
    "--ext-code-file": {"short": None, "arg": "varfile"},
    "--tla-str": {"short": "-A", "arg": "var"},
    "--tla-str-file": {"short": None, "arg": "varfile"},
    "--tla-code": {"short": None, "arg": "var"},
    "--tla-code-file": {"short": None, "arg": "varfile"},
}

FORMAT_FLAGS: dict[str, dict] = {
    "--help": {"short": "-h", "arg": "none"},
    "--exec": {"short": "-e", "arg": "none"},
    "--output-file": {"short": "-o", "arg": "file"},
    "--in-place": {"short": "-i", "arg": "none"},
    "--test": {"short": None, "arg": "none"},
    "--indent": {"short": "-n", "arg": "int"},
    "--max-blank-lines": {"short": None, "arg": "int"},
    "--string-style": {"short": None, "arg": "choice",
                       "choices": ["d", "s", "l"]},
    "--comment-style": {"short": None, "arg": "choice",
                        "choices": ["h", "s", "l"]},
    "--pretty-field-names": {"short": None, "arg": "none", "negatable": True},
    "--pad-arrays": {"short": None, "arg": "none", "negatable": True},
    "--pad-objects": {"short": None, "arg": "none", "negatable": True},
    "--sort-imports": {"short": None, "arg": "none", "negatable": True},
    "--debug-desugaring": {"short": None, "arg": "none"},
    "--version": {"short": None, "arg": "none"},
}

# Argument-handling rules that hold for both binaries, and that a port written
# against a conventional argument parser gets wrong.  Each is graded.
ARGV_RULES = {
    # `-abc` expands to `-a -b -c`.  A getopt-style parser does this; a
    # hand-rolled one usually does not.
    "multichar_expansion": True,
    # `--` stops option processing, so a filename that starts with `-` is
    # reachable.
    "double_dash_terminates": True,
    # `-` as the filename means stdin.
    "dash_is_stdin": True,
    # jsonnet takes exactly one filename; jsonnetfmt takes any number.
    "eval_filenames": "exactly-one",
    "format_filenames": "zero-or-more",
}

# --------------------------------------------------------------------------
# the standard library inventory
# --------------------------------------------------------------------------
# Read out of the reference with std.objectFieldsAll(std), not grepped out of
# std.jsonnet: the grep sees 96 of these and misses all 33 native ones, so it
# would have quietly under-specified the graded surface by a quarter.
#
# The case list guarantees at least one case per name here.  A submission that
# ports the evaluator but forgets, say, std.manifestTomlEx fails those cases and
# nothing else, which is the resolution we want -- per-function credit rather
# than one all-or-nothing stdlib verdict.
STDLIB_PUBLIC = (
    "abs", "acos", "all", "any", "asciiLower", "asciiUpper", "asin",
    "assertEqual", "atan", "base64", "base64Decode", "base64DecodeBytes",
    "ceil", "char", "clamp", "codepoint", "cos", "count", "decodeUTF8",
    "deepJoin", "encodeUTF8", "endsWith", "equals", "escapeStringBash",
    "escapeStringDollars", "escapeStringJson", "escapeStringPython",
    "escapeStringXML", "exp", "exponent", "extVar", "filter", "filterMap",
    "find", "findSubstr", "flatMap", "flattenArrays", "floor", "foldl",
    "foldr", "format", "get", "isArray", "isBoolean", "isEmpty", "isFunction",
    "isNumber", "isObject", "isString", "join", "length", "lines", "log",
    "lstripChars", "makeArray", "manifestIni", "manifestJson",
    "manifestJsonEx", "manifestJsonMinified", "manifestPython",
    "manifestPythonVars", "manifestToml", "manifestTomlEx",
    "manifestXmlJsonml", "manifestYamlDoc", "manifestYamlStream",
    "mantissa", "map", "mapWithIndex", "mapWithKey", "max", "md5", "member",
    "mergePatch", "min", "mod", "modulo", "native", "objectFields",
    "objectFieldsAll", "objectFieldsEx", "objectHas", "objectHasAll",
    "objectHasEx", "objectKeysValues", "objectKeysValuesAll",
    "objectValues", "objectValuesAll", "parseHex", "parseInt", "parseJson",
    "parseOctal", "parseYaml", "pow", "primitiveEquals", "prune", "range",
    "repeat", "resolvePath", "reverse", "round", "rstripChars", "set",
    "setDiff", "setInter", "setMember", "setUnion", "sign", "sin", "slice",
    "sort", "split", "splitLimit", "splitLimitR", "sqrt", "startsWith",
    "strReplace", "stringChars", "stripChars", "substr", "sum", "tan",
    "thisFile", "toString", "trace", "type", "uniq", "xnor", "xor",
)

# Implemented in C++ rather than in std.jsonnet.  These are exactly the names a
# port has to write from scratch; the other 89 come along inside the preserved
# std.jsonnet and only need a working evaluator.  The split matters for grading
# because the two groups fail for different reasons.
#
# The first 39 are `jsonnet_builtin_decl` (core/desugarer.cpp:40-88), indices
# 0..max_builtin.  What makes that the authoritative list rather than the
# `builtins[...]` map in vm.cpp is desugarer.cpp:924-940: for every decl the
# desugarer does `field->body = fn`, *overwriting* whatever std.jsonnet defined
# for that name.  So seven names -- join, substr, range, strReplace, asciiLower,
# asciiUpper, splitLimit -- have a pure-jsonnet body in the preserved
# std.jsonnet that upstream never executes, and a port that keeps std.jsonnet
# and does not implement them natively runs code the reference does not.
#
# That is observable, not theoretical.  std.jsonnet's join raises
# 'expected %s but arr[%d] was %s ' with a trailing space; the native at
# vm.cpp:1725 emits the same text without it, and the frozen expectation for
# test_suite/error.std_join_types1.jsonnet is the native's:
#
#     b'RUNTIME ERROR: expected string but arr[1] was array'
#
# 43 of the 102 fields in std.jsonnet reach one of those seven transitively -- the
# seven and 36 more, including format, split, lines, every manifest* and four of
# the five escapeString* (escapeStringDollars reaches only toString, foldl and
# stringChars) -- so listing that group as "comes free" is wrong in a way that
# costs real cases and is undiscoverable without a reference to diff against.
# `check_against_reference` recomputes both numbers from the preserved std.jsonnet
# rather than leaving them as prose, so a count that drifts from what the file
# contains fails the image build instead of standing unchallenged in a comment.
#
# thisFile is the fortieth and is not a builtin function: desugarer.cpp:944
# injects it as a hidden string field per file.
STDLIB_NATIVE = frozenset((
    "acos", "asciiLower", "asciiUpper", "asin", "atan", "ceil", "char",
    "codepoint", "cos", "decodeUTF8", "encodeUTF8", "exp", "exponent",
    "extVar", "filter", "floor", "join", "length", "log", "makeArray",
    "mantissa", "md5", "modulo", "native", "objectFieldsEx", "objectHasEx",
    "parseJson", "parseYaml", "pow", "primitiveEquals", "range", "sin",
    "splitLimit", "sqrt", "strReplace", "substr", "tan", "thisFile", "trace",
    "type",
))

# The two figures the comment above quotes, named so `_check_shadow_reach` can
# recompute them from the byte-pinned std.jsonnet at freeze time.  They are stated
# here and derived there; if they ever disagree the image build fails rather than
# the document quietly becoming wrong.  instruction.md quotes the same two.
STDLIB_FIELD_COUNT = 102
STDLIB_SHADOW_REACH = 43

# The seven whose std.jsonnet body is dead code upstream.  Kept as a named set
# because it is the part of STDLIB_NATIVE a port is most likely to get wrong:
# the name resolves and mostly works, so nothing fails until an error path or an
# edge case diverges.
STDLIB_NATIVE_SHADOWING = frozenset((
    "asciiLower", "asciiUpper", "join", "range", "splitLimit", "strReplace",
    "substr",
))

# std.thisFile is the one public member that is not a function.  A port that
# builds its std object from a table of builtins will naturally make everything
# callable and get this wrong; std.type(std.thisFile) == "string" is graded.
STDLIB_NON_FUNCTION = {"thisFile": "string"}

# Private helpers.  Not part of the public surface, but they exist in the object
# and objectFieldsAll(std) reports them, so a port that hides or renames them
# changes an observable.  Graded through reflection cases only.
STDLIB_DUNDER = (
    "__array_greater", "__array_greater_or_equal", "__array_less",
    "__array_less_or_equal", "__compare", "__compare_array",
)

# --------------------------------------------------------------------------
# the number rule
# --------------------------------------------------------------------------
# Every number the reference prints goes through jsonnet_unparse_number
# (core/parser.cpp:36-49), which is four lines of C++ and has no equivalent
# anywhere in .NET:
#
#     if (v == floor(v)) { ss << std::fixed << std::setprecision(0) << v; }
#     else               { ss << std::setprecision(17); ss << v;          }
#
# In C terms: "%.0f" for integral values, "%.17g" for the rest.  Consequences,
# all measured against the reference:
#
#  * Integral values never use exponent notation, at any magnitude.  1e100 is
#    101 digits and 1.7976931348623157e308 is 309 digits -- the exact decimal
#    expansion of the double, not a rounded one.
#  * "%.17g" keeps 17 significant digits and strips trailing zeros, so 0.1 is
#    0.10000000000000001 and 0.5 is 0.5.
#  * "%.17g" switches to exponent form when the decimal exponent is below -4,
#    giving 1e-4 -> 0.0001 but 1e-5 -> 1.0000000000000001e-05.
#  * The exponent carries a sign and at least two digits: e-05, not e-5.
#  * The other "%g" threshold -- exponent >= precision -- is unreachable here.
#    A non-integral double is smaller than 2**52 in magnitude, so its exponent
#    caps at 15, and the large-value branch never fires for the else arm.  Every
#    large number takes the "%.0f" path instead.
#  * -0.0 prints as -0.
#
# No .NET format string reproduces this: "R" and "G17" both emit E+xx notation
# for large values and drop the 17th digit when 16 round-trip, and "F0" mangles
# the small ones.  The port has to implement the rule.  Nothing else in the
# migration has this property of being simultaneously tiny, load-bearing for
# every single output byte, and impossible to get right by accident.
NUMBER_RULE = {
    "integral_format": "%.0f",
    "fractional_format": "%.17g",
    "significant_digits": 17,
    "exponent_min_digits": 2,
    "exponent_always_signed": True,
    "integral_uses_exponent": False,
    "negative_zero": "-0",
    # Non-finite doubles are unreachable: the reference raises before printing.
    # Graded as errors, not as renderings -- see NUMBER_NONFINITE.
    "finite_only": True,
}

# The reference's own reactions to the ways a program can leave the finite
# doubles.  Measured, one probe per row; these are error cases, so they are
# graded through the error vocabulary rather than the number rule.
NUMBER_NONFINITE = {
    "1e400": "overflow",
    "1 / 0": "division by zero.",
    "0 / 0": "division by zero.",
    "std.log(0)": "overflow",
    "std.sqrt(-1)": "not a number",
}

# Renderings that pin every branch of the rule.  The freeze step checks the
# reference against this table and refuses to build the verifier image if any
# row disagrees, which is what keeps the prose above honest.
NUMBER_CASES = (
    ("0", "0"),
    ("-0", "-0"),
    ("0.5", "0.5"),
    ("100", "100"),
    ("1e15", "1000000000000000"),
    ("1e18", "1000000000000000000"),
    ("0.1", "0.10000000000000001"),
    ("0.1 + 0.2", "0.30000000000000004"),
    ("1 / 3", "0.33333333333333331"),
    ("2 / 3", "0.66666666666666663"),
    ("1e-4", "0.0001"),
    ("1e-5", "1.0000000000000001e-05"),
    ("1.5e-5", "1.5e-05"),
    ("1e-7", "9.9999999999999995e-08"),
    ("1e-300", "1e-300"),
    ("5e-324", "4.9406564584124654e-324"),
    ("1e21", "1000000000000000000000"),
    # Not "1" followed by a hundred zeros.  "%.0f" prints the exact decimal
    # value of the double nearest 1e100, which diverges from a round power of
    # ten at the 18th digit.  A port that formats large integrals through any
    # kind of shortest-round-trip path produces the zeros and fails here.
    ("1e100",
     "1000000000000000015902891109759918046836080856394528138978132755774"
     "7838772170381060813469985856815104"),
    ("123456789012345678", "123456789012345680"),
    ("12345678901234567890", "12345678901234567168"),
    ("9007199254740993", "9007199254740992"),
    ("4.35", "4.3499999999999996"),
    ("1.005", "1.0049999999999999"),
    ("0.3", "0.29999999999999999"),
    ("4503599627370495.5", "4503599627370495.5"),
    ("1e-4 - 1e-20", "9.9999999999999991e-05"),
    ("-1e-7", "-9.9999999999999995e-08"),
)

# Round-half-even is the rule, and it is nearly unobservable: ties cannot occur
# on the "%.0f" path at all (an integral double over its power of ten divides
# exactly -- measured, 0 ties in 8k doubles), and in "%.17g" they need an exact
# half at the 18th significant digit.  2**-25 is the only negative power of two
# that produces one.  So sampling never reaches this rule and the values have to
# be named.  These are m * 2**-k for odd m, which yields 125 such values.
NUMBER_TIE_CASES = (
    "2.9802322387695312e-08",   # 2**-25, the only pow2 tie
    "1.0251998901367188e-05",
    "1.0728836059570312e-05",
    "1.1205673217773438e-05",
    "1.1682510375976562e-05",
    "1.2159347534179688e-05",
    "942033065782386.6",        # a tie on the fractional side of 2**52
)

# --------------------------------------------------------------------------
# std.parseYaml: an allowlist, because most of the surface is not behavior
# --------------------------------------------------------------------------
# parseYaml is native and delegates to a vendored YAML parser that aborts the
# process -- SIGABRT, exit 134 through a shell, with "Something went wrong
# during jsonnet_evaluate_snippet, please report this" -- on a wide range of
# ordinary YAML.  Measured: 14 of 61 scalars and 9 of 24 documents abort,
# including ".5", "5.", "+1", "1,000", every "!!tag", anchors, merge keys,
# literal and folded block scalars, and tab indentation.
#
# An abort is not a behavior.  Requiring a port to reproduce a crash and
# requiring it not to are both wrong, so aborting inputs are excluded -- and the
# exclusion is an allowlist rather than a denylist, because a denylist can never
# be complete and the first submission to wander outside it would be graded on
# a crash.  ASSERTED_NO_ABORT below is checked against the reference at freeze
# time, so an error in this derivation stops the verifier build instead of
# reaching a submission.
PARSEYAML_SCALARS = (
    # numbers and their near-misses
    "1", "-1", "0", "1.5", "-1.5", "1e3", "1E3", "1.0",
    # the booleans YAML 1.1 recognizes, and the ones this parser does not
    "true", "True", "TRUE", "false", "False",
    "yes", "Yes", "no", "No", "on", "On", "off", "Off", "y", "n",
    # nulls.  "~" resolves to the empty string here, not to null.
    "null", "Null", "NULL", "~",
    # infinities and NaN, none of which this parser resolves
    ".inf", ".Inf", ".INF", "-.inf", ".nan", ".NaN",
    "inf", "nan", "Infinity",
    # strings, including two that look like numbers and resolve as numbers
    "abc", "'1'", '"1"', "a b", " a ", "a:b", "1:2", "12:30",
    "2001-12-14", "1_000",
)

PARSEYAML_DOCUMENTS = (
    ("flow-seq", "[1, 2, 3]"),
    ("flow-map", "{a: 1, b: 2}"),
    ("block-seq", "- 1\n- 2\n"),
    ("block-map", "a: 1\nb: 2\n"),
    ("nested", "a:\n  b:\n    - 1\n    - c: 2\n"),
    ("multidoc", "---\na: 1\n---\nb: 2\n"),
    ("multidoc-one", "---\na: 1\n"),
    ("block-strip", "a: |-\n  one\n"),
    ("comment", "# c\na: 1\n"),
    ("dup-key", "a: 1\na: 2\n"),
    ("empty-value", "a:\n"),
    ("quoted-key", '"a b": 1\n'),
    ("unclosed-flow", "[1, 2\n"),
    ("utf8", "a: caf\u00e9\n"),
    ("deep-nest", "a:\n" + "".join(" " * (2 * i) + "b:\n"
                                   for i in range(1, 12))),
)

# Named so the contract can publish them and so a reader can see the boundary
# was measured rather than guessed.  Every one aborts the reference.
PARSEYAML_EXCLUDED = {
    "scalars": (".5", "5.", "0x10", "010", "0o10", "0b101", "",
                "1,000", "+1",
                "!!str 1", "!!int 1", "!!bool yes", "!!null ~", "!!float 1"),
    "documents": ("empty-string", "anchor", "merge",
                  "block-scalar-literal", "block-scalar-folded", "block-keep",
                  "tab-indent", "bad-indent", "tag-unknown"),
    "reason": "aborts the reference (SIGABRT); an abort is not a behavior",
}

# --------------------------------------------------------------------------
# the error surface
# --------------------------------------------------------------------------
# Three channels, and they are distinct things that happen to share a stream.
# Conflating the levels is exactly the mistake that cost two wrong diagnoses in
# lang03, so they get separate names here:
#
#   CHANNEL (where the failure was detected)  vs  the message text.
#
#   "cli"      argument handling, before any Jsonnet is read.  "ERROR: ..." on
#              stderr, exit 1.  No location, no trace.
#   "static"   parse and static analysis.  "STATIC ERROR: <loc>: ..." where loc
#              is file:line:col or file:line:col-col.  Exit 1.
#   "runtime"  evaluation.  "RUNTIME ERROR: ..." followed by a tab-indented
#              stack trace.  Exit 1.
ERROR_CHANNELS = ("cli", "static", "runtime")

ERROR_PREFIX = {
    "cli": "ERROR: ",
    "static": "STATIC ERROR: ",
    "runtime": "RUNTIME ERROR: ",
}

# Runtime traces are "\t<location>\t<context>", and the last frame has an empty
# context, so it ends in a bare tab -- a trailing-whitespace detail no port
# reproduces by accident and every diff tool hides.  Graded byte-exact.
TRACE_FORMAT = {
    "line_prefix": "\t",
    "field_separator": "\t",
    "last_frame_context_empty": True,
    # -t/--max-trace elides the middle with a lone "..." line, keeping the
    # outermost and innermost frames.
    "elision_marker": "...",
}

EXIT_CODES = {
    "ok": 0,
    "cli_error": 1,
    "static_error": 1,
    "runtime_error": 1,
    # jsonnetfmt --test uses 2 for "would reformat", which is neither success
    # nor an error and is the one exit code a port is likely to collapse into 1.
    "fmt_test_changed": 2,
}

# Inputs asserted not to abort.  The freeze step runs every one against the
# reference and requires exit in (0, 1); anything else means my allowlist is
# wrong and the verifier image must not be built.  This is the machine-checked
# half of the parseYaml exclusion.
ASSERTED_NO_ABORT = {
    "parseyaml_scalars": PARSEYAML_SCALARS,
    "parseyaml_documents": tuple(src for _, src in PARSEYAML_DOCUMENTS),
}

# --------------------------------------------------------------------------
# behavior that is excluded because the reference cannot do it either
# --------------------------------------------------------------------------
# jsonnetfmt --debug-desugaring is broken in 0.20.0 for *every* input:
#
#   $ echo '1+1' > t.jsonnet && jsonnetfmt --debug-desugaring t.jsonnet
#   STATIC ERROR: std.jsonnet:975:21-25: Truncated escape sequence in string
#   literal.
#
# The cause is a double unescape, traced through the 0.20.0 source:
#
#   desugarer.cpp:857   ast->value = jsonnet_string_unescape(...)   // decode once
#   desugarer.cpp:859   ast->tokenKind = LiteralString::DOUBLE
#   formatter.cpp:656   jsonnet_string_unescape(lit->value)         // decode again
#
# EnforceStringStyle early-returns for BLOCK and the two VERBATIM kinds, but
# desugaring has just rewritten every literal to DOUBLE, so nothing stops the
# second decode.  std.jsonnet:975 is `else if ch == '\\' then`: the raw value is
# the two characters \\, the first decode makes it one backslash, and the second
# sees a lone trailing backslash.  desugarFile() splices the stdlib into every
# program as `local std = <stdlib>; ast`, so the stdlib is always in the tree and
# the failure does not depend on the input at all.
#
# The flag is still accepted and still documented in --help, so it stays in
# FORMAT_FLAGS -- a port must accept it -- but its *output* is not graded.
# Requiring the error byte for byte would require a port to reproduce two
# internal accidents at once: that desugaring mutates literals in place before
# the formatter's passes run, and that the two passes each decode.  A port that
# implements the flag correctly would fail, which is the wrong way round.  So:
# accepted, unscored.
#
# The freeze step asserts each of these still fails as described.  An exclusion I
# assert but never check is the same mistake as an allowlist I never check: if a
# later patch fixes the flag, the image build should stop rather than quietly
# grade nothing.
# A JSON or YAML *number literal* that overflows a double aborts both parsers:
#
#   std.parseJson("1e400")   -> SIGABRT      std.parseYaml("1e400") -> SIGABRT
#   std.parseJson("1e309")   -> SIGABRT      std.parseJson("[1e400]") -> SIGABRT
#   std.parseJson("1e308")   -> fine         std.parseJson("1e-400")  -> fine
#
# Underflow is fine; only overflow to infinity aborts.  Note the asymmetry that
# makes this worth stating: the same magnitude written as a *source* literal is a
# clean static error, `1e400` -> exit 1.  So the language surface is unaffected and
# only the two parsers are excluded.
#
# This is a rule rather than a list, which matters: a list of the overflowing
# strings I happened to think of would be exactly the incomplete denylist that the
# parseYaml allowlist exists to avoid.  PARSEYAML_SCALARS is an allowlist for the
# same reason.  The freeze step asserts the boundary in both directions, so if a
# later patch changes where the cliff is, the image build stops.
PARSER_NUMBER_OVERFLOW = {
    "rule": "a JSON/YAML number literal that overflows a double aborts the "
            "reference parser; such inputs are excluded from the graded surface",
    "aborts": ("1e400", "-1e400", "1e309", "1e999999", "[1e400]"),
    "fine": ("1e308", "1e-400", "-1e308", "0e400"),
    "source_literal_is_a_clean_error": ("1e400", "-1e400", "1e309"),
}

ASSERTED_REFERENCE_BUGS = (
    {
        "id": "fmt-debug-desugaring-stdlib-reparse",
        "binary": "jsonnetfmt",
        "argv": ("--debug-desugaring",),
        "source": "1 + 1\n",
        "expect_exit": 1,
        "expect_stderr_contains":
            "STATIC ERROR: std.jsonnet:975:21-25: Truncated escape sequence",
        "consequence": "the --debug-desugaring output surface is not graded",
    },
)

# --------------------------------------------------------------------------
# how families combine into the behavioural score
# --------------------------------------------------------------------------
# The behavioural score is a weighted mean of per-family pass *rates*, not the
# fraction of all cases that pass.  The reason is mechanical: the operator matrix
# is 7 types x 19 operators = 931 cases exercising one code path, the binary-op
# type dispatcher.  Under per-case scoring it would carry 45% of the behavioural
# score, so a port with a correct dispatcher and nothing else would out-score a
# port with everything else and a wrong dispatcher.  Weighting by family
# decouples "how much of the language does this cover" from "how many inputs did
# I happen to enumerate", and lets the matrix stay exhaustive -- which is what
# makes it useful as a diagnostic -- without buying influence by being large.
#
# Weights are judgment, and they are stated here so they are arguable rather than
# emergent.  The rough rule: a family's weight tracks how much of a *port's work*
# it represents.  The evaluator core (numbers, objects, laziness, operators,
# errors, stdlib) is most of the work; the CLI and the formatter are each a real
# subsystem; the upstream suite gets the largest single share because it is the
# only family I did not write, so it is the only one that can fail in a way I did
# not anticipate.
FAMILY_WEIGHTS = {
    # evaluator core -- 0.46
    "num": 0.09,
    "obj": 0.07,
    "lazy": 0.05,
    "op": 0.06,
    "str": 0.05,
    "stdlib": 0.09,
    "format": 0.02,
    "parsejson": 0.01,
    "parseyaml": 0.02,
    # diagnostics -- 0.12.  Split three ways because the channels are produced by
    # different code: argv handling, the static analyser, and the interpreter.
    "err-static": 0.04,
    "err-runtime": 0.04,
    "err-trace": 0.04,
    # subsystems -- 0.17
    "imports": 0.04,
    "cli": 0.05,
    "out": 0.03,
    "fmtr": 0.05,
    # the suite I did not write -- 0.25.  There was a third op here,
    # --debug-desugaring, dropped for the reason in ASSERTED_REFERENCE_BUGS; its
    # 0.05 went to upstream-eval, which is the family that actually exercises the
    # evaluator over programs I did not write.
    "upstream-eval": 0.18,
    "upstream-fmt": 0.07,
}

# Families that only exist when the pristine tree is available.  Scoring must not
# silently renormalize around a missing family: if the verifier cannot produce
# these, that is a verifier defect, not a 0.75-weighted grade.
UPSTREAM_FAMILIES = ("upstream-eval", "upstream-fmt")

# --------------------------------------------------------------------------
# self-check
# --------------------------------------------------------------------------


def _self_check() -> list[str]:
    """Internal consistency.  Cheap, and it runs on import in the freeze step.

    This catches the failure mode that produced five gate bugs in lang03: a
    requirement stated in two places drifting apart.  It cannot catch a table
    that disagrees with the reference -- that is what verify_against_reference is
    for.
    """
    errs: list[str] = []

    if len(set(STDLIB_PUBLIC)) != len(STDLIB_PUBLIC):
        errs.append("STDLIB_PUBLIC has duplicates")
    if list(STDLIB_PUBLIC) != sorted(STDLIB_PUBLIC):
        errs.append("STDLIB_PUBLIC is not sorted")
    if not STDLIB_NATIVE <= set(STDLIB_PUBLIC):
        errs.append("STDLIB_NATIVE has names outside STDLIB_PUBLIC: "
                    f"{sorted(STDLIB_NATIVE - set(STDLIB_PUBLIC))}")
    if len(STDLIB_PUBLIC) != 129:
        errs.append(f"expected 129 public stdlib names, got "
                    f"{len(STDLIB_PUBLIC)}")
    if len(STDLIB_NATIVE) != 40:
        errs.append(f"expected 40 native names, got {len(STDLIB_NATIVE)}")
    if not STDLIB_NATIVE_SHADOWING <= STDLIB_NATIVE:
        errs.append("STDLIB_NATIVE_SHADOWING has names outside STDLIB_NATIVE: "
                    f"{sorted(STDLIB_NATIVE_SHADOWING - STDLIB_NATIVE)}")
    for name in STDLIB_NON_FUNCTION:
        if name not in STDLIB_PUBLIC:
            errs.append(f"STDLIB_NON_FUNCTION name not public: {name}")
    for name in STDLIB_DUNDER:
        if not name.startswith("__"):
            errs.append(f"dunder name without prefix: {name}")
        if name in STDLIB_PUBLIC:
            errs.append(f"dunder name listed as public: {name}")

    for label, table in (("EVAL_FLAGS", EVAL_FLAGS),
                         ("FORMAT_FLAGS", FORMAT_FLAGS)):
        shorts: dict[str, str] = {}
        for flag, spec in table.items():
            if not flag.startswith("--"):
                errs.append(f"{label}: {flag} is not a long flag")
            kind = spec.get("arg")
            if kind not in ("none", "int", "dir", "file", "var", "varfile",
                            "choice"):
                errs.append(f"{label}: {flag} has unknown arg kind {kind!r}")
            if kind == "choice" and not spec.get("choices"):
                errs.append(f"{label}: {flag} is a choice with no choices")
            if kind != "choice" and spec.get("choices"):
                errs.append(f"{label}: {flag} has choices but kind {kind!r}")
            if spec.get("negatable") and kind != "none":
                errs.append(f"{label}: {flag} is negatable but takes an arg")
            short = spec.get("short")
            if short is not None:
                if not (len(short) == 2 and short[0] == "-"):
                    errs.append(f"{label}: bad short flag {short!r}")
                if short in shorts:
                    errs.append(f"{label}: short {short} used by "
                                f"{shorts[short]} and {flag}")
                shorts[short] = flag

    # Both binaries must agree on the flags they share, or a port could
    # reasonably implement one and be graded against the other.
    for flag in set(EVAL_FLAGS) & set(FORMAT_FLAGS):
        a, b = EVAL_FLAGS[flag], FORMAT_FLAGS[flag]
        if a.get("short") != b.get("short") or a.get("arg") != b.get("arg"):
            errs.append(f"shared flag {flag} differs between binaries: "
                        f"{a} vs {b}")

    if len({src for _, src in NUMBER_CASES}) != len(NUMBER_CASES):
        errs.append("NUMBER_CASES has duplicate sources")
    for src, want in NUMBER_CASES:
        if not want or want.strip() != want:
            errs.append(f"NUMBER_CASES: bad expectation for {src!r}")
        if "e" in want:
            _, _, exp = want.partition("e")
            if exp[0] not in "+-" or len(exp) - 1 < 2:
                errs.append(f"NUMBER_CASES: exponent not signed/2-digit in "
                            f"{want!r}")

    names = [n for n, _ in PARSEYAML_DOCUMENTS]
    if len(set(names)) != len(names):
        errs.append("PARSEYAML_DOCUMENTS has duplicate names")
    if len(set(PARSEYAML_SCALARS)) != len(PARSEYAML_SCALARS):
        errs.append("PARSEYAML_SCALARS has duplicates")
    overlap = set(PARSEYAML_SCALARS) & set(PARSEYAML_EXCLUDED["scalars"])
    if overlap:
        errs.append(f"scalars both allowed and excluded: {sorted(overlap)}")
    doc_overlap = set(names) & set(PARSEYAML_EXCLUDED["documents"])
    if doc_overlap:
        errs.append(f"documents both allowed and excluded: "
                    f"{sorted(doc_overlap)}")

    if set(ERROR_PREFIX) != set(ERROR_CHANNELS):
        errs.append("ERROR_PREFIX and ERROR_CHANNELS disagree")
    # "ERROR: " is a prefix of the other two, so any code that classifies by
    # prefix has to test the longer ones first.  Assert the ordering hazard
    # exists rather than letting a later reader assume it does not.
    if not all(p.endswith("ERROR: ") for p in ERROR_PREFIX.values()):
        errs.append("an error prefix does not end in 'ERROR: '")

    total = sum(FAMILY_WEIGHTS.values())
    # Exact-sum-to-one on floats is not a thing; 1e-9 is far tighter than any
    # weight I would deliberately choose and loose enough for 19 additions.
    if abs(total - 1.0) > 1e-9:
        errs.append(f"FAMILY_WEIGHTS sums to {total!r}, not 1.0")
    if any(w <= 0 for w in FAMILY_WEIGHTS.values()):
        errs.append("a family weight is non-positive")
    missing_upstream = [f for f in UPSTREAM_FAMILIES if f not in FAMILY_WEIGHTS]
    if missing_upstream:
        errs.append(f"UPSTREAM_FAMILIES not weighted: {missing_upstream}")

    return errs


def check_family_coverage(families: set[str]) -> list[str]:
    """Weights and case list families must be the same set, in both directions.

    A family in the case list with no weight scores zero-weighted -- it runs, it
    fails, and nothing happens.  A weight with no family is worse: the weights no
    longer sum to 1 over what actually ran, so every score silently shifts.
    cases.assemble() calls this, so neither can be introduced quietly.
    """
    errs: list[str] = []
    unweighted = sorted(families - set(FAMILY_WEIGHTS))
    if unweighted:
        errs.append(f"case list families with no weight: {unweighted}")
    unused = sorted(set(FAMILY_WEIGHTS) - families)
    if unused:
        errs.append(f"weighted families not in the case list: {unused}")
    return errs


def check_against_reference(state_a: str) -> list[str]:
    """STDLIB_NATIVE against the C++ table it claims to describe.

    _self_check only proves the tables agree with each other, which is how the
    native list stayed wrong: it was internally consistent, exported to the
    contract, asserted to have exactly the length it had, and describing a set
    the reference does not use.  A count is not a check when the count is written
    from the same mistaken list.

    So this reads `jsonnet_builtin_decl` out of core/desugarer.cpp and compares.
    Called from the freeze step, where State A is present by construction; skipped
    with a note rather than an error when the tree is not there, since the graders
    also run in contexts that have no C++ source.
    """
    import os
    import re
    path = os.path.join(state_a, "core", "desugarer.cpp")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8", errors="replace") as f:
        src = f.read()
    decls = set(re.findall(r'case\s+\d+:\s*return\s*\{U"([A-Za-z0-9_]+)"', src))
    if not decls:
        return [f"could not parse jsonnet_builtin_decl out of {path}"]
    errs: list[str] = []
    # thisFile is injected as a field, not declared as a builtin, so it is the
    # one legitimate difference between the two sets.
    claimed = set(STDLIB_NATIVE) - set(STDLIB_NON_FUNCTION)
    missing = sorted(decls - claimed)
    if missing:
        errs.append(f"builtins declared in desugarer.cpp but absent from "
                    f"STDLIB_NATIVE: {missing}. The desugarer overwrites the "
                    f"std.jsonnet body of every declared builtin, so a port must "
                    f"implement each of these natively")
    extra = sorted(claimed - decls)
    if extra:
        errs.append(f"STDLIB_NATIVE claims names that desugarer.cpp does not "
                    f"declare: {extra}")
    errs += _check_shadow_reach(state_a)
    return errs


def _check_shadow_reach(state_a: str) -> list[str]:
    """The "43 of 102" figure, recomputed from the preserved std.jsonnet.

    Both numbers appear in prose -- in the comment above STDLIB_NATIVE and in
    instruction.md -- as the argument for why the seven shadowed names are not a
    corner case.  The reachability walk is cheap and std.jsonnet is byte-pinned,
    so the figures can be facts about the file rather than recollections of it.
    """
    import os
    import re
    path = os.path.join(state_a, "stdlib", "std.jsonnet")
    if not os.path.isfile(path):
        return []
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
    field = re.compile(r'^  ([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*::')
    starts = [(i, m.group(1)) for i, ln in enumerate(lines)
              if (m := field.match(ln))]
    bodies: dict[str, str] = {}
    for k, (i, name) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        bodies[name] = "\n".join(lines[i:end])

    errs: list[str] = []
    if len(starts) != STDLIB_FIELD_COUNT:
        errs.append(f"std.jsonnet defines {len(starts)} fields, but the spec and "
                    f"instruction.md say {STDLIB_FIELD_COUNT}")

    refs = {n: {r for r in re.findall(r'std\.([A-Za-z_][A-Za-z0-9_]*)', b)
                if r in bodies and r != n}
            for n, b in bodies.items()}
    reach = {n for n in bodies if n in STDLIB_NATIVE_SHADOWING}
    changed = True
    while changed:
        changed = False
        for name in bodies:
            if name not in reach and refs[name] & reach:
                reach.add(name)
                changed = True
    if len(reach) != STDLIB_SHADOW_REACH:
        errs.append(f"{len(reach)} std.jsonnet fields transitively reach one of "
                    f"the {len(STDLIB_NATIVE_SHADOWING)} shadowed names, but the "
                    f"spec and instruction.md say {STDLIB_SHADOW_REACH}")
    return errs


def check_or_die(state_a: str | None = None) -> None:
    errs = _self_check()
    if state_a:
        errs += check_against_reference(state_a)
    if errs:
        for e in errs:
            print(f"spec self-check: {e}")
        raise SystemExit(f"spec.py failed self-check ({len(errs)} problems)")


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------


def as_dict() -> dict:
    """The whole spec as plain data, for source-contract.json.

    The contract shipped to the submission is generated from this, so the
    document a submission reads and the tables the verifier grades against
    cannot disagree.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "upstream": UPSTREAM,
        "upstream_version": UPSTREAM_VERSION,
        "stdlib_sha256": STDLIB_SHA256,
        "eval_flags": EVAL_FLAGS,
        "format_flags": FORMAT_FLAGS,
        "argv_rules": ARGV_RULES,
        "stdlib": {
            "public": list(STDLIB_PUBLIC),
            "native": sorted(STDLIB_NATIVE),
            # Exported separately because it is the subset a port gets wrong
            # silently: these names have a Jsonnet body in the preserved
            # std.jsonnet that the reference overwrites and never runs, so
            # embedding the library and evaluating it as written produces
            # different output from the reference on their error paths.
            "native_shadowing": sorted(STDLIB_NATIVE_SHADOWING),
            "non_function": STDLIB_NON_FUNCTION,
            "dunder": list(STDLIB_DUNDER),
        },
        "number_rule": NUMBER_RULE,
        "number_cases": [list(p) for p in NUMBER_CASES],
        "number_tie_cases": list(NUMBER_TIE_CASES),
        "number_nonfinite": NUMBER_NONFINITE,
        "parseyaml": {
            "scalars": list(PARSEYAML_SCALARS),
            "documents": [list(p) for p in PARSEYAML_DOCUMENTS],
            "excluded": {k: (list(v) if isinstance(v, tuple) else v)
                         for k, v in PARSEYAML_EXCLUDED.items()},
        },
        "errors": {
            "channels": list(ERROR_CHANNELS),
            "prefix": ERROR_PREFIX,
            "trace_format": TRACE_FORMAT,
            "exit_codes": EXIT_CODES,
        },
        # The two surfaces 0.20.0 cannot do either, exported because a submission
        # is entitled to know them.  They were asserted at freeze time and stated
        # in instruction.md while the machine-readable contract said nothing, so
        # "behave exactly as it does today" was the only reading available here --
        # and a port that implemented --debug-desugaring *correctly* would have
        # been the one that looked wrong.  parseyaml.excluded above is the same
        # idea for a third surface and predates these two.
        "excluded_surfaces": {
            "parser_number_overflow": {
                k: (list(v) if isinstance(v, tuple) else v)
                for k, v in PARSER_NUMBER_OVERFLOW.items()
            },
            "reference_bugs": [
                {k: (list(v) if isinstance(v, tuple) else v)
                 for k, v in bug.items()}
                for bug in ASSERTED_REFERENCE_BUGS
            ],
            "note":
                "Each is excluded from the graded surface because the reference "
                "aborts or errs there, not because it is hard.  The flag in "
                "reference_bugs must still be accepted -- rejecting it as an "
                "unrecognised argument is graded and wrong -- while its output "
                "is not compared.  parser_number_overflow is stated as a rule "
                "rather than a list: an enumeration of the overflowing literals "
                "anyone happened to think of is the incomplete denylist that "
                "parseyaml.scalars is an allowlist to avoid.",
        },
    }


if __name__ == "__main__":
    import json

    check_or_die()
    doc = as_dict()
    print(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False))

