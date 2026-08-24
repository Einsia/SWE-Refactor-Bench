"""The POSIX path vectors, built once and shared by the suite.

Every vector is answered twice: by `node:path.posix` in the oracle container and
by whatever `src/core` exports.  Nothing here encodes an expected value -- the
awkward cases are awkward precisely because nobody remembers what
`path.posix.relative('/a/b', '/a/b')` returns, and a hand-written table would be
a second implementation to get wrong.

The inputs are chosen from the shapes a CSS preprocessor actually produces:
`@import` specifiers, `paths` entries, `url()` rewriting, the `basePath` of a
sourcemap.  Plus the degenerate ones, which is where reimplementations break.
"""
from __future__ import annotations

import functools

#: Single-argument fodder.  Covers empty, dot, dot-dot, roots, doubled and
#: trailing separators, hidden files, multiple extensions, and unicode.
UNARY = (
    "", ".", "..", "/", "//", "///", "a", "/a", "a/", "/a/", "a/b", "/a/b",
    "a/b/", "/a/b/", "//a//b", "/a//b//", "a//b", "./a", "../a", "./a/b",
    "../../a", "a/./b", "a/../b", "a/b/..", "a/b/../..", "a/b/../../..",
    "/a/b/../..", "/../a", "/../../a", "./", "../", "./.", "../..",
    ".hidden", "/.hidden", "a/.hidden", ".hidden.css", "a.styl", "/a/b.styl",
    "a/b.min.css", "a.", "a..", ".", "..styl", "index.styl", "nib/index.styl",
    "a b/c d.styl", "a-b_c.styl", "ünïcøde/påth.styl", "a/ünïcøde.styl",
    "very/deeply/nested/path/to/a/file.styl", "/very/deeply/nested/f.styl",
    "a/b/c/d/e/f/g/h", "/proj/test/cases/import.basic/index.styl",
)

#: `join` argument lists.  Multi-argument behaviour is where naive
#: implementations diverge: absolute segments, empty segments, dot segments.
JOIN_ARGS = (
    (), ("",), (".",), ("a",), ("a", "b"), ("a", "b", "c"),
    ("/a", "b"), ("a", "/b"), ("/a", "/b"), ("a", "", "b"), ("", "a"),
    ("a", "."), (".", "a"), ("a", ".."), ("a", "..", ".."), ("a", "b", ".."),
    ("/", "a"), ("/", ""), ("/", ".."), ("a/", "/b"), ("a//", "//b"),
    ("a", "b/"), ("a/b", "../c"), ("../a", "../b"), ("a", "b", "..", "c"),
    ("/proj/test/cases", "import.basic", "index.styl"),
    ("/proj", "test", "..", "test", "cases", "a.styl"),
    ("nib", "..", "nib", "index.styl"),
    ("a", "b", "c", "..", "..", "d"),
    (".", "..", "a"), ("..", ".", "a"), ("a", "ü", "b"),
)

#: `resolve` argument lists.  Resolve is cwd-dependent, so only forms whose
#: answer does not depend on the process cwd are used -- the first segment is
#: always absolute.
RESOLVE_ARGS = (
    ("/",), ("/a",), ("/a", "b"), ("/a", "/b"), ("/a", "..") ,
    ("/a/b", "../c"), ("/a/b", "./c"), ("/a/b/c", "../../d"),
    ("/a", "b", "c"), ("/a", "", "b"), ("/a", "."), ("/a", ".."),
    ("/a/b", "/c/d"), ("/", "..", "a"), ("/a//b", "..//c"),
    ("/proj/test", "cases", "a.styl"),
    ("/proj/test/cases", "..", "..", "images", "logo.png"),
    ("/a/b/c", "d/e/../f"), ("/", "a", "..", "b"), ("/x", "y/", "/z"),
)

#: `relative(from, to)` pairs.  The sourcemap and `url()` rewriting both depend
#: on this one, and its edge cases (identical paths, one a prefix of the other,
#: sibling trees) are the ones most often wrong.
RELATIVE_PAIRS = (
    ("/", "/"), ("/a", "/a"), ("/a", "/b"), ("/a", "/a/b"), ("/a/b", "/a"),
    ("/a/b", "/a/b"), ("/a/b", "/a/c"), ("/a/b/c", "/a/b/d"),
    ("/a/b/c", "/a/x/y"), ("/a/b", "/"), ("/", "/a/b"),
    ("/a/b/c/d", "/a"), ("/a", "/a/b/c/d"),
    ("/proj/test/cases", "/proj/test/images/logo.png"),
    ("/proj/test/sourcemap", "/proj/test/cases/a.styl"),
    ("/a/b", "/a/b/"), ("/a/b/", "/a/b"), ("/a//b", "/a/b"),
    ("a/b", "a/c"), ("a", "b"), ("", ""), ("", "a"), ("a", ""),
    ("/x/y/z", "/x/y/z/w/v"), ("/aa", "/a"), ("/a", "/aa"),
    ("/a/bb", "/a/b"),
)

#: Functions taking exactly one path.
UNARY_FNS = ("dirname", "basename", "extname", "normalize", "isAbsolute")

#: `basename(path, suffix)` pairs -- the two-argument form upstream uses to strip
#: `.styl` from a filename.
BASENAME_SUFFIX_PAIRS = (
    ("a.styl", ".styl"), ("/a/b.styl", ".styl"), ("a.css", ".styl"),
    ("index.styl", ".styl"), ("a.min.css", ".css"), (".styl", ".styl"),
    ("a", ""), ("a.styl", ""), ("/a/", ".styl"), ("a.STYL", ".styl"),
)


def _vec(vid: str, fn: str, args: tuple) -> dict:
    return {"id": vid, "fn": fn, "args": list(args)}


@functools.lru_cache(maxsize=1)
def vectors() -> tuple[dict, ...]:
    """Every vector, with a stable id derived from what it asks."""
    out: list[dict] = []
    for fn in UNARY_FNS:
        for i, p in enumerate(UNARY):
            out.append(_vec(f"{fn}:{i}", fn, (p,)))
    for i, args in enumerate(JOIN_ARGS):
        out.append(_vec(f"join:{i}", "join", args))
    for i, args in enumerate(RESOLVE_ARGS):
        out.append(_vec(f"resolve:{i}", "resolve", args))
    for i, pair in enumerate(RELATIVE_PAIRS):
        out.append(_vec(f"relative:{i}", "relative", pair))
    for i, pair in enumerate(BASENAME_SUFFIX_PAIRS):
        out.append(_vec(f"basename2:{i}", "basename", pair))
    return tuple(out)


@functools.lru_cache(maxsize=1)
def ids() -> tuple[str, ...]:
    return tuple(v["id"] for v in vectors())


@functools.lru_cache(maxsize=1)
def by_id() -> dict[str, dict]:
    return {v["id"]: v for v in vectors()}


def describe(vid: str) -> str:
    """`join('a', '/b')`, for a failure message."""
    v = by_id()[vid]
    return f"{v['fn']}({', '.join(repr(a) for a in v['args'])})"
