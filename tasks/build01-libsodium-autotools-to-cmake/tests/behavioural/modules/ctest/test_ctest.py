#!/usr/bin/env python3
"""Behavioural: the test suite still runs through the build system (contract §1.8).

State A runs its 80 test programs through `make check`. State B must run the
same 80 through CTest, under their Autotools names, and they must actually
pass — the test programs themselves are byte-identical to State A's, so a
failure here means the library was mis-built, not that a test is wrong.

What the checks below read is the *interface*, not the driver: which tests the
build system says it has, and what each of them did when run. `ctest_list()` and
`ctest_results()` answer that from `ctest -N` and CTest's own table for a CMake
tree, and from automake's `TESTS` and its `PASS:`/`FAIL:` lines for an Autotools
one. Three checks below cannot be phrased that way -- a CTestTestfile.cmake, a
second generator, an option automake does not have -- and they are gated rather
than left to pass vacuously.
"""
import json
import os

import pytest

import flavour

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with open(os.path.join(SUITE, "data", "tests.json")) as _fh:
    TESTS = json.load(_fh)

DEFAULT_TESTS = TESTS["default"]            # 80
MINIMAL_TESTS = TESTS["minimal"]            # 71
MINIMAL_EXCLUDED = TESTS["minimal_excluded"]  # 9


@pytest.fixture(scope="session")
def ctest_default(b_default):
    if not b_default.built:
        pytest.fail("default build failed, so CTest cannot be inspected:\n%s"
                    % b_default.failure_summary())
    return b_default.ctest_list()


@pytest.fixture(scope="session")
def ctest_minimal(b_minimal):
    if not b_minimal.built:
        pytest.fail("minimal build failed:\n%s" % b_minimal.failure_summary())
    return b_minimal.ctest_list()


@pytest.fixture(scope="session")
def ctest_run_default(b_default):
    if not b_default.built:
        pytest.fail("default build failed:\n%s" % b_default.failure_summary())
    return b_default.ctest_results()


@pytest.fixture(scope="session")
def ctest_run_minimal(b_minimal):
    if not b_minimal.built:
        pytest.fail("minimal build failed:\n%s" % b_minimal.failure_summary())
    return b_minimal.ctest_results()


# ---------------------------------------------------------------- registration

def test_ctest_knows_any_test(ctest_default):
    assert ctest_default, (
        "`ctest -N` in the build tree lists no tests at all; §1.8 requires the "
        "80 test programs to be registered with CTest")


@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_test_registered(ctest_default, name):
    assert name in ctest_default, (
        "test %r is not registered with CTest; State A runs it under `make "
        "check` (§1.8)" % name)


def test_no_extra_tests_registered(ctest_default):
    extra = sorted(set(ctest_default) - set(DEFAULT_TESTS))
    assert extra == [], (
        "CTest registers tests State A does not have: %s — a repository port "
        "adds no tests (§1.8, §3)" % extra)


def test_registered_test_count(ctest_default):
    assert len(ctest_default) == len(DEFAULT_TESTS), (
        "CTest registers %d tests, State A has %d"
        % (len(ctest_default), len(DEFAULT_TESTS)))


def test_registered_names_are_unique(ctest_default):
    dupes = sorted({n for n in ctest_default if ctest_default.count(n) > 1})
    assert dupes == [], "duplicate CTest test names: %s" % dupes


# ------------------------------------------------------------------- execution

@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_test_passes(ctest_run_default, name):
    if name not in ctest_run_default:
        pytest.fail("CTest never ran %r (it is not registered or the run "
                    "aborted early) (§1.8)" % name)
    assert ctest_run_default[name], (
        "test %r fails under CTest. Its source is byte-identical to State A's, "
        "so the library or the test wiring is wrong (§1.8)" % name)


def test_ctest_exit_status_clean(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    s = b_default.ctest_run()
    assert s.rc == 0, (
        "`ctest` exits %d; State A's `make check` is green (§1.8):\n%s"
        % (s.rc, s.tail(40)))


def test_ctest_ran_every_test(ctest_run_default):
    missing = sorted(set(DEFAULT_TESTS) - set(ctest_run_default))
    assert missing == [], (
        "CTest did not execute %d of State A's tests: %s (§1.8)"
        % (len(missing), missing[:10]))


def test_no_test_reports_not_run(ctest_run_default):
    """A registered-but-unbuildable test is worse than a missing one."""
    failed = sorted(n for n, ok in ctest_run_default.items() if not ok)
    assert failed == [], "failing CTest tests: %s" % failed


def test_ctest_is_parallel_safe(b_default):
    """§1.8: `ctest -j` must not corrupt the run.

    State A's tests each write <name>.res in the working directory, so a naive
    port that runs them all in one shared directory races.
    """
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    par = b_default.ctest_results(parallel=True)
    seq = b_default.ctest_results(parallel=False)
    par_bad = sorted(n for n, ok in par.items() if not ok)
    seq_bad = sorted(n for n, ok in seq.items() if not ok)
    assert par_bad == seq_bad, (
        "the outcome depends on parallelism: `ctest -j` fails %s while serial "
        "ctest fails %s — the tests share a working directory or output file "
        "(§1.8)" % (par_bad[:8], seq_bad[:8]))


def test_serial_ctest_is_green(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    s = b_default.ctest_run(parallel=False)
    assert s.rc == 0, ("serial `ctest` exits %d:\n%s" % (s.rc, s.tail(40)))


# ------------------------------------------------------------------ minimal set

@pytest.mark.parametrize("name", MINIMAL_TESTS)
def test_minimal_registers_test(ctest_minimal, name):
    assert name in ctest_minimal, (
        "the minimal configuration does not register %r; State A's "
        "--enable-minimal still runs it (§1.8)" % name)


@pytest.mark.parametrize("name", MINIMAL_EXCLUDED)
def test_minimal_omits_test(ctest_minimal, name):
    assert name not in ctest_minimal, (
        "the minimal configuration registers %r, but that test exercises API "
        "that SODIUM_MINIMAL removes — it cannot link (§1.8)" % name)


def test_minimal_test_count(ctest_minimal):
    assert len(ctest_minimal) == len(MINIMAL_TESTS), (
        "minimal registers %d tests, State A's --enable-minimal registers %d"
        % (len(ctest_minimal), len(MINIMAL_TESTS)))


def test_minimal_ctest_is_green(b_minimal):
    if not b_minimal.built:
        pytest.fail(b_minimal.failure_summary())
    s = b_minimal.ctest_run()
    assert s.rc == 0, (
        "`ctest` in the minimal configuration exits %d:\n%s" % (s.rc, s.tail(40)))


@pytest.mark.parametrize("name", MINIMAL_TESTS)
def test_minimal_test_passes(ctest_run_minimal, name):
    if name not in ctest_run_minimal:
        pytest.fail("minimal CTest never ran %r (§1.8)" % name)
    assert ctest_run_minimal[name], (
        "test %r fails in the minimal configuration (§1.8)" % name)


# ---------------------------------------------------------- tests-off behaviour

@flavour.only(flavour.CMAKE, reason=(
    "there is no configure switch to turn the tests off: automake builds the test "
    "programs under `make check` and never under `make`, so a plain build already "
    "registers and produces nothing. The capability §1.2 asks for is present, but "
    "as the default rather than as an option, and 'the option that does not exist "
    "registers nothing' is not a measurement"))
def test_tests_off_registers_nothing(b_notests):
    if not b_notests.built:
        pytest.fail("SODIUM_BUILD_TESTS=OFF build failed:\n%s"
                    % b_notests.failure_summary())
    names = b_notests.ctest_list()
    assert names == [], (
        "SODIUM_BUILD_TESTS=OFF still registers %d CTest tests: %s (§1.2/§1.8)"
        % (len(names), names[:6]))


def test_tests_off_builds_no_test_binaries(b_notests):
    if not b_notests.built:
        pytest.fail(b_notests.failure_summary())
    found = []
    for dirpath, _d, files in os.walk(b_notests.bld):
        for f in files:
            if f in ("sodium_utils", "aead_aes256gcm", "codecs", "sodium_core"):
                found.append(os.path.join(dirpath, f))
    assert found == [], (
        "SODIUM_BUILD_TESTS=OFF still produced test binaries: %s" % found[:4])


def test_tests_off_still_installs_library(b_notests):
    assert b_notests.installed, (
        "SODIUM_BUILD_TESTS=OFF broke the install step:\n%s"
        % b_notests.failure_summary())
    assert b_notests.shared_lib(), (
        "SODIUM_BUILD_TESTS=OFF installed no shared library — the option must "
        "only affect tests (§1.2)")


# ------------------------------------------------------- tests are not products

def test_test_binaries_not_installed(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    names = set(DEFAULT_TESTS)
    bad = [p for k, p in b_default.install_entries()
           if k == "f" and os.path.basename(p) in names]
    assert bad == [], (
        "test programs were installed into the prefix: %s — State A installs "
        "none (§1.3)" % bad[:5])


def test_test_data_not_installed(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    bad = [p for k, p in b_default.install_entries()
           if p.endswith((".exp", ".res", ".c")) and "include" not in p]
    assert bad == [], "test fixtures leaked into the install tree: %s" % bad[:5]


@flavour.only(flavour.CMAKE, reason=(
    "CTestTestfile.cmake is what `add_test()` writes, and requiring it is how this "
    "stage distinguishes a registered test from a script that happens to run one. "
    "An Autotools tree registers through `TESTS` in a Makefile.am, which the "
    "registration checks above read; there is no second artefact to require"))
def test_ctest_config_file_lives_in_build_tree(b_default):
    assert os.path.isfile(os.path.join(b_default.bld, "CTestTestfile.cmake")), (
        "no CTestTestfile.cmake in the build tree; tests were registered by "
        "some mechanism other than add_test() (§1.8)")


@flavour.only(flavour.CMAKE, reason=(
    "the generator is a CMake concept; `b_make` and `b_default` are the same build "
    "under Autotools, so this would compare a tree with itself"))
def test_ctest_works_with_makefiles_generator(b_make):
    if not b_make.built:
        pytest.fail("Unix Makefiles build failed:\n%s" % b_make.failure_summary())
    names = b_make.ctest_list()
    missing = sorted(set(DEFAULT_TESTS) - set(names))
    assert missing == [], (
        "the Unix Makefiles build registers a different test set; missing %s "
        "(§1.1/§1.8)" % missing[:8])


@flavour.only(flavour.CMAKE, reason=(
    "same tree as the default build under Autotools; running its suite a second "
    "time measures nothing the checks above have not already measured"))
def test_make_generator_ctest_is_green(b_make):
    if not b_make.built:
        pytest.fail(b_make.failure_summary())
    s = b_make.ctest_run()
    assert s.rc == 0, (
        "`ctest` under the Unix Makefiles generator exits %d:\n%s"
        % (s.rc, s.tail(30)))
