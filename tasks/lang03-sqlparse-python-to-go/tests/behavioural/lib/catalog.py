#!/usr/bin/env python3
"""Case catalog for lang03-sqlparse-python-to-go.

Every behavioural case is a differential comparison.  The reference is the pinned
Python sqlparse 0.5.3 that ships inside the verifier image, driven through the
same operation table the submission's Go probes are driven through, and the two
answers are compared byte for byte.  Nothing here records an expected output by
hand, so the catalog cannot drift from what the reference actually does, and the
reference's quirks are graded as behavior rather than repaired as bugs.

Kinds:
  probe      one operation over the wire to a probe binary, both sides compared
  cli        an argv for the installed sqlformat, compared on stdout/stderr/status
  struct     a property of what the submission built: the module graph, the binary,
             the install inventory, whether an outside consumer compiles against the
             published API; the logic lives in build.py / structure.py
  provenance where the artifacts came from, and whether the answers were computed
             rather than recalled -- the build's own interpreter ledger, and SQL
             composed at grading time from a seed drawn then, which no recorded
             answer can match.  provenance.py

The first three are graded against something frozen into the image.  The fourth is
the exception, and it is why it exists: everything comparable against a frozen
expectation is also passable by a submission that stored those expectations, so one
family has to ask a question whose input did not exist when the image was built.

All four kinds are declared here, so this file is the single inventory of what this
stage measures.  What it does not measure is anything answerable by reading the
submission's source: no case here opens a .go file and compares what it finds
against a description.  Those questions belong to the audit stage, which reads
the tree with an agent instead of a pattern.

Tiers.  A probe case names the tier that answers it.  The four tiers are separate
Go main packages, each naming a different slice of the submission's exported API:

  core      root package ONLY          Format / Parse / Split / Version / Error
  model     + sql + tokens             the parse tree and the token lattice
  keywords  keywords + tokens ONLY     the 809-entry keyword tables
  parts     all nine packages          lexer / engine / filters / formatter / utils

An op's tier follows its imports, never the reverse.  `core` names the root
package and nothing else -- it reaches Parse's statements through a one-method
interface rather than importing sql to spell the type -- so `validate`, which
calls formatter.ValidateOptions, answers from `parts`, and `keyword`, which needs
the reference's keyword resolution order, answers from `keywords` via
keywords.Lookup rather than from the lexer.

The split exists so the report is diagnostic, and it buys two different things.

The `keywords` tier is genuinely isolated: the reference's keyword tables depend
on nothing but the token lattice, so that binary's whole compile closure is two
packages.  A submission whose lexer does not compile at all still answers every
`keywords` case -- and that family carries the heaviest per-case weight in the
suite, because ten cases cover all 809 entries exactly.

The other three are nested rather than disjoint: the root package necessarily
pulls the lexer, the engine and the filters, so `core`'s compile closure is the
whole module and `model` and `parts` add nothing to it.  What differs is the
exported surface each binary NAMES.  `core` compiles against Parse, Format,
Split and Version alone; `model` additionally names ~30 accessors on sql.Node
and the tokens lattice; `parts` names the five lower-level packages.  A port in
progress fails these in order -- the top-level API lands first and the node
accessors last -- so a submission that has the entry points working and the tree
accessors missing passes `core` and fails `model`, which is the shape the tiers
are there to distinguish.

If one binary answered everything, a single missing accessor would report as a
whole-suite failure, and the report would stop saying how far the port got.  What
it reports and what it is paid are separate questions: the stage pays only for a
submission that passed every scored check, and the tiers are what turn that one
verdict into a diagnosis.

Weights.  Families that ride the docs produce hundreds of cases from one code
path, and families with a handful of cases can cover a whole package.  Raw case
counts would therefore hand ~20% of the score to whichever family happens to be
docs-shaped.  Each family carries a per-case weight chosen so that no single
family exceeds roughly a sixth of the behavioural weight; the weights are
published in the manifest and re-asserted by check-frozen.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import spec

TASK = "lang03-sqlparse-python-to-go"

# Published floors.  build_catalog aborts the verifier image build if coverage
# regressed below any of these, and check-frozen.py re-asserts them against
# the manifest that actually shipped.
MIN_TOTAL_CASES = 2000
MIN_BEHAVIOURAL_CASES = 1800
MIN_STRUCT_CASES = 30
MIN_CLI_CASES = 50
# The provenance kind is small, and one of its two families is the only thing in
# this stage graded without a frozen expectation.  A floor rather than an equality
# because DECLARED_SHAPE already checks the exact count; what this catches is that
# family disappearing while the totals still look plausible.
MIN_PROVENANCE_CASES = 20
MIN_TIER_CASES = {"core": 400, "model": 400, "keywords": 200, "parts": 200}

# The exact shape this catalog is expected to have, checked in both directions.
#
# The floors above only catch collapse: a catalog that lost half its probe cases
# to a docs builder regression still clears 2000, and one that gained cases
# because a family was accidentally emitted twice clears everything. Neither shows
# up in a floor, and both change what every submission is scored against.
#
# An exact count is only a fair thing to declare because it is deterministic:
# the baseline is a pinned tarball, the docs builder and the case builders are
# version-controlled, and the catalog already carries a digest over the result.
# Given the same inputs the numbers cannot move on their own, so if they move,
# something was edited -- and then the edit should be visible in a diff of this
# constant, reviewed alongside whatever caused it. Updating these numbers is a
# normal part of adding a case; discovering a stale one at grading time is not.
#
# These counts and the docs digest do not move together, which is why the digest is
# checked separately. Seven families emit one case per document, so adding a
# document adds seven cases; the api/lex/split families draw a fixed-size sample
# instead, and so change *which* documents they cover without changing how many. A
# resample can therefore leave every number here untouched while several hundred
# case ids appear and the same number disappear -- invisible in this constant, and
# visible in the digest.
#
# Measured: built from the pinned tarball, printed by build_catalog, pasted here.
DECLARED_SHAPE = {
    "total": 8526,
    "families": 26,
    "by_kind": {"probe": 8326, "cli": 127, "struct": 32, "provenance": 41},
}

# Per-case weights.  See the module docstring.
WEIGHTS = {
    "roundtrip": 0.25,
    "split": 0.40,
    "splitstream": 0.60,
    "format": 0.25,
    "format-wide": 0.35,
    "validate": 1.00,
    "encoding": 1.50,
    "error-type": 1.00,
    "tokenize": 0.60,
    "tree": 0.60,
    "stmt": 0.45,
    "api": 0.80,
    "ttype": 1.50,
    "keyword": 0.30,
    "kwtable": 2.00,
    "kwdialect": 1.00,
    "lex": 0.45,
    "filter": 0.50,
    "stack": 0.60,
    "util": 0.80,
    "lexstate": 2.00,
    "optkeys": 2.00,
    "cli": 2.00,
}


def stride(items: list, count: int) -> list:
    """Evenly spaced deterministic subset, order preserved.

    Stride rather than sampling: reproducible without a PRNG, obvious from the
    inputs, and it spreads coverage across a family instead of clustering at the
    front where the small hand-written documents live.
    """
    if count >= len(items):
        return list(items)
    if count <= 0:
        return []
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


class Catalog:
    def __init__(self, docs: dict, keywords: dict) -> None:
        self.docs = docs["documents"]
        self.doc_ids = [d["id"] for d in self.docs]
        self.by_id = {d["id"]: d for d in self.docs}
        self.utf8_ids = [d["id"] for d in self.docs if d["utf8"]]
        self.non_utf8_ids = [d["id"] for d in self.docs if not d["utf8"]]
        self.declared_encoding_ids = [
            d["id"] for d in self.docs if d["encoding"]
        ]
        # Documents the probe itself can turn into a Python str, for the four ops
        # in STR_PATH_OPS that have no bytes-taking form.  Every other op takes
        # bytes and decodes internally, so it accepts every document.
        #
        # A document with a declared encoding decodes with it; one without has to
        # be valid UTF-8.  The only other way to get a str out of undeclared
        # non-UTF-8 bytes is surrogateescape, whose lone surrogates have no Go
        # representation, so the pair would grade a Python-only rendering.
        self.text_ids = [
            d["id"] for d in self.docs if d["utf8"] or d["encoding"]
        ]
        self._text_id_set = set(self.text_ids)
        self.by_group: dict[str, list[str]] = {}
        for d in self.docs:
            self.by_group.setdefault(d["group"], []).append(d["id"])
        # keywords maps table name -> sorted list of words, read out of the
        # reference package at freeze time.
        self.keywords = keywords
        self.cases: list[dict] = []
        self._ids: set[str] = set()

    # -- registration -----------------------------------------------------

    def add(
        self,
        case_id: str,
        family: str,
        kind: str,
        *,
        tier: str = "",
        op: str = "",
        args: list[str] | None = None,
        argv: list[str] | None = None,
        stdin_doc: str | None = None,
        file_doc: str | None = None,
        grade: str = "full",
        check: str = "",
        params: dict | None = None,
        weight: float | None = None,
        note: str = "",
    ) -> None:
        if case_id in self._ids:
            raise SystemExit(f"duplicate case id: {case_id}")
        self._ids.add(case_id)
        if weight is None:
            weight = WEIGHTS.get(family, 1.0)
        case = {
            "id": case_id,
            "family": family,
            "kind": kind,
            "weight": weight,
        }
        if tier:
            case["tier"] = tier
        if op:
            case["op"] = op
        if args is not None:
            case["args"] = args
        if argv is not None:
            case["argv"] = argv
        if stdin_doc is not None:
            case["stdin_doc"] = stdin_doc
        if file_doc is not None:
            case["file_doc"] = file_doc
        if grade != "full":
            case["grade"] = grade
        if check:
            case["check"] = check
        if params:
            case["params"] = params
        if note:
            case["note"] = note
        self.cases.append(case)

    # Ops that hand the library a Python str rather than bytes, and so can only
    # be given a document the probe itself can decode.  Everything else passes
    # bytes through and lets the implementation do its own decoding.
    #
    #   parsestream  needs a text stream; there is no bytes form of the entry point
    #   api          walks the tree of a str parse
    #   filter/stack drive the filter pipeline from a str
    #
    # A document that is neither valid UTF-8 nor carrying a declared encoding can
    # only become a str through Python's surrogateescape, which produces lone
    # surrogates that Go has no representation for -- so the pair would be
    # grading a Python-only rendering.  Those documents are covered instead by
    # the bytes-taking ops, where the library's own fallback is the graded
    # behavior.
    STR_PATH_OPS = frozenset({"parsestream", "api", "filter", "stack"})

    def probe(self, case_id: str, family: str, tier: str, op: str,
              args: list[str], **kw) -> None:
        if op in self.STR_PATH_OPS:
            for arg in args:
                if arg[:1] in "de" and arg[2:] not in self._text_id_set:
                    raise SystemExit(
                        f"catalog: case {case_id} gives {op} the document "
                        f"{arg[2:]}, which is not valid UTF-8 and declares no "
                        f"encoding; {op} takes a str, so there is no rendering "
                        f"of it that both halves of the pair can produce"
                    )
        self.add(case_id, family, "probe", tier=tier, op=op, args=args, **kw)

    # -- argument encoding ------------------------------------------------
    # The wire is one line per case: id \t op \t arg \t arg ...  Every argument
    # carries a one-letter type tag so both probes reconstruct the same value:
    #
    #   d:ID   the docs document ID, read as bytes from the docs directory
    #   e:ID   the same document, plus its declared encoding as a second value
    #   s:TEXT an escaped literal string
    #   i:N    an integer
    #   b:0|1  a boolean
    #   n:NAME a name to look up in spec.json (a preset, filter, stack, ...)

    @staticmethod
    def d(doc_id: str) -> str:
        return f"d:{doc_id}"

    def e(self, doc_id: str) -> str:
        """A document plus its declared encoding.

        The op hands both to the library as bytes: with the declared encoding
        when there is one, and without when there is not, in which case the
        library applies its own documented utf-8-then-unicode-escape fallback.
        No restriction applies here -- any document can go through this tag,
        including one that is not valid UTF-8, because the decode happens inside
        the implementation under test rather than in the probe.  The ops that
        genuinely need a Python str are constrained by `probe()` instead.
        """
        return f"e:{doc_id}"

    @staticmethod
    def s(text: str) -> str:
        out = []
        for ch in text:
            if ch == "\\":
                out.append("\\\\")
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\t":
                out.append("\\t")
            elif ch == "\r":
                out.append("\\r")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                out.append(f"\\x{ord(ch):02x}")
            else:
                out.append(ch)
        return "s:" + "".join(out)

    @staticmethod
    def slug(text: str) -> str:
        """A case ID fragment for an arbitrary string.

        Case IDs are keys in the manifest and in every report, so they have to
        stay on one line and survive a file name.  The encoding names being
        graded include spaces, tabs, a semicolon and the empty string, and two of
        them differ only in case -- so the mapping has to be injective over the
        set actually used, which is why the case is folded into a marker rather
        than discarded.
        """
        out = []
        for ch in text:
            if ch.isalnum() and (ch.islower() or ch.isdigit()):
                out.append(ch)
            elif ch.isalnum():
                out.append("A" + ch.lower())
            elif ch in "-_.":
                out.append(ch)
            else:
                out.append(f"x{ord(ch):02x}")
        return "".join(out) or "empty"

    @staticmethod
    def i(value: int) -> str:
        return f"i:{value}"

    @staticmethod
    def b(value: bool) -> str:
        return f"b:{1 if value else 0}"

    @staticmethod
    def n(name: str) -> str:
        return f"n:{name}"

    # ------------------------------------------------------------------
    # A. core tier -- the root package
    # ------------------------------------------------------------------
    # Everything a caller who writes `import sqlparse` touches.  This tier
    # imports only the submission's root package, so it compiles even if the
    # parse tree and the filters are still stubs -- which is exactly the line
    # the tier split exists to draw in the report.

    def build_core(self) -> None:
        # A1. Round trip.  sqlparse's headline invariant: str(parse(sql)[i])
        # concatenated over all statements reproduces the input exactly, for
        # every input, including inputs that are not valid SQL.  Every document
        # in the docs is a case because this is the one property that must hold
        # universally, and a port that loses a single character anywhere -- a
        # dropped trailing newline, a normalized tab, a swallowed NUL -- fails
        # here and nowhere else.
        for doc_id in self.doc_ids:
            self.probe(f"rt-{doc_id}", "roundtrip", "core", "rt",
                       [self.e(doc_id)])

        # A2. Split.  The statement splitter, reported as a count plus each
        # statement's bytes.  Independent of grouping, so it belongs in core.
        for doc_id in self.doc_ids:
            self.probe(f"sp-{doc_id}", "split", "core", "split",
                       [self.e(doc_id)])

        # A3. Split with strip_semicolon, and the streaming reader.  Both are
        # separate published entry points with their own quirks.
        for doc_id in stride(self.utf8_ids, 90):
            self.probe(f"sps-{doc_id}", "split", "core", "split-strip",
                       [self.d(doc_id)])
        for doc_id in stride(self.by_group.get("written/multi", [])
                             + self.by_group.get("sample", []), 40):
            self.probe(f"pst-{doc_id}", "splitstream", "core", "parsestream",
                       [self.e(doc_id)])

        # A4. The format option matrix.  Every preset over every format target:
        # the presets are the option space and the targets are the shapes the
        # reindent and aligned-indent filters do something structural to.  Each
        # case is one preset applied to one document, so a submission that got
        # `comma_first` wrong fails 36 nameable cases rather than one opaque
        # one -- the matrix is built to localise a fault, not to price it.
        targets = self.by_group.get("written/format", [])
        if len(targets) < 30:
            raise SystemExit(f"only {len(targets)} format targets in the docs")
        for preset in sorted(spec.FORMAT_PRESETS):
            for doc_id in targets:
                self.probe(f"fmt-{preset}-{doc_id}", "format", "core", "fmt",
                           [self.n(preset), self.e(doc_id)])

        # A5. Format across the whole docs under the two presets that reach the
        # most code.  The matrix above is deep on 36 documents; this is shallow
        # on every document a str entry point can receive, and it is where the
        # encoding path, the pathological inputs and the malformed inputs meet
        # the formatter.
        for preset in ("pretty", "aligned"):
            for doc_id in self.doc_ids:
                self.probe(f"fw-{preset}-{doc_id}", "format-wide", "core", "fmt",
                           [self.n(preset), self.e(doc_id)])

        # A6. Encoding.  Twenty documents are not valid UTF-8 and nine declare an
        # encoding, which is 24 distinct documents once the overlap is counted.
        # Each is driven four ways, because the reference's decode-then-fall-back
        # logic sits at a different place in each entry point.
        enc_ids = sorted(set(self.non_utf8_ids) | set(self.declared_encoding_ids))
        for doc_id in enc_ids:
            # All four drives for every one of them.  Each passes the bytes to
            # the library -- with the declared encoding where there is one, and
            # without where there is not, which is the fallback path -- so an
            # undeclared non-UTF-8 document is as gradeable here as any other.
            for op, suffix in (("rt", "rt"), ("split", "split"),
                               ("fmt-identity", "fmt"), ("tostr", "str")):
                if op == "fmt-identity":
                    self.probe(f"enc-{suffix}-{doc_id}", "encoding", "core",
                               "fmt", [self.n("identity"), self.e(doc_id)])
                else:
                    self.probe(f"enc-{suffix}-{doc_id}", "encoding", "core",
                               op, [self.e(doc_id)])
            # And the same bytes with no declared encoding, which is the
            # fall-back path rather than the decode path.
            self.probe(f"enc-nodecl-{doc_id}", "encoding", "core", "rt-raw",
                       [self.d(doc_id)])

        # A6b. The published encoding gate, driven by name rather than by
        # declaration.  A6 covers the encodings the docs declares -- cp1251,
        # utf-8, utf-16 and the no-declaration fallback -- and leaves five of the
        # eight accepted encodings never applied to bytes and 33 of the 37
        # published aliases never named.  Both are contract, and an unexercised
        # contract line is one a port can skip for free.
        #
        # Four documents, chosen because the eight canonical codecs disagree on
        # them.  Together they separate every pair:
        #
        #   by-latin1-high   high bytes, odd length.  latin-1 and cp1251 decode
        #                    to different text; ascii and utf-8 reject; the three
        #                    utf-16 codecs reject on the odd length.
        #   by-utf16         BOM then little-endian.  utf-16 consumes the BOM,
        #                    utf-16-le keeps it as U+FEFF, utf-16-be reads it
        #                    byte-swapped.
        #   by-utf8-bom      the only document carrying a utf-8 BOM, and the only
        #                    thing that separates utf-8 from utf-8-sig: the sig
        #                    codec strips it, plain utf-8 yields U+FEFF as text.
        #                    Nothing else in the suite grades that difference.
        #   by-mixed-valid   valid utf-8 with one two-byte character, so the
        #                    single-byte codecs produce mojibake rather than an
        #                    error and a port cannot pass by erroring everywhere.
        for name in sorted(spec.ENCODING_ALIASES):
            for doc_id in ("by-latin1-high", "by-utf16", "by-utf8-bom",
                           "by-mixed-valid"):
                self.probe(f"enc-alias-{self.slug(name)}-{doc_id}", "encoding",
                           "core", "rt-enc", [self.d(doc_id), self.s(name)])

        # A6c. Normalization, and the gate's edges.  The alias table is consulted
        # after strip-and-lower, so a name that differs only in case or
        # surrounding space resolves; one that differs inside does not.  The
        # rejected names are the point of having a declared set at all: CPython
        # would decode every one of them, and a stdlib-only Go port has a table
        # for none, so a reference that accepted them would grade CPython's codec
        # registry instead of the contract.
        # Paired with a document the resolved codec can decode, so the answer
        # names the canonical it resolved to.  On bytes the codec rejects, the
        # answer would be `undecodable` -- which does prove the name resolved,
        # since an unresolved name is `unknown-encoding`, but it does not say
        # what it resolved to, and that is the half of the contract this block is
        # for.  The ascii and utf-16 names therefore get documents of their own.
        for name, doc_id in (
            ("UTF-8", "by-mixed-valid"),
            ("  utf-8  ", "by-mixed-valid"),
            ("\tLATIN1\n", "by-mixed-valid"),
            ("Windows-1251", "by-mixed-valid"),
            ("UTF_16_LE", "by-mixed-valid"),
            ("Utf-8-Sig", "by-utf8-bom"),
            ("us-ASCII", "ft-simple"),
            ("US_ASCII", "ft-simple"),
            ("  646\t", "ft-simple"),
            ("UTF-16", "by-utf16"),
            ("Utf16-BE", "by-utf16"),
            ("  L1  ", "by-latin1-high"),
            ("1251", "by-cp1251"),
        ):
            self.probe(f"enc-norm-{self.slug(name)}", "encoding", "core",
                       "rt-enc", [self.d(doc_id), self.s(name)])
        for name in ("gbk", "koi8-r", "big5", "shift_jis", "cp1252", "utf-32",
                     "iso-8859-5", "mac_roman", "utf 8", "utf-8 ;", "",
                     "not-an-encoding", "utf-9", "latin-2", "cp1251x"):
            self.probe(f"enc-reject-{self.slug(name)}", "encoding", "core",
                       "rt-enc", [self.d("by-mixed-valid"), self.s(name)])

        # A7. The error type.  sqlparse raises exactly one exception type from
        # exactly one place, and the port has to keep that surface: a
        # non-string, non-bytes argument is a type error, and an unknown
        # encoding is a lookup error.  Graded on the message.
        for name, args in (
            ("unknown-encoding", [self.d("ft-simple"), self.s("not-an-encoding")]),
            ("empty-encoding", [self.d("ft-simple"), self.s("")]),
            ("bogus-encoding-utf9", [self.d("ft-simple"), self.s("utf-9")]),
            ("encoding-ascii-on-unicode",
             [self.d("ft-unicode"), self.s("ascii")]),
            ("encoding-latin1-on-unicode",
             [self.d("ft-unicode"), self.s("latin-1")]),
            ("encoding-utf8-on-invalid",
             [self.d("by-invalid-utf8"), self.s("utf-8")]),
            ("encoding-utf8-on-truncated",
             [self.d("by-truncated-utf8"), self.s("utf-8")]),
            ("encoding-cp1251-on-gbk", [self.d("by-gbk"), self.s("cp1251")]),
        ):
            self.probe(f"err-{name}", "error-type", "core", "err-encoding", args)
        self.probe("err-version", "error-type", "core", "version", [])

    # ------------------------------------------------------------------
    # B. model tier -- the parse tree and the token lattice
    # ------------------------------------------------------------------

    def build_model(self) -> None:
        # B1. The flat token stream after grouping, one token per line as
        # (rendered type, escaped value).  This is the single densest signal in
        # the suite: it pins the lexer's regex ordering, the keyword resolution,
        # the whitespace handling and every group the grouper builds, for every
        # document.
        for doc_id in self.doc_ids:
            self.probe(f"tok-{doc_id}", "tokenize", "model", "tok",
                       [self.e(doc_id)])

        # B2. The tree, rendered with one line per node carrying depth, kind,
        # type and value.  tok says which tokens exist; tree says how they nest,
        # which is the grouper's whole job and the part of a port most likely to
        # be subtly wrong.
        for doc_id in self.doc_ids:
            self.probe(f"tr-{doc_id}", "tree", "model", "tree",
                       [self.e(doc_id)])

        # B3. Per-statement derived accessors: get_type, is_group, token count,
        # names, aliases.  Cheap, broad, and a different code path from the tree
        # rendering -- these are the methods a downstream caller actually reaches
        # for, and they are computed from the tree rather than read off it.
        for doc_id in self.doc_ids:
            self.probe(f"st-{doc_id}", "stmt", "model", "stmt",
                       [self.e(doc_id)])

        # B4. The Node method battery, applied at fixed addresses inside the
        # tree: the statement, its first children, its first sublists and its
        # first flat tokens.  ~30 methods per node over ~8 nodes per document.
        # Strided rather than exhaustive because the payload is large and the
        # marginal document adds little once the shapes repeat.
        for doc_id in stride(self.utf8_ids, 260):
            self.probe(f"api-{doc_id}", "api", "model", "api", [self.d(doc_id)])

        # B5. The token type lattice, resolved by rendered name.  Each case
        # reports the type's rendering, its parent chain, and its containment
        # relation against all 40 probe names -- a 40-wide relation per case.
        # This is where a hand-rolled Go type hierarchy diverges from Python's
        # _TokenType.__contains__, and it is graded independently of parsing so a
        # broken lexer does not hide a broken lattice.
        for name in spec.TTYPE_NAMES:
            slug = name.replace(".", "-").lower()
            self.probe(f"tt-{slug}", "ttype", "model", "ttype",
                       [self.s(name)])

    # ------------------------------------------------------------------
    # C. keywords tier -- the 809-entry tables
    # ------------------------------------------------------------------

    def build_keywords(self) -> None:
        # C1. Whole-table dumps.  Nine tables, every entry, sorted, as
        # (word, rendered type).  These nine cases cover all 809 entries
        # exactly, which is why they carry the heaviest weight in the suite: a
        # single mistyped ttype anywhere in the port fails the table that
        # contains it.
        for table in sorted(self.keywords):
            self.probe(f"kwt-{table}", "kwtable", "keywords", "kwtable",
                       [self.s(table)])
        # And the set of table names itself: which dialects exist is part of the
        # published surface, and a port that silently merged them all into one
        # table passes every lookup and fails this.
        self.probe("kwt-names", "kwtable", "keywords", "kwnames", [])

        # C2. Individual lookups.  The table dump proves the data is right; a
        # lookup proves the resolution order is right, because the reference
        # consults the dialect tables in a fixed sequence and the same word can
        # appear in several of them. Strided over each table so every table
        # contributes, plus case variants because the reference upper-cases
        # before looking up.
        for table in sorted(self.keywords):
            words = self.keywords[table]
            for word in stride(words, min(40, len(words))):
                slug = _slug(word)
                self.probe(f"kw-{table}-{slug}", "keyword", "keywords",
                           "keyword", [self.s(word)])
        for word in ("select", "SELECT", "SeLeCt", "sElEcT", "select ",
                     " select", "sel", "selects", "select\t", "insert",
                     "InSeRt", "create", "CREATE", "with", "WITH", "as",
                     "AS", "cast", "CAST", "int", "INT", "varchar",
                     "VARCHAR", "row_number", "ROW_NUMBER", "not",
                     "NOT", "in", "IN", "like", "LIKE", "between",
                     "BETWEEN", "and", "AND", "or", "OR", "is", "IS",
                     "null", "NULL", "true", "TRUE"):
            self.probe(f"kwc-{_slug(word)}", "keyword", "keywords",
                       "keyword", [self.s(word)])
        # Words that must not resolve: the negative half of the table is as much
        # of the contract as the positive half, and a port that over-matches
        # (say, by treating every all-caps identifier as a keyword) only fails
        # here.
        for word in ("", " ", "\t", "\n", "zzz", "nonexistent_keyword",
                     "my_table", "customer_id", "a", "x1", "_private",
                     "SELECTED", "SELECTING", "FROMAGE", "WHEREVER",
                     "1", "1select", "select1", "s-e-l-e-c-t", "sélect",
                     "中文", "sel ect", "'select'", '"select"', "`select`",
                     "[select]", "select;", "select(", "--select", "/*select*/"):
            self.probe(f"kwn-{_slug(word) or 'empty'}", "keyword", "keywords",
                       "keyword", [self.s(word)])

        # C3. Dialect tokenization.  The keyword tables only matter through the
        # lexer, and the dialect documents are the ones whose meaning changes
        # depending on which tables were loaded.
        #
        # This family answers from the `parts` tier, not from `keywords`: the
        # keywords tier's import closure has no lexer, so it cannot tokenize
        # anything.  Keeping the family separate anyway is deliberate -- when
        # kwtable passes and kwdialect fails, the tables are right and the
        # lexer's use of them is wrong, which is a different bug in a different
        # file from a wrong table.
        dialect_ids = (self.by_group.get("written/dialect", [])
                       + self.by_group.get("written/keywords", [])
                       + self.by_group.get("written/plsql", []))
        for doc_id in dialect_ids:
            self.probe(f"kwd-{doc_id}", "kwdialect", "parts", "kwlex",
                       [self.d(doc_id)])

    # ------------------------------------------------------------------
    # D. parts tier -- lexer, engine, filters, formatter, utils
    # ------------------------------------------------------------------

    def build_parts(self) -> None:
        # D1. The lexer's raw stream, before any grouping.  Isolates regex
        # ordering from the grouper: when tok and lex disagree about the same
        # document, the failure is in the grouper, and when they agree and both
        # differ from the reference, it is in the lexer.  That distinction is
        # what makes the report actionable.
        # The stride is for breadth; the lexical groups are mandatory.  A 1-in-300
        # sample over 696 documents is a good way to cover the docs and a bad way
        # to cover a *rule*: `id-dot-no-space` ("select a .b from t") is the only
        # document that distinguishes the lexer's `[A-ZÀ-Ü]\w*(?=\s*\.)` pattern
        # from one without the `\s*`, and the stride skipped it.  Mutating that
        # pattern was still caught -- by tokenize, tree, stmt and format-wide -- so
        # the behavior was graded; what was missing was the family that exists to
        # say the failure is in the lexer and not the grouper.  A document whose
        # whole purpose is a lexical boundary belongs in the family that isolates
        # lexical boundaries, rather than in it by luck.
        lexical = ("written/identifiers", "written/numbers", "written/keywords",
                   "written/operators", "written/strings", "written/whitespace")
        mandatory = sorted({doc_id for group in lexical
                            for doc_id in self.by_group.get(group, [])})
        missing = [g for g in lexical if not self.by_group.get(g)]
        if missing:
            raise SystemExit(
                f"lex family: docs groups {missing} are empty, so the "
                f"mandatory lexical documents would silently be no documents")
        for doc_id in sorted(set(stride(self.doc_ids, 300)) | set(mandatory)):
            self.probe(f"lx-{doc_id}", "lex", "parts", "lex", [self.e(doc_id)])

        # D2. Individual filters, each installed alone at its own pipeline
        # stage.  A submission can implement every filter correctly and still
        # compose them wrongly; grading them one at a time separates the two.
        filter_docs = stride(self.by_group.get("written/format", []), 8)
        for name in sorted(spec.FILTERS):
            for doc_id in filter_docs:
                self.probe(f"flt-{name}-{doc_id}", "filter", "parts", "filter",
                           [self.n(name), self.d(doc_id)])

        # D3. Whole stacks.  The composition itself, including grouping and
        # strip_semicolon, which are stack settings rather than filters.
        stack_docs = stride(self.by_group.get("written/format", [])
                            + self.by_group.get("written/multi", []), 10)
        for name in sorted(spec.STACKS):
            for doc_id in stack_docs:
                self.probe(f"stk-{name}-{doc_id}", "stack", "parts", "stack",
                           [self.n(name), self.d(doc_id)])

        # D4. utils.  Small, pure, and the place where Python's str-of-code-
        # points and Go's string-of-bytes diverge most quietly.
        for idx, text in enumerate(spec.REMOVE_QUOTES_INPUTS):
            self.probe(f"u-rq-{idx:02d}", "util", "parts", "remove-quotes",
                       [self.s(text)])
        # `util` stops at the parts the contract exports.  Reindent's column
        # arithmetic is not probed directly: isolating it would need the
        # contract to export `utils.LastLineWidth`, and the reference's version
        # is a private method on the filter, so the export would be API invented
        # for the grader.  The two wrong implementations that would matter --
        # byte length, and splitting on \n alone -- change no byte of public
        # output on any input found, including all 672 docs documents under five
        # reindent option sets, because reindent inserts its newline before
        # measuring.  What the arithmetic feeds is covered by fmt and filter.
        for idx, text in enumerate(spec.SPLIT_UNQUOTED_NEWLINES_INPUTS):
            self.probe(f"u-sun-{idx:02d}", "util", "parts",
                       "split-unquoted-newlines", [self.s(text)])

        # D5. The formatter's declared option surface, and the lexer's mutable
        # keyword state.  The lexer is a process-wide singleton in the
        # reference, with an add/clear/reinitialize protocol that observably
        # changes tokenization; each script drives that protocol and reports the
        # stream after each step.
        self.probe("opt-keys", "optkeys", "parts", "optkeys", [])
        self.probe("opt-keys-sorted", "optkeys", "parts", "optkeys-sorted", [])
        for script in sorted(spec.LEXSTATE_SCRIPTS):
            self.probe(f"lexstate-{script}", "lexstate", "parts", "lexstate",
                       [self.n(script)])

        # D6. validate_options.  The invalid cases are graded on the error text
        # verbatim: those strings are the reference's API surface and a caller
        # catches them.  The valid cases are graded on the absence of an error --
        # the normalized map itself is full of Python objects with no portable
        # rendering, which is recorded as not-graded in the contract.
        #
        # In `parts` rather than `core` because both halves of the probe call the
        # validator directly, and the validator lives in the formatter package.
        # Reaching these messages through sqlparse.Format instead would have kept
        # the family in core, at the cost of grading them only where the whole
        # pipeline already works -- and the point of a rejection message is that a
        # caller sees it before anything is parsed.
        for name in sorted(spec.VALIDATE_CASES):
            self.probe(f"val-{name}", "validate", "parts", "validate",
                       [self.n(name)])

    # ------------------------------------------------------------------
    # E. the installed executable
    # ------------------------------------------------------------------

    def build_cli(self) -> None:
        # The only artifact a user runs.  Every case is graded on exit status
        # plus whichever streams the spec says are contractual: argparse's own
        # help layout is not a portable contract, so those cases grade the
        # status and the shape, while the formatting cases grade stdout
        # verbatim.
        for name in sorted(spec.CLI_CASES):
            entry = spec.CLI_CASES[name]
            doc = entry.get("doc")
            argv = list(entry["argv"])
            kwargs = {"grade": entry.get("grade", "full")}
            if doc is not None:
                if any("{doc}" in a for a in argv):
                    kwargs["file_doc"] = doc
                else:
                    kwargs["stdin_doc"] = doc
            self.add(f"cli-{name}", "cli", "cli", argv=argv, **kwargs)

        # Argument shapes over more documents: the flags above are covered once
        # each, and these re-run the three most-used shapes across the format
        # targets so a CLI that works on one document and not the next is caught.
        for shape, argv in (
            ("plain", ["-"]),
            ("reindent", ["-", "-r"]),
            ("kw-upper-reindent", ["-", "-r", "-k", "upper"]),
        ):
            for doc_id in stride(self.by_group.get("written/format", []), 24):
                self.add(f"cli-{shape}-{doc_id}", "cli", "cli",
                         argv=list(argv), stdin_doc=doc_id)

    # ------------------------------------------------------------------
    # F. structural cases
    # ------------------------------------------------------------------
    # Declared here, evaluated by build.py and structure.py.  Every one of these
    # is a question about what the submission *built*: does it compile, does it
    # link, does the binary that came out have the properties a Go binary has,
    # does an outside consumer type-check against the API it publishes.
    #
    # What is deliberately absent is any case that reads the submission's source
    # and compares it with a description.  There were nineteen of those -- the
    # eleven api-* cases comparing an API dump against a checklist, a doc-comment
    # ratio, a gofmt invocation, a line-count floor, and five cases hashing files
    # the grader never opens again.  A case like that is answered by looking at how
    # the code is written, and two ports that behave identically can answer it
    # differently, which makes it a style rule with a score attached.  The
    # migration claims they stood for did not disappear: `consumer-compiles`
    # replaces the whole api-* family by compiling a downstream module that names
    # every contracted symbol in a position where the type has to be right, and
    # the rest moved to the audit stage, where an agent reads the tree and can
    # say *why* something looks wrong instead of matching it against a pattern.

    def build_struct(self) -> None:
        for name, note in (
            ("go-mod-parses", "go.mod exists, declares the contract module path"),
            ("go-mod-directive", "the go directive is inside the pinned range"),
            ("go-mod-no-requires", "no non-stdlib requirements"),
            ("go-sum-absent-or-empty", "nothing to verify means no go.sum entries"),
            ("build-all", "go build ./... succeeds"),
            ("vet-all", "go vet ./... succeeds"),
            ("test-all", "go test ./... succeeds if any tests exist"),
            ("install-sqlformat", "go install produces bin/sqlformat"),
            ("binary-is-elf", "the installed artifact is an ELF64 executable"),
            ("binary-static", "no PT_INTERP, no PT_DYNAMIC, no DT_NEEDED"),
            ("binary-buildinfo", ".go.buildinfo is present and parses"),
            ("binary-buildid", ".note.go.buildid is present"),
            ("binary-trimpath", "the build settings record -trimpath=true"),
            ("binary-cgo-disabled", "the build settings record CGO_ENABLED=0"),
            ("binary-module-path", "buildinfo names the contract module path"),
            ("binary-reproducible", "a second build is byte identical"),
            ("package-set", "every contracted package exists"),
            ("package-core-standalone", "the root package compiles alone"),
            ("package-model-standalone", "sql+tokens compile without the rest"),
            ("package-keywords-standalone", "keywords+tokens compile alone"),
            ("package-cmd-main", "cmd/sqlformat is package main with a main func"),
            ("probe-core-compiles", "the core tier links against the submission"),
            ("probe-model-compiles", "the model tier links"),
            ("probe-keywords-compiles", "the keywords tier links"),
            ("probe-parts-compiles", "the parts tier links"),
            ("consumer-compiles", "every contracted symbol type-checks "
                                  "from an outside consumer"),
            ("import-closure-stdlib", "go list -deps yields stdlib only"),
            ("module-closure-empty", "go list -m all yields the module alone"),
            ("no-vendor-dir", "no vendor/ directory"),
            ("install-inventory", "the install tree matches the contract"),
            ("man-page-installed", "the man page ships where the contract says"),
        ):
            self.add(f"struct-{name}", "structure", "struct", check=name, note=note)

        # Recorded at weight 0, and the reason is the one the whole stage runs on: an
        # expectation is scored only if State A can answer it, because State A is the
        # oracle every frozen expectation here was computed from.  Every check above
        # is about the artifact the build installs and has a pre-migration analogue
        # -- State A installs an entry point too, and "the install tree matches the
        # contract" is a claim about a deliverable rather than about a language.
        # This one has none: the absence of libpython, Py_Initialize and PyRun_ is
        # unsatisfiable for a CPython library, absolutely and by definition, and
        # grading the reference below full marks on its own corpus would stop a
        # shortfall from being readable as a behavioural difference.
        #
        # `no-interpreter-dependency` in stage 1 owns the question, required, and
        # asks it of the tree rather than of one binary's symbol table -- an embedded
        # interpreter, a cgo link against libpython, a plugin load, a build tag under
        # which an interpreter path is compiled in.  Failing it there is a zero for
        # the submission.  The symbols are still read and still reported.
        self.add("struct-binary-no-cpython", "structure", "struct",
                 check="binary-no-cpython", weight=0.0,
                 note="no libpython, Py_Initialize or PyRun_ symbols "
                      "[recorded, not scored: stage 1 owns this]")

    def build_provenance(self) -> None:
        """Where the artifacts came from, and whether the answers were computed.

        Two families, and the split is the point.  `shim` opens a file the graded
        build wrote.  `fresh` runs the built binary on SQL that did not exist when
        the image was built, so there is nothing for a recorded answer to match --
        the one thing in this stage not graded against a frozen expectation, and the
        reason a submission cannot pass it from a table.

        The fresh cases carry an index rather than a document id, because the
        documents are composed at grading time from a seed drawn then; the catalog
        can say how many there will be and nothing about which they are.  That is
        also why the count lives in provenance.py and is imported rather than
        written twice: the module composes them, and a second number here would be a
        number that can disagree with the draw.  freeze.py checks the assertion holds
        for the reference across twenty draws before the image ships.
        """
        import provenance

        # weight=0.0: recorded, reported, and scored by nobody here.
        #
        # Its subject is the retired interpreter, and the criterion this stage runs
        # on is that an expectation is scored only if State A -- the sqlparse every
        # frozen expectation in this catalog was computed from -- can answer it.
        # State A cannot: it *is* Python, its build reaches an interpreter by
        # construction, and a ledger written during its build would be full.  A
        # check the oracle fails by definition makes the corpus unfalsifiable, since
        # a shortfall stops being readable as a behavioural difference.
        #
        # Nothing is lost.  `no-interpreter-dependency` in stage 1 is required and
        # asks the stronger form of the same question -- which file asks for an
        # interpreter, and under what conditions, including conditions this image
        # does not create -- and a required stage 1 gate failing scores the whole
        # submission zero before this image starts.  The ledger entry still reaches
        # the report as evidence for a reader; it just is not priced twice.
        self.add("prov-python-shim-clean", "shim", "provenance",
                 check="python-shim-clean", weight=0.0,
                 note="the graded build never invoked a Python interpreter "
                      "[recorded, not scored: stage 1 owns this]")
        for index in range(provenance.FRESH_DOCUMENTS):
            self.add(
                f"prov-fresh-lossless-{index:03d}", "fresh", "provenance",
                check="fresh-lossless", params={"index": index},
                note="parse then concatenate reproduces SQL composed at "
                     "grading time")

    def build_all(self) -> None:
        self.build_core()
        self.build_model()
        self.build_keywords()
        self.build_parts()
        self.build_cli()
        self.build_struct()
        self.build_provenance()


def _slug(text: str) -> str:
    """A filesystem- and JUnit-safe id fragment that is injective on our inputs.

    Case is preserved: half the keyword cases exist precisely to check that the
    reference upper-cases before looking a word up, so `select` and `SELECT` are
    two different cases and must not collapse to one id.
    """
    out = []
    for ch in text:
        if ch.isalnum() and ord(ch) < 128:
            out.append(ch)
        else:
            out.append(f"x{ord(ch):02x}")
    return "".join(out)[:48]


def check_shape(total: int, by_kind: dict[str, int], families: int) -> list[str]:
    """Compare a built catalog against DECLARED_SHAPE.  Returns what differs.

    Reported as a list rather than raised so every difference is visible at once.
    A drift of one case usually has one cause, and seeing "struct 50 -> 51, total
    8322 -> 8323" together names that cause; being told only about the total means
    finding out about the kind on the next run.

    Both directions are errors, and so is a kind that appeared or vanished
    entirely: a new kind nothing scores is as much a problem as a missing one,
    because the driver dispatches on kind and would silently ignore it.
    """
    problems: list[str] = []
    if total != DECLARED_SHAPE["total"]:
        problems.append(
            f"total: declared {DECLARED_SHAPE['total']}, built {total} "
            f"({total - DECLARED_SHAPE['total']:+d})")
    if families != DECLARED_SHAPE["families"]:
        problems.append(
            f"families: declared {DECLARED_SHAPE['families']}, built {families} "
            f"({families - DECLARED_SHAPE['families']:+d})")
    declared_kinds = DECLARED_SHAPE["by_kind"]
    for kind in sorted(set(declared_kinds) | set(by_kind)):
        want = declared_kinds.get(kind)
        got = by_kind.get(kind, 0)
        if want is None:
            problems.append(f"kind {kind}: not declared, built {got}")
        elif got != want:
            problems.append(
                f"kind {kind}: declared {want}, built {got} ({got - want:+d})")
    return problems


# Every kind whose cases name a check that a class has to answer, and how that
# class finds it.  Derived from the dispatchers themselves: `Structure.evaluate`
# looks up `check_<name>`, `Provenance.evaluate` looks up `gate_<name>`, both with
# dashes turned into underscores.  Kinds absent from this table (`probe`, `cli`) are
# answered by comparison against a frozen expectation and name no method.
CHECK_DISPATCH = {
    "struct": ("structure", "Structure", "check_"),
    "provenance": ("provenance", "Provenance", "gate_"),
}


def check_handlers(cases: list[dict]) -> None:
    """Every declared check must resolve to a method on the class that answers it.

    Both dispatchers look their handler up by name and, finding none, record the
    case as *failed* with "no handler for ...".  That is the right thing to do at
    grading time -- one missing handler must not abort 8,000 other cases -- and the
    wrong thing to find out then, because on a report the case is indistinguishable
    from a submission that got it wrong.  It fails for every submission, including a
    correct one, and the number it moves is the score.

    So the question is asked here, at image build time, in the direction that
    matters: not "is every method declared" (an undeclared method is dead code,
    harmless) but "is every declared name answerable".  Resolved with getattr
    against the imported class rather than by parsing the source, because getattr is
    what the dispatcher does.

    A kind that carries a `check` and is not in CHECK_DISPATCH is also an error.  The
    failure it stands for is a new kind added here and nowhere else: the driver
    dispatches on kind, so its cases would be collected, weighed, reported -- and
    never run.
    """
    import importlib

    problems: list[str] = []
    kinds = {case["kind"] for case in cases if case.get("check")}
    for kind in sorted(kinds - set(CHECK_DISPATCH)):
        problems.append(
            f"kind {kind!r} declares cases with a `check` but names no class to "
            f"answer them; add it to CHECK_DISPATCH")
    for kind, (module_name, class_name, prefix) in sorted(CHECK_DISPATCH.items()):
        declared = {case["check"] for case in cases
                    if case["kind"] == kind and case.get("check")}
        if not declared:
            problems.append(
                f"kind {kind!r} is in CHECK_DISPATCH and has no cases; either it "
                f"stopped being built or the table is stale")
            continue
        cls = getattr(importlib.import_module(module_name), class_name)
        available = {name[len(prefix):].replace("_", "-")
                     for name in dir(cls) if name.startswith(prefix)}
        for name in sorted(declared - available):
            problems.append(
                f"{kind} case declares check {name!r} with no "
                f"{class_name}.{prefix}{name.replace('-', '_')}")
    if problems:
        raise SystemExit(
            "cases declared with no handler to answer them:\n  "
            + "\n  ".join(problems)
            + "\nA declared name with no handler fails for every submission, "
              "including a correct one."
        )


def build_catalog(docs: dict, keywords: dict) -> dict:
    catalog = Catalog(docs, keywords)
    catalog.build_all()
    cases = catalog.cases
    check_handlers(cases)

    by_kind: dict[str, int] = {}
    by_family: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    weight_by_family: dict[str, float] = {}
    for case in cases:
        by_kind[case["kind"]] = by_kind.get(case["kind"], 0) + 1
        by_family[case["family"]] = by_family.get(case["family"], 0) + 1
        if case.get("tier"):
            by_tier[case["tier"]] = by_tier.get(case["tier"], 0) + 1
        if case["kind"] in ("probe", "cli"):
            weight_by_family[case["family"]] = (
                weight_by_family.get(case["family"], 0.0) + case["weight"]
            )

    behavioural = by_kind.get("probe", 0) + by_kind.get("cli", 0)
    total_weight = sum(weight_by_family.values())

    if len(cases) < MIN_TOTAL_CASES:
        raise SystemExit(f"catalog has {len(cases)} cases, floor is {MIN_TOTAL_CASES}")
    if behavioural < MIN_BEHAVIOURAL_CASES:
        raise SystemExit(
            f"only {behavioural} behavioural cases, floor is {MIN_BEHAVIOURAL_CASES}")
    if by_kind.get("struct", 0) < MIN_STRUCT_CASES:
        raise SystemExit(f"only {by_kind.get('struct', 0)} structural cases")
    if by_kind.get("cli", 0) < MIN_CLI_CASES:
        raise SystemExit(f"only {by_kind.get('cli', 0)} CLI cases")
    if by_kind.get("provenance", 0) < MIN_PROVENANCE_CASES:
        raise SystemExit(
            f"only {by_kind.get('provenance', 0)} provenance cases, floor is "
            f"{MIN_PROVENANCE_CASES}")
    for tier, floor in sorted(MIN_TIER_CASES.items()):
        if by_tier.get(tier, 0) < floor:
            raise SystemExit(
                f"tier {tier} has {by_tier.get(tier, 0)} cases, floor is {floor}")

    drift = check_shape(len(cases), by_kind, len(by_family))
    if drift:
        raise SystemExit(
            "catalog shape drifted from DECLARED_SHAPE:\n  "
            + "\n  ".join(drift)
            + "\nIf the change was intended, update DECLARED_SHAPE in catalog.py "
              "in the same commit."
        )

    # No family may dominate.  A family worth more than a sixth of the
    # behavioural weight would let one code path decide the score, which defeats
    # the point of grading a whole-repository port.
    for family, weight in sorted(weight_by_family.items()):
        share = weight / total_weight
        if share > 0.17:
            raise SystemExit(
                f"family {family} carries {share:.1%} of the behavioural weight; "
                f"lower WEIGHTS[{family!r}]"
            )

    manifest = {
        "schema": "swerefactor-catalog-v1",
        "task": TASK,
        "documents_digest": docs["digest"],
        "weights": dict(sorted(WEIGHTS.items())),
        "floors": {
            "total": MIN_TOTAL_CASES,
            "behavioural": MIN_BEHAVIOURAL_CASES,
            "struct": MIN_STRUCT_CASES,
            "cli": MIN_CLI_CASES,
            "provenance": MIN_PROVENANCE_CASES,
            "tier": dict(sorted(MIN_TIER_CASES.items())),
        },
        "counts": {
            "total": len(cases),
            "behavioural": behavioural,
            "by_kind": dict(sorted(by_kind.items())),
            "by_family": dict(sorted(by_family.items())),
            "by_tier": dict(sorted(by_tier.items())),
        },
        "weight_share": {
            family: round(weight / total_weight, 5)
            for family, weight in sorted(weight_by_family.items())
        },
        "cases": cases,
    }
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["digest"] = hashlib.sha256(payload).hexdigest()
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", required=True, type=Path)
    parser.add_argument("--keywords", required=True, type=Path,
                        help="JSON table name -> word list, read from the reference")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()

    docs = json.loads(args.documents.read_text(encoding="utf-8"))
    keywords = json.loads(args.keywords.read_text(encoding="utf-8"))
    manifest = build_catalog(docs, keywords)
    if args.out:
        args.out.write_text(
            json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8"
        )
    counts = manifest["counts"]
    print(f"cases: {counts['total']}  behavioural: {counts['behavioural']}")
    for kind, count in sorted(counts["by_kind"].items()):
        print(f"  kind {kind:8s} {count:6d}")
    for tier, count in sorted(counts["by_tier"].items()):
        print(f"  tier {tier:10s} {count:6d}")
    if args.summary:
        for family, count in sorted(counts["by_family"].items()):
            share = manifest["weight_share"].get(family)
            extra = f"  {share:6.2%} of weight" if share else ""
            print(f"  family {family:14s} {count:6d}{extra}")
    print(f"digest: {manifest['digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
