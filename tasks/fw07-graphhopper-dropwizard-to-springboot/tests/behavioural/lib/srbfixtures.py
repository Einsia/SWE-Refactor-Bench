"""Fixtures shared by every behavioural module, loaded as a pytest plugin.

Each module in ``modules/`` is its own process with its own pytest run, so these
cannot live in a conftest.py only one of them can see.  They are a plugin on
``PYTHONPATH`` instead, and every module's ``run.sh`` loads it.

What a module gets
------------------
``capture_doc``  the ledger: both sides' answers to all 88 cases, published once
                 by the build module
``pair``         a factory — ``pair("route-plain-car").check()`` — resolving one
                 case id to both sides' answers plus the measured volatility
``cases_in``     a factory — ``cases_in("routing")`` — returning that suite's case
                 ids, used by each module's coverage guard

There is deliberately no ``repo`` fixture, and there is no way to add one
usefully: nothing in this stage's data holds source text.  A check here that
wanted to know how the submission was written would have to go and read the tree
itself, and would notice while doing so that it belongs in stage 1 — which reads
both trees and executes neither.  Stage 2's contract is narrower and completely
mechanical: build both sides, ask both the same questions, compare the answers.

The expensive work happens once.  Two servers times two profiles is four graph
imports with CH and LM preparations, which is most of this stage's wall clock; a
per-module replay would pay it eleven more times.  The build module runs the
whole matrix, writes the ledger to $SRB_SUITE_WORK, and every later module reads
it.  Safe because the runner executes modules strictly sequentially in
declaration order — one writer, finished before the first reader starts.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness import capture, corpus, diff

SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"))
DATA = SUITE / "data"


def _fail_hard(message: str) -> None:
    """Stop the module as a harness fault rather than as a wall of failures.

    A missing ledger is not a property of the submission, and reporting it as 22
    failed cases would publish a score for something that was never measured.
    Exit 70 is the runner's harness-fault code: the module's result becomes a
    re-run rather than a zero.
    """
    print(f"harness fault: {message}", flush=True)
    raise SystemExit(70)


def pytest_configure(config) -> None:
    """Check inputs once, before collection.

    Here rather than in a fixture: from a fixture the same error is re-raised per
    test, so one missing ledger becomes several hundred identical tracebacks.

    Exempt under --collect-only, and that exemption is load-bearing.  The stage
    image collects every module at build time to prove there is no import or
    parametrisation error, long before any submission exists and in an image
    that by design holds no ledger.  Nothing runs during collection.
    """
    if getattr(config.option, "collectonly", False):
        return
    path = capture.capture_path()
    if not path.is_file():
        _fail_hard(
            f"no capture at {path}.  The build module runs the corpus against "
            f"both sides and publishes it; it either did not run or did not "
            f"finish.  Every module after build depends on it.")


@pytest.fixture(scope="session")
def capture_doc() -> dict:
    """The ledger, with its corpus fingerprint checked against this process's.

    The fingerprint check is not ceremony.  A ledger cannot notice being asked
    about different requests than the ones it recorded: edit a target and every
    comparison still runs, still passes, and silently stops grading the
    behaviour the case exists for.  A mismatch is a harness fault, not a
    submission failure — the suite would not be measuring what it claims.
    """
    try:
        return capture.load()
    except RuntimeError as exc:
        _fail_hard(str(exc))


class Pair:
    """One case's two answers, and everything a failure report needs."""

    __slots__ = ("case_id", "suite", "profile", "port", "why", "reference",
                 "submission", "masked", "entry", "authorities")

    def __init__(self, case_id, entry, ref, sub, masked, authorities):
        self.case_id = case_id
        self.suite = entry["suite"]
        self.profile = entry["profile"]
        self.port = entry["port"]
        self.why = entry["why"]
        self.reference = ref
        self.submission = sub
        self.masked = masked
        self.entry = entry
        # The (reference, submission) port numbers this case was reached on, from
        # the ledger's meta.  The two differ on purpose; diff folds each side's
        # own out of the headers before comparing them.
        self.authorities = authorities

    def check(self) -> str:
        """The one comparison, for this case.

        Raises AssertionError through diff.compare_case on a difference, and
        returns a one-line summary of what matched on success.  The summary is
        printed rather than discarded so that a passing run still says what it
        compared: a case that passes because both sides answered 404 is worth
        having in the log, and it is invisible in a bare green dot.
        """
        summary = diff.compare_case(self.case_id, self.reference,
                                    self.submission, self.masked, self.entry,
                                    self.authorities)
        print(summary)          # already prefixed with the case id by diff
        return summary


@pytest.fixture
def pair(capture_doc):
    """A factory resolving one case id to its Pair.

    def test_car_route(pair): pair("route-plain-car").check()

    A factory rather than a parametrised fixture, and that follows from how the
    tests are written.  Every graded check in this suite is one named function per
    case — `test_bike_route_uses_landmarks` — because a named failure is a
    diagnosis and `test_case[14]` is a lookup table.  A parametrised fixture would
    force the opposite convention; a factory lets the test name carry the meaning
    while the resolution stays in one place.

    A case missing from the ledger, or one where either side never answered, fails
    the test that asked for it rather than the module: a submission whose server
    died on one endpoint should lose the cases that touch it, not the eighty that
    do not.  "Did not answer" and "answered differently" are distinguished in the
    message.
    """
    def resolve(case_id: str) -> Pair:
        try:
            ref, sub, masked, entry, auth = capture.case_pair(
                capture_doc, case_id)
        except RuntimeError as exc:
            pytest.fail(str(exc))
        return Pair(case_id, entry, ref, sub, masked, auth)

    return resolve


@pytest.fixture
def cases_in():
    """A factory returning one corpus suite's case ids, in corpus order.

    Used by each module's coverage guard rather than by the graded checks.  The
    guard compares its own test count against this, so a case added to the corpus
    and not to the module owning its suite fails loudly instead of being recorded,
    paid for in wall clock, and never compared.
    """
    def resolve(suite: str) -> list[str]:
        return [c["id"] for c in corpus.cases_for(suite)]

    return resolve
