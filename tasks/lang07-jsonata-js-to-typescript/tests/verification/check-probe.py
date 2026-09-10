#!/usr/bin/env python3
"""Build-time check for stage 3: the probe manifest, and the harness end to end.

Runs inside the stage-3 image build, after the reference has been published and
State A unpacked.  Two halves, and the second is the one that matters.

The manifest half is what `swerefactor validate` does while a task is authored: an
adversary count that disagrees with the scoring policy, an empty scope, a
candidate command that is not there.  This is the backstop for a probe.toml
edited without re-validating.

The end-to-end half exists because of one asymmetry in how this stage scores.  A
candidate counts as a break only when it PASSES on the original and FAILS on the
submission.  So anything that stops the original from answering converts every
candidate into "does not pass on the original", no break is ever upheld, and the
submission is paid all sixty points.  A broken stage 3 does not fail loudly -- it
awards full marks, and it looks exactly like a submission nobody could break.

Reasoning about that is not enough, so this runs the real thing: candidate files,
through the real `run-candidate.sh`, against the real published reference.

  probe_ok        asserts things true of this engine.  Must PASS on the original.
  probe_diverges  asserts something false of it.       Must FAIL on the original.

Both directions, because only the pair distinguishes a working harness from one
that always passes or always fails.  A harness stuck at "pass" upholds nothing and
pays sixty points; one stuck at "fail" upholds everything and pays none.  The
first check catches the first and the second catches the second, and neither
catches both.

WHAT THIS FILE HAS THAT A COMPILED-REFERENCE TASK'S DOES NOT

On the other language pairs the submission cannot reach the reference because the
reference needs a toolchain the image withholds -- shim `dotnet` to exit 127 and a
C# file in plain sight answers nothing.  Here both sides are `node` and
node has to be present, so nothing can be withheld: `require`ing the published
reference is one line, and a submission that did it would answer every candidate
correctly without having ported anything.

The seal is therefore filesystem permissions and nothing else, which makes it the
single point of failure of the whole stage.  So it is not merely asserted here, it
is attacked: `check_seal_holds` builds a submission whose probe reaches for the
reference on purpose and requires that it fail.  A passing build of this image is
one where that attack was run and lost.

The other three submission-side checks cover the opposite failure, which is easy
to overlook: the submission's build and probe both run unprivileged, and if that
user cannot build, cannot traverse down to `dist/probe.js`, or cannot run node,
then every candidate fails on the submission, every candidate reads as a break,
and an honest port loses all sixty points.  Nothing else in this stage would
notice -- six rounds of upheld breaks is what a genuinely broken port looks like.

Unlike those tasks, the submission-side checks need no scaffold.  State A ships a
package.json with a `build` script and a working `src/probe.js`, so State A *is* a
valid submission behaviourally -- which is the fact the whole task is built around,
and it makes the positive control one copytree instead of three written files.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.  Not read from
#: there: evaluation.toml is one level up, outside this build context.
#: `swerefactor validate` compares against the real thing.
EXPECTED_ADVERSARIES = 6

STAGE = Path("/tests/verification")
#: The published reference.  The whole `dist/` and not just the one file, because
#: `dist/probe.js` requires its three siblings; a check that only looked for
#: probe.js would pass on a directory the reference cannot run out of.
REFERENCE_DIR = Path("/opt/reference")
REFERENCE = REFERENCE_DIR / "dist" / "probe.js"

#: The unprivileged users, as run-candidate.sh names them.
REF_USER = "srbref"
PROBE_USER = "srbprobe"

# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
# Everything asserted below was established by running the built reference and
# pasting what it said, not from memory.  Four claims, chosen so that a harness
# which somehow answered one by accident would still have to answer the rest:
# the handshake's own advertisement, the encoding's treatment of undefined, an
# error payload's field set, and per-evaluation state under `repeat`.
CANDIDATE_OK = '''
import srbjsonata


def test_hello_advertises_six_ops():
    hello = srbjsonata.op("hello")
    assert hello.ok, hello.text()
    assert hello.result["protocol"] == "jsonata-probe/1", hello.text()
    assert hello.result["ops"] == [
        "assign", "ast", "eval", "evalcb", "hello", "register"], hello.text()


def test_undefined_is_dropped_from_a_structure_not_tagged():
    bare = srbjsonata.op("eval", "$nope")
    inside = srbjsonata.op("eval", "[1, $nope, 3]")
    assert bare.ok and inside.ok, (bare, inside)
    assert bare.results == [["u"]], bare.text()
    assert inside.results == [["a", [["d", 1], ["d", 3]]]], inside.text()


def test_absent_input_is_not_the_same_answer_as_null_input():
    absent = srbjsonata.op("eval", "$exists($)")
    null = srbjsonata.op("eval", "$exists($)", input=None)
    assert absent.ok and null.ok, (absent, null)
    assert absent.results == [["b", False]], absent.text()
    assert null.results == [["b", True]], null.text()


def test_a_signature_failure_carries_index_and_type():
    record = srbjsonata.op("eval", '$sum("x")')
    assert not record.ok, record.text()
    assert record.kind == "JsonataError", record.text()
    assert record.code == "T0412", record.text()
    assert list(record.error) == [
        "kind", "code", "position", "token", "value", "index", "type",
        "message"], record.text()


def test_a_counter_counts_across_repeats_of_one_expression():
    # `repeat` exists to make per-evaluation state observable: one compiled
    # expression, evaluated more than once, with whatever it accumulated intact.
    record = srbjsonata.op(
        "register", "$c()", funcs=[{"name": "c", "impl": "counter"}], repeat=3)
    assert record.ok, record.text()
    assert record.results == [["d", 1], ["d", 2], ["d", 3]], record.text()
'''

# The same shape, asserting something false.  Nothing subtle: `1+1` does not fail.
# A harness that reports this as a pass is a harness that reports everything as a
# pass.
CANDIDATE_DIVERGES = '''
import srbjsonata


def test_a_claim_that_is_false_of_the_original():
    record = srbjsonata.op("eval", "1+1")
    assert not record.ok, (
        "this assertion is deliberately wrong: evaluating `1+1` succeeds. If "
        "this test passes, the harness is not running the probe.")
'''

# The cheapest true thing there is, for the runs whose point is the exit code
# rather than the assertion.
CANDIDATE_HELLO_ONLY = '''
import srbjsonata


def test_the_probe_answers_hello():
    hello = srbjsonata.op("hello")
    assert hello.ok, hello.text()
    assert hello.result["protocol"] == "jsonata-probe/1", hello.text()
'''

# --- The attack --------------------------------------------------------------
# A submission whose probe delegates to the published reference.  This is the
# cheat the seal exists to stop, written out rather than described: one require of
# an absolute path, and every candidate in every round would be answered by the
# engine the submission was supposed to have ported.
#
# `src/probe.js` and not a new file, because State A's own `tools/build.js` walks
# the require graph from `src/probe.js` and copies what it reaches -- so this is
# the edit that actually ships into `dist/`.  It also means the failure lands at
# build time rather than at probe time: build.js loads every module it copies, so
# the require is resolved on the near side of `npm run build`.  Both are a loss for
# the attack, which is why the check below asserts on the shape of the denial and
# the non-zero exit rather than on one particular exit code.
#
# What node actually says here is worth writing down, because it is not what the
# obvious version of this check expects.  The seal is 0700 on the *directory*, so
# `require` cannot stat the path at all, and node's resolver reports that as
# `MODULE_NOT_FOUND` -- "Cannot find module" -- rather than as EACCES.  The errno
# is only visible to something that opens the file directly, which is what the
# first half of `check_seal_holds` does and why it is there: on its own, "cannot
# find module" is also what an image with no reference in it would say.
CHEAT_PROBE_PREFIX = """'use strict';
// Written by check-probe.py: the cheat the seal exists to stop.
var stolen = require('/opt/reference/dist/jsonata.js');
if (typeof stolen !== 'function') { throw new Error('not the reference'); }
"""

def log(message: str) -> None:
    print(f"check-probe: {message}", flush=True)


# --------------------------------------------------------------------------- #
# Half one: the manifest
# --------------------------------------------------------------------------- #


def check_manifest() -> list[str]:
    problems: list[str] = []
    probe = config.Probe.load(STAGE / "probe.toml")

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        problems.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}")
    if not probe.scope.allow and not probe.scope.deny:
        problems.append(
            "probe.toml [scope] states neither allow nor deny, so every "
            "divergence counts and no submission can survive a round")
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")

    seen: set[str] = set()
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        if adv.id in seen:
            # Two rounds under one id are one row in the report, so the second
            # one's result overwrites the first and ten points vanish.
            problems.append(f"adversary id {adv.id!r} appears twice")
        seen.add(adv.id)

    if problems:
        return problems

    log(f"probe.toml: {len(probe.adversaries)} adversaries, "
        f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
        f"{len(probe.scope.deny)} out")
    return []


# --------------------------------------------------------------------------- #
# Running one candidate the way the harness does
# --------------------------------------------------------------------------- #


def run_candidate(role: str, code: str, work: Path, name: str,
                  wrapper: tuple[str, str] | None = None,
                  patch: dict[str, str] | None = None) -> tuple[int, str]:
    """Run one candidate through the real script, as the harness would.

    `wrapper` is (script text, first argument) and inserts a shell between here
    and `run-candidate.sh`, for the traced run in `check_role_gating`.  The
    default is no wrapper, so the other checks invoke the script exactly as the
    harness does.

    `patch` is {relative path: contents} written into the tree copy before the run,
    which is how `check_seal_holds` turns State A into a cheating submission.
    """
    candidate = work / f"{name}.py"
    candidate.write_text(code, encoding="utf-8")

    # The harness stages a *copy* of each tree.  Copied here too: the script
    # deletes dist/ and node_modules from what it is given, and doing that to
    # /opt/original would leave the image's State A altered for whatever runs
    # next.  `copytree` preserves the 0700 the Dockerfile set, which is what the
    # submission branch's own chown then works from.
    tree = work / f"tree-{name}"
    shutil.copytree("/opt/original", tree, symlinks=True)
    for rel, contents in (patch or {}).items():
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")

    env = dict(os.environ)
    env.update({
        "SRB_CANDIDATE": str(candidate),
        "SRB_CANDIDATE_NAME": name,
        "SRB_TARGET": str(tree),
        # A stand-in for the harness's sha256(salt:role).  Any opaque string
        # does: what it must not be is the word "original".
        "SRB_TARGET_TOKEN": f"buildcheck{name}",
        "SRB_WORK": str(work / "run"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
    })
    for leaked in ("SRB_TARGET_NAME", "SRB_TARGET_ROLE", "SRB_ORIGINAL"):
        env.pop(leaked, None)

    # The command probe.toml declares, not a path spelled out here.  Spelling it
    # out is how this check passed for as long as probe.toml said
    # `["bash", "run-candidate.sh"]`: the harness runs that with cwd set to a copy
    # of the tree, where there is no such file, so every candidate came back exit
    # 127 while this check -- naming the script absolutely -- ran it happily.  A
    # self-test that does not use the graded command cannot see a broken one.
    command = config.Probe.load(STAGE / "probe.toml").candidate_command
    if wrapper is None:
        argv = list(command) + [role]
    else:
        text, first = wrapper
        script = work / f"{name}-wrapper.sh"
        script.write_text(text, encoding="utf-8")
        argv = ["bash", str(script), first] + list(command) + [role]

    proc = subprocess.run(
        argv, cwd=str(tree), env=env, capture_output=True, text=True, timeout=900,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-40:])
    return proc.returncode, tail


# --------------------------------------------------------------------------- #
# Half two: the reference path, and the script that gates it
# --------------------------------------------------------------------------- #


def _is_pure_assignment(trace_line: str) -> bool:
    """Is this xtrace line only a variable assignment?

    `REFERENCE=/opt/reference/dist/probe.js` is a trace line naming the reference
    and is not a use of it: the submission branch never expands the variable.
    Excluded so the check reports reaching the reference rather than mentioning it.

    Narrow deliberately.  `PATH=... setpriv ... node ...` is an assignment *prefix*
    on a command and is a use, so the rule is that after stripping every leading
    assignment nothing may remain.  A filter that merely looked for `=` in the line
    would hide the case this whole function exists to catch.
    """
    body = trace_line.lstrip("+").strip()
    while True:
        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*=(\S*)\s*", body)
        if not match:
            return False
        body = body[match.end():]
        if not body:
            return True


def _is_the_seal_check(trace_line: str) -> bool:
    """Is this the submission branch testing that it *cannot* read the reference?

    That check names the reference and must: it is `setpriv ... sh -c "test -r
    '/opt/reference/dist/probe.js'"`.  It is the one command in the submission
    branch allowed to name the path, and letting it through is not a hole -- the
    pattern requires `test -r`, which reads a mode and never opens the file, and
    requires the setpriv drop, so it cannot match a privileged read.

    Recognised by shape rather than by an exact string, because the trace renders
    the quoting its own way and an exact match here would silently stop matching
    after any edit to that line -- passing the gating check by finding no hits at
    all, on a script that had stopped sealing anything.  `check_seal_is_present`
    is the other half: this function permits the line, that one requires it.
    """
    body = trace_line.lstrip("+").strip()
    return bool(
        re.search(r"\bsetpriv\b", body)
        and re.search(r"--reuid=" + re.escape(PROBE_USER), body)
        and re.search(r"\btest\s+-r\b", body)
    )


def check_role_gating(work: Path) -> list[str]:
    """The reference must be unreachable when the role is `submission`.

    Established from an execution trace rather than by reading the script.  The
    obvious version of this check greps for `/opt/reference` and requires every hit
    to sit inside the `original` branch, which means locating that branch, which
    means matching `if`/`else`/`fi` in shell -- and a hand-rolled brace matcher
    that gets it wrong reports a clean file as broken or a broken one as clean.

    So: run the submission role with xtrace on, and require that no command bash
    actually executed named the reference, except the seal check itself.  That is a
    statement about what runs, which is the thing that matters, and it needs no
    parser.

    Its limit is real and worth stating: a trace covers the paths this run took.
    What makes the coverage adequate here is that the run goes all the way through
    the submission branch and out the far end -- State A builds, produces
    `dist/probe.js`, answers the handshake, answers the candidate, and exits 0.
    The exit code is asserted below to keep that true, because a run that died at
    `npm run build` would trace the first third of the branch and this check would
    then be quietly making a claim about code it never reached.

    That State A can play the submission here without a scaffold is the fact this
    whole task rests on: a rename is behaviourally the original, so "was it
    rewritten" is stage 1's question and not this stage's.  A task whose State A is
    not already a valid submission needs a scaffold, because bare State A has no
    package.json and the build stops on the first line of the branch.
    """
    problems: list[str] = []

    # A trace on fd 9 rather than stderr: the script logs to stderr itself, and
    # separating the two keeps "bash ran this" apart from "the script said this".
    trace = work / "submission.trace"
    # The interpreter comes off the front of the graded command rather than being
    # written in here, so that this trace follows probe.toml wherever it points.
    # Everything after it is passed through untouched, which is what makes the
    # traced run the same run the other checks make.
    wrapper = (
        'exec 9>"$1"\n'
        'export BASH_XTRACEFD=9\n'
        'shift\n'
        'interpreter="$1"\n'
        'shift\n'
        'exec "$interpreter" -x "$@"\n'
    )
    rc, tail = run_candidate(
        "submission", CANDIDATE_HELLO_ONLY, work, "gating",
        wrapper=(wrapper, str(trace)))

    if not trace.is_file():
        return ["the xtrace file was never written, so this check established "
                "nothing; BASH_XTRACEFD did not survive into the script"]

    traced = trace.read_text(encoding="utf-8", errors="replace")
    hits = [ln for ln in traced.splitlines()
            if ("/opt/reference" in ln or "reference/dist" in ln)
            and not _is_pure_assignment(ln)
            and not _is_the_seal_check(ln)]
    if hits:
        problems.append(
            "a submission-role run executed a command naming the reference:\n    "
            + "\n    ".join(hits[:5])
            + "\n  A submission probed through the published reference answers "
              "every candidate correctly and is paid all sixty points without "
              "having ported anything.")
    else:
        log(f"submission role: {len(traced.splitlines())} traced commands, "
            f"none reaching the reference")

    # 0 is the whole branch: built, handshake answered, candidate run, exit clean.
    # Asserted so that the trace above keeps meaning "the whole branch ran". A
    # failure at `npm run build` (71), a missing dist/probe.js (72), a probe that
    # will not shake hands (73) or a seal that did not hold (74) each stop the
    # branch early, and the trace would then cover less than this function claims
    # while still finding no reference -- a pass for the wrong reason, which is the
    # failure mode worth guarding.
    if rc != 0:
        problems.append(
            f"a submission-role run of State A exited {rc}, expected 0 (built, "
            f"answered the handshake, passed the candidate). 71 is the build, 72 "
            f"a missing dist/probe.js, 73 the handshake, 74 a seal that did not "
            f"hold. The reference trace above only covers the part of the "
            f"submission branch that ran, and this exit code is what says that "
            f"was all of it. Output:\n{tail}")
    else:
        log("submission role: State A builds, answers and exits 0 -- so the "
            "branch was traced from end to end")

    return problems


def check_seal_is_present(work: Path) -> list[str]:
    """The submission branch must actually run the seal check.

    `check_role_gating` permits the seal-check line through its filter.  Nothing
    there requires it to exist, and a script that had stopped sealing would pass
    that check by producing no hits at all -- the strongest possible clean result
    for the weakest possible reason.  So this asserts the presence of what that one
    tolerates, from the same trace, and the two together say: exactly one command
    on the submission side names the reference, and it is the one that proves the
    reference cannot be read.
    """
    trace = work / "submission.trace"
    if not trace.is_file():
        return ["no trace to check the seal against; check_role_gating must run "
                "first"]
    traced = trace.read_text(encoding="utf-8", errors="replace")

    seal_lines = [ln for ln in traced.splitlines() if _is_the_seal_check(ln)]
    if not seal_lines:
        return ["a submission-role run never tested whether it could read the "
                "reference. The seal is the only thing standing between a "
                "submission and a working implementation of the engine it is "
                "compared against -- node is in this image by necessity, so "
                "nothing else prevents a `require`. Without this check a seal "
                "broken by an image edit would go unnoticed and every round "
                "would be paid out."]

    trees_lines = [ln for ln in traced.splitlines()
                   if re.search(r"\bsetpriv\b", ln)
                   and re.search(r"\bls\b", ln)]
    if not trees_lines:
        return ["a submission-role run never tested whether it could list the "
                "harness's staged trees. The other tree is SRB_TARGET's sibling "
                "for the whole run and is a second working engine, so that "
                "directory is the second half of the seal."]

    log(f"submission role: the seal is checked in both halves "
        f"({len(seal_lines)} read check, {len(trees_lines)} list check)")
    return []


def check_seal_holds(work: Path) -> list[str]:
    """Attack the seal, and require the attack to lose.

    Two attacks, from the outside in.

    The first is the bare filesystem question, asked as the user that would be
    asking it: can $PROBE_USER open the reference?  Node rather than `test -r`
    because a read is what a cheat performs, and the errno it comes back with is
    the positive evidence -- ENOENT would mean the reference is not where this file
    thinks it is, and a check that accepted "could not read it" without looking at
    why would pass on an image with no reference in it at all.

    The second runs the cheat for real: State A with a `require` of the reference
    spliced into `src/probe.js`, through `run-candidate.sh`, as a submission.  It
    must not reach a passing candidate.  Where it fails is not pinned -- exit 71 at
    the build is what happens today, since `tools/build.js` loads every module it
    copies -- and what is pinned is that it failed and that the denial named the
    reference.

    The first without the second would establish a mode and not a consequence.  The
    second without the first would accept any failure at all as success, including
    one caused by the reference being absent -- which matters more here than it
    looks, because the seal's 0700 is on the directory, so node cannot stat the path
    and reports "Cannot find module" rather than a permission error.  The two
    failures are spelled identically from inside the run; only the first check tells
    them apart.
    """
    problems: list[str] = []

    probe_js = REFERENCE_DIR / "dist" / "jsonata.js"
    reader = (
        "try { require('fs').readFileSync(%r); process.stdout.write('READ'); } "
        "catch (e) { process.stdout.write(String(e.code || e.message)); }"
        % str(probe_js)
    )
    proc = subprocess.run(
        ["setpriv", f"--reuid={PROBE_USER}", f"--regid={PROBE_USER}",
         "--init-groups", "--", "node", "-e", reader],
        capture_output=True, text=True, timeout=120,
    )
    said = (proc.stdout + proc.stderr).strip()
    if said != "EACCES":
        problems.append(
            f"{PROBE_USER} reading the reference said {said!r}, expected "
            f"'EACCES'. 'READ' means the seal is off and a submission can "
            f"`require` the engine it is graded against; anything else -- "
            f"'ENOENT' in particular -- means this check is not testing what it "
            f"claims, because there is nothing at {probe_js} to be denied.")
    else:
        log(f"{PROBE_USER} cannot open the reference: EACCES, not ENOENT")

    # The cheat, end to end.
    tree_probe = (Path("/opt/original") / "src" / "probe.js").read_text(
        encoding="utf-8")
    cheat = CHEAT_PROBE_PREFIX + tree_probe
    rc, tail = run_candidate(
        "submission", CANDIDATE_HELLO_ONLY, work, "cheat",
        patch={"src/probe.js": cheat})

    if rc == 0:
        problems.append(
            "a submission that requires the published reference built and "
            "answered. That submission would pass every candidate in all six "
            "rounds and be paid all sixty points without having ported "
            "anything, and stage 2 would not catch it either -- it answers "
            "correctly because it *is* the reference. This is the failure the "
            f"seal exists to prevent. Output:\n{tail}")
    # The denial has to name the reference.  Anything else -- a build that broke
    # for its own reasons, a probe that died before the require -- is a failure
    # this check would otherwise read as a success.  Two spellings accepted
    # because the seal produces the second and a mode change on the files alone
    # would produce the first, and this check should keep working across that edit.
    elif not re.search(
            r"(EACCES|permission denied|Cannot find module).{0,120}"
            + re.escape(str(REFERENCE_DIR)),
            tail, re.IGNORECASE | re.DOTALL):
        problems.append(
            f"a submission that requires the published reference failed (exit "
            f"{rc}), but its output does not show the reference being denied -- "
            f"no permission error and no unresolvable module naming "
            f"{REFERENCE_DIR}. So something else stopped it, and this check has "
            f"not established that the seal works. Output:\n{tail}")
    else:
        log(f"a submission that requires the reference is denied by the seal "
            f"(exit {rc}, the path does not resolve)")

    return problems


def check_end_to_end(work: Path) -> list[str]:
    problems: list[str] = []
    if not REFERENCE.is_file():
        return [f"the published reference is not at {REFERENCE}; this check runs "
                f"after the reference stage and cannot do anything without it"]
    for sibling in ("jsonata.js", "probe-wire.js", "probe-fixtures.js"):
        if not (REFERENCE_DIR / "dist" / sibling).is_file():
            problems.append(
                f"the published reference has no dist/{sibling}, which "
                f"dist/probe.js requires -- so the reference will not start and "
                f"every round would be paid out")
    if problems:
        return problems

    rc, tail = run_candidate("original", CANDIDATE_OK, work, "ok")
    if rc != 0:
        problems.append(
            f"a candidate asserting true things about the engine FAILED on the "
            f"original (exit {rc}). Every candidate in every round would read as "
            f"'does not pass on the original', no break could be upheld, and the "
            f"submission would be paid all sixty points whatever it contains. "
            f"Output:\n{tail}")
    else:
        log("a true candidate passes on the original")

    rc, tail = run_candidate("original", CANDIDATE_DIVERGES, work, "diverges")
    if rc == 0:
        problems.append(
            "a candidate asserting something FALSE about the engine passed on the "
            "original, so the harness is not running the probe -- it is "
            f"reporting success without executing anything. Output:\n{tail}")
    else:
        log(f"a false candidate fails on the original (exit {rc})")

    return problems


def check_submission(work: Path) -> list[str]:
    """A working submission must be probeable, unprivileged and all.

    The positive control for the submission path.  Everything on that side runs as
    an unprivileged user -- `npm run build`, then node -- and the ways that can
    break are quiet: a work directory whose ancestors are mode 0700 so node cannot
    open its own script, an npm that has no writable HOME, a missing setpriv.  Each
    of those makes every candidate fail on the submission, which the adjudicator
    reads as every candidate breaking it, and an honest port loses sixty points.

    Not redundant with `check_role_gating`, which runs the same tree and candidate:
    that one inserts a wrapper shell to get `BASH_XTRACEFD` in, so it does not
    invoke `run-candidate.sh` the way the harness does.  This one does, with no
    wrapper, which is the only place that exact invocation is exercised.

    And it runs the full CANDIDATE_OK rather than the hello-only one, which makes
    it the check that State A passes on both sides of the comparison -- the same
    five claims, both roles. That is the differential identity the stage assumes
    and nothing else here states: a submission that changed nothing is broken by
    nothing.
    """
    problems: list[str] = []
    rc, tail = run_candidate("submission", CANDIDATE_OK, work, "working")
    if rc != 0:
        problems.append(
            f"State A FAILED the submission path (exit {rc}). The submission's "
            f"build and probe run as an unprivileged user; if that cannot work, "
            f"every candidate fails on every submission, the adjudicator upholds "
            f"all of them, and an honest port is scored zero for sixty points. "
            f"State A is behaviourally a valid submission, so the same candidate "
            f"that passes as the original must pass here. Output:\n{tail}")
    else:
        log("State A passes the same candidate on both sides, built and run "
            "unprivileged")
    return problems


def main() -> int:
    problems = check_manifest()
    with tempfile.TemporaryDirectory(prefix="check-probe-") as tmp:
        work = Path(tmp)
        problems += check_role_gating(work)
        problems += check_seal_is_present(work)
        problems += check_seal_holds(work)
        problems += check_end_to_end(work)
        problems += check_submission(work)

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    log("stage 3 harness verified end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())

