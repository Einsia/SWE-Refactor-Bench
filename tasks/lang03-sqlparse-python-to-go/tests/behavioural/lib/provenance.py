#!/usr/bin/env python3
"""Where the artifacts came from, and whether the answers were computed.

Two questions, both answered by opening something the build produced or by running
it on input that did not exist when the submission was frozen.  That is the whole
membership rule for this module, and it is the rule lang01 states for its own
provenance stage: not "mechanical versus semantic" but *whether the gate has an
artifact to point at*.

  python-shim-clean   the interpreter shim's ledger, written during the graded
                      build.  A build that reached for python, pip or poetry left
                      a record; the shim refused, so it also failed.

  fresh-lossless-*    one case per SQL document composed at grading time from a
                      seed drawn from the OS.  The round trip is asserted to be
                      lossless -- `"".join(str(s) for s in parse(x)) == x` -- which
                      is a property of the input, so it can be checked without a
                      reference, and this image has no reference to check against.

The second is why this module exists at all.  Everything else in this stage is
graded against expectations frozen into the image, and a submission that mapped
document digests to stored outputs would pass all of it.  These documents are
generated after the image was built, from `documents.synthesize` with a random
seed, so nothing could have stored them.

Losslessness holds for every document this family composes, which is not the same
as holding for every document: the reference drops a newline that follows a
terminating semicolon, so the draw removes trailing newlines.  `fresh_documents`
carries the measurement.  That is the shape of the whole trade -- an assertion
made without a reference has to be one whose truth was established *with* one,
before the reference was deleted.

What the family does not claim is that the parse is right.  A submission that
returned the whole document as one statement would pass every case here, and for a
single-statement document that answer is also correct.  The frozen cases are what
compare a parse against the reference's; this family only establishes that the
answers were computed rather than recalled, and it is deliberately the weakest
assertion that cannot be satisfied from a table.

What is deliberately *not* here
-------------------------------
A seen/unseen pass-rate comparison: the score on State A's own test SQL against
the score on generated SQL, failing on a large one-sided gap.  Two problems with
it here.  It needs every other module's graded results, which a per-module process
cannot see, and its verdict is a statistical argument rather than a fact about an
artifact.  The strong form of that question -- find an input where the two
implementations disagree -- is stage 3's, run by six models with both trees and
both builds in front of them.  A gap statistic is a poor approximation of a search,
and a deterministic stage where one failing check costs everything is the last
place to put one.

The source-reading gates are stage 1's, for the same reason.  Whether Python is
really gone, whether a licence survived, whether a build tag switches behaviour:
those are read off the tree, and a pattern that reads a tree cannot tell a port
from a transliteration.  A model with both trees open can.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import documents
import vlib
from build import BuildOutcome
from executor import Record
from vlib import CaseOutcome, Log

# How many documents the freshness family composes, and how many statements each
# gets.  Forty is chosen against the runtime: each case is one probe round trip on
# an already-running binary, so the family costs a second or two in total, and
# forty distinct documents from the grammar cover every construct it emits several
# times over.
FRESH_DOCUMENTS = 40

# The tier that answers the freshness family.  `core` on purpose: it names only the
# root package, so a submission whose sql package does not compile is still asked
# whether its answers were computed.  The cheat this catches does not need the
# accessors.
FRESH_TIER = "core"


class Provenance:
    """Evaluates the provenance cases against one built submission."""

    def __init__(self, repo: Path, outcome: BuildOutcome, scratch: Path,
                 log: Log, *, runner=None, docs_root: Path | None = None) -> None:
        self.repo = repo
        self.outcome = outcome
        self.scratch = scratch
        self.log = log
        # Supplied by the driver: `runner(docs_dir, documents_json, cases)` answers a
        # list of probe cases against a docs directory of this module's choosing and
        # hands back `{case_id: Record}`.  Injected rather than constructed here so
        # this module does not own a second copy of the probe protocol -- the executor
        # already speaks it, and a second speaker is a second thing that can disagree
        # with the reference.  Taking the docs directory as an argument is what lets
        # the graded run's own argv builder point at input composed after the image
        # was built.
        self.runner = runner
        # The frozen document set, whose texts the fresh draw excludes.  The
        # directory holding `documents.json` and `docs/`.
        self.docs_root = docs_root
        self.seed: int | None = None
        self._fresh: list[tuple[str, str]] | None = None
        self._records: dict[str, Record] | None = None
        self._error = ""
        scratch.mkdir(parents=True, exist_ok=True)

    # -- applicability -----------------------------------------------------

    # One case here reads scaffolding that only the Go build installs, and reading
    # it off a pre-migration tree does not produce a wrong answer -- it produces a
    # *true-looking* one, which is worse.  `gate_python_shim_clean` treats an absent
    # ledger as a pass, correctly, because the shim writes it on first refusal; but
    # PyBuilder never installs the shim (a shim whose job is to break Python cannot
    # be in the way of the implementation being Python), so the ledger is absent for
    # a reason that has nothing to do with what the build did.  The case would report
    # "the graded build never invoked a Python interpreter" about a build that ran
    # `compileall` and `import sqlparse`.
    #
    # Skipped rather than failed: the claim is not false of the submission, it is
    # unasked.  Skipped rather than answered: a weight-zero case still prints its
    # summary into the report a human reads.
    PY_PROV_SKIP = {
        "python-shim-clean":
            "the interpreter shim is not installed for a pre-migration tree, so "
            "there is no ledger to read; this case's finding would be vacuous "
            "rather than true. Stage 1's required no-interpreter-dependency gate "
            "asks the stronger form of the question of the tree itself",
    }

    def skip_reason(self, check: str) -> str:
        """Why `check` cannot be asked of this build, or "" when it can.

        `outcome is None` is freeze.check_fresh_premise, which evaluates only the
        `fresh` family against the reference and has no build to describe.  Nothing
        is skipped there, which is the behaviour that check had before this table
        existed -- and the family it does run is the one no table may skip.
        """
        if self.outcome is None or self.outcome.language == "go":
            return ""
        return self.PY_PROV_SKIP.get(check, "")

    # -- dispatch ----------------------------------------------------------

    def evaluate(self, cases: list[dict]) -> list[CaseOutcome]:
        results: list[CaseOutcome] = []
        for case in cases:
            check = case["check"]
            reason = self.skip_reason(check)
            if reason:
                results.append(CaseOutcome(
                    case_id=case["id"], family=case["family"], kind="provenance",
                    passed=False, weight=float(case.get("weight", 1.0)),
                    detail=reason, skipped=True, skip_reason=reason,
                ))
                continue
            handler = getattr(self, f"gate_{check.replace('-', '_')}", None)
            start = vlib.now()
            if handler is None:
                passed, detail = False, f"no handler for provenance case '{check}'"
            else:
                try:
                    passed, detail = handler(case)
                except Exception as exc:  # one broken case must not lose the rest
                    passed = False
                    detail = f"{type(exc).__name__}: {exc}"
            results.append(CaseOutcome(
                case_id=case["id"],
                family=case["family"],
                kind="provenance",
                passed=passed,
                weight=float(case.get("weight", 1.0)),
                detail=vlib.clip(detail, 2000),
                duration=vlib.now() - start,
            ))
        return results

    # -- the build's own record --------------------------------------------

    def gate_python_shim_clean(self, case: dict) -> tuple[bool, str]:
        """The graded build never reached for a Python interpreter.

        Read from the shim's ledger rather than inferred from the build log, and the
        distinction matters: the shim *refuses*, so a build that needed Python also
        failed, and the ledger says which name it asked for and with what arguments.
        Every invocation is a finding -- unlike the C siblings, where a configure
        probe compiling C is legitimate, a Go build has no honest reason to run an
        interpreter.

        An absent ledger is a pass, and that is not a hole: the shim only creates it
        on first refusal, so absent means nothing was intercepted.  Whether the tree
        *contains* a script that would reach for python is a different question, read
        off the tree in stage 1.
        """
        ledger = self.outcome.shim_log
        if ledger is None or not ledger.is_file():
            return True, (
                "the interpreter shim recorded nothing: no python, pip, poetry or "
                "pypy invocation during the graded build"
            )
        records: list[dict] = []
        broken = 0
        for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                broken += 1
        if not records and not broken:
            return True, "the interpreter shim ledger is empty"
        shown = []
        for record in records[:12]:
            argv = " ".join(str(a) for a in (record.get("argv") or [])[:6])
            shown.append(
                f"{record.get('tool', '?')} {argv} (cwd {record.get('cwd', '?')})")
        tools = sorted({str(r.get("tool", "?")) for r in records})
        note = f"; {broken} unparseable ledger line(s)" if broken else ""
        return False, (
            f"the graded build invoked a Python interpreter {len(records)} time(s) "
            f"({', '.join(tools)}). The port is not free of Python if building it "
            f"requires one{note}. Ledger: " + " | ".join(shown)
        )

    # -- answers computed rather than recalled -----------------------------

    def frozen_texts(self) -> set[str]:
        """Every document already in the image, as text, for the generator to avoid.

        The exclusion is the family's whole claim.  `synthesize` draws from a small
        grammar, and `documents.build` says so in as many words -- "create unique
        index i on t (a, b)" is a thing both a person and the generator would write.
        A fresh draw that rediscovered a frozen document would be one a submission
        could have an answer for, and the family would be asserting freshness about
        an input that is not fresh.  Cheap to rule out, so it is ruled out.

        Read from the shipped document set rather than recomputed, and restricted
        to the documents that have a text form at all: `build` excluded exactly the
        undeclared-encoding UTF-8 documents from its own draw, for the same reason.
        """
        meta = json.loads(
            (self.docs_root / "documents.json").read_text(encoding="utf-8"))
        docs_dir = self.docs_root / "docs"
        texts: set[str] = set()
        for entry in meta.get("documents", []):
            if entry.get("encoding") or not entry.get("utf8"):
                continue
            path = docs_dir / entry["id"]
            if path.is_file():
                texts.add(path.read_bytes().decode("utf-8"))
        return texts

    def fresh_records(self) -> dict[str, Record]:
        """Compose the documents, put them to the submission, cache the answers.

        One draw and one probe session for the whole family, computed on first ask.
        Forty separate sessions would be forty processes to say the same thing, and
        forty seeds would make the family forty experiments instead of one -- when a
        report says a submission failed this family, it should mean one document set
        it had never seen, not a scatter.

        The documents are written where the tier binary will look for them: a docs
        directory and a documents.json of the same shape the frozen set has, in this
        module's scratch.  The runner is handed that directory, so the same argv
        builder the graded run uses points at fresh input without this file knowing
        how a tier is invoked.
        """
        if self._records is not None:
            return self._records
        # `e:` and not `d:`.  The two tags differ in whether the op is also told
        # the document's declared encoding, and `rt` reads it -- a `d:` argument
        # makes the probe answer `defect`, not a wrong answer, so every case in
        # the family becomes a harness failure rather than a verdict.  The fresh
        # documents declare no encoding, which is the branch that hands the
        # library bytes and lets it apply its own fallback.
        cases = [
            {"id": _probe_id(index), "kind": "probe", "family": "fresh",
             "tier": FRESH_TIER, "op": "rt", "args": [f"e:{doc_id}"]}
            for index, (doc_id, _) in enumerate(self.fresh_documents())
        ]
        try:
            docs = self.fresh_docs_dir()
            self._records = self.runner(docs, docs / "documents.json", cases)
        except Exception as exc:
            # Cached as a failure rather than re-raised out of every case: the
            # alternative is running a runner that has already thrown forty more
            # times and reporting the same exception forty times as if forty things
            # went wrong.  The message is kept so each case can say what it was.
            self._error = f"{type(exc).__name__}: {exc}"
            self._records = {}
            self.log.write(
                f"provenance: the fresh document family could not be run: "
                f"{self._error}")
            return self._records
        answered = sum(1 for r in self._records.values() if r.answered)
        self.log.write(
            f"provenance: {answered}/{len(cases)} fresh document(s) answered "
            f"by the {FRESH_TIER} tier")
        return self._records

    def fresh_docs_dir(self) -> Path:
        """Write the fresh draw as a docs directory plus its metadata."""
        docs = self.scratch / "fresh-docs"
        docs.mkdir(parents=True, exist_ok=True)
        entries = []
        for doc_id, sql in self.fresh_documents():
            data = sql.encode("utf-8")
            (docs / doc_id).write_bytes(data)
            entries.append({
                "id": doc_id,
                "group": "fresh",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "encoding": "",
                "utf8": True,
            })
        (docs / "documents.json").write_text(
            json.dumps({"schema": "swerefactor-documents-v1",
                        "count": len(entries),
                        "documents": entries}, indent=1, sort_keys=True) + "\n",
            encoding="utf-8")
        return docs

    def fresh_documents(self) -> list[tuple[str, str]]:
        """Documents composed here, now, from a seed the image does not contain.

        `documents.synthesize` is the same grammar the frozen set drew its
        `generated` group from -- the same constructs, the same shapes, the same
        distribution -- but the seed comes from the OS, so these particular
        documents exist in no tree, no expectation file and no manifest.  A
        submission that recorded answers has nothing to look up.

        Two restrictions on the draw, both measured against the reference rather
        than reasoned about:

        Trailing newlines are removed.  The reference drops a newline that follows a
        terminating semicolon -- `parse("select 1;\\n")` serializes back to
        `"select 1;"` -- so on documents ending that way the round trip is lossy for
        a *correct* port, and this family would be failing correctness.  Over 80
        seeds x 40 documents, 119 of 3,200 were lossy untouched and 0 of 3,200 were
        lossy with trailing newlines removed.  Which documents to compose is this
        family's own choice, so the fix belongs in the draw; the quirk itself is
        graded where it should be, by the frozen cases, which put the whole document
        set to both implementations and compare.

        Frozen documents are excluded, in both directions.  The grammar is small and
        it rediscovers curated SQL often: without the exclusion, 184 of 200 draws
        contained a document already in the image, which a submission could have an
        answer for -- so the family would be claiming freshness about input that is
        not fresh.  Re-checked after the newlines come off, because stripping could
        in principle turn a fresh document into a frozen one; that was 0 of 200
        draws, and the check is three lines.

        Ids are `fresh-` rather than the generator's own `gen-` so nothing in a
        report or a scratch directory can be mistaken for a document from the frozen
        set, whose generated ids have that prefix.  The construct name is kept: on a
        failure it says which part of the grammar the port lost text on.
        """
        if self._fresh is not None:
            return self._fresh
        frozen = self.frozen_texts()
        seed = int.from_bytes(os.urandom(8), "big")
        self.seed = seed
        picked: list[tuple[str, str]] = []
        seen: set[str] = set()
        # Drawn wider than needed so the two filters have room to reject without
        # shortening the family.  In practice they reject nothing -- the exclusion is
        # already applied inside `synthesize` -- and the margin is what makes that a
        # measurement rather than an assumption.
        for doc_id, sql in documents.synthesize(
                FRESH_DOCUMENTS * 3, seed=seed, exclude=frozen):
            text = sql.rstrip("\n")
            if not text.strip() or text in frozen or text in seen:
                continue
            seen.add(text)
            picked.append(("fresh-" + doc_id[len("gen-"):], text))
            if len(picked) == FRESH_DOCUMENTS:
                break
        self._fresh = picked
        self.log.write(
            f"provenance: composed {len(picked)} document(s) at grading time from "
            f"seed {seed:#018x}, excluding {len(frozen)} document(s) already in the "
            f"image")
        return self._fresh

    def gate_fresh_lossless(self, case: dict) -> tuple[bool, str]:
        """One freshly composed document, round-tripped, asserted lossless.

        The assertion needs no reference, which is what lets it run in an image the
        reference was deleted from: `"".join(str(s) for s in parse(x))` is `x`, for
        every `x` the reference accepts.  That is a property of the input, checkable
        against the input.

        freeze.py establishes the premise rather than this file asserting it -- it
        round-trips the reference over every document in the `generated` group and
        refuses to build an image where any of them is lossy.  So "the reference is
        lossless on this grammar" is measured, once, against the reference, and what
        runs here is the consequence.

        A case is identified by its index into the draw rather than by a document id,
        because the ids come from a seed that did not exist when the catalog was
        built.  The document that failed is reported in full: forty lines of SQL is
        not too much to put in front of a reader who has to decide whether a port
        parses or remembers.
        """
        if self.runner is None or self.docs_root is None:
            # Not a submission failure, and it must not read as one: without a runner
            # no document can be put to the binary, and without the frozen set the
            # draw cannot be checked for collisions.  Both are the driver's to
            # supply, so this says whose fault it is.
            missing = " and ".join(
                part for part, ok in (("a probe runner", self.runner is not None),
                                      ("the document set", self.docs_root is not None))
                if not ok)
            return False, (
                f"verifier misconfigured: this module was constructed without "
                f"{missing}, so no freshly composed document reached the submission")
        index = int((case.get("params") or {}).get("index", -1))
        docs = self.fresh_documents()
        if not 0 <= index < len(docs):
            return False, (
                f"verifier misconfigured: this case wants document {index} of the "
                f"fresh draw and the draw produced {len(docs)}. The catalog declares "
                f"{FRESH_DOCUMENTS} of them, so either the generator could not "
                f"compose that many distinct documents or the two numbers have "
                f"drifted apart; nothing here is the submission's doing")
        doc_id, sql = docs[index]
        # Keyed by index, not by `case["id"]`: the catalog is free to name its cases
        # what it likes, and the probe cases this module composes are its own.  The
        # index is what both agree on.
        record = self.fresh_records().get(_probe_id(index))
        if self._error:
            return False, (
                f"the fresh document family could not be put to the submission: "
                f"{self._error}")
        if record is None:
            return False, f"the probe produced no answer for {doc_id}"
        if not record.answered:
            return False, (
                f"the {FRESH_TIER} tier answered {record.status!r} on a freshly "
                f"composed document: {record.note or vlib.clip(record.payload, 300)}"
                f"\n  input: {sql!r}")
        fields = _fields(record.payload)
        if record.status == "err":
            return False, (
                f"the submission raises on SQL composed at grading time, which the "
                f"reference accepts: {fields.get('kind', '?')} "
                f"{fields.get('message', '')[:200]}\n  input: {sql!r}")
        lossless = fields.get("lossless")
        if lossless is None:
            return False, (
                f"the answer carries no `lossless` field, so the round trip cannot "
                f"be read: {vlib.clip(record.payload, 300)}")
        if lossless != "1":
            # `joined` is the wire's escaped rendering, shown as the probe wrote it.
            # Decoding it here would mean a second implementation of the answer
            # encoding, which could disagree with the probe's and put a
            # verifier-side bug in front of the reader as a submission bug.
            joined = fields.get("joined", "(not reported)")
            return False, (
                f"the round trip is lossy on a document composed at grading time: "
                f"parse then concatenate did not reproduce the input. A port that "
                f"parses cannot lose text it has never seen before\n"
                f"  input:    {sql!r}\n"
                f"  rejoined: {joined[:400]} (escaped as the probe rendered it)")

        # The `lossless` line is the submission's verdict on its own answer, so it
        # is corroborated rather than believed.  The per-statement lines carry the
        # count and each statement's length in code points, and for a lossless round
        # trip those lengths sum to the length of the input.  A submission that
        # printed the verdict without doing the work has to have got the pieces
        # right too, and getting them right for a document composed after the image
        # was built is the parse.
        stmts = _stmt_lengths(record.payload)
        if stmts is None:
            return False, (
                f"a `stmt` line does not carry an integer length, so the reported "
                f"statements cannot be checked against the input: "
                f"{vlib.clip(record.payload, 300)}")
        count = fields.get("count")
        if count is not None and count.isdigit() and int(count) != len(stmts):
            return False, (
                f"the answer reports {count} statement(s) and carries {len(stmts)} "
                f"`stmt` line(s); the two cannot both describe one parse")
        total = sum(stmts)
        if total != len(sql):
            return False, (
                f"the answer claims a lossless round trip, but its {len(stmts)} "
                f"statement(s) are {total} code point(s) against {len(sql)} in the "
                f"input. A lossless parse of a document splits it without changing "
                f"its length\n  input: {sql!r}")
        return True, (
            f"{len(stmts)} statement(s), {total} code point(s), lossless round trip "
            f"over SQL composed at grading time ({doc_id})")


def _probe_id(index: int) -> str:
    """The wire id for one fresh document.  Internal to this module."""
    return f"fresh-doc-{index:03d}"


def _fields(payload: bytes) -> dict[str, str]:
    """The answer's tab-separated lines as a first-field-keyed mapping.

    Only the single-valued lines are useful here (`lossless`, `count`, `joined`),
    and those appear once each; a repeated key like `stmt` keeps its first
    occurrence, and `stmt` is read by `_stmt_lengths` instead.
    """
    out: dict[str, str] = {}
    for line in payload.decode("utf-8", "replace").splitlines():
        key, _, rest = line.partition("\t")
        if key and key not in out:
            out[key] = rest
    return out


def _stmt_lengths(payload: bytes) -> list[int] | None:
    """Each `stmt` line's length field, in order; None if one is not an integer.

    The line is `stmt \\t index \\t length \\t escaped-text`.  Only the length is
    read: it is an integer on both sides of the wire, where the text is an encoding
    this module would have to reimplement to compare.
    """
    lengths: list[int] = []
    for line in payload.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if parts[0] != "stmt":
            continue
        if len(parts) < 3 or not parts[2].isdigit():
            return None
        lengths.append(int(parts[2]))
    return lengths

