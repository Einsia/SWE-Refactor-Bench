"""The graded inputs.

Every case is a directory of files plus an argv, run against one of the two
binaries, with stdout, stderr and exit status captured byte-exact.  Nothing here
holds an expected answer: the freeze step produces those by running the
reference at verifier-image build time, before any submission exists.  That
split is what makes the reference's quirks gradeable as behavior instead of
requiring me to decide which ones are correct.

The generator is deterministic -- same list, same order, same ids, every run --
because the freeze digest is compared across builds and any instability there
would look like a submission failure.  No randomness, no dict iteration over
unsorted keys, no time or path dependence.

Families are chosen so that a failure localizes.  A port with a broken number
formatter fails `num*` and the manifest families; a port that skipped
std.manifestTomlEx fails one stdlib case; a port whose argv handling is
conventional rather than jsonnet's fails `argv*` and nothing else.  One
all-or-nothing verdict would tell a submission far less.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict

import spec

# Written into every case directory that needs the preserved stdlib visible as
# an import target; see the `stdlib` family.
MAIN = "main.jsonnet"


@dataclass(frozen=True)
class Case:
    """One graded invocation.

    files:  relative path -> contents, written into a fresh directory
    argv:   arguments after the binary name, relative to that directory
    stdin:  text fed to the process, or None to give it an empty stdin
    """
    cid: str
    family: str
    binary: str
    argv: tuple[str, ...]
    files: tuple[tuple[str, str], ...] = ()
    stdin: str | None = None
    note: str = ""
    # A directory from the pristine State A tree, copied into the case directory
    # before `files` are written.  Used by the upstream families, whose programs
    # import their siblings and so need the real directory around them.  The
    # copy always comes from the verifier's own original.tar.gz, never from the
    # submission -- otherwise editing test_suite/ would edit the tests.
    source_dir: str = ""

    def key(self) -> str:
        """Stable identity of the *input*, independent of any expectation."""
        h = hashlib.sha256()
        h.update(self.binary.encode())
        for a in self.argv:
            h.update(b"\x00")
            h.update(a.encode())
        for path, body in self.files:
            h.update(b"\x01")
            h.update(path.encode())
            h.update(b"\x02")
            h.update(body.encode())
        if self.stdin is not None:
            h.update(b"\x03")
            h.update(self.stdin.encode())
        if self.source_dir:
            h.update(b"\x04")
            h.update(self.source_dir.encode())
        return h.hexdigest()[:16]


def ev(cid: str, family: str, src: str, *extra: str, note: str = "") -> Case:
    """A jsonnet evaluation of `src` held in a file.

    A file rather than -e because -e reports locations as <cmdline>, and file
    locations exercise more of the error surface.
    """
    return Case(cid=cid, family=family, binary="jsonnet",
                argv=tuple(extra) + (MAIN,),
                files=((MAIN, src),), note=note)


def ev_exec(cid: str, family: str, src: str, *extra: str,
            note: str = "") -> Case:
    """A jsonnet evaluation via -e, so locations read <cmdline>."""
    return Case(cid=cid, family=family, binary="jsonnet",
                argv=("-e",) + tuple(extra) + ("--", src), note=note)


def fm(cid: str, family: str, src: str, *extra: str, note: str = "") -> Case:
    """A jsonnetfmt run over a file."""
    return Case(cid=cid, family=family, binary="jsonnetfmt",
                argv=tuple(extra) + (MAIN,),
                files=((MAIN, src),), note=note)


def cli(cid: str, family: str, binary: str, *argv: str,
        files: tuple[tuple[str, str], ...] = (), stdin: str | None = None,
        note: str = "") -> Case:
    """A raw invocation, for argument handling and diagnostics."""
    return Case(cid=cid, family=family, binary=binary, argv=tuple(argv),
                files=files, stdin=stdin, note=note)


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------
def numbers() -> list[Case]:
    """The rule from spec.NUMBER_RULE, one case per branch and then some.

    This family is deliberately large.  Every other family's output contains
    numbers, so a port with a broken formatter fails broadly -- but only this
    family says *why*, and the tie cases in particular are unreachable by
    sampling (see spec.NUMBER_TIE_CASES).
    """
    out: list[Case] = []
    for i, (src, _expected) in enumerate(spec.NUMBER_CASES):
        # The expectation in spec.NUMBER_CASES is checked against the reference
        # at freeze time; here only the input is used, so the two can never
        # disagree about what was graded.
        out.append(ev_exec(f"num-case-{i:02d}", "num", src))
    for i, lit in enumerate(spec.NUMBER_TIE_CASES):
        out.append(ev_exec(f"num-tie-{i:02d}", "num", lit,
                           note="round-half-even at the 18th digit"))

    # Arithmetic that lands on the interesting values rather than naming them,
    # so a port cannot special-case the literals in spec.NUMBER_CASES.
    # "0.1 + 0.2" and "2 / 3" belong here by rights but are already in
    # spec.NUMBER_CASES; grading the same input twice would inflate the count
    # without adding evidence, and assemble() rejects duplicate inputs.
    computed = [
        "0.1 * 3", "1 - 0.9", "1 / 7", "10 / 3",
        "1e308 * 10 / 10", "2 ^ 53", "2 ^ 53 + 1", "(2 ^ 53) * 1.0",
        "1 / 1024", "3 / 4096", "1 / (2 ^ 25)", "std.pow(2, -25)",
        "std.exp(1)", "std.log(2)", "std.sqrt(2)", "std.sqrt(1e-300)",
        "std.sin(1)", "std.cos(1)", "std.tan(1)", "std.atan(1) * 4",
        "std.floor(-0.5)", "std.ceil(-0.5)", "std.round(0.5)",
        "std.round(1.5)", "std.round(2.5)", "std.round(-0.5)",
        "std.abs(-0.0)", "std.sign(-0.0)", "std.exponent(1)",
        "std.mantissa(1)", "std.exponent(0.1)", "std.mantissa(0.1)",
        "std.modulo(7, 3)", "7 % 3", "7.5 % 2", "-7 % 3",
        "std.clamp(1.5, 0, 1)", "std.max(0.1, 0.2)", "std.min(-0.0, 0.0)",
        "std.sum([0.1, 0.2, 0.3])", "std.parseInt('42')",
        "std.parseInt('-42')", "std.parseHex('ff')", "std.parseOctal('777')",
        "std.parseHex('FFFFFFFFFFFFFFFF')",
    ]
    for i, src in enumerate(computed):
        out.append(ev_exec(f"num-computed-{i:02d}", "num", src))

    # Powers of ten and two across the whole exponent range, as literals.  These
    # sweep both %g thresholds and the %.0f path at every magnitude.
    for k in range(-320, 309, 8):
        out.append(ev_exec(f"num-pow10-{k:+04d}", "num", f"1e{k}"))
    for k in range(-1074, 1024, 29):
        out.append(ev_exec(f"num-pow2-{k:+05d}", "num",
                           f"std.pow(2, {k})"))

    # Numbers in context: the manifesters and the formatter all re-render them,
    # and a port that fixes only the top-level writer fails here.
    ctx = [
        ("array", "[0.1, 1e100, -0.0, 1e-5]"),
        ("object", "{a: 0.1, b: 1e100, c: 1e-5}"),
        ("nested", "{a: [{b: 0.1}], c: {d: [1e-5, 2 ^ 53]}}"),
        ("key", "{[std.toString(0.1)]: 1}"),
        ("tostring", "std.toString([0.1, 1e100, 1e-5])"),
        ("manifest-json", "std.manifestJson([0.1, 1e-5])"),
        ("manifest-json-min", "std.manifestJsonMinified([0.1, 1e-5])"),
        ("manifest-yaml", "std.manifestYamlDoc([0.1, 1e-5])"),
        ("manifest-python", "std.manifestPython([0.1, 1e-5])"),
        ("manifest-toml", "std.manifestToml({a: 0.1, b: 1e-5})"),
        ("manifest-ini", "std.manifestIni({sections: {s: {k: 0.1}}})"),
        ("manifest-xml", "std.manifestXmlJsonml(['a', {}, '0.1'])"),
        ("format-d", "std.format('%d', 0.1)"),
        ("format-f", "std.format('%.3f', 0.1)"),
        ("format-e", "std.format('%e', 0.1)"),
        ("format-g", "std.format('%g', 0.1)"),
        ("format-s", "std.format('%s', 0.1)"),
        ("join", "std.join(',', [std.toString(x) for x in [0.1, 1e-5]])"),
        ("string-mult", "'%s' % 0.1"),
        ("equality", "[0.1 + 0.2 == 0.3, 1e100 == 1e100 + 1]"),
        ("sort", "std.sort([1e-5, 0.1, -0.0, 1e100, 2 ^ 53])"),
        ("set", "std.set([0.1, 0.1, 1e-5])"),
    ]
    for name, src in ctx:
        out.append(ev_exec(f"num-ctx-{name}", "num", src))

    # -y and -S change how the top level is written but not how numbers are
    # rendered inside it; a port that reimplements each writer separately can
    # get one right and the other wrong.
    out.append(cli("num-yaml-stream", "num", "jsonnet", "-y", "-e", "--",
                   "[[0.1, 1e-5], [1e100]]"))
    out.append(cli("num-string-out", "num", "jsonnet", "-S", "-e", "--",
                   "std.toString(0.1)"))
    for src, _ in spec.NUMBER_NONFINITE.items():
        cid = "num-nonfinite-" + src.replace(" ", "").replace("/", "div") \
            .replace("(", "").replace(")", "").replace(".", "-")
        out.append(ev_exec(cid, "num", src,
                           note="leaves the finite doubles; graded as an error"))
    return out


# --------------------------------------------------------------------------
# the object model
# --------------------------------------------------------------------------
OBJECT_SRC: tuple[tuple[str, str], ...] = (
    ("empty", "{}"),
    ("simple", "{a: 1, b: 2}"),
    # Field order in the output is sorted, not source order.  Worth pinning
    # because a port using an insertion-ordered dictionary passes the simple
    # cases and fails here.
    ("sorted-fields", "{z: 1, a: 2, m: 3, B: 4, _q: 5, '0': 6}"),
    ("hidden", "{a: 1, b:: 2}"),
    ("hidden-only", "{a:: 1}"),
    ("forced-visible", "{a::: 1}"),
    ("hidden-then-visible", "{a:: 1} + {a: 2}"),
    ("visible-then-hidden", "{a: 1} + {a:: 2}"),
    ("forced-over-hidden", "{a:: 1} + {a::: 2}"),
    ("computed-name", "{['a' + 'b']: 1}"),
    ("computed-hidden", "{['a']:: 1, b: $.a}"),
    ("computed-dup", "local k = 'a'; {[k]: 1, [k]: 2}"),
    ("computed-null-name", "{[null]: 1, a: 2}"),
    ("field-plus", "{a: [1]} + {a+: [2]}"),
    ("field-plus-string", "{a: 'x'} + {a+: 'y'}"),
    ("field-plus-object", "{a: {p: 1}} + {a+: {q: 2}}"),
    ("field-plus-number", "{a: 1} + {a+: 2}"),
    ("field-plus-no-left", "{a+: [1]}"),
    ("super", "{a: 1, b: self.a} + {a: 2}"),
    ("super-explicit", "local o = {a: 1} + {a: super.a + 1}; o"),
    ("super-nested", "{f: {a: 1}} + {f+: {b: super.a + 1}}"),
    ("super-in-hidden", "{a:: 1} + {a:: super.a + 1, b: self.a}"),
    ("super-missing", "{} + {a: super.a}"),
    ("self-recursive", "{a: 1, b: self.a + 1, c: self.b + 1}"),
    ("dollar", "{a: 1, o: {b: $.a}}"),
    ("dollar-nested", "{a: 1, o: {p: {q: $.a}}}"),
    ("local-in-object", "{local x = 1, a: x, b: x + 1}"),
    ("local-shadow", "local x = 1; {local x = 2, a: x}"),
    ("assert-pass", "{assert self.a == 1, a: 1}"),
    ("assert-fail", "{assert self.a == 2, a: 1}"),
    ("assert-message", "{assert self.a == 2 : 'bad a', a: 1}"),
    ("assert-inherited", "{assert self.a > 0, a: 1} + {a: -1}"),
    ("objcomp", "{[k]: k for k in ['a', 'b']}"),
    ("objcomp-value", "{[k]: std.length(k) for k in ['a', 'bb']}"),
    ("objcomp-cond", "{[k]: 1 for k in ['a', 'b'] if k != 'a'}"),
    ("objcomp-nested", "{[a + b]: 1 for a in ['x'] for b in ['y', 'z']}"),
    ("objcomp-dollar", "{a: 1, o: {[k]: $.a for k in ['b']}}"),
    ("objcomp-dup", "{[k]: 1 for k in ['a', 'a']}"),
    ("in-super", "{a: 1} + {b: 'a' in super}"),
    ("comparison", "[{a: 1} == {a: 1}, {a: 1} == {a: 2}]"),
    ("comparison-hidden", "{a: 1, b:: 2} == {a: 1}"),
    ("deep-merge", "{a: {b: {c: 1}}} + {a+: {b+: {d: 2}}}"),
    ("merge-chain", "{a: 1} + {a: 2} + {a: 3}"),
    ("plus-chain", "{a: [0]} + {a+: [1]} + {a+: [2]}"),
    ("mixin-function", "local m(o) = o + {b: 2}; m({a: 1})"),
    ("object-of-functions", "{f(x): x + 1, a: self.f(1)}"),
    ("keyword-fields", "{if: 1, then: 2, else: 3, local: 4, super: 5}"),
    ("unicode-field", "{'\\u00e9': 1, 'caf\\u00e9': 2}"),
    ("empty-field-name", "{'': 1}"),
    ("numeric-string-fields", "{'10': 1, '9': 2, '1': 3}"),
)


def object_model() -> list[Case]:
    out = [ev(f"obj-{name}", "obj", src) for name, src in OBJECT_SRC]
    # objectHas/objectFields over the same shapes: the visibility rules are the
    # part of the object model most likely to be simplified in a port, and
    # reflection is where a simplification becomes visible.
    shapes = ("{a: 1, b:: 2, c::: 3}", "{a:: 1} + {a: 2}",
              "{a: 1} + {a:: 2}", "{local x = 1, a: x}")
    probes = ("std.objectFields", "std.objectFieldsAll", "std.objectValues",
              "std.objectValuesAll", "std.objectKeysValues",
              "std.objectKeysValuesAll")
    for i, shape in enumerate(shapes):
        for probe in probes:
            out.append(ev_exec(f"obj-refl-{i}-{probe.split('.')[1]}", "obj",
                               f"{probe}({shape})"))
        for fn in ("std.objectHas", "std.objectHasAll"):
            out.append(ev_exec(f"obj-has-{i}-{fn.split('.')[1]}", "obj",
                               f"[{fn}({shape}, k) for k in ['a','b','c','x']]"))
    return out


# --------------------------------------------------------------------------
# laziness, recursion, functions
# --------------------------------------------------------------------------
LAZY_SRC: tuple[tuple[str, str], ...] = (
    ("unused-error", "local x = error 'never'; 1"),
    ("unused-field", "{a: 1, b: error 'never'}.a"),
    ("unused-array", "[1, error 'never'][0]"),
    ("lazy-arg", "local f(x) = 1; f(error 'never')"),
    ("lazy-if", "if true then 1 else error 'never'"),
    ("lazy-and", "false && error 'never'"),
    ("lazy-or", "true || error 'never'"),
    ("thunk-once", "local x = 1; [x, x, x]"),
    ("self-reference", "local o = {a: 1, b: o.a}; o"),
    ("mutual-recursion",
     "local ev(n) = if n == 0 then true else od(n - 1);\n"
     "local od(n) = if n == 0 then false else ev(n - 1);\n"
     "[ev(10), od(10)]"),
    ("fib", "local f(n) = if n < 2 then n else f(n-1) + f(n-2); f(20)"),
    ("ackermann-small",
     "local a(m, n) = if m == 0 then n + 1\n"
     "  else if n == 0 then a(m - 1, 1)\n"
     "  else a(m - 1, a(m, n - 1));\n"
     "a(2, 3)"),
    ("y-combinator",
     "local Y(f) = (function(x) x(x))(function(x) f(function(v) x(x)(v)));\n"
     "local fact = Y(function(self) function(n)"
     " if n <= 1 then 1 else n * self(n - 1));\n"
     "fact(10)"),
    ("infinite-list-take",
     "local nat(n) = [n, function() nat(n + 1)];\n"
     "local take(l, k) = if k == 0 then [] else [l[0]] + take(l[1](), k - 1);\n"
     "take(nat(0), 8)"),
    ("default-arg", "local f(a, b = a + 1) = [a, b]; f(1)"),
    ("default-arg-lazy", "local f(a, b = error 'never') = a; f(1)"),
    ("named-arg", "local f(a, b) = [a, b]; f(b = 2, a = 1)"),
    ("named-and-positional", "local f(a, b, c) = [a,b,c]; f(1, c = 3, b = 2)"),
    ("named-duplicate", "local f(a) = a; f(1, a = 2)"),
    ("missing-arg", "local f(a, b) = a; f(1)"),
    ("too-many-args", "local f(a) = a; f(1, 2)"),
    ("unknown-named-arg", "local f(a) = a; f(z = 1)"),
    ("function-value", "local f(x) = x; std.type(f)"),
    ("function-in-output", "{f: function(x) x}"),
    ("closure-capture",
     "local mk(n) = function() n;\n"
     "[mk(1)(), mk(2)()]"),
    ("closure-in-comprehension", "[function() i for i in [1, 2]][0]()"),
    ("std-in-lambda", "std.map(function(x) x * 2, [1, 2, 3])"),
    ("higher-order", "std.foldl(function(a, b) a + b, [1, 2, 3], 0)"),
    ("tailstrict", "local f(x) = x; f(1) tailstrict"),
    ("stack-overflow", "local f(n) = f(n + 1); f(0)"),
    ("deep-recursion-ok",
     "local f(n) = if n == 0 then 0 else f(n - 1); f(400)"),
)


def laziness() -> list[Case]:
    out = [ev(f"lazy-{name}", "lazy", src) for name, src in LAZY_SRC]
    # -s bounds the stack, and the bound is observable both ways.
    out.append(cli("lazy-max-stack-small", "lazy", "jsonnet", "-s", "20",
                   "-e", "--",
                   "local f(n) = if n == 0 then 0 else f(n - 1); f(100)"))
    out.append(cli("lazy-max-stack-enough", "lazy", "jsonnet", "-s", "500",
                   "-e", "--",
                   "local f(n) = if n == 0 then 0 else f(n - 1); f(100)"))
    return out


# --------------------------------------------------------------------------
# strings
# --------------------------------------------------------------------------
# Jsonnet strings are sequences of Unicode code points, and the reference's
# encoder escapes non-ASCII on output while its decoder accepts \u pairs.  That
# round trip is where a port on a UTF-16 runtime gets into trouble: C# strings
# are UTF-16, so std.length, std.codepoint, std.char and std.substr all have to
# count code points rather than chars.  Astral-plane characters are the whole
# hazard, so they appear here in every form.
STRING_SRC: tuple[tuple[str, str], ...] = (
    ("plain", "'abc'"),
    ("double", '"abc"'),
    ("empty", "''"),
    ("escape-basic", r"'a\tb\nc\rd\\e\'f'"),
    ("escape-double", r'"a\"b"'),
    ("escape-slash", r"'a\/b'"),
    ("escape-backspace-formfeed", r"'a\bb\fc'"),
    ("escape-u", "'\\u0041\\u00e9\\u4e2d'"),
    # Written as Jsonnet \u escapes rather than literal characters, so the
    # case text is exactly what the reference parses and this file stays ASCII.
    ("escape-u-zero", "'\\u0000'"),
    ("escape-u-surrogate-pair", "'\\ud83d\\ude00'"),
    ("escape-u-lone-high", r"'\ud83d'"),
    ("escape-u-lone-low", r"'\ude00'"),
    ("literal-astral", "'\U0001f600'"),
    ("literal-bmp", "'中文'"),
    ("literal-combining", "'é'"),
    ("verbatim", "@'a\\tb'"),
    ("verbatim-double", '@"a\\tb"'),
    ("verbatim-quote", "@'a''b'"),
    ("block", "|||\n  one\n  two\n|||"),
    ("block-indent", "|||\n    one\n  two\n|||"),
    ("block-blank-line", "|||\n  one\n\n  two\n|||"),
    ("block-trailing", "|||\n  one\n|||"),
    ("block-chomp", "|||-\n  one\n|||"),
    ("block-tabs", "|||\n\tone\n|||"),
    ("concat", "'a' + 'b'"),
    ("concat-number", "'a' + 1"),
    ("number-concat", "1 + 'a'"),
    ("concat-object", "'a' + {b: 1}"),
    ("concat-array", "'a' + [1]"),
    ("concat-null", "'a' + null"),
    ("concat-bool", "'a' + true"),
    ("index", "'abc'[1]"),
    ("index-astral", "'\U0001f600ab'[0]"),
    ("slice", "'abcdef'[1:4]"),
    ("slice-step", "'abcdef'[::2]"),
    ("slice-negative", "'abcdef'[-2:]"),
    ("length-ascii", "std.length('abc')"),
    ("length-bmp", "std.length('中文')"),
    ("length-astral", "std.length('\U0001f600')"),
    ("length-combining", "std.length('é')"),
    ("codepoint", "std.codepoint('\U0001f600')"),
    ("char", "std.char(128512)"),
    ("char-zero", "std.char(0)"),
    ("char-max", "std.char(1114111)"),
    ("char-surrogate", "std.char(55296)"),
    ("stringChars-astral", "std.stringChars('\U0001f600a')"),
    ("substr-astral", "std.substr('\U0001f600ab', 1, 2)"),
    ("encodeUTF8", "std.encodeUTF8('\U0001f600')"),
    ("decodeUTF8", "std.decodeUTF8([240, 159, 152, 128])"),
    ("decodeUTF8-invalid", "std.decodeUTF8([255])"),
    ("md5-ascii", "std.md5('abc')"),
    ("md5-astral", "std.md5('\U0001f600')"),
    ("md5-empty", "std.md5('')"),
    ("base64-string", "std.base64('abc')"),
    ("base64-astral", "std.base64('\U0001f600')"),
    ("base64-bytes", "std.base64([1, 2, 3])"),
    ("base64Decode", "std.base64Decode('YWJj')"),
    ("base64DecodeBytes", "std.base64DecodeBytes('YWJj')"),
    ("escapeStringJson", "std.escapeStringJson('a\"b\\n中')"),
    ("escapeStringBash", "std.escapeStringBash(\"a'b\")"),
    ("escapeStringDollars", "std.escapeStringDollars('a$b')"),
    ("escapeStringPython", "std.escapeStringPython('a\"b')"),
    ("escapeStringXML", "std.escapeStringXML('<a&b>')"),
    ("output-nonascii", "{'中': '文'}"),
    ("output-astral", "['\U0001f600']"),
    ("output-control", "['\\u0001\\u001f']"),
    ("output-del", "['\\u007f']"),
)


def strings() -> list[Case]:
    out = [ev(f"str-{name}", "str", src) for name, src in STRING_SRC]
    # -S writes a string result raw instead of as JSON, so the escaping rules
    # differ between the two paths and both need covering.
    for name, src in (("astral", "'\U0001f600'"), ("newline", "'a\\nb'"),
                      ("quote", "'a\"b'"), ("control", "'\\u0001'")):
        out.append(cli(f"str-raw-{name}", "str", "jsonnet", "-S", "-e", "--",
                       src))
    return out


# --------------------------------------------------------------------------
# errors, in all three channels
# --------------------------------------------------------------------------
STATIC_ERR: tuple[tuple[str, str], ...] = (
    ("unexpected-eof", "1 +"),
    ("unknown-variable", "x"),
    ("unknown-variable-nested", "{a: y}"),
    ("duplicate-field", "{a: 1, a: 2}"),
    ("duplicate-field-hidden", "{a: 1, a:: 2}"),
    ("self-outside-object", "self.x"),
    ("super-outside-object", "super.x"),
    ("dollar-outside-object", "$.x"),
    ("bad-token", "1 @@ 2"),
    ("unterminated-string", "'abc"),
    ("unterminated-block", "|||\n  a\n"),
    ("unterminated-comment", "/* a"),
    ("bad-escape", r"'\q'"),
    ("bad-unicode-escape", r"'\uZZZZ'"),
    ("short-unicode-escape", r"'\u12'"),
    ("expected-semicolon", "local x = 1 x"),
    ("expected-brace", "{a: 1"),
    ("expected-bracket", "[1, 2"),
    ("expected-paren", "(1 + 2"),
    ("assert-no-semicolon", "assert false"),
    ("function-no-body", "function(x)"),
    ("comprehension-no-for", "[x]"),
    ("bad-comprehension", "[x for]"),
    ("if-no-then", "if true"),
    ("import-not-string", "import 1"),
    ("importstr-not-string", "importstr 1"),
    ("bad-field-name", "{1: 2}"),
    ("plus-super-no-object", "a+: 1"),
    ("trailing-comma-params", "local f(a,) = a; f(1)"),
    ("empty-program", ""),
    ("only-comment", "// nothing"),
    ("tab-in-source", "{\ta: 1}"),
    ("crlf-source", "{a: 1}\r\n"),
    ("bom-source", "﻿{a: 1}"),
)

RUNTIME_ERR: tuple[tuple[str, str], ...] = (
    ("error-string", "error 'boom'"),
    ("error-object", "error {a: 1}"),
    ("error-number", "error 42"),
    ("error-null", "error null"),
    # The depth sweep in errors() covers f(5) already, so this one recurses
    # through a different frame kind to avoid grading an identical input twice.
    ("error-nested-trace",
     "local f(x) = if x == 0 then error 'deep' else [f(x - 1)][0]; f(4)"),
    ("error-in-field", "{a: error 'in-field'}"),
    ("error-in-array", "[error 'in-array']"),
    ("error-in-comprehension", "[error 'x' for i in [1]]"),
    ("assert-toplevel", "assert 1 == 2; 1"),
    ("assert-message-toplevel", "assert 1 == 2 : 'nope'; 1"),
    ("assert-in-function", "local f(x) = assert x > 0; x; f(-1)"),
    ("field-missing", "{}.foo"),
    ("field-missing-nested", "{a: {}}.a.b"),
    ("index-object-number", "{a: 1}[0]"),
    ("index-array-string", "[1]['a']"),
    ("index-array-oob", "[1, 2][5]"),
    ("index-array-negative", "[1, 2][-1]"),
    ("index-array-fractional", "[1, 2][0.5]"),
    ("index-string-oob", "'ab'[5]"),
    ("index-null", "null.x"),
    ("index-number", "(1).x"),
    ("index-bool", "true.x"),
    ("index-function", "(function(x) x).y"),
    ("call-non-function", "1(2)"),
    ("plus-mismatch", "1 + true"),
    ("plus-null", "null + 1"),
    ("minus-string", "'a' - 'b'"),
    ("times-string", "'a' * 'b'"),
    ("divide-zero", "1 / 0"),
    ("modulo-zero", "1 % 0"),
    ("compare-object", "{} < {}"),
    ("compare-mixed", "1 < 'a'"),
    ("compare-null", "null < null"),
    ("bitwise-fractional", "1.5 & 1"),
    ("bitwise-negative-shift", "1 << -1"),
    ("bitwise-huge-shift", "1 << 1000"),
    ("unary-not-number", "!1"),
    ("unary-minus-string", "-'a'"),
    ("in-non-object", "'a' in [1]"),
    ("stdlib-wrong-type", "std.length(1)"),
    ("stdlib-wrong-arity", "std.length()"),
    ("stdlib-negative-length", "std.makeArray(-1, function(i) i)"),
    ("stdlib-parseInt-bad", "std.parseInt('abc')"),
    ("stdlib-parseHex-bad", "std.parseHex('zz')"),
    ("stdlib-format-bad", "std.format('%d', 'a')"),
    ("stdlib-format-missing", "std.format('%d %d', [1])"),
    ("stdlib-format-extra", "std.format('%d', [1, 2])"),
    ("stdlib-format-unknown", "std.format('%q', 1)"),
    ("stdlib-assertEqual", "std.assertEqual(1, 2)"),
    ("stdlib-manifest-function", "std.manifestJson({f: function(x) x})"),
    ("manifest-function-toplevel", "function(x) x"),
    ("manifest-function-in-object", "{f: function(x) x, g: 1}"),
    ("import-missing", "import 'nope.libsonnet'"),
    ("importstr-missing", "importstr 'nope.txt'"),
    ("import-directory", "import '.'"),
    ("objcomp-non-string-key", "{[1]: 1 for i in [1]}"),
    ("objcomp-over-object", "{[k]: 1 for k in {a: 1}}"),
    ("arrcomp-over-number", "[x for x in 1]"),
    ("string-index-object", "'abc'[{}]"),
)


def errors() -> list[Case]:
    out: list[Case] = []
    # Both invocation forms: locations read <cmdline> for -e and the filename
    # otherwise, and that difference is in every message.
    for name, src in STATIC_ERR:
        out.append(ev(f"err-static-{name}", "err-static", src))
        out.append(ev_exec(f"err-static-e-{name}", "err-static", src))
    for name, src in RUNTIME_ERR:
        out.append(ev(f"err-runtime-{name}", "err-runtime", src))
    # Traces: depth, elision, and the tab-terminated last frame.
    deep = "local f(x) = if x == 0 then error 'deep' else f(x - 1); f(%d)"
    for depth in (0, 1, 2, 5, 20, 60):
        out.append(ev(f"err-trace-depth-{depth:02d}", "err-trace",
                      deep % depth))
    for t in ("1", "2", "3", "5", "0", "100"):
        out.append(cli(f"err-trace-max-{t}", "err-trace", "jsonnet",
                       "-t", t, "-e", "--", deep % 30))
    # A trace through every frame kind the reference labels differently.
    frames = (
        ("thunk", "local x = error 'e'; {a: x}"),
        ("function", "local f() = error 'e'; f()"),
        ("object", "{a: error 'e'}"),
        ("array", "[error 'e']"),
        ("comprehension", "[error 'e' for i in [1]]"),
        ("objcomp", "{[k]: error 'e' for k in ['a']}"),
        ("stdlib", "std.map(function(x) error 'e', [1])"),
        ("import", "import 'lib.libsonnet'"),
        ("binary", "1 + error 'e'"),
        ("index", "{a: error 'e'}.a"),
        ("assert", "assert error 'e'; 1"),
    )
    for name, src in frames:
        files = ((MAIN, src),)
        if "import" in src:
            files = files + (("lib.libsonnet", "error 'from-import'"),)
        out.append(Case(cid=f"err-frame-{name}", family="err-trace",
                        binary="jsonnet", argv=(MAIN,), files=files))
    return out


# --------------------------------------------------------------------------
# the standard library
# --------------------------------------------------------------------------
# At least one call per public name in spec.STDLIB_PUBLIC, and _self_check
# enforces that -- a stdlib case list that silently loses a function would make
# that function free to skip.  Where a function has interesting corners they get
# extra cases; the single-case entries are the ones whose behavior is fully
# pinned by one call.
STDLIB_CALLS: tuple[tuple[str, str], ...] = (
    ("abs", "[std.abs(-1), std.abs(1), std.abs(-1.5), std.abs(0)]"),
    ("acos", "std.acos(0.5)"),
    ("all", "[std.all([]), std.all([true]), std.all([true, false])]"),
    ("any", "[std.any([]), std.any([false]), std.any([false, true])]"),
    ("asciiLower", "std.asciiLower('AbC1\\u00c9')"),
    ("asciiUpper", "std.asciiUpper('AbC1\\u00e9')"),
    ("asin", "std.asin(0.5)"),
    ("assertEqual", "std.assertEqual(1, 1)"),
    ("atan", "std.atan(1)"),
    ("base64", "[std.base64('a'), std.base64('ab'), std.base64('abc')]"),
    ("base64Decode", "std.base64Decode('YWJjZA==')"),
    ("base64DecodeBytes", "std.base64DecodeBytes('YWJjZA==')"),
    ("ceil", "[std.ceil(1.2), std.ceil(-1.2), std.ceil(2)]"),
    ("char", "[std.char(65), std.char(233), std.char(19990)]"),
    ("clamp", "[std.clamp(-1, 0, 2), std.clamp(1, 0, 2), std.clamp(3, 0, 2)]"),
    ("codepoint", "[std.codepoint('A'), std.codepoint('\\u00e9')]"),
    ("cos", "std.cos(0)"),
    ("count", "std.count([1, 2, 1, 3, 1], 1)"),
    ("decodeUTF8", "std.decodeUTF8([104, 105])"),
    ("deepJoin", "std.deepJoin(['a', ['b', ['c']], 'd'])"),
    ("encodeUTF8", "std.encodeUTF8('hi\\u00e9')"),
    ("endsWith", "[std.endsWith('abc', 'bc'), std.endsWith('abc', 'ab')]"),
    ("equals", "[std.equals(1, 1), std.equals([1], [1]), std.equals(1, '1')]"),
    ("escapeStringBash", "std.escapeStringBash('a b\\'c')"),
    ("escapeStringDollars", "std.escapeStringDollars('a$b$$c')"),
    ("escapeStringJson", "std.escapeStringJson('a\\nb\"c\\\\d')"),
    ("escapeStringPython", "std.escapeStringPython('a\\nb\"c')"),
    ("escapeStringXML", "std.escapeStringXML('<a href=\"b\">&amp;</a>')"),
    ("exp", "[std.exp(0), std.exp(1)]"),
    ("exponent", "[std.exponent(1), std.exponent(0.5), std.exponent(1024)]"),
    ("extVar", "std.extVar('v')"),
    ("filter", "std.filter(function(x) x > 1, [1, 2, 3])"),
    ("filterMap",
     "std.filterMap(function(x) x > 1, function(x) x * 10, [1, 2, 3])"),
    ("find", "[std.find(1, [1, 2, 1]), std.find(9, [1])]"),
    ("findSubstr", "[std.findSubstr('a', 'banana'), std.findSubstr('z', 'a')]"),
    ("flatMap", "std.flatMap(function(x) [x, x], [1, 2])"),
    ("flattenArrays", "std.flattenArrays([[1, 2], [3], []])"),
    ("floor", "[std.floor(1.8), std.floor(-1.8), std.floor(2)]"),
    ("foldl", "std.foldl(function(a, b) a + b, [1, 2, 3], 100)"),
    ("foldr", "std.foldr(function(a, b) a + b, ['a', 'b'], 'z')"),
    ("get", "[std.get({a: 1}, 'a'), std.get({}, 'a'), "
            "std.get({}, 'a', 'dflt'), std.get({a:: 1}, 'a'), "
            "std.get({a:: 1}, 'a', null, false)]"),
    ("isArray", "[std.isArray([]), std.isArray({})]"),
    ("isBoolean", "[std.isBoolean(true), std.isBoolean(1)]"),
    ("isEmpty", "[std.isEmpty(''), std.isEmpty('a')]"),
    ("isFunction", "[std.isFunction(function() 1), std.isFunction(1)]"),
    ("isNumber", "[std.isNumber(1), std.isNumber('1')]"),
    ("isObject", "[std.isObject({}), std.isObject([])]"),
    ("isString", "[std.isString(''), std.isString(1)]"),
    ("join", "[std.join(',', ['a', 'b']), std.join(',', ['a', null, 'b']), "
             "std.join([0], [[1], [2]])]"),
    ("length", "[std.length(''), std.length([1]), std.length({a: 1}), "
               "std.length(function(a, b) 1)]"),
    ("lines", "std.lines(['a', 'b'])"),
    ("log", "std.log(1)"),
    ("lstripChars", "std.lstripChars('aabxa', 'a')"),
    ("makeArray", "std.makeArray(3, function(i) i * i)"),
    ("manifestIni",
     "std.manifestIni({main: {a: 1}, sections: {s: {b: 2}}})"),
    ("manifestJson", "std.manifestJson({a: [1, {b: null}]})"),
    ("manifestJsonEx", "std.manifestJsonEx({a: [1, 2]}, '  ')"),
    ("manifestJsonMinified", "std.manifestJsonMinified({a: [1, 2]})"),
    ("manifestPython", "std.manifestPython({a: [1, true, null, 'x']})"),
    ("manifestPythonVars", "std.manifestPythonVars({a: 1, b: 'x'})"),
    ("manifestToml", "std.manifestToml({a: 1, t: {b: 'x'}})"),
    ("manifestTomlEx", "std.manifestTomlEx({a: 1, t: {b: 'x'}}, '  ')"),
    ("manifestXmlJsonml", "std.manifestXmlJsonml(['a', {href: 'b'}, 'c'])"),
    ("manifestYamlDoc", "std.manifestYamlDoc({a: [1, 2], b: 'x'})"),
    ("manifestYamlStream", "std.manifestYamlStream([{a: 1}, {b: 2}])"),
    ("mantissa", "[std.mantissa(1), std.mantissa(0.5), std.mantissa(1024)]"),
    ("map", "std.map(function(x) x + 1, [1, 2])"),
    ("mapWithIndex", "std.mapWithIndex(function(i, x) [i, x], ['a', 'b'])"),
    ("mapWithKey", "std.mapWithKey(function(k, v) k + v, {a: 'x'})"),
    ("max", "[std.max(1, 2), std.max(2, 1), std.max(-0.0, 0.0)]"),
    ("md5", "[std.md5(''), std.md5('abc')]"),
    ("member", "[std.member([1, 2], 1), std.member('abc', 'b')]"),
    ("mergePatch", "std.mergePatch({a: 1, b: 2}, {b: null, c: 3})"),
    ("min", "[std.min(1, 2), std.min(2, 1)]"),
    ("mod", "[std.mod(7, 3), std.mod('%d', 1)]"),
    ("modulo", "[std.modulo(7, 3), std.modulo(-7, 3), std.modulo(7.5, 2)]"),
    ("native", "std.native('nosuch')"),
    ("objectFields", "std.objectFields({b: 1, a: 2, c:: 3})"),
    ("objectFieldsAll", "std.objectFieldsAll({b: 1, a: 2, c:: 3})"),
    ("objectFieldsEx",
     "[std.objectFieldsEx({a: 1, b:: 2}, false), "
     "std.objectFieldsEx({a: 1, b:: 2}, true)]"),
    ("objectHas", "[std.objectHas({a: 1}, 'a'), std.objectHas({a:: 1}, 'a')]"),
    ("objectHasAll", "std.objectHasAll({a:: 1}, 'a')"),
    ("objectHasEx",
     "[std.objectHasEx({a:: 1}, 'a', false), "
     "std.objectHasEx({a:: 1}, 'a', true)]"),
    ("objectKeysValues", "std.objectKeysValues({b: 1, a: 2})"),
    ("objectKeysValuesAll", "std.objectKeysValuesAll({a: 1, b:: 2})"),
    ("objectValues", "std.objectValues({b: 1, a: 2, c:: 3})"),
    ("objectValuesAll", "std.objectValuesAll({b: 1, a: 2, c:: 3})"),
    ("parseHex", "[std.parseHex('ff'), std.parseHex('FF'), std.parseHex('0')]"),
    ("parseInt", "[std.parseInt('0'), std.parseInt('-12'), "
                 "std.parseInt('007')]"),
    ("parseJson", "std.parseJson('{\"a\": [1, 2.5, null, true, \"x\"]}')"),
    ("parseOctal", "[std.parseOctal('777'), std.parseOctal('0')]"),
    ("parseYaml", "std.parseYaml('a: 1\\n')"),
    ("pow", "[std.pow(2, 10), std.pow(2, -1), std.pow(2, 0.5)]"),
    ("primitiveEquals", "[std.primitiveEquals(1, 1), "
                        "std.primitiveEquals(1, 2)]"),
    ("prune", "std.prune({a: null, b: [], c: {}, d: 1, e: [null]})"),
    ("range", "[std.range(1, 3), std.range(1, 1), std.range(2, 1)]"),
    ("repeat", "[std.repeat('ab', 3), std.repeat([1], 2), "
               "std.repeat('a', 0)]"),
    ("resolvePath", "std.resolvePath('a/b/c', 'd')"),
    ("reverse", "[std.reverse([1, 2, 3]), std.reverse('abc')]"),
    ("round", "[std.round(0.5), std.round(1.5), std.round(-0.5), "
              "std.round(2.4)]"),
    ("rstripChars", "std.rstripChars('axbaa', 'a')"),
    ("set", "std.set([3, 1, 2, 1])"),
    ("setDiff", "std.setDiff([1, 2, 3], [2])"),
    ("setInter", "std.setInter([1, 2, 3], [2, 3, 4])"),
    ("setMember", "[std.setMember(2, [1, 2]), std.setMember(9, [1, 2])]"),
    ("setUnion", "std.setUnion([1, 3], [2, 3])"),
    ("sign", "[std.sign(-2), std.sign(0), std.sign(2)]"),
    ("sin", "std.sin(0)"),
    ("slice", "[std.slice([1,2,3,4], 1, 3, 1), std.slice('abcd', 0, 4, 2), "
              "std.slice([1,2,3], null, null, null)]"),
    ("sort", "[std.sort([3, 1, 2]), std.sort(['b', 'a']), "
             "std.sort([{k: 2}, {k: 1}], function(o) o.k)]"),
    ("split", "[std.split('a,b,,c', ','), std.split('abc', 'b')]"),
    ("splitLimit", "std.splitLimit('a,b,c', ',', 1)"),
    ("splitLimitR", "std.splitLimitR('a,b,c', ',', 1)"),
    ("sqrt", "[std.sqrt(4), std.sqrt(2), std.sqrt(0)]"),
    ("startsWith", "[std.startsWith('abc', 'ab'), "
                   "std.startsWith('abc', 'bc')]"),
    ("strReplace", "std.strReplace('aaa', 'aa', 'b')"),
    ("stringChars", "std.stringChars('a\\u00e9')"),
    ("stripChars", "std.stripChars('aaxbaa', 'a')"),
    ("substr", "[std.substr('abcdef', 1, 3), std.substr('abc', 0, 0)]"),
    ("sum", "[std.sum([]), std.sum([1, 2, 3])]"),
    ("tan", "std.tan(0)"),
    ("thisFile", "std.thisFile"),
    ("toString", "[std.toString(1), std.toString(null), std.toString('a'), "
                 "std.toString([1, 'a']), std.toString({a: 1}), "
                 "std.toString(true)]"),
    ("trace", "std.trace('msg', 42)"),
    ("type", "[std.type(null), std.type(true), std.type(1), std.type('a'), "
             "std.type([]), std.type({}), std.type(function() 1)]"),
    ("uniq", "[std.uniq([1, 1, 2, 1]), std.uniq([{k:1},{k:1}], "
             "function(o) o.k)]"),
    ("xnor", "[std.xnor(true, true), std.xnor(true, false)]"),
    ("xor", "[std.xor(true, true), std.xor(true, false)]"),
)


def stdlib() -> list[Case]:
    out: list[Case] = []
    for name, src in STDLIB_CALLS:
        extra: tuple[str, ...] = ()
        if name == "extVar":
            extra = ("-V", "v=hello")
        out.append(ev_exec(f"std-{name}", "stdlib", src, *extra))

    # The dunder helpers, reachable only through reflection.
    out.append(ev_exec("std-dunder-fields", "stdlib",
                       "[f for f in std.objectFieldsAll(std) "
                       "if std.startsWith(f, '__')]"))
    out.append(ev_exec("std-public-count", "stdlib",
                       "std.length([f for f in std.objectFieldsAll(std) "
                       "if !std.startsWith(f, '__')])"))
    out.append(ev_exec("std-compare", "stdlib",
                       "[std.__compare(1, 2), std.__compare('a', 'a'), "
                       "std.__compare([1, 2], [1, 3])]"))
    out.append(ev_exec("std-array-less", "stdlib",
                       "[std.__array_less([1], [2]), "
                       "std.__array_greater_or_equal([2], [2])]"))
    # std is an object, so the object model applies to it too.
    out.append(ev_exec("std-is-object", "stdlib", "std.isObject(std)"))
    out.append(ev_exec("std-type-of-members", "stdlib",
                       "std.set([std.type(std[f]) "
                       "for f in std.objectFieldsAll(std)])"))
    return out


# --------------------------------------------------------------------------
# std.format, which is its own language
# --------------------------------------------------------------------------
FORMAT_CASES: tuple[str, ...] = (
    "std.format('%d', 1)",
    "std.format('%5d', 1)",
    "std.format('%-5d|', 1)",
    "std.format('%05d', 1)",
    "std.format('%+d', 1)",
    "std.format('% d', 1)",
    "std.format('%d', -1)",
    "std.format('%d', 1.9)",
    "std.format('%d', -1.9)",
    "std.format('%i', 42)",
    "std.format('%u', 42)",
    "std.format('%c', 65)",
    "std.format('%c', 'A')",
    "std.format('%s', 'x')",
    "std.format('%10s|', 'x')",
    "std.format('%-10s|', 'x')",
    "std.format('%s', null)",
    "std.format('%s', true)",
    "std.format('%s', [1, 2])",
    "std.format('%s', {a: 1})",
    "std.format('%f', 1.5)",
    "std.format('%.0f', 1.5)",
    "std.format('%.0f', 2.5)",
    "std.format('%.3f', 1.0 / 3)",
    "std.format('%10.3f|', 1.5)",
    "std.format('%-10.3f|', 1.5)",
    "std.format('%e', 1234.5)",
    "std.format('%E', 1234.5)",
    "std.format('%.2e', 1234.5)",
    "std.format('%g', 1234.5)",
    "std.format('%G', 0.00001234)",
    "std.format('%.3g', 1234.5)",
    "std.format('%x', 255)",
    "std.format('%X', 255)",
    "std.format('%o', 8)",
    "std.format('%#x', 255)",
    "std.format('%#o', 8)",
    "std.format('%%', [])",
    "std.format('%s%%%s', ['a', 'b'])",
    "std.format('%(a)s', {a: 1})",
    "std.format('%(a)d-%(b)s', {a: 1, b: 'x'})",
    "std.format('%(a)s %(a)s', {a: 1})",
    "std.format('%*d', [5, 1])",
    "std.format('%.*f', [2, 1.5])",
    "std.format('%s and %s', ['a', 'b'])",
    "std.format('no directives', [])",
    "std.format('', [])",
    "'%s' % 'x'",
    "'%s %s' % ['a', 'b']",
    "'%(k)s' % {k: 'v'}",
    "std.format('%s', 0.30000000000000004)",
    "std.format('%s', 1e100)",
    "std.format('%s', 1e-5)",
    "std.format('%d', 1e15)",
    "std.format('%d', 2 ^ 53)",
    "std.format('%.17g', 0.1)",
)


def format_family() -> list[Case]:
    out = [ev_exec(f"fmt-{i:03d}", "format", src)
           for i, src in enumerate(FORMAT_CASES)]
    return out


# --------------------------------------------------------------------------
# std.parseYaml and std.parseJson
# --------------------------------------------------------------------------
def parsers() -> list[Case]:
    out: list[Case] = []
    for i, s in enumerate(spec.PARSEYAML_SCALARS):
        out.append(ev_exec(f"yaml-scalar-{i:02d}", "parseyaml",
                           "std.parseYaml(%s)" % json.dumps(s)))
        out.append(ev_exec(f"yaml-scalar-type-{i:02d}", "parseyaml",
                           "std.type(std.parseYaml(%s)[0])" % json.dumps(s)))
    for name, src in spec.PARSEYAML_DOCUMENTS:
        out.append(ev_exec(f"yaml-doc-{name}", "parseyaml",
                           "std.parseYaml(%s)" % json.dumps(src)))
    json_srcs = (
        '{"a": 1}', '[1, 2, 3]', 'null', 'true', '"s"', '1', '1.5', '1e5',
        # 1e308 and 1e-400, not 1e400: overflow aborts the parser, so it is
        # excluded by spec.PARSER_NUMBER_OVERFLOW.  These two sit just inside the
        # cliff on both sides, which is the part a port can actually be asked to
        # reproduce.
        '-0', '0.1', '1e308', '1e-400', '{"a": {"b": [1, {"c": null}]}}',
        '{"a": 1, "a": 2}', '[]', '{}', '"\\u00e9"', '"\\ud83d\\ude00"',
        '"\\/"', '  {"a":1}  ', '{"a":1,}', '[1,]', "{'a': 1}", '{a: 1}',
        'NaN', 'Infinity', '01', '+1', '.5', '', '   ',
    )
    for i, s in enumerate(json_srcs):
        out.append(ev_exec(f"json-{i:02d}", "parsejson",
                           "std.parseJson(%s)" % json.dumps(s)))
    return out


# --------------------------------------------------------------------------
# imports, importstr, and the search path
# --------------------------------------------------------------------------
def imports() -> list[Case]:
    """import resolution is filesystem behavior, so it needs real files.

    The rules that matter: imports resolve relative to the importing file, not
    the process cwd; -J directories are searched in order after that; an import
    is evaluated once and cached by resolved path; and importstr reads bytes
    without parsing.
    """
    out: list[Case] = []

    out.append(Case("imp-basic", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'lib.libsonnet'"),
        ("lib.libsonnet", "{a: 1}"),
    )))
    out.append(Case("imp-nested-dir", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'sub/lib.libsonnet'"),
        ("sub/lib.libsonnet", "{a: 1}"),
    )))
    # Relative to the importing file, not the cwd: sub/a imports 'b', which must
    # resolve to sub/b.  A port that resolves against cwd finds nothing.
    out.append(Case("imp-relative-to-importer", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'sub/a.libsonnet'"),
        ("sub/a.libsonnet", "import 'b.libsonnet'"),
        ("sub/b.libsonnet", "{deep: true}"),
        ("b.libsonnet", "{wrong: true}"),
    )))
    out.append(Case("imp-parent-dir", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'sub/up.libsonnet'"),
        ("sub/up.libsonnet", "import '../top.libsonnet'"),
        ("top.libsonnet", "{top: true}"),
    )))
    out.append(Case("imp-absolute", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'sub/../lib.libsonnet'"),
        ("lib.libsonnet", "{a: 1}"),
        ("sub/keep.txt", "x"),
    )))
    # Cached by resolved path: the side effect of a trace fires once even though
    # the import appears twice.
    out.append(Case("imp-cached-once", "imports", "jsonnet", (MAIN,), (
        (MAIN, "local a = import 'l.libsonnet'; local b = import "
               "'l.libsonnet'; [a, b]"),
        ("l.libsonnet", "std.trace('evaluated', {v: 1})"),
    )))
    out.append(Case("imp-diamond", "imports", "jsonnet", (MAIN,), (
        (MAIN, "[import 'a.libsonnet', import 'b.libsonnet']"),
        ("a.libsonnet", "import 'c.libsonnet'"),
        ("b.libsonnet", "import 'c.libsonnet'"),
        ("c.libsonnet", "std.trace('c', {v: 1})"),
    )))
    out.append(Case("imp-cycle", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'a.libsonnet'"),
        ("a.libsonnet", "import 'b.libsonnet'"),
        ("b.libsonnet", "import 'a.libsonnet'"),
    )))
    out.append(Case("imp-str", "imports", "jsonnet", (MAIN,), (
        (MAIN, "importstr 'data.txt'"),
        ("data.txt", "line one\nline two\n"),
    )))
    out.append(Case("imp-str-binary-ish", "imports", "jsonnet", (MAIN,), (
        (MAIN, "importstr 'data.txt'"),
        ("data.txt", "tab\there\r\ncrlf\n"),
    )))
    out.append(Case("imp-str-utf8", "imports", "jsonnet", (MAIN,), (
        (MAIN, "importstr 'data.txt'"),
        ("data.txt", "café \U0001f600\n"),
    )))
    out.append(Case("imp-str-empty", "imports", "jsonnet", (MAIN,), (
        (MAIN, "importstr 'data.txt'"),
        ("data.txt", ""),
    )))
    out.append(Case("imp-str-no-parse", "imports", "jsonnet", (MAIN,), (
        (MAIN, "importstr 'broken.jsonnet'"),
        ("broken.jsonnet", "this is not ) valid jsonnet"),
    )))
    out.append(Case("imp-of-broken", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'broken.libsonnet'"),
        ("broken.libsonnet", "1 +"),
    )))
    out.append(Case("imp-error-inside", "imports", "jsonnet", (MAIN,), (
        (MAIN, "(import 'l.libsonnet').a"),
        ("l.libsonnet", "{a: error 'from lib'}"),
    )))
    out.append(Case("imp-function", "imports", "jsonnet", (MAIN,), (
        (MAIN, "(import 'f.libsonnet')(2)"),
        ("f.libsonnet", "function(x) x * 21"),
    )))
    out.append(Case("imp-self", "imports", "jsonnet", (MAIN,), (
        (MAIN, "import 'main.jsonnet'"),
    )))
    # -J search path: order matters, and the importing file's directory wins.
    jpath_files = (
        (MAIN, "import 'shared.libsonnet'"),
        ("libA/shared.libsonnet", "{from: 'A'}"),
        ("libB/shared.libsonnet", "{from: 'B'}"),
    )
    out.append(Case("imp-jpath-single", "imports", "jsonnet",
                    ("-J", "libA", MAIN), jpath_files))
    out.append(Case("imp-jpath-order-ab", "imports", "jsonnet",
                    ("-J", "libA", "-J", "libB", MAIN), jpath_files))
    out.append(Case("imp-jpath-order-ba", "imports", "jsonnet",
                    ("-J", "libB", "-J", "libA", MAIN), jpath_files))
    out.append(Case("imp-jpath-local-wins", "imports", "jsonnet",
                    ("-J", "libA", MAIN), (
                        (MAIN, "import 'shared.libsonnet'"),
                        ("shared.libsonnet", "{from: 'local'}"),
                        ("libA/shared.libsonnet", "{from: 'A'}"),
                    )))
    out.append(Case("imp-jpath-missing-dir", "imports", "jsonnet",
                    ("-J", "nosuch", MAIN), (
                        (MAIN, "import 'shared.libsonnet'"),
                        ("shared.libsonnet", "{ok: true}"),
                    )))
    out.append(Case("imp-jpath-not-found", "imports", "jsonnet",
                    ("-J", "libA", MAIN), (
                        (MAIN, "import 'absent.libsonnet'"),
                        ("libA/other.libsonnet", "{}"),
                    )))
    # std.thisFile inside an import reports the imported file, not the entry.
    out.append(Case("imp-thisfile", "imports", "jsonnet", (MAIN,), (
        (MAIN, "[std.thisFile, import 'l.libsonnet']"),
        ("l.libsonnet", "std.thisFile"),
    )))
    return out


# --------------------------------------------------------------------------
# argument handling and the CLI surface
# --------------------------------------------------------------------------
def cli_surface() -> list[Case]:
    """The interface being preserved, exercised as an interface.

    Includes the malformed invocations, because a diagnostic is part of a CLI's
    contract and these are the cases a port written against a conventional
    argument parser gets wrong: multichar expansion, `--`, `-` for stdin, and
    the exact wording of each refusal.
    """
    out: list[Case] = []
    src_files = ((MAIN, "{a: 1, b: 'x'}"),)

    for flag in ("--help", "-h", "--version"):
        out.append(cli(f"cli-eval{flag.replace('-', '_')}", "cli", "jsonnet",
                       flag))
        out.append(cli(f"cli-fmt{flag.replace('-', '_')}", "cli", "jsonnetfmt",
                       flag))

    # Ways to be wrong on the command line.
    bad = (
        ("no-args", ()),
        ("two-files", (MAIN, "other.jsonnet")),
        ("unknown-long", ("--nope", MAIN)),
        ("unknown-short", ("-Z", MAIN)),
        ("missing-value-o", ("-o",)),
        ("missing-value-J", ("-J",)),
        ("empty-o", ("-o", "", MAIN)),
        ("empty-J", ("-J", "", MAIN)),
        ("empty-m", ("-m", "", MAIN)),
        ("bad-max-stack", ("-s", "x", MAIN)),
        ("zero-max-stack", ("-s", "0", MAIN)),
        ("negative-max-stack", ("-s", "-1", MAIN)),
        ("bad-max-trace", ("-t", "x", MAIN)),
        ("bad-gc-min", ("--gc-min-objects", "x", MAIN)),
        ("bad-gc-growth", ("--gc-growth-trigger", "x", MAIN)),
        ("negative-gc-growth", ("--gc-growth-trigger", "-1", MAIN)),
        ("ext-str-no-value-undefined", ("-V", "UNSET_VAR_XYZ", MAIN)),
        ("ext-str-file-malformed", ("--ext-str-file", "novareq", MAIN)),
        ("ext-code-file-malformed", ("--ext-code-file", "novareq", MAIN)),
        ("tla-str-file-malformed", ("--tla-str-file", "novareq", MAIN)),
        ("missing-file", ("nosuch.jsonnet",)),
        ("directory-as-file", (".",)),
        ("dash-prefixed-name", ("-weird.jsonnet",)),
    )
    for name, argv in bad:
        out.append(cli(f"cli-bad-{name}", "cli", "jsonnet", *argv,
                       files=src_files))

    fmt_bad = (
        ("no-args", ()),
        ("unknown-long", ("--nope", MAIN)),
        ("inplace-with-exec", ("-i", "-e", "--", "{a:1}")),
        ("inplace-with-stdin", ("-i", "-")),
        ("bad-indent", ("-n", "x", MAIN)),
        ("bad-blank-lines", ("--max-blank-lines", "x", MAIN)),
        ("bad-string-style", ("--string-style", "q", MAIN)),
        ("bad-comment-style", ("--comment-style", "q", MAIN)),
        ("missing-style-value", ("--string-style",)),
        ("missing-file", ("nosuch.jsonnet",)),
    )
    for name, argv in fmt_bad:
        out.append(cli(f"cli-fmt-bad-{name}", "cli", "jsonnetfmt", *argv,
                       files=src_files))

    # The argv rules from spec.ARGV_RULES, each as a case that fails if the rule
    # is not implemented.
    out.append(cli("argv-multichar-Sy", "cli", "jsonnet", "-Se", "--",
                   "'raw'", note="-Se expands to -S -e"))
    out.append(cli("argv-multichar-three", "cli", "jsonnet", "-Sye", "--",
                   "['a']", note="-Sye expands to -S -y -e"))
    out.append(cli("argv-double-dash-file", "cli", "jsonnet", "--",
                   "-weird.jsonnet",
                   files=(("-weird.jsonnet", "{ok: true}"),),
                   note="a filename starting with - is reachable only after --"))
    out.append(cli("argv-double-dash-code", "cli", "jsonnet", "-e", "--",
                   "-1", note="a program starting with - needs --"))
    out.append(cli("argv-stdin", "cli", "jsonnet", "-", stdin="{a: 1}\n"))
    out.append(cli("argv-stdin-empty", "cli", "jsonnet", "-", stdin=""))
    out.append(cli("argv-stdin-error", "cli", "jsonnet", "-", stdin="1 +\n"))
    out.append(cli("argv-fmt-stdin", "cli", "jsonnetfmt", "-",
                   stdin="local x=1;\n{a:x}\n"))
    # Zero filenames succeeds only via -e; bare zero-arg is in cli-fmt-bad-*.
    out.append(cli("argv-fmt-exec", "cli", "jsonnetfmt", "-e", "--",
                   "local x=1;{a:x}"))
    out.append(cli("argv-fmt-two-files", "cli", "jsonnetfmt", MAIN, "two.jsonnet",
                   files=((MAIN, "{a:1}"), ("two.jsonnet", "{b:2}")),
                   note="jsonnetfmt takes any number of filenames"))
    out.append(cli("argv-fmt-three-files", "cli", "jsonnetfmt",
                   "a.jsonnet", "b.jsonnet", "c.jsonnet",
                   files=(("a.jsonnet", "{a:1}"), ("b.jsonnet", "{b:2}"),
                          ("c.jsonnet", "{c:3}"))))
    out.append(cli("argv-flag-after-file", "cli", "jsonnet", MAIN, "-S",
                   files=src_files,
                   note="options are accepted after the filename"))
    out.append(cli("argv-repeated-flag", "cli", "jsonnet", "-S", "-S", "-e",
                   "--", "'x'"))
    out.append(cli("argv-long-with-equals", "cli", "jsonnet",
                   "--max-stack=200", "-e", "--", "1",
                   note="whether --flag=value is accepted at all"))

    # External variables and top-level arguments: four families, each with a
    # -file variant, plus the interactions.
    ext = (
        ("ext-str", ("-V", "v=hello"), "std.extVar('v')"),
        ("ext-str-long", ("--ext-str", "v=hello"), "std.extVar('v')"),
        ("ext-str-empty", ("-V", "v="), "std.extVar('v')"),
        ("ext-str-equals-in-value", ("-V", "v=a=b"), "std.extVar('v')"),
        ("ext-code", ("--ext-code", "v={a: 1}"), "std.extVar('v')"),
        ("ext-code-number", ("--ext-code", "v=0.1"), "std.extVar('v')"),
        ("ext-code-error", ("--ext-code", "v=error 'x'"), "std.extVar('v')"),
        ("ext-code-lazy", ("--ext-code", "v=error 'x'"), "1"),
        ("ext-code-broken", ("--ext-code", "v=1 +"), "std.extVar('v')"),
        ("ext-missing", (), "std.extVar('absent')"),
        ("ext-two", ("-V", "a=1", "-V", "b=2"),
         "[std.extVar('a'), std.extVar('b')]"),
        ("ext-shadow", ("-V", "a=1", "-V", "a=2"), "std.extVar('a')"),
        ("ext-code-vs-str", ("-V", "a=1", "--ext-code", "a=2"),
         "std.extVar('a')"),
    )
    for name, argv, src in ext:
        out.append(cli(f"cli-{name}", "cli", "jsonnet", *argv, "-e", "--", src))

    tla = (
        ("tla-str", ("-A", "x=hi"), "function(x) x"),
        ("tla-str-long", ("--tla-str", "x=hi"), "function(x) x"),
        ("tla-code", ("--tla-code", "x=[1, 2]"), "function(x) x"),
        ("tla-default", (), "function(x = 'dflt') x"),
        ("tla-missing", (), "function(x) x"),
        ("tla-extra", ("-A", "y=1"), "function(x = 1) x"),
        ("tla-two", ("-A", "a=1", "-A", "b=2"), "function(a, b) [a, b]"),
        ("tla-not-function", ("-A", "x=1"), "{a: 1}"),
        ("tla-code-error", ("--tla-code", "x=error 'e'"), "function(x) x"),
        ("tla-code-lazy", ("--tla-code", "x=error 'e'"), "function(x) 1"),
    )
    for name, argv, src in tla:
        out.append(cli(f"cli-{name}", "cli", "jsonnet", *argv, "-e", "--", src))

    # The -file variants read the value from a file, including a trailing
    # newline the string form would never contain.
    out.append(Case("cli-ext-str-file", "cli", "jsonnet",
                    ("--ext-str-file", "v=val.txt", "-e", "--",
                     "std.extVar('v')"),
                    (("val.txt", "from file\n"),)))
    out.append(Case("cli-ext-str-file-no-newline", "cli", "jsonnet",
                    ("--ext-str-file", "v=val.txt", "-e", "--",
                     "std.extVar('v')"),
                    (("val.txt", "no newline"),)))
    out.append(Case("cli-ext-code-file", "cli", "jsonnet",
                    ("--ext-code-file", "v=val.jsonnet", "-e", "--",
                     "std.extVar('v')"),
                    (("val.jsonnet", "{a: 0.1}\n"),)))
    out.append(Case("cli-ext-code-file-missing", "cli", "jsonnet",
                    ("--ext-code-file", "v=absent.jsonnet", "-e", "--",
                     "std.extVar('v')"), ()))
    out.append(Case("cli-tla-str-file", "cli", "jsonnet",
                    ("--tla-str-file", "x=val.txt", "-e", "--",
                     "function(x) x"),
                    (("val.txt", "tla from file\n"),)))
    out.append(Case("cli-tla-code-file", "cli", "jsonnet",
                    ("--tla-code-file", "x=val.jsonnet", "-e", "--",
                     "function(x) x"),
                    (("val.jsonnet", "[1, 2]\n"),)))

    # JSONNET_PATH is the environment half of -J.  The executor sets it when the
    # case asks; encoded in argv as a pseudo-argument the executor strips, since
    # Case has no env field and adding one for a single family would complicate
    # every other case's identity.
    out.append(Case("cli-env-jsonnet-path", "cli", "jsonnet",
                    ("@env:JSONNET_PATH=libA", MAIN), (
                        (MAIN, "import 'shared.libsonnet'"),
                        ("libA/shared.libsonnet", "{from: 'env'}"),
                    ), note="JSONNET_PATH is searched like -J"))
    out.append(Case("cli-env-jsonnet-path-two", "cli", "jsonnet",
                    ("@env:JSONNET_PATH=libB:libA", MAIN), (
                        (MAIN, "import 'shared.libsonnet'"),
                        ("libA/shared.libsonnet", "{from: 'A'}"),
                        ("libB/shared.libsonnet", "{from: 'B'}"),
                    )))
    out.append(Case("cli-env-and-flag", "cli", "jsonnet",
                    ("@env:JSONNET_PATH=libB", "-J", "libA", MAIN), (
                        (MAIN, "import 'shared.libsonnet'"),
                        ("libA/shared.libsonnet", "{from: 'A'}"),
                        ("libB/shared.libsonnet", "{from: 'B'}"),
                    ), note="which of -J and JSONNET_PATH wins"))
    return out


# --------------------------------------------------------------------------
# output modes: -o, -m, -y, -S
# --------------------------------------------------------------------------
def output_modes() -> list[Case]:
    """Modes that write files or reshape the top level.

    The executor treats any file the run creates as part of the observation, so
    -o and -m are graded on their side effects as well as their streams.
    """
    out: list[Case] = []
    out.append(cli("out-o-file", "out", "jsonnet", "-o", "result.json", "-e",
                   "--", "{a: 0.1}"))
    out.append(cli("out-o-overwrite", "out", "jsonnet", "-o", "result.json",
                   "-e", "--", "{a: 1}",
                   files=(("result.json", "STALE\n"),)))
    out.append(cli("out-o-subdir-missing", "out", "jsonnet", "-o",
                   "nosuch/result.json", "-e", "--", "{a: 1}"))
    out.append(cli("out-o-and-stdout", "out", "jsonnet", "-o", "result.json",
                   "-e", "--", "std.trace('side', {a: 1})"))
    out.append(cli("out-multi", "out", "jsonnet", "-m", ".", "-e", "--",
                   "{'a.json': {x: 1}, 'b.json': {y: 0.1}}"))
    out.append(cli("out-multi-nested-name", "out", "jsonnet", "-m", ".", "-e",
                   "--", "{'sub/a.json': {x: 1}}"))
    out.append(cli("out-multi-empty", "out", "jsonnet", "-m", ".", "-e", "--",
                   "{}"))
    out.append(cli("out-multi-not-object", "out", "jsonnet", "-m", ".", "-e",
                   "--", "[1]"))
    out.append(cli("out-multi-string-values", "out", "jsonnet", "-m", ".",
                   "-S", "-e", "--", "{'a.txt': 'raw'}"))
    out.append(cli("out-multi-missing-dir", "out", "jsonnet", "-m", "nosuch",
                   "-e", "--", "{'a.json': 1}"))
    out.append(cli("out-multi-unchanged", "out", "jsonnet", "-m", ".", "-e",
                   "--", "{'a.json': {x: 1}}",
                   files=(("a.json", '{\n   "x": 1\n}\n'),),
                   note="whether an identical file is rewritten or skipped"))
    out.append(cli("out-yaml-stream", "out", "jsonnet", "-y", "-e", "--",
                   "[{a: 1}, {b: 2}]"))
    out.append(cli("out-yaml-stream-empty", "out", "jsonnet", "-y", "-e", "--",
                   "[]"))
    out.append(cli("out-yaml-stream-scalars", "out", "jsonnet", "-y", "-e",
                   "--", "[1, 'a', null, true]"))
    out.append(cli("out-yaml-stream-not-array", "out", "jsonnet", "-y", "-e",
                   "--", "{a: 1}"))
    out.append(cli("out-yaml-stream-nested", "out", "jsonnet", "-y", "-e",
                   "--", "[[1, 2], {a: {b: 1}}]"))
    out.append(cli("out-string-mode", "out", "jsonnet", "-S", "-e", "--",
                   "'line\\n'"))
    out.append(cli("out-string-mode-not-string", "out", "jsonnet", "-S", "-e",
                   "--", "{a: 1}"))
    out.append(cli("out-string-and-yaml", "out", "jsonnet", "-S", "-y", "-e",
                   "--", "['a', 'b']"))
    out.append(cli("out-o-with-multi", "out", "jsonnet", "-o", "r.json", "-m",
                   ".", "-e", "--", "{'a.json': 1}"))
    return out


# --------------------------------------------------------------------------
# the formatter
# --------------------------------------------------------------------------
# jsonnetfmt is a second, independent implementation surface: it parses with
# fodder retained -- comments, blank lines and whitespace are attached to tokens
# rather than discarded -- and unparses.  A port that reuses the evaluator's
# parser cannot format, because the evaluator's parser throws fodder away.  That
# makes this family the strongest evidence in the case list that a real
# reimplementation happened.
FMT_SRC: tuple[tuple[str, str], ...] = (
    ("already-clean", "{ a: 1 }\n"),
    ("no-spaces", "{a:1,b:2}\n"),
    ("extra-spaces", "{   a:    1   }\n"),
    ("local", "local x=1;{a:x}\n"),
    ("local-multi", "local a=1,b=2;{x:a,y:b}\n"),
    ("nested", "{a:{b:{c:1}}}\n"),
    ("array", "[1,2,3]\n"),
    ("array-nested", "[[1,2],[3]]\n"),
    ("array-long",
     "[" + ",".join(str(i) for i in range(30)) + "]\n"),
    ("object-long",
     "{" + ",".join(f"k{i}:{i}" for i in range(20)) + "}\n"),
    ("comment-hash", "# c\n{a:1}\n"),
    ("comment-slash", "// c\n{a:1}\n"),
    ("comment-block", "/* c */\n{a:1}\n"),
    ("comment-inline", "{a:1 // trailing\n}\n"),
    ("comment-between-fields", "{\na:1,\n// between\nb:2,\n}\n"),
    ("comment-in-array", "[\n1, // one\n2,\n]\n"),
    ("comment-multiline-block", "/*\n * a\n * b\n */\n{a:1}\n"),
    ("comment-doc-position", "{\n// doc\na:1}\n"),
    ("shebang", "#!/usr/bin/env jsonnet\n{a:1}\n"),
    ("blank-lines-one", "{\na:1,\n\nb:2}\n"),
    ("blank-lines-many", "{\na:1,\n\n\n\n\nb:2}\n"),
    ("blank-lines-leading", "\n\n{a:1}\n"),
    ("blank-lines-trailing", "{a:1}\n\n\n"),
    ("no-trailing-newline", "{a:1}"),
    ("crlf", "{a:1}\r\n"),
    ("tabs", "{\n\ta:1\n}\n"),
    ("string-single", "{a:'x'}\n"),
    ("string-double", '{a:"x"}\n'),
    ("string-mixed", "{a:'x',b:\"y\"}\n"),
    ("string-with-quote", "{a:'it\\'s'}\n"),
    ("string-with-dquote", '{a:"say \\"hi\\""}\n'),
    ("string-verbatim", "{a:@'raw\\t'}\n"),
    ("string-block", "{a:|||\n  text\n|||}\n"),
    ("string-unicode", "{a:'caf\\u00e9'}\n"),
    ("field-quoted-simple", "{'a':1}\n"),
    ("field-quoted-needs-quotes", "{'a b':1,'if':2,'0':3}\n"),
    ("field-computed", "{['a'+'b']:1}\n"),
    ("index-bracket-simple", "{a:1}['a']\n"),
    ("index-bracket-complex", "{a:1}['a b']\n"),
    ("imports-unsorted",
     "local c=import 'c.libsonnet';\nlocal a=import 'a.libsonnet';\n"
     "local b=import 'b.libsonnet';\n{x:[a,b,c]}\n"),
    ("imports-with-comment",
     "// header\nlocal b=import 'b.libsonnet';\n"
     "local a=import 'a.libsonnet';\n{x:[a,b]}\n"),
    ("imports-mixed-with-locals",
     "local b=import 'b.libsonnet';\nlocal z=1;\n"
     "local a=import 'a.libsonnet';\n{x:[a,b,z]}\n"),
    ("function-def", "local f(x,y)=x+y;f(1,2)\n"),
    ("function-lambda", "std.map(function(x)x*2,[1,2])\n"),
    ("function-default", "local f(x=1)=x;f()\n"),
    ("if-else", "if true then 1 else 2\n"),
    ("if-else-nested", "if true then if false then 1 else 2 else 3\n"),
    ("comprehension", "[x for x in [1,2] if x>1]\n"),
    ("objcomp", "{[k]:1 for k in ['a']}\n"),
    ("operators", "1+2*3-4/5%6\n"),
    ("operators-compare", "1<2&&3>=4||!true\n"),
    ("operators-bitwise", "1&2|3^4<<5>>6\n"),
    ("in-operator", "'a' in {a:1}\n"),
    ("super-plus", "{a:1}+{a+:2}\n"),
    ("assert", "assert 1==1;{a:1}\n"),
    ("assert-message", "assert 1==1:'msg';{a:1}\n"),
    ("object-assert", "{assert self.a==1,a:1}\n"),
    ("error", "error 'x'\n"),
    ("dollar", "{a:1,b:$.a}\n"),
    ("self", "{a:1,b:self.a}\n"),
    ("paren-redundant", "((1))\n"),
    ("paren-needed", "(1+2)*3\n"),
    ("long-line-object",
     "{averyveryverylongfieldname:'and a very long value string here too'}\n"),
    ("deeply-nested", "{" * 8 + "a:1" + "}" * 8 + "\n"),
    ("empty-object", "{}\n"),
    ("empty-array", "[]\n"),
    ("empty-file", ""),
    ("only-comment", "// nothing\n"),
    ("only-blank", "\n\n"),
    ("trailing-comma-object", "{a:1,}\n"),
    ("trailing-comma-array", "[1,]\n"),
    ("syntax-error", "{a:1\n"),
    ("syntax-error-mid", "local x = ; {a:1}\n"),
)


def formatter() -> list[Case]:
    out: list[Case] = []
    lib_files = tuple((f"{n}.libsonnet", f"{{{n}: 1}}\n")
                      for n in ("a", "b", "c"))

    for name, src in FMT_SRC:
        files = ((MAIN, src),)
        if "import" in src:
            files = files + lib_files
        out.append(Case(f"fmtr-{name}", "fmtr", "jsonnetfmt", (MAIN,), files))

    # Every flag, over a source that responds to it.  The default run above
    # already covers the defaults, so these are the non-default settings.
    responsive = "local x=1;\n{a:x,'b c':[1,2],d:'s',e:\"t\"}\n"
    flag_runs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("indent-0", ("-n", "0")),
        ("indent-1", ("-n", "1")),
        ("indent-4", ("-n", "4")),
        ("indent-8", ("-n", "8")),
        ("blank-0", ("--max-blank-lines", "0")),
        ("blank-1", ("--max-blank-lines", "1")),
        ("blank-5", ("--max-blank-lines", "5")),
        ("string-d", ("--string-style", "d")),
        ("string-s", ("--string-style", "s")),
        ("string-l", ("--string-style", "l")),
        ("comment-h", ("--comment-style", "h")),
        ("comment-s", ("--comment-style", "s")),
        ("comment-l", ("--comment-style", "l")),
        ("no-pretty-field-names", ("--no-pretty-field-names",)),
        ("pretty-field-names", ("--pretty-field-names",)),
        ("pad-arrays", ("--pad-arrays",)),
        ("no-pad-arrays", ("--no-pad-arrays",)),
        ("pad-objects", ("--pad-objects",)),
        ("no-pad-objects", ("--no-pad-objects",)),
        ("no-sort-imports", ("--no-sort-imports",)),
        ("sort-imports", ("--sort-imports",)),
        ("combo-tight", ("-n", "0", "--no-pad-objects", "--no-pad-arrays")),
        ("combo-loose", ("-n", "4", "--pad-arrays", "--string-style", "d")),
    )
    blanky = "local b=import 'b.libsonnet';\nlocal a=import 'a.libsonnet';\n\n\n\n" \
             "// c\n# h\n{a:1,'b c':2,d:'s',e:\"t\",f:[1,2]}\n"
    for name, flags in flag_runs:
        out.append(Case(f"fmtr-flag-{name}", "fmtr", "jsonnetfmt",
                        flags + (MAIN,), ((MAIN, responsive),)))
        out.append(Case(f"fmtr-flag-{name}-rich", "fmtr", "jsonnetfmt",
                        flags + (MAIN,), ((MAIN, blanky),) + lib_files))

    # --test reports by exit status: 0 clean, 2 would-reformat.  A port that
    # collapses that into 1 fails here and nowhere else.
    for name, src in (("clean", "{ a: 1 }\n"), ("dirty", "{a:1}\n"),
                      ("broken", "{a:1\n"), ("empty", "")):
        out.append(Case(f"fmtr-test-{name}", "fmtr", "jsonnetfmt",
                        ("--test", MAIN), ((MAIN, src),)))
    # -i rewrites the file, so the observation is the file's new contents.
    for name, src in (("clean", "{ a: 1 }\n"), ("dirty", "{a:1,b:2}\n"),
                      ("broken", "{a:1\n")):
        out.append(Case(f"fmtr-inplace-{name}", "fmtr", "jsonnetfmt",
                        ("-i", MAIN), ((MAIN, src),)))
    out.append(Case("fmtr-inplace-many", "fmtr", "jsonnetfmt",
                    ("-i", "a.jsonnet", "b.jsonnet"),
                    (("a.jsonnet", "{a:1}\n"), ("b.jsonnet", "{b:2}\n"))))
    out.append(Case("fmtr-inplace-one-broken", "fmtr", "jsonnetfmt",
                    ("-i", "a.jsonnet", "b.jsonnet"),
                    (("a.jsonnet", "{a:1}\n"), ("b.jsonnet", "{b:\n"))))
    out.append(Case("fmtr-o-file", "fmtr", "jsonnetfmt",
                    ("-o", "out.jsonnet", MAIN), ((MAIN, "{a:1}\n"),)))

    # Idempotence: formatting formatted output must be a fixed point.  Checked
    # here by formatting a source that is already the reference's own output.
    for name, src in (("object", "{ a: 1, b: 2 }\n"),
                      ("array", "[1, 2]\n"),
                      ("local", "local x = 1;\n{ a: x }\n")):
        out.append(Case(f"fmtr-fixedpoint-{name}", "fmtr", "jsonnetfmt",
                        (MAIN,), ((MAIN, src),)))

    # One source per syntactic construct, formatted.  These were written for
    # --debug-desugaring, which turned out to be broken in the reference for every
    # input (spec.ASSERTED_REFERENCE_BUGS), so they run through plain formatting
    # instead: the same sources still exercise the parser and the unparser across
    # every construct in the language, which is what the formatter family is for.
    constructs = (
        ("object", "{a: 1}"),
        ("hidden", "{a:: 1}"),
        ("plus-field", "{a+: 1}"),
        ("computed", "{['a']: 1}"),
        ("object-local", "{local x = 1, a: x}"),
        ("object-assert", "{assert true, a: 1}"),
        ("self", "{a: self.b, b: 1}"),
        ("dollar", "{a: 1, o: {b: $.a}}"),
        ("super", "{a: 1} + {b: super.a}"),
        ("arraycomp", "[x for x in [1, 2]]"),
        ("arraycomp-if", "[x for x in [1, 2] if x > 1]"),
        ("arraycomp-nested", "[x + y for x in [1] for y in [2]]"),
        ("objcomp", "{[k]: 1 for k in ['a']}"),
        ("objcomp-if", "{[k]: 1 for k in ['a'] if k != 'b'}"),
        ("local", "local x = 1; x"),
        ("local-func", "local f(x) = x; f(1)"),
        ("local-recursive", "local f(x) = f(x); 1"),
        ("if-no-else", "if true then 1"),
        ("assert", "assert true; 1"),
        ("assert-msg", "assert true : 'm'; 1"),
        ("error", "error 'x'"),
        ("slice", "[1, 2, 3][1:2]"),
        ("slice-step", "[1, 2, 3][::2]"),
        ("string-index", "'abc'[1]"),
        ("in-op", "'a' in {a: 1}"),
        ("percent-format", "'%s' % 'x'"),
        ("not-equal", "1 != 2"),
        ("chained-compare", "1 < 2"),
        ("unary", "-1"),
        ("import", "import 'a.libsonnet'"),
        ("function-default", "local f(x = 1) = x; f()"),
        ("tailstrict", "local f(x) = x; f(1) tailstrict"),
        ("block-string", "|||\n  a\n|||"),
        ("verbatim-string", "@'a'"),
    )
    for name, src in constructs:
        files = ((MAIN, src),)
        if "import" in src:
            files = files + lib_files
        out.append(Case(f"fmtr-construct-{name}", "fmtr", "jsonnetfmt",
                        (MAIN,), files))
    # The flag itself still has to be accepted, even though its output is not
    # graded.  One case pins that: it must not be an "unrecognized argument"
    # error, which is what a port that never implemented the flag would produce.
    out.append(Case("fmtr-debug-desugaring-accepted", "fmtr", "jsonnetfmt",
                    ("--debug-desugaring", MAIN), ((MAIN, "1 + 1\n"),),
                    note="graded on exit status and stderr, per "
                         "spec.ASSERTED_REFERENCE_BUGS"))
    return out


# --------------------------------------------------------------------------
# the operator matrix
# --------------------------------------------------------------------------
# Every operator against every type pair.  Most combinations are errors, and the
# error text differs per operator and per type, so this is where a port's
# type-checking messages get pinned.  Generated rather than written out: 7 values
# x 19 binary operators is 931 cases, and hand-listing them would guarantee gaps.
OPERAND_VALUES: tuple[tuple[str, str], ...] = (
    ("null", "null"),
    ("bool", "true"),
    ("num", "2"),
    ("str", "'s'"),
    ("arr", "[1]"),
    ("obj", "{a: 1}"),
    ("func", "(function(x) x)"),
)

BINARY_OPS: tuple[tuple[str, str], ...] = (
    ("add", "+"), ("sub", "-"), ("mul", "*"), ("div", "/"), ("mod", "%"),
    ("lt", "<"), ("le", "<="), ("gt", ">"), ("ge", ">="),
    ("eq", "=="), ("ne", "!="),
    ("and", "&&"), ("or", "||"),
    ("band", "&"), ("bor", "|"), ("bxor", "^"),
    ("shl", "<<"), ("shr", ">>"),
    ("in", "in"),
)

UNARY_OPS: tuple[tuple[str, str], ...] = (
    ("neg", "-"), ("pos", "+"), ("not", "!"), ("bnot", "~"),
)


def operators() -> list[Case]:
    out: list[Case] = []
    for lname, lval in OPERAND_VALUES:
        for oname, op in BINARY_OPS:
            for rname, rval in OPERAND_VALUES:
                # `in` takes a string on the left and an object on the right;
                # every other combination is an error, which is the point.
                src = f"{lval} {op} {rval}"
                out.append(ev_exec(f"op-{oname}-{lname}-{rname}", "op", src))
    for uname, op in UNARY_OPS:
        for vname, val in OPERAND_VALUES:
            out.append(ev_exec(f"op-u{uname}-{vname}", "op", f"{op}{val}"))
    # Precedence and associativity, where a hand-written parser drifts.
    prec = (
        "1 + 2 * 3", "2 * 3 + 1", "1 - 2 - 3", "8 / 4 / 2", "2 ^ 3",
        "1 + 2 < 4", "1 < 2 == true", "!true == false", "-2 ^ 2",
        "1 | 2 & 3", "1 ^ 2 | 3", "1 << 2 + 3", "true || false && false",
        "1 + 2 + 'a'", "'a' + 1 + 2", "[1] + [2] + [3]",
        "{a: 1} + {b: 2} + {c: 3}", "1 == 1 == true",
        "-1 - -1", "- - 1", "!!true", "~~1", "+-1",
        "1 < 2 && 2 < 3 || false", "(1 + 2) * 3", "1 + (2 * 3)",
        "std.length('ab') + 1", "if true then 1 else 2 + 3",
        "local x = 1; x + 1",
    )
    for i, src in enumerate(prec):
        out.append(ev_exec(f"op-prec-{i:02d}", "op", src))
    # Integer-valued bitwise operations, which coerce through int64.
    bits = (
        "0 & 0", "255 & 15", "255 | 256", "255 ^ 15", "1 << 0", "1 << 31",
        "1 << 32", "1 << 62", "-1 >> 1", "-8 >> 2", "~0", "~1", "~-1",
        "2147483647 + 1", "9007199254740992 & 1", "(2 ^ 53) & 1",
    )
    for i, src in enumerate(bits):
        out.append(ev_exec(f"op-bits-{i:02d}", "op", src))
    return out


# --------------------------------------------------------------------------
# the upstream conformance suite
# --------------------------------------------------------------------------
# The repository's own tests, used as graded inputs.  These are real jsonnet
# programs written by the project -- broader and nastier than anything I would
# write -- and State A ships them, so a submission can and should run them
# itself.  That is not a leak: the expectations are frozen from the reference,
# not read from the .golden files, so editing a golden changes nothing, and the
# verifier materializes test_suite/ from its own copy of State A rather than
# from the submission.
#
# The file list is discovered from the pristine tree at freeze time rather than
# hardcoded here, so this cannot drift from what State A actually ships.
UPSTREAM_DIRS = ("test_suite", "test_cmd", "examples")


def upstream(files_by_dir: dict[str, list[str]]) -> list[Case]:
    """One evaluation, one format and one desugar per upstream .jsonnet file.

    `files_by_dir` maps a source_dir directory to the .jsonnet files in it,
    relative to that directory, sorted.  The freeze step supplies it by listing
    the pristine State A tree.
    """
    out: list[Case] = []
    for source_dir in UPSTREAM_DIRS:
        for rel in files_by_dir.get(source_dir, []):
            slug = rel.replace("/", "_").removesuffix(".jsonnet")
            argv: tuple[str, ...] = (rel,)
            # stdlib.jsonnet is the one upstream file that needs external
            # variables; run_tests.sh supplies these, so the case does too.
            if source_dir == "test_suite" and rel == "stdlib.jsonnet":
                argv = ("-V", "var1=test",
                        "--ext-code", "var2={x:1,y:2}") + argv
            out.append(Case(f"up-eval-{source_dir}-{slug}", "upstream-eval",
                            "jsonnet", argv, source_dir=source_dir))
            out.append(Case(f"up-fmt-{source_dir}-{slug}", "upstream-fmt",
                            "jsonnetfmt", (rel,), source_dir=source_dir))
            # No --debug-desugaring op: see spec.ASSERTED_REFERENCE_BUGS.  The
            # flag fails identically on every input in the reference, so 189
            # cases here would grade one broken code path.
    return out


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------
# Order matters: it is part of the digest, and the digest is compared between
# two independent freeze runs to prove the case list is deterministic.
AUTHORED_FAMILIES = (
    numbers, object_model, laziness, strings, operators, errors, stdlib,
    format_family, parsers, imports, cli_surface, output_modes, formatter,
)

MIN_CASES = 2000


class CaseError(Exception):
    pass


def assemble(files_by_dir: dict[str, list[str]] | None = None) -> list[Case]:
    """The full case list, self-checked.

    `files_by_dir` is None when the case list is inspected without a State A
    tree at hand (the unit self-test, `python cases.py --stats`).  The upstream
    families are then absent, which is why the case floor is asserted against
    the synthetic families alone -- so a missing tree cannot silently shrink the
    graded surface below the floor and still look healthy.
    """
    cases: list[Case] = []
    for fn in AUTHORED_FAMILIES:
        cases.extend(fn())
    synthetic = len(cases)
    if files_by_dir is not None:
        cases.extend(upstream(files_by_dir))

    ids: dict[str, str] = {}
    keys: dict[str, str] = {}
    for c in cases:
        if c.cid in ids:
            raise CaseError(
                f"duplicate case id {c.cid!r} (families {ids[c.cid]!r} and "
                f"{c.family!r})")
        ids[c.cid] = c.family
        k = c.key()
        if k in keys:
            raise CaseError(
                f"case {c.cid!r} has the same input as {keys[k]!r}; two cases "
                f"grading one input inflate the count without adding coverage")
        keys[k] = c.cid
        if c.binary not in ("jsonnet", "jsonnetfmt"):
            raise CaseError(f"case {c.cid!r} names binary {c.binary!r}")
        if c.source_dir and c.source_dir not in UPSTREAM_DIRS:
            raise CaseError(f"case {c.cid!r} wants source_dir {c.source_dir!r}")
        for path, _ in c.files:
            if path.startswith("/") or ".." in path.split("/"):
                raise CaseError(
                    f"case {c.cid!r} writes outside its directory: {path!r}")

    if synthetic < MIN_CASES:
        raise CaseError(
            f"synthetic case list has {synthetic} cases, floor is {MIN_CASES}")

    # Family set vs the weight table.  When no tree was supplied the source_dir
    # families are legitimately absent, so exclude them from the comparison
    # rather than weakening the check for the real run.
    present = {c.family for c in cases}
    if files_by_dir is None:
        present |= set(spec.UPSTREAM_FAMILIES)
    errs = spec.check_family_coverage(present)
    if errs:
        raise CaseError("; ".join(errs))

    # Every public stdlib field must appear in some case's source.  Scanned from
    # the sources rather than compared against a curated list, because a curated
    # list is a second place to forget a name -- and the whole point of the check
    # is to catch a forgotten name.
    text = "\n".join(
        src for c in cases for _, src in c.files) + "\n".join(
        a for c in cases for a in c.argv)
    missing = sorted(
        n for n in spec.STDLIB_PUBLIC
        if n not in spec.STDLIB_DUNDER and f"std.{n}" not in text)
    if missing:
        raise CaseError(
            f"{len(missing)} public stdlib fields are in no case: "
            f"{', '.join(missing)}")
    return cases


def digest(cases: list[Case]) -> str:
    """Identity of the case list as a whole, over ids and inputs only."""
    h = hashlib.sha256()
    for c in cases:
        h.update(c.cid.encode())
        h.update(b"\x00")
        h.update(c.key().encode())
        h.update(b"\x00")
    return h.hexdigest()


def family_counts(cases: list[Case]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in cases:
        counts[c.family] = counts.get(c.family, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    import sys

    spec.check_or_die()
    cs = assemble()
    print(f"synthetic cases: {len(cs)}")
    print(f"digest: {digest(cs)}")
    for fam, n in family_counts(cs).items():
        print(f"  {fam:20s} {n:5d}")
    if "--json" in sys.argv:
        json.dump([asdict(c) for c in cs], sys.stdout, indent=1, sort_keys=True)
