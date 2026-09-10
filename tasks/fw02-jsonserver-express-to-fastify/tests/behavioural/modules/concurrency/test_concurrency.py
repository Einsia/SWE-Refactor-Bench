"""What overlapping requests do to each other.

The replay is sequential: one request, one response, then the next. That is the
friendliest schedule a server will ever see, and it hides the defect that a port
to an async framework is most likely to introduce -- per-request state parked
somewhere that outlives the request, or a read-modify-write on the database with
an await in the middle of it, where two writes can interleave and one is lost.

Nothing here has a recorded answer, and nothing here needs one. Every check is
the submission against itself: fire the same request twice and the answers must
agree; fire twenty inserts at once and there must be twenty records with twenty
distinct ids; fire two different queries together and each must get its own
answer rather than the other's.

State A serialises writes through lowdb's write queue, so the sequential and the
overlapping schedule produce the same database. A port that dropped that
serialisation passes every other module in this stage.
"""

from __future__ import annotations

import json as jsonlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from harness.sessions import Case, Session

pytestmark = pytest.mark.behaviour

#: Wide enough to interleave on any reasonable event loop, small enough that the
#: module stays inside its timeout on a loaded machine.
FANOUT = 20


def fan(server, cases: list[Case]) -> list[dict]:
    """Issue every case at once and return the answers in the order given.

    Each request opens its own connection, so the only thing shared between them
    is the server. Whether that is enough is the question.
    """
    with ThreadPoolExecutor(max_workers=len(cases)) as pool:
        return list(pool.map(server.request, cases))


@pytest.fixture(scope="module")
def reader(boot):
    """A read-only server. Nothing in the read half of this module mutates it."""
    return boot(Session(id="overlap-read", cases=()))


@pytest.fixture
def writer(boot):
    """A freshly seeded server per test, because these tests write."""
    return boot(Session(id="overlap-write", cases=(), mutating=True))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def test_the_same_read_twice_over_gives_the_same_bytes(reader):
    """Twenty copies of one request. One answer, or the response is not a function
    of the request."""
    cases = [Case(id=f"same-{i}", path="/posts?_sort=views&_order=desc")
             for i in range(FANOUT)]
    answers = fan(reader, cases)
    bodies = {a["body"] for a in answers}
    assert len(bodies) == 1, (
        f"{len(bodies)} distinct bodies came back from {FANOUT} identical "
        "requests")
    assert {a["status"] for a in answers} == {200}


def test_overlapping_different_queries_each_get_their_own_answer(reader):
    """The classic symptom of shared per-request state: A gets B's answer.

    Ten distinct filters, interleaved. Each is checked against what it asked for,
    so a swap shows up as a row that does not match the filter rather than as a
    count that happens to be right.
    """
    wanted = list(range(1, 11))
    cases = [Case(id=f"byid-{i}", path=f"/posts?id={i}") for i in wanted]
    answers = fan(reader, cases)
    for i, entry in zip(wanted, answers):
        rows = entry["json"]
        assert isinstance(rows, list), entry["body"][:200]
        assert [r["id"] for r in rows] == [i], (
            f"the request for id={i} was answered with "
            f"{[r.get('id') for r in rows]}")


def test_a_bad_request_in_the_batch_does_not_disturb_the_good_ones(reader):
    """A 404 alongside nine 200s. The nine must still be right.

    An error path that unwinds too far -- or that leaves a half-built reply in
    something shared -- shows up as a neighbour getting the error's status or an
    error's body.
    """
    cases = [Case(id="missing", path="/posts/999999")]
    cases += [Case(id=f"ok-{i}", path=f"/posts/{i}") for i in range(1, 10)]
    answers = fan(reader, cases)
    assert answers[0]["status"] == 404, answers[0]["body"][:200]
    for i, entry in zip(range(1, 10), answers[1:]):
        assert entry["status"] == 200, (
            f"/posts/{i} answered {entry['status']} while a 404 was in flight")
        assert entry["json"]["id"] == i, entry["json"]


def test_total_count_agrees_across_overlapping_pages(reader):
    """Every page of one query reports the same total, whatever order they land."""
    cases = [Case(id=f"page-{p}", path=f"/posts?_page={p}&_limit=2",
                  headers_extra=("x-total-count",)) for p in range(1, 8)]
    answers = fan(reader, cases)
    totals = {(a["headers"].get("x-total-count") or [None])[0] for a in answers}
    assert len(totals) == 1, f"pages of one query disagreed on the total: {totals}"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def test_overlapping_inserts_all_survive_with_distinct_ids(writer):
    """The read-modify-write test. Twenty inserts at once, twenty records after.

    State A allocates ``max + 1`` and serialises the write, so twenty concurrent
    inserts get twenty consecutive ids. This does not require the ids to be
    consecutive -- an implementation may reasonably allocate differently under
    overlap -- but it does require that no insert is lost and no id is issued
    twice, because a duplicate id makes the record unaddressable.
    """
    before = len(writer.request(Case(id="before", path="/posts"))["json"])
    cases = [Case(id=f"ins-{i}", method="POST", path="/posts",
                  json={"title": f"overlap {i}", "authorId": 1,
                        "views": 1000 + i}) for i in range(FANOUT)]
    answers = fan(writer, cases)

    statuses = [a["status"] for a in answers]
    assert set(statuses) == {201}, f"not every insert was accepted: {statuses}"
    ids = [a["json"]["id"] for a in answers]
    assert len(set(ids)) == FANOUT, f"ids were issued twice: {sorted(ids)}"

    after = writer.request(Case(id="after", path="/posts"))["json"]
    assert len(after) == before + FANOUT, (
        f"{FANOUT} inserts were accepted but the collection grew by "
        f"{len(after) - before}; a write was lost")
    assert set(ids) <= {r["id"] for r in after}, (
        "an insert answered 201 with an id that is not in the collection")


def test_overlapping_writes_to_different_collections_all_land(writer):
    """Four collections written at once. The database is one file; the writes are
    not one write."""
    cases = [
        Case(id="w-post", method="POST", path="/posts",
             json={"title": "mixed", "authorId": 2, "views": 7}),
        Case(id="w-comment", method="POST", path="/comments",
             json={"body": "mixed", "postId": 1}),
        Case(id="w-note", method="POST", path="/notes",
             json={"text": "mixed", "postId": 1}),
        Case(id="w-user", method="POST", path="/users",
             json={"name": "Mixed", "email": "mixed@example.com",
                   "age": 31, "city": "Mixedton", "roles": ["reader"]}),
    ]
    answers = fan(writer, cases)
    for case, entry in zip(cases, answers):
        assert entry["status"] == 201, (
            f"{case.path} answered {entry['status']}: {entry['body'][:160]}")
    db = writer.request(Case(id="db", path="/db"))["json"]
    assert "mixed" in jsonlib.dumps(db["posts"][-1])
    assert "mixed" in jsonlib.dumps(db["comments"][-1])


def test_overlapping_patches_to_one_record_leave_one_of_them_intact(writer):
    """Ten patches to the same row, each setting a different value.

    Which one wins is a race and is not asserted. What is asserted is that the
    winner is one of the ten, and that the fields nobody touched are still there
    -- a read-modify-write that interleaved produces a row built from two
    different patches, or a row that lost its untouched fields.
    """
    created = writer.request(Case(
        id="seed-row", method="POST", path="/posts",
        json={"title": "contended", "authorId": 3, "views": 1,
              "category": "keep", "meta": {"slug": "keep"}}))
    rid = created["json"]["id"]
    values = [5000 + i for i in range(10)]
    cases = [Case(id=f"patch-{v}", method="PATCH", path=f"/posts/{rid}",
                  json={"views": v}) for v in values]
    answers = fan(writer, cases)
    assert {a["status"] for a in answers} == {200}, [a["status"] for a in answers]

    final = writer.request(Case(id="final", path=f"/posts/{rid}"))["json"]
    assert final["views"] in values, (
        f"the row ended up with views={final['views']}, which no patch sent")
    assert final["category"] == "keep", final
    assert final["meta"] == {"slug": "keep"}, final
    assert final["title"] == "contended", final


def test_a_read_that_overlaps_a_write_sees_one_state_or_the_other(writer):
    """Never a half-written database.

    Twelve inserts with a full-collection read interleaved between each pair. Each
    read must parse, and must hold a whole number of complete records -- a read
    that observed the file mid-rewrite comes back truncated, and a read served
    from a partially-updated in-memory copy comes back with a record missing its
    fields.
    """
    cases: list[Case] = []
    for i in range(12):
        cases.append(Case(id=f"mix-ins-{i}", method="POST", path="/posts",
                          json={"title": f"torn {i}", "authorId": 1,
                                "views": 2000 + i}))
        cases.append(Case(id=f"mix-read-{i}", path="/posts"))
    answers = fan(writer, cases)
    reads = [a for a, c in zip(answers, cases) if c.method == "GET"]
    for entry in reads:
        rows = entry["json"]
        assert isinstance(rows, list), (
            f"a read during a write did not parse as JSON: {entry['body'][:200]}")
        for row in rows:
            assert "id" in row and "title" in row, (
                f"a read during a write returned an incomplete record: {row}")


def test_delete_and_read_of_the_same_row_do_not_produce_a_third_answer(writer):
    """The row is either there or gone. A 500 is neither."""
    created = writer.request(Case(
        id="doomed", method="POST", path="/posts",
        json={"title": "doomed", "authorId": 1, "views": 3}))
    rid = created["json"]["id"]
    cases = [Case(id="del", method="DELETE", path=f"/posts/{rid}")]
    cases += [Case(id=f"get-{i}", path=f"/posts/{rid}") for i in range(8)]
    answers = fan(writer, cases)
    for case, entry in zip(cases, answers):
        assert entry["status"] in (200, 404), (
            f"{case.method} {case.path} answered {entry['status']} while the "
            f"delete was in flight: {entry['body'][:200]}")
    assert writer.request(Case(id="gone", path=f"/posts/{rid}"))["status"] == 404


def test_the_server_is_still_healthy_after_all_of_that(writer):
    """A port that leaked a handle or wedged a queue fails here rather than in
    whichever module happened to run next."""
    fan(writer, [Case(id=f"burst-{i}", method="POST", path="/comments",
                      json={"body": f"burst {i}", "postId": 2})
                 for i in range(FANOUT)])
    entry = writer.request(Case(id="health", path="/posts/1"))
    assert entry["status"] == 200, entry["body"][:200]
    assert entry["json"]["id"] == 1
