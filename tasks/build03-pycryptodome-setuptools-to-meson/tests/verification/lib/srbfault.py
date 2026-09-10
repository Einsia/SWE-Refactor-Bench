"""Turn a build failure that escaped a candidate into an exit status.

Registered from run-candidate.sh with `-p srbfault`, and needed because this task
builds the tree INSIDE the candidate's own pytest process. Most tasks' stage 3
compiles in run-candidate.sh and can `exit 71` there; here a configuration is a
compiler shim rather than a build-system flag, so it is something a candidate asks
for by name -- `srbcrypto.tree("default")` -- and it builds a wheel and installs it
on first use. A tree that will not build raises `BuildFailed` at that call.

Without this plugin that raise is an AssertionError like any other, so pytest exits
1 and the harness reads "the candidate failed against this tree". Against the
submission that is a divergence -- passes on the original, fails on the submission,
reproducibly -- and six rounds would charge up to 60 points for a submission that
does not build. That is stage 2's finding, and stage 2 already scores it.

So a build failure that nothing caught becomes exit 71, which
swerefactor.verification.FAULT_EXIT_CODES (sysexits 64-78) reads as "this tree could
not be tested". The adjudicator answers `invalid` for it on whichever tree it
happened to be, which is the point: requiring a PASS on the original does not cover
this on its own, because a submission that will not build passes there.
docs/SCHEMA.md, "Condition 0: a tree that could not be tested", is the rule.

Two things it deliberately does not do:

  * it does not fire for a `BuildFailed` a candidate CAUGHT. srbcrypto documents
    catching it and asserting the message as the way to make a configuration itself
    the subject, and a candidate that does that and passes has run.
  * it does not fire when something else also failed. A failure that is not a build
    failure means something WAS learned about behaviour, so the candidate's verdict
    is its own. Only a run whose every failure was "it would not build" learned
    nothing -- which is also the shape the dangerous case actually has, because a
    submission that does not build fails every test that asks for a tree.
"""
from __future__ import annotations

import sys

# (module, class) -> exit status. Matched through the exception's MRO, so a
# candidate's own subclass of BuildFailed still counts as one; matched on the
# module as well as the name, so a candidate that defines its own class called
# BuildFailed does not.
FAULTS: dict[tuple[str, str], int] = {
    ("srbcrypto", "BuildFailed"): 71,
}

_seen: list[tuple[str, int]] = []
_other = 0


def _fault_for(exc: BaseException) -> tuple[str, int] | None:
    for cls in type(exc).__mro__:
        code = FAULTS.get((cls.__module__, cls.__name__))
        if code is not None:
            return f"{cls.__module__}.{cls.__name__}", code
    return None


def pytest_exception_interact(node, call, report):
    """Every failure, fixture error and collection error passes through here.

    Collection matters as much as the call phase: a candidate that asks for a tree
    at module level fails to import, which is pytest's exit 2 -- below the fault
    range, and so read as an ordinary candidate failure.
    """
    global _other
    if not getattr(report, "failed", False):
        return                      # an xfail or a skip: nothing failed
    excinfo = getattr(call, "excinfo", None)
    hit = _fault_for(excinfo.value) if excinfo is not None else None
    if hit is None:
        _other += 1
        return
    name, code = hit
    _seen.append((name, code))
    first = (str(excinfo.value).splitlines() or [""])[0]
    print(f"SRB-INFRA-FAULT: {node.nodeid}: {name}: {first}", file=sys.stderr)


def pytest_sessionfinish(session, exitstatus):
    if not _seen or exitstatus == 0:
        return
    # session.testsfailed as well as the local count: the two disagree only if a
    # failure reached the report without reaching this plugin, and an undercount
    # here would claim "nothing was learned" over a candidate that learned
    # something.
    others = _other or max(0, int(getattr(session, "testsfailed", 0)) - len(_seen))
    if others:
        print(f"SRB-INFRA-FAULT: {len(_seen)} build failure(s) and {others} other "
              f"failure(s); reporting the candidate's own verdict ({exitstatus}), "
              f"because something was learned about behaviour", file=sys.stderr)
        return
    code = _seen[0][1]
    print(f"SRB-INFRA-FAULT: every failure in this run was a build failure; "
          f"exiting {code} so the adjudicator reads this tree as untestable "
          f"rather than as a divergence", file=sys.stderr)
    session.exitstatus = code
