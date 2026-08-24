"""Has the retired stack left the tree, and left what the tree declares?

Advisory.  Nothing in this file can fail the audit gate: the gate is the five
prose questions in evaluation.toml, and this module's output reaches the reviewer
as findings to check rather than as verdicts to inherit.  That distinction is why
these questions are asked here rather than in stage 2.

The clearest case for it is the WSGI scan below.  ``PATH_INFO`` in a delivered
``.py`` file is a real lead: the most common way to fake this migration is to
reimplement the WSGI environ dict under a new name.  It is also what you write in
a comment that says *we no longer build a WSGI environ*, and in a docstring
explaining what changed, and in a test that asserts the old key is absent.
Scoring on the token punishes the third case exactly as hard as the first.
Reporting it, to a reader who can open the file, costs the third case nothing.

Same for directory names.  ``httpbin/static/flasgger/`` is worth looking at, and
it is also where a careful migration might keep the Swagger UI assets it still
has to serve, under the name upstream shipped them with.  A reviewer can tell
those apart in one read; a string match cannot tell them apart at all.

The exemptions instruction.md publishes are honoured: prose, templates and static
assets are not scanned.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

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

#: Text that only appears in a copied Flask/Werkzeug/flasgger source tree -- with
#: one exception worth naming, because the header would otherwise be read as a
#: guarantee.  ``"flasgger"`` is a bare distribution name, not a code shape: it
#: matches a URL path, an endpoint name, a comment about the migration.  A tree
#: that still serves its Swagger assets from `/flasgger_static/` matches on the
#: URL -- a true finding, since §4 of the brief requires a path not named after
#: the retired package, but not a vendored copy.  It arrives under this test's
#: name, and the reviewer reclassifies it from the citation, which is what the
#: prompt asks for.  ``swag_from`` and ``APISpec(`` and ``class Swagger(`` are the
#: entries that do what the header says.
VENDORED_SOURCE_MARKERS = [
    "class Flask(",
    "from .wrappers import Request",
    "werkzeug.exceptions",
    "werkzeug.wrappers",
    "def make_wsgi_app",
    "class BaseRequest",
    "LocalProxy",
    "class Headers(",
    "flasgger",
    "swag_from",
    "APISpec(",
]

#: WSGI protocol surface.  Its presence is a lead, not a finding: see the module
#: docstring.
WSGI_SURFACE = [
    "start_response", "wsgi.input", "wsgi.url_scheme", "wsgi.errors",
    "PATH_INFO", "QUERY_STRING", "REQUEST_METHOD", "SERVER_PROTOCOL",
    "HTTP_HOST", "CONTENT_LENGTH\"", "wsgi_app", "WSGIRequestHandler",
]

MANIFEST_FILES = [
    "pyproject.toml", "requirements.txt", "setup.cfg", "setup.py",
    "MANIFEST.in", "Dockerfile", "Procfile", "tox.ini", "runtime.txt",
    "app.json", "now.json", ".dockerignore",
]


# ---------------------------------------------------------------------------
# What the code imports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dist", RETIRED)
def test_no_source_file_imports_the_retired_stack(dist, repo):
    offenders = {}
    for path, rel in srbscan.python_files(repo):
        hit = srbscan.imported_names(path) & set(RETIRED_MODULES[dist])
        if hit:
            offenders[rel] = sorted(hit)
    assert not offenders, (
        f"these files import the retired {dist} stack: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_no_dynamic_import_of_the_retired_stack(repo):
    """``__import__("flask")`` and ``importlib.import_module`` too."""
    pattern = re.compile(
        r"""(?:__import__|import_module)\s*\(\s*['"]([\w.]+)['"]"""
    )
    retired = {m for ms in RETIRED_MODULES.values() for m in ms}
    offenders = {}
    for path, rel in srbscan.python_files(repo):
        for match in pattern.finditer(srbscan.read(path)):
            if match.group(1).split(".")[0] in retired:
                offenders.setdefault(rel, []).append(match.group(1))
    assert not offenders, (
        f"these files import a retired module dynamically: "
        f"{json.dumps(offenders, indent=2)}"
    )


@pytest.mark.parametrize("marker", VENDORED_SOURCE_MARKERS)
def test_no_vendored_copy_of_the_retired_stack(marker, repo):
    """Copying Flask or flasgger into the tree is not retiring it."""
    offenders = []
    for path, rel in srbscan.source_files(repo):
        if marker in srbscan.read(path):
            offenders.append(srbscan.cite(path, marker, rel))
    assert not offenders, (
        f"the marker {marker!r} appears in {offenders}. In a vendored copy of "
        f"the retired stack this is the finding; in a comment about the "
        f"migration it is not. Open the file."
    )


@pytest.mark.parametrize("token", WSGI_SURFACE)
def test_no_wsgi_protocol_surface(token, repo):
    """Reimplementing WSGI is not migrating away from it.

    Advisory in the strongest sense: this is the check whose scoring version did
    the most damage, and the whole reason it says "look here" instead of "you
    failed". A hit in a live code path means the protocol came back under a new
    name. A hit in a comment, a docstring or a test means someone documented that
    it is gone.
    """
    offenders = []
    for path, rel in srbscan.python_files(repo):
        if token in srbscan.read(path):
            offenders.append(srbscan.cite(path, token, rel))
    assert not offenders, (
        f"the WSGI protocol token {token!r} appears in {offenders}. Whether that "
        f"is a reimplemented environ dict or a comment saying there is no longer "
        f"one is a question for the reader"
    )


# ---------------------------------------------------------------------------
# What the tree declares
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dist", RETIRED)
def test_manifests_do_not_name_the_retired_stack(dist, repo):
    """Packaging metadata, the pin file, the Dockerfile and the Procfile."""
    offenders = {}
    for name in MANIFEST_FILES:
        path = repo / name
        if not path.exists():
            continue
        for lineno, line in enumerate(srbscan.read(path).splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.search(rf"(?<![\w-]){re.escape(dist)}(?![\w-])",
                         stripped, re.IGNORECASE):
                offenders.setdefault(name, []).append(f"{lineno}: {stripped}")
    assert not offenders, (
        f"{dist} is still named in the project's manifests: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_no_directory_is_named_after_the_retired_stack(repo):
    """A lead about vendoring, and sometimes just about asset naming.

    The Swagger UI assets are part of the product and have to be served from
    somewhere. Upstream served them from a flasgger directory, so a submission
    that kept the layout and rewrote the code that serves them will show up here
    and be fine. One that has ``vendor/werkzeug/`` will show up here and not be.
    """
    offenders = []
    for path in repo.rglob("*"):
        rel = str(path.relative_to(repo))
        if rel.startswith((".git/", "__pycache__")) or "/__pycache__" in rel:
            continue
        # setuptools' copy of a path this list already holds is not a second
        # lead: `build/lib/httpbin/templates/flasgger` beside the real
        # `httpbin/templates/flasgger` is one asset directory and two findings,
        # and the duplicate exists only because the brief's §7 says to install
        # the project.  `srbscan.is_generated` is the same predicate the text
        # walk uses, so the two cannot disagree about what counts as delivered.
        if srbscan.is_generated(rel):
            continue
        lowered = path.name.lower()
        if any(dist in lowered for dist in RETIRED):
            offenders.append(rel)
    assert not offenders, (
        f"these paths are named after the retired stack: {sorted(offenders)}. "
        f"A vendored copy and a kept asset directory both look like this"
    )


def test_ci_and_tox_reference_the_new_stack(repo):
    """The automation must have moved too, not just the package."""
    checked = []
    path = repo / "tox.ini"
    if path.exists():
        checked.append(("tox.ini", srbscan.read(path)))
    for wf in sorted((repo / ".github" / "workflows").glob("*.y*ml")):
        checked.append((str(wf.relative_to(repo)), srbscan.read(wf)))
    assert checked, "no tox.ini and no CI workflow survived the migration"

    offenders = {}
    for rel, text in checked:
        for dist in RETIRED:
            if re.search(rf"(?<![\w-]){dist}(?![\w-])", text, re.IGNORECASE):
                offenders.setdefault(rel, []).append(dist)
    assert not offenders, (
        f"the automation still references the retired stack: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_ported_test_suite_still_exists(repo):
    """The project's own tests should have been ported, not deleted."""
    tests = repo / "tests"
    assert tests.is_dir(), (
        "the project's tests/ directory is gone; the suite was to be ported, "
        "not removed"
    )
    files = list(tests.rglob("test_*.py"))
    assert files, f"tests/ contains no test modules: {sorted(tests.iterdir())}"
    total = sum(len(srbscan.read(p).splitlines()) for p in files)
    assert total > 200, (
        f"the ported suite is only {total} lines across {len(files)} files, "
        f"which is far smaller than the original; it looks emptied rather than "
        f"ported"
    )


def test_requirements_pin_the_new_stack_with_hashes(repo):
    """The project's Dockerfile installs with ``--require-hashes``.

    Whether the pins are correct is settled by stage 2, where the install either
    works against the grader's wheelhouse or does not. What is read here is
    whether the file still describes the new stack at all.
    """
    path = repo / "requirements.txt"
    assert path.exists(), "requirements.txt is missing"
    text = srbscan.read(path)
    entries = [line for line in text.splitlines()
               if line.strip() and not line.strip().startswith("#")]
    assert entries, "requirements.txt is empty"
    assert "--hash=sha256:" in text, (
        "requirements.txt carries no hashes, but the project's Dockerfile "
        "installs it with --require-hashes"
    )
    named = {n.lower() for n in re.findall(r"^([A-Za-z0-9._-]+)==", text,
                                           re.MULTILINE)}
    assert named, f"requirements.txt has no pinned versions: {entries[:5]}"
    missing = [n for n in ("starlette", "uvicorn") if n not in named]
    assert not missing, (
        f"requirements.txt does not pin {missing}: {sorted(named)}"
    )
