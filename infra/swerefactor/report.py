"""Render a :class:`~swerefactor.scoring.Verdict` as text a person can read.

The JSON is what the harness consumes; this is what a human opens first when a
submission scored 0 and they want to know whether that was the submission's fault
or the grader's.  So the top of the report answers exactly that, and the detail
follows.
"""

from __future__ import annotations

import textwrap

from .scoring import Verdict, _reported

RULE = "=" * 78
THIN = "-" * 78

#: Why the ladder stopped, in the words a reader needs.  One table, because the
#: same `blocked_by` was being translated by three separate literals -- and stage
#: 2's copy carried only the audit keys, so a stage-2 reason reaching it would
#: have fallen through to the generic "an earlier stage stopped the ladder" and
#: named the wrong stage.  A key with no entry is echoed raw rather than described,
#: which is the one honest thing to do with a reason this table has not been told
#: about: a wrong cause reads exactly as authoritative as a right one.
_BLOCKED = {
    "audit-gate": "the audit gate failed",
    "audit-missing": "the audit stage produced no result",
    "audit-error": "the audit stage did not complete",
    "audit-undecided": "the audit gate could not be decided",
    "audit-empty": "the audit gate declares no required checks",
    # Names the module rather than the total.  Stage 2 is all-or-nothing, so there
    # is no partial figure for a reader to work out how far short they were from:
    # the stage-2 note lists which modules were incomplete and what each of them
    # passed.  A module that timed out, errored or was never reached fails the same
    # rule as one that answered wrongly, and arrives here under the same id.
    "behavioural-incomplete":
        "a behavioural module did not pass every scored check",
    "behavioural-error": "the behavioural stage did not complete",
    "behavioural-missing": "the behavioural stage produced no result",
    "behavioural-empty": "the behavioural stage declared no modules",
    "behavioural-weights": "the behavioural module weights are unusable",
    # Stage 3, all three set beside a `harness_error`.  The stage is worth 60 of
    # the 100 points, so each of these is a run whose total is short by points that
    # were never contested -- as against a submission that was broken by an
    # adversary, which is a finding and carries no `blocked_by` at all.
    "verification-missing": "the verification stage produced no result",
    "verification-error": "the verification stage did not complete",
    "verification-incomplete": "some verification rounds did not complete",
}


def render(verdict: Verdict, title: str = "") -> str:
    v = verdict
    out: list[str] = [RULE]
    out.append(f" SWERefactorBench  {title or v.task}")
    out.append(RULE)
    out.append("")

    headline = f"score  {v.score:.2f} / {v.max_score:g}"
    if not v.valid:
        out.append(f"  {headline}   *** NOT A VALID RESULT ***")
        out.append(f"  the grader could not complete: {v.harness_error}")
        out.append("  this run says nothing about the submission and should be re-run.")
    else:
        out.append(f"  {headline}")
    out.append("")

    # -- stage 1 ------------------------------------------------------------
    gate = {"pass": "PASS", "fail": "FAIL", "not-run": "NOT RUN"}[v.audit_gate]
    out.append(f"  stage 1  agentic audit gate .......... {gate}")
    if v.audit_gate == "fail":
        out.append("           the migration is not honest; the score is 0 by policy.")
    for f in v.audit_failures[:12]:
        out.append(f"           [{f['verdict']}] {f['id']}: {f['summary']}")
        for line in str(f.get("detail", "")).splitlines()[:6]:
            out.append(f"                 {line}")
        for ev in (f.get("evidence") or [])[:4]:
            out.append(f"                 evidence: {_evidence_line(ev)}")
    if len(v.audit_failures) > 12:
        out.append(f"           ... and {len(v.audit_failures) - 12} more")

    # -- stage 2 ------------------------------------------------------------
    if "behavioural" in v.stages_run:
        out.append("")
        # Measured-but-not-counted has to say so in the header.  The table is
        # identical either way, so a reader who sees points here and 0.00 on the
        # score line below has to reconcile two numbers that do not disagree.
        if v.metadata.get("behavioural_measured_not_counted"):
            # Reached only by a verdict that arrived carrying both this flag and
            # "behavioural" in `stages_run` -- a stored score.json, or one written by
            # a grader that appended.  This tree's own grader does not append for an
            # uncredited stage (see `_report_behavioural_only`), so a freshly graded
            # verdict takes the `else` branch far below, which says the same thing
            # in more detail.  Kept because `from_dict` will read such a verdict and
            # the alternative is the scored header over an unscored stage.
            #
            # Same words as that branch, deliberately.  It said "MEASURED, NOT
            # COUNTED" while four other sites -- including both stage-3 lines and
            # the branch below -- said "RAN, NOT COUNTED", so one state had two
            # spellings and a reader comparing two reports had to work out whether
            # they were describing the same thing.
            #
            # From metadata, not from `behavioural_points`: that field is the
            # amount awarded and stays 0 here, so that `stage2_points` in the
            # published numbers never contradicts `score`.
            out.append(f"  stage 2  behavioural modules ............. "
                       f"RAN, NOT COUNTED  "
                       f"(would have been "
                       f"{v.metadata.get('behavioural_measured_points', 0.0):.2f}"
                       f" points, rate "
                       f"{v.metadata.get('behavioural_measured_rate', 0.0):.4f})")
            out.append("           stage 1 stopped the ladder; this ran and is "
                       "shown so a zero can be attributed")
            would_block = v.metadata.get("behavioural_would_have_blocked")
            if would_block:
                out.append(f"           on its own it would also have stopped "
                           f"the ladder: {would_block}")
        else:
            # The rate beside the points, because the points are all-or-nothing and
            # a zero cannot say which zero it is: a tree failing one check of six
            # thousand and a tree that never compiled are both "0.00 points", and
            # only the rate separates them.  Named as a pass rate rather than left
            # as "rate", since a reader who sees 0.9998 beside 0.00 points needs the
            # line to say the number is a measurement and not an award.
            out.append(f"  stage 2  behavioural modules ............. "
                       f"{v.behavioural_points:.2f} points  "
                       f"({v.behavioural_rate:.4f} of weighted checks passed)")
        # A block here used to explain the stage-wide zero a required module at rate 0
        # imposed -- naming the rule and printing what the rest of the tree measured,
        # because "rate 0.0000" above a table of non-zero rates reads as an arithmetic
        # error otherwise.  There is no such rule now: no module can take the stage to
        # zero, so the total is always the table added up and needs no defence.
        out.append("")
        out.extend(_module_header())
        for m in sorted(v.modules, key=lambda x: (-x.weight, x.id)):
            # `ok` marks the rows that carry the stage.  A weight-0 row prints `-`
            # rather than a verdict: it reports without judging, so neither yes nor
            # no would be true of it.
            out.append(f"    {m.id[:26]:<26} {m.weight:>7.2f} {m.rate:>8.4f} "
                       f"{_done(m.weight, m.complete):>8} "
                       f"{m.passed:>7} {m.failed:>6} {m.errored:>5} {m.skipped:>5}")
            if m.note:
                out.append(f"      -> {m.note}")
            # What the module said about its own run, after the scorer's reason.
            # A module that declined cases as inapplicable is rated over the
            # ones it asked, so a reader comparing its rate against the
            # catalogue needs the reason here and not only in the stage log.
            for own in m.notes:
                out.extend(textwrap.wrap(own, width=74,
                                         initial_indent="      note: ",
                                         subsequent_indent="            "))
            # `observations` is a different field from `notes` and both print.
            # A note is the module explaining its own rate; an observation is a
            # weight-zero finding from a module whose whole job is to report one,
            # and it is not about the rate at all.  Printed under its row rather
            # than in a section of their own, so that a reader looking at a rate
            # they do not understand finds the explanation in the same place.
            for obs in m.observations:
                out.append(f"         . {obs}")
    else:
        out.append("")
        seen = v.uncredited.get("behavioural") or {}
        # The other branch's one-line summary of the same discarded stage, printed
        # under whichever header line follows.  It was being written into
        # score.json and rendered nowhere, and a field that is written and never
        # printed reads to a reader exactly like a field that was never written --
        # which is the failure the fix was for.  It is not a duplicate of the
        # header: it names the module-ok count and the check counts in one
        # sentence, and on the path where the stage did not complete it is the
        # only line that says so in the stage's own words.
        summary_line = v.unscored_stages.get("behavioural") or ""
        # What the stage recorded about itself.  `ladder._record_stage_failure`
        # writes it for a stage that left no result file at all, and
        # `_report_behavioural_only` copies it onto the verdict.  Read before the
        # branch because it decides which branch is honest, not just what it says.
        stage_error = str((v.metadata.get("behavioural") or {}).get("error") or "").strip()
        # "Did stage 2 measure anything", which is not the same question as "is
        # there an uncredited entry for stage 2".  `_record_unscored` writes an
        # entry for any result it is handed, including one whose status is `error`
        # with no units -- and it grades that through a scratch verdict, so the
        # entry arrives complete, with `rate: 0.0` and `points: 0.0`.  Keying the
        # branch on the entry's presence alone therefore sent a stage that measured
        # nothing to the branch that prints "rate 0.0000 measured, not credited":
        # a zero the modules never took, reading exactly like a tree that failed
        # every check.
        #
        # Two fields, not one, and the second is why: the module ROWS are dropped
        # by the `uncounted` spelling, which stores a count instead -- so a
        # verdict re-read from a JSON carrying only that key has a real
        # measurement and no rows, and asking for rows alone calls it unmeasured.
        # A count of zero and no rows is the only state that means neither.
        #
        # `_reported` rather than `len`, and shared with the scorer rather than
        # re-derived: the suite now seeds a row per declared module before the first
        # one runs, so a table full of `unreached` rows is a stage that measured
        # nothing while looking, by row count alone, like a stage that measured
        # everything.  This branch and `Verdict.uncounted` have to answer that
        # question the same way -- one prints the line and the other sets the flag the
        # arithmetic sentence keys on, and a disagreement puts "measured nothing"
        # above a table of modules or the reverse.
        measured = (_reported(seen.get("modules"))
                    or bool(seen.get("module_count")))
        if not seen:
            # Stage 2's own account of itself first, and `blocked_by` only when it has
            # none.  These two answer different questions -- why the ladder stopped
            # crediting, and why this stage produced nothing -- and `blocked_by` was
            # standing in for both.  On a failed-gate run it names the gate, so a
            # stage that never started printed "NOT RUN (the audit gate failed)"
            # over a copy-in that broke or a stage that overran its own timeout: a
            # sentence consistent with the verdict, contradicted by nothing on the
            # page, and wrong.  Twelve runs in the 0807 campaign published it.
            #
            # A cause the harness observed outranks one inferred from a policy id.
            reason = stage_error or _BLOCKED.get(
                v.blocked_by, v.blocked_by or "an earlier stage stopped the ladder")
            # "NOT RUN" only when nothing ran.  A stage that ran and left no result --
            # the timeout case -- is not the same fact, and calling it NOT RUN is what
            # made three hours of grading disappear from lang04's report.
            label = "NO RESULT" if stage_error else "NOT RUN"
            out.append(f"  stage 2  behavioural modules ............. {label} "
                       f"({reason})")
            summary_line = ""   # nothing was credited; saying otherwise here is the old bug
        elif "rate" not in seen:
            out.append("  stage 2  behavioural modules ............. RAN, NOT COUNTED "
                       f"(could not be summarised: {seen.get('error', '')})")
            out.append(f"           {seen.get('note', '')}")
            if summary_line:
                out.append(f"           the stage reported: {summary_line}")
        elif not measured:
            # The stage started and got no further: an entry exists, complete with
            # the 0.0 rate the scratch verdict produced from nothing, and no module
            # ever ran.  It gets its own branch rather than joining either
            # neighbour, because both of those would state something false -- the
            # first that the stage never started, the last that its modules
            # measured zero -- and the difference decides whether the next person
            # re-runs the stage or reads the submission's code.
            #
            # Preference order: what the stage recorded about itself, then what its
            # own scoring made of that, then the ladder's blocker.  The last is a
            # fallback and not the answer: on these runs it names the audit
            # gate, which is why the stage was not credited and not why it produced
            # nothing.
            own = str(seen.get("harness_error") or "")
            if not own and seen.get("blocked_by"):
                own = _BLOCKED.get(str(seen["blocked_by"]), str(seen["blocked_by"]))
            reason = stage_error or own or _BLOCKED.get(
                v.blocked_by, v.blocked_by or "an earlier stage stopped the ladder")
            out.append("  stage 2  behavioural modules ............. NO RESULT "
                       f"({reason})")
            # Kept here, unlike on the never-ran branch: it is the stage's own
            # account of how far it got -- "ran but did not complete
            # (status=error)" -- and it claims no rate.
            if summary_line:
                out.append(f"           the stage reported: {summary_line}")
            for line in seen.get("notes") or []:
                out.extend(textwrap.wrap(str(line), width=74,
                                         initial_indent="           - ",
                                         subsequent_indent="             "))
        else:
            # It ran.  Saying NOT RUN here discarded the only measurement of what
            # the submission actually does -- and a task author reading a failed
            # gate needs it, because whether the port works and whether it is
            # honest are separate questions with separate fixes.
            #
            # Both numbers, because they answer different questions and one of
            # them is not a measurement: `points` is what the policy produced for
            # this stage, and the rate is what the modules measured.  A required
            # module at zero makes them disagree on purpose, which is what the
            # lines below explain.
            out.append(f"  stage 2  behavioural modules ............. RAN, NOT COUNTED  "
                       f"{float(seen.get('points', 0.0)):.2f} points uncredited  "
                       f"(rate {float(seen['rate']):.4f} measured, not credited)")
            # "reported, not counted" in those words on purpose: the number and its
            # disavowal have to arrive in one breath, because a reader who adds the
            # figure above to the total gets a score the benchmark does not award.
            out.append("           stage 1 failed, so by policy this earns "
                       "nothing; the rate is shown because the stage ran -- it is "
                       "reported, not counted.")
            if summary_line:
                out.append(f"           the stage reported: {summary_line}")
            # Stage 2's own verdict on itself.  The audit gate is not the only
            # thing that can zero this stage, and without this line a reader
            # infers "fix stage 1 and this becomes N points" from a stage that
            # would have earned nothing anyway.
            # The audit ids are excluded: that stop is the gate's and the line
            # above has already said so, so printing it again here would read as a
            # second and independent reason, when stage 2 in fact ran and measured
            # the rate printed above it.
            stopped = str(seen.get("blocked_by") or "")
            if stopped and not stopped.startswith("audit"):
                out.append(f"           stage 2 also stopped on its own account: "
                           f"{_BLOCKED.get(stopped, stopped)}.")
            for line in seen.get("notes") or []:
                out.extend(textwrap.wrap(str(line), width=74,
                                         initial_indent="           - ",
                                         subsequent_indent="             "))
            mods = seen.get("modules") or []
            out.append("")
            out.extend(_module_header())
            for m in sorted(mods,
                            key=lambda x: (-float(x.get("weight", 0)),
                                           str(x.get("id", "")))):
                c = m.get("checks") or {}
                # Derived from the rate when the column is absent, so a verdict
                # written before it renders the same way a fresh one does.  The
                # `scored_weight` term is what keeps a module that scored nothing --
                # rate 0.0 by definition -- from reading as complete.
                done = _done(float(m.get("weight", 0)),
                             bool(m.get("complete",
                                        float(m.get("rate", 0)) >= 1.0
                                        and float(m.get("scored_weight", 0)) > 0)))
                out.append(f"    {str(m.get('id',''))[:26]:<26} "
                           f"{float(m.get('weight',0)):>7.2f} "
                           f"{float(m.get('rate',0)):>8.4f} "
                           f"{done:>8} "
                           f"{int(c.get('passed',0)):>7} "
                           f"{int(c.get('failed',0)):>6} "
                           f"{int(c.get('errored',0)):>5} "
                           f"{int(c.get('skipped',0)):>5}")
                if m.get("note"):
                    out.append(f"      -> {m['note']}")
                for own in m.get("notes") or []:
                    out.extend(textwrap.wrap(str(own), width=74,
                                             initial_indent="      note: ",
                                             subsequent_indent="            "))

    # -- stage 3 ------------------------------------------------------------
    if "verification" in v.stages_run:
        out.append("")
        out.append(f"  stage 3  verification probe .............. "
                   f"{v.verification_points:.2f} points  "
                   f"({v.adversaries_survived}/{v.adversaries_total} models found "
                   f"no valid breaking test)")
        for b in v.verification_breaks[:12]:
            out.append(f"           BROKEN BY {b['adversary']}: {b['summary']}")
            for line in str(b.get("detail", "")).splitlines()[:8]:
                out.append(f"                 {line}")
    else:
        # Three situations reach this branch, distinguishable without guessing:
        # the ladder was blocked (``blocked_by`` names where); the harness broke
        # (``harness_error`` names what); or nothing stopped anything and stage 3
        # simply left no result -- skipped by the driver, or never collected.
        # ``grade_verification`` records that last one in
        # ``metadata.verification_missing`` and appends its own note, which a single
        # `blocked_by or "an earlier stage stopped the ladder"` fallback directly
        # contradicted by blaming an earlier stage that had not stopped anything.
        out.append("")
        u = v.unscored.get("verification") or {}
        if v.blocked_by:
            # The shared table, not a second copy of it.  The two were identical
            # when they merged, and the way that stops being true is one of them
            # gaining a rung the other does not have.
            reason = _BLOCKED.get(v.blocked_by, v.blocked_by)
        elif v.harness_error:
            reason = v.harness_error
        else:
            reason = ("no verification result was produced -- no stage-3 result "
                      "was collected; no earlier stage blocked it")
        if u.get("counted") is False:
            out.append(f"  stage 3  verification probe .............. RAN, NOT COUNTED  "
                       f"{u.get('points', 0.0):.2f} points  "
                       f"({u.get('models_survived', 0)}/{u.get('models_total', 0)} "
                       f"models found no valid breaking test)")
            out.append(f"           not counted: {reason}.")
            # A second line rather than a longer first one: `test_unscored_stages`
            # pins that sentence ending at the reason, and it is right to -- the
            # cause and the disavowal are two different things to say.
            out.append("           the figure above is reported, not counted.")
        elif v.unscored_stages.get("verification"):
            # The same fix as the branch above, reached through the other field.
            # `unscored` carries the numbers and `unscored_stages` one sentence,
            # and a stage-1 failure fills both -- but a driver that ran stage 3
            # past an incomplete stage 2 fills only the second, and without this the
            # line below would say NOT RUN about a result file sitting beside the
            # report.  Two branches fixed that sentence in two places each.
            out.append("  stage 3  verification probe .............. RAN, NOT COUNTED "
                       f"({reason})")
            out.append(f"           {v.unscored_stages['verification']}")
        else:
            out.append(f"  stage 3  verification probe .............. NOT RUN ({reason})")

    # -- arithmetic ---------------------------------------------------------
    out.append("")
    out.append(THIN)
    if v.audit_gate != "pass":
        out.append(f"  score = 0.00  (stage 1 {gate.lower()})")
        # `measured`, not merely present.  This sentence offers a reader something
        # specific -- that a number exists and policy declined to add it -- and a
        # stage that ran without measuring anything has no such number.  Saying it
        # anyway sends the reader looking for the figure it promises.
        ran = [label for key, label in (("behavioural", "stage 2"),
                                        ("verification", "stage 3"))
               if (v.uncounted.get(key) or {}).get("measured")]
        if ran:
            # This is the line a reader quotes, so it is the line that has to
            # reconcile with the figures above it.
            out.append(f"  {' and '.join(ran)} ran: those measurements are excluded "
                       f"from that total by policy, not missing from it.")
    elif "verification" not in v.stages_run:
        # "uncounted", not "not run", when a result for it is on disk -- the
        # arithmetic line must agree with the stage line four rows above it.
        #
        # `v.uncounted`, which is the field the stage line eight rows above reads.
        # This asked `v.metadata` instead, and nothing writes "uncounted" there:
        # `metadata` only ever receives "audit"/"behavioural" and the
        # behavioural_measured_* keys, so reading `metadata` here would leave the
        # condition constantly False and render a stage 3 that ran and measured its
        # 60 points as "not run" beside its own result file.  A reviewer reading
        # that re-runs six model-hours that already ran.
        why = "not counted" if "verification" in v.uncounted else "not run"
        out.append(f"  score = {v.behavioural_points:.2f} (stage 2)"
                   f" + 0.00 (stage 3 {why}) = {v.score:.2f}")
    else:
        out.append(f"  score = {v.behavioural_points:.2f} (stage 2)"
                   f" + {v.verification_points:.2f} (stage 3) = {v.score:.2f}")
    out.append(THIN)

    if v.notes:
        out.append("")
        out.append("notes")
        for note in v.notes:
            out.append(f"  - {note}")

    # Which grader produced this.  The stage images take the harness from a
    # mutable tag, so a rebuilt donor changes the grader with nothing else in the
    # record moving; the fingerprint is what separates two runs that disagree.
    # Read off the stage results rather than computed here, because the number
    # that matters is the one that graded, not the one rendering.
    graders = sorted({str(h["fingerprint"])
                      for h in v.harnesses if h.get("fingerprint")})
    if graders:
        out.append("")
        out.append(f"  graded by swerefactor {', '.join(graders)}"
                   + ("   *** stages disagree on the harness ***"
                      if len(graders) > 1 else ""))
    out.append("")
    return "\n".join(out)


def _done(weight: float, complete: bool) -> str:
    """The `done` cell: did this row pass everything it scored?

    A weight-0 module reports without judging, so it can neither carry the stage nor
    block it, and printing `no` against one would invite a reader to hunt for a
    failure that does not exist.  `-` is that row saying the column does not apply.
    """
    if weight <= 0:
        return "-"
    return "ok" if complete else "NO"


def _module_header() -> list[str]:
    """The module table's column head.

    `done` is the only column that scores.  The stage is all-or-nothing, so it pays
    in full exactly when every weighted row reads `ok`, and a single `NO` is the
    whole explanation of a 0.00 sitting above a table of rates near 1.00.

    `rate` is `pass / (pass + fail + err)` over the module's scoring checks: each
    counts once, so the row can be read as a fraction with no weight arithmetic in
    between.  Observation checks -- declared weight 0 -- are in none of those columns.
    It is reported and not paid; it exists so a reader can tell a port that fails one
    case from one that never built, which the points cannot say.

    `weight` is not a price either.  It is the one bit `done` needs: positive means
    this module is one the stage has to see pass, 0 means it only reports.
    """
    return [f"    {'module':<26} {'weight':>7} {'rate':>8} {'done':>8} "
            f"{'pass':>7} {'fail':>6} {'err':>5} {'skip':>5}"]


def _evidence_line(ev: dict) -> str:
    if not isinstance(ev, dict):
        return str(ev)[:200]
    path = ev.get("path") or ev.get("file") or ""
    line = ev.get("line")
    where = f"{path}:{line}" if path and line else (path or "")
    quote = (ev.get("quote") or ev.get("note") or ev.get("excerpt") or "").strip()
    quote = quote.replace("\n", " ")[:160]
    grounded = ev.get("grounded")
    mark = ""
    if grounded is False:
        mark = "  [UNVERIFIED CITATION]"
    return f"{where}  {quote}{mark}".strip()
