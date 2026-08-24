#!/usr/bin/env python3
"""Behavioural: behavioural equivalence against a verifier-owned test corpus.

Contract §3. This is the heart of the behavioural score and the part that cannot
be faked from inside the repository.

The 80 test programs are *not* taken from the agent's tree. The verifier ships
its own checksum-verified copy (the suite's data/corpus.tar.gz), compiles each
program against the *installed* library, and runs it with
TEST_SRCDIR pointing at the verifier's own expected-output files. So:

  * editing the repository's test/ directory changes nothing here;
  * weakening a .exp file changes nothing here;
  * the only way to pass is for the installed library to compute the same
    answers State A computes.

Each program is checked three ways: it must compile against the installed
headers, exit 0, and produce a .res file whose SHA-256 matches State A's.
Every configuration is exercised: default shared, default static, minimal.
"""
import json
import os

import pytest

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with open(os.path.join(SUITE, "data", "tests.json")) as _fh:
    TESTS = json.load(_fh)

DEFAULT_TESTS = TESTS["default"]
MINIMAL_TESTS = TESTS["minimal"]
GOLDENS = TESTS["goldens"]


def _result(bundle, name):
    if bundle["__unavailable__"]:
        pytest.fail("the library could not be exercised: %s"
                    % bundle["__unavailable__"])
    r = bundle["results"].get(name)
    if r is None:
        pytest.fail("the harness produced no result for %r" % name)
    return r


def _check_compiled(bundle, name):
    r = _result(bundle, name)
    assert r["compiled"], (
        "the verifier's own copy of %s.c does not compile against the installed "
        "library. Its source is State A's, unmodified, so a public header or "
        "the include layout is wrong (§1.3/§3):\n%s"
        % (name, (r["output"] or "")[-1200:]))
    return r


def _check_ran(bundle, name):
    r = _check_compiled(bundle, name)
    assert r["rc"] == 0, (
        "%s exits %d against the installed library (State A: 0). libsodium's "
        "cmptest harness exits 99 when the output differs from the expected "
        "file, so this is a behavioural regression (§3):\n%s"
        % (name, r["rc"], (r["output"] or "")[-1200:]))
    return r


def _check_golden(bundle, name):
    r = _check_ran(bundle, name)
    want = GOLDENS[name]
    assert r["res_sha"] == want["sha256"], (
        "%s produced %d bytes of output with SHA-256 %s; State A produces %d "
        "bytes with %s (§3)"
        % (name, r["res_bytes"], (r["res_sha"] or "")[:16], want["bytes"],
           want["sha256"][:16]))
    return r


# ------------------------------------------------------- default, shared link --

@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_corpus_shared_compiles(corpus_default_shared, name):
    _check_compiled(corpus_default_shared, name)


@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_corpus_shared_exit_status(corpus_default_shared, name):
    _check_ran(corpus_default_shared, name)


@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_corpus_shared_output_matches_state_a(corpus_default_shared, name):
    _check_golden(corpus_default_shared, name)


# ------------------------------------------------------- default, static link --

@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_corpus_static_exit_status(corpus_default_static, name):
    _check_ran(corpus_default_static, name)


@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_corpus_static_output_matches_state_a(corpus_default_static, name):
    _check_golden(corpus_default_static, name)


# ------------------------------------------------------------------- minimal --

@pytest.mark.parametrize("name", MINIMAL_TESTS)
def test_corpus_minimal_output_matches_state_a(corpus_minimal_shared, name):
    _check_golden(corpus_minimal_shared, name)


# ------------------------------------------------------------------ aggregates --

def test_shared_corpus_fully_available(corpus_default_shared):
    assert corpus_default_shared["__unavailable__"] is None, (
        "the default configuration produced no usable shared library: %s"
        % corpus_default_shared["__unavailable__"])


def test_static_corpus_fully_available(corpus_default_static):
    assert corpus_default_static["__unavailable__"] is None, (
        "the default configuration produced no usable static library: %s"
        % corpus_default_static["__unavailable__"])


def test_minimal_corpus_fully_available(corpus_minimal_shared):
    assert corpus_minimal_shared["__unavailable__"] is None, (
        "the minimal configuration produced no usable library: %s"
        % corpus_minimal_shared["__unavailable__"])


def test_every_shared_program_ran(corpus_default_shared):
    if corpus_default_shared["__unavailable__"]:
        pytest.fail(corpus_default_shared["__unavailable__"])
    missing = sorted(set(DEFAULT_TESTS) - set(corpus_default_shared["results"]))
    assert missing == [], "no result for %s" % missing[:8]


def test_shared_and_static_agree(corpus_default_shared, corpus_default_static):
    """The two link modes must compute identical answers."""
    if corpus_default_shared["__unavailable__"] or corpus_default_static["__unavailable__"]:
        pytest.fail("one of the link modes is unavailable")
    diff = []
    for name in DEFAULT_TESTS:
        a = corpus_default_shared["results"].get(name, {})
        b = corpus_default_static["results"].get(name, {})
        if a.get("res_sha") != b.get("res_sha"):
            diff.append(name)
    assert diff == [], (
        "these programs behave differently when linked statically vs. "
        "dynamically: %s — the two libraries were built from different sources "
        "or with different macros (§1.4/§3)" % diff[:8])


def test_minimal_agrees_with_default_where_applicable(corpus_default_shared,
                                                      corpus_minimal_shared):
    if corpus_default_shared["__unavailable__"] or corpus_minimal_shared["__unavailable__"]:
        pytest.fail("one of the configurations is unavailable")
    diff = []
    for name in MINIMAL_TESTS:
        a = corpus_default_shared["results"].get(name, {})
        b = corpus_minimal_shared["results"].get(name, {})
        if a.get("res_sha") != b.get("res_sha"):
            diff.append(name)
    assert diff == [], (
        "SODIUM_MINIMAL changes the answers of %s; it may only remove API, "
        "never alter results (§1.2/§3)" % diff[:8])


def test_no_program_crashed(corpus_default_shared):
    if corpus_default_shared["__unavailable__"]:
        pytest.fail(corpus_default_shared["__unavailable__"])
    crashed = sorted(n for n, r in corpus_default_shared["results"].items()
                     if r.get("rc", 0) < 0 or r.get("rc", 0) in (134, 139, 132))
    assert crashed == [], (
        "these programs crashed (signal/abort) against the installed library: "
        "%s — this usually means an ISA-specific implementation was compiled "
        "for the wrong target (§1.5)" % crashed)


def test_aggregate_pass_rate_is_total(corpus_default_shared):
    if corpus_default_shared["__unavailable__"]:
        pytest.fail(corpus_default_shared["__unavailable__"])
    bad = sorted(n for n in DEFAULT_TESTS
                 if corpus_default_shared["results"].get(n, {}).get("rc") != 0)
    assert bad == [], "%d of %d corpus programs fail: %s" % (
        len(bad), len(DEFAULT_TESTS), bad[:10])


def test_corpus_is_the_verifiers_own(corpus_default_shared):
    """Sanity: the corpus must come from the verifier image, not the repo."""
    runner = corpus_default_shared["runner"]
    srcdir = getattr(runner, "default", "")
    repo = os.environ.get("SRB_REPO", "/workspace/repo")
    assert srcdir and not srcdir.startswith(repo), (
        "the corpus runner is reading test sources from the agent's repository "
        "(%s); the verifier must use its own copy" % srcdir)
    assert os.path.isfile(os.path.join(srcdir, "cmptest.h")), (
        "the verifier's corpus is missing cmptest.h at %s" % srcdir)


def test_corpus_checksum_verified(corpus_default_shared):
    runner = corpus_default_shared["runner"]
    assert getattr(runner, "verified", False), (
        "the verifier's test corpus failed its own SHA-256 check; refusing to "
        "score against an unverified corpus")
