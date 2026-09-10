"""Writes: 108 exchanges across seven sessions, plus what survives them.

A write is where a rewrite has to reproduce behaviour instead of forwarding it.
Every one decides four things a client sees -- the status, the ``Location``, the id
the server allocated, and the shape it persisted -- and json-server's answers to
those are conventions rather than derivations, so none of them fall out of using
Fastify correctly.

The recorded sessions interleave writes with reads of what was just written, so
persistence is already part of the comparison: if a POST returns the right body
and the following GET does not, one of the two fails. What the recording cannot
show is whether the *server's own* answers agree with each other, because both
sides are compared to State A rather than to each other. The four tests at the
bottom ask that directly, against a fresh server and data the recording does not
contain.

Nothing here inspects the database file. Its format is an implementation detail --
a rewrite is free to change how it stores what it stores -- so persistence is
asked through the API, which is where a client would ask.
"""

from __future__ import annotations

import json

import pytest
import srbcompare as compare
from harness.sessions import Case, Session
from srbfixtures import WRITE_SESSIONS, parametrize

pytestmark = pytest.mark.behaviour


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

@parametrize(*WRITE_SESSIONS)
def test_status(pair, key):
    compare.status(pair)


@parametrize(*WRITE_SESSIONS)
def test_status_class(pair, key):
    compare.status_class(pair)


@parametrize(*WRITE_SESSIONS)
def test_no_new_server_error(pair, key):
    compare.no_new_server_error(pair)


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #

@parametrize(*WRITE_SESSIONS)
def test_content_type(pair, key):
    compare.content_type(pair)


@parametrize(*WRITE_SESSIONS)
def test_header_values(pair, key):
    compare.header_values(pair)


@parametrize(*WRITE_SESSIONS)
def test_case_headers(pair, key):
    compare.case_headers(pair)


@parametrize(*WRITE_SESSIONS)
def test_no_extra_headers(pair, key):
    compare.no_extra_headers(pair)


@parametrize(*WRITE_SESSIONS)
def test_no_missing_headers(pair, key):
    compare.no_missing_headers(pair)


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #

@parametrize(*WRITE_SESSIONS)
def test_body(pair, key):
    compare.body(pair)


@parametrize(*WRITE_SESSIONS)
def test_body_length(pair, key):
    compare.body_length(pair)


@parametrize(*WRITE_SESSIONS)
def test_json_shape(pair, key):
    compare.json_shape(pair)


@parametrize(*WRITE_SESSIONS)
def test_json_values(pair, key):
    compare.json_values(pair)


@parametrize(*WRITE_SESSIONS)
def test_json_style(pair, key):
    compare.json_style(pair)


@parametrize(*WRITE_SESSIONS)
def test_stack_body(pair, key):
    compare.stack_body(pair)


@parametrize(*WRITE_SESSIONS)
def test_stack_present(pair, key):
    compare.stack_present(pair)


@parametrize(*WRITE_SESSIONS)
def test_stack_validator_shape(pair, key):
    compare.stack_validator_shape(pair)


@parametrize(*WRITE_SESSIONS)
def test_body_mode_known(pair, key):
    compare.body_mode_known(pair)


# --------------------------------------------------------------------------- #
# Does the server agree with itself?
# --------------------------------------------------------------------------- #
# Each of these boots its own server, because each mutates and a mutation must
# not leak into the next test. The comparison is submission-against-submission:
# no recorded answer is involved, so these hold for any correct implementation
# and cannot be satisfied by reproducing a stored response.

@pytest.fixture
def server(boot):
    """A fresh default-flags server, for one test."""
    return boot(Session(id="write-probe", cases=(), mutating=True))


def _json(response) -> object:
    return json.loads(response["body"])


def test_created_resource_is_reachable_at_its_location(server):
    """A 201's Location must name something the same server will serve.

    json-server answers a create with a ``Location`` and the created object.
    Following that URL has to produce the object -- if it 404s, the header is
    describing a resource that was never persisted, and no amount of matching the
    recorded response body makes that correct.
    """
    created = server.request(Case(
        id="probe-create", method="POST", path="/posts",
        json={"title": "location probe", "views": 1}))
    assert created["status"] == 201, (
        f"POST /posts returned {created['status']}, not 201\n"
        f"  body: {created['body'][:400]}")

    location = created["headers"].get("location")
    assert location, ("a 201 with no Location; a client has nothing to follow\n"
                      f"  headers: {sorted(created['header_names'])}")
    path = location[0] if isinstance(location, list) else location
    if path.startswith("http://") or path.startswith("https://"):
        path = "/" + path.split("/", 3)[3]

    fetched = server.request(Case(id="probe-follow", path=path))
    assert fetched["status"] == 200, (
        f"POST said the resource is at {path!r}, but GET {path} returned "
        f"{fetched['status']}\n  body: {fetched['body'][:400]}")
    assert _json(fetched) == _json(created), (
        f"the object at {path} is not the object the create returned\n"
        f"  created: {created['body'][:300]}\n"
        f"  fetched: {fetched['body'][:300]}")


def test_allocated_ids_are_not_reused(server):
    """Two creates into one collection must not collide.

    The allocation rule is the server's own; what a client relies on is that it
    produces a fresh id each time. A rewrite that derives the id from the
    collection length gets this wrong as soon as anything has been deleted, which
    is exactly the case the recorded sessions cannot reach twice.
    """
    ids = []
    for i in range(4):
        made = server.request(Case(
            id=f"probe-alloc-{i}", method="POST", path="/comments",
            json={"body": f"alloc {i}", "postId": 1}))
        assert made["status"] == 201, (
            f"create {i} returned {made['status']}: {made['body'][:300]}")
        ids.append(_json(made)["id"])
    assert len(set(map(str, ids))) == len(ids), (
        f"the server allocated a duplicate id across four creates: {ids}")

    listed = server.request(Case(id="probe-alloc-list", path="/comments"))
    present = {str(row["id"]) for row in _json(listed)}
    missing = [i for i in ids if str(i) not in present]
    assert not missing, (
        f"ids {missing} were returned by a 201 but are not in the collection; "
        f"the create reported an object it did not keep")


def test_delete_removes_and_stays_removed(server):
    """A successful delete has to be visible to the next request.

    Two observations, because they fail separately: the resource is gone, and the
    collection no longer lists it. A rewrite that removes the item from an
    in-memory index without persisting passes the first and fails the second.
    """
    made = server.request(Case(
        id="probe-del-create", method="POST", path="/posts",
        json={"title": "delete probe"}))
    assert made["status"] == 201, made["body"][:300]
    ident = _json(made)["id"]

    gone = server.request(Case(id="probe-del", method="DELETE",
                               path=f"/posts/{ident}"))
    assert gone["status"] == 200, (
        f"DELETE /posts/{ident} returned {gone['status']}, not 200\n"
        f"  body: {gone['body'][:400]}")

    after = server.request(Case(id="probe-del-get", path=f"/posts/{ident}"))
    assert after["status"] == 404, (
        f"after a successful DELETE, GET /posts/{ident} returned "
        f"{after['status']}; the resource is still being served")

    listed = server.request(Case(id="probe-del-list", path="/posts"))
    assert str(ident) not in {str(row["id"]) for row in _json(listed)}, (
        f"post {ident} was deleted but the collection still lists it")


def test_put_replaces_and_patch_merges(server):
    """The one semantic difference between the two update verbs.

    PUT drops a field the request omits; PATCH keeps it. Both are recorded, but
    only against fields the recording chose -- and both look identical on any
    object whose fields the request happens to send in full. Asked here on an
    object created for the purpose, so the omitted field is unambiguous.
    """
    made = server.request(Case(
        id="probe-upd-create", method="POST", path="/posts",
        json={"title": "verbs", "views": 7, "keep": "yes"}))
    assert made["status"] == 201, made["body"][:300]
    ident = _json(made)["id"]

    patched = server.request(Case(
        id="probe-patch", method="PATCH", path=f"/posts/{ident}",
        json={"title": "patched"}))
    assert patched["status"] == 200, (
        f"PATCH returned {patched['status']}: {patched['body'][:300]}")
    body = _json(patched)
    assert body.get("title") == "patched", f"PATCH did not apply: {body}"
    assert body.get("keep") == "yes", (
        f"PATCH dropped a field the request did not mention; that is PUT's "
        f"behaviour, not PATCH's: {body}")

    replaced = server.request(Case(
        id="probe-put", method="PUT", path=f"/posts/{ident}",
        json={"title": "replaced"}))
    assert replaced["status"] == 200, (
        f"PUT returned {replaced['status']}: {replaced['body'][:300]}")
    body = _json(replaced)
    assert body.get("title") == "replaced", f"PUT did not apply: {body}"
    assert "keep" not in body, (
        f"PUT kept a field the request omitted; that is PATCH's behaviour, not "
        f"PUT's: {body}")

    after = server.request(Case(id="probe-put-get", path=f"/posts/{ident}"))
    assert _json(after) == body, (
        f"the replaced object reads back differently from what PUT returned\n"
        f"  returned: {replaced['body'][:300]}\n"
        f"  re-read:  {after['body'][:300]}")
