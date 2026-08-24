"""pycryptodome's own test suite, against the library the submission installed.

This is the only module that runs the delivered code rather than reading it, and
it is the one that matters most: a build-system migration that produces a
cryptography library computing different answers has failed regardless of how
clean the build files are.

39,245 distinct test ids, from the project's own vectors. Reported as one check
per test module rather than one per id, for two reasons. The report stays
readable -- 71 lines instead of 39,245 -- and the score stays honest:
`test_GCM` alone holds 31,535 ids, so per-id checks would make four fifths of
this module a single vector file, and a submission that broke every cipher except
GCM would still look mostly fine.

Nothing here knows what a meson.build looks like. The input is a JSON result the
`build` module produced by running the suite out of the install tree, and the
comparison is against the same suite's result on State A, frozen in
data/selftest.json.gz.
"""

from __future__ import annotations

import collections

import pytest

GROUP_DEPTH = 4  # Crypto.SelfTest.<package>.<module>
#: How many failing ids a single check names before it stops. Enough to see the
#: shape of a failure; a full list of 31,535 would bury the other 70 checks.
MAX_NAMED = 12


def _group(test_id: str) -> str:
    return ".".join(test_id.split(".")[:GROUP_DEPTH])


def _groups(tests: dict) -> dict:
    out: dict[str, dict] = collections.defaultdict(dict)
    for test_id, outcome in tests.items():
        out[_group(test_id)][test_id] = outcome
    return dict(out)


@pytest.fixture(scope="module")
def expected(data) -> dict:
    return data["selftest"]


@pytest.fixture(scope="module")
def observed(selftest_result) -> dict:
    return selftest_result


# The group list is State A's, and it has to exist at import time, before any
# fixture does -- so it is read from the shipped ground truth directly rather
# than through the `data` fixture. Same file, same bytes.
#
# Parametrising over the *submission's* result instead would let an install that
# produced nothing collect zero checks and score them all as passed. A group the
# submission never ran has to be a failing check, not an absent one.
def _expected_groups():
    import gzip
    import json
    import os
    from pathlib import Path

    path = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data/selftest.json.gz"
    if not path.is_file():
        return []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return sorted(_groups(json.load(fh)["tests"]))


EXPECTED_GROUPS = _expected_groups()


def test_the_suite_ran_at_all(observed):
    """A result with no tests in it is a failed run, not a passed one."""
    assert not observed.get("crashed"), (
        f"the self-test suite did not run against the installed library: "
        f"rc={observed.get('child_rc')} {str(observed.get('output', ''))[-800:]}"
    )
    assert observed.get("recorded", 0) > 0, (
        "the self-test suite recorded no test ids, so the installed library "
        "was never exercised"
    )


def test_the_expected_ground_truth_is_present():
    """71 groups from State A. Zero means the image shipped no ground truth."""
    assert EXPECTED_GROUPS, (
        "data/selftest.json.gz holds no test groups, so this module has nothing "
        "to compare against and every check below would pass vacuously"
    )
    assert len(EXPECTED_GROUPS) == 71, (
        f"State A's frozen result holds {len(EXPECTED_GROUPS)} test modules, expected 71"
    )


def test_the_whole_suite_was_collected(expected, observed):
    """As many test instances loaded as State A loads, from the same vectors.

    A library that loads 3,603 instances instead of 42,784 has not failed
    anything -- it never ran the known-answer vectors, which is the part that
    would notice a wrong compiler flag. Reported separately from the outcomes so
    that "fewer tests ran" cannot look like "all tests passed".

    "Not fewer than", not "equal to", and the asymmetry is deliberate. State A's
    figures were measured on the interpreter State A ships for; this image runs a
    newer one, and `Crypto.SelfTest` does branch on `sys.version_info` in a
    handful of places. Every one of those branches changes what a test asserts
    rather than whether it exists, so the id set should be identical -- but a
    count that came out higher is an interpreter difference, not a migration
    defect, and it should not cost the submission anything. A count that came out
    *lower* is exactly the failure this check exists for, and the per-module
    checks below still require every single State A id to have run, so nothing is
    given away by allowing the slack in one direction.
    """
    assert observed["collected"] >= expected["collected"], (
        f"the suite loaded {observed['collected']} test instances against the installed "
        f"library; State A loads {expected['collected']}. A smaller suite means vectors "
        f"were not found, not that the library is correct."
    )
    assert observed["recorded"] >= expected["recorded"], (
        f"{observed['recorded']} distinct test ids ran, State A runs {expected['recorded']}"
    )


@pytest.mark.parametrize("group", EXPECTED_GROUPS)
def test_test_module_passes(group, expected, observed):
    """Every id State A passes in this module, passing against the submission."""
    want = _groups(expected["tests"]).get(group, {})
    got = _groups(observed.get("tests", {})).get(group, {})

    missing = sorted(k for k in want if k not in got)
    regressed = sorted(k for k, v in got.items() if k in want and v != "pass" and want[k] == "pass")

    if missing:
        named = ", ".join(missing[:MAX_NAMED])
        more = f" (+{len(missing) - MAX_NAMED} more)" if len(missing) > MAX_NAMED else ""
        raise AssertionError(
            f"{len(missing)} of {len(want)} test ids in {group} did not run against the "
            f"installed library: {named}{more}"
        )
    if regressed:
        notes = observed.get("notes", {})
        detail = []
        for test_id in regressed[:3]:
            tail = str(notes.get(test_id, "")).strip().splitlines()[-1:] or [""]
            detail.append(f"{test_id} [{got[test_id]}] {tail[0][:200]}")
        more = f" (+{len(regressed) - MAX_NAMED} more)" if len(regressed) > MAX_NAMED else ""
        raise AssertionError(
            f"{len(regressed)} of {len(want)} test ids in {group} stopped passing: "
            + " | ".join(detail) + more
        )


def test_no_test_was_silently_skipped(expected, observed):
    """State A skips nothing. A skip here is a capability that went missing.

    Separate from the per-module checks because a skip is not a wrong answer:
    it is the library declining to answer, which usually means an optional
    backend did not load. Worth its own line in the report.
    """
    skipped = sorted(k for k, v in observed.get("tests", {}).items() if v == "skip")
    assert not skipped, (
        f"{len(skipped)} test ids were skipped; State A's run skips none. "
        f"First: {', '.join(skipped[:MAX_NAMED])}"
    )


def test_the_math_backend_is_the_same(expected, observed):
    """Which big-integer implementation answered, asked of the library.

    pycryptodome picks between GMP, its own `_modexp` object and a pure-Python
    integer at import time, and the fallbacks pass every test while being orders
    of magnitude slower -- so the choice is invisible in the outcomes above.

    Honest about what this can and cannot catch: GMP is tried first and comes
    from the image, so on this image the expected answer is `gmp` whatever the
    submission does, and only a build that broke `Crypto.Math`'s imports outright
    would move it. The `_modexp` object that the `custom` backend needs is
    covered where it belongs, in the artefacts module, which loads all 41
    libraries through the project's own ctypes loader.
    """
    want = expected.get("implementation", {})
    got = observed.get("implementation", {})
    assert got.get("library") == want.get("library"), (
        f"the installed library's integer backend is {got.get('library')!r}; "
        f"State A's is {want.get('library')!r}. A silent fallback passes the vectors "
        f"and is much slower."
    )
    assert got.get("api") == want.get("api"), (
        f"the installed library reaches its compiled code through {got.get('api')!r}, "
        f"State A through {want.get('api')!r}"
    )
