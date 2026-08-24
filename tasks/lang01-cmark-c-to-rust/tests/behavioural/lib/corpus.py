#!/usr/bin/env python3
"""Builds the verifier-private document corpus for lang01-cmark-c-to-rust.

Every behavioural case in this task is "feed this document through this
production entry point and compare the bytes against the reference".  The
corpus is therefore the backbone of the whole evaluation, and it has to satisfy
three constraints at once:

* **Coverage.** Documents must reach every block construct, every inline
  construct, every renderer escape table, the reference/link-label case-folding
  path, the entity table, the UTF-8 validator and the wrap engine.  A migration
  that skipped one renderer or one scanner rule has to fail somewhere.
* **Determinism.** The corpus is derived from the pinned upstream tree plus a
  fixed-seed generator, so the frozen expectations stay valid forever.
* **Non-disclosure.** The generated part never ships in the agent workspace.
  A solver cannot enumerate it, so hard-coding answers is not a strategy.

Sections named `spec`, `smart`, `regression` are extracted from the pinned
upstream conformance files.  Everything else is synthesized here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

EXAMPLE_RE = re.compile(
    r"^`{32} example\n(.*?)^\.\n(.*?)^`{32}$", re.M | re.S
)

# ---------------------------------------------------------------------------
# Upstream conformance extraction
# ---------------------------------------------------------------------------


def extract_examples(text: str) -> list[str]:
    """Return the markdown side of every fenced conformance example."""
    documents = []
    for match in EXAMPLE_RE.finditer(text):
        markdown = match.group(1).replace("→", "\t")
        documents.append(markdown)
    return documents


# ---------------------------------------------------------------------------
# Synthetic fragment library
#
# Fragments are grouped by the parser subsystem they exercise so the generator
# can build documents that mix subsystems without becoming random noise.
# ---------------------------------------------------------------------------

BLOCK_FRAGMENTS: list[tuple[str, str]] = [
    ("atx-h1", "# Heading one"),
    ("atx-h6", "###### Heading six"),
    ("atx-closed", "## Closed heading ##"),
    ("atx-empty", "#"),
    ("atx-escaped", "\\## not a heading"),
    ("setext-h1", "Setext primary\n=============="),
    ("setext-h2", "Setext secondary\n----------------"),
    ("setext-multiline", "line one\nline two\n==="),
    ("para-simple", "A single ordinary paragraph."),
    ("para-lazy", "first line\nlazy continuation line"),
    ("para-trailing-space", "trailing spaces here   \nnext line"),
    ("hr-dash", "---"),
    ("hr-star-spaced", "* * *"),
    ("hr-underscore", "___"),
    ("hr-indented", "   ***"),
    ("fence-plain", "```\nliteral text\n```"),
    ("fence-info", "```rust\nfn main() {}\n```"),
    ("fence-info-attrs", "``` c  extra info here\nint x;\n```"),
    ("fence-tilde", "~~~\ntilde fenced\n~~~"),
    ("fence-nested-backticks", "````\n```\ninner\n```\n````"),
    ("fence-unclosed", "```\nnever closed"),
    ("fence-indented", "  ```\n  indented fence\n  ```"),
    ("indent-code", "    indented code line\n    second line"),
    ("indent-code-blank", "    first\n\n    after blank"),
    ("indent-code-tab", "\ttab indented code"),
    ("quote-simple", "> quoted paragraph"),
    ("quote-lazy", "> quoted\ncontinued lazily"),
    ("quote-nested", "> > doubly quoted"),
    ("quote-triple", "> > > triple"),
    ("quote-empty", ">"),
    ("quote-with-list", "> - item in quote\n> - second"),
    ("quote-with-fence", "> ```\n> code in quote\n> ```"),
    ("list-bullet-dash", "- alpha\n- beta\n- gamma"),
    ("list-bullet-star", "* alpha\n* beta"),
    ("list-bullet-plus", "+ alpha\n+ beta"),
    ("list-loose", "- alpha\n\n- beta"),
    ("list-ordered-period", "1. one\n2. two\n3. three"),
    ("list-ordered-paren", "1) one\n2) two"),
    ("list-ordered-start", "7. seven\n8. eight"),
    ("list-ordered-large", "999999999. big start"),
    ("list-nested-mixed", "- outer\n  1. inner\n  2. inner two\n- outer two"),
    ("list-deep-nest", "- a\n  - b\n    - c\n      - d"),
    ("list-item-multiblock", "- para one\n\n  para two\n- next item"),
    ("list-item-code", "- item\n\n      code in item"),
    ("list-interrupt", "text\n- list interrupts paragraph"),
    ("list-empty-item", "-\n- second"),
    ("html-block-div", "<div>\nraw block\n</div>"),
    ("html-block-comment", "<!-- a comment -->"),
    ("html-block-pi", "<?php echo 1; ?>"),
    ("html-block-decl", "<!DOCTYPE html>"),
    ("html-block-cdata", "<![CDATA[raw cdata]]>"),
    ("html-block-script", "<script>\nvar x = 1;\n</script>"),
    ("html-block-pre", "<pre>\n  preformatted\n</pre>"),
    ("html-block-cond", "<table>\n  <tr><td>cell</td></tr>\n</table>"),
    ("blank-lines", "para one\n\n\n\npara two"),
    ("tab-mix", "\t- tab list item\n\t- second"),
]

INLINE_FRAGMENTS: list[tuple[str, str]] = [
    ("em-star", "*emphasis*"),
    ("em-underscore", "_emphasis_"),
    ("strong-star", "**strong**"),
    ("strong-underscore", "__strong__"),
    ("em-in-strong", "**outer *inner* outer**"),
    ("strong-in-em", "*outer **inner** outer*"),
    ("em-triple", "***both***"),
    ("em-intraword", "intra*word*emphasis"),
    ("em-unmatched", "*unmatched emphasis"),
    ("em-adjacent", "*a**b*"),
    ("em-punctuation", "*(a)*"),
    ("code-span", "`code span`"),
    ("code-span-double", "``code with ` tick``"),
    ("code-span-strip", "` padded `"),
    ("code-span-newline", "`multi\nline`"),
    ("code-span-unmatched", "`unmatched"),
    ("link-inline", "[label](/url)"),
    ("link-title-double", '[label](/url "title")'),
    ("link-title-single", "[label](/url 'title')"),
    ("link-title-paren", "[label](/url (title))"),
    ("link-empty-url", "[label]()"),
    ("link-angle-url", "[label](</url with space>)"),
    ("link-escaped-url", "[label](/url\\)x)"),
    ("link-nested-text", "[*emph* label](/url)"),
    ("link-ref-full", "[label][ref]\n\n[ref]: /url"),
    ("link-ref-collapsed", "[ref][]\n\n[ref]: /url"),
    ("link-ref-shortcut", "[ref]\n\n[ref]: /url"),
    ("link-ref-missing", "[nope][missing]"),
    ("link-ref-title", '[ref]\n\n[ref]: /url "the title"'),
    ("link-ref-case", "[REF]\n\n[ref]: /url"),
    ("link-ref-unicode-case", "[ΣΊΣΥΦΟΣ]\n\n[σίσυφος]: /url"),
    ("link-ref-dupe", "[a]\n\n[a]: /first\n[a]: /second"),
    ("link-ref-multiline", '[a]\n\n[a]:\n  /url\n  "title"'),
    ("image-inline", "![alt](/img.png)"),
    ("image-title", '![alt](/img.png "t")'),
    ("image-nested", "![*alt*](/img.png)"),
    ("image-ref", "![alt][ref]\n\n[ref]: /img.png"),
    ("image-empty-alt", "![](/img.png)"),
    ("autolink-uri", "<https://example.com/path?q=1>"),
    ("autolink-mail", "<user@example.com>"),
    ("autolink-scheme", "<custom-scheme:body>"),
    ("html-inline-tag", "text <span class='x'>inline</span> text"),
    ("html-inline-selfclose", "before <br/> after"),
    ("html-inline-comment", "a <!-- inline comment --> b"),
    ("html-inline-attr-quotes", '<a href="x" title=\'y\'>z</a>'),
    ("entity-named", "&amp; &lt; &gt; &quot; &nbsp;"),
    ("entity-decimal", "&#35; &#1234; &#992; &#0;"),
    ("entity-hex", "&#X22; &#xD06; &#xcab;"),
    ("entity-invalid", "&nope; &#x110000; &#;"),
    ("escape-punct", "\\*not em\\* \\`not code\\`"),
    ("escape-backslash", "a\\\\b"),
    ("escape-nonpunct", "\\a \\1 \\ "),
    ("hardbreak-spaces", "line one  \nline two"),
    ("hardbreak-backslash", "line one\\\nline two"),
    ("softbreak", "line one\nline two"),
    ("smart-quotes", '"double" and \'single\' quotes'),
    ("smart-dashes", "en -- dash and em --- dash"),
    ("smart-ellipsis", "ellipsis... here"),
    ("smart-apostrophe", "it's John's"),
    ("special-chars", "< > & \" ' chars"),
    ("latex-specials", "100% of $x_i$ & #hash {brace} ~tilde^caret"),
    ("man-specials", "a\\-b and a'b and .leading dot"),
    ("bracket-unmatched", "[unclosed and ]stray"),
    ("literal-nul-adjacent", "before\x00after"),
]

UTF8_FRAGMENTS: list[tuple[str, str]] = [
    ("utf8-ascii", "plain ascii"),
    ("utf8-latin1", "café naïve"),
    ("utf8-greek", "αβγδ"),
    ("utf8-cyrillic", "добро"),
    ("utf8-cjk", "中文文字"),
    ("utf8-hangul", "한국어"),
    ("utf8-emoji", "\U0001f600\U0001f680"),
    ("utf8-astral-pair", "\U0001d11e\U0001f1e6\U0001f1e7"),
    ("utf8-combining", "éà"),
    ("utf8-zwj", "a‍joined"),
    ("utf8-bidi", "‮reversed‬"),
    ("utf8-nbsp", "a b"),
    ("utf8-ideographic-space", "a　b"),
    ("utf8-bom", "﻿document with bom"),
    ("utf8-replacement", "a�b"),
    ("utf8-high-plane", "\U0010fffe end of plane"),
]

# Raw byte fragments. These are deliberately not valid UTF-8: the reference
# implementation has specific behavior with and without --validate-utf8 and a
# migration that leans on Rust's UTF-8-only string types will diverge here.
RAW_BYTE_FRAGMENTS: list[tuple[str, bytes]] = [
    ("raw-lone-continuation", b"before \x80 after"),
    ("raw-truncated-2byte", b"before \xc3 after"),
    ("raw-truncated-3byte", b"before \xe2\x82 after"),
    ("raw-truncated-4byte", b"before \xf0\x9f\x98 after"),
    ("raw-overlong-2", b"overlong \xc0\xaf end"),
    ("raw-overlong-3", b"overlong \xe0\x80\xaf end"),
    ("raw-overlong-4", b"overlong \xf0\x80\x80\xaf end"),
    ("raw-surrogate-high", b"surrogate \xed\xa0\x80 end"),
    ("raw-surrogate-low", b"surrogate \xed\xb0\x80 end"),
    ("raw-above-max", b"above \xf4\x90\x80\x80 end"),
    ("raw-fe-ff", b"invalid \xfe\xff end"),
    ("raw-nul", b"nul \x00 byte"),
    ("raw-nul-in-code", b"`code \x00 span`"),
    ("raw-nul-in-link", b"[a](/u\x00rl)"),
    ("raw-cr-only", b"cr only\rsecond line"),
    ("raw-crlf", b"crlf\r\nsecond line"),
    ("raw-mixed-endings", b"a\nb\r\nc\rd"),
    ("raw-trailing-cr", b"trailing cr\r"),
    ("raw-no-final-newline", b"no final newline"),
    ("raw-only-newlines", b"\n\n\n"),
    ("raw-empty", b""),
    ("raw-ff-in-text", b"form\x0cfeed"),
    ("raw-vt-in-text", b"vert\x0btab"),
    ("raw-c1-controls", b"c1 \xc2\x80\xc2\x9f end"),
    ("raw-invalid-in-fence-info", b"```\xff\ncode\n```"),
    ("raw-invalid-in-heading", b"# head \xc3 ing"),
    ("raw-invalid-in-url", b"[a](/x\xffy)"),
    ("raw-invalid-in-entity", b"&am\xffp;"),
]

# Pathological shapes. Upstream keeps these because they historically caused
# quadratic blow-up or stack exhaustion; a Rust rewrite has to survive them
# with the same output, not merely avoid crashing.
def pathological_documents() -> list[tuple[str, bytes]]:
    n_small, n_mid = 60, 200
    return [
        ("path-nested-brackets", (b"[" * n_mid) + b"a" + (b"]" * n_mid)),
        ("path-nested-parens", (b"(" * n_mid) + b"a" + (b")" * n_mid)),
        ("path-nested-emph-star", (b"*" * n_mid) + b"a" + (b"*" * n_mid)),
        ("path-nested-emph-under", (b"_" * n_mid) + b"a" + (b"_" * n_mid)),
        ("path-nested-strong", (b"**" * n_small) + b"a" + (b"**" * n_small)),
        ("path-unclosed-links", b"[a](<b" * n_mid),
        ("path-unclosed-links2", b"[a](b" * n_mid),
        ("path-image-bracket", b"![[]()" * n_small),
        ("path-emph-link", b"**x [a*b**c*](d)" * n_small),
        ("path-backticks", b"".join(b"e" + b"`" * i for i in range(1, 80))),
        ("path-backtick-runs", (b"`" * 80) + b"a" + (b"`" * 79)),
        ("path-nested-blockquote", (b"> " * n_mid) + b"a"),
        ("path-nested-list", b"".join(b" " * i + b"- a\n" for i in range(60))),
        ("path-many-refs", b"".join(b"[a]: /u%d\n" % i for i in range(200))),
        ("path-ref-lookups", b"[a]\n" * n_mid + b"\n[a]: /u\n"),
        ("path-many-blanks", b"a" + b"\n" * 400 + b"b"),
        ("path-long-line", b"word " * 2000),
        ("path-long-word", b"w" * 8000),
        ("path-many-paragraphs", b"para\n\n" * 400),
        ("path-many-headings", b"".join(b"# h%d\n\n" % i for i in range(200))),
        ("path-many-hrs", b"---\n\n" * 300),
        ("path-many-entities", b"&amp;" * 800),
        ("path-many-escapes", b"\\*" * 800),
        ("path-html-open", b"</" * n_mid),
        ("path-list-marker-only", b"- \n" * n_mid),
        ("path-quote-marker-only", b"> \n" * n_mid),
        ("path-star-only", b"*\n" * n_mid),
        ("path-tab-indent", b"\t" * 40 + b"code"),
        ("path-space-indent", b" " * 400 + b"text"),
        ("path-mixed-nesting", (b"> - " * 40) + b"deep"),
        ("path-fence-in-list", b"- ```\n  a\n  ```\n" * 40),
        ("path-setext-runs", b"a\n" + b"=" * 2000),
        ("path-autolink-runs", b"<https://a.example/" + b"p" * 2000 + b">"),
        ("path-entity-prefix", b"&" + b"a" * 2000 + b";"),
        ("path-many-softbreaks", b"a\n" * 800),
        ("path-many-hardbreaks", b"a  \n" * 400),
    ]


def wrap_documents() -> list[tuple[str, str]]:
    """Documents whose rendering differs across wrap widths."""
    lorem = (
        "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do "
        "eiusmod tempor incididunt ut labore et dolore magna aliqua."
    )
    return [
        ("wrap-plain-para", lorem),
        ("wrap-two-paras", lorem + "\n\n" + lorem),
        ("wrap-in-quote", "> " + lorem),
        ("wrap-in-list", "- " + lorem + "\n- " + lorem),
        ("wrap-ordered-list", "1. " + lorem + "\n2. " + lorem),
        ("wrap-nested-quote-list", "> - " + lorem),
        ("wrap-with-emph", "*" + lorem + "*"),
        ("wrap-with-code", "`" + lorem + "`"),
        ("wrap-with-link", "[" + lorem + "](/url)"),
        ("wrap-heading", "# " + lorem),
        ("wrap-setext", lorem + "\n" + "=" * 10),
        ("wrap-long-url", "[a](/" + "u" * 200 + ")"),
        ("wrap-fence", "```\n" + lorem + "\n```"),
        ("wrap-indent-code", "    " + lorem),
        ("wrap-hardbreak", lorem + "  \n" + lorem),
        ("wrap-mixed", "# " + lorem + "\n\n> " + lorem + "\n\n- " + lorem),
        ("wrap-no-space-word", "x" * 300),
        ("wrap-cjk", "中文" * 200),
        ("wrap-entity-heavy", "&amp; " * 200),
        ("wrap-html-inline", "<em>" + lorem + "</em>"),
    ]


ARRAY_RE = re.compile(
    r"static const (?:uint32_t|unsigned char) (\w+)\[\d+\]\s*=\s*\{(.*?)\};",
    re.S,
)
HEX_RE = re.compile(r"0x[0-9A-Fa-f]+")


def read_entity_names(repo: Path) -> list[str]:
    """Decode every named entity out of the pinned upstream entity table.

    `src/entities.inc` is the largest piece of generated data in the C tree:
    2125 entities bit-packed into a `uint32_t` index array over a flat
    `unsigned char` text blob, looked up by binary search.  Nothing about that
    encoding is part of the public interface, so a rewrite is free to store the
    table however it likes -- but every name still has to resolve to the same
    replacement.  Decoding the table here means the corpus covers the real
    contents rather than a hand-picked sample, and it stays correct even though
    the verifier never assumes the rewrite kept the packing.
    """
    text = (repo / "src" / "entities.inc").read_text(encoding="utf-8")
    arrays = {name: body for name, body in ARRAY_RE.findall(text)}
    missing = {"cmark_entities", "cmark_entity_text"} - set(arrays)
    if missing:
        raise SystemExit(f"entities.inc is missing arrays: {sorted(missing)}")
    packed = [int(v, 16) for v in HEX_RE.findall(arrays["cmark_entities"])]
    blob = bytes(int(v, 16) for v in HEX_RE.findall(arrays["cmark_entity_text"]))

    names: list[str] = []
    for value in packed:
        offset = value & 0x7FFF
        name_size = (value >> 15) & 0x1F
        name = blob[offset : offset + name_size]
        if len(name) != name_size or not name:
            raise SystemExit(f"entity decode failed at packed value {value:#x}")
        names.append(name.decode("ascii") + ";")
    if len(names) != 2125:
        raise SystemExit(f"entity table yielded {len(names)} names, want 2125")
    return names


def entity_documents(names: list[str], batch: int = 20) -> list[tuple[str, str]]:
    documents = []
    for start in range(0, len(names), batch):
        chunk = names[start : start + batch]
        body = " ".join("&" + name for name in chunk)
        # Mix in a code span and a fence so the escape tables are exercised on
        # entity output too, not just plain paragraph text.
        text = f"{body}\n\n`{body}`\n\n```\n{body}\n```\n"
        documents.append((f"entity-batch-{start // batch:04d}", text))
    return documents


def numeric_entity_documents() -> list[tuple[str, str]]:
    """Numeric character references, including the illegal ranges."""
    groups = {
        "ascii": [0x21, 0x23, 0x26, 0x3C, 0x3E, 0x40, 0x5C, 0x60, 0x7E],
        "controls": [0x00, 0x01, 0x08, 0x09, 0x0A, 0x0D, 0x1F, 0x7F],
        "latin": [0xA0, 0xA9, 0xAE, 0xB1, 0xBF, 0xFF],
        "bmp": [0x100, 0x3A9, 0x4E2D, 0xFEFF, 0xFFFD],
        "surrogates": [0xD800, 0xDBFF, 0xDC00, 0xDFFF],
        "astral": [0x10000, 0x1D11E, 0x1F600, 0x10FFFF],
        "overmax": [0x110000, 0x200000, 0xFFFFFFF],
    }
    documents = []
    for group, points in groups.items():
        decimal = " ".join(f"&#{p};" for p in points)
        hex_lower = " ".join(f"&#x{p:x};" for p in points)
        hex_upper = " ".join(f"&#X{p:X};" for p in points)
        text = f"{decimal}\n\n{hex_lower}\n\n{hex_upper}\n"
        documents.append((f"numeric-entity-{group}", text))
    malformed = [
        "&#;", "&#x;", "&#X;", "&#-1;", "&#+1;", "&# 1;", "&#1", "&#x1",
        "&#0000000000000001;", "&#xxx;", "&amp", "&;", "&#a;", "&#1a;",
        "&AMP; &Amp; &amp;", "&NotAnEntity; &notanentity;",
    ]
    documents.append(("numeric-entity-malformed", "\n\n".join(malformed) + "\n"))
    return documents


def case_folding_documents() -> list[tuple[str, str]]:
    """Reference labels that only match under Unicode case folding.

    cmark normalizes link labels with its own case-folding table.  These
    documents pair a reference definition with a differently-cased use so a
    rewrite that reached for plain ASCII lowercasing produces unresolved links.
    """
    pairs = [
        ("ascii", "Simple Label", "simple label"),
        ("greek-sigma", "ΣΊΣΥΦΟΣ", "σίσυφος"),
        ("greek-final", "ὈΔΥΣΣΕΎΣ", "ὀδυσσεύς"),
        ("german-sharp-s", "STRASSE", "straße"),
        ("turkish-dotless", "İSTANBUL", "istanbul"),
        ("cyrillic", "ПРИВЕТ", "привет"),
        ("armenian", "ԵՒ", "եւ"),
        ("ligature-fi", "FI", "ﬁ"),
        ("cherokee", "Ꭰ", "ꭰ"),
        ("deseret", "\U00010400", "\U00010428"),
        ("kelvin", "K", "K"),
        ("angstrom", "Å", "Å"),
        ("long-s", "S", "ſ"),
        ("mixed-ws", "a  \t b", "a b"),
        ("mixed-case-ws", "  A   B  ", "a b"),
    ]
    documents = []
    for name, used, defined in pairs:
        text = f"[{used}]\n\n[{defined}]: /url-{name}\n"
        documents.append((f"fold-{name}", text))
        # Reverse direction: define with the upper form, use the lower one.
        text = f"[{defined}]\n\n[{used}]: /rev-{name}\n"
        documents.append((f"fold-rev-{name}", text))
    return documents


class Stream:
    """SHA-256 counter-mode PRNG.

    Python's `random` module is explicitly not a stable API across releases;
    this corpus has to hash identically for the lifetime of the task, so the
    randomness comes from a construction that is pinned by the standard.
    """

    def __init__(self, seed: str) -> None:
        self._seed = seed.encode("utf-8")
        self._counter = 0
        self._buffer = b""

    def _refill(self) -> None:
        block = hashlib.sha256(
            self._seed + self._counter.to_bytes(8, "big")
        ).digest()
        self._counter += 1
        self._buffer += block

    def byte(self) -> int:
        if not self._buffer:
            self._refill()
        value = self._buffer[0]
        self._buffer = self._buffer[1:]
        return value

    def below(self, limit: int) -> int:
        """Uniform in [0, limit) by rejection sampling on 32-bit draws."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        span = (1 << 32) // limit * limit
        while True:
            value = int.from_bytes(bytes(self.byte() for _ in range(4)), "big")
            if value < span:
                return value % limit

    def pick(self, items):
        return items[self.below(len(items))]

    def sample(self, items, count: int):
        pool = list(items)
        chosen = []
        for _ in range(min(count, len(pool))):
            chosen.append(pool.pop(self.below(len(pool))))
        return chosen


CONTAINER_WRAPS: list[tuple[str, str]] = [
    ("plain", "{}"),
    ("quote", "> {}"),
    ("quote2", "> > {}"),
    ("bullet", "- {}"),
    ("ordered", "3. {}"),
    ("bullet-nested", "- outer\n\n  - {}"),
    ("quote-bullet", "> - {}"),
    ("bullet-quote", "- > {}"),
]


def indent_block(text: str, prefix: str) -> str:
    """Re-indent a multi-line fragment so it stays inside its container."""
    lines = text.split("\n")
    out = []
    for index, line in enumerate(lines):
        if index == 0:
            out.append(prefix + line)
        elif line == "":
            out.append(prefix.rstrip())
        else:
            out.append(" " * len(prefix) + line)
    return "\n".join(out)


def compose_documents(count: int, seed: str) -> list[tuple[str, str]]:
    """Build integration documents by nesting real constructs inside containers.

    Each document mixes several subsystems, so the composite family catches
    interaction bugs that per-construct documents miss: a container that
    forgets to strip its marker before handing the line to the inline parser,
    a renderer that loses indentation state, a wrap engine that miscounts a
    prefix.
    """
    stream = Stream(seed)
    all_blocks = BLOCK_FRAGMENTS
    all_inlines = INLINE_FRAGMENTS + [(n, t) for n, t in UTF8_FRAGMENTS]
    documents = []
    for index in range(count):
        pieces: list[str] = []
        for _ in range(2 + stream.below(4)):
            kind = stream.below(3)
            if kind == 0:
                _, body = stream.pick(all_blocks)
            elif kind == 1:
                names = stream.sample(all_inlines, 1 + stream.below(3))
                body = " ".join(text for _, text in names)
            else:
                _, inner = stream.pick(all_blocks)
                wrap_name, wrap = stream.pick(CONTAINER_WRAPS)
                prefix = wrap.split("{}")[0]
                body = indent_block(inner, prefix) if prefix else inner
            pieces.append(body)
        text = "\n\n".join(pieces)
        if not text.endswith("\n"):
            text += "\n"
        documents.append((f"compose-{index:04d}", text))
    return documents


COMPOSE_COUNT = 120
COMPOSE_SEED = "swerefactor/lang01-cmark/compose/v1"


def build(repo: Path, out: Path) -> dict:
    """Assemble every family and write the corpus plus its index."""
    families: list[tuple[str, list[tuple[str, bytes]]]] = []

    def as_bytes(items):
        return [
            (name, body if isinstance(body, bytes) else body.encode("utf-8"))
            for name, body in items
        ]

    spec = repo / "test" / "spec.txt"
    smart = repo / "test" / "smart_punct.txt"
    regression = repo / "test" / "regression.txt"

    spec_docs = extract_examples(spec.read_text(encoding="utf-8"))
    smart_docs = extract_examples(smart.read_text(encoding="utf-8"))
    regr_docs = extract_examples(regression.read_text(encoding="utf-8"))
    if len(spec_docs) != 652:
        raise SystemExit(f"spec.txt yielded {len(spec_docs)} examples, want 652")
    if len(smart_docs) != 16:
        raise SystemExit(f"smart_punct.txt yielded {len(smart_docs)}, want 16")
    if len(regr_docs) != 23:
        raise SystemExit(f"regression.txt yielded {len(regr_docs)}, want 23")

    families.append(
        ("spec", as_bytes((f"spec-{i:04d}", d) for i, d in enumerate(spec_docs)))
    )
    families.append(
        ("smart", as_bytes((f"smart-{i:04d}", d) for i, d in enumerate(smart_docs)))
    )
    families.append(
        ("regression", as_bytes((f"regr-{i:04d}", d) for i, d in enumerate(regr_docs)))
    )
    families.append(("block", as_bytes(BLOCK_FRAGMENTS)))
    families.append(("inline", as_bytes(INLINE_FRAGMENTS)))
    families.append(("utf8", as_bytes(UTF8_FRAGMENTS)))
    families.append(("rawbytes", as_bytes(RAW_BYTE_FRAGMENTS)))
    families.append(("pathological", as_bytes(pathological_documents())))
    families.append(("wrap", as_bytes(wrap_documents())))
    families.append(("entity", as_bytes(entity_documents(read_entity_names(repo)))))
    families.append(("numeric", as_bytes(numeric_entity_documents())))
    families.append(("folding", as_bytes(case_folding_documents())))
    families.append(
        ("compose", as_bytes(compose_documents(COMPOSE_COUNT, COMPOSE_SEED)))
    )

    docs_dir = out / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    entries = []
    seen_ids: set[str] = set()
    index = 0
    for family, items in families:
        for name, body in items:
            if name in seen_ids:
                raise SystemExit(f"duplicate corpus id {name}")
            seen_ids.add(name)
            path = docs_dir / f"{index:05d}.md"
            path.write_bytes(body)
            entries.append(
                {
                    "index": index,
                    "id": name,
                    "family": family,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
            index += 1

    manifest = {
        "schema": "swerefactor-corpus-v1",
        "task": "lang01-cmark-c-to-rust",
        "compose_seed": COMPOSE_SEED,
        "count": len(entries),
        "families": {f: sum(1 for e in entries if e["family"] == f) for f, _ in families},
        "documents": entries,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["digest"] = hashlib.sha256(payload).hexdigest()
    (out / "corpus.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    manifest = build(args.repo, args.out)
    print(f"corpus documents: {manifest['count']}")
    for family, count in sorted(manifest["families"].items()):
        print(f"  {family:14s} {count:5d}")
    print(f"corpus digest: {manifest['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
