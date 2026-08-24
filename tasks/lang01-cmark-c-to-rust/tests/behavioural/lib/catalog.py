#!/usr/bin/env python3
"""Case catalog for lang01-cmark-c-to-rust.

This module enumerates the evaluation's workflows.  A *workflow* here is one
distinct product behavior exercised end to end through a shipped entry point --
the installed library through its published header, the installed `cmark`
executable, the installed CMake package, or the installed pkg-config file.
Changing a parameter does not create a workflow: rendering the same document at
four wrap widths is one workflow with four assertions, not four workflows.
Changing the *code path* does: the same nested list through the HTML writer and
through the roff writer are two workflows, because those are two escape tables
and two layout engines that can fail independently.

Every behavioural workflow is graded by differential comparison against the
pinned C reference, which is built from source inside the verifier image.  No
expected output is written by hand, so the catalog cannot drift away from what
the reference actually does, and quirks are graded as behavior rather than as
bugs to be fixed.

Kinds:
  probe  -- a line of the probe protocol, run against both the reference and
            the submission and compared byte for byte
  cli    -- an argv for the installed executable, compared on stdout, stderr
            and exit status
  struct -- a structural fact about the build, install tree, ABI or package
            metadata, evaluated by the build/abi modules
  guard  -- a migration-audit gate, evaluated by the audit module

`struct` and `guard` cases are declared here so the catalog is the single
inventory of what the task measures; their logic lives in the modules that own
the evidence.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

# Wrap widths asserted inside a single wrap workflow.
WRAP_WIDTHS = (0, 20, 40, 72)

# Chunk sizes used by the streaming families.  Each size is its own workflow:
# a one-byte feed and a 4096-byte feed exercise different buffering states.
STREAM_CHUNKS = (1, 7, 64, 4096)


def stride(items: list, count: int) -> list:
    """Evenly spaced deterministic subset, preserving order.

    Stride selection rather than sampling: the choice is obvious from the
    inputs, reproducible without a PRNG, and spreads coverage across a family
    instead of clustering at its start.
    """
    if count >= len(items):
        return list(items)
    if count <= 0:
        return []
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


class Catalog:
    def __init__(self, corpus: dict) -> None:
        self.docs = corpus["documents"]
        self.by_family: dict[str, list[int]] = {}
        for entry in self.docs:
            self.by_family.setdefault(entry["family"], []).append(entry["index"])
        self.cases: list[dict] = []
        self._ids: set[str] = set()

    # -- registration -----------------------------------------------------

    def add(
        self,
        case_id: str,
        family: str,
        kind: str,
        *,
        fields: list[str] | None = None,
        argv: list[str] | None = None,
        stdin_doc: int | None = None,
        check: str | None = None,
        params: dict | None = None,
        configs: tuple[str, ...] = ("shared",),
        weight: float = 1.0,
        note: str = "",
    ) -> None:
        if case_id in self._ids:
            raise SystemExit(f"duplicate case id: {case_id}")
        self._ids.add(case_id)
        case = {
            "id": case_id,
            "family": family,
            "kind": kind,
            "configs": list(configs),
            "weight": weight,
        }
        if fields is not None:
            # A workflow may carry several assertions: the same behavior probed
            # at several widths or chunk sizes is one workflow, and it only
            # passes if every assertion matches.
            rows = fields if fields and isinstance(fields[0], list) else [fields]
            case["asserts"] = rows
        if argv is not None:
            case["argv"] = argv
        if stdin_doc is not None:
            case["stdin_doc"] = stdin_doc
        if check is not None:
            case["check"] = check
        if params:
            case["params"] = params
        if note:
            case["note"] = note
        self.cases.append(case)

    def probe(self, case_id: str, family: str, fields: list[str], **kw) -> None:
        self.add(case_id, family, "probe", fields=fields, **kw)

    # -- A. conformance: the specification corpus through the HTML writer --

    def build_conformance(self) -> None:
        """The CommonMark conformance suite, one workflow per example.

        These are the behaviors the upstream project treats as its definition
        of correctness, so they carry the most weight per workflow.  Each is
        run through `cmark_markdown_to_html`, the one-call convenience entry
        point most consumers actually use.
        """
        for index in self.by_family["spec"]:
            doc = self.docs[index]
            self.probe(
                f"spec/{doc['id']}",
                "conformance",
                [str(index), "md2html", str(index), "default"],
                weight=1.0,
            )
        for index in self.by_family["smart"]:
            doc = self.docs[index]
            self.probe(
                f"smart/{doc['id']}",
                "conformance",
                [str(index), "md2html", str(index), "smart"],
            )
        for index in self.by_family["regression"]:
            doc = self.docs[index]
            self.probe(
                f"regression/{doc['id']}",
                "conformance",
                [str(index), "md2html", str(index), "default"],
            )

    # -- B. renderers: every writer over the construct corpus --------------

    def build_renderers(self) -> None:
        """Each (construct family, writer) pair is an independent workflow.

        cmark ships five writers.  They share a tree and nothing else: HTML
        escapes for markup, roff escapes for the man writer, LaTeX escapes for
        the LaTeX writer, and the CommonMark writer has to re-emit source that
        parses back to the same tree.  A rewrite that ports the HTML writer and
        approximates the rest passes family A and fails here, which is exactly
        the distinction the score needs to make.
        """
        writers = ("html", "xml", "man", "latex", "commonmark")
        for family in ("block", "inline", "utf8"):
            for index in self.by_family[family]:
                doc = self.docs[index]
                for writer in writers:
                    self.probe(
                        f"render/{writer}/{doc['id']}",
                        f"render-{writer}",
                        [
                            f"{index}-{writer}",
                            "render",
                            str(index),
                            writer,
                            "default",
                            "0",
                        ],
                    )
        # The conformance corpus through the non-HTML writers, strided: enough
        # to cover the whole construct space without restating family A.
        for writer in ("xml", "man", "latex", "commonmark"):
            for index in stride(self.by_family["spec"], 80):
                doc = self.docs[index]
                self.probe(
                    f"render/{writer}/{doc['id']}",
                    f"render-{writer}",
                    [
                        f"{index}-{writer}",
                        "render",
                        str(index),
                        writer,
                        "default",
                        "0",
                    ],
                )
        # Round-trip: source emitted by the CommonMark writer must re-parse to
        # the same HTML.  This is a property the writer promises and the only
        # place where two subsystems are checked against each other rather than
        # against the reference.
        for index in stride(self.by_family["spec"], 60):
            doc = self.docs[index]
            self.probe(
                f"roundtrip/{doc['id']}",
                "roundtrip",
                [f"{index}-rt", "render", str(index), "commonmark", "default", "0"],
                params={"roundtrip": True},
                note="commonmark output re-parsed and compared as HTML",
            )

    # -- C. data tables ----------------------------------------------------

    def build_data_tables(self) -> None:
        """Entity table and numeric character references.

        `entities.inc` is 2125 generated entries.  Batched documents cover the
        whole table, so a rewrite that ported a subset (or reached for a Rust
        crate's differently-sourced table) diverges on a specific batch, and the
        report names it.
        """
        for index in self.by_family["entity"]:
            doc = self.docs[index]
            self.probe(
                f"entity/{doc['id']}",
                "entities",
                [str(index), "md2html", str(index), "default"],
            )
        for index in self.by_family["numeric"]:
            doc = self.docs[index]
            self.probe(
                f"numeric/{doc['id']}",
                "entities",
                [str(index), "md2html", str(index), "default"],
            )
        # Case folding drives link-label matching; a mis-ported fold table
        # silently turns links into literal text.
        for index in self.by_family["folding"]:
            doc = self.docs[index]
            self.probe(
                f"folding/{doc['id']}",
                "casefold",
                [str(index), "render", str(index), "html", "default", "0"],
            )
            self.probe(
                f"folding/tree/{doc['id']}",
                "casefold",
                [f"{index}-tree", "tree", str(index), "html", "default", "0"],
            )

    # -- D. encoding -------------------------------------------------------

    def build_encoding(self) -> None:
        """Byte-level input handling, including inputs that are not UTF-8.

        cmark accepts arbitrary bytes and has defined behavior for invalid
        UTF-8 both with and without CMARK_OPT_VALIDATE_UTF8.  This is the
        family most likely to catch a rewrite that assumed its input was a
        Rust `str`.
        """
        for index in self.by_family["rawbytes"]:
            doc = self.docs[index]
            for options in ("default", "validate_utf8"):
                self.probe(
                    f"bytes/{options}/{doc['id']}",
                    "encoding",
                    [
                        f"{index}-{options}",
                        "md2html",
                        str(index),
                        options,
                    ],
                )
            self.probe(
                f"bytes/tree/{doc['id']}",
                "encoding",
                [f"{index}-tree", "tree", str(index), "html", "default", "0"],
            )
        for index in self.by_family["utf8"]:
            doc = self.docs[index]
            self.probe(
                f"utf8/validate/{doc['id']}",
                "encoding",
                [f"{index}-val", "md2html", str(index), "validate_utf8"],
            )

    # -- E. robustness -----------------------------------------------------

    def build_pathological(self) -> None:
        """Inputs upstream keeps because they once caused blow-ups.

        Both output equality and completion are graded: an implementation that
        is correct but quadratic fails by timing out, which is the right
        outcome for a library whose callers feed it untrusted input.
        """
        for index in self.by_family["pathological"]:
            doc = self.docs[index]
            self.probe(
                f"path/html/{doc['id']}",
                "pathological",
                [str(index), "md2html", str(index), "default"],
            )
            self.probe(
                f"path/cm/{doc['id']}",
                "pathological",
                [f"{index}-cm", "render", str(index), "commonmark", "default", "0"],
            )

    def build_wrapping(self) -> None:
        """The wrap engine, asserted at several widths per workflow.

        Width is a parameter, not a workflow: one document through the roff
        writer is one behavior, checked at four widths.
        """
        for writer in ("commonmark", "man", "latex"):
            for index in self.by_family["wrap"]:
                doc = self.docs[index]
                self.probe(
                    f"wrap/{writer}/{doc['id']}",
                    "wrapping",
                    [
                        [
                            f"{index}-{writer}-w{width}",
                            "render",
                            str(index),
                            writer,
                            "default",
                            str(width),
                        ]
                        for width in WRAP_WIDTHS
                    ],
                )

    # -- F. options --------------------------------------------------------

    def build_options(self) -> None:
        """Each documented option is its own behavior on its own inputs.

        Options are the library's configuration surface: `--safe` is a security
        control, `--sourcepos` feeds editor integrations, `--smart` changes
        typography.  A rewrite that ignores an option flag still passes the
        default-path families, so each option gets graded separately against
        inputs chosen to be sensitive to it.
        """
        sensitive = {
            "sourcepos": ("spec", 70),
            "hardbreaks": ("inline", 40),
            "nobreaks": ("inline", 40),
            "safe": ("block", 40),
            "unsafe": ("block", 40),
            "normalize": ("inline", 30),
            "smart": ("inline", 40),
            "validate_utf8": ("block", 20),
        }
        for option, (family, count) in sensitive.items():
            for index in stride(self.by_family[family], count):
                doc = self.docs[index]
                self.probe(
                    f"option/{option}/{doc['id']}",
                    f"option-{option}",
                    [
                        f"{index}-{option}",
                        "md2html",
                        str(index),
                        option,
                    ],
                )
        # Option interaction: flags are a bit set and callers combine them.
        combos = (
            "sourcepos,hardbreaks",
            "sourcepos,smart",
            "safe,sourcepos",
            "unsafe,hardbreaks",
            "smart,nobreaks",
            "normalize,smart",
            "validate_utf8,smart",
            "safe,smart,sourcepos",
            "unsafe,smart,hardbreaks",
            "sourcepos,normalize,validate_utf8",
        )
        for combo in combos:
            label = combo.replace(",", "+")
            for index in stride(self.by_family["spec"], 12):
                doc = self.docs[index]
                self.probe(
                    f"option/{label}/{doc['id']}",
                    "option-combined",
                    [f"{index}-{label}", "md2html", str(index), combo],
                )
        # Sourcepos through the XML writer, which is where positions are
        # actually published to consumers.
        for index in stride(self.by_family["spec"], 40):
            doc = self.docs[index]
            self.probe(
                f"option/sourcepos-xml/{doc['id']}",
                "option-sourcepos",
                [f"{index}-spxml", "render", str(index), "xml", "sourcepos", "0"],
            )

    # -- G. streaming ------------------------------------------------------

    def build_streaming(self) -> None:
        """The incremental parser: feed / finish rather than one-shot parse.

        This is a separate published entry point with its own buffering, and it
        is how large inputs are actually processed.  Each chunk size is its own
        workflow because each puts the parser in a different partial-line state;
        a one-byte feed splits every multi-byte character and every fence
        marker, a 4096-byte feed splits almost nothing.
        """
        for chunk in STREAM_CHUNKS:
            for index in stride(self.by_family["spec"], 45):
                doc = self.docs[index]
                self.probe(
                    f"stream/{chunk}/{doc['id']}",
                    "streaming",
                    [
                        f"{index}-c{chunk}",
                        "render",
                        str(index),
                        "html",
                        "default",
                        "0",
                        str(chunk),
                    ],
                )
        # Chunk boundaries inside multi-byte sequences and inside raw invalid
        # bytes -- the cases where a naive buffer reassembly differs.
        for chunk in (1, 2, 3):
            for index in self.by_family["rawbytes"]:
                doc = self.docs[index]
                self.probe(
                    f"stream/raw{chunk}/{doc['id']}",
                    "streaming",
                    [
                        f"{index}-rc{chunk}",
                        "render",
                        str(index),
                        "html",
                        "default",
                        "0",
                        str(chunk),
                    ],
                )
        for index in stride(self.by_family["utf8"], 16):
            doc = self.docs[index]
            self.probe(
                f"stream/utf8/{doc['id']}",
                "streaming",
                [
                    [
                        f"{index}-u{chunk}",
                        "render",
                        str(index),
                        "html",
                        "default",
                        "0",
                        str(chunk),
                    ]
                    for chunk in (1, 2, 3, 5)
                ],
            )
        # The FILE* entry point: a distinct read loop from the buffer one.
        for index in stride(self.by_family["spec"], 40):
            doc = self.docs[index]
            self.probe(
                f"parsefile/{doc['id']}",
                "parsefile",
                [f"{index}-pf", "parsefile", str(index), "html", "default"],
            )
        for index in stride(self.by_family["rawbytes"], 14):
            doc = self.docs[index]
            self.probe(
                f"parsefile/raw/{doc['id']}",
                "parsefile",
                [f"{index}-pfr", "parsefile", str(index), "html", "default"],
            )

    # -- H. tree / AST -----------------------------------------------------

    def build_tree(self) -> None:
        """The parsed tree as consumers see it through the accessors.

        Rendered bytes can agree while the tree disagrees: wrong source spans,
        a dropped fence info string, a list that reports itself tight when it is
        loose.  Consumers that walk the tree -- linters, editors, translators --
        depend on these, so they are graded independently of any writer.
        """
        for index in stride(self.by_family["spec"], 120):
            doc = self.docs[index]
            self.probe(
                f"tree/accessors/{doc['id']}",
                "tree-accessors",
                [f"{index}-acc", "tree", str(index), "html", "sourcepos", "0"],
            )
        for index in stride(self.by_family["spec"], 60):
            doc = self.docs[index]
            self.probe(
                f"tree/links/{doc['id']}",
                "tree-links",
                [f"{index}-lnk", "links", str(index), "html", "default", "0"],
                note="navigation accessors must agree with the iterator",
            )
        for family, count in (("block", 56), ("inline", 64), ("compose", 60)):
            for index in stride(self.by_family[family], count):
                doc = self.docs[index]
                self.probe(
                    f"tree/{family}/{doc['id']}",
                    "tree-accessors",
                    [f"{index}-tacc", "tree", str(index), "html", "sourcepos", "0"],
                )
        # Text-node consolidation is a published tree transform.
        for index in stride(self.by_family["inline"], 40):
            doc = self.docs[index]
            self.probe(
                f"tree/consolidate/{doc['id']}",
                "tree-consolidate",
                [f"{index}-cons", "consolidate", str(index), "html", "default", "0"],
            )
        # Structural self-consistency of a freshly parsed tree.
        for index in stride(self.by_family["compose"], 40):
            doc = self.docs[index]
            self.probe(
                f"tree/check/{doc['id']}",
                "tree-check",
                [f"{index}-chk", "itercheck", str(index), "html", "default", "0"],
            )

    # -- I. iterators ------------------------------------------------------

    def build_iterators(self) -> None:
        """Iterator traversal, reset, and the documented edit-during-walk mode.

        The header explicitly permits modifying or freeing the current node on
        EXIT, and consumers build their tree rewriting on that guarantee.  It is
        also the easiest guarantee to lose in a rewrite whose iterator holds a
        borrow of the node it yields.
        """
        for mode in ("free_emph", "replace_text", "unlink_code", "strip_links"):
            for index in stride(self.by_family["spec"], 22):
                doc = self.docs[index]
                self.probe(
                    f"iter/edit/{mode}/{doc['id']}",
                    "iterator-edit",
                    [
                        f"{index}-ie-{mode}",
                        "iteredit",
                        str(index),
                        mode,
                        "default",
                        "0",
                    ],
                )
        for skip in (0, 1, 2, 5, 9):
            for index in stride(self.by_family["compose"], 12):
                doc = self.docs[index]
                self.probe(
                    f"iter/reset/{skip}/{doc['id']}",
                    "iterator-reset",
                    [
                        f"{index}-ir{skip}",
                        "iterreset",
                        str(index),
                        str(skip),
                        "default",
                        "0",
                    ],
                )

    # -- J. allocator ------------------------------------------------------

    def build_memory(self) -> None:
        """The pluggable allocator, and parsing into a caller-supplied root.

        `cmark_mem` is a transparent struct in the public header: callers embed
        it and pass it in, so it is ABI as well as API.  What is graded is the
        contract -- the allocator really backs the parse, returned buffers are
        freeable through it, nothing is outstanding at the end -- not the number
        of allocations, which a rewrite is free to change.
        """
        for index in stride(self.by_family["spec"], 40):
            doc = self.docs[index]
            self.probe(
                f"mem/parse/{doc['id']}",
                "allocator",
                [f"{index}-mem", "mem", str(index), "html", "default", "0"],
            )
        for index in stride(self.by_family["pathological"], 12):
            doc = self.docs[index]
            self.probe(
                f"mem/path/{doc['id']}",
                "allocator",
                [f"{index}-memp", "mem", str(index), "html", "default", "0"],
            )
        # Only container roots.  cmark's block parser backs up through
        # `finalize` until it finds a node that can contain the child, so a root
        # that can never contain a block (CUSTOM_BLOCK, PARAGRAPH, ...) walks off
        # the top of the tree and dereferences NULL.  That is undefined in the C
        # implementation, so it is not a behavior a rewrite can be asked to match.
        for root_type in ("DOCUMENT", "BLOCK_QUOTE", "ITEM"):
            for index in stride(self.by_family["spec"], 12):
                doc = self.docs[index]
                self.probe(
                    f"mem/intoroot/{root_type}/{doc['id']}",
                    "allocator-root",
                    [
                        f"{index}-ir-{root_type}",
                        "intoroot",
                        str(index),
                        root_type,
                        "default",
                        "0",
                    ],
                )

    # -- K. programmatic construction --------------------------------------

    # Node types and the text-bearing setters that are valid on them, used to
    # generate construction workflows without hand-writing each one.
    LEAF_WITH_LITERAL = ("TEXT", "CODE", "HTML_INLINE", "CODE_BLOCK", "HTML_BLOCK")
    INLINE_CONTAINERS = ("EMPH", "STRONG", "LINK", "IMAGE", "CUSTOM_INLINE")
    BLOCK_CONTAINERS = (
        "BLOCK_QUOTE",
        "PARAGRAPH",
        "HEADING",
        "ITEM",
        "CUSTOM_BLOCK",
    )
    ALL_TYPES = (
        "DOCUMENT", "BLOCK_QUOTE", "LIST", "ITEM", "CODE_BLOCK", "HTML_BLOCK",
        "CUSTOM_BLOCK", "PARAGRAPH", "HEADING", "THEMATIC_BREAK", "TEXT",
        "SOFTBREAK", "LINEBREAK", "CODE", "HTML_INLINE", "CUSTOM_INLINE",
        "EMPH", "STRONG", "LINK", "IMAGE",
    )
    WRITERS = ("html", "xml", "man", "latex", "commonmark")

    def _render_all(self, reg: str = "0") -> str:
        return ";".join(f"render:{reg}:{w}" for w in self.WRITERS)

    def build_api_construct(self) -> None:
        """Trees built through the API rather than by parsing.

        Every writer must handle a tree that no parser would produce: a heading
        containing a code span, a link with no children, an image inside an
        emphasis.  Consumers build such trees (translators, generators,
        templating systems), and a writer that only ever saw parser output tends
        to assume structure the API does not guarantee.
        """
        # A text-bearing leaf inside a paragraph, through all five writers.
        for leaf in self.LEAF_WITH_LITERAL:
            script = (
                "new:0:DOCUMENT;new:1:PARAGRAPH;append:0:1;"
                f"new:2:{leaf};setlit:2:alpha%20beta;append:1:2;"
                "dump:0;" + self._render_all()
            )
            self.probe(
                f"api/leaf/{leaf}",
                "api-construct",
                [f"leaf-{leaf}", "script", script],
            )
        # Inline containers wrapping a text node.
        for container in self.INLINE_CONTAINERS:
            script = (
                "new:0:DOCUMENT;new:1:PARAGRAPH;append:0:1;"
                f"new:2:{container};append:1:2;"
                "new:3:TEXT;setlit:3:inner;append:2:3;"
                "seturl:2:/target;settitle:2:the%20title;"
                "setonenter:2:%3Center%3E;setonexit:2:%3C/exit%3E;"
                "dump:0;" + self._render_all()
            )
            self.probe(
                f"api/inline/{container}",
                "api-construct",
                [f"inline-{container}", "script", script],
            )
        # Block containers holding a paragraph.
        for container in self.BLOCK_CONTAINERS:
            script = (
                f"new:0:DOCUMENT;new:1:{container};append:0:1;"
                "new:2:PARAGRAPH;append:1:2;new:3:TEXT;setlit:3:body;append:2:3;"
                "setonenter:1:%3Cb%3E;setonexit:1:%3C/b%3E;"
                "dump:0;" + self._render_all()
            )
            self.probe(
                f"api/block/{container}",
                "api-construct",
                [f"block-{container}", "script", script],
            )
        # Headings at every valid level.
        for level in range(1, 7):
            script = (
                f"new:0:DOCUMENT;new:1:HEADING;sethead:1:{level};append:0:1;"
                "new:2:TEXT;setlit:2:Head;append:1:2;" + self._render_all()
            )
            self.probe(
                f"api/heading/{level}",
                "api-construct",
                [f"heading-{level}", "script", script],
            )
        # Lists across type, delimiter, start and tightness.
        for list_type in ("BULLET", "ORDERED"):
            for delim in ("PERIOD", "PAREN"):
                for start in (1, 5):
                    for tight in (0, 1):
                        script = (
                            "new:0:DOCUMENT;new:1:LIST;append:0:1;"
                            f"setltype:1:{list_type};setldelim:1:{delim};"
                            f"setlstart:1:{start};setltight:1:{tight};"
                            "new:2:ITEM;append:1:2;new:3:PARAGRAPH;append:2:3;"
                            "new:4:TEXT;setlit:4:one;append:3:4;"
                            "new:5:ITEM;append:1:5;new:6:PARAGRAPH;append:5:6;"
                            "new:7:TEXT;setlit:7:two;append:6:7;"
                            "getall:1;" + self._render_all()
                        )
                        self.probe(
                            f"api/list/{list_type}-{delim}-{start}-{tight}",
                            "api-construct",
                            [
                                f"list-{list_type}-{delim}-{start}-{tight}",
                                "script",
                                script,
                            ],
                        )
        # Fenced code blocks with and without info strings.
        for info, label in (
            ("", "none"),
            ("rust", "lang"),
            ("c%20extra%20words", "attrs"),
            ("%3Cscript%3E", "markup"),
            ("caf%C3%A9", "utf8"),
        ):
            setter = f"setfence:1:{info};" if info else ""
            script = (
                "new:0:DOCUMENT;new:1:CODE_BLOCK;append:0:1;"
                + setter
                + "setlit:1:let%20x%20%3D%201%3B%0A;getall:1;"
                + self._render_all()
            )
            self.probe(
                f"api/fence/{label}",
                "api-construct",
                [f"fence-{label}", "script", script],
            )
        # Empty containers: no children at all, which parsers never emit.
        for node_type in self.ALL_TYPES:
            script = (
                f"new:0:DOCUMENT;new:1:{node_type};append:0:1;"
                "dump:0;check:0;" + self._render_all()
            )
            self.probe(
                f"api/empty/{node_type}",
                "api-empty",
                [f"empty-{node_type}", "script", script],
            )
        # Every node type as a document's sole child, carrying whatever text
        # setters it accepts -- the writers' full type dispatch.
        for node_type in self.ALL_TYPES:
            script = (
                f"new:0:DOCUMENT;new:1:{node_type};append:0:1;"
                "setlit:1:LIT;seturl:1:/U;settitle:1:T;setfence:1:F;"
                "setonenter:1:E;setonexit:1:X;sethead:1:2;"
                "setltype:1:ORDERED;setldelim:1:PAREN;setlstart:1:3;"
                "setltight:1:1;getall:1;" + self._render_all()
            )
            self.probe(
                f"api/allsetters/{node_type}",
                "api-setters",
                [f"allset-{node_type}", "script", script],
            )

    # A three-item list is the smallest tree with a middle, so every structural
    # mutation has a distinguishable before and after.
    THREE_ITEMS = (
        "parsetext:0:-%20one%0A-%20two%0A-%20three%0A:default;"
        "nav:1:0:first;nav:2:1:first;nav:3:2:next;nav:4:1:last;"
    )

    MUTATIONS: tuple[tuple[str, str], ...] = (
        ("unlink-first", "unlink:2;dump:0"),
        ("unlink-middle", "unlink:3;dump:0"),
        ("unlink-last", "unlink:4;dump:0"),
        ("unlink-list", "unlink:1;dump:0"),
        ("unlink-twice", "unlink:3;unlink:3;dump:0"),
        ("free-after-unlink", "unlink:3;free:3;dump:0;check:0"),
        ("insert-before-first", "new:5:ITEM;before:2:5;dump:0"),
        ("insert-before-middle", "new:5:ITEM;before:3:5;dump:0"),
        ("insert-after-last", "new:5:ITEM;after:4:5;dump:0"),
        ("insert-after-middle", "new:5:ITEM;after:3:5;dump:0"),
        ("replace-first", "new:5:ITEM;replace:2:5;dump:0;check:0"),
        ("replace-middle", "new:5:ITEM;replace:3:5;dump:0;check:0"),
        ("replace-last", "new:5:ITEM;replace:4:5;dump:0;check:0"),
        ("replace-list", "new:5:PARAGRAPH;replace:1:5;dump:0;check:0"),
        ("prepend-to-item", "new:5:PARAGRAPH;prepend:2:5;dump:0"),
        ("append-to-item", "new:5:PARAGRAPH;append:2:5;dump:0"),
        ("reparent-item", "unlink:4;append:2:4;dump:0;check:0"),
        ("move-to-front", "unlink:4;prepend:1:4;dump:0;check:0"),
        ("swap-neighbors", "unlink:2;after:3:2;dump:0;check:0"),
        ("self-append", "append:1:1;dump:0"),
        ("self-replace", "replace:1:1;dump:0"),
        ("append-ancestor", "append:2:1;dump:0"),
        ("insert-before-root", "new:5:ITEM;before:0:5;dump:0"),
        ("insert-after-root", "new:5:ITEM;after:0:5;dump:0"),
        ("append-document", "new:5:DOCUMENT;append:1:5;dump:0"),
        ("replace-with-document", "new:5:DOCUMENT;replace:3:5;dump:0"),
        ("nested-move", "nav:5:3:first;unlink:5;append:2:5;dump:0;check:0"),
        ("clear-list", "unlink:2;unlink:3;unlink:4;dump:0;check:0"),
    )

    def build_api_mutate(self) -> None:
        """Structural editing of a parsed tree.

        Each mutation is graded on its return code *and* on the resulting tree
        and rendered output.  That combination is what catches an implementation
        which reports success while leaving a dangling sibling pointer: the
        return code matches, the tree dump does not.  The refusals matter as
        much as the successes -- appending a node to its own descendant has to
        fail, or a consumer can build a cycle and hang.
        """
        for name, tail in self.MUTATIONS:
            script = self.THREE_ITEMS + tail + ";render:0:html;render:0:commonmark"
            self.probe(
                f"api/mutate/{name}",
                "api-mutate",
                [f"mut-{name}", "script", script],
            )
        # Mutations applied to real documents, so the operation meets trees the
        # generator would not produce.
        for name, tail in stride(list(self.MUTATIONS), 10):
            for index in stride(self.by_family["compose"], 6):
                doc = self.docs[index]
                script = (
                    f"parsedoc:0:{index}:default;"
                    "nav:1:0:first;nav:2:1:first;nav:3:2:next;nav:4:1:last;"
                    + tail
                    + ";render:0:html"
                )
                self.probe(
                    f"api/mutate/{name}/{doc['id']}",
                    "api-mutate-doc",
                    [f"mutd-{name}-{index}", "script", script],
                )

    LIFECYCLE: tuple[tuple[str, str], ...] = (
        ("free-detached", "new:0:PARAGRAPH;free:0;note:survived"),
        ("free-empty-doc", "new:0:DOCUMENT;free:0;note:survived"),
        ("free-tree-from-root", "new:0:DOCUMENT;new:1:PARAGRAPH;append:0:1;"
                               "new:2:TEXT;setlit:2:x;append:1:2;free:0;note:ok"),
        ("free-then-new", "new:0:DOCUMENT;free:0;new:0:DOCUMENT;"
                          "new:1:PARAGRAPH;append:0:1;render:0:html"),
        ("unlink-free-child", "new:0:DOCUMENT;new:1:PARAGRAPH;append:0:1;"
                              "unlink:1;free:1;dump:0;check:0"),
        ("build-after-partial-free",
         "new:0:DOCUMENT;new:1:PARAGRAPH;append:0:1;unlink:1;free:1;"
         "new:2:HEADING;sethead:2:1;append:0:2;new:3:TEXT;setlit:3:t;"
         "append:2:3;render:0:html;check:0"),
        ("replace-then-free-old",
         "new:0:DOCUMENT;new:1:PARAGRAPH;append:0:1;new:2:HEADING;"
         "sethead:2:2;replace:1:2;free:1;dump:0;check:0;render:0:html"),
        ("literal-overwrite",
         "new:0:TEXT;setlit:0:first;setlit:0:second;setlit:0:third;getall:0"),
        ("literal-to-empty", "new:0:TEXT;setlit:0:value;setlit:0:;getall:0"),
        ("url-overwrite",
         "new:0:LINK;seturl:0:/a;seturl:0:/b;settitle:0:t1;settitle:0:t2;getall:0"),
        ("fence-overwrite",
         "new:0:CODE_BLOCK;setfence:0:c;setfence:0:rust;setlit:0:x;getall:0"),
        ("custom-overwrite",
         "new:0:CUSTOM_BLOCK;setonenter:0:a;setonenter:0:b;setonexit:0:c;"
         "setonexit:0:d;getall:0;render:0:html"),
        ("userdata-roundtrip",
         "new:0:PARAGRAPH;setuserdata:0:1;setuserdata:0:2;setuserdata:0:-1"),
        ("consolidate-detached",
         "new:0:PARAGRAPH;new:1:TEXT;setlit:1:a;append:0:1;new:2:TEXT;"
         "setlit:2:b;append:0:2;consolidate:0;dump:0;render:0:html"),
        ("consolidate-empty", "new:0:DOCUMENT;consolidate:0;dump:0"),
        ("consolidate-twice",
         "parsetext:0:a%20b%20c:default;consolidate:0;consolidate:0;dump:0"),
        ("check-detached-leaf", "new:0:TEXT;setlit:0:x;check:0"),
        ("nav-off-ends",
         "new:0:DOCUMENT;nav:1:0:next;nav:2:0:prev;nav:3:0:parent;"
         "nav:4:0:first;nav:5:0:last;type:1;type:2;type:3;type:4;type:5"),
        ("nav-through-tree",
         "parsetext:0:%23%20h%0A%0Atext:default;nav:1:0:first;nav:2:1:next;"
         "nav:3:2:prev;nav:4:3:parent;type:1;type:2;type:3;type:4"),
        ("newmem-node",
         "memreset;newmem:0:DOCUMENT;newmem:1:PARAGRAPH;append:0:1;"
         "render:0:html;memstat;free:0;memstat"),
    )

    def build_api_lifecycle(self) -> None:
        """Ownership and mutation ordering.

        cmark's ownership rule is that freeing a node frees its subtree, so a
        node must be unlinked before it is freed independently.  Getting this
        wrong is the classic C-to-Rust hazard in both directions: a rewrite that
        adds ownership where the C had none double-frees, and one that leaks
        instead grows without bound.  Only defined sequences are graded --
        genuinely undefined ones (double free, use after free) would make the
        reference's own behavior arbitrary and are deliberately absent.
        """
        for name, script in self.LIFECYCLE:
            self.probe(
                f"api/lifecycle/{name}",
                "api-lifecycle",
                [f"life-{name}", "script", script],
            )

    def build_api_errors(self) -> None:
        """Defined failure behavior: NULL operands, wrong types, out-of-range.

        These are contract, not accident.  cmark's accessors are NULL-tolerant
        and its setters are type-checked, and callers depend on both -- passing
        an unchecked `cmark_node *` straight from a lookup is normal usage.
        """
        for group in (
            "null_getters",
            "null_nav",
            "null_setters",
            "null_structure",
            "null_iter",
            "type_errors",
            "bounds",
        ):
            self.probe(
                f"api/error/{group}",
                "api-errors",
                [f"sweep-{group}", "sweep", group],
                weight=2.0,
                note="whole-surface sweep; one workflow, many assertions",
            )
        # Setters against every wrong type individually, so a failure names the
        # type rather than the sweep.
        for node_type in self.ALL_TYPES:
            script = (
                f"new:0:{node_type};"
                "sethead:0:3;setltype:0:BULLET;setldelim:0:PERIOD;"
                "setlstart:0:2;setltight:0:1;setlit:0:L;seturl:0:U;"
                "settitle:0:T;setfence:0:F;setonenter:0:E;setonexit:0:X;"
                "getall:0"
            )
            self.probe(
                f"api/error/setters/{node_type}",
                "api-errors",
                [f"seterr-{node_type}", "script", script],
            )
        # Invalid enum values passed through the ABI.
        for bad in ("BAD",):
            script = (
                f"new:0:LIST;setltype:0:{bad};setldelim:0:{bad};getall:0;"
                "render:0:html;render:0:commonmark"
            )
            self.probe(
                f"api/error/enum/{bad}",
                "api-errors",
                [f"enumerr-{bad}", "script", script],
            )

    def build_abi_constants(self) -> None:
        """Values a caller compiled against the old header hands to the library.

        Option bits, enum ordinals and `sizeof(cmark_mem)` are all part of the
        binary interface.  A rewrite that renumbers an enum keeps every
        source-level test passing and breaks every already-compiled consumer,
        so these are graded through the compiled probe rather than by reading
        the header.
        """
        self.probe("abi/version", "abi-constants", ["abiver", "version"], weight=2.0)
        self.probe("abi/optbits", "abi-constants", ["abiopt", "optbits"], weight=2.0)
        self.probe("abi/enumbits", "abi-constants", ["abienum", "enumbits"], weight=2.0)

    # -- L. the installed executable ---------------------------------------

    def cli(
        self,
        case_id: str,
        argv: list[str],
        *,
        stdin_doc: int | None = None,
        files: list[int] | None = None,
        family: str = "cli",
        weight: float = 1.0,
        note: str = "",
    ) -> None:
        params = {"files": files} if files else None
        self.add(
            case_id,
            family,
            "cli",
            argv=argv,
            stdin_doc=stdin_doc,
            params=params,
            weight=weight,
            note=note,
        )

    def build_cli(self) -> None:
        """The installed `cmark` executable: the task's other release artifact.

        This is the interface distributors package and users script against, so
        stdout, stderr and exit status are all compared.  Getting the usage text
        or an exit code wrong breaks callers just as surely as getting the HTML
        wrong, and neither shows up in the library families.
        """
        # Informational output, byte for byte -- packaging and shell completions
        # depend on it.
        self.cli("cli/version-long", ["--version"], weight=2.0)
        self.cli("cli/help-long", ["--help"], weight=2.0)
        self.cli("cli/help-short", ["-h"], weight=2.0)

        # Default path: stdin to stdout, no flags.
        for index in stride(self.by_family["spec"], 30):
            doc = self.docs[index]
            self.cli(f"cli/default/{doc['id']}", [], stdin_doc=index)

        # Each writer selected through both spellings of the flag.
        for writer in self.WRITERS:
            for index in stride(self.by_family["block"], 8):
                doc = self.docs[index]
                self.cli(f"cli/to/{writer}/{doc['id']}", ["-t", writer],
                         stdin_doc=index)
            for index in stride(self.by_family["inline"], 4):
                doc = self.docs[index]
                self.cli(f"cli/to-long/{writer}/{doc['id']}", ["--to", writer],
                         stdin_doc=index)

        # Each option flag on inputs sensitive to it.
        for flag in (
            "--sourcepos",
            "--hardbreaks",
            "--nobreaks",
            "--smart",
            "--safe",
            "--unsafe",
            "--validate-utf8",
        ):
            for index in stride(self.by_family["inline"], 8):
                doc = self.docs[index]
                self.cli(f"cli/flag/{flag.strip('-')}/{doc['id']}", [flag],
                         stdin_doc=index)

        # Width, including the values that shape the wrap engine's edges.
        for width in ("0", "1", "10", "40", "72", "-1"):
            for index in stride(self.by_family["wrap"], 4):
                doc = self.docs[index]
                self.cli(
                    f"cli/width/{width}/{doc['id']}",
                    ["-t", "commonmark", "--width", width],
                    stdin_doc=index,
                )

        # Flag combinations, in both orders: option parsing is order-sensitive
        # in a hand-written loop.
        for combo in (
            ["--smart", "-t", "latex"],
            ["-t", "latex", "--smart"],
            ["--sourcepos", "-t", "xml"],
            ["--safe", "-t", "html"],
            ["--unsafe", "-t", "html"],
            ["--hardbreaks", "--nobreaks"],
            ["--nobreaks", "--hardbreaks"],
            ["--smart", "--sourcepos", "-t", "xml"],
            ["-t", "man", "--width", "50"],
            ["--width", "50", "-t", "man"],
            ["--validate-utf8", "--smart"],
            ["--safe", "--unsafe"],
            ["--unsafe", "--safe"],
        ):
            label = "+".join(part.strip("-") for part in combo)
            for index in stride(self.by_family["compose"], 3):
                doc = self.docs[index]
                self.cli(f"cli/combo/{label}/{doc['id']}", list(combo),
                         stdin_doc=index)

        # File arguments, including the documented multi-file concatenation.
        pick = stride(self.by_family["spec"], 6)
        for index in pick:
            doc = self.docs[index]
            self.cli(f"cli/file/{doc['id']}", ["@FILE0"], files=[index])
        self.cli("cli/files/two", ["@FILE0", "@FILE1"], files=pick[:2],
                 note="multiple inputs are concatenated before parsing")
        self.cli("cli/files/three", ["@FILE0", "@FILE1", "@FILE2"],
                 files=pick[:3])
        self.cli("cli/files/repeat", ["@FILE0", "@FILE0"], files=pick[:1])
        self.cli("cli/files/flag-between",
                 ["@FILE0", "-t", "xml", "@FILE1"], files=pick[:2])
        self.cli("cli/files/flag-after", ["@FILE0", "--smart"], files=pick[:1])

        # Failure paths: exit status and the exact diagnostic.
        self.cli("cli/err/unknown-flag", ["--nope"], weight=2.0)
        self.cli("cli/err/unknown-short", ["-z"], weight=2.0)
        self.cli("cli/err/bad-format", ["-t", "markdown"], weight=2.0)
        self.cli("cli/err/to-no-arg", ["-t"], weight=2.0)
        self.cli("cli/err/to-long-no-arg", ["--to"], weight=2.0)
        self.cli("cli/err/width-no-arg", ["--width"], weight=2.0)
        self.cli("cli/err/width-not-number", ["--width", "wide"], weight=2.0)
        self.cli("cli/err/width-trailing", ["--width", "40x"], weight=2.0)
        self.cli("cli/err/missing-file", ["/nonexistent/does-not-exist.md"],
                 weight=2.0)
        self.cli("cli/err/dir-as-file", ["/tmp"], weight=2.0)
        self.cli("cli/err/empty-arg", [""], weight=1.0)

        # Edge inputs on the default path.
        self.cli("cli/stdin/empty", [], stdin_doc=None,
                 note="closed stdin with no file arguments")
        for index in stride(self.by_family["rawbytes"], 10):
            doc = self.docs[index]
            self.cli(f"cli/raw/{doc['id']}", [], stdin_doc=index)
        for index in stride(self.by_family["pathological"], 8):
            doc = self.docs[index]
            self.cli(f"cli/path/{doc['id']}", [], stdin_doc=index)

    # -- M. structure: build, install, ABI, packaging -----------------------

    # (id, check, configs, weight, note)
    STRUCT_CASES: tuple[tuple[str, str, tuple[str, ...], float, str], ...] = (
        # Configure and build, both link modes.
        ("build/configure/shared", "configure", ("shared",), 3.0,
         "cmake configure with BUILD_SHARED_LIBS=ON"),
        ("build/configure/static", "configure", ("static",), 3.0,
         "cmake configure with BUILD_SHARED_LIBS=OFF"),
        ("build/compile/shared", "compile", ("shared",), 4.0,
         "build to completion with no compiler available for C"),
        ("build/compile/static", "compile", ("static",), 4.0, ""),
        ("build/install/shared", "install", ("shared",), 3.0, ""),
        ("build/install/static", "install", ("static",), 3.0, ""),
        ("build/warnings/shared", "build-warnings", ("shared",), 1.0,
         "build log free of hard errors and unresolved-symbol warnings"),
        ("build/insource-refused", "insource-refused", ("shared",), 1.0,
         "an in-source configure must still be refused"),
        ("build/reconfigure-clean", "reconfigure", ("shared",), 1.0,
         "a second configure in a used build dir must succeed"),
        ("build/rebuild-idempotent", "rebuild", ("shared",), 1.0,
         "an immediate second build must do nothing and stay green"),
        # Both link modes: they generate the same graph, and a submission that
        # only wired one of them correctly should be told which.
        ("build/parallel-safe", "parallel-safe", ("shared", "static"), 1.0,
         "no output is built by two targets that nothing orders"),
        ("build/version-reported", "cmake-version", ("shared",), 1.0,
         "project version reported by cmake is unchanged"),

        # Install inventory: the release artifacts.
        ("install/lib-soname", "inv-lib-shared", ("shared",), 3.0,
         "libcmark.so.0.31.1 plus its SONAME and dev symlinks"),
        ("install/lib-static", "inv-lib-static", ("static",), 3.0,
         "libcmark.a"),
        ("install/exe", "inv-exe", ("shared", "static"), 3.0, "bin/cmark"),
        ("install/header-cmark", "inv-header-cmark", ("shared", "static"), 2.0,
         "include/cmark.h"),
        ("install/header-export", "inv-header-export", ("shared", "static"), 2.0,
         "include/cmark_export.h, generated by generate_export_header"),
        ("install/header-version", "inv-header-version", ("shared", "static"), 2.0,
         "include/cmark_version.h with the pinned version macros"),
        ("install/manpages", "inv-man", ("shared",), 1.0,
         "share/man pages shipped by the release"),
        ("install/no-extra", "inv-no-extra", ("shared", "static"), 2.0,
         "no unexpected files added to the install tree"),
        ("install/no-source-leak", "inv-no-source-leak", ("shared", "static"), 2.0,
         "no .rs/.c/.o/.rlib artifacts installed"),
        ("install/prefix-clean", "inv-prefix", ("shared",), 1.0,
         "nothing installed outside the requested prefix"),

        # ABI of the shipped shared object.
        ("abi/soname", "abi-soname", ("shared",), 4.0, ""),
        ("abi/symbol-count", "abi-symbol-count", ("shared",), 4.0,
         "exactly the 70 exported cmark_* symbols"),
        ("abi/symbol-names", "abi-symbol-names", ("shared",), 4.0, ""),
        ("abi/no-extra-exports", "abi-no-extra-exports", ("shared",), 2.0,
         "default visibility stays hidden; no internals exported"),
        ("abi/symbol-type", "abi-symbol-type", ("shared",), 2.0,
         "every cmark_* export is a global function object"),
        ("abi/needed", "abi-needed", ("shared",), 2.0,
         "no dependency beyond the platform C runtime"),
        ("abi/no-undefined", "abi-no-undefined", ("shared",), 2.0,
         "no unresolved symbols outside the NEEDED closure"),
        ("abi/static-symbols", "abi-static-symbols", ("static",), 3.0,
         "the static archive defines the same public surface"),
        ("abi/elf-class", "abi-elf-class", ("shared",), 1.0, ""),
        ("abi/versioned-links", "abi-links", ("shared",), 2.0,
         "libcmark.so -> libcmark.so.0.31.1 development symlink chain"),

        # Package metadata consumed by downstreams.
        ("pkg/pc-exists", "pc-exists", ("shared", "static"), 2.0, "libcmark.pc"),
        ("pkg/pc-fields", "pc-fields", ("shared", "static"), 2.0,
         "Name/Description/Version/Libs/Cflags"),
        ("pkg/pc-query", "pc-query", ("shared", "static"), 3.0,
         "pkg-config --cflags --libs --modversion resolve"),
        ("pkg/cmake-config", "cmake-config", ("shared", "static"), 2.0,
         "cmarkConfig.cmake"),
        ("pkg/cmake-version-file", "cmake-version-file", ("shared", "static"), 2.0,
         "cmarkConfigVersion.cmake with SameMajorVersion policy"),
        ("pkg/cmake-targets", "cmake-targets", ("shared", "static"), 2.0,
         "the exported cmark-targets file"),
        ("pkg/cmake-namespace", "cmake-namespace", ("shared", "static"), 2.0,
         "the cmark::cmark imported target"),
        ("pkg/static-define", "static-define", ("static",), 2.0,
         "CMARK_STATIC_DEFINE reaches static consumers"),

        # Downstream consumers, built and run against the install tree.
        ("consumer/pkgconfig-shared", "consumer-pc", ("shared",), 4.0,
         "a C program built with pkg-config flags links and runs"),
        ("consumer/pkgconfig-static", "consumer-pc", ("static",), 4.0, ""),
        ("consumer/cmake-shared", "consumer-cmake", ("shared",), 4.0,
         "find_package(cmark) + cmark::cmark links and runs"),
        ("consumer/cmake-static", "consumer-cmake", ("static",), 4.0, ""),
        ("consumer/manual-flags", "consumer-manual", ("shared",), 2.0,
         "hand-written -I/-L/-lcmark still works"),
        ("consumer/version-guard", "consumer-version", ("shared", "static"), 2.0,
         "compile-time CMARK_VERSION checks compile"),
        ("consumer/probe-runs", "consumer-probe", ("shared", "static"), 4.0,
         "the differential probe itself links against the submission"),
    )

    def build_struct(self) -> None:
        for case_id, check, configs, weight, note in self.STRUCT_CASES:
            self.add(
                case_id,
                "structure",
                "struct",
                check=check,
                configs=configs,
                weight=weight,
                note=note,
            )

    # -- N. migration audit (anti-cheat) --------------------------------

    # These are gates, not score: the behavioural score is only awarded when the
    # mandatory ones all hold.  A submission that keeps the C around, shells out
    # to another implementation, or special-cases the evaluation has not done
    # the migration, however well it renders markdown.
    #
    # Every guard is declared here regardless of which stage answers it, because
    # this list is the single place the set is written down.  Which stage answers
    # which is `driver.py`'s `PROVENANCE_GATES` and `SEMANTIC_GATES`, and its
    # orphan check refuses to run if a guard declared here appears in neither --
    # otherwise dropping a guard from both lists would quietly stop it being asked
    # without anything failing.  The ones marked `[stage 1]` below are read off the
    # source by a scan and judged by a reviewer with both trees open; the rest are
    # measured here, against artifacts the build produced.
    GUARD_CASES: tuple[tuple[str, str, bool, str], ...] = (
        # G1 -- the old implementation has left the source closure.  All five are
        # claims about the submitted files, so all five are stage 1's.
        ("guard/no-c-sources", "no-c-sources", True,
         "[stage 1] no .c/.cc/.cpp/.m/.inc/.re anywhere in the repository"),
        ("guard/no-foreign-headers", "no-foreign-headers", True,
         "[stage 1] only the published cmark.h remains; no private C headers"),
        ("guard/no-c-in-build", "no-c-in-build", True,
         "[stage 1] the build declares no C/C++ sources and fetches nothing"),
        ("guard/rust-present", "rust-present", True,
         "[stage 1] a substantial Rust implementation exists in the repository"),
        ("guard/rust-is-primary", "rust-is-primary", True,
         "[stage 1] the library target's artifact comes from the Rust build"),

        # G2 -- no C toolchain participated in producing the artifacts.
        ("guard/compiler-shim-clean", "compiler-shim-clean", True,
         "the shimmed C/C++ compilers were never asked to compile"),
        ("guard/no-compile-syscalls", "no-compile-syscalls", True,
         "no cc1/cc1plus/as execution observed during the build"),
        ("guard/no-prebuilt-objects", "no-prebuilt-objects", True,
         "[stage 1] no .o/.a/.so/.rlib checked into the submission"),
        ("guard/no-embedded-reference", "no-embedded-reference", True,
         "[stage 1] no copy of the reference library or executable is shipped"),

        # G3 -- provenance of the shipped binaries.
        ("guard/elf-rust-provenance", "elf-rust-provenance", True,
         "the shipped objects carry Rust producer metadata"),
        ("guard/no-c-provenance", "no-c-provenance", True,
         "the shipped objects carry no C translation units"),
        ("guard/needed-whitelist", "needed-whitelist", True,
         "no dependency on an unexpected shared library"),
        ("guard/no-dlopen", "no-dlopen", True,
         "no dynamic loading of an outside implementation"),
        ("guard/no-exec-helpers", "no-exec-helpers", True,
         "the library and executable spawn no helper processes"),

        # G4 -- one implementation on one default path.
        ("guard/single-implementation", "single-implementation", True,
         "no second implementation kept behind a flag or feature"),
        ("guard/no-env-dispatch", "no-env-dispatch", True,
         "behavior does not change with environment variables"),
        ("guard/no-verifier-awareness", "no-verifier-awareness", True,
         "[stage 1] no branch keyed on test paths, case ids or the verifier"),
        ("guard/default-path", "default-path", True,
         "[stage 1] the graded artifacts are what a default build produces"),
        ("guard/no-network", "no-network", True,
         "[stage 1] the build performs no network access"),

        # G5 -- the release contract is intact.
        ("guard/version-unchanged", "version-unchanged", False,
         "the project still reports version 0.31.1"),
        ("guard/no-abi-widening", "no-abi-widening", False,
         "no new exported symbols beyond the pinned set"),
        ("guard/no-corpus-answers", "no-corpus-answers", True,
         "no hard-coded expected outputs keyed by input digest"),
    )

    def build_guards(self) -> None:
        for case_id, check, mandatory, note in self.GUARD_CASES:
            self.add(
                case_id,
                "audit",
                "guard",
                check=check,
                configs=("shared", "static"),
                weight=0.0,
                params={"mandatory": mandatory},
                note=note,
            )

    # -- assembly ----------------------------------------------------------

    def build_all(self) -> None:
        self.build_conformance()
        self.build_renderers()
        self.build_data_tables()
        self.build_encoding()
        self.build_pathological()
        self.build_wrapping()
        self.build_options()
        self.build_streaming()
        self.build_tree()
        self.build_iterators()
        self.build_memory()
        self.build_api_construct()
        self.build_api_mutate()
        self.build_api_lifecycle()
        self.build_api_errors()
        self.build_abi_constants()
        self.build_cli()
        self.build_struct()
        self.build_guards()


# The catalog is generated, so a mistake in a generator could silently shrink
# coverage.  These floors are asserted at verifier image build time: the image
# does not build if the catalog comes up short.
MIN_TOTAL_CASES = 800
MIN_BEHAVIOURAL_CASES = 600
MIN_GUARD_CASES = 20
MIN_STRUCT_CASES = 40


def build_catalog(corpus: dict) -> dict:
    catalog = Catalog(corpus)
    catalog.build_all()
    cases = catalog.cases

    by_kind: dict[str, int] = {}
    by_family: dict[str, int] = {}
    asserts = 0
    for case in cases:
        by_kind[case["kind"]] = by_kind.get(case["kind"], 0) + 1
        by_family[case["family"]] = by_family.get(case["family"], 0) + 1
        asserts += len(case.get("asserts", [1]))

    behavioural = by_kind.get("probe", 0) + by_kind.get("cli", 0)
    if len(cases) < MIN_TOTAL_CASES:
        raise SystemExit(f"catalog has {len(cases)} cases, floor is {MIN_TOTAL_CASES}")
    if behavioural < MIN_BEHAVIOURAL_CASES:
        raise SystemExit(f"only {behavioural} behavioural cases")
    if by_kind.get("guard", 0) < MIN_GUARD_CASES:
        raise SystemExit(f"only {by_kind.get('guard', 0)} guard cases")
    if by_kind.get("struct", 0) < MIN_STRUCT_CASES:
        raise SystemExit(f"only {by_kind.get('struct', 0)} structural cases")

    manifest = {
        "schema": "swerefactor-catalog-v1",
        "task": "lang01-cmark-c-to-rust",
        "corpus_digest": corpus["digest"],
        "counts": {
            "total": len(cases),
            "behavioural": behavioural,
            "assertions": asserts,
            "by_kind": by_kind,
            "by_family": by_family,
        },
        "cases": cases,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["digest"] = hashlib_sha256(payload)
    return manifest


def hashlib_sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    manifest = build_catalog(corpus)
    if args.out:
        args.out.write_text(
            json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    counts = manifest["counts"]
    print(f"cases: {counts['total']}  behavioural: {counts['behavioural']}"
          f"  assertions: {counts['assertions']}")
    for kind, count in sorted(counts["by_kind"].items()):
        print(f"  kind {kind:8s} {count:6d}")
    if args.summary:
        for family, count in sorted(counts["by_family"].items()):
            print(f"  family {family:22s} {count:6d}")
    print(f"catalog digest: {manifest['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
