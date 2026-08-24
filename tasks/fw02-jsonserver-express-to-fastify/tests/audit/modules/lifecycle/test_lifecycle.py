"""Is the connect middleware chain still the thing that handles a request?

This is the module whose findings need reading most carefully, and the module
docstring is the place to say why.

Fastify has hooks.  They take ``(request, reply)`` and they run in a documented
order, and a migration that uses them is doing the right thing -- ``onRequest``,
``preHandler`` and ``onSend`` are the whole point of the exercise, not evidence
against it.  Two Fastify APIs also legitimately end in a callback: an error
handler is ``(error, request, reply)`` and a content-type parser is
``(request, payload, done)``.  Neither is connect.

What connect looks like is different, and it is what the checks below describe: a
handler whose parameters are ``(req, res, next)``, an array of such handlers, a
loop or a recursive ``next()`` that advances through the array, and one route
registered as a catch-all with the real matching done inside it.  Those are shapes
in the text, and every finding here is a line number for the reviewer to open.

The scan cannot tell a surviving chain from a faithful port that happens to name
a parameter ``next``.  It is not trying to.  It says which line, and the reviewer
reads the function.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: A function whose parameter list is connect's.  Arrow and classic forms.
#:
#: The second parameter is what decides it.  Fastify's hooks are
#: ``(request, reply)`` and three of them may take a trailing ``done``, so
#: ``(request, reply, done)`` is Fastify's own shape and matching it would make
#: this check fire on every correct migration -- which is the failure mode the
#: module docstring warns about, committed by the check itself.  ``res`` is
#: connect's: Fastify has no ``res``, and a function that named its second
#: parameter that is either connect's or a port that kept connect's vocabulary.
CONNECT_SIGNATURE = re.compile(
    r"""(?:function\s*[\w$]*\s*\(|\(\s*)"""
    r"""(?P<req>req|request|rq)\s*,\s*(?P<res>res|response|rs)\s*,\s*"""
    r"""(?P<next>next|nxt|done|_next)\s*\)""")

#: `app.use(...)` and the aliases a re-implementation reaches for.
USE_CALLS = re.compile(
    r"""(?<![\w.])(?:app|server|fastify|instance|self|this)\s*\.\s*"""
    r"""(?:use|useMiddleware|addMiddleware|stack\s*\.\s*push)\s*\(""")

#: A registration of a bridge, by shape rather than by package name -- the
#: closure module already looks for the names.
BRIDGE_REGISTRATION = re.compile(
    r"""register\s*\(\s*(?:require\s*\(\s*['"]@fastify/(?:express|middie)['"]|"""
    r"""(?:fastifyExpress|fastifyMiddie|middie|expressPlugin|connectPlugin))""")

#: A path pattern that matches everything.  Legitimate for a static-file fallback
#: and not legitimate as the thing that serves the API.
#:
#: Only ever looked for as the first argument of a route registration.  A bare
#: ``'*'`` is `If-None-Match: *`, `Accept-Encoding: *` and the content-type parser
#: wildcard, all of which an HTTP service says several times, and none of which is
#: a route.  ``setNotFoundHandler`` is not on this list either: it is Fastify's
#: 404 API and belongs in the lifecycle a migration is supposed to use.
CATCHALL_PATTERNS = ["/*", "*", "/:splat*", "/*path", "/(.*)", "/:path(.*)",
                     "/:rest*"]

#: Route registration with a given first argument, for the check above.
_ROUTE_CALL = (
    r"""(?<![\w.])(?:app|server|fastify|instance|scope|router)\s*\.\s*"""
    r"""(?:route|get|post|put|patch|delete|all|head|options)\s*\(\s*"""
)

#: Hand-rolled path matching, which is what a catch-all needs behind it.
PRIVATE_MATCHER = [
    "pathToRegexp", "path-to-regexp", "new RegExp('^/'", 'new RegExp("^/"',
    "url.split('/')", 'url.split("/")', ".match(/^\\/", "matchRoute",
    "resolveRoute", "dispatchRoute", "findHandler", "routeTable",
]

#: Express's own routing verbs, applied to something called app.
EXPRESS_ROUTING = re.compile(
    r"""(?<![\w.])(?:app|server|router)\s*\.\s*"""
    r"""(?:get|post|put|patch|delete|all|options|head)\s*\(\s*"""
    r"""['"`][^'"`]*['"`]\s*,\s*(?:[\w$]+\s*,\s*)*"""
    r"""(?:function\s*\(|\()\s*req\s*,\s*res\s*,""")

#: Fastify APIs that legitimately take a trailing callback, listed so a reviewer
#: reading a finding knows what the scan already excluded.
LICENSED_CALLBACK_APIS = ["setErrorHandler", "addContentTypeParser",
                          "setNotFoundHandler", "addHook"]


def _code(repo, skip_tests=True):
    for path, rel in srbscan.code_files(repo):
        if skip_tests and srbscan.in_tests(rel):
            continue
        yield path, rel


# ---------------------------------------------------------------------------
# The connect signature
# ---------------------------------------------------------------------------

def test_no_connect_signature_in_the_service(repo):
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            match = CONNECT_SIGNATURE.search(line)
            if not match:
                continue
            # An error handler and a content-type parser end in a callback by
            # Fastify's own design, so a registration on the same line is noted.
            offenders.append({
                "path": rel, "line": lineno, "quote": line.strip()[:160],
                "third_parameter": match.group("next"),
                "licensed_api_on_line": [n for n in LICENSED_CALLBACK_APIS
                                         if n in line],
                "looks_commented": srbscan.looks_commented(line),
            })
    unlicensed = [o for o in offenders if not o["licensed_api_on_line"]]
    assert not unlicensed, (
        "these lines declare a function whose second parameter is res; Fastify's "
        "reply is never called res, and its own (request, reply, done) hooks are "
        f"not matched by this check{srbscan.comment_caveat(unlicensed)}:\n"
        f"{json.dumps(unlicensed[:30], indent=2)}"
    )


def test_no_next_call_threading_a_chain(repo):
    """``next()`` as flow control, which is connect's dispatch and not Fastify's.

    Fastify's hooks may take a ``done`` callback, so the check looks for the
    caller-side shape: ``next()`` / ``next(err)`` inside a function that also
    declared ``next`` as its third parameter.
    """
    offenders = []
    for path, rel in _code(repo):
        text = srbscan.read(path)
        if not CONNECT_SIGNATURE.search(text):
            continue
        for lineno, line in srbscan.lines(path):
            if re.search(r"(?<![\w.])next\s*\(\s*(?:\)|err|error|e\b)", line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these files declare a (req, res, next) function and call next() as "
        f"flow control{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:30], indent=2)}"
    )


def test_no_middleware_array(repo):
    """A list of handlers, which is the chain as a data structure."""
    pattern = re.compile(
        r"""(?:middlewares?|stack|chain|handlers|pipeline)\s*"""
        r"""(?::\s*[\w<>\[\]]+\s*)?=\s*\[""")
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if pattern.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    # The caveat goes before the `--middlewares` note rather than after it: this
    # headline is already ~220 characters, and the digest keeps 300, so a clause
    # appended at the end would be cut off on exactly the findings it explains.
    assert not offenders, (
        "these lines build an array of middleware, which is connect's stack as "
        f"a data structure{srbscan.comment_caveat(offenders)}. Note that "
        "`--middlewares` is a documented json-server CLI flag, so an accumulator "
        "for the paths the user passed looks exactly like this:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


def test_no_use_call(repo):
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if USE_CALLS.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines call .use(); Fastify composes with register() and addHook() "
        f"and has no .use() of its own{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


def test_no_bridge_registration_shape(repo):
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if BRIDGE_REGISTRATION.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines register an Express or connect compatibility layer"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


# ---------------------------------------------------------------------------
# Routing: declared to Fastify, or done by hand behind a catch-all
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wildcard", CATCHALL_PATTERNS)
def test_no_catchall_route(wildcard, repo):
    pattern = re.compile(_ROUTE_CALL + r"""['"`]""" + re.escape(wildcard)
                         + r"""['"`]""")
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if pattern.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        f"a route is registered at {wildcard!r}; a wildcard is legitimate for a "
        "static-file fallback and is a finding when it is what serves the API"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


@pytest.mark.parametrize("marker", PRIVATE_MATCHER)
def test_no_private_path_matcher(marker, repo):
    """Hand-rolled path matching, cited with the line's text.

    Two of these markers are package names, and a package name is the one thing on
    this list that a correct port has a reason to write down: ``path-to-regexp`` is
    what Express 4's router used, so a reimplementation of URL rewriting documents
    which flavour of it the new code is compatible with.

    Measured on fw02's round-3 submission, where the sole match for
    ``path-to-regexp`` was a comment header in ``src/server/rewriter.js`` saying
    exactly that -- 0 occurrences in ``package.json``, 0 in the lockfile, and the
    package absent from the mirror the agent installed from, so it could not have
    been live. Reported as ``[{"path": ..., "line": 2}]``, it reached a reviewer
    deciding ``old_stack_retired`` -- a required gate -- as evidence that Express's
    router had survived the migration.

    So the citation carries the line and the comment flag, like the other checks in
    this module. What the reviewer still has to do is unchanged: open the line and
    decide whether it is documentation or dispatch.
    """
    offenders = []
    for path, rel in _code(repo):
        entry = srbscan.marker_citation(path, rel, marker)
        if entry:
            offenders.append(entry)
    assert not offenders, (
        f"{marker!r} appears here; behind a catch-all this is the router that "
        f"replaced Fastify's{srbscan.comment_caveat(offenders)}: "
        f"{json.dumps(offenders[:10], indent=2)}"
    )


def test_no_express_style_route_registration(repo):
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if EXPRESS_ROUTING.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines register a route with an Express handler signature"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


def test_routes_are_declared_to_fastify(repo):
    """The counter-evidence check: are there registrations at all?

    A migrated tree should have a number of ``route``/``get``/``post``
    declarations somewhere.  Finding none is not proof of anything, but it is the
    single most useful orientation fact for a reviewer that has just been handed a
    catch-all finding.
    """
    declarations = []
    # The receiver matters: `db.get('posts')` is lowdb and appears on a hundred
    # lines, so a bare `get(` would count the database as a router.
    pattern = re.compile(
        r"""(?<![\w.])(?:app|server|fastify|instance|scope|router)\s*\.\s*"""
        r"""(?:route|get|post|put|patch|delete|all|head|options)"""
        r"""\s*\(\s*['"`{]""")
    for path, rel in _code(repo):
        count = len(pattern.findall(srbscan.read(path)))
        if count:
            declarations.append({"path": rel, "route_like_calls": count})
    total = sum(d["route_like_calls"] for d in declarations)
    assert total >= 6, (
        "the tree contains almost no route-like declarations "
        f"({total} across {len(declarations)} files): "
        f"{json.dumps(declarations[:20], indent=2)}"
    )


# ---------------------------------------------------------------------------
# Hooks and lifecycle: what a migration is expected to look like
# ---------------------------------------------------------------------------

def test_fastify_lifecycle_is_used(repo):
    """Is there any Fastify lifecycle in the tree?

    Counter-evidence again.  ``addHook``, ``setErrorHandler``,
    ``setNotFoundHandler``, ``addContentTypeParser``, ``decorate`` and
    ``register`` are what the migration was supposed to produce; their total
    absence alongside a working service is worth a reviewer's attention.
    """
    apis = ["addHook", "setErrorHandler", "setNotFoundHandler",
            "addContentTypeParser", "decorate", "register", "onRequest",
            "preHandler", "onSend", "preValidation", "preSerialization"]
    seen = {}
    for path, rel in _code(repo):
        text = srbscan.read(path)
        for api in apis:
            if api in text:
                seen.setdefault(api, []).append(rel)
    assert len(seen) >= 3, (
        "the tree names almost none of Fastify's lifecycle API; found "
        f"{sorted(seen)}"
    )


def test_no_wrapped_express_application(repo):
    """A variable holding an Express app, under any name."""
    pattern = re.compile(
        r"""(?:const|let|var)\s+(?P<name>[\w$]+)\s*=\s*"""
        r"""(?:express|connect|createApplication|makeApp)\s*\(\s*\)""")
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            match = pattern.search(line)
            if match:
                offenders.append({"path": rel, "line": lineno,
                                  "binding": match.group("name"),
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines construct an Express or connect application"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


def test_no_http_createserver_around_a_handler(repo):
    """`http.createServer(app)` is how a connect app is served.

    Fastify owns its own server.  A tree that constructs one by hand and hands it
    a request handler has kept the node-level plumbing json-server had.
    """
    pattern = re.compile(
        r"""createServer\s*\(\s*(?!\s*\)|function\s*\(\s*\)|\{)"""
        r"""(?:[\w$.]+|\(\s*req)""")
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if pattern.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines construct an HTTP server around a request handler"
        f"{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )


def test_no_response_written_through_the_raw_socket(repo):
    """`res.end` / `res.writeHead` on the reply's raw stream.

    Fastify's ``reply.raw`` exists and is sometimes the right answer -- streaming
    a file, hijacking a socket.  Used for ordinary JSON it means the reply
    lifecycle, and therefore the serialiser and the hooks, were bypassed.
    """
    pattern = re.compile(r"""(?<![\w.])(?:res|reply\s*\.\s*raw|response)"""
                         r"""\s*\.\s*(?:writeHead|end|setHeader)\s*\(""")
    offenders = []
    for path, rel in _code(repo):
        for lineno, line in srbscan.lines(path):
            if pattern.search(line):
                offenders.append({"path": rel, "line": lineno,
                                  "quote": line.strip()[:160],
                                  "looks_commented": srbscan.looks_commented(line)})
    assert not offenders, (
        "these lines write a response through a raw node stream rather than "
        f"through Fastify's reply{srbscan.comment_caveat(offenders)}:\n"
        f"{json.dumps(offenders[:20], indent=2)}"
    )
