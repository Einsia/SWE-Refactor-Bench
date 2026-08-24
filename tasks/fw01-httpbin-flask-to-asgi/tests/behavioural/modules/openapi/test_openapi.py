"""``/spec.json`` and the Swagger UI, compared against the frozen State A spec.

flasgger generated this document by scraping YAML out of every view's docstring.
Once flasgger is gone the submission has to produce the same document some other
way, so this is the part of the migration most likely to be quietly dropped --
and the easiest to fake with a stub.

The comparison is structural rather than byte-wise (key order and whitespace in
the spec are not contractual), but it is exhaustive: 52 paths, 78 operations, and
every operation's tags/summary/produces/parameters/responses. That is a few
hundred assertions on its own, each attributable to one operation.
"""

from __future__ import annotations

import json
import re

import pytest

from harness import normalize

pytestmark = pytest.mark.behaviour

SPEC_METADATA_KEYS = ["swagger", "basePath", "host", "schemes", "info", "tags",
                      "definitions", "protocol"]


@pytest.fixture(scope="session")
def spec(replay):
    if "spec-json" in replay["errors"]:
        pytest.fail(f"GET /spec.json failed: {replay['errors']['spec-json']}")
    raw = normalize.decode_body(replay["responses"]["spec-json"]["body"])
    try:
        return json.loads(raw)
    except ValueError as exc:
        pytest.fail(f"/spec.json is not valid JSON: {exc}\n{raw[:400]!r}")


def _ops(document):
    return {
        f"{method.upper()} {path}": op
        for path, methods in document.get("paths", {}).items()
        for method, op in methods.items()
        if method.lower() in ("get", "post", "put", "patch", "delete",
                             "options", "head", "trace")
    }


# ---------------------------------------------------------------------------
# Document-level metadata
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", SPEC_METADATA_KEYS)
def test_spec_metadata(key, spec, golden_spec):
    want = golden_spec.get(key)
    got = spec.get(key)
    if want is None:
        # A key State A did not emit must not appear from nowhere.
        assert got is None, f"/spec.json gained a {key!r} key: {got!r}"
        return
    assert got is not None, f"/spec.json is missing {key!r}"
    assert normalize.normalise_json(got) == normalize.normalise_json(want), (
        f"/spec.json {key} differs.\n--- State A ---\n"
        f"{json.dumps(want, indent=2, sort_keys=True)[:800]}\n--- got ---\n"
        f"{json.dumps(got, indent=2, sort_keys=True)[:800]}"
    )


def test_spec_declares_swagger_2(spec):
    assert spec.get("swagger") == "2.0", (
        f"the document must stay Swagger 2.0, got {spec.get('swagger')!r}; "
        f"an OpenAPI 3 document is a different format and breaks the UI"
    )


def test_spec_path_set(spec, golden_spec):
    got = set(spec.get("paths", {}))
    want = set(golden_spec["paths"])
    missing = sorted(want - got)
    extra = sorted(got - want)
    assert not missing and not extra, (
        f"/spec.json documents the wrong paths.\n"
        f"missing ({len(missing)}): {missing}\nextra ({len(extra)}): {extra}"
    )


def test_spec_operation_set(spec, golden_spec):
    got = set(_ops(spec))
    want = set(_ops(golden_spec))
    missing = sorted(want - got)
    extra = sorted(got - want)
    assert not missing and not extra, (
        f"/spec.json documents the wrong operations.\n"
        f"missing ({len(missing)}): {missing}\nextra ({len(extra)}): {extra}"
    )


# ---------------------------------------------------------------------------
# Per-operation comparison: one test per operation per field
# ---------------------------------------------------------------------------

def _golden_ops():
    import pathlib
    import os
    path = pathlib.Path(
        os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data" / "expected-openapi.json"
    if not path.exists():
        return {}
    return _ops(json.loads(path.read_text()))


GOLDEN_OPS = _golden_ops()
OP_FIELDS = ("tags", "summary", "produces", "parameters", "responses",
             "description")
OP_PARAMS = [
    (name, field)
    for name in sorted(GOLDEN_OPS)
    for field in OP_FIELDS
]


@pytest.mark.parametrize("op_name,field", OP_PARAMS,
                         ids=[f"{n}::{f}" for n, f in OP_PARAMS])
def test_spec_operation_field(op_name, field, spec, golden_spec):
    """Each documented operation keeps each of its documented fields."""
    got_ops = _ops(spec)
    if op_name not in got_ops:
        pytest.fail(f"/spec.json no longer documents {op_name}")
    want = _ops(golden_spec)[op_name].get(field)
    got = got_ops[op_name].get(field)

    if want is None:
        assert got is None, (
            f"{op_name} gained a {field!r} the State A spec did not have: {got!r}"
        )
        return
    assert got is not None, f"{op_name} is missing {field!r}"

    # Parameter order inside an operation is not meaningful; the set is.
    if field == "parameters":
        keyed = lambda ps: sorted(  # noqa: E731
            (json.dumps(p, sort_keys=True) for p in ps)
        )
        assert keyed(got) == keyed(want), (
            f"{op_name} parameters differ.\n--- State A ---\n"
            f"{json.dumps(want, indent=2, sort_keys=True)}\n--- got ---\n"
            f"{json.dumps(got, indent=2, sort_keys=True)}"
        )
        return

    assert normalize.normalise_json(got) == normalize.normalise_json(want), (
        f"{op_name} {field} differs.\n"
        f"State A: {json.dumps(want, sort_keys=True)}\n"
        f"got:     {json.dumps(got, sort_keys=True)}"
    )


# ---------------------------------------------------------------------------
# The Swagger UI page
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def ui_html(replay):
    if "swagger-ui-root" in replay["errors"]:
        pytest.fail(f"GET / failed: {replay['errors']['swagger-ui-root']}")
    return normalize.decode_body(
        replay["responses"]["swagger-ui-root"]["body"]
    ).decode("utf-8", "replace")


def test_ui_is_an_html_document(ui_html, replay):
    assert "<html" in ui_html.lower(), "GET / did not return an HTML document"
    ctype = replay["responses"]["swagger-ui-root"]["headers"].get("content-type")
    assert ctype == ["text/html; charset=utf-8"], (
        f"GET / content type is {ctype!r}"
    )


def test_ui_loads_the_local_spec(ui_html):
    """The UI must point at this app's own /spec.json."""
    assert "/spec.json" in ui_html, (
        "the Swagger UI page does not reference /spec.json, so it cannot render "
        "this app's API"
    )


def test_ui_mentions_swagger(ui_html):
    low = ui_html.lower()
    assert "swagger" in low or "redoc" in low or "openapi" in low, (
        "GET / does not look like an API documentation page"
    )


_ASSET_RE = re.compile(
    r'(?:src|href)\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE
)


def _asset_refs(html):
    return [
        u for u in _ASSET_RE.findall(html)
        if not u.startswith(("data:", "#", "mailto:"))
    ]


def test_ui_assets_are_served_by_this_app(ui_html, http, base_url):
    """Every same-origin asset the page references must actually resolve.

    instruction.md requires the UI's assets to be served by the application, so
    a page that renders only because it pulls JavaScript off a CDN has not
    completed the migration -- and would not work in the offline container.
    """
    refs = _asset_refs(ui_html)
    assert refs, "the Swagger UI page references no assets at all"

    local = [u for u in refs if u.startswith("/")]
    assert local, (
        f"the Swagger UI page references no same-origin assets; all of "
        f"{refs[:8]} are external, so the UI cannot work offline"
    )

    broken = {}
    for ref in sorted(set(local)):
        try:
            r = http.get(base_url + ref)
        except Exception as exc:
            broken[ref] = f"{type(exc).__name__}: {exc}"
            continue
        if r.status_code != 200:
            broken[ref] = f"HTTP {r.status_code}"
        elif not r.content:
            broken[ref] = "empty body"
    assert not broken, (
        f"the Swagger UI references assets this app does not serve: "
        f"{json.dumps(broken, indent=2)}"
    )


_INLINE_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
_CALL_RE = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")
_DECL_RE = re.compile(
    r"(?:function\s+|var\s+|let\s+|const\s+|class\s+)([A-Za-z_$][\w$]*)")

# Names a browser supplies itself, plus the keywords that look like calls to the
# regex above. Neither kind has to be defined by a served script.
_AMBIENT = frozenset("""
if for while switch catch return function typeof new do else delete void await
in of case throw with yield super this
Array Object String Number Boolean Date RegExp Error TypeError RangeError
SyntaxError Function Promise Map Set WeakMap WeakSet Symbol BigInt Proxy Reflect
JSON Math Intl URL URLSearchParams Blob FormData Headers Request Response
fetch alert confirm prompt setTimeout setInterval clearTimeout clearInterval
requestAnimationFrame queueMicrotask encodeURIComponent decodeURIComponent
encodeURI decodeURI parseInt parseFloat isNaN isFinite eval btoa atob
addEventListener removeEventListener structuredClone import require
""".split())


def test_the_ui_page_can_actually_boot(ui_html, http, base_url):
    """Every global the page's own bootstrap calls must be defined by a script
    it serves.

    This replaces a check that asserted ``max(sizes.values()) > 100_000`` -- at
    least one referenced ``.js`` had to exceed 100,000 bytes. That number is not
    a property of the migration. It failed a submission whose UI bundle was
    99KB because it shipped a newer or differently-minified Swagger UI, or split
    one bundle into two, and it would have passed 100KB of comments.

    What it was really reaching for is "the JavaScript is not a stub", and that
    has an exact behavioural statement. State A's page ends with an inline
    ``window.onload`` that fetches ``/spec.json`` and hands it to
    ``SwaggerUIBundle({...})``. If nothing the page loads defines
    ``SwaggerUIBundle``, the browser throws ReferenceError on load and renders an
    empty div -- so a two-byte ``/x.js`` fails here, and so does a page that
    references the right filenames while serving something that does not contain
    the application. It is derived from the submission's own page rather than
    from a constant, so a UI wired through ``Redoc.init`` or a different bundle
    name is judged against what *it* calls.
    """
    scripts = [
        u for u in _asset_refs(ui_html)
        if u.startswith("/") and u.split("?")[0].endswith(".js")
    ]
    if not scripts:
        pytest.fail(
            "the Swagger UI page loads no same-origin JavaScript; the real UI "
            "is a JavaScript application and must be served offline"
        )

    served = {}
    for ref in scripts:
        r = http.get(base_url + ref)
        served[ref] = r.text if r.status_code == 200 else ""
    source = "\n".join(served.values())

    inline = "\n".join(_INLINE_RE.findall(ui_html))
    # A commented-out call is not a call. Stripping comments first keeps the
    # regex below from inventing a requirement out of somebody's note to self.
    inline = re.sub(r"/\*.*?\*/", " ", inline, flags=re.S)
    inline = re.sub(r"(?m)//.*$", " ", inline)
    called = set(_CALL_RE.findall(inline))
    local = set(_DECL_RE.findall(inline))
    wanted = {n for n in called if n not in _AMBIENT and n not in local}

    missing = sorted(n for n in wanted if n not in source)
    assert not missing, (
        f"the page's inline bootstrap calls {missing}, which none of the "
        f"JavaScript it serves defines; on load the browser raises "
        f"ReferenceError and the UI renders nothing. Served: "
        f"{ {k: len(v) for k, v in served.items()} }"
    )

    # If the page has no inline bootstrap there is nothing above to check
    # against, so fall back to asking that the served JavaScript is a program
    # rather than a placeholder -- still no byte threshold.
    if not wanted:
        assert re.search(r"function\s*[\w$]*\s*\(|=>", source), (
            f"no script the UI page serves contains a single function; these "
            f"cannot be an application: "
            f"{ {k: len(v) for k, v in served.items()} }"
        )


def test_spec_json_content_type(replay):
    ctype = replay["responses"]["spec-json"]["headers"].get("content-type")
    assert ctype and ctype[0].startswith("application/json"), (
        f"/spec.json content type is {ctype!r}"
    )
