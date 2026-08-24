"""The same questions, about data that did not exist when the submission was written.

Everything the four comparison modules check is, in principle, memorisable. The
request list is fixed, so a submission could ship a lookup from request to recorded
response and score well without implementing anything. This module is what makes
that cheat worthless, and it is why it is weighted above the quirks.

Each trial invents a nonce at run time -- a random token, a random tag, seven random
view counts far outside the seed's range -- writes records derived from it, and then
asks questions whose answers are arithmetic over those values: filters, ranges,
sorts, pagination windows, embeds, deep queries, cascades. The expected answers are
computed here, from the nonce, immediately before comparing. Nothing about them
existed when the submission was written, and nothing about them is in the recording.

Four independent trials with different values, so a submission that special-cased
one probe still fails the others.

The seed is the grader's copy, not the submission's, so a tree that edited the data
it is queried about gains nothing. A trial that cannot boot fails its assertions
with the server log attached rather than erroring out and losing the diagnosis.
"""

from __future__ import annotations

import json as jsonlib
import random
import secrets
import string

import pytest
from harness.sessions import Case, Session
from srbfixtures import SEED

pytestmark = pytest.mark.behaviour

#: Four trials. Fixed labels so the report is readable; the values are not fixed.
TRIALS = ["t1", "t2", "t3", "t4"]


class Nonce:
    """The invented data for one trial, and the arithmetic over it."""

    def __init__(self, label: str):
        rng = random.Random(secrets.randbits(64))
        self.label = label
        self.token = "".join(rng.choice(string.ascii_lowercase) for _ in range(11))
        self.tag = "zq" + "".join(rng.choice(string.digits) for _ in range(6))
        #: Distinct, non-round, and far outside the seed's own range so a filter
        #: on them cannot accidentally match a seeded record.
        self.views = rng.sample(range(90001, 99999), 7)
        self.ratings = [round(rng.uniform(1.01, 4.99), 2) for _ in range(7)]
        self.parent_id = 9000 + rng.randrange(1, 899)
        self.child_count = rng.randrange(3, 7)
        self.flat_child_count = rng.randrange(2, 5)

    # -- what gets written -------------------------------------------------
    def posts(self) -> list[dict]:
        """Seven posts, each carrying the nonce in a searchable field."""
        out = []
        for i, (views, rating) in enumerate(zip(self.views, self.ratings)):
            out.append({
                "title": f"{self.token} entry {i}",
                "authorId": 1 + (i % 3),
                "category": self.tag if i % 2 == 0 else f"{self.tag}-odd",
                "views": views,
                "published": i % 2 == 0,
                "tags": [self.token, f"t{i}"],
                "meta": {"slug": f"{self.token}-{i}", "rating": rating,
                         "reviewer": self.token},
            })
        return out

    # -- the answers -------------------------------------------------------
    @property
    def sorted_views_desc(self) -> list[int]:
        return sorted(self.views, reverse=True)

    @property
    def median_view(self) -> int:
        return sorted(self.views)[len(self.views) // 2]

    def views_at_least(self, bound: int) -> list[int]:
        return sorted(v for v in self.views if v >= bound)


class Trial:
    """One booted server, its nonce, and the recorded probe responses."""

    def __init__(self, label: str):
        self.nonce = Nonce(label)
        self.responses: dict[str, dict] = {}
        self.failure: str | None = None
        self.seed: dict = {}
        self.created_ids: list = []
        self.bumped: int | None = None


def _get(server, path: str, key: str) -> dict:
    return server.request(Case(id=key, method="GET", path=path))


def _post(server, path: str, payload, key: str) -> dict:
    return server.request(Case(id=key, method="POST", path=path, json=payload))


def _run_trial(boot, label: str) -> Trial:
    trial = Trial(label)
    nonce = trial.nonce
    # The grader's seed, not the submission's. What the seeded rows are is part
    # of the question being asked -- the id the first insert gets is one more
    # than the highest already there -- so reading it out of the tree under test
    # would let that tree choose its own expected answer.
    try:
        trial.seed = jsonlib.loads(SEED.read_text())
    except (OSError, jsonlib.JSONDecodeError) as exc:
        trial.failure = f"could not read the grader's seed: {exc}"
        return trial

    try:
        server = boot(Session(id=f"unseen-{label}", cases=(), mutating=True))
        r = trial.responses

        # --- write the nonce records -------------------------------------
        for i, post in enumerate(nonce.posts()):
            entry = _post(server, "/posts", post, f"create-{i}")
            r[f"create-{i}"] = entry
            if entry["json"] and isinstance(entry["json"], dict):
                trial.created_ids.append(entry["json"].get("id"))

        # A parent with an explicit high id, and children pointing at it.
        r["create-parent"] = _post(
            server, "/posts",
            {"id": nonce.parent_id, "title": f"{nonce.token} parent",
             "authorId": 1, "category": nonce.tag,
             "views": nonce.views[0] + 1, "published": True,
             "tags": [nonce.token], "meta": {"slug": "parent",
                                             "rating": 5,
                                             "reviewer": nonce.token}},
            "create-parent")
        for i in range(nonce.child_count):
            r[f"create-child-{i}"] = _post(
                server, f"/posts/{nonce.parent_id}/comments",
                {"body": f"{nonce.token} child {i}"}, f"create-child-{i}")
        # The same children again, but through the flat route with a numeric
        # foreign key. The two groups behave differently on `_embed`, and both
        # behaviours are graded below.
        for i in range(nonce.flat_child_count):
            r[f"create-flat-child-{i}"] = _post(
                server, "/comments",
                {"body": f"{nonce.token} flat {i}",
                 "postId": nonce.parent_id}, f"create-flat-child-{i}")

        # --- read it back ------------------------------------------------
        first_id = trial.created_ids[0] if trial.created_ids else 1
        probes = {
            "search-token": f"/posts?q={nonce.token}",
            "filter-tag": f"/posts?category={nonce.tag}",
            "like-token": f"/posts?title_like={nonce.token}",
            "deep-reviewer": f"/posts?meta.reviewer={nonce.token}",
            "views-gte-median": f"/posts?views_gte={nonce.median_view}",
            "views-range": (f"/posts?views_gte={min(nonce.views)}"
                            f"&views_lte={max(nonce.views)}"),
            "sort-desc": f"/posts?q={nonce.token}&_sort=views&_order=desc",
            "sort-asc-limit": (f"/posts?q={nonce.token}&_sort=views"
                               f"&_order=asc&_limit=3"),
            "page-window": f"/posts?q={nonce.token}&_page=2&_limit=3",
            "slice-window": f"/posts?q={nonce.token}&_start=2&_end=5",
            "total-count": f"/posts?category={nonce.tag}&_page=1&_limit=2",
            "parent-item": f"/posts/{nonce.parent_id}",
            "parent-embed": f"/posts/{nonce.parent_id}?_embed=comments",
            "parent-nested": f"/posts/{nonce.parent_id}/comments",
            "parent-expand": f"/posts/{nonce.parent_id}?_expand=author",
            "multi-value": f"/posts?id={nonce.parent_id}&id={first_id}",
            "ne-tag": f"/posts?category_ne={nonce.tag}",
            "whole-db": "/db",
            "collection": "/posts",
        }
        for key, path in probes.items():
            r[key] = _get(server, path, key)

        # --- mutate again, then re-read ----------------------------------
        trial.bumped = nonce.views[0] + 12345
        r["patch-parent"] = server.request(Case(
            id="patch-parent", method="PATCH",
            path=f"/posts/{nonce.parent_id}", json={"views": trial.bumped}))
        r["patch-verify"] = _get(server, f"/posts/{nonce.parent_id}",
                                 "patch-verify")

        r["put-parent"] = server.request(Case(
            id="put-parent", method="PUT", path=f"/posts/{nonce.parent_id}",
            json={"title": f"{nonce.token} replaced"}))
        r["put-verify"] = _get(server, f"/posts/{nonce.parent_id}",
                               "put-verify")

        r["delete-parent"] = server.request(Case(
            id="delete-parent", method="DELETE",
            path=f"/posts/{nonce.parent_id}"))
        r["delete-verify"] = _get(server, f"/posts/{nonce.parent_id}",
                                  "delete-verify")
        r["cascade-verify"] = _get(server,
                                   f"/comments?postId={nonce.parent_id}",
                                   "cascade-verify")
        r["final-search"] = _get(server, f"/posts?q={nonce.token}",
                                 "final-search")
    except Exception as exc:                            # noqa: BLE001
        trial.failure = f"{type(exc).__name__}: {exc}"
    return trial


@pytest.fixture(scope="session", params=TRIALS)
def trial(boot, request) -> Trial:
    """One trial's worth of writes and probes, run once and asked about often.

    Session-scoped: the forty-odd questions below are all about the same run, so
    replaying them per test would multiply the work by forty and, worse, would
    ask each question of a differently-shaped database.
    """
    return _run_trial(boot, request.param)


def body(trial: Trial, key: str):
    if trial.failure:
        pytest.fail(f"the trial did not run: {trial.failure}")
    entry = trial.responses.get(key)
    if entry is None:
        pytest.fail(f"probe {key!r} was not recorded")
    if entry["json"] is None:
        pytest.fail(f"probe {key!r} did not answer JSON: {entry['body'][:200]!r}")
    return entry["json"]


def status(trial: Trial, key: str) -> int:
    if trial.failure:
        pytest.fail(f"the trial did not run: {trial.failure}")
    entry = trial.responses.get(key)
    if entry is None:
        pytest.fail(f"probe {key!r} was not recorded")
    return entry["status"]


def header(trial: Trial, key: str, name: str):
    """One header of one recorded probe, or None if the probe carried no such header.

    The two guards are the same two ``body()`` and ``status()`` have, and this
    function is where their absence cost something.  ``.responses.get(key) or {}``
    cannot tell "the server never started" from "the response had no such header":
    a trial that died leaves ``responses`` empty, so every caller here read None and
    reported an *absent header* -- about a server that never answered anything.

    Measured on fw02's round-3 submission, whose launcher exited 1 on every one of
    the four trials: ``test_trial_ran`` failed with ServerFailed as it should, and
    then these callers blamed missing Location, X-Total-Count and Link headers, at
    weight 1.0 each, in the same module.  Naming a cause the run disproved is worse
    than saying nothing, because a reader who trusts it goes looking at header code.

    Returning None still means exactly what it says -- the response arrived and did
    not carry the header -- which is why the callers may assert on it.
    """
    if trial.failure:
        pytest.fail(f"the trial did not run: {trial.failure}")
    entry = trial.responses.get(key)
    if entry is None:
        pytest.fail(f"probe {key!r} was not recorded")
    values = (entry.get("headers") or {}).get(name.lower())
    return values[0] if values else None


# ---------------------------------------------------------------------------
# The writes themselves
# ---------------------------------------------------------------------------

def test_trial_ran(trial):
    assert trial.failure is None, trial.failure


def test_every_insert_was_accepted(trial):
    for i in range(7):
        assert status(trial, f"create-{i}") == 201, (
            f"insert {i} answered {status(trial, f'create-{i}')}")


def test_inserted_ids_are_sequential_from_the_seed_maximum(trial):
    """State A's id generator is ``max + 1``; seven inserts advance it seven times."""
    seed_max = max(p["id"] for p in trial.seed["posts"])
    expected = list(range(seed_max + 1, seed_max + 8))
    assert trial.created_ids == expected, (
        f"expected ids {expected}, got {trial.created_ids}")


def test_insert_echoes_the_written_record(trial):
    created = body(trial, "create-0")
    assert created["title"] == f"{trial.nonce.token} entry 0"
    assert created["views"] == trial.nonce.views[0]
    assert created["meta"]["reviewer"] == trial.nonce.token


def test_insert_sets_a_location_for_the_new_id(trial):
    # Two asserts rather than `loc and loc.endswith(...), loc`: `header()` answers
    # None for an absent header, so passing the value as the message made the
    # message the word "None" in the one case that fails.  Four of these reached
    # the artifact that way on the round-3 submission, at weight 1.0 each, saying
    # nothing a reader could act on.  A test function is one check however many
    # asserts it holds, so splitting costs nothing in the count.
    loc = header(trial, "create-0", "Location")
    want = f"/posts/{trial.created_ids[0]}"
    assert loc, f"the create response carried no Location header; expected one ending {want}"
    assert loc.endswith(want), f"Location said {loc!r}, which does not end {want}"


def test_explicit_high_id_is_honoured(trial):
    created = body(trial, "create-parent")
    assert created["id"] == trial.nonce.parent_id, created


def test_nested_children_carry_the_url_key_as_a_string(trial):
    """The nested route copies the key out of the path, so it stays text.

    Not a detail worth preserving for its own sake, but it is the cause of the
    ``_embed`` asymmetry two tests below, and a port that quietly coerces here
    changes that observable. Asserted on the response, which is where it shows.
    """
    for i in range(trial.nonce.child_count):
        entry = body(trial, f"create-child-{i}")
        assert entry["postId"] == str(trial.nonce.parent_id), entry


def test_flat_children_keep_their_numeric_foreign_key(trial):
    for i in range(trial.nonce.flat_child_count):
        entry = body(trial, f"create-flat-child-{i}")
        assert entry["postId"] == trial.nonce.parent_id, entry
        assert isinstance(entry["postId"], int), entry


# ---------------------------------------------------------------------------
# Search and filter over invented values
# ---------------------------------------------------------------------------

def test_full_text_search_finds_exactly_the_nonce_records(trial):
    rows = body(trial, "search-token")
    assert len(rows) == 8, (
        f"expected the 7 entries plus the parent, got {len(rows)}")
    assert all(trial.nonce.token in jsonlib.dumps(r) for r in rows)


def test_search_does_not_match_seeded_records(trial):
    rows = body(trial, "search-token")
    seeded = {p["id"] for p in trial.seed["posts"]}
    assert not (seeded & {r["id"] for r in rows}), (
        "the search matched pre-existing records, so it is not filtering on the "
        "nonce at all")


def test_filter_on_the_invented_category(trial):
    rows = body(trial, "filter-tag")
    # entries 0, 2, 4, 6 plus the parent
    assert len(rows) == 5, f"expected 5, got {len(rows)}"
    assert all(r["category"] == trial.nonce.tag for r in rows)


def test_like_operator_over_the_nonce_token(trial):
    rows = body(trial, "like-token")
    assert len(rows) == 8, f"expected 8, got {len(rows)}"


def test_deep_query_on_an_invented_nested_value(trial):
    rows = body(trial, "deep-reviewer")
    assert len(rows) == 8, f"expected 8, got {len(rows)}"
    assert all(r["meta"]["reviewer"] == trial.nonce.token for r in rows)


def test_gte_bound_computed_from_the_nonce(trial):
    rows = body(trial, "views-gte-median")
    got = sorted(r["views"] for r in rows)
    want = trial.nonce.views_at_least(trial.nonce.median_view)
    # The parent carries views[0]+1, which may also clear the bound.
    extra = trial.nonce.views[0] + 1
    if extra >= trial.nonce.median_view:
        want = sorted(want + [extra])
    assert got == want, f"expected {want}, got {got}"


def test_range_bounds_computed_from_the_nonce(trial):
    rows = body(trial, "views-range")
    lo, hi = min(trial.nonce.views), max(trial.nonce.views)
    assert rows, "the range matched nothing"
    assert all(lo <= r["views"] <= hi for r in rows), (
        sorted(r["views"] for r in rows))


def test_ne_operator_excludes_the_invented_category(trial):
    rows = body(trial, "ne-tag")
    assert all(r["category"] != trial.nonce.tag for r in rows)
    assert len(rows) == len(trial.seed["posts"]) + 3, (
        f"expected the seed plus the 3 odd-category entries, got {len(rows)}")


def test_multi_value_filter_over_invented_ids(trial):
    rows = body(trial, "multi-value")
    want = sorted({trial.nonce.parent_id, trial.created_ids[0]})
    assert sorted(r["id"] for r in rows) == want, rows


# ---------------------------------------------------------------------------
# Ordering and windows, computed from the nonce
# ---------------------------------------------------------------------------

def test_descending_sort_matches_the_invented_order(trial):
    rows = body(trial, "sort-desc")
    got = [r["views"] for r in rows]
    assert got == sorted(got, reverse=True), got
    assert got[0] == max(got), got
    assert set(trial.nonce.views) <= set(got), (
        "the sorted result does not contain every inserted value")


def test_ascending_sort_with_limit_takes_the_three_smallest(trial):
    rows = body(trial, "sort-asc-limit")
    assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
    got = [r["views"] for r in rows]
    assert got == sorted(got), got
    assert got[0] == min(trial.nonce.views), (
        f"the smallest inserted value is {min(trial.nonce.views)}, got {got[0]}")


def test_page_window_over_the_nonce_set(trial):
    rows = body(trial, "page-window")
    assert len(rows) == 3, f"expected page 2 of 3 to hold 3 rows, got {len(rows)}"


def test_page_window_total_count_counts_the_matches_not_the_page(trial):
    total = header(trial, "page-window", "X-Total-Count")
    assert total == "8", f"expected 8 matches, header said {total}"


def test_slice_window_over_the_nonce_set(trial):
    rows = body(trial, "slice-window")
    assert len(rows) == 3, f"expected _start=2&_end=5 to yield 3, got {len(rows)}"


def test_total_count_on_the_invented_category(trial):
    total = header(trial, "total-count", "X-Total-Count")
    assert total == "5", f"expected 5, header said {total}"


def test_pagination_link_is_present_for_the_invented_set(trial):
    link = header(trial, "total-count", "Link")
    assert link, ('the paginated response carried no Link header; Express sends one '
                  'carrying rel="first", "prev", "next" and "last"')
    assert 'rel="last"' in link, f'Link said {link!r}, with no rel="last" in it'


def test_collection_length_reflects_every_insert(trial):
    rows = body(trial, "collection")
    assert len(rows) == len(trial.seed["posts"]) + 8, (
        f"expected {len(trial.seed['posts']) + 8} posts, got {len(rows)}")


def test_whole_database_reflects_the_writes(trial):
    db = body(trial, "whole-db")
    ids = {p["id"] for p in db["posts"]}
    assert trial.nonce.parent_id in ids
    assert set(trial.created_ids) <= ids


# ---------------------------------------------------------------------------
# Relations over invented records
# ---------------------------------------------------------------------------

def test_parent_item_is_readable_by_its_invented_id(trial):
    assert status(trial, "parent-item") == 200
    assert body(trial, "parent-item")["id"] == trial.nonce.parent_id


def test_embed_matches_numeric_foreign_keys_only(trial):
    """``_embed`` compares strictly, so only the flat-created children attach.

    The children created through ``/posts/<id>/comments`` carry ``postId`` as a
    *string*, because the nested route copies it out of the URL. ``_embed``
    compares it to the parent's numeric ``id`` and finds nothing. The children
    created through the flat route with a numeric key do attach. Both halves are
    State A behaviour, and a rewrite that coerces either side gets a different
    count -- which is the point of asking with data that has no recorded answer.
    """
    post = body(trial, "parent-embed")
    assert "comments" in post, post
    assert len(post["comments"]) == trial.nonce.flat_child_count, (
        f"expected only the {trial.nonce.flat_child_count} numeric-key children "
        f"to embed, got {len(post['comments'])}")


def test_embed_excludes_the_string_keyed_children(trial):
    post = body(trial, "parent-embed")
    bodies = [c["body"] for c in post.get("comments", [])]
    assert not [b for b in bodies if "child" in b], (
        f"string-keyed children were embedded: {bodies}")


def test_nested_route_returns_every_child_regardless_of_key_type(trial):
    """The nested route filters with a string, which matches both key types."""
    rows = body(trial, "parent-nested")
    expected = trial.nonce.child_count + trial.nonce.flat_child_count
    assert len(rows) == expected, f"expected {expected}, got {len(rows)}"
    assert all(trial.nonce.token in c["body"] for c in rows)


def test_expand_resolves_the_parent_reference(trial):
    post = body(trial, "parent-expand")
    assert "author" in post, post
    assert post["author"]["id"] == post["authorId"]


# ---------------------------------------------------------------------------
# Mutation, re-read, cascade
# ---------------------------------------------------------------------------

def test_patch_is_visible_on_the_next_read(trial):
    assert status(trial, "patch-parent") == 200
    assert body(trial, "patch-verify")["views"] == trial.bumped, (
        f"expected {trial.bumped}, got {body(trial, 'patch-verify')['views']}")


def test_patch_left_the_other_fields_alone(trial):
    after = body(trial, "patch-verify")
    assert after["meta"]["reviewer"] == trial.nonce.token, after
    assert after["category"] == trial.nonce.tag, after


def test_put_dropped_the_absent_fields(trial):
    after = body(trial, "put-verify")
    assert set(after) == {"id", "title"}, after
    assert after["title"] == f"{trial.nonce.token} replaced"


def test_delete_removes_the_invented_record(trial):
    assert status(trial, "delete-parent") == 200
    assert status(trial, "delete-verify") == 404


def test_delete_cascades_to_the_children_just_created(trial):
    remaining = body(trial, "cascade-verify")
    assert remaining == [], (
        f"{len(remaining)} comment(s) still reference the deleted parent")


def test_the_search_set_shrinks_by_exactly_one_after_the_delete(trial):
    before = len(body(trial, "search-token"))
    after = len(body(trial, "final-search"))
    assert after == before - 1, (
        f"the token matched {before} records before the delete and {after} "
        "after; exactly one record was removed")


# ---------------------------------------------------------------------------
# The nonce has to actually be a nonce
# ---------------------------------------------------------------------------

def test_the_nonce_is_not_a_constant(trial):
    """A trial whose token matched something in the seed proves nothing."""
    assert trial.nonce.token not in jsonlib.dumps(trial.seed), (
        "the invented token already appears in the seed")
    for post in trial.seed["posts"]:
        assert post["views"] not in trial.nonce.views, (
            "an invented view count collides with the seed")


def test_created_records_carry_the_invented_values_verbatim(trial):
    rows = body(trial, "search-token")
    got = {r["views"] for r in rows}
    assert set(trial.nonce.views) <= got, (
        f"inserted {sorted(trial.nonce.views)}, read back {sorted(got)}")


@pytest.mark.srb_weight(0.0)
@pytest.mark.migration
def test_no_framework_fingerprint_on_the_probe_responses(trial):
    """Asked again here, on paths that are not in the recording.

    The comparison modules establish it for the recorded requests. A tree that
    removed the header by listing those requests rather than by removing the
    thing that sets it answers them and fails here.

    Recorded at weight 0.0, for the reason `test_x_powered_by_is_gone` in the
    `semantics` module gives at length: State A sends X-Powered-By on every
    response, because Express sends it. An absence that State A cannot produce is
    a question about whether the migration happened, not about behaviour a rewrite
    has to preserve, and stage 1's `old_stack_retired` gate asks it over both trees
    where a failure scores the submission zero.

    Still recorded, and still run against all four trials, because the signal it
    carries is one no other check in this module carries: the four probe trials
    drive paths that are not in the recording, so a tree that stripped the header
    only from the recorded requests passes `semantics` and shows up here.
    """
    if trial.failure:
        pytest.skip("the trial did not run")
    for key, entry in trial.responses.items():
        assert "x-powered-by" not in entry["headers"], (
            f"probe {key} answered with X-Powered-By")


@pytest.mark.migration
def test_probe_responses_are_indented_like_state_a(trial):
    """The formatting has to come from the serialiser, not from a stored answer."""
    entry = trial.responses.get("parent-item")
    if entry is None:
        pytest.fail("probe parent-item was not recorded")
    assert entry["body"].startswith("{\n  \""), entry["body"][:60]
