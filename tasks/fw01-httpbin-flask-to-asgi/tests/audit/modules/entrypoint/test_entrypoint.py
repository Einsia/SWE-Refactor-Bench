"""What the published entry points name, and whether a second implementation is
still in the tree.

Advisory.  Nothing here gates: see the module docstring in ``closure``.

The division of labour with stage 2 is the point of this module.  Stage 2 boots
the submission and asks it 437 questions; if the service does not start, or
starts and answers wrong, stage 2 says so and it costs points.  What stage 2 does
not do is require the boot to happen through one particular script.  Which script
starts it, what that script names, and whether the Procfile and Dockerfile agree,
are read here.

For the same reason the checks below are conditional on the file existing.  A
tree that moved start-up to its console script has not lost its entry point, and
a finding saying "httpbin.bash is missing" in the reviewer's prompt would invite
a fail on a gate about something else.  What is still worth reporting is a tree
that publishes *no* entry point at all.

``test_core_module_is_substantial`` and ``test_route_count_is_plausible`` are the
two weakest checks in the file and they stay because they are cheap and
occasionally decisive -- a 40-line ``httpbin/`` package cannot implement 52
documented paths, whatever else is true of it.  As a score they would be noise; as
a line in a report that says "this package is 60 lines, go and look", they are
worth the two seconds.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

RETIRED_TOKENS = ["flask", "werkzeug", "flasgger", "quart", "gunicorn",
                  "gevent", "eventlet", "greenlet", "asgiref", "a2wsgi",
                  "six", "decorator"]


def _read(path):
    return srbscan.read(path)


def _prose_spans(text, suffix):
    """Character ranges that are comment or string, so not executable code.

    Python goes through ``tokenize``, which is exact and also covers the
    docstring case -- a triple-quoted string is a STRING token, and a line-based
    ``#`` heuristic cannot see one.  Anything else gets the ``#`` rule, which is
    what those suffixes use.  A file that will not tokenize returns nothing: an
    unparseable file is reported rather than excused, and stage 1 has a separate
    check that every delivered Python file parses.
    """
    spans = []
    if suffix == ".py":
        import io
        import tokenize
        lines = text.splitlines(keepends=True)
        starts, off = [], 0
        for ln in lines:
            starts.append(off)
            off += len(ln)

        def pos(row, col):
            return (starts[row - 1] + col) if 0 < row <= len(starts) else 0

        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    spans.append((pos(*tok.start), pos(*tok.end)))
        except (tokenize.TokenError, IndentationError, SyntaxError):
            return []
        return spans
    off = 0
    for ln in text.splitlines(keepends=True):
        i = ln.find("#")
        if i >= 0:
            spans.append((off + i, off + len(ln)))
        off += len(ln)
    return spans


def _is_prose(text, index, suffix=".py", _cache={}):
    key = (id(text), len(text), suffix)
    spans = _cache.get(key)
    if spans is None:
        spans = _cache[key] = _prose_spans(text, suffix)
    return any(a <= index < b for a, b in spans)


def _launches_asgi(repo, text, _depth=0):
    """Does this launcher start the ASGI server, directly or by delegation?

    ``"uvicorn" in text`` alone would be a token check wearing a behaviour
    check's name.  A launcher may delegate: a ``Procfile`` reading ``web: python3
    -m httpbin.server``, an ``httpbin.bash`` ending ``exec "$PYBIN" -m
    httpbin.server``, and a ``httpbin/server.py`` that imports uvicorn and calls
    ``uvicorn.run()`` start the ASGI server correctly, and a token check reports
    both launchers as not starting it -- a finding against a submission for
    choosing one launcher module over duplicating the command, which is the better
    arrangement of the two.

    Enumerating the delegates is not the fix either; that is the same check with a
    longer list.  So the delegation is resolved instead: follow ``-m pkg.mod`` and
    a referenced in-tree script one hop, and let the module docstring's rule
    ("which script starts it is not this module's business") hold for how it starts
    it too.
    """
    if re.search(r"\buvicorn\b", text):
        return True
    if _depth >= 2:
        return False
    for mod in re.findall(r"-m\s+([A-Za-z_][\w.]*)", text):
        cand = repo / (mod.replace(".", "/") + ".py")
        pkg = repo / mod.replace(".", "/") / "__main__.py"
        for p in (cand, pkg):
            if p.exists() and _launches_asgi(repo, _read(p), _depth + 1):
                return True
    for ref in re.findall(r"([\w./-]+\.(?:bash|sh|py))", text):
        p = repo / ref.lstrip("./")
        if p.exists() and _launches_asgi(repo, _read(p), _depth + 1):
            return True
    return False


# ---------------------------------------------------------------------------
# The entry points
# ---------------------------------------------------------------------------

def test_the_project_publishes_a_way_to_start_itself(repo):
    """At least one of the three published entry points is present.

    Which one is not this module's business -- stage 2 and stage 3 both take the
    first that answers HTTP, in this order.  What would be worth reporting is a
    tree that publishes none, because then nothing but a hand-typed uvicorn line
    starts the service.
    """
    published = []
    script = repo / "httpbin.bash"
    if script.exists():
        published.append("httpbin.bash")
    proc = repo / "Procfile"
    if proc.exists() and re.search(r"^\s*web:", _read(proc), re.M):
        published.append("Procfile web:")
    pyproject = _read(repo / "pyproject.toml")
    if "[project.scripts]" in pyproject or "console_scripts" in pyproject:
        published.append("console script in pyproject.toml")
    assert published, (
        "the project publishes no way to start itself: no httpbin.bash, no web: "
        "line in a Procfile, and no console script in pyproject.toml"
    )


# There is deliberately no check on httpbin.bash's mode bit.  State A ships it
# 0644, the instruction never asks for the bit, and both runtime stages launch it
# as `bash ./httpbin.bash`, which does not need it.  A check for it would have
# reported a finding against every submission that left the file as upstream has
# it -- noise in the reviewer's prompt about a property nothing measures.


def test_httpbin_bash_if_present_launches_the_asgi_server(repo):
    script = repo / "httpbin.bash"
    if not script.exists():
        pytest.skip("this tree does not publish httpbin.bash")
    text = _read(script)
    assert _launches_asgi(repo, text), (
        f"httpbin.bash does not start the ASGI server, directly or through a "
        f"module it runs:\n{text}"
    )
    offenders = [t for t in RETIRED_TOKENS if re.search(rf"\b{t}\b", text, re.I)]
    assert not offenders, (
        f"httpbin.bash still references the retired stack {offenders}:\n{text}"
    )


def test_httpbin_bash_if_present_honours_host_and_port(repo):
    script = repo / "httpbin.bash"
    if not script.exists():
        pytest.skip("this tree does not publish httpbin.bash")
    text = _read(script)
    for var in ("HTTPBIN_HOST", "HTTPBIN_PORT"):
        assert var in text, (
            f"httpbin.bash no longer honours {var}; the contract keeps both, "
            f"defaulting to 0.0.0.0 and 8080"
        )
    assert "0.0.0.0" in text, "the default host 0.0.0.0 is gone"
    assert "8080" in text, "the default port 8080 is gone"


def test_procfile_launches_the_asgi_server(repo):
    path = repo / "Procfile"
    assert path.exists(), "the Procfile is missing"
    text = _read(path)
    assert _launches_asgi(repo, text) or "httpbin.bash" in text, (
        f"the Procfile does not start the ASGI server:\n{text}"
    )
    offenders = [t for t in RETIRED_TOKENS if re.search(rf"\b{t}\b", text, re.I)]
    assert not offenders, f"the Procfile still references {offenders}:\n{text}"


def test_dockerfile_runs_the_asgi_service(repo):
    path = repo / "Dockerfile"
    assert path.exists(), "the Dockerfile is missing"
    text = _read(path)
    offenders = [t for t in RETIRED_TOKENS if re.search(rf"\b{t}\b", text, re.I)]
    assert not offenders, (
        f"the Dockerfile still references the retired stack {offenders}"
    )
    assert "requirements.txt" in text, (
        "the Dockerfile no longer installs requirements.txt"
    )
    assert "--require-hashes" in text, (
        "the Dockerfile must keep installing requirements.txt with "
        "--require-hashes"
    )
    assert "httpbin.bash" in text or "uvicorn" in text, (
        f"the Dockerfile does not run the service:\n{text[-600:]}"
    )


def test_pyproject_describes_the_new_stack(repo):
    path = repo / "pyproject.toml"
    assert path.exists(), "pyproject.toml is missing"
    text = _read(path)
    assert 'name = "httpbin"' in text or "name = 'httpbin'" in text, (
        "the project name must stay httpbin"
    )
    assert "0.10.2" in text, (
        "the version must stay 0.10.2; this is a migration, not a release"
    )
    assert "setuptools" in text, "the build backend must stay setuptools-based"
    assert "starlette" in text.lower(), (
        "pyproject.toml does not declare starlette as a dependency"
    )
    offenders = [t for t in RETIRED_TOKENS if re.search(rf"\b{t}\b", text, re.I)]
    assert not offenders, (
        f"pyproject.toml still names the retired stack {offenders}"
    )


def test_mainapp_extra_describes_the_new_server(repo):
    """instruction.md keeps the ``mainapp`` extra, pointed at the new server."""
    text = _read(repo / "pyproject.toml")
    assert "mainapp" in text, "the mainapp extra is gone from pyproject.toml"
    block = text[text.index("mainapp"):][:400]
    assert "uvicorn" in block.lower(), (
        f"the mainapp extra does not describe the new server:\n{block[:300]}"
    )


def test_packaging_still_ships_data_files(repo):
    """Templates, static files and the UI assets have to be packaged."""
    blob = (_read(repo / "MANIFEST.in") + _read(repo / "setup.cfg")
            + _read(repo / "pyproject.toml"))
    assert blob.strip(), (
        "none of MANIFEST.in, setup.cfg or pyproject.toml survived"
    )
    assert ("templates" in blob or "package-data" in blob
            or "package_data" in blob or "include-package-data" in blob), (
        "no packaging directive mentions the data files; the templates and "
        "static assets will not ship"
    )


# ---------------------------------------------------------------------------
# No parallel implementation
# ---------------------------------------------------------------------------

def test_no_leftover_wsgi_entry_point(repo):
    """A second entry point that still serves the old app is not a migration."""
    offenders = {}
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if rel.startswith(".git/") or "__pycache__" in rel:
            continue
        if path.suffix not in (".py", ".bash", ".sh", ".cfg", ".ini", ".toml",
                               ".yml", ".yaml") and path.name not in (
                "Procfile", "Dockerfile"):
            continue
        text = _read(path)
        for pattern in (r"\bgunicorn\b", r"\bwsgi:application\b",
                        r"\bapp\.run\(", r"\bwsgi_app\b", r"WsgiToAsgi",
                        r"WSGIMiddleware"):
            for m in re.finditer(pattern, text):
                # Skip a match inside a comment or a string.  `\bgunicorn\b` is
                # the reason: a port can carry a dozen prose hits ("Headers
                # gunicorn mapped onto the request scheme") plus a helper named
                # `_gunicorn_style_error`, which exists to reproduce the error
                # pages stage 2 compares byte for byte, and have no WSGI surface
                # at all -- every `test_no_wsgi_protocol_surface` check passing on
                # the same tree this one reports.
                if _is_prose(text, m.start(), path.suffix):
                    continue
                offenders.setdefault(rel, []).append(pattern)
                break
    # The message says what was found, not what it implies.  "a WSGI entry point
    # or bridge survives" is a structural claim a token match cannot establish,
    # and a reviewer reading it in the findings prompt is being told a conclusion
    # the evidence does not carry -- the prompt's own instruction is to open the
    # file, which a definite claim discourages.
    assert not offenders, (
        f"these patterns appear outside comments and strings, which is how a "
        f"surviving WSGI entry point or bridge would look -- open each and "
        f"decide: {json.dumps(offenders, indent=2)}"
    )


def test_no_backup_copy_of_the_old_implementation(repo):
    """``core_old.py``, ``core.py.bak``, ``legacy/`` and friends."""
    suspicious = re.compile(
        r"(\.bak$|\.orig$|~$|_old\.py$|_flask\.py$|\.py\.save$"
        r"|(^|/)old_|(^|/)legacy_|(^|/)backup)", re.IGNORECASE
    )
    offenders = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if rel.startswith(".git/") or "__pycache__" in rel:
            continue
        # /legacy is a real httpbin route, so only path *names* are flagged.
        if suspicious.search(rel):
            offenders.append(rel)
    assert not offenders, (
        f"these look like retained copies of the old implementation: {offenders}"
    )


def test_app_module_is_not_a_thin_reexport(repo):
    """``httpbin/__init__.py`` must not just re-export something else's app."""
    text = _read(repo / "httpbin" / "__init__.py")
    assert text.strip(), "httpbin/__init__.py is empty"
    lowered = text.lower()
    for token in ("flask", "werkzeug", "flasgger", "wsgi"):
        assert token not in lowered, (
            f"httpbin/__init__.py mentions {token!r}:\n{text[:400]}"
        )


def test_core_module_is_substantial(repo):
    """The application still has to contain an implementation.

    State A's core.py is ~1800 lines. A port can be much shorter -- Starlette
    does more per line than Flask plus flasgger did -- so the threshold is set
    where "the routes went somewhere else, or nowhere" starts, not where "this is
    smaller than the original" starts.
    """
    package = repo / "httpbin"
    py_files = list(package.rglob("*.py"))
    assert py_files, "httpbin/ contains no Python modules"
    total = sum(len(_read(p).splitlines()) for p in py_files)
    assert total > 700, (
        f"the httpbin package is only {total} lines across {len(py_files)} "
        f"modules. State A is roughly 2500. This is too small to contain the "
        f"52 documented paths and their behaviours."
    )


def test_route_count_is_plausible(repo):
    """The routing table should still describe the whole surface."""
    package = repo / "httpbin"
    paths = set()
    pattern = re.compile(r"""["'](/[\w\-./{}<>:]*)["']""")
    for py in package.rglob("*.py"):
        for match in pattern.finditer(_read(py)):
            if len(match.group(1)) > 1:
                paths.add(match.group(1))
    assert len(paths) >= 40, (
        f"only {len(paths)} route-shaped literals appear in httpbin/: "
        f"{sorted(paths)[:20]}. State A documents 52 paths."
    )


def test_filters_no_longer_use_the_decorator_package(repo):
    """``filters.py`` was built on ``decorator``, which is retired."""
    for py in (repo / "httpbin").rglob("*.py"):
        text = _read(py)
        assert "from decorator import" not in text, (
            f"{py.relative_to(repo)} still imports the retired decorator package"
        )
        assert "@decorator" not in text, (
            f"{py.relative_to(repo)} still uses the retired decorator package"
        )


def test_no_retired_module_reachable_from_the_package(repo):
    """Does the import graph of ``httpbin`` reach the retired stack?

    Read, not imported.  The version of this in stage 2 imported the package into
    the grader's own interpreter and inspected ``sys.modules``, which is running
    submitted code inside the harness to find out what it imports -- and stage 1
    may not run anything.  Following the import edges statically answers the same
    question and cannot execute a module's top level.
    """
    package = repo / "httpbin"
    assert package.is_dir(), "httpbin/ is missing"
    reached: dict[str, list[str]] = {}
    for path in sorted(package.rglob("*.py")):
        hit = sorted(srbscan.imported_names(path) & set(RETIRED_TOKENS))
        if hit:
            reached[str(path.relative_to(repo))] = hit
    assert not reached, (
        f"the application package imports the retired stack: "
        f"{json.dumps(reached, indent=2)}"
    )
