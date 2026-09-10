"""The suite-wide pytest plugin: licensed skips, and the one hard requirement.

Loaded by `lib/run-pytest.sh` for every module, alongside
`swerefactor.pytest_module`, which is what turns the collected tests into the
module contract's JSON.  This file adds the two things that plugin cannot know
about pf02.

**Licensed skips.**  `swerefactor.pytest_module` scores an unlicensed skip as a
failure, and it is right to: the environment is fixed and offline, so a check that
declines to run has usually found nothing rather than excused itself.  The licence
is the `srb_skip_ok` marker, which is normally applied at collection time.  Two of
this suite's skips cannot be decided then:

  * State A itself does not compile a particular upstream input.  A few of the
    `.styl` files upstream ships are inputs to tests that expect an error, or
    depend on a platform detail; which ones is a fact about State A, discovered by
    running it, and a submission that also declines to compile them is not wrong.
  * a driver produced no result for a case that State A also produced no result
    for -- the same fact, one layer out.

Neither is evidence about the submission, so neither is scored either way.
`permitted_skip()` writes a licence into the skip reason and the report hook below
turns it into the marker the scorer looks for, which is what makes a decision
taken mid-test count the same as one taken at collection.

The rule that keeps this honest: **`permitted_skip()` is for something State A
did, never for something the submission did.**  A submission whose adapter cannot
run a case, whose CLI exits non-zero, or whose core fails to link has failed that
check.  Excusing it would let a broken submission score above a partial one that
at least tried, which inverts the ranking the stage exists to produce.  Nothing
here can enforce that -- it is a review rule, and the reason every call site names
whose behaviour it is describing.

**The one hard requirement.**  `require_core()` fails rather than skips when there
is no compiler core to drive at all.  Something has to be there; a tree with
neither the ported entry point nor the shape it was ported from has not attempted
the task, and that is a zero for the check rather than an exemption from it.

**Checks with no pre-migration analogue.**  Almost everything in this stage asks
what the compiler *does*, and a platform port must not change that -- which is why
`layout.face()` exists and why those rows are asked of whichever face the tree
presents.  Three checks are different: their subject is a mechanism the port
introduces -- a capability the core can be denied, a platform shape it must refuse,
the path module it must export -- so on a tree that has not been ported there is
nothing there to compare against.

Those three are asked of that face anyway, and charged there.  Not weighted to 0.0:
a weight decided at run time from what the tree turned out to be makes a module's
size depend on how far the submission got, and a weight-0 row is dropped when the
result is collected, so two trees would be scored out of different totals and
compared as though out of one.  This stage exists to compare them.  So the missing
mechanism is the answer rather than a reason not to ask: §1.1 asks for the export,
§1.3 for the refusal, §1.4 for the injected read, and a tree that has none of them
fails those three rows and passes the behavioural ones -- which is the ranking,
since the migration is the task.
"""
from __future__ import annotations

import pytest

from harness import layout

#: Marker on a skip reason that this plugin recognises.  A skip that arrives
#: without it remains a failure -- which is why nothing in this suite calls
#: `pytest.skip` directly.
#:
#: The marker travels in the reason string because that is the only channel that
#: survives every route a licensed skip can arrive by (see the report hook below).
#: It is not, on its own, how a reader learns why the skip was neutral: the reason
#: text pytest keeps is not a field the graded artifact carries.  The hook copies
#: it into `srb_note` for that, and `permitted_skip: true` records the licence.
LICENCE = "state-a-limitation"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "srb_skip_ok: a skip here is licensed by the suite contract")
    layout.ensure_dirs()


def permitted_skip(reason: str):
    """Skip neutrally: neither a pass nor a miss.

    Only for a fact about State A -- see this module's docstring.
    """
    pytest.skip(f"{LICENCE}: {reason}")


def require_core():
    """Fail -- not skip -- when there is no compiler to drive.

    A tree is measured on the face it presents (`layout.face()`), and this is the
    case where it presents neither: no `src/core/index.js` and no `lib/stylus.js`
    behind it.  Measured as absent rather than excused, so that a submission which
    built nothing cannot outscore one that built half.
    """
    if not layout.core_face_available():
        pytest.fail(
            f"there is no compiler core to drive: neither {layout.CORE_ENTRY}, which "
            f"the task makes the entry point of the port, nor {layout.LEGACY_ENTRY}, "
            "the entry point it is ported from. There is nothing here to run."
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Attach `srb_skip_ok` to a report whose skip carried the licence.

    The scorer reads marker names off `report.keywords`, and a marker added to the
    item after `pytest.skip` has unwound would never get there -- the report is
    built from the item as it was.  Writing the keyword onto the report itself
    covers every route a licensed skip can arrive by: raised in a test body, in a
    helper it called, or re-raised during setup for the second test to request a
    module-scoped fixture that skipped.
    """
    outcome = yield
    report = outcome.get_result()
    if report.outcome != "skipped":
        return
    reason = _skip_reason(call, report)
    if LICENCE not in reason:
        return
    try:
        report.keywords["srb_skip_ok"] = 1
    except TypeError:  # pragma: no cover - keywords is a dict in every pytest 7/8
        pass

    # And carry the reason into the artifact.  A licensed skip takes its weight out
    # of the module's *total* as well as its earned share -- the module is scored
    # out of what remains -- so it removes more from a score than a `weight: 0.0`
    # row does, and a row that costs that much has to say why.  Without this the
    # check arrives in `behavioural.json` as the bare word "skipped" beside
    # `permitted_skip: true`: a reviewer can see that the suite meant it, and
    # cannot see what it was about State A that licensed it.
    #
    # Purely additive.  The verdict is already `skip` and the weight is untouched,
    # so nothing here can move a score; `pytest_module` reads `srb_note` into
    # `metadata.note` and truncates it. The licence marker itself is dropped from
    # the text because `permitted_skip: true` already records that fact.
    detail = reason.split(f"{LICENCE}:", 1)[-1].strip() or reason.strip()
    props = getattr(report, "user_properties", None)
    if isinstance(props, list) and not any(
            name == "srb_note" for name, _ in props):
        props.append(("srb_note", f"licensed skip: {detail}"))


def _skip_reason(call, report) -> str:
    exc = getattr(call, "excinfo", None)
    value = getattr(exc, "value", None)
    msg = getattr(value, "msg", None)
    if isinstance(msg, str):
        return msg
    longrepr = getattr(report, "longrepr", None)
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr or "")
