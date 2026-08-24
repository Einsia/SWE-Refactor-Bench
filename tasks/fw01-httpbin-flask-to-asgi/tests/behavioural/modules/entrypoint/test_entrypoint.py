"""The service starts the way it says it starts, and the thing that answers is
the new server.

Three checks, all of them observations of a running process.  Only the first is
scored, and the split is the whole point of the module:

    starts from a published entry point   weight 1   verification
    the answering server is not retired   weight 0   detection
    no retired module is importable       weight 0   detection

"Does this project publish a working way to start itself" is a behavioural
question with the same answer before and after a migration -- State A publishes
a Procfile that works, and so must anything claiming to have replaced it.  "Is
the thing that answered gunicorn" and "does the artefact still import Flask" are
questions about *which stack*, and State A answers both of them the wrong way by
definition.  Scoring them here would mean the recorded repository could not pass
a suite recorded from it, and a failing suite would then no longer tell anyone
whether behaviour changed.  Stage 1 asks them, with both trees open.

What is deliberately not read here is the tree: the text of ``httpbin.bash``, the
``web:`` line of the Procfile, the ``CMD`` of the Dockerfile, the ``[project]``
table, the size of a module, the number of ``add_route`` calls.  Those are in
``tests/audit/modules/entrypoint/``, where a reviewer with both trees
open reads them and where a hit is a lead rather than a deduction.

Nor is any particular launcher required.  Asserting ``server.how ==
"httpbin.bash"`` would dock a submission whose service starts fine through the
console script its own ``pyproject.toml`` declares, which is a published entry
point by any reading.  So the fixture tries each published way in turn, most
published first, and the check below fails only on ``srbfixtures.FALLBACK`` -- the
harness's own ``uvicorn`` line, which nothing in the project publishes.
"""

from __future__ import annotations

import importlib
import sys

import pytest

import srbfixtures

pytestmark = pytest.mark.migration

RETIRED_MODULES = ["flask", "werkzeug", "flasgger", "quart", "gunicorn",
                   "gevent", "eventlet", "greenlet", "asgiref", "a2wsgi",
                   "six", "decorator"]


def test_service_starts_from_a_published_entry_point(server):
    """Something the project publishes has to be able to start it.

    ``httpbin.bash``, a console script installed by the built distribution, or the
    Procfile's ``web:`` line -- any of the three. The harness's own
    ``uvicorn httpbin:app`` is not one of them: it is what the fixture falls back
    to so the rest of the suite can still run, and needing it means the release
    closure was left behind by the migration.
    """
    assert server.how != srbfixtures.FALLBACK, (
        f"the service only started under the harness's own uvicorn line. "
        f"Nothing the project publishes worked:\n  "
        + "\n  ".join(server.attempts or ["(no candidate was even tried)"])
    )


@pytest.mark.srb_weight(0.0)
def test_the_server_that_answered_is_not_the_retired_one(server, http, base_url):
    """Which server answered, from the wire.

    The ``Server`` header is out of contract for byte comparison -- the corpus
    normalises it away -- but it is still the running process describing itself,
    and a process that describes itself as gunicorn or Werkzeug is the retired
    stack answering requests.

    Note what is not asserted: that the header *says* uvicorn. A submission
    entitled to put its own name there is not migrating any less.

    Weight 0: this is *detection*, and State A fails it by construction. Its
    Procfile runs gunicorn, so the honest answer for the unmigrated repository
    is "gunicorn answered" -- which is a true and useful observation, and not a
    behavioural regression. Stage 2 grades behaviour through the interface, and
    behaviour is identical whichever server carries it. Stage 1 is where a
    submission still serving from the retired stack is caught, with both trees
    open. Kept here because the wire is the only place this can be seen at all:
    a reviewer reading the recorded verdict learns something a static read of
    the tree cannot tell them.
    """
    r = http.get(base_url + "/status/200")
    header = r.headers.get("server", "")
    for retired in ("gunicorn", "werkzeug", "waitress", "mod_wsgi"):
        assert retired not in header.lower(), (
            f"the process answering requests identifies itself as {header!r}, "
            f"which is the retired stack still serving (started via "
            f"{server.how})"
        )


@pytest.mark.srb_weight(0.0)
def test_no_retired_module_is_importable_from_the_built_distribution():
    """Import the installed package and see what comes with it.

    This runs in the venv the ``install`` module built, so what is imported is the
    built distribution rather than the submitted tree -- the same reason the
    ``closure`` module lives in this stage. It catches what a static read cannot:
    a module pulled in at run time, through ``importlib``, a plugin registry or a
    conditional fallback.

    Stage 1 asks the same question of the import graph without executing
    anything. Both answers are useful, and they are not the same question: this
    one is about the artefact, that one is about the source.

    Weight 0, for the same reason as the check above and the three like it in
    ``closure``: importing State A loads Flask, because State A *is* Flask. That
    is the definition of the starting point, not a defect in it. What is measured
    here is whether the migration happened, and stage 2 is not allowed to answer
    that -- if it does, the unmigrated repository cannot pass a suite whose
    expectations were recorded from the unmigrated repository, and then a failure
    no longer means the behaviour changed.
    """
    for name in ("httpbin", "httpbin.core"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            pytest.fail(
                f"import {name} failed in the venv built from the submission: "
                f"{type(exc).__name__}: {exc}"
            )
    loaded = sorted(m for m in RETIRED_MODULES if m in sys.modules)
    assert not loaded, (
        f"importing the installed application loads the retired modules "
        f"{loaded}, so they are still on a live code path"
    )
