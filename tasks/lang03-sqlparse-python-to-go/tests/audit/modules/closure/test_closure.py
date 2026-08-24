"""Did the Python leave, and is there Go where it was.

The mechanical half of `no-python-implementation` and `go-present`.  Every check
here answers a question a string can answer, so that the review spends its turns on
the two it cannot: whether the Go is an implementation or a shell around one, and
whether it is a port or a transliteration.

Per module and per suffix rather than one "no Python remains" check, because the
answer a reviewer needs is a list.  "Twelve of State A's twenty-one modules are
still on disk" and "sqlparse/keywords.py is still on disk" lead to different
readings, and a single boolean gives neither.

The module names come from the mounted original rather than from a list written
here.  A list would be a second description of the upstream release, free to drift
from the first, and this scan already carries one description of the task -- the
contract -- which it reads instead of restating.

Every check that can skip carries `srb_skip_ok`, and that marker is not decoration.
Without it the harness scores a skip as a miss, which is right for stage 2 -- an
offline module skips only when the artefact it needed is missing -- and wrong here:
these skips mean another check in this scan is the decisive one, and an unmarked skip
would reach the reviewer as a flagged finding claiming the opposite of what happened.
So a skip added later needs the same question asked of it: is some other check
already reporting this, or is the reviewer being told nothing?
"""

from __future__ import annotations

import re

import pytest
import srbscan
from srbscan import CONTRACT, ORIGINAL, REPO

pytestmark = pytest.mark.scan

#: State A's implementation modules, derived from `original/sqlparse`.  The fallback
#: keeps collection working against the empty mount the build-time check uses: an
#: empty parametrize list is a collection error, and a scan that fails to collect
#: produces no findings, which in the rendered prompt looks exactly like a clean
#: tree.
MODULES = srbscan.state_a_python_modules() or ["<state-a-unreadable>"]

#: From the contract's `python_policy.must_delete`.  Narrower than `removable_paths`
#: and different in kind: these three must not survive in any form, where the
#: removable set is material a port has no use for but may keep.
MUST_DELETE = tuple(
    CONTRACT.get("python_policy", {}).get("must_delete", ())
) or ("<contract-unreadable>",)

#: The packages the contract requires, from `go_contract.packages`.  Read rather
#: than listed for the same reason as the module names above.
GO_PACKAGES = tuple(sorted(CONTRACT.get("go_contract", {}).get("packages", {}))) or (
    "<contract-unreadable>",
)


def test_contract_file_is_readable():
    """The scan's own copy of source-contract.json parsed.

    First check in the module by design.  Several parametrize lists above degrade to
    a one-element fallback when this file cannot be read, and a degraded list still
    collects and still passes -- so without this check a missing contract would make
    the scan quieter rather than louder, which is the failure mode the whole stage
    exists to avoid.
    """
    assert CONTRACT.get("task") == "lang03-sqlparse-python-to-go", (
        "the scan could not read data/source-contract.json, so the checks that "
        "derive their lists from it are running against fallbacks and proving "
        "nothing. This is a defect in the verifier, not in the submission.")


# ------------------------------------------------------- the Python is gone

@pytest.mark.parametrize("relative", MODULES)
@pytest.mark.srb_skip_ok
def test_state_a_module_is_gone(relative):
    """One check per module State A shipped under sqlparse/.

    `sqlparse/` is the old implementation and the contract requires it gone -- not
    renamed, not moved under a subdirectory, not kept as reference material.  Named
    individually because which modules survived is the interesting part: a tree
    holding only `keywords.py` has kept the 809-entry keyword table it did not want
    to retype, and a tree holding all twenty-one has not started.
    """
    if relative == "<state-a-unreadable>":
        pytest.skip("/opt/original is empty; the mount check reports that")
    assert not (REPO / relative).exists(), (
        f"{relative} is still present. source-contract.json's python_policy requires "
        f"sqlparse/ to be gone, and the Go port is what replaces it.")


@pytest.mark.parametrize("suffix", sorted(set(srbscan.PYTHON_SUFFIXES)))
def test_no_file_with_python_suffix(suffix, files):
    """One check per suffix the contract forbids outright, anywhere.

    Wider than the per-module list above, and it is the wider one that matters: a
    port that moved `sqlparse/` to `reference/py/` passes every check above and fails
    here.  The contract's wording is "with no exceptions", so a `conftest.py` at the
    root and a `docs/source/conf.py` inherited from State A are both reported -- the
    second is upstream's Sphinx configuration and the reviewer will read it as such,
    but a scan that decided which `.py` files were innocent would be deciding the
    gate.
    """
    hits = sorted(srbscan.rel(REPO, p) for p in files
                  if p.suffix.lower() == suffix)
    assert not hits, (
        "%d file(s) with the forbidden %s suffix: %s. The contract's no_python_sources "
        "clause admits no exceptions."
        % (len(hits), suffix, ", ".join(hits[:12])))


@pytest.mark.parametrize("suffix", sorted(set(srbscan.PYTHON_ADJACENT_SUFFIXES)))
def test_no_file_with_python_adjacent_suffix(suffix, files):
    """Other spellings of Python source, which the contract does not name.

    A `.pyx` is not on the forbidden list, and this is a lead rather than a
    violation: what it catches is Python given a name the suffix check does not
    read.  Cython source in a Go repository has no innocent reading, but it is the
    reviewer that says so.
    """
    hits = sorted(srbscan.rel(REPO, p) for p in files
                  if p.name.lower().endswith(suffix))
    assert not hits, (
        "%d file(s) ending %s: %s. Not named by the contract's suffix list, which is "
        "why this is a lead: read what is in them."
        % (len(hits), suffix, ", ".join(hits[:12])))


@pytest.mark.parametrize("relative", MUST_DELETE)
@pytest.mark.srb_skip_ok
def test_must_delete_path_is_gone(relative):
    """One check per path the contract says must not survive in any form.

    Three of them: `sqlparse/` is the implementation, and `pyproject.toml` plus
    `.flake8` are what make the repository install and lint as a Python project.
    Separate from the suffix checks because a `pyproject.toml` holds no Python and
    is still the thing that makes this a Python package.
    """
    if relative == "<contract-unreadable>":
        pytest.skip("the contract is unreadable; the check above reports that")
    assert not (REPO / relative.rstrip("/")).exists(), (
        f"{relative} is still present, and the contract's must_delete list names it "
        f"specifically.")


@pytest.mark.srb_skip_ok
def test_no_python_source_survives_under_a_new_name(files):
    """A State A `.py` present with its name or extension changed.

    The check no per-path comparison can make: `sqlparse/keywords.py` copied to
    `internal/keywords/table.txt`, or to `attic/keywords`, has the same bytes and a
    name none of the suffix checks look at.  Compared by hash against every `.py`
    State A shipped, so a relocated copy is found wherever it went.

    An *edited* copy is not reported here at all, which is the limit of the method
    and worth stating: this finds the file that was moved, not the file that was
    moved and touched.  That case is the reviewer's, and it has both trees open.

    Reported for the whole tree rather than per file so that a submission which kept
    all thirty-five of State A's Python files under new names yields one finding
    naming them, not thirty-five findings.
    """
    baseline: dict[str, str] = {}
    if ORIGINAL.is_dir():
        for path in srbscan.walk_source(ORIGINAL):
            if path.suffix.lower() == ".py":
                baseline[srbscan.sha256(path)] = srbscan.rel(ORIGINAL, path)
    if not baseline:
        pytest.skip("/opt/original is empty; the mount check reports that")
    hits = []
    for path in files:
        origin = baseline.get(srbscan.sha256(path))
        if origin is None or srbscan.rel(REPO, path) == origin:
            continue
        hits.append("%s is State A's %s, byte for byte"
                    % (srbscan.rel(REPO, path), origin))
    assert not hits, (
        "%d relocated Python file(s): %s" % (len(hits), "; ".join(sorted(hits)[:10])))


def test_no_python_bytecode_under_another_name(files):
    """A `.pyc` renamed, found by its magic rather than its suffix.

    Bytes 2-4 of a CPython bytecode file are `\\r\\n` and bytes 0-2 are the version
    magic, so a `data/tables.bin` that is really a compiled module is identifiable
    without a Python to import it.  Worth its own check because bytecode is the one
    form of Python that carries no readable source: a reviewer grepping for `def `
    would find nothing in it.
    """
    hits = sorted(
        "%s (CPython bytecode, magic %d)"
        % (srbscan.rel(REPO, p),
           int.from_bytes(srbscan.magic(p, 2), "little"))
        for p in files
        if p.suffix.lower() not in srbscan.PYTHON_SUFFIXES and srbscan.is_pyc(p))
    assert not hits, "%d disguised bytecode file(s): %s" % (
        len(hits), "; ".join(hits[:10]))


# --------------------------------------------------------- there is Go here

def test_go_module_file_exists():
    """`go.mod` at the root, which is what makes this a Go module at all."""
    assert (REPO / "go.mod").is_file(), (
        "there is no go.mod at the root of the submission, so `go build ./...` has "
        "nothing to build and this is not a Go module.")


@pytest.mark.srb_skip_ok
def test_go_module_path_is_the_contracted_one():
    """The module path the contract publishes, since importers are held to it.

    Read rather than asserted-against-a-constant: the contract names it in two
    places, `build_contract.module_path` and `go_contract.module_path`, and a
    disagreement between those two is a defect in this task rather than in the
    submission -- so the check compares them to each other as well.
    """
    build_path = CONTRACT.get("build_contract", {}).get("module_path")
    go_path = CONTRACT.get("go_contract", {}).get("module_path")
    if not build_path:
        pytest.skip("the contract is unreadable; the first check reports that")
    assert build_path == go_path, (
        f"the contract names two module paths, {build_path!r} and {go_path!r}. That "
        f"is a verifier defect, not a submission defect.")
    text = srbscan.read_text(REPO / "go.mod")
    match = re.search(r"(?m)^\s*module\s+(\S+)", text)
    assert match, "go.mod declares no module path"
    assert match.group(1) == build_path, (
        f"go.mod declares module {match.group(1)!r}; the contract publishes "
        f"{build_path!r}, and stage 2 reads the path out of the built binary's own "
        f"buildinfo.")


def test_go_files_exist(go_files):
    """Some Go, anywhere.  A floor low enough that only an empty port fails it."""
    assert go_files, (
        "there is no .go file anywhere in the submission. Nothing later in this "
        "scan can say anything useful about a tree with no Go in it.")


def test_go_logic_lines_reach_the_published_floor(go_impl_files):
    """The floor source-contract.json publishes, over non-test Go only.

    3,500 lines against State A's 4,024 of Python.  The floor is below the original
    on purpose and the contract says why: a faithful Go port is normally *larger*,
    because the four regex features the reference's lexer depends on -- lookbehind,
    lookahead, backreferences and Unicode character classes -- do not exist in Go's
    regexp package and have to be written out by hand.

    A line count cannot tell an implementation from filler, and this check does not
    claim to.  What it does is put a number in front of the reviewer before it reads
    anything: a submission at 400 lines has stubbed the port, and a submission at
    9,000 has a different question to answer, which is whether the lines do anything.
    Test files are excluded so the floor cannot be reached on table-driven data.
    """
    floor = CONTRACT.get("go_code_policy", {}).get("min_go_logic_lines",
                                                   srbscan.MIN_GO_LOGIC_LINES)
    total = srbscan.count_go_logic_lines(go_impl_files)
    assert total >= floor, (
        "%d lines of non-comment, non-blank Go across %d non-test file(s); the "
        "contract's floor is %d and State A is %d lines of Python. Below this the "
        "port has been stubbed rather than written."
        % (total, len(go_impl_files), floor, srbscan.STATE_A_PYTHON_LINES))


@pytest.mark.parametrize("package", GO_PACKAGES)
@pytest.mark.srb_skip_ok
def test_contracted_package_directory_exists(package):
    """One check per package `go_contract.packages` names.

    Eleven of them, and the directory layout is fixed because the import paths are:
    a consumer writing `sqlparse.../keywords` needs a `keywords/` directory.  This
    is presence only -- whether the package exports what the contract requires is
    stage 2's `api` module, which compiles a program against the built module and
    can therefore answer it properly.
    """
    if package == "<contract-unreadable>":
        pytest.skip("the contract is unreadable; the first check reports that")
    target = REPO if package == "." else REPO / package
    if not target.is_dir():
        pytest.fail(f"{package}/ does not exist; the contract requires the package "
                    f"at that import path.", pytrace=False)
    go_here = sorted(p.name for p in target.glob("*.go")
                     if not p.name.endswith("_test.go"))
    assert go_here, (
        f"{package}/ exists but holds no non-test .go file, so there is no package "
        f"at that import path for a consumer to import.")


def test_go_files_declare_a_package(go_files):
    """Every `.go` file carries a `package` clause.

    Go's own compiler enforces this, so a failure means the file is not reachable by
    a build at all -- which is the interesting reading: a file that does not compile
    is a file nobody ran, and in a tree that otherwise passes it is where dead
    padding would sit.  This scan has no Go toolchain, so this is the strongest
    version of the claim available here.
    """
    hits = []
    for path in go_files:
        text = srbscan.read_text(path)
        if not re.search(r"(?m)^\s*package\s+[A-Za-z_]\w*", text):
            hits.append(srbscan.rel(REPO, path))
    assert not hits, (
        "%d .go file(s) with no package clause: %s. Go will not compile these, so "
        "nothing in the built artefact comes from them."
        % (len(hits), ", ".join(sorted(hits)[:12])))


def test_at_least_one_go_test_file_exists(go_files):
    """`test_files_required: 1` from the contract.

    A low bar, deliberately.  The port's own tests are not graded -- stage 2 has
    4,662 cases frozen from the reference and does not care what the submission
    asserts about itself -- so this is not a coverage claim.  It is here because a
    whole-repository rewrite with no test of its own is a fact about how the work was
    done, and the reviewer should have it.
    """
    required = CONTRACT.get("go_code_policy", {}).get("test_files_required", 1)
    tests = sorted(srbscan.rel(REPO, p) for p in go_files
                   if p.name.endswith("_test.go"))
    assert len(tests) >= required, (
        "%d _test.go file(s); the contract asks for at least %d. The submission's own "
        "tests are not graded, so this is an observation about the work rather than a "
        "measurement of it." % (len(tests), required))


def test_state_a_is_mounted():
    """The original is where the scan expects it.

    Last in the module rather than first, because when this fails almost everything
    above it has already skipped or passed vacuously -- and this message is the one
    that explains why.  A run that reports it is a misconfigured harness, not a
    submission defect.
    """
    assert ORIGINAL.is_dir() and any(ORIGINAL.iterdir()), (
        f"{ORIGINAL} is empty or absent. The per-module and by-hash checks derive "
        f"their lists from State A, so they proved nothing on this run.")
    assert (ORIGINAL / "sqlparse").is_dir(), (
        f"{ORIGINAL} does not hold sqlparse/, so it is either not State A or not "
        f"resolved to the repository root.")
