#!/usr/bin/env python3
"""The corpus: every request both probes are asked.

A case is a probe request plus the family it belongs to.  Nothing here knows what
the answer is -- `freeze.py` runs the reference to find that out, once, when the
verifier image is built.  This file only decides what gets asked.

    {"id": str, "family": str, "request": {...}, "wire_id": int}
    {"id": str, "family": "_protocol", "raw_request": <str|obj>, "solo": bool}

`id` is a stable readable string built from the family and the expression, never an
index.  The corpus is regenerated whenever this file changes, and an index-based id
silently re-points every expectation after the one that moved; a named id turns a
stale expectation into a missing key instead of a wrong answer filed under the
wrong case.

`wire_id` is the integer that goes in the request, assigned over the whole file at
emission.  It is how the executor matches a response to the request that caused
it.

Four stems:

  main        the frozen corpus: everything below, deterministic
  fresh       the same generators under seeds no submission has seen
  protocol    the transport contract, one process per case
  fixture     upstream's own test suite, converted to requests

The split is not organisational.  `protocol` cases have to run one per process
because several of them are answered under id 0 -- the reference could not recover
an id from the line -- so two in one batch are indistinguishable in the reply
stream.  `fresh` has to be generated and answered inside the verifier image,
because by grading time there is no engine left there that could answer a new
expression.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
# A small closed set of named documents, crossed with expressions by the families
# below.  Named rather than inline so that a case id says which document it used,
# and so that two families asking about the same shape ask about the same bytes.

ACCOUNT = {
    "Account": {
        "Account Name": "Firefly",
        "Order": [
            {
                "OrderID": "order103",
                "Product": [
                    {"Product Name": "Bowler Hat", "ProductID": 858383,
                     "SKU": "0406654608", "Description":
                         {"Colour": "Purple", "Width": 300, "Height": 200,
                          "Depth": 210, "Weight": 0.75},
                     "Price": 34.45, "Quantity": 2},
                    {"Product Name": "Trilby hat", "ProductID": 858236,
                     "SKU": "0406634348", "Description":
                         {"Colour": "Orange", "Width": 300, "Height": 200,
                          "Depth": 210, "Weight": 0.6},
                     "Price": 21.67, "Quantity": 1},
                ],
            },
            {
                "OrderID": "order104",
                "Product": [
                    {"Product Name": "Bowler Hat", "ProductID": 858383,
                     "SKU": "040657863", "Description":
                         {"Colour": "Purple", "Width": 300, "Height": 200,
                          "Depth": 210, "Weight": 0.75},
                     "Price": 34.45, "Quantity": 4},
                    {"Product Name": "Cloak", "ProductID": 345664,
                     "SKU": "0406654603", "Description":
                         {"Colour": "Black", "Width": 30, "Height": 20,
                          "Depth": 210, "Weight": 2.0},
                     "Price": 107.99, "Quantity": 1},
                ],
            },
        ],
    },
}

ITEMS = {"items": [
    {"name": "alpha", "price": 2.5, "qty": 4, "tags": ["x", "y"]},
    {"name": "beta", "price": 10, "qty": 1, "tags": []},
    {"name": "gamma", "price": 0.5, "qty": 12, "tags": ["y", "z", "y"]},
]}

NESTED = {"a": {"b": {"c": {"d": {"e": "deep"}}}}, "n": 1}

MIXED = {
    "nul": None, "zero": 0, "negzero": -0.0, "emptyStr": "", "emptyArr": [],
    "emptyObj": {}, "t": True, "f": False, "arr": [1, "two", None, True, [], {}],
    "nested": [[1, [2, [3]]]], "obj": {"z": 1, "a": 2, "m": 3},
    "big": 1e308, "small": 5e-324, "int": 9007199254740993,
    "uni": "héllo 漢 \U0001f600", "ctrl": "a\tb\nc d",
}

NUMS = {"xs": [-2, -1, 0, 1, 2, 2.5, -2.5, 1e10, 1e-10]}

DUPES = {"rows": [
    {"k": "a", "v": 1}, {"k": "b", "v": 2}, {"k": "a", "v": 3},
    {"k": "c", "v": 4}, {"k": "b", "v": 5},
]}

SINGLE = {"a": [{"b": 1}]}
DOUBLE = {"a": [{"b": 1}, {"b": 2}]}

INPUTS: dict[str, object] = {
    "account": ACCOUNT, "items": ITEMS, "nested": NESTED, "mixed": MIXED,
    "nums": NUMS, "dupes": DUPES, "single": SINGLE, "double": DOUBLE,
    "empty": {}, "null": None, "arr": [1, 2, 3], "scalar": 7,
}

# --------------------------------------------------------------------------- #
# case construction
# --------------------------------------------------------------------------- #

MAX_TAG = 56


def _tag(text: str, index: int) -> str:
    """A short, readable, stable tag for an expression."""
    flat = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")
    flat = flat.replace("\t", "\\t")
    if len(flat) > MAX_TAG:
        flat = flat[:MAX_TAG] + "..."
    return f"{index:04d}:{flat}"


def _cross(family: str, exprs: list[str], inputs: list[str],
           ops: tuple[str, ...] = ("eval",), label: str | None = None,
           **extra: object) -> list[dict]:
    """Cross a family's expressions with its documents and ops.

    The id carries the expression, not just its index, so that a case that starts
    failing can be read without cross-referencing the corpus file.

    `label` distinguishes two crossings of the same expressions -- the same request
    under eight different option sets, say -- and goes only into the id.  It is a
    named parameter rather than one more entry in `extra` precisely because `extra`
    is merged into the *request*, and a request carrying a field the protocol does
    not define is answered with a P0006 instead of the answer the case wanted.

    `_absent` as an input name omits `input` entirely, which is a different request
    from one carrying `null`.
    """
    out: list[dict] = []
    prefix = f"{family}/{label}" if label else family
    for i, expr in enumerate(exprs):
        tag = _tag(expr, i)
        for name in inputs:
            for op in ops:
                request: dict[str, object] = {"op": op, "expr": expr}
                if name != "_absent":
                    request["input"] = INPUTS[name]
                request.update(extra)
                out.append({"id": f"{prefix}/{op}/{name}/{tag}",
                            "family": family, "request": request})
    return out


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

def paths() -> list[str]:
    """Navigation: fields, wildcards, descent, predicates, context, indexes.

    The interesting half of this family is not "does `a.b` work" but the four ways
    JSONata's path semantics differ from every other query language: a step over an
    array flattens, a singleton sequence is indistinguishable from a scalar unless
    it was kept, `[0]` on a non-array is the value itself, and a negative index
    counts from the end.  A port that models paths as list-of-values gets the easy
    half and loses the rest.
    """
    return [
        # fields and quoting
        "a", "n", "a.b", "a.b.c", "a.b.c.d", "a.b.c.d.e", "a.b.c.d.e.f",
        "`a`", "`a`.`b`", 'a."b"', "Account", "Account.`Account Name`",
        "Account.Order.Product.`Product Name`",
        "Account.Order.Product.Description.Colour",
        "Account.Order.Product.SKU",
        "$.Account.`Account Name`", "$$.Account.`Account Name`",
        "items.name", "items.price", "items.tags", "items.tags[0]",
        "nul", "zero", "negzero", "emptyStr", "emptyArr", "emptyObj",
        "t", "f", "arr", "obj", "obj.z", "obj.a",
        "missing", "missing.deeper", "a.missing", "missing[0]",
        # wildcards and descent
        "*", "*.*", "*.*.*", "a.*", "a.*.*", "**", "**.c", "**.e",
        "**.Colour", "Account.**.Price", "**.*", "*.**",
        "items.*", "items.**", "$.*", "obj.*",
        # predicates
        "items[price > 1]", "items[price > 1].name", "items[qty >= 4].name",
        "items[name = 'beta']", "items[name != 'beta'].name",
        "items[price > 1 and qty > 2].name",
        "items[price > 1 or qty > 100].name",
        "items[tags]", "items[tags].name", "items[$not(tags)].name",
        "items[0]", "items[1]", "items[2]", "items[3]", "items[-1]",
        "items[-2]", "items[-4]", "items[0].name", "items[-1].name",
        "items[0.4]", "items[1.7]", "items[-0.5]",
        "items[[0..1]].name", "items[[0,2]].name", "items[[2,0]].name",
        "items[[]]", "items[[5]]",
        "arr[0]", "arr[-1]", "arr[$ > 1]", "arr[$ >= 2]",
        "scalar[0]", "scalar[-1]", "scalar[1]", "nul[0]", "zero[0]",
        "Account.Order[0].OrderID", "Account.Order[-1].OrderID",
        "Account.Order.Product[Price > 30].`Product Name`",
        "Account.Order.Product[Quantity > 1].Price",
        "Account.Order[OrderID = 'order104'].Product.SKU",
        "Account.Order.Product[Description.Weight > 1].`Product Name`",
        # singleton vs sequence
        "a.b", "a[0].b", "a.b[0]", "a.b[]", "a[].b", "a.b[0][0]",
        "$count(a.b)", "$count(a[0].b)", "$count(items.tags)",
        # context and parent
        "items.(price * qty)", "items.(name & ':' & qty)",
        "items.{'n': name, 'total': price * qty}",
        "Account.Order.Product.(Price * Quantity)",
        "Account.Order.Product.{'id': ProductID, 'order': %.OrderID}",
        "Account.Order.Product.%.OrderID",
        "Account.Order.Product.%.%.`Account Name`",
        "Account.Order.Product.Description.%.`Product Name`",
        "items.tags.%.name", "items.tags@$t.%.name",
        # binds
        "items@$i.$i.name", "items@$i.{'n': $i.name}",
        "Account.Order@$o.Product@$p.{'o': $o.OrderID, 'p': $p.ProductID}",
        "items^(price).name", "items^(>price).name", "items^(qty).name",
        "items#$i.{'i': $i, 'n': name}",
        "items#$i[$i > 0].name",
        "Account.Order#$oi.Product#$pi.{'o': $oi, 'p': $pi}",
    ]


# --------------------------------------------------------------------------- #
# operators
# --------------------------------------------------------------------------- #

def operators() -> list[str]:
    """Arithmetic, comparison, boolean, concat, range, chain, conditional.

    Includes the cases where an operator meets something it was not designed for --
    a string in `+`, an array in `<`, `null` in `and` -- because that is where the
    error codes live (T2001..T2010, D1001), and an error code is a behaviour.
    """
    out = [
        # arithmetic, including the results JSON cannot spell
        "1 + 1", "1 - 2", "3 * 4", "10 / 4", "10 % 3", "-7 % 3", "7 % -3",
        "$power(2, 10)", "$power(2, 0.5)", "$power(-8, 1/3)",
        "1 / 0", "-1 / 0", "0 / 0", "0 * -1", "-(0)", "1e308 * 10",
        "-1e308 * 10", "1e-320 / 1e10", "0.1 + 0.2", "0.3 - 0.1",
        "9007199254740992 + 1", "9007199254740993 - 1",
        "1 + 2 * 3", "(1 + 2) * 3", "2 * 3 + 4 * 5", "-3 - -4", "- -5",
        "1 - - 1", "10 / 5 / 2", "2 * (3 + (4 - 1))",
        # arithmetic on the wrong types
        "'a' + 1", "1 + 'a'", "true + 1", "[1] + 1", "{} + 1", "nul + 1",
        "missing + 1", "1 + missing", "missing * missing",
        # concatenation coerces everything
        "'a' & 'b'", "1 & 2", "'a' & 1", "true & 'x'", "nul & 'x'",
        "[1,2] & 'x'", "{'a':1} & 'x'", "missing & 'x'", "'x' & missing",
        "'' & ''", "1.0 & ''", "1e21 & ''", "0.000001 & ''",
        "1e-7 & ''", "-0 & ''", "100000000000000000000 & ''",
        # comparison
        "1 < 2", "2 <= 2", "3 > 4", "4 >= 4", "'a' < 'b'", "'b' <= 'a'",
        "1 = 1", "1 = '1'", "1 != 2", "'a' = 'a'", "true = true",
        "true = 1", "nul = nul", "nul = 0", "missing = missing",
        "missing = 1", "1 = missing",
        "[1,2] = [1,2]", "[1,2] = [2,1]", "{'a':1} = {'a':1}",
        "{'a':1} = {'b':1}", "[1,[2]] = [1,[2]]",
        "1 < 'a'", "true < false", "[1] < [2]", "{} < {}",
        "nul < 1", "missing < 1", "1 < missing",
        "arr = [1,2,3]", "arr[0] = 1", "obj = {'z':1,'a':2,'m':3}",
        "obj = {'a':2,'m':3,'z':1}",
        # boolean and coercion
        "true and true", "true and false", "false or true", "$not(true)",
        "$not(false)", "$not(0)", "$not(1)", "$not('')", "$not('x')",
        "$not(nul)", "$not(missing)", "$not([])", "$not([0])", "$not({})",
        "$not({'a':1})", "1 and 1", "0 and 1", "'' or 'x'", "[] or []",
        "[0] and [0]", "{} or {}", "{'a':1} and {'a':1}",
        "nul and true", "missing and true", "true and missing",
        "missing or false", "'x' and 'y'", "[[]] and true",
        # range
        "[1..5]", "[1..1]", "[5..1]", "[-2..2]", "[0..0]",
        "[1..3, 7..9]", "[1..2, 2..3]", "[1.0..3.0]", "[1..3][1]",
        "$count([1..100])", "$sum([1..10])", "[1.5..3]", "[1..'3']",
        "[nul..3]", "[missing..3]", "[1..missing]",
        # conditional and coalescing
        "true ? 1 : 2", "false ? 1 : 2", "0 ? 1 : 2", "'' ? 1 : 2",
        "nul ? 1 : 2", "missing ? 1 : 2", "[] ? 1 : 2", "[0] ? 1 : 2",
        "{} ? 1 : 2", "true ? 1", "false ? 1", "missing ? 1",
        "missing ?? 'dflt'", "nul ?? 'dflt'", "0 ?? 'dflt'",
        "'' ?? 'dflt'", "[] ?? 'dflt'", "false ?? 'dflt'",
        "missing ?? missing ?? 3", "zero ?? 9", "nul ?? 9", "emptyArr ?? 9",
        # chain, block, variable
        "[1,2,3] ~> $sum", "[1,2,3] ~> $sum ~> $string",
        "'abc' ~> $uppercase ~> $length",
        "[3,1,2] ~> $sort ~> $reverse", "items ~> $count",
        "($x := 1; $x + 1)", "($x := 1; $y := 2; $x * $y)",
        "($x := 1; ($x := 2; $x); $x)", "(1; 2; 3)", "()",
        "($f := function($x) { $x + 1 }; $f(1))",
        "($x := items; $count($x))",
        # in / includes
        "1 in [1,2,3]", "4 in [1,2,3]", "'x' in ['x']", "1 in 1",
        "1 in missing", "missing in [1]", "nul in [nul]",
        "'alpha' in items.name", "'zeta' in items.name",
    ]
    return out


# --------------------------------------------------------------------------- #
# strings
# --------------------------------------------------------------------------- #

def strings() -> list[str]:
    """The string library, and the literal syntax that feeds it.

    Substring arithmetic is the part worth being careful about: JSONata's
    `$substring` clamps, accepts negatives, and rounds non-integers, and it does all
    three in a particular order.  Surrogate pairs are here too, because `$length`
    counts code points while a naive port counts UTF-16 units.
    """
    return [
        # literals and escapes
        "'plain'", '"double"', "''", '""', "'quote\\''", '"quote\\""',
        "'\"'", '"\'"',
        "'tab\\there'", "'nl\\nhere'", "'cr\\rhere'", "'bs\\\\here'",
        "'sl\\/here'", "'bell\\bhere'", "'ff\\fhere'",
        "'u\\u0041here'", "'u\\u00e9here'", "'u\\u4e2dhere'",
        "'\\ud83d\\ude00'", "'héllo'", "'漢字'", "'\U0001f600'",
        "'\\u0000'", "'\\u001f'", "'\\u007f'", "'a\\u0301'",
        # length, case, trim, pad
        "$length('')", "$length('abc')", "$length('héllo')",
        "$length('\U0001f600')", "$length('a\U0001f600b')",
        "$length(uni)", "$length(missing)", "$length(nul)",
        "$uppercase('aBc')", "$lowercase('AbC')", "$uppercase('héllo')",
        "$uppercase('ß')", "$lowercase('İ')", "$uppercase(missing)",
        "$trim('  a  b  ')", "$trim('\\t a \\n')", "$trim('')",
        "$trim(' a\\u00a0b ')", "$trim(missing)",
        "$pad('x', 5)", "$pad('x', -5)", "$pad('x', 5, '-')",
        "$pad('x', -5, '-')", "$pad('abc', 2)", "$pad('', 3, 'ab')",
        "$pad('x', 0)", "$pad('x', 5, '')",
        # substring family
        "$substring('hello', 0)", "$substring('hello', 1)",
        "$substring('hello', -2)", "$substring('hello', 10)",
        "$substring('hello', 0, 2)", "$substring('hello', 1, 100)",
        "$substring('hello', -3, 2)", "$substring('hello', 1, -1)",
        "$substring('hello', 1.5, 2.5)", "$substring('hello', 0, 0)",
        "$substring('\U0001f600ab', 0, 1)", "$substring('\U0001f600ab', 1)",
        "$substringBefore('a-b-c', '-')", "$substringBefore('abc', 'x')",
        "$substringBefore('abc', '')", "$substringAfter('a-b-c', '-')",
        "$substringAfter('abc', 'x')", "$substringAfter('abc', '')",
        # search and split
        "$contains('abc', 'b')", "$contains('abc', 'z')",
        "$contains('abc', '')", "$contains('', '')",
        "$split('a,b,c', ',')", "$split('a,b,c', ',', 2)",
        "$split('abc', '')", "$split('', ',')", "$split('a,,b', ',')",
        "$split(',a,', ',')", "$split('a1b2c', /[0-9]/)",
        "$join(['a','b'])", "$join(['a','b'], '-')", "$join([], '-')",
        "$join('a', '-')", "$join([1,2], '-')", "$join(items.name, '|')",
        # replace
        "$replace('abcabc', 'b', 'X')", "$replace('abcabc', 'b', 'X', 1)",
        "$replace('abc', '', 'X')", "$replace('abc', 'z', 'X')",
        "$replace('abc', 'b', '')", "$replace('aaa', 'a', 'aa')",
        "$replace('abc', 'b', 'X', 0)", "$replace('abc', 'b', 'X', -1)",
        "$replace('a-b', /[-]/, ':')", "$replace('a1b2', /[0-9]/, 'N')",
        "$replace('a1b2', /([0-9])/, '<$1>')",
        "$replace('a1b2', /([0-9])/, '$0-$1')",
        "$replace('a1b2', /([0-9])/, '$$')",
        "$replace('a1b2', /([0-9])/, function($m) { $m.match & '!' })",
        "$replace('abc', /b/, function($m) { $m.groups })",
        # encode / decode
        "$base64encode('hello')", "$base64encode('')",
        "$base64encode('héllo')", "$base64decode('aGVsbG8=')",
        "$base64decode('')", "$base64decode('!!!')",
        "$encodeUrl('a b&c=d/e?f')", "$encodeUrlComponent('a b&c=d/e?f')",
        "$decodeUrl('a%20b')", "$decodeUrlComponent('a%2Bb')",
        "$decodeUrl('%zz')", "$decodeUrlComponent('%')",
        "$encodeUrl('漢')", "$encodeUrlComponent('\U0001f600')",
        # eval and error
        "$eval('1+1')", "$eval('$x', {})", "$eval('a.b', {'a':{'b':2}})",
        "$eval('1+')", "$eval('$notafunc()')", "$eval(missing)",
        "$string(1)", "$string('a')", "$string(true)", "$string(nul)",
        "$string(missing)", "$string([1,2])", "$string({'a':1})",
        "$string([1,2], true)", "$string({'a':{'b':1}}, true)",
        "$string(1/0)", "$string(-0)", "$string($string)",
        "$string(function($x){$x})",
    ]


# --------------------------------------------------------------------------- #
# numbers
# --------------------------------------------------------------------------- #

def numbers() -> list[str]:
    """Numeric functions, and the two picture-string formatters.

    `$formatNumber` implements XPath's `format-number`, which is a small language of
    its own: digit families, grouping separators, per-mille, exponents, patterns with
    a negative sub-picture.  It is the single densest function in the library and the
    one a port is most likely to reimplement approximately.
    """
    return [
        # literals
        "0", "-0", "1", "-1", "1.5", "-1.5", "0.5",
        "1e3", "1e-3", "1E3", "1.5e3", "-1.5e-3", "1e400", "-1e400",
        "1e-400", "9007199254740993", "0.1", "1000000000000000000000",
        "0.000001", "0.0000001", "123456789012345678901234567890",
        # rounding
        "$round(1.5)", "$round(2.5)", "$round(-1.5)", "$round(-2.5)",
        "$round(0.5)", "$round(1.45, 1)", "$round(1.55, 1)",
        "$round(12345, -2)", "$round(12350, -2)", "$round(12450, -2)",
        "$round(1.005, 2)", "$round(-0.5)", "$round(0)", "$round(-0)",
        "$floor(1.5)", "$floor(-1.5)", "$ceil(1.5)", "$ceil(-1.5)",
        "$floor(-0)", "$ceil(-0.5)",
        "$abs(-1.5)", "$abs(1.5)", "$abs(-0)", "$abs(1/0)",
        # roots, powers, logs
        "$sqrt(4)", "$sqrt(2)", "$sqrt(0)", "$sqrt(-1)", "$sqrt(-0)",
        "$power(2, 3)", "$power(2, -1)", "$power(-2, 0.5)", "$power(0, 0)",
        "$power(1e300, 2)", "$power(2, 1024)",
        # conversions
        "$number('1')", "$number('1.5')", "$number('-0')", "$number('0x10')",
        "$number('1e3')", "$number('')", "$number(' 1 ')", "$number('1,000')",
        "$number('Infinity')", "$number('NaN')", "$number(true)",
        "$number(false)", "$number(nul)", "$number(missing)",
        "$number([1])", "$number([1,2])", "$number({})", "$number('abc')",
        "$number(1e400 & '')",
        # sums and aggregates
        "$sum([1,2,3])", "$sum([])", "$sum([1.1, 2.2])", "$sum(1)",
        "$sum(nums.xs)", "$sum(items.price)", "$sum(missing)",
        "$sum(['a'])", "$max([1,2,3])", "$max([])", "$max(nums.xs)",
        "$min([1,2,3])", "$min([])", "$min(nums.xs)",
        "$average([1,2,3])", "$average([])", "$average([1,2])",
        "$max(missing)", "$min(missing)", "$average(missing)",
        # random and formatting
        "$formatNumber(12345.6, '#,###.00')",
        "$formatNumber(12345.6, '0,000.00')",
        "$formatNumber(1234.5678, '#,##0.000')",
        "$formatNumber(0.14, '00.0%')",
        "$formatNumber(0.14, '###.0‰')",
        "$formatNumber(-1234.5, '#,##0.0;(#,##0.0)')",
        "$formatNumber(-1234.5, '#,##0.0')",
        "$formatNumber(1234.5, '#,##0.0;(#,##0.0)')",
        "$formatNumber(12345678, '#.#e0')",
        "$formatNumber(0.00012, '0.###E0')",
        "$formatNumber(1, '#')", "$formatNumber(0, '#')",
        "$formatNumber(0, '0')", "$formatNumber(1.5, '#')",
        "$formatNumber(2.5, '#')", "$formatNumber(-0, '#')",
        "$formatNumber(1e21, '#')", "$formatNumber(1/0, '#')",
        "$formatNumber(1234, '#,#')", "$formatNumber(1234, '###,###')",
        "$formatNumber(1234, '#')", "$formatNumber(1234, 'x#y')",
        "$formatNumber(1234, '')", "$formatNumber(1234, '##;##;##')",
        "$formatNumber(1234, '#.#.#')", "$formatNumber(1234, '#,')",
        "$formatNumber(1234, ',#')", "$formatNumber(1234, '#%')",
        "$formatNumber(1234, '#%‰')",
        "$formatNumber(1234, '#', {'decimal-separator': ',', 'grouping-separator': '.'})",
        "$formatNumber(1234.5, '#,##0.0', {'decimal-separator': ',', 'grouping-separator': ' '})",
        "$formatNumber(1234, '#', {'zero-digit': '٠'})",
        "$formatNumber(1234, '#', {'minus-sign': '~'})",
        "$formatNumber(-1234, '#', {'minus-sign': '~'})",
        "$formatNumber(1234, '#', {'nope': 'x'})",
        "$formatNumber('x', '#')",
        "$formatBase(255, 16)", "$formatBase(255, 2)", "$formatBase(255)",
        "$formatBase(255, 1)", "$formatBase(255, 37)", "$formatBase(-255, 16)",
        "$formatBase(255.7, 16)", "$formatBase(0, 16)",
        "$formatInteger(12345, '#,##0')", "$formatInteger(7, 'w')",
        "$formatInteger(7, 'W')", "$formatInteger(7, 'Ww')",
        "$formatInteger(7, 'o')", "$formatInteger(21, 'o(en)')",
        "$formatInteger(7, 'i')", "$formatInteger(7, 'I')",
        "$formatInteger(7, 'a')", "$formatInteger(7, 'A')",
        "$formatInteger(1999, 'I')", "$formatInteger(0, 'I')",
        "$formatInteger(-7, 'w')", "$formatInteger(1.5, '#')",
        "$parseInteger('12,345', '#,##0')", "$parseInteger('seven', 'w')",
        "$parseInteger('MCMXCIX', 'I')", "$parseInteger('vii', 'i')",
        "$parseInteger('7th', 'o')", "$parseInteger('nope', 'w')",
    ]


# --------------------------------------------------------------------------- #
# datetime
# --------------------------------------------------------------------------- #

def datetimes() -> list[str]:
    """`$fromMillis` / `$toMillis` and their picture strings.

    Every case here is asked with `clock: "pinned"`, so `$now` and `$millis` return
    a fixed instant.  Without that this family would be unusable: the corpus is
    frozen once and graded months later.
    """
    return [
        # ISO round trips
        "$toMillis('2020-01-01T00:00:00.000Z')",
        "$toMillis('1970-01-01T00:00:00Z')",
        "$toMillis('1969-12-31T23:59:59Z')",
        "$toMillis('2020-02-29T12:00:00Z')",
        "$toMillis('2020-12-31T23:59:59.999Z')",
        "$toMillis('2020-01-01T00:00:00+05:30')",
        "$toMillis('2020-01-01T00:00:00-08:00')",
        "$toMillis('2020-01-01')", "$toMillis('2020-01')",
        "$toMillis('2020')", "$toMillis('nope')", "$toMillis('')",
        "$toMillis('2020-13-01T00:00:00Z')",
        "$toMillis('2020-01-32T00:00:00Z')",
        "$fromMillis(0)", "$fromMillis(1)", "$fromMillis(-1)",
        "$fromMillis(1577836800000)", "$fromMillis(-2208988800000)",
        "$fromMillis(253402300799999)", "$fromMillis(1.5)",
        "$fromMillis(-0)", "$fromMillis(1/0)",
        # picture strings
        "$fromMillis(1577836800000, '[Y0001]-[M01]-[D01]')",
        "$fromMillis(1577836800000, '[D01]/[M01]/[Y0001]')",
        "$fromMillis(1577923445678, '[H01]:[m01]:[s01].[f001]')",
        "$fromMillis(1577923445678, '[h#1]:[m01] [P]')",
        "$fromMillis(1577923445678, '[h]:[m] [PN]')",
        "$fromMillis(1577836800000, '[F]')",
        "$fromMillis(1577836800000, '[FNn]')",
        "$fromMillis(1577836800000, '[F0]')",
        "$fromMillis(1577836800000, '[MNn] [D1o], [Y]')",
        "$fromMillis(1577836800000, '[MN,*-3]')",
        "$fromMillis(1577836800000, '[Mn]')",
        "$fromMillis(1577836800000, '[D1o] of [MNn]')",
        "$fromMillis(1577836800000, '[dwo] day')",
        "$fromMillis(1577836800000, '[d]')", "$fromMillis(1577836800000, '[W]')",
        "$fromMillis(1577836800000, '[w]')", "$fromMillis(1577836800000, '[xNn]')",
        "$fromMillis(1577836800000, '[X0001]-W[W01]-[F1]')",
        "$fromMillis(1577836800000, '[Y0001]')",
        "$fromMillis(1577836800000, '[Y]')",
        "$fromMillis(1577836800000, '[Y,2-2]')",
        "$fromMillis(1577836800000, '[Y#4]')",
        "$fromMillis(1577836800000, '[E]')",
        "$fromMillis(1577836800000, '[Z]')",
        "$fromMillis(1577836800000, '[Z0]')",
        "$fromMillis(1577836800000, '[ZN]')",
        "$fromMillis(1577836800000, '[z]')",
        "$fromMillis(1577836800000, '[Y]', '+0530')",
        "$fromMillis(1577836800000, '[H01]:[m01]', '-0800')",
        "$fromMillis(1577836800000, '[H01]', '+0000')",
        "$fromMillis(1577836800000, '[H01]', 'nope')",
        "$fromMillis(1577836800000, 'literal [[Y]] text')",
        "$fromMillis(1577836800000, '[')",
        "$fromMillis(1577836800000, '[Q]')",
        "$fromMillis(1577836800000, '')",
        "$fromMillis(1577836800000, '[Y0001][')",
        "$fromMillis(1577836800000, '[Y1,*-2]')",
        "$fromMillis(1577836800000, '[Ya]')",
        "$fromMillis(1577836800000, '[YI]')",
        "$fromMillis(1577836800000, '[Yw]')",
        # parsing with pictures
        "$toMillis('01/01/2020', '[D01]/[M01]/[Y0001]')",
        "$toMillis('2020-001', '[Y0001]-[d001]')",
        "$toMillis('1 January 2020', '[D1] [MNn] [Y]')",
        "$toMillis('Jan 1 2020', '[MN,*-3] [D1] [Y]')",
        "$toMillis('2020-W01-3', '[X0001]-W[W01]-[F1]')",
        # A time-only picture, with the date the parser defaulted erased from the
        # answer.  The bare `$toMillis('7 pm', '[h] [P]')` is what belongs here and
        # cannot be: a picture with no date field defaults the date from the clock,
        # and the clock this reads is not one `clock: "pinned"` can reach.  See
        # TOMILLIS_CLOCK_ALLOWED in `_self_check` for why, and `identity.py`'s
        # shifted-clock pass for what notices a spelling that gets this wrong.
        #
        # Wrapped rather than dropped, so the path is still graded end to end: this
        # asserts `[h]` parsed as 7, `[P]` carried it to 19, and every field right of
        # the least significant one defaulted to zero.  Only the absolute epoch value
        # is lost, which is the part no expectation could have held.
        "$fromMillis($toMillis('7 pm', '[h] [P]'), '[H01]:[m01]:[s01].[f001]')",
        "$toMillis('twenty twenty', '[Yw]')",
        "$toMillis('MMXX', '[YI]')",
        "$toMillis('nope', '[Y0001]')",
        "$toMillis('2020', '[Y0001]-[M01]')",
        # the clock
        "$now()", "$millis()", "$now() = $now()", "$millis() = $millis()",
        "$now('[Y0001]')", "$now('[H01]:[m01]', '+0100')",
        "$toMillis($now())", "$fromMillis($millis())",
        "($a := $millis(); $b := $millis(); $a = $b)",
    ]


# --------------------------------------------------------------------------- #
# collections
# --------------------------------------------------------------------------- #

def collections() -> list[str]:
    """Arrays, objects, and the constructors that build them.

    Object construction is where key order becomes observable: JSONata preserves
    insertion order, `$merge` lets a later key win while keeping the earlier
    position, and `$each`/`$spread` walk in that order.  The wire encoding carries
    ordered pairs precisely so this family can be graded.
    """
    return [
        # array functions
        "$count([])", "$count([1,2])", "$count(1)", "$count(missing)",
        "$count(nul)", "$count([[1,2]])", "$count(items)",
        "$append([1],[2])", "$append(1, 2)", "$append([1], 2)",
        "$append(missing, [1])", "$append([1], missing)",
        "$append([], [])", "$append(nul, nul)",
        "$reverse([1,2,3])", "$reverse([])", "$reverse(1)",
        "$reverse(missing)", "$reverse(items.name)",
        "$sort([3,1,2])", "$sort(['b','a'])", "$sort([])", "$sort(1)",
        "$sort([1,'a'])", "$sort([true,false])", "$sort(missing)",
        "$sort([3,1,2], function($a,$b){ $a < $b })",
        "$sort(items, function($a,$b){ $a.price > $b.price }).name",
        "$sort([1,2,3], function($a,$b){ true })",
        "$sort([1,2,3], function($a,$b){ 'x' })",
        "$distinct([1,1,2])", "$distinct([])", "$distinct(1)",
        "$distinct([[1],[1]])", "$distinct([{'a':1},{'a':1}])",
        "$distinct(['a','a','b'])", "$distinct(dupes.rows.k)",
        "$zip([1,2],[3,4])", "$zip([1,2],[3])", "$zip([1],[2],[3])",
        "$zip([])", "$zip(1,2)", "$zip([1,2])",
        "$single([1])", "$single([1,2])", "$single([])",
        "$single([1,2], function($v){ $v = 2 })",
        "$single(items, function($v){ $v.name = 'beta' }).price",
        # array constructors and flattening
        "[1,2,3]", "[[1,2],[3]]", "[1,[2,[3]]]", "[]", "[[]]", "[[[]]]",
        "[missing]", "[nul]", "[missing, 1]", "[items.name]",
        "[items.name[]]", "[1..3][]", "[[1,2]][0]",
        "items.[name]", "items.[tags]", "items.tags[]",
        "$count([missing])", "$count([nul])",
        # object constructors
        "{}", "{'a': 1}", "{'a': 1, 'b': 2}", "{'a': missing}",
        "{'a': nul}", "{'b': 2, 'a': 1}", "{'a': 1, 'a': 2}",
        "{'a': {'b': {}}}", "{items.name: price}", "{'k': items.name}",
        "items{name: price}", "items{'all': price}",
        "dupes.rows{k: v}", "dupes.rows{k: [v]}", "dupes.rows{v: k}",
        "Account.Order.Product{`Product Name`: Price}",
        "Account.Order.Product{`Product Name`: [Price]}",
        "{1: 2}", "{nul: 1}", "{true: 1}", "{[1]: 2}",
        # object functions
        "$keys({'z':1,'a':2})", "$keys([{'a':1},{'b':2}])",
        "$keys([{'a':1},{'a':2}])", "$keys(1)", "$keys(missing)",
        "$keys(obj)", "$keys([])",
        "$lookup({'a':1}, 'a')", "$lookup({'a':1}, 'z')",
        "$lookup([{'a':1},{'a':2}], 'a')", "$lookup(1, 'a')",
        "$lookup(obj, 'z')", "$lookup(missing, 'a')",
        "$spread({'a':1,'b':2})", "$spread([{'a':1},{'b':2}])",
        "$spread(1)", "$spread([])", "$spread(obj)", "$spread(missing)",
        "$merge([{'a':1},{'b':2}])", "$merge([{'a':1},{'a':2}])",
        "$merge([{'b':1},{'a':2},{'b':3}])", "$merge([])",
        "$merge({'a':1})", "$merge(1)", "$merge([1])", "$merge(missing)",
        "$each({'a':1,'b':2}, function($v,$k){ $k & $v })",
        "$each({'a':1}, function($v){ $v })",
        "$each(obj, function($v,$k){ $k })",
        "$each(1, function($v){ $v })", "$each(missing, function($v){ $v })",
        "$type(1)", "$type('a')", "$type(true)", "$type(nul)",
        "$type([])", "$type({})", "$type(missing)", "$type($type)",
        "$type(function($x){$x})", "$type(1/0)",
        "$exists(1)", "$exists(nul)", "$exists(missing)", "$exists([])",
        "$exists(a.b)", "$exists(a.missing)",
        "$boolean(1)", "$boolean(0)", "$boolean('')", "$boolean('x')",
        "$boolean([])", "$boolean([0])", "$boolean([0,0])", "$boolean([[]])",
        "$boolean({})", "$boolean({'a':1})", "$boolean(nul)",
        "$boolean(missing)", "$boolean($boolean)",
        "$boolean(function($x){$x})",
        "$assert(true, 'msg')", "$assert(false, 'msg')",
        "$assert(1, 'msg')", "$assert(missing, 'msg')",
        "$error('boom')", "$error()",
        # `$shuffle` reaches `Math.random` directly -- not the `$random` binding the
        # probe shadows, so nothing can pin it -- and a bare `$shuffle([1,2,3])` is
        # therefore unanswerable: the reference disagrees with its own frozen answer
        # five times in six. It is still a published builtin a port has to implement,
        # so it is exercised through spellings whose answer does not depend on the
        # permutation. All three elements go through the Fisher-Yates loop in each.
        # See SHUFFLE_ALLOWED in `_self_check`, which is what keeps a bare one from
        # coming back.
        #
        # Kept at the tail deliberately. `callback` grades `collections()[:40]` and the
        # mutator's base takes `[:60]`, so inserting five entries mid-list pushed five
        # expressions out of each window -- `$zip([1,2],[3,4])`, `[items.name]` and
        # four others stopped being covered, invisibly, as a side effect of an edit
        # about something else. Appending grades the new spellings in the `collections`
        # family (which takes the whole pool) and leaves both windows untouched. Add
        # here, not above.
        "$sort($shuffle([1,2,3]))", "$count($shuffle([1,2,3]))",
        "$sum($shuffle([1,2,3]))", "$shuffle([1])", "$shuffle([])",
    ]


# --------------------------------------------------------------------------- #
# behavioural
# --------------------------------------------------------------------------- #

def functionals() -> list[str]:
    """Lambdas, closures, higher-order functions, signatures, recursion.

    JSONata applies a lambda with as many arguments as it declares and passes the
    extras that `$map` and `$filter` offer only if the lambda asked for them, so
    arity is observable.  Signatures are a second type system layered on top: `<n:n>`
    rejects a string with T0410 before the body runs, and `<x-` binds the context.
    Tail recursion has to actually be tail-recursive or the stack goes.
    """
    return [
        # lambdas and application
        "function($x){ $x }(1)", "function($x,$y){ $x + $y }(1,2)",
        "function(){ 1 }()", "function($x){ $x }()",
        "function($x){ $x }(1,2)", "λ($x){ $x }(1)",
        "($f := function($x){ $x * 2 }; $f(3))",
        "($f := function($x){ $x * 2 }; $f)",
        "($f := function($x){ $x * 2 }; $type($f))",
        "($f := λ($x){ $x }; $f('a'))",
        "1 ~> function($x){ $x + 1 }",
        "[1,2] ~> function($x){ $count($x) }",
        # closures and scope
        "($n := 10; $f := function($x){ $x + $n }; $f(1))",
        "($n := 10; $f := function($x){ $x + $n }; $n := 20; $f(1))",
        "($f := function($x){ function($y){ $x + $y } }; $f(1)(2))",
        "($f := function($x){ $x }; $g := function($x){ $f($x) + 1 }; $g(1))",
        "($x := 1; function(){ $x }())",
        "items.(function($n){ $n }(name))",
        "($f := function($x){ $x.price }; $f(items[0]))",
        # higher order
        "$map([1,2,3], function($v){ $v * 2 })",
        "$map([1,2,3], function($v,$i){ $i })",
        "$map([1,2,3], function($v,$i,$a){ $count($a) })",
        "$map([1,2,3], function($v){ $v > 1 ? $v })",
        "$map([], function($v){ $v })", "$map(1, function($v){ $v })",
        "$map(missing, function($v){ $v })",
        "$map([1,2], $string)", "$map(items.name, $uppercase)",
        "$map([1,2], function($v){ missing })",
        "$filter([1,2,3], function($v){ $v > 1 })",
        "$filter([1,2,3], function($v,$i){ $i > 0 })",
        "$filter([1,2,3], function($v,$i,$a){ $v = $a[0] })",
        "$filter([1,2,3], function($v){ $v })",
        "$filter([], function($v){ true })",
        "$filter(items, function($v){ $v.qty > 1 }).name",
        "$reduce([1,2,3], function($a,$b){ $a + $b })",
        "$reduce([1,2,3], function($a,$b){ $a + $b }, 10)",
        "$reduce([], function($a,$b){ $a + $b })",
        "$reduce([], function($a,$b){ $a + $b }, 5)",
        "$reduce([1], function($a,$b){ $a + $b })",
        "$reduce([1,2,3], function($a,$b,$i){ $a + $i }, 0)",
        "$reduce([1,2,3], function($a,$b,$i,$c){ $count($c) }, 0)",
        "$reduce([1,2], function($a){ $a })",
        "$reduce(items, function($a,$b){ $a + $b.price }, 0)",
        "$sift({'a':1,'b':2}, function($v){ $v > 1 })",
        "$sift({'a':1,'b':2}, function($v,$k){ $k = 'a' })",
        "$sift(obj, function($v){ $v > 1 })",
        "$sift(1, function($v){ true })",
        "items.$sift(function($v){ $v = 'alpha' })",
        # partial application
        "($add := function($a,$b){ $a + $b }; $add(1, 2))",
        "($add := function($a,$b){ $a + $b }; $add(1, ?)(2))",
        "($add := function($a,$b){ $a + $b }; $add(?, 2)(1))",
        "($add := function($a,$b,$c){ $a & $b & $c }; $add(?, 'b', ?)('a','c'))",
        "($s := $substring(?, 0, 2); $s('hello'))",
        "$substring(?, 1)('abc')", "$sum(?)([1,2])",
        "($f := $string(?); $f(1))",
        # signatures
        "function($x)<n:n>{ $x }(1)",
        "function($x)<n:n>{ $x }('a')",
        "function($x)<s:s>{ $x }(1)",
        "function($x)<b:b>{ $x }(true)",
        "function($x)<a:n>{ $count($x) }([1,2])",
        "function($x)<a:n>{ $count($x) }(1)",
        "function($x)<o:n>{ 1 }({})",
        "function($x)<f:n>{ 1 }($string)",
        "function($x)<x:x>{ $x }(nul)",
        "function($x)<j:j>{ $x }(1)",
        "function($x)<j:j>{ $x }($string)",
        "function($x,$y)<nn:n>{ $x + $y }(1,2)",
        "function($x,$y)<n-n:n>{ $x + $y }(1,2)",
        "function($x,$y)<nn?:n>{ $x }(1)",
        "function($x)<a<n>:n>{ $count($x) }([1,2])",
        "function($x)<a<n>:n>{ $count($x) }(['a'])",
        "function($x)<(sn):s>{ $string($x) }(1)",
        "function($x)<(sn):s>{ $string($x) }(true)",
        "function($x)<:n>{ 1 }()",
        "function($x)<nope>{ 1 }(1)",
        "1 ~> function($x)<n:n>{ $x }",
        "'a' ~> function($x)<n:n>{ $x }",
        "items ~> function($x)<a:n>{ $count($x) }",
        # recursion and tail calls
        "($f := function($n){ $n <= 1 ? 1 : $n * $f($n - 1) }; $f(5))",
        "($f := function($n){ $n <= 1 ? 1 : $n * $f($n - 1) }; $f(20))",
        "($f := function($n,$a){ $n <= 0 ? $a : $f($n - 1, $a + $n) }; $f(2000, 0))",
        "($f := function($n,$a){ $n <= 0 ? $a : $f($n - 1, $a + $n) }; $f(20000, 0))",
        "($f := function($n){ $n = 0 ? 0 : $f($n - 1) }; $f(5000))",
        "($even := function($n){ $n = 0 ? true : $odd($n - 1) };"
        " $odd := function($n){ $n = 0 ? false : $even($n - 1) }; $even(1000))",
        "($f := function($n){ $n <= 1 ? $n : $f($n-1) + $f($n-2) }; $f(15))",
        # library functions as values
        "$string", "$sum", "$type($sum)", "$string($sum)",
        "[$sum, $count]", "{'f': $sum}", "$count([$sum, $count])",
        "$map([$sum], $type)", "$sum ~> $type", "$type($type)",
        "$type(function(){1})", "$string(function(){1})",
    ]


# --------------------------------------------------------------------------- #
# regex
# --------------------------------------------------------------------------- #

def regexes() -> list[str]:
    """Regex literals, `$match`, and the functions that take a pattern.

    A regex literal in JSONata evaluates to a *function*, which is why `$match` and
    `$replace` accept one interchangeably with a string.  The match object is
    `{match, index, groups}` and the group list omits the whole match, which is one
    off from most host languages' convention.
    """
    return [
        "$match('ababab', /ab/)", "$match('ababab', /(a)(b)/)",
        "$match('ababab', /ab/, 2)", "$match('ababab', /ab/, 0)",
        "$match('ababab', /ab/, -1)", "$match('abc', /z/)",
        "$match('abc', /b*/)", "$match('', /a*/)", "$match('abc', //)",
        "$match('aaa', /a*?/)", "$match('abc', /(?<g>b)/)",
        "$match('abc', /(b)|(z)/)", "$match('AB', /ab/)",
        "$match('AB', /ab/i)", "$match('a\\nb', /^b$/m)",
        "$match('a\\nb', /a.b/s)", "$match('abc', 'b')",
        "$match(missing, /a/)", "$match('abc', missing)",
        "$match(1, /1/)", "$match('abc', /b/).match",
        "$match('abc', /b/).index", "$match('abc', /(b)/).groups",
        "$match('abc', /b/).groups", "$count($match('ababab', /ab/))",
        "/ab/", "$type(/ab/)", "/ab/('xabx')", "/ab/('xx')",
        "/(a)(b)/('ab')", "/ab/i('AB')", "$map(['ab','cd'], /ab/)",
        "$string(/ab/)", "$boolean(/ab/)",
        "$contains('abc', /b/)", "$contains('abc', /z/)",
        "$contains('AB', /ab/i)", "$contains('abc', //)",
        "$split('a1b2c', /[0-9]/)", "$split('abc', //)",
        "$split('a1b22c', /[0-9]+/)", "$split('a1b2', /([0-9])/)",
        "$replace('a1b2', /[0-9]/, 'N')",
        "$replace('a1b2', /[0-9]/, 'N', 1)",
        "$replace('abc', //, 'X')", "$replace('abc', /x*/, 'X')",
        "$replace('abc', /(a)(b)/, '$2$1')",
        "$replace('abc', /(a)(b)/, '$3')",
        "$replace('abc', /(a)/, '$01')",
        "$replace('abc', /a/, '$$1')",
        "$replace('abc', /a/, function($m){ $m.match & $m.index })",
        "$replace('abc', /(b)/, function($m){ $m.groups[0] })",
        "$replace('abc', /b/, function($m){ 1 })",
        "$replace('abc', /b/, function($m){ missing })",
        "$replace('abc', /b/, function($m){ $m })",
        "$filter(['ab','cd'], /ab/)",
        "$single(['ab','cd'], /ab/)",
        "$match('ab-cd', /-/)", "$sift({'ab':1,'cd':2}, /ab/)",
        # the same expressions under a replaced engine.  See `probe-fixtures.js`.
        "$match('ABAB', /ab/)", "$match('abab', /ab/)",
        "$contains('ABAB', /ab/)", "$replace('ABAB', /ab/, 'x')",
        "$split('AxBxC', /x/)", "$count($match('abab', /ab/))",
    ]


REGEX_ENGINE_EXPRS = [
    "$match('ABAB', /ab/)", "$match('abab', /ab/)",
    "$contains('ABAB', /ab/)", "$contains('abab', /ab/)",
    "$replace('ABAB', /ab/, 'x')", "$replace('abab', /ab/, 'x')",
    "$split('AxBxC', /x/)", "$split('axbxc', /x/)",
    "$count($match('abab', /ab/))", "$match('abab', /(a)(b)/).groups",
    "$match('abab', /ab/, 1)", "$match('abab', /zz/)",
    "/ab/('abab')", "$filter(['ab','AB'], /ab/)",
]


# --------------------------------------------------------------------------- #
# transform
# --------------------------------------------------------------------------- #

def transforms() -> list[str]:
    """`|...|...|` transform, sort operator, group-by, and their interactions.

    The transform operator returns a *function*, deep-copies its input, and applies
    updates before deletes regardless of the order they were written.  All three are
    load-bearing and none of them is obvious.
    """
    return [
        "|$|{'n': 1}|",
        "$type(|$|{'n': 1}|)",
        "items ~> |$|{'price': 0}|",
        "items ~> |$|{'price': price * 2}|",
        "items ~> |$|{}, ['price']|",
        "items ~> |$|{'x': 1}, ['price']|",
        "items ~> |$|{'price': 0}, ['price']|",
        "items ~> |$|{}, 'price'|",
        "items ~> |$|{}, ['nope']|",
        "items ~> |$[0]|{'price': 99}|",
        "items ~> |$[price > 1]|{'price': 0}|",
        "items ~> |name|{}|",
        "$ ~> |a.b.c|{'d': 1}|",
        "$ ~> |a.b|{'c': 'replaced'}|",
        "$ ~> |a|{'b': missing}|",
        "$ ~> |nope|{'x': 1}|",
        "$ ~> |$|{'n': n + 1}|",
        "$ ~> |$|{'n': 1}, ['a']|",
        "Account ~> |Order|{'OrderID': 'x'}|",
        "Account ~> |Order.Product|{'Price': Price + 1}|",
        "Account ~> |Order.Product|{}, ['SKU','Description']|",
        "Account ~> |Order[0].Product[0]|{'Quantity': 0}|",
        # The transform deep-copies its input by *calling `$clone`* out of the
        # environment rather than by cloning directly, so rebinding `$clone` to a
        # non-function is observable: T2013, and the only way to reach that code.
        # A port that inlines the copy instead of looking the function up passes
        # every other case in this family and answers these two with a result.
        "($clone := 1; $ ~> |a|{'b': 2}|)",
        "($clone := 'x'; $ ~> |$|{'n': 1}|)",
        # And the same lookup used as intended: a `$clone` that *is* a function is
        # called, so this is the case that says T2013 is a type check on the
        # binding and not a refusal to accept one.
        "($clone := function($x){ $x }; $ ~> |$|{'n': 1}|)",
        "($t := |$|{'n': 1}|; $t({'n': 0}))",
        "($t := |$|{'n': 1}|; $t({}))",
        "($t := |$|{'n': 1}|; $d := {'n': 0}; $t($d); $d)",
        "|$|{'n': 1}|({'n': 0})",
        "|$|{'n': 1}|(1)",
        "|$|{'n': 1}|([{'n': 0},{'n': 2}])",
        "|$|1|({})",
        "|$|{'n': 1}, 1|({})",
        "$map([{'a':1},{'a':2}], |$|{'a': a * 10}|)",
        # sort operator
        "items^(price)", "items^(>price)", "items^(<price)",
        "items^(price).name", "items^(>price).name",
        "items^(qty, price).name", "items^(>qty, <price).name",
        "items^(name).name", "items^(>name).name",
        "items^($.price * -1).name",
        "items^(tags).name", "items^(nope).name",
        "arr^($)", "arr^(>$)", "[3,1,2]^($)", "['b','a']^($)",
        "[1,'a']^($)", "[]^($)", "scalar^($)",
        "Account.Order.Product^(Price).`Product Name`",
        "Account.Order.Product^(>Price, `Product Name`).ProductID",
        "Account.Order.Product^(Description.Weight).`Product Name`",
        "nums.xs^($)", "nums.xs^(>$)",
        "dupes.rows^(k).v", "dupes.rows^(k, >v).v",
        # group-by combined
        "dupes.rows{k: $sum(v)}", "dupes.rows{k: $count(v)}",
        "items{$string(qty > 1): name}",
        "Account.Order.Product{`Product Name`: $sum(Price * Quantity)}",
        "Account.Order.Product{Description.Colour: [`Product Name`]}",
        "Account.Order{OrderID: $sum(Product.(Price * Quantity))}",
        "items^(price){name: price}",
        "(items{name: price}) ~> $keys",
        "items{name: price} ~> |$|{'alpha': 0}|",
    ]


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

def errors() -> list[str]:
    """Expressions chosen for the error each one raises.

    jsonata carries about a hundred error codes, and the envelope is four fields
    wide: `code`, `position`, `token`, and sometimes `value`.  All four are graded.
    `position` is the character offset the parser had reached, which a port that
    tokenises differently gets wrong while still reporting the right code -- and that
    is a real difference in behaviour, because the position is what an editor
    underlines.
    """
    return [
        # parser: S0xxx
        "", " ", "1 +", "+", "(", ")", "(1", "1)", "[", "]", "[1",
        "{", "}", "{1", "{'a'", "{'a':", "'unterminated",
        '"unterminated', "`unterminated", "1 ~", "~>", "1 ~> ",
        "$", "$$", "$$$", "$-", "@", "#", "^", "%", "?", ":", ";",
        "1 ? 2", "a[", "a[1", "a[]]", "**.", "*.", "..", "1..", "..1",
        "[1..]", "[..2]", "function", "function(", "function()",
        "function($x)", "function($x){", "function($x){}",
        "λ", "|", "|$", "|$|", "|$|{}", "|$|{},|",
        "\\", "€", "1e", "1e+", "0x", "1.", ".1", "1.2.3",
        "'\\q'", "'\\u12'", "'\\uZZZZ'", "/", "/unterminated",
        "/a/z", "a b", "1 1", "'a' 'b'", "a.", ".a", "a..b",
        "$foo(", "$foo()", "$1", "$'a'", "a:=1", ":=1", "$x:=",
        "(1;", "(;)", "{,}", "[,]", "[1,]", "{'a':1,}",
        "a[1][", "@$", "@x", "#$", "#x", "a@", "a#",
        "$a := 1", "1 := 2", "true := 1",
        # evaluator: T1xxx, T2xxx, D1xxx, D2xxx, D3xxx
        "$notafunction()", "$notafunction", "1()", "'a'()", "nul()",
        "[1]()", "{}()", "a.b()", "$sum()", "$sum(1,2,3)",
        "$substring()", "$substring('a')", "$substring('a','b')",
        "$round('a')", "$round(1,'a')", "$sqrt('a')", "$power('a',1)",
        "$abs('a')", "$floor('a')", "$ceil('a')",
        "$length(1)", "$uppercase(1)", "$trim(1)", "$pad(1,1)",
        "$pad('a','b')", "$split(1,',')", "$split('a',1)",
        "$join(1)", "$join([1])", "$join(['a'],1)",
        "$replace(1,'a','b')", "$replace('a',1,'b')",
        "$replace('a','b',1)", "$replace('a','b','c','d')",
        "$contains(1,'a')", "$contains('a',1)",
        "$match(1,/a/)", "$match('a',1)", "$match('a',/a/,'b')",
        "$number({})", "$string()", "$boolean(1,2)",
        "$keys(1,2)", "$lookup({})", "$merge(1)", "$each({},1)",
        "$map([1],1)", "$filter([1],1)", "$reduce([1],1)",
        "$sort([1],1)", "$sift({},1)", "$single([1],1)",
        "$map([1], function($a,$b,$c,$d){ 1 })",
        "$reduce([1,2], function($a,$b,$c,$d,$e){ 1 })",
        "$formatNumber(1)", "$formatNumber('a','#')",
        "$formatNumber(1, 1)", "$formatBase(1,'a')",
        "$formatInteger(1)", "$formatInteger('a','#')",
        "$parseInteger('a')", "$fromMillis('a')", "$toMillis(1)",
        "$base64encode(1)", "$base64decode(1)",
        "$encodeUrl(1)", "$decodeUrl(1)",
        "$eval(1)", "$eval('a','b')", "$assert(false)",
        "$error(1)", "$error('m','x')",
        "a.b.c.d.e.f.g", "1.foo", "'a'.foo", "true.foo",
        "[1,2].foo", "$sum.foo", "a[b]", "arr[arr]",
        "arr['a']", "arr[{}]", "arr[$sum]",
        "{'a':1}[0]", "{'a':1}[1]",
        "obj{z: 1}", "1{'a':1}", "'a'{'a':1}",
        "items{name: price, name: qty}",
        # The range guard, not the range.  `[1..1e7]` is *under* jsonata's limit and
        # succeeds, producing a 1.6 GB answer -- measured, and the reason this is
        # `1e8`: the guard fires before anything is allocated, so the case is cheap
        # and the D2014 it reports is the behaviour worth grading.
        "[1..1e8]", "$count([1..1e8])", "[-1e8..1]",
    ]


# --------------------------------------------------------------------------- #
# encoding
# --------------------------------------------------------------------------- #

def encodings() -> list[str]:
    """Expressions whose answers JSON cannot spell.

    Four values reachable from ordinary JSONata are not JSON: `undefined` (which is
    not `null`), the non-finite numbers, negative zero, and a *sequence*, which is an
    array carrying a flag that changes what a later step does to it.  The wire format
    exists for these, and this family is what proves a port reproduces them rather
    than rounding them off to the nearest JSON value.
    """
    return [
        # undefined vs null
        "missing", "nul", "[missing]", "[nul]", "{'a': missing}",
        "{'a': nul}", "$exists(missing)", "$exists(nul)",
        "$count([missing])", "$string(missing)", "$string(nul)",
        "$type(missing)", "$type(nul)", "missing = nul",
        "$append(missing, missing)", "[missing, missing]",
        "$map([1], function(){ missing })",
        "$filter([1], function(){ missing })",
        "$sum([missing])", "$boolean(missing)",
        # non-finite
        "1/0", "-1/0", "0/0", "1e308 * 10", "-1e308 * 10",
        "$sqrt(-1)", "$power(1e300, 3)", "$number('Infinity')",
        "[1/0]", "{'a': 1/0}", "$string(1/0)", "$type(1/0)",
        "1/0 = 1/0", "0/0 = 0/0", "$sum([1/0, -1/0])",
        "$max([1/0, 1])", "$abs(-1/0)", "$floor(1/0)",
        # negative zero
        "-0", "-(0)", "0 * -1", "$number('-0')", "-0.0", "1 / -1/0",
        "[-0]", "{'a': -0}", "$string(-0)", "-0 = 0", "$abs(-0)",
        "$floor(-0)", "$round(-0)", "$sum([-0])", "$min([-0, 0])",
        "$formatNumber(-0, '#.0')", "-0 & ''",
        # sequences
        "a.b", "a[0].b", "$count(a.b)", "[a.b]", "a.b[]",
        "items.name", "items.tags", "items.tags[]",
        "$append(a.b, [9])", "$reverse(a.b)", "$distinct(a.b)",
        "a.b ~> $count", "{'v': a.b}", "[a.b, a.b]",
        "Account.Order.Product.Price",
        "Account.Order.Product.Price[]",
        "$count(Account.Order.Product)",
        "arr[$ > 0]", "arr[$ > 2]", "arr[$ > 9]",
        "items[price > 1].tags",
        # key order
        "obj", "$keys(obj)", "$spread(obj)", "{'z':1,'a':2,'m':3}",
        "$merge([{'z':1},{'a':2},{'z':3}])",
        "$each(obj, function($v,$k){ $k })",
        "dupes.rows{k: v}", "$keys(dupes.rows{k: v})",
        "obj ~> |$|{'a': 9}|", "$keys(obj ~> |$|{'a': 9}|)",
        "obj ~> |$|{'new': 0}|", "$keys(obj ~> |$|{'new': 0}|)",
        "$keys($merge([obj, {'b': 0}]))",
        "$string(obj)", "$string(obj, true)",
        # deep and wide
        "nested", "mixed", "mixed.arr", "mixed.nested",
        "mixed.uni", "mixed.ctrl", "mixed.big", "mixed.small",
        "mixed.int", "$string(mixed.int)", "mixed.int + 0",
        "$string(mixed)", "$string(mixed, true)",
        "$keys(mixed)", "$count($spread(mixed))",
    ]


# --------------------------------------------------------------------------- #
# syntax -- the parse tree
# --------------------------------------------------------------------------- #

def syntaxes() -> list[str]:
    """Expressions asked for their AST rather than their value.

    `op: "ast"` reports the parse tree, normalised by `probe-wire.js` to the fields
    jsonata actually documents.  It is the one place the corpus looks at something
    other than a value, and it is here because two engines can agree on every answer
    and still disagree about precedence in an expression no case happened to cover.
    Asked with `recover: true` as well, which turns a syntax error into a partial
    tree plus an error list -- a mode a port is likely to skip entirely.
    """
    return [
        "1", "-1", "1 + 2", "1 + 2 * 3", "(1 + 2) * 3", "1 - 2 - 3",
        "1 & 2 & 3", "a", "a.b", "a.b.c", "a[0]", "a[b][c]", "a[b > 1]",
        "*", "**", "**.a", "a.*.b", "$", "$$", "$x", "$x.y",
        "'s'", '"s"', "true", "false", "null", "[1,2]", "[[1],[2]]",
        "{'a':1}", "{'a':1,'b':2}", "[1..2]", "a^(b)", "a^(>b,<c)",
        "a@$x", "a#$i", "a%", "a.%.b",
        "a ~> $f", "a ~> $f ~> $g", "$f(1)", "$f(1,2)", "$f()",
        "function($x){ $x }", "function($x,$y)<nn:n>{ $x }",
        "λ($x){ $x }", "($x := 1; $x)", "(1;2)", "()",
        "a ? b : c", "a ? b", "a ?? b", "a and b or c",
        "a = b", "a != b", "a < b", "a in b", "a.b = c.d",
        "|$|{}|", "|a|{'b':1},['c']|", "/re/", "/re/i",
        "$f(?, 1)", "$f(1, ?)", "a.(b)", "a.(b;c)",
        "a{b:c}", "a{b:c}.d", "-a", "- -a", "-$x", "-(a.b)",
        "a.b[c].d{e:f}^(g)", "$f($g($h(1)))",
        "[a.b, c.d]", "{'k': [a.b]}", "a[b[c]]",
        # syntax errors, which `recover: true` turns into partial trees
        "1 +", "(", "a[", "{", "function(", "|$|", "1 1", "a..b",
        "'unterminated", "/unterminated", "$f(", "a ? ", "a ~>",
        "^", "?", ":=", "[1,", "a[1", "{'a':", "a ~> ", "$f(1,",
        # `a := 1` and `1 := 2` are deliberately absent.  Under `recover: true`
        # jsonata's error recovery does not terminate on them and the process dies of
        # a stack overflow -- measured, both spellings.  The depth at which a runtime
        # exhausts its stack is a property of the machine, so a case that reached it
        # would grade the grader's load rather than the port.  The plain (non-recover)
        # parse of both is fine and is covered by `errors()`.
    ]


# --------------------------------------------------------------------------- #
# options
# --------------------------------------------------------------------------- #

# `{sequence: n}` is the answer-size cap, and it is the option most likely to be
# mis-threaded because three different sites raise D2015 with three different
# meanings for `value`: a range reports the size it wanted, the sequence push
# override reports the limit, and `$append` reports the size.  Measured, not read
# from the source.
OPTION_SETS: list[tuple[str, dict]] = [
    ("recover-true", {"recover": True}),
    ("recover-false", {"recover": False}),
    ("sequence-2", {"sequence": 2}),
    ("sequence-3", {"sequence": 3}),
    ("sequence-1", {"sequence": 1}),
    ("stack-8", {"stack": 8}),
    ("stack-1", {"stack": 1}),
    ("timeout-large", {"timeout": 60000}),
]

OPTION_EXPRS = [
    "[1..5]", "a.b", "[1,2,3]", "$append([1,2],[3])", "items.name",
    "$count([1..5])", "Account.Order.Product.Price",
    "$map([1,2,3], function($v){ $v })", "1 +", "a.",
    "($f := function($n){ $n = 0 ? 0 : $f($n - 1) }; $f(50))",
    "$sort([3,1,2])",
]


# --------------------------------------------------------------------------- #
# bindings, assigns, registered functions
# --------------------------------------------------------------------------- #

# `bindings` on the request are what `jsonata(...).evaluate(input, bindings)` takes:
# plain JSON values bound as `$name`.  `assigns` is the same thing through
# `assign()`, which binds into the *static* frame and is therefore visible to a
# lambda defined before the assignment.  Two paths, one observable difference.

BINDING_SETS: list[tuple[str, dict]] = [
    ("num", {"v": 1}),
    ("str", {"v": "bound"}),
    ("null", {"v": None}),
    ("arr", {"v": [1, 2]}),
    ("obj", {"v": {"k": "w"}}),
    ("two", {"v": 1, "w": 2}),
    ("shadow", {"string": "not the function"}),
    ("deep", {"v": {"a": {"b": [1, {"c": 2}]}}}),
]

BINDING_EXPRS = [
    "$v", "$v.k", "$v[0]", "$count($v)", "$type($v)", "$exists($v)",
    "$v & ''", "$v = 1", "$w", "$v + $w", "$string($v)",
    "$v.a.b[1].c", "$string('x')", "$notbound",
    "items[price > 1].{'n': name, 'v': $v}",
    "$map([1], function(){ $v })",
    "($f := function(){ $v }; $f())",
]

ASSIGN_SETS: list[tuple[str, list]] = [
    ("num", [{"name": "v", "value": 1}]),
    ("str", [{"name": "v", "value": "assigned"}]),
    ("null", [{"name": "v", "value": None}]),
    ("arr", [{"name": "v", "value": [1, 2, 3]}]),
    ("obj", [{"name": "v", "value": {"k": 1}}]),
    ("two", [{"name": "v", "value": 1}, {"name": "w", "value": 2}]),
    ("rebind", [{"name": "v", "value": 1}, {"name": "v", "value": 2}]),
    ("dollar", [{"name": "$v", "value": 1}]),
    ("empty-name", [{"name": "", "value": 1}]),
    ("shadow-fn", [{"name": "string", "value": "shadowed"}]),
]

ASSIGN_EXPRS = [
    "$v", "$w", "$v + 1", "$count($v)", "$type($v)", "$exists($v)",
    "$string($v)", "($f := function(){ $v }; $f())",
    "$map([1], function(){ $v })", "$v.k", "$string(1)",
]

FUNC_SETS: list[tuple[str, list]] = [
    ("double", [{"name": "f", "impl": "double"}]),
    ("double-nosig", [{"name": "f", "impl": "double", "signature": None}]),
    ("concat2", [{"name": "f", "impl": "concat2"}]),
    ("describe", [{"name": "f", "impl": "describe"}]),
    ("throwing", [{"name": "f", "impl": "throwing"}]),
    ("throwingCode", [{"name": "f", "impl": "throwingCode"}]),
    ("focusInput", [{"name": "f", "impl": "focusInput"}]),
    ("focusLookup", [{"name": "f", "impl": "focusLookup"}]),
    ("asyncDouble", [{"name": "f", "impl": "asyncDouble"}]),
    ("makeAdder", [{"name": "f", "impl": "makeAdder"}]),
    ("counter", [{"name": "f", "impl": "counter"}]),
    ("nothing", [{"name": "f", "impl": "nothing"}]),
    ("override-sum", [{"name": "sum", "impl": "double"}]),
    ("two", [{"name": "f", "impl": "double"},
             {"name": "g", "impl": "concat2"}]),
    ("custom-sig", [{"name": "f", "impl": "double", "signature": "<x:x>"}]),
    ("bad-sig", [{"name": "f", "impl": "double", "signature": "<nope"}]),
    # The two host-object fixtures.  They are the only way the corpus reaches the
    # wire's `x` tag: every value a JSONata expression can produce is a primitive, a
    # plain object, an array, or a callable, so without these `x` would be documented
    # and unreachable -- and a port could get it wrong at no cost.
    ("hostDate", [{"name": "f", "impl": "hostDate"}]),
    ("hostMap", [{"name": "f", "impl": "hostMap"}]),
]

# Asked of the host-object fixtures specifically: what the library does with a value
# it has no type for.  Measured -- `$string` of a Date is its JSON serialisation while
# `$string` of a Map is `{}`, `$boolean` is false for both, `$count` is 1, and `$keys`
# is undefined.  None of that is guessable from the documentation.
HOST_EXPRS = [
    "$f()", "$type($f())", "$string($f())", "[$f()]", "{'k': $f()}",
    "$f() = $f()", "$boolean($f())", "$count($f())", "$keys($f())",
    "$f().x", "$f() & ''", "$exists($f())", "$f()[0]",
    "$map([1], function(){ $f() })", "$f() ~> $type",
]

FUNC_EXPRS = [
    "$f(2)", "$f('a')", "$f()", "$f(2, 3)", "$f(nul)", "$f(missing)",
    "$f", "$type($f)", "$string($f)", "$f('a', 'b')",
    "$map([1,2], $f)", "$f(2) + $f(3)", "$f($f(2))",
    "$g('a','b')", "$sum([1,2])", "$f('v')",
    "[$f(1), $f(1), $f(1)]", "items.$f(qty)",
    "$f(2) ~> $string", "($x := $f; $x(2))",
]


# --------------------------------------------------------------------------- #
# composed and mutated
# --------------------------------------------------------------------------- #

# Fragments that combine into expressions nobody wrote down.  The point is coverage
# of *interactions*: a port can special-case every expression in the tables above and
# still be wrong about what happens when a predicate contains a lambda that closes
# over a bound variable and returns a sequence.

_ATOMS = ["1", "0", "-1", "2.5", "'a'", "'b'", "''", "true", "false",
          "null", "[]", "[1]", "[1,2]", "{}", "{'k':1}", "$v", "missing",
          "a", "n", "items", "arr", "obj", "scalar", "nul"]

# `_UNARY` is filled by `str.replace("{}", ...)` and `_BINARY` by `str.format`, so
# the two tables spell braces differently: a literal `{` is written once here and
# doubled there.  Mixing them up produces expressions with stray braces that all
# fail to parse, which looks like a corpus of error cases and is really a bug.
_UNARY = ["$string({})", "$number({})", "$count({})", "$type({})",
          "$boolean({})", "$not({})", "$exists({})", "$reverse({})",
          "$sort({})", "$distinct({})", "$keys({})", "$sum({})",
          "-({})", "({})[0]", "({})[]", "[{}]", "{'w': {}}",
          "$map([{}], function($e){ $e })", "({}) ~> $string"]

_BINARY = ["{0} + {1}", "{0} - {1}", "{0} * {1}", "{0} / {1}", "{0} % {1}",
           "{0} & {1}", "{0} = {1}", "{0} != {1}", "{0} < {1}", "{0} >= {1}",
           "{0} and {1}", "{0} or {1}", "{0} in {1}", "{0} ?? {1}",
           "{0} ? {1} : 0", "[{0}, {1}]", "$append({0}, {1})",
           "({0}; {1})", "($x := {0}; $x = {1})", "{0}[{1}]",
           "$map([{0}], function($e){{ $e = {1} }})",
           "$filter([{0}, {1}], function($e){{ $e }})",
           "$reduce([{0}, {1}], function($p, $q){{ $p & $q }})",
           "{{'l': {0}, 'r': {1}}}", "{0} ~> function($z){{ $z = {1} }}"]


def _compose_one(rng: random.Random) -> str:
    """One composed expression, three shapes deep at most."""
    depth = rng.randint(1, 3)
    expr = rng.choice(_ATOMS)
    for _ in range(depth):
        roll = rng.random()
        if roll < 0.45:
            expr = rng.choice(_UNARY).replace("{}", expr)
        else:
            other = rng.choice(_ATOMS)
            if rng.random() < 0.5:
                expr = rng.choice(_BINARY).format(expr, other)
            else:
                expr = rng.choice(_BINARY).format(other, expr)
    return expr


def composed(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    seen: set[str] = set()
    out: list[str] = []
    guard = 0
    while len(out) < count and guard < count * 200:
        guard += 1
        expr = _compose_one(rng)
        if expr in seen or len(expr) > 300:
            continue
        seen.add(expr)
        out.append(expr)
    if len(out) < count:
        raise AssertionError(
            f"composed: only {len(out)} distinct expressions after {guard} tries; "
            f"the fragment tables are too small for count={count}"
        )
    return out


# Edits applied to an expression that already parses.  Each one is a plausible slip,
# so the mutated family lands mostly on parse errors and occasionally on a different
# valid expression -- both of which are answers, and neither of which anyone wrote.
_EDITS = [
    lambda s, r: s[: r.randrange(len(s))] if len(s) > 1 else s,
    lambda s, r: s[r.randrange(len(s)) :] if len(s) > 1 else s,
    lambda s, r: s + r.choice([")", "]", "}", "(", "[", "{", "'", '"', "`"]),
    lambda s, r: r.choice([")", "]", "}", "(", "["]) + s,
    lambda s, r: s.replace(".", "..", 1),
    lambda s, r: s.replace("(", "", 1),
    lambda s, r: s.replace(")", "", 1),
    lambda s, r: s.replace("'", "", 1),
    lambda s, r: s.replace(",", ";", 1),
    lambda s, r: s.replace("$", "$$", 1),
    lambda s, r: s.replace("=", "==", 1),
    lambda s, r: s.replace("+", "+++", 1),
    lambda s, r: s.replace(" ", "", 1),
    lambda s, r: s + " " + s,
    lambda s, r: s + " ~> $string",
    lambda s, r: "$count(" + s + ")",
    lambda s, r: "[" + s,
    lambda s, r: s.upper(),
    lambda s, r: s + "\n",
    lambda s, r: s + "\t",
    lambda s, r: s + "/* unterminated",
    lambda s, r: "/* comment */ " + s,
    lambda s, r: s.replace("function", "λ", 1),
    lambda s, r: s + "[",
]


def _mutate_one(expr: str, rng: random.Random) -> str:
    out = expr
    for _ in range(rng.randint(1, 2)):
        try:
            out = _EDITS[rng.randrange(len(_EDITS))](out, rng)
        except (IndexError, ValueError):
            return out
    return out


def MUTATE_BASE() -> list[str]:
    """The expressions the mutator edits, shared by `main` and `fresh`.

    One function rather than the same slice expression written twice.
    """
    return (paths()[:60] + operators()[:60] + collections()[:60] +
            strings()[:40] + functionals()[:40] + transforms()[:30])


# Builtins whose answer depends on something no port can be asked to reproduce, and
# which therefore must not reach the mutator. The vetted spellings in the pools are
# gradable because a wrapper erases the nondeterminism -- and an edit is free to
# delete exactly that wrapper. `$sort($shuffle([1,2,3]))` truncated one character from
# the right is `$sort($shuffle([1,2,3])`, which is a parse error and fine; truncated
# from the left it can become `$shuffle([1,2,3])`, which is not.
#
# So the filter is applied to the mutator's input rather than trusting its output.
# `_self_check`'s SHUFFLE_ALLOWED then governs only expressions somebody wrote down,
# where "is this spelling gradable" is a question with an author to answer it.
#
# As of now this drops nothing: the five spellings live at the tail of `collections()`,
# past the `[:60]` window `MUTATE_BASE` takes. It is kept because that is a fact about
# where they happen to sit, not a rule -- the next spelling added in the wrong place
# would reach the mutator, and the failure would be a handful of ungradable cases in
# `compose`, which is a bad thing to have to diagnose twice.
UNMUTABLE_TOKENS = ("$shuffle", "$random")


def _mutable(base: list[str]) -> list[str]:
    return [expr for expr in base
            if not any(token in expr for token in UNMUTABLE_TOKENS)]


def mutated(base: list[str], count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    seen: set[str] = set()
    out: list[str] = []
    guard = 0
    while len(out) < count and guard < count * 200:
        guard += 1
        expr = _mutate_one(base[rng.randrange(len(base))], rng)
        if not expr or expr in seen or len(expr) > 300:
            continue
        seen.add(expr)
        out.append(expr)
    if len(out) < count:
        raise AssertionError(
            f"mutated: only {len(out)} distinct expressions after {guard} tries"
        )
    return out


# --------------------------------------------------------------------------- #
# the transport
# --------------------------------------------------------------------------- #

# Each entry is a name and the exact lines fed to one process.  Lines, plural,
# because half the contract is that a bad line is *answered* rather than fatal: the
# next request must still get its reply.  A port that throws out of its read loop
# passes every single-line case here and fails the pairs.
#
# These are written as text, not as objects, for two reasons.  Key order is
# observable -- the validator reports the first offending field in document order --
# and a dict would be re-serialised in sorted order by the corpus writer.  And some
# of them are not JSON at all, which no dict can express.
#
# Every one of them runs solo.  Several are answered under id 0 because no id could
# be recovered from the line, so two in one batch would be indistinguishable in the
# reply stream and the second would be dropped as a duplicate.

_GOOD = '{"id":1,"op":"eval","expr":"1+1"}'

PROTOCOL_LINES: list[tuple[str, list[str]]] = [
    # the happy path, as a control
    ("ok-eval", [_GOOD]),
    ("ok-hello", ['{"id":1,"op":"hello"}']),
    ("ok-two", [_GOOD, '{"id":2,"op":"eval","expr":"2+2"}']),
    # P0001 -- not JSON
    ("p0001-empty", [""]),
    ("p0001-space", ["   "]),
    ("p0001-word", ["not json"]),
    ("p0001-truncated", ['{"id":1,"op":"eval"']),
    ("p0001-trailing-comma", ['{"id":1,"op":"hello",}']),
    ("p0001-single-quotes", ["{'id':1,'op':'hello'}"]),
    ("p0001-nan-literal", ['{"id":1,"op":"hello","x":NaN}']),
    ("p0001-bom", ["\ufeff" + _GOOD]),
    ("p0001-then-good", ["not json", _GOOD]),
    ("p0001-blank-then-good", ["", _GOOD]),
    ("p0001-good-then-bad", [_GOOD, "{{{"]),
    ("p0001-bad-bad-good", ["a", "b", _GOOD]),
    # P0002 -- JSON, but not an object
    ("p0002-array", ["[]"]),
    ("p0002-number", ["1"]),
    ("p0002-string", ['"s"']),
    ("p0002-null", ["null"]),
    ("p0002-true", ["true"]),
    ("p0002-then-good", ["null", _GOOD]),
    # P0003 -- no integer id
    ("p0003-absent", ['{"op":"hello"}']),
    ("p0003-string", ['{"id":"1","op":"hello"}']),
    ("p0003-float", ['{"id":1.5,"op":"hello"}']),
    ("p0003-null", ['{"id":null,"op":"hello"}']),
    ("p0003-array", ['{"id":[1],"op":"hello"}']),
    ("p0003-negative", ['{"id":-1,"op":"hello"}']),
    ("p0003-zero", ['{"id":0,"op":"hello"}']),
    ("p0003-big", ['{"id":9007199254740993,"op":"hello"}']),
    ("p0003-exponent", ['{"id":1e2,"op":"hello"}']),
    ("p0003-then-good", ['{"op":"hello"}', _GOOD]),
    # P0004 -- no string op
    ("p0004-absent", ['{"id":1}']),
    ("p0004-number", ['{"id":1,"op":1}']),
    ("p0004-null", ['{"id":1,"op":null}']),
    ("p0004-object", ['{"id":1,"op":{}}']),
    # P0005 -- unknown op
    ("p0005-empty", ['{"id":1,"op":""}']),
    ("p0005-unknown", ['{"id":1,"op":"nope"}']),
    ("p0005-case", ['{"id":1,"op":"HELLO"}']),
    ("p0005-space", ['{"id":1,"op":" hello"}']),
    ("p0005-proto", ['{"id":1,"op":"__proto__"}']),
    ("p0005-tostring", ['{"id":1,"op":"toString"}']),
    ("p0005-constructor", ['{"id":1,"op":"constructor"}']),
    # P0006 -- unexpected field, reported in document order
    ("p0006-one", ['{"id":1,"op":"hello","extra":1}']),
    ("p0006-first-of-two", ['{"id":1,"op":"hello","aaa":1,"bbb":2}']),
    ("p0006-reversed", ['{"id":1,"op":"hello","bbb":2,"aaa":1}']),
    ("p0006-on-eval", ['{"id":1,"op":"eval","expr":"1","nope":1}']),
    ("p0006-expr-on-hello", ['{"id":1,"op":"hello","expr":"1"}']),
    ("p0006-bindings-on-ast", ['{"id":1,"op":"ast","expr":"1","bindings":{}}']),
    ("p0006-assigns-on-eval", ['{"id":1,"op":"eval","expr":"1","assigns":[]}']),
    ("p0006-option", ['{"id":1,"op":"eval","expr":"1","options":{"nope":1}}']),
    ("p0006-option-cased", ['{"id":1,"op":"eval","expr":"1","options":{"Recover":true}}']),
    ("p0006-regexengine", ['{"id":1,"op":"eval","expr":"1","options":{"RegexEngine":"native"}}']),
    ("p0006-assign-entry", ['{"id":1,"op":"assign","expr":"1","assigns":[{"name":"v","value":1,"x":1}]}']),
    ("p0006-func-entry", ['{"id":1,"op":"register","expr":"1","funcs":[{"name":"f","impl":"double","x":1}]}']),
    # P0007 -- wrong type
    ("p0007-expr-number", ['{"id":1,"op":"eval","expr":1}']),
    ("p0007-expr-null", ['{"id":1,"op":"eval","expr":null}']),
    ("p0007-bindings-array", ['{"id":1,"op":"eval","expr":"1","bindings":[]}']),
    ("p0007-bindings-null", ['{"id":1,"op":"eval","expr":"1","bindings":null}']),
    ("p0007-options-array", ['{"id":1,"op":"eval","expr":"1","options":[]}']),
    ("p0007-repeat-string", ['{"id":1,"op":"eval","expr":"1","repeat":"2"}']),
    ("p0007-repeat-float", ['{"id":1,"op":"eval","expr":"1","repeat":1.5}']),
    ("p0007-repeat-zero", ['{"id":1,"op":"eval","expr":"1","repeat":0}']),
    ("p0007-repeat-nine", ['{"id":1,"op":"eval","expr":"1","repeat":9}']),
    ("p0007-repeat-negative", ['{"id":1,"op":"eval","expr":"1","repeat":-1}']),
    ("p0007-clock-bad", ['{"id":1,"op":"eval","expr":"1","clock":"frozen"}']),
    ("p0007-clock-number", ['{"id":1,"op":"eval","expr":"1","clock":1}']),
    ("p0007-recover-string", ['{"id":1,"op":"ast","expr":"1","recover":"yes"}']),
    ("p0007-sequence-float", ['{"id":1,"op":"eval","expr":"1","options":{"sequence":1.5}}']),
    ("p0007-engine-number", ['{"id":1,"op":"eval","expr":"1","options":{"engine":1}}']),
    ("p0007-assigns-object", ['{"id":1,"op":"assign","expr":"1","assigns":{}}']),
    ("p0007-assign-entry", ['{"id":1,"op":"assign","expr":"1","assigns":[1]}']),
    ("p0007-func-entry", ['{"id":1,"op":"register","expr":"1","funcs":["f"]}']),
    ("p0007-func-signature", ['{"id":1,"op":"register","expr":"1","funcs":[{"name":"f","impl":"double","signature":1}]}']),
    # P0008 -- required field missing
    ("p0008-expr", ['{"id":1,"op":"eval"}']),
    ("p0008-expr-ast", ['{"id":1,"op":"ast"}']),
    ("p0008-assigns", ['{"id":1,"op":"assign","expr":"1"}']),
    ("p0008-funcs", ['{"id":1,"op":"register","expr":"1"}']),
    ("p0008-assign-name", ['{"id":1,"op":"assign","expr":"1","assigns":[{"value":1}]}']),
    ("p0008-assign-value", ['{"id":1,"op":"assign","expr":"1","assigns":[{"name":"v"}]}']),
    ("p0008-assign-value-null", ['{"id":1,"op":"assign","expr":"$v","assigns":[{"name":"v","value":null}]}']),
    ("p0008-func-name", ['{"id":1,"op":"register","expr":"1","funcs":[{"impl":"double"}]}']),
    ("p0008-func-impl", ['{"id":1,"op":"register","expr":"1","funcs":[{"name":"f"}]}']),
    # P0009 -- unknown fixture
    ("p0009-engine", ['{"id":1,"op":"eval","expr":"1","options":{"engine":"nope"}}']),
    ("p0009-engine-empty", ['{"id":1,"op":"eval","expr":"1","options":{"engine":""}}']),
    ("p0009-engine-cased", ['{"id":1,"op":"eval","expr":"1","options":{"engine":"Native"}}']),
    ("p0009-impl", ['{"id":1,"op":"register","expr":"1","funcs":[{"name":"f","impl":"nope"}]}']),
    ("p0009-impl-proto", ['{"id":1,"op":"register","expr":"1","funcs":[{"name":"f","impl":"__proto__"}]}']),
    # order of checks, and other shapes
    ("order-unknown-op-beats-field", ['{"id":1,"op":"nope","extra":1}']),
    ("order-no-id-beats-op", ['{"op":"nope"}']),
    ("order-no-id-beats-field", ['{"extra":1}']),
    ("order-field-beats-missing", ['{"id":1,"op":"eval","nope":1}']),
    ("dup-id", ['{"id":1,"id":2,"op":"hello"}']),
    ("dup-op", ['{"id":1,"op":"hello","op":"eval"}']),
    ("id-after-op", ['{"op":"hello","id":1}']),
    ("expr-before-op", ['{"expr":"1+1","op":"eval","id":1}']),
    ("nested-whitespace", ['  {"id":1,"op":"hello"}  ']),
    ("inner-whitespace", ['{ "id" : 1 , "op" : "hello" }']),
    ("trailing-cr", [_GOOD + "\r"]),
    ("leading-tab", ["\t" + _GOOD]),
    ("unicode-escaped-op", ['{"id":1,"op":"\\u0068ello"}']),
    ("unicode-expr", ['{"id":1,"op":"eval","expr":"\'\\u6f22\'"}']),
    ("astral-expr", ['{"id":1,"op":"eval","expr":"\'\\ud83d\\ude00\'"}']),
    ("lone-surrogate-expr", ['{"id":1,"op":"eval","expr":"\'\\ud800\'"}']),
    ("nul-in-expr", ['{"id":1,"op":"eval","expr":"\'a\\u0000b\'"}']),
    ("long-expr", ['{"id":1,"op":"eval","expr":"' + "1+" * 400 + '1"}']),
    ("deep-input", ['{"id":1,"op":"eval","expr":"$string($)","input":'
                    + "[" * 60 + "]" * 60 + "}"]),
    ("repeat-eight", ['{"id":1,"op":"eval","expr":"1+1","repeat":8}']),
    ("repeat-one", ['{"id":1,"op":"eval","expr":"1+1","repeat":1}']),
    ("input-absent", ['{"id":1,"op":"eval","expr":"$"}']),
    ("input-null", ['{"id":1,"op":"eval","expr":"$","input":null}']),
    ("input-scalar", ['{"id":1,"op":"eval","expr":"$","input":7}']),
    ("clock-pinned-now", ['{"id":1,"op":"eval","expr":"$now()","clock":"pinned"}']),
    ("clock-pinned-millis", ['{"id":1,"op":"eval","expr":"$millis()","clock":"pinned"}']),
    ("hello-version", ['{"id":1,"op":"hello"}']),
    ("id-reuse", [_GOOD, _GOOD]),
    ("mixed-good-bad-good", [_GOOD, "[]", '{"id":3,"op":"hello"}']),
    ("five-lines", [_GOOD, "x", "null", '{"id":4,"op":"nope"}',
                    '{"id":5,"op":"hello"}']),
]


def _protocol_cases() -> list[dict]:
    out: list[dict] = []
    for name, lines in PROTOCOL_LINES:
        out.append({"id": f"_protocol/{name}", "family": "_protocol",
                    "raw_lines": lines, "solo": True})
    return out


# --------------------------------------------------------------------------- #
# the upstream suite
# --------------------------------------------------------------------------- #

UPSTREAM_TARBALL = "jsonata-2.2.2-test-suite.tar.gz"
UPSTREAM_SHA256 = ("9e323e85e3145bec70a32b4e58bf4af1"
                   "32042c58b111c18fcae432efc896ec3c")

# jsonata's own suite: 102 groups, 1686 cases, 28 datasets.  It is the closest thing
# to a specification the project has, and it covers ground no corpus written from the
# outside would think to cover.
#
# Its *expected results are discarded*.  Only the inputs are used -- the expression,
# the dataset, the bindings -- and the answer is whatever the reference engine says
# when asked.  Two reasons.  Upstream's expectations are written in JSON and so
# cannot spell the four values that matter most here.  And a case whose upstream
# expectation is wrong, or whose behaviour has drifted since it was written, would
# otherwise be graded against a claim no engine satisfies -- including the original,
# which by construction must pass everything.
#
# Two categories are dropped, and only these two:
#
#   `timelimit`  asserts an expression finishes within n milliseconds, which measures
#                the machine and the load on it, not the port
#   `depth`      asserts a recursion depth limit, which measures the runtime's stack
#
# Both would fail intermittently on a loaded grader for reasons the submission cannot
# influence, and an intermittent case is worse than an absent one.
UPSTREAM_SKIP_KEYS = ("timelimit", "depth")

UPSTREAM_GROUPS = 102
UPSTREAM_CASES = 1686
UPSTREAM_DATASETS = 28


def _load_upstream(data_dir: Path) -> list[dict]:
    """Read the vendored suite, returning one dict per usable case.

    Each is `{"name": str, "expr": str, "dataset": str|None, "data": any,
    "has_data": bool, "bindings": dict}`.  `dataset` names a file under
    `datasets/`; `data` is inline.  A case may carry either, neither, or an
    explicit null -- and `null` is not the same as absent, so `has_data` records
    which it was.
    """
    import hashlib
    import tarfile

    path = data_dir / UPSTREAM_TARBALL
    if not path.exists():
        raise AssertionError(
            f"{path} is missing. It is vendored into the task, not downloaded; "
            f"see tests/behavioural/data/README.md."
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != UPSTREAM_SHA256:
        raise AssertionError(
            f"{UPSTREAM_TARBALL}: sha256 is {digest}, expected {UPSTREAM_SHA256}. "
            f"The vendored suite changed -- if that was deliberate, the counts in "
            f"this file move with it and the frozen corpus has to be regenerated."
        )

    groups: dict[str, dict[str, bytes]] = {}
    datasets: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = member.name.split("/")
            handle = tar.extractfile(member)
            if handle is None:
                continue
            blob = handle.read()
            if parts[0] == "datasets" and len(parts) == 2:
                datasets[parts[1]] = blob
            elif parts[0] == "groups" and len(parts) == 3:
                groups.setdefault(parts[1], {})[parts[2]] = blob

    if len(groups) != UPSTREAM_GROUPS:
        raise AssertionError(
            f"upstream: {len(groups)} groups, expected {UPSTREAM_GROUPS}"
        )
    if len(datasets) != UPSTREAM_DATASETS:
        raise AssertionError(
            f"upstream: {len(datasets)} datasets, expected {UPSTREAM_DATASETS}"
        )

    out: list[dict] = []
    total = 0
    for group in sorted(groups):
        files = groups[group]
        for fname in sorted(files):
            if not fname.endswith(".json"):
                continue
            parsed = json.loads(files[fname].decode("utf-8"))
            entries = parsed if isinstance(parsed, list) else [parsed]
            for index, entry in enumerate(entries):
                total += 1
                if any(key in entry for key in UPSTREAM_SKIP_KEYS):
                    continue
                stem = fname[: -len(".json")]
                # An `expr-file` case keeps its expression in a sibling `.jsonata`
                # file, usually because it spans lines or contains a comment.  Those
                # are exactly the interesting ones, so they are read rather than
                # skipped.
                if "expr-file" in entry:
                    sibling = entry["expr-file"]
                    if sibling not in files:
                        raise AssertionError(
                            f"{group}/{fname}: expr-file {sibling} not in the tarball"
                        )
                    expr = files[sibling].decode("utf-8")
                elif "expr" in entry:
                    expr = entry["expr"]
                else:
                    raise AssertionError(
                        f"{group}/{fname}[{index}]: neither expr nor expr-file"
                    )
                name = f"{group}/{stem}"
                if len(entries) > 1:
                    name = f"{name}#{index}"
                out.append({
                    "name": name,
                    "expr": expr,
                    "dataset": entry.get("dataset"),
                    "data": entry.get("data"),
                    "has_data": "data" in entry,
                    "bindings": entry.get("bindings") or {},
                })

    if total != UPSTREAM_CASES:
        raise AssertionError(
            f"upstream: read {total} cases, expected {UPSTREAM_CASES}"
        )
    return out


def _upstream_datasets(data_dir: Path) -> dict[str, object]:
    import tarfile

    out: dict[str, object] = {}
    with tarfile.open(data_dir / UPSTREAM_TARBALL, "r:gz") as tar:
        for member in tar.getmembers():
            parts = member.name.split("/")
            if not member.isfile() or parts[0] != "datasets" or len(parts) != 2:
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            stem = parts[1]
            if stem.endswith(".json"):
                stem = stem[: -len(".json")]
            out[stem] = json.loads(handle.read().decode("utf-8"))
    return out


def _fixture_cases(data_dir: Path) -> list[dict]:
    """Upstream's inputs, as probe requests.

    `clock: "pinned"` on every one of them: a handful of upstream cases call `$now`,
    and this corpus is frozen once and graded later.
    """
    datasets = _upstream_datasets(data_dir)
    out: list[dict] = []
    for entry in _load_upstream(data_dir):
        request: dict[str, object] = {"op": "eval", "expr": entry["expr"]}
        name = entry["dataset"]
        if name is not None:
            if name not in datasets:
                raise AssertionError(
                    f"{entry['name']}: dataset {name!r} is not in the tarball"
                )
            request["input"] = datasets[name]
        elif entry["has_data"]:
            request["input"] = entry["data"]
        if entry["bindings"]:
            request["bindings"] = entry["bindings"]
        request["clock"] = "pinned"
        out.append({"id": f"_fixture/{entry['name']}", "family": "_fixture",
                    "request": request})
    return out


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #

# Which documents each family is asked against.  A family is crossed with the inputs
# its expressions can actually see: asking `$formatNumber` about `ACCOUNT` twelve
# times measures nothing and costs twelve cases.
FAMILY_INPUTS: dict[str, list[str]] = {
    "paths": ["account", "items", "nested", "mixed", "dupes", "single",
              "double", "arr", "scalar", "null", "empty"],
    "operators": ["mixed", "arr", "items", "empty"],
    "strings": ["mixed", "empty"],
    "numbers": ["nums", "empty"],
    "datetime": ["empty"],
    "collections": ["items", "mixed", "dupes", "account", "arr", "empty"],
    "behavioural": ["items", "arr", "empty"],
    "regex": ["empty"],
    "transform": ["account", "items", "nested", "nums", "dupes", "arr"],
    "errors": ["account", "items", "mixed", "arr", "empty"],
    "encoding": ["mixed", "single", "double", "items", "account", "arr",
                 "empty"],
    "syntax": ["empty"],
    "compose": ["items", "mixed", "arr", "empty"],
}

# Seeds are written here rather than passed in, so the committed corpus is a function
# of this file alone.
COMPOSE_SEED = 20260803
COMPOSE_COUNT = 340
MUTATE_SEED = 20260804
MUTATE_COUNT = 220

# The held-out corpus differs from the frozen one by these four integers and nothing
# else.  A submission that scores well on `main` and badly on `fresh` has told you it
# memorised answers rather than ported an engine -- which is why `fresh` is generated
# and answered inside the verifier image, after the submission is already fixed.
FRESH_COMPOSE_SEED = 918273
FRESH_COMPOSE_COUNT = 260
FRESH_MUTATE_SEED = 918274
FRESH_MUTATE_COUNT = 170


def build_main_cases(data_dir: Path) -> list[dict]:
    """Every case in the frozen corpus, in a stable order."""
    cases: list[dict] = []
    cases += _cross("paths", paths(), FAMILY_INPUTS["paths"])
    cases += _cross("operators", operators(), FAMILY_INPUTS["operators"])
    cases += _cross("strings", strings(), FAMILY_INPUTS["strings"])
    cases += _cross("numbers", numbers(), FAMILY_INPUTS["numbers"])
    cases += _cross("datetime", datetimes(), FAMILY_INPUTS["datetime"],
                    clock="pinned")
    cases += _cross("collections", collections(), FAMILY_INPUTS["collections"])
    cases += _cross("behavioural", functionals(), FAMILY_INPUTS["behavioural"])
    cases += _cross("regex", regexes(), FAMILY_INPUTS["regex"])
    cases += _cross("transform", transforms(), FAMILY_INPUTS["transform"])
    cases += _cross("errors", errors(), FAMILY_INPUTS["errors"])
    cases += _cross("encoding", encodings(), FAMILY_INPUTS["encoding"])

    # The AST, under both recovery modes.  `_absent`, not `empty`: `ast` takes no
    # `input` field, and a request that carries one is a P0006 rather than a parse.
    cases += _cross("syntax", syntaxes(), ["_absent"], ops=("ast",),
                    label="plain")
    cases += _cross("syntax", syntaxes(), ["_absent"], ops=("ast",),
                    label="recover", recover=True)

    # `evalcb`: the same expressions through `evaluate(input, bindings, callback)`
    # instead of the returned promise.  The answer must be identical, which is the
    # whole point -- a port that implements one path and forwards the other loses
    # every case here and none anywhere else.
    cb_exprs = (paths()[:40] + operators()[:40] + collections()[:40] +
                functionals()[:30] + encodings()[:30] + errors()[:20])
    cases += _cross("callback", cb_exprs, ["items", "mixed"], ops=("evalcb",))

    # Options, bindings, assigns, registered functions.
    for label, opts in OPTION_SETS:
        cases += _cross("options", OPTION_EXPRS, ["single", "double", "items"],
                        label=label, options=opts)
    for engine in ("native", "caseless", "nomatch", "broken"):
        cases += _cross("options", REGEX_ENGINE_EXPRS, ["empty"],
                        label=f"engine-{engine}", options={"engine": engine})
    for label, binds in BINDING_SETS:
        cases += _cross("bindings", BINDING_EXPRS, ["items", "mixed"],
                        label=label, bindings=binds)
    for label, assigns in ASSIGN_SETS:
        cases += _cross("assigns", ASSIGN_EXPRS, ["items", "empty"],
                        ops=("assign",), label=label, assigns=assigns)
    for label, funcs in FUNC_SETS:
        cases += _cross("register", FUNC_EXPRS, ["items", "empty"],
                        ops=("register",), label=label, funcs=funcs)
    for impl in ("hostDate", "hostMap"):
        cases += _cross("register", HOST_EXPRS, ["empty"], ops=("register",),
                        label=f"host-{impl}",
                        funcs=[{"name": "f", "impl": impl}])

    # `repeat`: the same expression evaluated n times in one process, which is how a
    # case observes that evaluation left no state behind.  `counter` is the one
    # fixture where repeating legitimately changes the answer.
    cases += _cross("repeat", ["1+1", "$millis()", "items.name", "[1..3]",
                               "($x := 1; $x)", "$now()", "a.b",
                               "$sort([3,1,2])", "1 +"],
                    ["single", "items"], repeat=3, clock="pinned")
    cases += _cross("repeat", ["$f()", "$f(1)"], ["empty"], ops=("register",),
                    label="counter", funcs=[{"name": "f", "impl": "counter"}],
                    repeat=4)

    composed_exprs = composed(COMPOSE_COUNT, seed=COMPOSE_SEED)
    mutated_exprs = mutated(_mutable(MUTATE_BASE()), MUTATE_COUNT, seed=MUTATE_SEED)
    cases += _cross("compose", composed_exprs + mutated_exprs,
                    FAMILY_INPUTS["compose"], bindings={"v": 2})

    cases += _fixture_cases(data_dir)
    return cases


def build_fresh_cases() -> list[dict]:
    """The held-out corpus: same generators, different seeds, no upstream."""
    cases: list[dict] = []
    cases += _cross("fresh-composed",
                    composed(FRESH_COMPOSE_COUNT, seed=FRESH_COMPOSE_SEED),
                    ["items", "mixed", "arr", "empty"], bindings={"v": 2})
    cases += _cross("fresh-mutated",
                    mutated(_mutable(MUTATE_BASE()), FRESH_MUTATE_COUNT,
                            seed=FRESH_MUTATE_SEED),
                    ["items", "mixed", "empty"], bindings={"v": 2})
    return cases


def _assign_wire_ids(cases: list[dict]) -> None:
    """Number the cases from 1.

    Protocol cases carry their ids inside their raw lines and get none: the whole
    subject of half of them is that no id could be recovered.
    """
    n = 0
    for case in cases:
        if "raw_lines" in case:
            continue
        n += 1
        case["wire_id"] = n


def _check_unique(cases: list[dict], label: str) -> None:
    seen: set[str] = set()
    for case in cases:
        if case["id"] in seen:
            raise AssertionError(
                f"{label}: duplicate case id {case['id']!r}. Two cases under one id "
                f"cannot both be graded -- one expectation would overwrite the "
                f"other, and the family's weight would be charged twice."
            )
        seen.add(case["id"])


def write_corpus(out_dir: Path, data_dir: Path) -> dict[str, int]:
    """Write the four case files. Returns a stem -> count map."""
    out_dir.mkdir(parents=True, exist_ok=True)
    groups = {
        "main": build_main_cases(data_dir),
        "fresh": build_fresh_cases(),
        "protocol": _protocol_cases(),
    }
    # The fixture family is written separately so that a grader can weigh it as its
    # own module; it is built by the same loader either way.
    groups["fixture"] = [c for c in groups["main"] if c["family"] == "_fixture"]
    groups["main"] = [c for c in groups["main"] if c["family"] != "_fixture"]

    counts: dict[str, int] = {}
    for stem, cases in groups.items():
        _check_unique(cases, stem)
        _assign_wire_ids(cases)
        path = out_dir / f"{stem}-cases.jsonl"
        # `ensure_ascii=True`, and it has to be.  One upstream case is
        # `$encodeUrl('\ud800')` -- a lone surrogate, which is a perfectly good
        # JavaScript string and not encodable as UTF-8 at all.  Writing it as the
        # escape `\ud800` keeps the corpus pure ASCII and lossless in both
        # directions; writing it as a character raises UnicodeEncodeError, and
        # dropping the case would lose the one place the corpus asks what an engine
        # does with an unpaired surrogate.
        with path.open("w", encoding="ascii") as fh:
            for case in cases:
                fh.write(json.dumps(case, ensure_ascii=True,
                                    sort_keys=True) + "\n")
        counts[stem] = len(cases)
    return counts


def family_counts(cases: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for case in cases:
        out[case["family"]] = out.get(case["family"], 0) + 1
    return out


# --------------------------------------------------------------------------- #
# self-check
# --------------------------------------------------------------------------- #

def _self_check(data_dir: Path) -> None:
    """Everything that must hold about the corpus before it is worth freezing."""
    import catalog

    main = build_main_cases(data_dir)
    fresh = build_fresh_cases()
    protocol = _protocol_cases()

    emitted = set(family_counts(main)) | set(family_counts(fresh))
    emitted |= {"_protocol"}
    budgeted = set(catalog.all_budgets()) - catalog.NON_CORPUS_FAMILIES
    assert emitted == budgeted, (
        f"family mismatch:\n"
        f"  emitted but not budgeted: {sorted(emitted - budgeted)}\n"
        f"  budgeted but not emitted: {sorted(budgeted - emitted)}\n"
        f"  (non-corpus families, excluded from both sides: "
        f"{sorted(catalog.NON_CORPUS_FAMILIES)})"
    )
    # And the exclusion must not become a hole: a non-corpus family still needs a
    # budget, or its module's weight would be short and the missing points would
    # spread silently over the other modules.
    for family in sorted(catalog.NON_CORPUS_FAMILIES):
        assert family in catalog.all_budgets(), (
            f"{family} is excluded from the emitted/budgeted comparison but has no "
            f"budget, so nothing grades it and no check would say so"
        )

    # Every request must be one the protocol defines, with fields the protocol
    # allows. A case built with a typo'd field name would otherwise be graded on the
    # P0006 it draws rather than on the behaviour it was written for -- an expectation
    # the reference and the submission would agree on for the wrong reason.
    known_ops = {"hello", "ast", "eval", "evalcb", "assign", "register"}
    allowed = {
        "hello": set(),
        "ast": {"expr", "recover"},
        "eval": {"expr", "input", "bindings", "options", "clock", "repeat"},
        "evalcb": {"expr", "input", "bindings", "options", "clock", "repeat"},
        "assign": {"expr", "input", "options", "clock", "repeat", "assigns"},
        "register": {"expr", "input", "options", "clock", "repeat", "funcs"},
    }
    options = {"recover", "timeout", "stack", "sequence", "engine"}
    engines = {"native", "caseless", "nomatch", "broken"}
    impls = {"double", "concat2", "describe", "throwing", "throwingCode",
             "focusInput", "focusLookup", "asyncDouble", "makeAdder",
             "counter", "nothing", "hostDate", "hostMap"}
    for case in main + fresh:
        request = case["request"]
        op = request["op"]
        assert op in known_ops, f"{case['id']}: unknown op {op!r}"
        for key in request:
            if key == "op":
                continue
            assert key in allowed[op], f"{case['id']}: `{key}` is not a field of {op}"
        for key in request.get("options", {}):
            assert key in options, f"{case['id']}: `{key}` is not an option"
        engine = request.get("options", {}).get("engine")
        assert engine is None or engine in engines, \
            f"{case['id']}: no engine named {engine!r}"
        for entry in request.get("funcs", []):
            assert entry["impl"] in impls, \
                f"{case['id']}: no implementation named {entry['impl']!r}"
        repeat = request.get("repeat")
        assert repeat is None or 1 <= repeat <= 8, \
            f"{case['id']}: repeat={repeat} is outside 1..8"
        clock = request.get("clock")
        assert clock is None or clock in ("pinned", "live"), \
            f"{case['id']}: clock={clock!r}"

    # Any case that can see the clock must pin it, or the expectation frozen at image
    # build time cannot match the answer given at grading time.  `datetime` and
    # `repeat` pin it wholesale; this catches a clock-reading expression that arrived
    # in some other family by way of the composed generators.
    for case in main + fresh:
        expr = case["request"].get("expr", "")
        if "$now" in expr or "$millis" in expr:
            assert case["request"].get("clock") == "pinned", (
                f"{case['id']}: reads the clock without `clock: \"pinned\"`. The "
                f"expectation was frozen when the image was built; a live clock "
                f"makes the case unanswerable rather than hard."
            )

    # The second way an expression reads the clock, and the one `clock: "pinned"`
    # cannot help with.
    #
    # `$toMillis(s, picture)` needs a full date to produce an instant.  When the
    # picture names no year, the rule in `parseDateTime` is to default every
    # component left of the most significant one that *was* named from "the values
    # returned by $now()" -- so a time-only picture means today's date.  But it does
    # not go through the `$now` binding to get there: it reads
    # `this.environment.timestamp`, which the engine sets from `new Date()` inside
    # every `evaluate()` call.  `pinClock` shadows `$now`, `$millis` and `$random`
    # through `registerFunction`, which is the only hook a consumer has, and that
    # hook does not reach an internal field.  So the answer is the date the grader
    # ran, the expectation is the date the image was built, and the case fails on
    # every day but one.  This is the same shape as `$shuffle` reaching `Math.random`
    # directly above, and it cost lang07 one real case before it was written down:
    # `$toMillis('7 pm', '[h] [P]')` was in the pool, measured 84/85 on the reference
    # itself, and the missing point was the day rolling over.
    #
    # The fix is the same too -- wrap it in something that erases the defaulted date
    # -- and so is the shape of the guard: spellings, because "does this wrapper
    # erase the date" is a question for whoever wrote the case.
    #
    # This regex reads the picture only when both arguments are string literals,
    # which is every spelling anybody has written here.  It is a reading aid, not the
    # guard: what actually establishes that no case drifts is `identity.py`'s
    # shifted-clock pass, which answers the whole corpus a second time with the
    # process clock moved and requires the same bytes.  A case this regex cannot see
    # is caught there.
    tomillis_pictured = re.compile(
        r"\$toMillis\(\s*'[^']*'\s*,\s*'([^']*)'\s*\)")
    TOMILLIS_CLOCK_ALLOWED = frozenset({
        # Authored here.  `[h]`/`[P]` parsed, everything right of them defaulted to
        # zero, and the defaulted date formatted away.
        "$fromMillis($toMillis('7 pm', '[h] [P]'), '[H01]:[m01]:[s01].[f001]')",
        # Upstream's own three, from `groups/function-tomillis/parseDateTime`.  Each
        # was read and checked here rather than exempted as a class, because a
        # fixture whose answer is today's date is exactly as unanswerable as an
        # authored one:
        #   - the first formats the date away, as above, and is upstream arriving at
        #     the same wrapper independently
        "$toMillis('13:45', '[H]:[m]') ~> $fromMillis() ~> $substringAfter('T')",
        #   - the second is upstream's attempt to grade the defaulting rule itself,
        #     and under the probe it is a stable `false`: the left side is the real
        #     date, the right is `$now()`, and `$now()` is pinned to FIXED_MILLIS
        #     (2023-11-14), which no grading day equals.  Vacuous, therefore, but
        #     stable and worth nothing to anybody -- so it stays as upstream wrote it
        #     rather than being repaired into a case that could never hold.
        "$toMillis('13:45', '[H]:[m]') ~> $fromMillis() ~> "
        "$substringBefore('T') = $substringBefore($now(), 'T')",
        #   - the third never reaches the defaulting: month-and-day with minutes but
        #     no hours is a gap between the named components, and the answer is D3136
        "$toMillis('5-22 23:59', '[M]-[D] [m]:[s]')",
    })
    for case in main + fresh:
        expr = case["request"].get("expr", "")
        for picture in tomillis_pictured.findall(expr):
            # A picture with no component at all -- upstream's `'Hello'` -- is matched
            # as a literal and never defaults anything.  Only a picture that names
            # components but no year reaches the rule.
            if "[" not in picture:
                continue
            if "[Y" in picture or "[X" in picture:
                continue
            assert expr in TOMILLIS_CLOCK_ALLOWED, (
                f"{case['id']}: {expr!r} parses with the picture {picture!r}, which "
                f"names no year, so `parseDateTime` defaults the date from the "
                f"engine's own timestamp -- today's date at grading time, and the "
                f"image's build date in the frozen expectation. `clock: \"pinned\"` "
                f"cannot reach that field: it shadows the `$now` binding, and this "
                f"reads `environment.timestamp` directly. Wrap the call so the "
                f"defaulted date does not reach the answer (`$fromMillis(..., "
                f"'[H01]:[m01]:[s01].[f001]')` is the spelling used above) and add "
                f"that to TOMILLIS_CLOCK_ALLOWED."
            )

    # Two builtins whose answers no expectation can predict, guarded separately
    # because only one of them can be shadowed.
    #
    # `$random` is reachable: the probe rebinds it per evaluation through the same
    # `registerFunction` a consumer would use, so a case *could* call it -- but the
    # value would then be a property of the probe's seeded generator rather than of
    # jsonata, and a port reproducing it would be reproducing `mulberry32`. So it
    # stays out of the corpus entirely.
    #
    # `$shuffle` cannot be shadowed at all. jsonata's implementation calls
    # `Math.random()` directly rather than going through the `$random` binding, so
    # rebinding `$random` does nothing for it and no seeding a port could do would
    # make its permutation predictable. This was measured, not reasoned about: with
    # `$shuffle([1,2,3])` in the pool, the reference disagreed with its own frozen
    # answers on ten cases across three families -- `identity.py` is what caught it.
    #
    # Wrapping is allowed where the wrapper erases the order, so the builtin is still
    # exercised end to end -- all three elements traverse the Fisher-Yates loop -- and
    # the answer is fixed. The allowlist is spellings rather than a pattern because
    # "does this wrapper erase order" is not decidable by inspection: `$sort` erases
    # it, `$reverse` does not, `$string` does not, and a regex permissive enough to
    # accept the first would accept all three.
    SHUFFLE_ALLOWED = frozenset({
        # Authored here, for the pools.
        "$sort($shuffle([1,2,3]))",   # a permutation, sorted, is the sorted array
        "$count($shuffle([1,2,3]))",  # 3, whatever the order
        "$sum($shuffle([1,2,3]))",    # 6, whatever the order
        "$shuffle([1])",              # returned as-is; length <= 1 short-circuits
        "$shuffle([])",               # likewise
        # Upstream's own four, from `groups/function-shuffle` in the vendored 2.2.2
        # suite. They are listed rather than exempted: a fixture whose answer depends
        # on the permutation is exactly as unanswerable as an authored one, and the
        # freeze would pin one particular order for it either way. Upstream hit this
        # same wall and solved it the same way -- two of the four use the identical
        # wrappers -- so each was read and checked before being written down here.
        "$count($shuffle([1..10]))",  # 10, whatever the order
        "$sort($shuffle([1..10]))",   # 1..10 again
        "$shuffle(nothing)",          # undefined in, undefined out
    })
    for case in main + fresh:
        expr = case["request"].get("expr", "")
        assert "$random" not in expr, (
            f"{case['id']}: calls $random, whose answer no expectation can predict"
        )
        if "$shuffle" in expr:
            assert expr in SHUFFLE_ALLOWED, (
                f"{case['id']}: {expr!r} calls $shuffle in a spelling whose answer "
                f"depends on the permutation. $shuffle reaches Math.random directly, "
                f"so the probe's determinism shim cannot reach it and no port can be "
                f"asked to reproduce a particular order. Wrap it in something that "
                f"erases the order and add that spelling to SHUFFLE_ALLOWED."
            )

    # Protocol cases must all be solo, and none may carry a wire id.  See
    # `_protocol_cases`.
    for case in protocol:
        assert case.get("solo") is True, f"{case['id']}: protocol case not solo"
        assert "raw_lines" in case, f"{case['id']}: no raw_lines"
        assert case["raw_lines"], f"{case['id']}: empty raw_lines"
        for line in case["raw_lines"]:
            assert "\n" not in line, (
                f"{case['id']}: a raw line contains a newline, which would make it "
                f"two requests and break the pairing"
            )

    # The upstream suite must have loaded, and its shape must be the one measured.
    fixtures = [c for c in main if c["family"] == "_fixture"]
    assert fixtures, "the upstream fixture family is empty"

    # Case floors. These are the numbers the suite's weighting assumes.
    counts = {"main": len([c for c in main if c["family"] != "_fixture"]),
              "fresh": len(fresh), "protocol": len(protocol),
              "fixture": len(fixtures)}
    floors = {"main": catalog.MIN_CORPUS_CASES, "fresh": catalog.MIN_FRESH_CASES,
              "protocol": catalog.MIN_PROTOCOL_CASES,
              "fixture": catalog.MIN_FIXTURE_CASES}
    for stem, floor in floors.items():
        assert counts[stem] >= floor, (
            f"{stem}: {counts[stem]} cases, floor is {floor}"
        )

    # Every family carrying a budget must actually have cases, and vice versa.
    for family, n in family_counts(main).items():
        assert n > 0, f"{family}: budgeted but empty"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="generate the lang07 case corpus")
    ap.add_argument("--out", required=True, type=Path,
                    help="directory to write <stem>-cases.jsonl into")
    ap.add_argument("--data", type=Path, default=None,
                    help="directory holding the vendored jsonata test-suite "
                         "tarball (default: ../data relative to this file)")
    ap.add_argument("--check-only", action="store_true",
                    help="run the self-check and write nothing")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    data_dir = args.data or (Path(__file__).resolve().parent.parent / "data")
    _self_check(data_dir)
    print("gen: self-check passed")
    if args.check_only:
        return 0

    counts = write_corpus(args.out, data_dir)
    for stem in ("main", "fresh", "protocol", "fixture"):
        print(f"gen: {stem}-cases.jsonl  {counts[stem]:6d}")
    print(f"gen: {sum(counts.values())} cases total -> {args.out}")

    print("gen: families in the frozen corpus:")
    for family, n in sorted(family_counts(build_main_cases(data_dir)).items(),
                            key=lambda kv: -kv[1]):
        print(f"       {family:16s} {n:6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
