"""The retired stack must be gone from what the build produced.

Every check here is a question about the venv the ``install`` module just built
from the submitted source, asked through ``importlib.metadata`` and
``importlib.util``: what did pip resolve, what is importable, what does the built
distribution declare it needs.

That makes them measurements of a build rather than readings of a repository. The
distinction matters because the build is the thing this stage exists to compare:
the target wheelhouse contains no Flask, so a project that still needs it cannot
be installed here at all, and "``flask`` is not importable in the venv" is a fact
about the artefact, not a grep. The nine checks that *were* greps -- imports,
vendored copies, WSGI tokens, manifest text, directory names -- are in
``tests/audit/modules/closure/``, where a reviewer reads them.
"""

from __future__ import annotations

import importlib.metadata as md
import re

import pytest

pytestmark = pytest.mark.migration

RETIRED = [
    "flask", "werkzeug", "flasgger", "quart", "six", "decorator",
    "gunicorn", "gevent", "eventlet", "greenlet", "asgiref", "a2wsgi",
]

#: Modules whose import means the retired stack is back, keyed by distribution.
RETIRED_MODULES = {
    "flask": ["flask"],
    "werkzeug": ["werkzeug"],
    "flasgger": ["flasgger"],
    "quart": ["quart"],
    "six": ["six"],
    "decorator": ["decorator"],
    "gunicorn": ["gunicorn"],
    "gevent": ["gevent"],
    "eventlet": ["eventlet"],
    "greenlet": ["greenlet"],
    "asgiref": ["asgiref"],
    "a2wsgi": ["a2wsgi"],
}

# ---------------------------------------------------------------------------
# G2: the installed closure
# ---------------------------------------------------------------------------

@pytest.mark.srb_weight(0.0)
@pytest.mark.parametrize("dist", RETIRED)
def test_retired_distribution_is_not_installed(dist):
    """After the verifier's own offline install, none of these may be present.

    Recorded at weight 0.0: State A declares Flask, and State A is the capture every
    scored expectation in this stage was recorded from.  ``flask_retired`` in stage 1
    owns the judgement.  See the module docstring.
    """
    try:
        version = md.version(dist)
    except md.PackageNotFoundError:
        return
    pytest.fail(
        f"{dist} {version} is installed in the verifier environment, which "
        f"means the project still declares it (directly or transitively)"
    )


@pytest.mark.srb_weight(0.0)
@pytest.mark.parametrize("dist", RETIRED)
def test_retired_module_is_not_importable(dist):
    """Recorded at weight 0.0.  See `test_retired_distribution_is_not_installed`."""
    import importlib.util
    for module in RETIRED_MODULES[dist]:
        spec = None
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ValueError):
            spec = None
        assert spec is None, (
            f"the module {module!r} is importable (from {spec.origin}), so the "
            f"retired {dist} stack is still reachable at runtime"
        )


def test_httpbin_is_installed_as_a_distribution():
    """The project itself must still install under its own name and version."""
    version = md.version("httpbin")
    assert version == "0.10.2", (
        f"the version must stay 0.10.2 (this is a migration, not a release), "
        f"got {version}"
    )


@pytest.mark.srb_weight(0.0)
def test_declared_requirements_are_within_the_allowlist():
    """Every declared dependency must be installable from the wheelhouse.

    Recorded at weight 0.0: the allowlist excludes Flask, which State A declares, so
    this asks whether the migration happened rather than whether the service behaves.
    ``flask_retired`` in stage 1 owns it.  See the module docstring.
    """
    allowed = {
        "starlette", "uvicorn", "jinja2", "python-multipart", "brotlicffi",
        "pyyaml", "setuptools", "wheel", "pytest", "pytest-timeout", "httpx",
        # transitive, and present in the wheelhouse
        "anyio", "idna", "sniffio", "click", "h11", "certifi", "httpcore",
        "markupsafe", "cffi", "pycparser", "typing-extensions", "exceptiongroup",
        "iniconfig", "packaging", "pluggy", "tomli", "httpbin", "brotlicffi",
        "h2", "hpack", "hyperframe", "uvloop", "httptools", "watchfiles",
        "websockets", "python-dotenv", "pyyaml", "colorama",
        # Declared before the migration and nothing to do with the web
        # framework: a marker that never applies on this interpreter, and the
        # test-matrix runner. Removing them is not part of this task, so they
        # are not held against a submission. The retired stack is caught by
        # test_retired_distribution_is_not_installed instead, which is what the
        # list above is for.
        "importlib-metadata", "tox",
    }
    requires = md.requires("httpbin") or []
    offenders = []
    for spec in requires:
        name = re.split(r"[\s\[<>=!;(]", spec.strip(), 1)[0].lower()
        if not name:
            continue
        if name.replace("_", "-") not in allowed:
            offenders.append(spec)
    assert not offenders, (
        f"httpbin declares dependencies outside the offline allowlist: "
        f"{offenders}"
    )


# ---------------------------------------------------------------------------
# What this module does not read
# ---------------------------------------------------------------------------
#
# Everything above the line is a question about the venv the `install` module
# built: what pip resolved, what is importable, what metadata the built
# distribution declares.  Those are facts about a build, and a build is what this
# stage is for.
#
# The submitted tree is not read here at all -- no imports by AST, no vendored
# source markers, no WSGI protocol tokens, no directory names.  Those are in
# `tests/audit/modules/closure/`, and the reason is that a grep for
# `PATH_INFO` in a `.py` file needs no venv and no server, and its meaning depends
# entirely on which line it is on.  Scored here, a submission loses points for a
# comment.  Stage 1 reads both trees with no toolchain at all and hands its
# findings to a reviewer who can open the file, which is the right reader for a
# string match.
