#!/usr/bin/env python3
"""Post-build: what the build actually did.

The `provenance` and `sources` modules read the delivered tree. These checks read
the *evidence a real build left behind* — the compile database, the build log, the
build tree, the registered CTest commands and the install tree — and are the ones
that catch a build system that looks right but does something else:

  * fetching or vendoring a prebuilt library instead of compiling the sources,
  * registering `add_test(NAME auth COMMAND true)` so `ctest` is green,
  * compiling sources from outside the repository,
  * writing generated files into the source tree,
  * reaching the network during configure or build.

Two checks here read the transcript for the old driver rather than for a
capability -- an Autotools artefact in the build tree, an `autoreconf` in the log.
Those ask whether the migration happened, which is stage 1's required gates over
both trees, so they are gated to a CMake delivery rather than asked of the oracle
they are recorded from.
"""
import hashlib
import json
import os
import re
import shlex

import pytest

import flavour

pytestmark = pytest.mark.migration

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import elfutil  # noqa: E402

with open(os.path.join(SUITE, "data", "tests.json")) as _fh:
    TESTS = json.load(_fh)
with open(os.path.join(SUITE, "data", "sources.json")) as _fh:
    SOURCES = json.load(_fh)
with open(os.path.join(SUITE, "data", "flags.json")) as _fh:
    FLAGS = json.load(_fh)

DEFAULT_TESTS = TESTS["default"]
CHECKSUMS = SOURCES["checksums"]
N_TU = FLAGS["n_translation_units"]          # 119

NETWORK_LOG_MARKERS = [
    "Downloading", "Cloning into", "git clone", "curl ", "wget ",
    "-- Populating",
]
STUB_COMMANDS = {"true", ":", "/bin/true", "/usr/bin/true", "echo", "/bin/echo",
                 "test", "exit"}


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------- the build did happen --

def test_default_build_succeeds(b_default):
    assert b_default.installed, (
        "the default configuration must configure, build and install "
        "cleanly:\n%s" % b_default.failure_summary())


def test_compile_database_exists(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    cc = b_default.compile_commands()
    assert cc, ("no usable compile_commands.json was produced; the build did "
                "not compile anything through CMake")


def test_translation_unit_count(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    lib = [p for p, _c in b_default.compile_commands()
           if "/src/libsodium/" in p.replace("\\", "/")]
    uniq = {os.path.basename(p) for p in lib}
    assert len(uniq) >= N_TU, (
        "only %d distinct library translation units were compiled; State A "
        "compiles %d. Part of the library was not built from source."
        % (len(uniq), N_TU))


def test_every_compiled_source_comes_from_the_repository(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    outside = []
    for path, _cmd in b_default.compile_commands():
        norm = os.path.normpath(path)
        if not norm.startswith(b_default.src) and not norm.startswith(b_default.bld):
            outside.append(norm)
    assert outside == [], (
        "the build compiled sources from outside the repository: %s"
        % sorted(set(outside))[:5])


def test_no_fetched_dependency_tree(b_default):
    if not b_default.configured:
        pytest.fail(b_default.failure_summary())
    for name in ("_deps", "downloads", "external", "third_party"):
        p = os.path.join(b_default.bld, name)
        assert not os.path.isdir(p), (
            "the build created %s/, which means it fetched or vendored code "
            "instead of building the repository's own sources" % name)


# Whether the CMake *source* declares a fetch was a check here -- a scan for
# FetchContent_Declare and friends. That reading is stage 1's: `ExternalProject_Add`
# pointed at a local directory is not a download, a `file(DOWNLOAD` inside a
# branch that never runs is not either, and a fetch spelled with a variable
# defeats the pattern. The two checks above and below are the observations: the
# build either created a dependency tree and said so in its transcript, or it did
# not, and both are true of the build that actually ran.


@pytest.mark.parametrize("marker", NETWORK_LOG_MARKERS)
def test_build_log_shows_no_network_activity(b_default, marker):
    if not b_default.configured:
        pytest.fail(b_default.failure_summary())
    blob = "\n".join(s.out for s in b_default.steps.values() if s)
    assert marker not in blob, (
        "the configure/build transcript contains %r, which suggests network "
        "access; the verifier runs with no network" % marker)


# ------------------------------------------- the tests are real, not stubbed --

@pytest.fixture(scope="session")
def ctest_commands(b_default):
    """{test_name: argv} -- what the test runner will actually execute.

    `Build.registered_commands()` reads it from `ctest --show-only=json-v1` for a
    CMake tree and from automake's `TESTS` for an Autotools one.  The question the
    checks below ask of it is the same either way: is the thing registered under
    this name a compiled program that links libsodium, or is it `true`.
    """
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    return b_default.registered_commands()


def test_ctest_json_available(ctest_commands):
    assert ctest_commands, (
        "the build system publishes no test commands, so nothing can be said "
        "about what its registered tests run; §1.8 requires the 80 programs to "
        "be registered as tests")


@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_ctest_command_runs_a_real_binary(ctest_commands, name, b_default):
    argv = ctest_commands.get(name)
    if argv is None:
        pytest.fail("test %r is not registered, so its command cannot be "
                    "inspected" % name)
    head = os.path.basename(argv[0]) if argv else ""
    assert head not in STUB_COMMANDS, (
        "test %r is registered as %s — a command that always succeeds is not a "
        "test" % (name, " ".join(argv[:3])))
    elves = []
    for tok in argv:
        if os.path.isabs(tok) and os.path.isfile(tok):
            if elfutil.file_type(tok).startswith("ELF"):
                elves.append(tok)
    assert elves, (
        "test %r runs %s, none of which is an executable produced by this build"
        % (name, " ".join(argv[:4])))


@pytest.mark.parametrize("name", DEFAULT_TESTS)
def test_test_binary_links_the_library(ctest_commands, name, b_default):
    argv = ctest_commands.get(name) or []
    binary = None
    for tok in argv:
        if os.path.isabs(tok) and os.path.isfile(tok) and \
                elfutil.file_type(tok).startswith("ELF"):
            binary = tok
            break
    if binary is None:
        pytest.fail("no executable found in the command for %r" % name)
    needed = elfutil.needed(binary)
    if any(n.startswith("libsodium.so") for n in needed):
        return
    undef = elfutil.dynamic_undefined(binary)
    exports = elfutil.dynamic_exports(binary)
    syms = set(undef) | set(exports)
    assert any(s.startswith(("sodium_", "crypto_", "randombytes_")) for s in syms), (
        "the binary for test %r neither links libsodium.so nor contains any "
        "libsodium symbol; it cannot be exercising the library" % name)


@pytest.mark.parametrize("name", DEFAULT_TESTS[:40])
def test_test_binary_is_not_trivial(ctest_commands, name):
    argv = ctest_commands.get(name) or []
    for tok in argv:
        if os.path.isabs(tok) and os.path.isfile(tok) and \
                elfutil.file_type(tok).startswith("ELF"):
            size = os.path.getsize(tok)
            assert size > 8 * 1024, (
                "the binary registered for %r is only %d bytes; it cannot be a "
                "compiled libsodium test" % (name, size))
            return
    pytest.fail("no executable found in the command for %r" % name)


# ------------------------------------------------- the sources stayed pristine --

def test_sources_unmodified_after_build(b_default):
    """The build must not rewrite the sources it compiles."""
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    bad = []
    for rel, want in CHECKSUMS.items():
        p = os.path.join(b_default.src, rel)
        if not os.path.isfile(p):
            bad.append("%s: missing after build" % rel)
            continue
        got = _sha(p)
        if got != want:
            bad.append("%s: %s != %s" % (rel, got[:12], want[:12]))
    assert bad == [], (
        "the build modified %d source files in place: %s"
        % (len(bad), bad[:5]))


def test_build_writes_nothing_into_the_source_tree(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    strays = []
    for dirpath, dirnames, files in os.walk(b_default.src):
        dirnames[:] = [d for d in dirnames if d not in (".git",)]
        for f in files:
            if f.endswith((".o", ".lo", ".la", ".so", ".a", ".obj")) or \
                    f in ("CMakeCache.txt", "config.h", "version.h",
                          "libsodium.pc", "Makefile", "build.ninja"):
                strays.append(os.path.relpath(os.path.join(dirpath, f),
                                              b_default.src))
    assert strays == [], (
        "an out-of-source build left generated files in the source tree: %s"
        % strays[:8])


@flavour.only(flavour.CMAKE, reason=(
    "a `config.status` and a build-tree `libtool` are what an Autotools build "
    "produces, so this asks whether the migration happened rather than whether a "
    "capability survived. That is `cmake_is_the_build` in stage 1, over both trees "
    "with a reviewer and required; here it would only restate it against the very "
    "tree every expectation in data/ was recorded from"))
def test_no_autotools_artifact_in_build_tree(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    bad = []
    for dirpath, _d, files in os.walk(b_default.bld):
        for f in files:
            if f in ("config.status", "config.log", "libtool", "configure") or \
                    f.endswith((".la", ".lo")):
                bad.append(os.path.relpath(os.path.join(dirpath, f), b_default.bld))
    assert bad == [], (
        "the build produced Autotools/libtool artifacts: %s — the old build "
        "system is still doing the work" % bad[:6])


def test_installed_library_was_built_now(b_default):
    """A committed prebuilt .so would be older than this build."""
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    lib = b_default.shared_lib()
    if lib is None:
        pytest.fail("no shared library installed")
    cfg = b_default.step("configure")
    assert cfg is not None, "no configure step recorded"
    # The library must be at least as new as the configure step's start.
    assert os.path.getmtime(lib) >= cfg.started - 5, (
        "the installed shared library predates this build; it was copied, not "
        "compiled")


def test_object_count_matches_translation_units(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    objs = set()
    for dirpath, _d, files in os.walk(b_default.bld):
        for f in files:
            if f.endswith(".o"):
                objs.add(os.path.join(dirpath, f))
    lib_objs = [o for o in objs if "test" not in os.path.basename(o).lower()]
    assert len(lib_objs) >= N_TU, (
        "the build tree holds only %d library object files; State A compiles "
        "%d translation units" % (len(lib_objs), N_TU))


def test_static_archive_is_built_from_this_builds_objects(b_default):
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    ar = b_default.static_lib()
    if ar is None:
        pytest.fail("no static library installed")
    members = elfutil.archive_members(ar)
    assert len(members) >= N_TU, (
        "libsodium.a holds %d members; State A's holds %d — it was not built "
        "from the repository's sources" % (len(members), N_TU))


def test_verbose_build_shows_real_compiler_invocations(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    lines = b_default.verbose_compile_lines()
    assert len(lines) >= N_TU, (
        "the verbose build transcript shows only %d compiler invocations; %d "
        "translation units must be compiled" % (len(lines), N_TU))


@flavour.only(flavour.CMAKE, reason=(
    "the patterns below are the Autotools driver itself -- `./configure`, "
    "`autoreconf`, `libtool --mode=`. Against an Autotools tree the check reads as "
    "'the Autotools build ran an Autotools build', which is true and measures "
    "nothing. Whether the submission still drives configure is `autotools_retired` "
    "and `cmake_is_the_build` in stage 1, and both are required gates"))
def test_build_does_not_invoke_a_shell_script_build(b_default):
    if not b_default.configured:
        pytest.fail(b_default.failure_summary())
    blob = "\n".join(s.out for s in b_default.steps.values() if s)
    for pat in (r"\bconfigure\b\s*$", r"\./configure", r"\bautoreconf\b",
                r"\blibtool\b\s+--mode"):
        m = re.search(pat, blob, re.M)
        assert m is None, (
            "the build transcript matches %r (%r): the Autotools build is "
            "still being driven" % (pat, blob[max(0, m.start() - 60):m.end() + 40]))


#: Paths and names the build is never handed and has no reason to know.  A build
#: runs inside $SRB_SUITE_WORK, so its own tree appears in every compile line and
#: cannot be a marker; what cannot appear innocently is the *grader's* side of the
#: container -- the suite directory, the result file, the other two stages.
GRADER_PATHS = ["/tests/behavioural", "/tests/audit",
                "/tests/verification", "/logs/verifier", "reward.json",
                "SRB_RESULT", "SRB_SUITE_DIR", "evaluation.toml"]


@pytest.mark.parametrize("marker", GRADER_PATHS)
def test_no_grader_paths_referenced_in_build_output(b_default, marker):
    """A build that went looking for the grader had to name it somewhere.

    Whether the build system *branches* on being graded is stage 1's question --
    a marker list cannot answer it, and naming the markers here would only tell a
    submission which strings to avoid. This is the narrower, checkable thing: the
    transcript of a real build, and whether it mentions a path nothing gave it.
    """
    if not b_default.configured:
        pytest.fail(b_default.failure_summary())
    blob = "\n".join(s.out for s in b_default.steps.values() if s)
    assert marker not in blob, (
        "the build transcript mentions %r, which is the grader's own side of the "
        "container and was never passed to the build" % marker)
