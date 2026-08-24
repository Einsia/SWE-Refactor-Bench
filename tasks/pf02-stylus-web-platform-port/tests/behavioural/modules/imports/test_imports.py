"""Import resolution, and the asynchronous filesystem contract underneath it.

Upstream resolves imports with `fs.statSync` and `glob.sync` and can therefore
decide, inside a synchronous `@import` visit, which of several candidate files
exists.  A web port has neither: `platform.stat` and `platform.readDir` return
promises, so the whole resolution walk has to be restructured -- which is the
single largest piece of surgery in the port, and the one most likely to come out
subtly wrong.

Two kinds of check here.

The scenarios in `harness/resolution.py` compile purpose-built trees where several
candidates match at once, so the *precedence* is observable.  The corpus cannot
ask those questions: every import in it resolves exactly one way, so a port that
searched candidates in the wrong order would still pass all 356 cases.

The rest pin down the contract itself: that a reversed directory listing changes
nothing, that a cycle terminates, and that the bytes were fetched through the
capability object rather than found some other way.
"""
from __future__ import annotations

import pytest

from harness import layout, resolution as R, runners
from srbstylus import permitted_skip, require_core


@pytest.fixture(scope="module")
def expected():
    return runners.oracle(R.ops(), mounts=R.oracle_mounts())


@pytest.fixture(scope="module")
def actual():
    require_core()
    return runners.sandbox(R.ops(), files=R.sandbox_files())


@pytest.fixture(scope="module")
def actual_reversed():
    """The same trees, with every directory listing handed back reversed.

    `platform.readDir` is documented as unordered. Upstream's globs come from
    `glob@7`, which sorts its results, so a port that forwards the host's order
    into the output is relying on something it was told not to rely on.
    """
    require_core()
    return runners.sandbox(R.ops(), files=R.sandbox_files(), read_dir_order="reverse")


@pytest.fixture(scope="module")
def actual_sync():
    """The same trees against a synchronous host.

    §1.2 allows a host to supply either; the port must work with both, and the
    results must not depend on which.
    """
    require_core()
    return runners.sandbox(R.ops(), files=R.sandbox_files(), sync=True)


def _trim(text: str | None, limit: int = 400) -> str:
    if text is None:
        return "<none>"
    one = " ".join(str(text).split())
    return one if len(one) <= limit else f"{one[:limit]}... ({len(one)} chars)"


# ------------------------------------------------------------------ the sweep


def test_core_loaded_for_resolution_scenarios(actual):
    assert actual.load_error is None, (
        "src/core failed to load in the web realm:\n"
        f"{actual.load_error.get('name')}: {actual.load_error.get('message')}"
    )


@pytest.mark.parametrize("sid", R.scenario_ids())
def test_resolution_matches_state_a(sid, expected, actual):
    """The scenario resolves to the same file State A resolves to."""
    s = R.by_id(sid)
    exp, got = expected.get(sid), actual.get(sid)

    if s.throws:
        if exp.ok:
            permitted_skip(f"State A did not raise for {sid}; the harness expected it to")
        assert not got.ok, (
            f"{sid}: src/core compiled a tree State A rejects\n"
            f"  rule:     {s.note}\n"
            f"  state-a:  raised {_trim((exp.error or {}).get('message'), 200)}\n"
            f"  src/core: produced {_trim(got.css, 200)}"
        )
        # Raising is not enough. A port that cannot compile anything raises here
        # too, so the failure has to be the same failure: same diagnosis, same
        # file, same line.
        exp_why, got_why = R.diagnostic(exp.error), R.diagnostic(got.error)
        assert got_why == exp_why, (
            f"{sid}: src/core rejected the tree for a different reason than State A\n"
            f"  rule:     {s.note}\n"
            f"  state-a:  {exp_why!r}\n"
            f"  src/core: {got_why!r}"
        )
        exp_at = ((exp.error or {}).get("filename"), (exp.error or {}).get("lineno"))
        got_at = ((got.error or {}).get("filename"), (got.error or {}).get("lineno"))
        assert got_at == exp_at, (
            f"{sid}: the failure was reported at the wrong place\n"
            f"  rule:     {s.note}\n"
            f"  state-a:  {exp_at[0]}:{exp_at[1]}\n"
            f"  src/core: {got_at[0]}:{got_at[1]}"
        )
        return

    if not exp.ok:
        permitted_skip(
            f"State A could not compile scenario {sid}: {(exp.error or {}).get('message')}"
        )
    assert got.ok, (
        f"{sid}: src/core raised where State A resolved the import\n"
        f"  rule: {s.note}\n"
        f"  {(got.error or {}).get('name')}: {_trim((got.error or {}).get('message'))}"
    )
    assert got.css == exp.css, (
        f"{sid}: resolved to different content than State A\n"
        f"  rule:     {s.note}\n"
        f"  state-a:  {_trim(exp.css)}\n"
        f"  src/core: {_trim(got.css)}"
    )


# ------------------------------------------------- the rules, named individually
#
# Each of these is already decided by a row above.  They exist so that the report
# names the rule that was broken instead of the tree that revealed it.


def test_lookup_paths_are_searched_last_first(expected, actual):
    """`paths` is walked backwards -- `lib/utils.js`, `while (i--)`.

    The likeliest single mistake in a rewritten resolver, and one the corpus
    cannot catch: it never sets two lookup paths that both contain the same name.
    """
    for sid in ("lookup-last-path-wins", "lookup-order-is-not-alphabetical",
                "lookup-three-deep"):
        exp, got = expected.get(sid), actual.get(sid)
        if not exp.ok:
            permitted_skip(f"oracle could not compile {sid}")
        assert got.ok, f"{sid}: src/core raised: {_trim((got.error or {}).get('message'))}"
        assert got.css == exp.css, (
            f"{sid}: the wrong lookup path won.\n"
            "  Upstream iterates `paths` from the end, so the last entry that "
            "contains the name is the one used.\n"
            f"  state-a:  {_trim(exp.css)}\n"
            f"  src/core: {_trim(got.css)}"
        )


def test_candidate_order_within_a_directory(expected, actual):
    """`name.styl`, then `name/index.styl`, then `name/name.styl`."""
    for sid in ("file-beats-dir-index", "index-beats-dir-named-file",
                "dir-named-file-fallback", "index-fallback",
                "ext-beats-extensionless"):
        exp, got = expected.get(sid), actual.get(sid)
        if not exp.ok:
            permitted_skip(f"oracle could not compile {sid}")
        assert got.ok, f"{sid}: src/core raised: {_trim((got.error or {}).get('message'))}"
        assert got.css == exp.css, (
            f"{sid}: the wrong candidate won ({R.by_id(sid).note})\n"
            f"  state-a:  {_trim(exp.css)}\n"
            f"  src/core: {_trim(got.css)}"
        )


def test_require_dedupes_and_import_does_not(expected, actual):
    """`@require` remembers what it has emitted; `@import` does not."""
    for sid in ("import-twice-duplicates", "require-twice-dedupes",
                "require-then-import", "require-glob-dedupes"):
        exp, got = expected.get(sid), actual.get(sid)
        if not exp.ok:
            permitted_skip(f"oracle could not compile {sid}")
        assert got.ok, f"{sid}: src/core raised: {_trim((got.error or {}).get('message'))}"
        assert got.css == exp.css, (
            f"{sid}: @require/@import bookkeeping differs from State A "
            f"({R.by_id(sid).note})\n"
            f"  state-a:  {_trim(exp.css)}\n"
            f"  src/core: {_trim(got.css)}"
        )


def test_cycles_terminate(expected, actual):
    """A cycle must end, and end the way State A ends it.

    `@import` raises; the same cycle through `@require` compiles. A port whose
    resolution became asynchronous can easily turn either into a hang, which is
    why both spellings are checked rather than just the one that throws.
    """
    exp, got = expected.get("cycle-through-require"), actual.get("cycle-through-require")
    if not exp.ok:
        permitted_skip("oracle could not compile the @require cycle")
    assert got.ok, (
        "src/core failed on a cycle State A resolves through @require: "
        f"{_trim((got.error or {}).get('message'))}"
    )
    assert got.css == exp.css, (
        "the @require cycle produced different output than State A\n"
        f"  state-a:  {_trim(exp.css)}\n"
        f"  src/core: {_trim(got.css)}"
    )

    hard = actual.get("cycle-through-import")
    assert not hard.ok, (
        "src/core compiled a cyclic @import that State A rejects. Output was "
        f"{_trim(hard.css, 200)}"
    )


def test_globs_are_expanded_in_sorted_order(expected, actual):
    """Upstream globs with `glob@7`, which sorts unless told not to."""
    for sid in ("glob-flat", "glob-sorted-not-readdir-order", "glob-recursive",
                "glob-suffixed"):
        exp, got = expected.get(sid), actual.get(sid)
        if not exp.ok:
            permitted_skip(f"oracle could not compile {sid}")
        assert got.ok, f"{sid}: src/core raised: {_trim((got.error or {}).get('message'))}"
        assert got.css == exp.css, (
            f"{sid}: glob expansion differs from State A ({R.by_id(sid).note})\n"
            f"  state-a:  {_trim(exp.css)}\n"
            f"  src/core: {_trim(got.css)}"
        )


def test_failed_imports_still_fail(expected, actual):
    """Absence has to be detected, not skipped.

    An asynchronous `stat` makes "file missing" a rejected promise rather than a
    thrown error, and a port that swallowed it would silently emit nothing where
    upstream stops with a diagnostic.
    """
    for sid in ("missing-import", "missing-nested", "glob-no-match",
                "import-of-directory-without-index", "self-require"):
        exp, got = expected.get(sid), actual.get(sid)
        if exp.ok:
            permitted_skip(f"State A did not raise for {sid}")
        assert not got.ok, (
            f"{sid}: src/core produced CSS where State A raised "
            f"({R.by_id(sid).note}). Output was {_trim(got.css, 200)}"
        )
        assert R.diagnostic(got.error) == R.diagnostic(exp.error), (
            f"{sid}: src/core failed for a different reason than State A "
            f"({R.by_id(sid).note})\n"
            f"  state-a:  {R.diagnostic(exp.error)!r}\n"
            f"  src/core: {R.diagnostic(got.error)!r}"
        )

    # The same negatives, against a port that resolves the positives. Without
    # this, a submission that raises unconditionally would collect every check
    # above: "it failed" and "it correctly detected the failure" look identical
    # until you also require the successes.
    resolved = [
        s.id for s in R.SCENARIOS
        if not s.throws and expected.get(s.id).ok and actual.get(s.id).ok
    ]
    assert resolved, (
        "src/core raised for every resolution scenario, including the ones State A "
        "compiles. Detecting a missing import cannot be distinguished from being "
        "unable to resolve anything, so none of the negative cases above count."
    )


# ----------------------------------------------- the host contract, exercised
#
# These compare the port against itself under two different hosts.  The claim is
# that the port's output does not depend on things instruction.md 1.2 says it may
# not depend on.
#
# They still consult the oracle, for one reason: "the two runs agree" is free for a
# port that fails both times, and a port that resolves nothing would collect every
# row in both sweeps below on the strength of failing consistently.  So each row
# first requires the run to have got the scenario *right*, and only then asks
# whether the second host changed it.  The overlap with the comparison sweep above
# is deliberate -- the alternative is 64 rows that a stub passes.


def _invariant_of(res, exp):
    """The quantity that must not change between hosts, or None if the run is wrong.

    For a scenario State A compiles, that quantity is the CSS; for one it rejects,
    the diagnosis. Returning None means this run does not match State A at all, so
    there is no invariance left to test -- the caller fails the row.
    """
    if exp.ok:
        return res.css if (res.ok and res.css == exp.css) else None
    if res.ok:
        return None
    got, want = R.diagnostic(res.error), R.diagnostic(exp.error)
    return got if got == want else None


def _assert_invariant(sid, first, second, exp, *, label_a, label_b, why):
    a, b = _invariant_of(first, exp), _invariant_of(second, exp)
    assert a is not None, (
        f"{sid}: src/core does not match State A under {label_a}, so there is no "
        f"invariance to check. {why}\n"
        f"  state-a:  {'ok: ' + _trim(exp.css) if exp.ok else 'raised: ' + R.diagnostic(exp.error)}\n"
        f"  src/core: {'ok: ' + _trim(first.css) if first.ok else 'raised: ' + R.diagnostic(first.error)}"
    )
    assert b is not None, (
        f"{sid}: src/core matches State A under {label_a} but not under {label_b}. {why}\n"
        f"  {label_a}: {'ok: ' + _trim(first.css) if first.ok else 'raised: ' + R.diagnostic(first.error)}\n"
        f"  {label_b}: {'ok: ' + _trim(second.css) if second.ok else 'raised: ' + R.diagnostic(second.error)}"
    )
    assert a == b, (
        f"{sid}: the result changed between {label_a} and {label_b}. {why}\n"
        f"  {label_a}: {_trim(a)}\n"
        f"  {label_b}: {_trim(b)}"
    )


@pytest.mark.parametrize("sid", R.scenario_ids())
def test_result_is_independent_of_directory_listing_order(sid, expected, actual, actual_reversed):
    """§1.2: `platform.readDir` is unordered, so the output cannot depend on it.

    The two runs differ only in the order the host lists directories. A port that
    forwarded that order into a glob expansion produces stylesheets whose rule
    order varies by host -- and, since the last rule wins in CSS, whose meaning
    varies with it.
    """
    exp = expected.get(sid)
    if not exp.ok and not R.by_id(sid).throws:
        permitted_skip(f"oracle could not compile {sid}")
    _assert_invariant(
        sid, actual.get(sid), actual_reversed.get(sid), exp,
        label_a="a natural directory listing", label_b="a reversed one",
        why="platform.readDir is documented as unordered; upstream's glob sorts its "
            "results, so a correct port must sort too.",
    )


@pytest.mark.parametrize("sid", R.scenario_ids())
def test_result_is_the_same_against_a_synchronous_host(sid, expected, actual, actual_sync):
    """§1.2: a host may answer synchronously; the port must not care.

    The interesting failure is the reverse of the obvious one: a port that only
    ever `await`s works here, while one that branches on `platform.sync` and keeps
    two resolution paths will drift between them.
    """
    exp = expected.get(sid)
    if not exp.ok and not R.by_id(sid).throws:
        permitted_skip(f"oracle could not compile {sid}")
    _assert_invariant(
        sid, actual.get(sid), actual_sync.get(sid), exp,
        label_a="an asynchronous host", label_b="a synchronous one",
        why="platform.stat and platform.readDir may return values or promises, and "
            "the port must not keep a separate resolution path for each.",
    )


# ------------------------------------------------------------------ provenance


def test_imports_were_resolved_through_the_capability_object(actual):
    """Every candidate probe and every read has to be visible in the access log."""
    assert actual.access_log, (
        f"src/core resolved {len(R.SCENARIOS)} scenarios without a single call to "
        "platform.stat, platform.readDir or platform.readFile. The files were not "
        "fetched through the capability object."
    )
    reads = [e for e in actual.access_log if e.startswith("readFile ")]
    assert reads, (
        "no platform.readFile calls across every resolution scenario, though the "
        "imports resolved. The contents came from somewhere other than the host."
    )


def test_missing_candidates_are_probed_not_guessed(actual):
    """A failed lookup should show its attempts.

    `missing-import` has no resolution, and upstream tries `nope.styl`,
    `nope/index.styl` and `nope/nope.styl` before giving up. A port that decided
    the file was absent without asking the host would have an empty log here --
    which would also mean it could never see a file the host does have.
    """
    misses = [
        e for e in actual.access_log
        if ("ENOENT" in e or e.endswith(" null")) and "/missing-import/" in e
    ]
    assert misses, (
        "src/core reported the missing import without probing for it: no failed "
        f"stat or read under {layout.VPROJ}/missing-import appears in the access "
        "log. The candidate list was never actually tried against the host."
    )


def test_glob_expansion_lists_directories(actual):
    """A glob has to enumerate; there is no other way to know what matched."""
    listings = [e for e in actual.access_log if e.startswith("readDir ")]
    assert listings, (
        "no platform.readDir calls, yet the glob scenarios resolved. Upstream "
        "expands `parts/*` by listing the directory; a port that produced the "
        "same files without listing anything hard-coded them."
    )


# ------------------------------------------------------- harness self-checks


#: `(a, b)` scenario pairs whose State A outputs must differ, with the rule each
#: pair isolates.  If a pair ever collides, the rule stops being measured.
_MUST_DIFFER: tuple[tuple[str, str], ...] = (
    ("lookup-last-path-wins", "lookup-order-is-not-alphabetical"),
    ("import-twice-duplicates", "require-twice-dedupes"),
    ("require-then-import", "require-twice-dedupes"),
    ("css-import-passthrough", "css-import-inlined"),
    ("index-beats-dir-named-file", "dir-named-file-fallback"),
    ("file-beats-dir-index", "index-fallback"),
)


@pytest.mark.harness
@pytest.mark.parametrize("a,b", _MUST_DIFFER, ids=[f"{x}-vs-{y}" for x, y in _MUST_DIFFER])
def test_precedence_pairs_disagree_in_state_a(a, b, expected):
    """Each pair must resolve to different files in State A.

    A harness self-check. If the two halves of a precedence pair produced the same
    CSS, the rule between them would be unmeasured and both rows would be handed
    to a port that got the precedence backwards.
    """
    ra, rb = expected.get(a), expected.get(b)
    if not ra.ok or not rb.ok:
        permitted_skip(f"oracle could not compile {a} or {b}")
    assert ra.css != rb.css, (
        f"harness defect: `{a}` and `{b}` produce identical output in State A, so "
        "the precedence rule they exist to pin down is not actually being tested."
    )


@pytest.mark.harness
def test_scenario_outputs_are_distinct_or_declared(expected):
    by_output: dict[str, list[str]] = {}
    for s in R.SCENARIOS:
        r = expected.get(s.id)
        if r.ok and r.css is not None:
            by_output.setdefault(r.css.strip(), []).append(s.id)
    undeclared = [
        ids for ids in by_output.values()
        if len(ids) > 1 and not any(i in R.INTENDED_COLLISIONS for i in ids)
    ]
    assert not undeclared, (
        f"resolution scenarios produce identical State A output without being "
        f"declared as intended collisions: {undeclared}"
    )


@pytest.mark.harness
def test_scenarios_that_should_throw_do_throw_in_state_a(expected):
    """The `throws` flags describe State A, and are checked against it.

    Written from measurement rather than from reading the source, so this is what
    keeps them honest if the pinned upstream is ever moved.
    """
    wrong = []
    for s in R.SCENARIOS:
        r = expected.get(s.id)
        if s.throws and r.ok:
            wrong.append(f"{s.id}: marked as throwing, but State A compiled it")
        if not s.throws and not r.ok:
            wrong.append(
                f"{s.id}: marked as compiling, but State A raised "
                f"{_trim((r.error or {}).get('message'), 120)}"
            )
    assert not wrong, "harness defect -- scenario expectations disagree with State A:\n  " + "\n  ".join(wrong)
