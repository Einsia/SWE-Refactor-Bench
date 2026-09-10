"""The pytest plugin every comparison module runs under.

Loaded with ``-p srbfixtures`` from ``lib/`` on ``PYTHONPATH`` rather than as a
``conftest.py``, for the same reason fw01 does it: a conftest is picked up by
rootdir discovery, and a module's rootdir is its own directory.  A plugin on the
path is found the same way from all sixteen of them.

What it provides is deliberately narrow -- the two recordings, and the routing of
this module's share of the corpus onto its tests.  In particular there is **no
fixture that hands a module the submitted tree**.  A module here compares
responses; a question about source belongs to stage 1, which reads both trees and
executes neither.

The parametrisation is done by hook rather than by decorator so a battery function
reads as one assertion about one case.  A test that declares ``case`` is run once
per case this module owns; one that declares ``session`` is run once per session
whose per-session facts this module asserts.
"""

from __future__ import annotations

import os

import pytest

import battery
import replay
import routing


def _module_id() -> str:
    module_id = os.environ.get("SRB_MODULE_ID", "")
    if not module_id:
        raise pytest.UsageError(
            "SRB_MODULE_ID is unset. These modules are parametrised from the "
            "routing table by module id, so run them through the suite runner "
            "(`swerefactor behavioural`) rather than by invoking pytest directly.")
    return module_id


# --------------------------------------------------------------------------- #
# The recordings
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def golden() -> dict:
    return replay.load_golden()


@pytest.fixture(scope="session")
def run(request) -> dict:
    """The submission's recording, or a failure that names the build module.

    Raising ``UsageError`` here rather than failing each test individually is
    deliberate: if the build module did not publish a recording there is exactly
    one finding, and reporting it 600 times buries it.  The module still reports
    every one of its checks as failed, because the runner scores a module that
    wrote no checks as an error, and either way the stage pays nothing: a weighted
    module that did not pass every scored check closes stage 2.
    """
    try:
        return replay.load_actual()
    except replay.MissingRecording as exc:
        pytest.fail(str(exc), pytrace=False)


@pytest.fixture(scope="session")
def expected(golden):
    return replay.lookup(golden, golden=True)


@pytest.fixture(scope="session")
def actual(run):
    return replay.lookup(run, golden=False)


@pytest.fixture(scope="session")
def module_id() -> str:
    return _module_id()


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def pytest_generate_tests(metafunc):
    """Give each battery this module's share of the corpus.

    The axis is chosen by the fixture name the battery declares -- ``case`` for
    every case the module owns, ``html_case`` for the ones State A rendered a page
    for, ``archive_case`` for the ones it archived, and so on.  ``battery.AXES``
    holds the predicates; keeping them there rather than here means the same table
    is used to parametrise the tests and to assert at build time that every module
    grades the axes it has cases on.

    An empty axis is never expected: ``battery.self_check`` fails the image build
    if a module imports a battery it has no cases for, precisely because pytest
    would report the empty parameter set as a skip and the runner scores an
    unlicensed skip as a miss.
    """
    names = set(metafunc.fixturenames)
    module_id = _module_id()
    for axis, select in battery.AXES.items():
        if axis not in names:
            continue
        pairs = select(module_id, replay.load_golden())
        metafunc.parametrize(
            f"session_id,{axis}",
            [(s.id, c) for s, c in pairs],
            ids=[f"{s.id}::{c.id}" for s, c in pairs],
        )
        return
    if "session" in names:
        sessions = routing.sessions_for(module_id)
        metafunc.parametrize("session", sessions,
                             ids=[s.id for s in sessions])


def pytest_report_header(config):
    module_id = os.environ.get("SRB_MODULE_ID", "?")
    surface = routing.SURFACE_BY_ID.get(module_id)
    if surface is None:
        return f"srb: module {module_id} (no routed cases)"
    return (f"srb: module {module_id} -- {surface.title}: "
            f"{len(routing.cases_for(module_id))} cases, "
            f"{len(surface.sessions)} session(s)")
