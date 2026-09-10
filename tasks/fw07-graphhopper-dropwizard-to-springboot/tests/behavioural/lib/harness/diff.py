"""The single comparison every graded case goes through.

One function, `compare_case`, used by every module.  Deliberately one: a suite
where each module compares responses its own way is a suite where the strictness
of a check depends on who wrote the module, and where a bug fixed in one module
survives in six others.

What is compared, in order, and why that order:

  1. status        The coarsest signal, and the one no framework can fudge.  A
                   mismatch here is reported alone — the headers and body of a
                   500 say nothing useful about a rewrite that should have
                   answered 200, and printing them buries the finding.
  2. media type    Type without parameters.  See normalize.media_type for why
                   charset is excluded.
  3. header map    Names to values, unordered across names, ordered within one.
                   Each side's own loopback authority folds to a token first —
                   the two sides are given different ports deliberately, so a
                   header that echoes one differs by construction.
  4. body          Content-Encoding undone first (after step 3, so the coding a
                   response announces is still graded as a header), then compared
                   per the case's body_mode.

No check anywhere in this file looks at the submission's source, and it has no
way to: the capture holds responses, not files.  That is the stage-2 primitive —
build both, compare what they answer — enforced by what the data structure
contains rather than by a rule an author has to remember.
"""
from __future__ import annotations

import json

from . import normalize


class Mismatch(AssertionError):
    """A graded difference, with a report the submitter can act on."""


def _report(case_id: str, why: str, headline: str, detail: list[str]) -> str:
    lines = [
        f"{case_id}: {headline}",
        f"  what this case grades: {why}",
    ]
    lines.extend(f"  {d}" for d in detail)
    lines.append("  (reference = State A, the unmodified repository; "
                 "submission = the rewrite)")
    return "\n".join(lines)


def _body_preview(raw: bytes, limit: int = 240) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<{len(raw)} bytes, not UTF-8: {raw[:32].hex()}...>"
    text = text.replace("\n", "\\n")
    return text[:limit] + ("..." if len(text) > limit else "")


def compare_case(case_id: str, ref, sub, masked: set[tuple], entry: dict,
                 authorities: tuple[int, int] | None = None):
    """Raise Mismatch on any graded difference; return a note on success.

    `masked` is the measured-volatile path set for this case, already unioned
    across both sides by capture.case_pair.

    `authorities` is (reference_port, submission_port) — the port each side was
    actually reached on for this case.  The two are different on purpose, so a
    response echoing its own authority differs by construction; each side's own
    folds to one token before the headers are compared.  Optional so that a caller
    which does not know the ports still gets every other comparison, and when it
    is absent nothing is folded and an echoed authority reads as a difference.
    """
    why = entry["why"]
    mode = entry["body_mode"]

    # --- 1. status -----------------------------------------------------------
    if ref.status != sub.status:
        raise Mismatch(_report(
            case_id, why,
            f"status differs: reference {ref.status}, submission {sub.status}",
            [f"reference body: {_body_preview(ref.body)}",
             f"submission body: {_body_preview(sub.body)}"]))

    ref_h = normalize.header_map(ref.headers)
    sub_h = normalize.header_map(sub.headers)
    if authorities is not None:
        ref_h = normalize.fold_authority(ref_h, authorities[0])
        sub_h = normalize.fold_authority(sub_h, authorities[1])

    # --- 2. media type -------------------------------------------------------
    ref_ct = normalize.media_type(ref_h)
    sub_ct = normalize.media_type(sub_h)
    if ref_ct != sub_ct:
        raise Mismatch(_report(
            case_id, why,
            f"content type differs: reference {ref_ct!r}, submission {sub_ct!r}",
            ["charset is deliberately not compared; this is the media type "
             "alone"]))

    # --- 3. headers ----------------------------------------------------------
    problems = []
    for name in sorted(set(ref_h) | set(sub_h)):
        r = ref_h.get(name)
        s = sub_h.get(name)
        if r == s:
            continue
        if r is None:
            problems.append(f"{name}: absent in reference, submission sent {s}")
        elif s is None:
            problems.append(f"{name}: reference sent {r}, submission omits it")
        else:
            problems.append(f"{name}: reference {r}, submission {s}")
    if problems:
        raise Mismatch(_report(
            case_id, why,
            f"{len(problems)} header difference(s)",
            problems[:8] + (["..."] if len(problems) > 8 else [])))

    # --- 4. body -------------------------------------------------------------
    if mode == "ignore":
        return (f"{case_id}: status {ref.status}, media type {ref_ct}, headers "
                f"agree (body not compared for this case)")

    # Content-Encoding is undone first, and only AFTER the header comparison
    # above: the coding a response announces is part of its contract and is
    # graded as a header, while the compressed bytes are not comparable at all
    # (two servers may pick different levels, or link different zlib builds).  A
    # body that does not decode as it claims is reported per side, because which
    # side failed to decode is the whole finding.
    ref_body, ref_derr = normalize.decode_body(ref_h, ref.body)
    sub_body, sub_derr = normalize.decode_body(sub_h, sub.body)
    if ref_derr or sub_derr:
        which = "reference" if ref_derr else "submission"
        raise Mismatch(_report(
            case_id, why,
            f"the {which} body could not be decoded",
            [f"reference: {ref_derr or 'decoded'}",
             f"submission: {sub_derr or 'decoded'}"]))

    if mode == "exact":
        if ref_body != sub_body:
            raise Mismatch(_report(
                case_id, why,
                f"body differs ({len(ref_body)} vs {len(sub_body)} bytes)",
                [f"reference: {_body_preview(ref_body)}",
                 f"submission: {_body_preview(sub_body)}"]))
        return f"{case_id}: status {ref.status}, headers and body byte-identical"

    # mode == "json"
    if ref_body == sub_body:
        return f"{case_id}: status {ref.status}, headers and body byte-identical"

    if entry["reference_volatility"]["unstable_bytes"]:
        # The reference itself answered this case two different ways in bytes
        # that are not JSON.  Nothing here is comparable, and that is a fact
        # about the endpoint rather than about the submission.
        return (f"{case_id}: status {ref.status} and headers agree; the body is "
                f"not comparable (the reference is not byte-stable against "
                f"itself here and the body is not JSON)")

    # An empty reference body is not a construction error, and has to be settled
    # before the parse below decides whose fault it is.  Three graded cases answer
    # with no body at all and are right to: `hdr-head-info` and `hdr-head-route`
    # are HEAD, which has no body by definition, and `asset-root` is a 303 whose
    # meaning is in Location.  For an identical submission all three return on the
    # byte-equality check above and never reach here.
    #
    # Reaching here means the bodies already differ, so an empty reference means
    # the SUBMISSION wrote one — and that is a live porting bug rather than a
    # hypothetical: Spring and Jersey disagree about whether a HEAD handler may
    # write a body, and a redirect handler that renders a courtesy page is the same
    # shape.  Blamed explicitly, because the json-mode message below would
    # otherwise report the corpus as broken and advise changing this case's mode —
    # the check would fail either way, so the score would be right and the reader
    # would be sent to the wrong file.  `exact` mode already reports this
    # correctly, which is what makes the asymmetry a bug in this branch alone.
    if not ref_body:
        raise Mismatch(_report(
            case_id, why,
            f"the reference answered with no body; the submission sent "
            f"{len(sub_body)} byte(s)",
            [f"submission: {_body_preview(sub_body)}",
             "the reference's empty body is correct for this case — a HEAD "
             "response carries none, and a redirect carries its meaning in "
             "Location"]))

    try:
        ref_doc = json.loads(ref_body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Mismatch(_report(
            case_id, why,
            "the reference body is not JSON, but this case is declared json-mode",
            [f"parse error: {exc}",
             f"reference: {_body_preview(ref_body)}",
             "this is a construction error in the corpus, not a submission "
             "failure — the case should be exact or ignore mode"]))
    try:
        sub_doc = json.loads(sub_body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Mismatch(_report(
            case_id, why,
            "the submission did not answer with JSON where the reference did",
            [f"parse error: {exc}",
             f"submission: {_body_preview(sub_body)}"]))

    left, right, measured, declared = normalize.mask_pair(
        ref_doc, sub_doc,
        {tuple(p) for p in entry["reference_volatility"]["paths"]},
        {tuple(p) for p in entry["submission_volatility"]["paths"]})
    if left == right:
        notes = []
        if measured:
            notes.append(f"{len(measured)} measured-volatile")
        if declared:
            notes.append(f"{len(declared)} declared wall-clock")
        return (f"{case_id}: status {ref.status}, headers agree, body matches "
                f"structurally"
                + (f" ({', '.join(notes)} field(s) masked)" if notes else ""))

    diffs = normalize.first_differences(left, right)
    raise Mismatch(_report(
        case_id, why,
        "body differs structurally",
        diffs + [f"({len(measured)} field(s) masked as measured-volatile and "
                 f"{len(declared)} as declared wall-clock; neither is among "
                 f"these)"]))
