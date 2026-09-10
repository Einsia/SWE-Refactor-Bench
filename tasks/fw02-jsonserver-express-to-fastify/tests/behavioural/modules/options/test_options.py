"""The launcher flag surface: eight configurations, 57 exchanges.

A flag is not configuration in the sense of a value somebody reads out of a file.
Each of these changes what the server answers, so each is asserted through a
request whose answer differs with it -- ``--read-only`` by rejecting a write,
``--ng`` by not compressing, ``--id`` by keying on a different field, ``--static``
by serving a different directory.

The launcher is what receives them. State A's ``serve.sh`` takes the host, port,
seed, database and routes from the environment and passes everything else straight
through to the server, so a rewrite that parses its own flags differently is
observable here without anybody reading its argument parser.

Two of these sessions exist because the flags in them do *nothing*. ``--no-cors``
and ``--no-gzip`` look like the long forms of ``--nc`` and ``--ng`` and are not:
State A's option parser does not define them, so they are accepted and ignored,
and CORS and compression stay on. A rewrite that tidies this up by wiring the long
forms to the short ones has changed behaviour, and the ``inert-flags`` session is
where that shows.
"""

from __future__ import annotations

import json

import pytest
import srbcompare as compare
from harness.sessions import Case, Session
from srbfixtures import OPTION_SESSIONS, parametrize

pytestmark = pytest.mark.behaviour


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

@parametrize(*OPTION_SESSIONS)
def test_status(pair, key):
    compare.status(pair)


@parametrize(*OPTION_SESSIONS)
def test_status_class(pair, key):
    compare.status_class(pair)


@parametrize(*OPTION_SESSIONS)
def test_no_new_server_error(pair, key):
    compare.no_new_server_error(pair)


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #

@parametrize(*OPTION_SESSIONS)
def test_content_type(pair, key):
    compare.content_type(pair)


@parametrize(*OPTION_SESSIONS)
def test_header_values(pair, key):
    compare.header_values(pair)


@parametrize(*OPTION_SESSIONS)
def test_case_headers(pair, key):
    compare.case_headers(pair)


@parametrize(*OPTION_SESSIONS)
def test_no_extra_headers(pair, key):
    compare.no_extra_headers(pair)


@parametrize(*OPTION_SESSIONS)
def test_no_missing_headers(pair, key):
    compare.no_missing_headers(pair)


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #

@parametrize(*OPTION_SESSIONS)
def test_body(pair, key):
    compare.body(pair)


@parametrize(*OPTION_SESSIONS)
def test_body_length(pair, key):
    compare.body_length(pair)


@parametrize(*OPTION_SESSIONS)
def test_json_shape(pair, key):
    compare.json_shape(pair)


@parametrize(*OPTION_SESSIONS)
def test_json_values(pair, key):
    compare.json_values(pair)


@parametrize(*OPTION_SESSIONS)
def test_json_style(pair, key):
    compare.json_style(pair)


@parametrize(*OPTION_SESSIONS)
def test_stack_body(pair, key):
    compare.stack_body(pair)


@parametrize(*OPTION_SESSIONS)
def test_stack_present(pair, key):
    compare.stack_present(pair)


@parametrize(*OPTION_SESSIONS)
def test_stack_validator_shape(pair, key):
    compare.stack_validator_shape(pair)


@parametrize(*OPTION_SESSIONS)
def test_body_mode_known(pair, key):
    compare.body_mode_known(pair)


@parametrize(*OPTION_SESSIONS)
def test_compressed_body_decodes(pair, key):
    compare.decompresses(pair)


# --------------------------------------------------------------------------- #
# Each flag, asserted as the thing it does
# --------------------------------------------------------------------------- #
# The comparisons above would catch a broken flag as a run of differing bodies.
# These say which flag, and what about it: a submission whose --read-only lets a
# POST through should be told that, not handed 57 diffs. The claims are about the
# documented meaning of the flag, so they hold for any correct implementation and
# need no recorded answer.

@pytest.mark.migration
def test_read_only_rejects_every_write(boot):
    """``--read-only`` has to reject all four write verbs, not just POST."""
    server = boot(Session(id="opt-readonly", cases=(), argv=("--read-only",),
                          mutating=True))
    allowed = []
    for method, path, payload in (
        ("POST", "/posts", {"title": "x"}),
        ("PUT", "/posts/1", {"title": "x"}),
        ("PATCH", "/posts/1", {"title": "x"}),
        ("DELETE", "/posts/1", None),
    ):
        got = server.request(Case(id=f"ro-{method}", method=method, path=path,
                                  json=payload))
        if got["status"] < 400:
            allowed.append(f"{method} {path} -> {got['status']}")
    assert not allowed, (
        "started with --read-only, and these writes were accepted anyway:\n  "
        + "\n  ".join(allowed))

    read = server.request(Case(id="ro-read", path="/posts/1"))
    assert read["status"] == 200, (
        f"--read-only also stopped a read: GET /posts/1 returned "
        f"{read['status']}")


@pytest.mark.migration
def test_no_gzip_short_form_disables_compression(boot):
    """``--ng`` off, default on: the same request, two encodings."""
    plain = boot(Session(id="opt-ng", cases=(), argv=("--ng",), mutating=True))
    asked = Case(id="ng-probe", path="/posts",
                 headers=(("Accept-Encoding", "gzip"),))
    got = plain.request(asked)
    encoding = got["headers"].get("content-encoding")
    assert not encoding, (
        f"started with --ng, and the response is still encoded as {encoding!r}")

    default = boot(Session(id="opt-gz", cases=(), mutating=True))
    got = default.request(asked)
    assert got["headers"].get("content-encoding"), (
        "without --ng, a gzip-accepting client got an unencoded response; "
        "compression is off by default, which is not State A's behaviour")


@pytest.mark.migration
def test_no_cors_short_form_removes_the_cors_headers(boot):
    """``--nc`` off, default on."""
    plain = boot(Session(id="opt-nc", cases=(), argv=("--nc",), mutating=True))
    asked = Case(id="nc-probe", path="/posts",
                 headers=(("Origin", "http://example.test"),))
    got = plain.request(asked)
    assert not got["headers"].get("access-control-allow-origin"), (
        "started with --nc, and Access-Control-Allow-Origin is still being sent: "
        f"{got['headers'].get('access-control-allow-origin')!r}")

    default = boot(Session(id="opt-cors", cases=(), mutating=True))
    got = default.request(asked)
    assert got["headers"].get("access-control-allow-origin"), (
        "without --nc, no Access-Control-Allow-Origin; CORS is off by default, "
        "which is not State A's behaviour")


@pytest.mark.migration
def test_long_form_flags_are_still_inert(boot):
    """``--no-cors`` and ``--no-gzip`` are accepted and do nothing.

    State A's option parser does not define them. They reach the server, are not
    recognised, and CORS and compression stay on -- and a rewrite that "fixes"
    this has changed what a client sees. Asserted as the two headers staying
    present, which is the observable form of the quirk.
    """
    server = boot(Session(id="opt-inert", cases=(),
                          argv=("--no-cors", "--no-gzip"), mutating=True))
    got = server.request(Case(id="inert-probe", path="/posts",
                              headers=(("Origin", "http://example.test"),
                                       ("Accept-Encoding", "gzip"))))
    problems = []
    if not got["headers"].get("access-control-allow-origin"):
        problems.append("  --no-cors suppressed CORS; in State A it is ignored "
                        "and only --nc does that")
    if not got["headers"].get("content-encoding"):
        problems.append("  --no-gzip suppressed compression; in State A it is "
                        "ignored and only --ng does that")
    assert not problems, (
        "the long-form flags are inert in State A:\n" + "\n".join(problems))


@pytest.mark.migration
def test_delay_applies_to_api_responses(boot):
    """``--delay 150`` is a floor on latency, so it is asserted as a floor.

    Wall clock is not comparable between runs, which is why the recording carries
    no timing expectation. A lower bound is: three API requests all slower than
    the delay is a claim a server without the delay fails, and one a slow server
    cannot fake in the other direction.

    Only API requests. State A installs the delay in the router, so anything
    answered by the static mount skips it entirely -- the recording has
    ``GET /`` at 2.3 ms under ``--delay 150`` while the API requests in the same
    session took 155 ms. That exemption is left to the byte comparison above
    rather than asserted here, because it would need a latency *upper* bound and
    an upper bound is not reproducible under load.
    """
    server = boot(Session(id="opt-delay", cases=(), argv=("--delay", "150"),
                          mutating=True))
    measured = []
    for i in range(3):
        got = server.request(Case(id=f"delay-{i}", path="/posts/1"))
        assert got["status"] == 200, f"GET /posts/1 -> {got['status']}"
        measured.append(got["elapsed_ms"])
    too_fast = [ms for ms in measured if ms < 150]
    assert not too_fast, (
        f"started with --delay 150, and {len(too_fast)} of 3 responses arrived "
        f"sooner than that: {[round(m) for m in measured]} ms")


@pytest.mark.migration
def test_custom_id_field_keys_lookups(boot):
    """``--id _id`` moves which field addresses a resource.

    The seed for this configuration has its ``id`` renamed to ``_id``, exactly as
    the recording had it, so a server still keyed on ``id`` cannot find anything.
    """
    server = boot(Session(id="opt-id", cases=(), argv=("--id", "_id"),
                          seed="underscore-id", mutating=True))
    got = server.request(Case(id="id-probe", path="/posts/1"))
    assert got["status"] == 200, (
        f"with --id _id and a seed keyed on _id, GET /posts/1 returned "
        f"{got['status']}; the server is not addressing resources by _id\n"
        f"  body: {got['body'][:300]}")
    body = json.loads(got["body"])
    assert str(body.get("_id")) == "1", (
        f"the object served at /posts/1 does not carry _id=1: {body}")


@pytest.mark.migration
def test_static_flag_changes_which_directory_is_served(boot):
    """``--static altpublic`` serves a different tree at the same path."""
    default = boot(Session(id="opt-static-default", cases=(), mutating=True))
    alt = boot(Session(id="opt-static-alt", cases=(),
                       argv=("--static", "altpublic"), mutating=True))
    asked = Case(id="static-probe", path="/index.html")
    first, second = default.request(asked), alt.request(asked)
    assert first["status"] == 200 and second["status"] == 200, (
        f"/index.html: default {first['status']}, --static altpublic "
        f"{second['status']}; both directories ship an index.html")
    assert first["body"] != second["body"], (
        "--static altpublic served the same bytes as the default directory, so "
        "the flag did not change which directory is mounted")
