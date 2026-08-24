"""Did the C++ leave the tree, and is there C# where it was?

Every check here reads files.  None of them decides anything: `swerefactor.scan`
records each with `required = False`, and the gate is the prose questions in
evaluation.toml, answered by a reviewer with both trees open.  What these produce is
the part a string can settle -- *which* of State A's twenty-one native translation
units is still on disk, *which* of its seventeen headers survived -- so the reviewer
spends its turns on the part a string cannot: whether the C# is an interpreter or a
shell, and whether it is a port or a transliteration.

Two lists, two sources.  The file names come from State A itself, because a list of
them written here would be a second description of the upstream release and free to
drift from it.  The rules -- which directories must go, which prefixes are exempt --
come from `source-contract.json`, the same bytes the submitter was handed, because a
rule restated in a scan module is a rule that can disagree with the one the task
actually states.  Neither list is typed into this file.
"""

from __future__ import annotations

import pytest
import srbscan
from srbscan import ORIGINAL, REPO

pytestmark = pytest.mark.scan


#: Extensions that make a file a translation unit rather than a header.  Both are
#: needed: `core/` ships twelve `.cpp` and also two `.c` -- the ABI smoke tests --
#: and a list built from `*.cpp` alone would leave two of State A's twenty-one
#: implementation units unnamed and therefore unlooked-for.
UNIT_EXTS = (".c", ".cc", ".cpp", ".cxx")
HEADER_EXTS = (".h", ".hh", ".hpp", ".hxx")


def _names(subdir: str, exts: tuple[str, ...]) -> list[str]:
    """The basenames directly under ``ORIGINAL/subdir`` with one of ``exts``.

    Derived from State A rather than typed here: a list written in the scan is a
    second description of the upstream release, free to drift from the release
    itself, and a name that drifts is a check that cannot fail.
    """
    base = ORIGINAL / subdir
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir()
                  if p.is_file() and p.suffix.lower() in exts)


#: State A's C++ implementation, by directory.  `core/` is the interpreter, `cmd/`
#: the two programs.  Test translation units are included: a submission that kept
#: `core/lexer_test.cpp` kept C++ that something still has to compile.
CORE_UNITS = _names("core", UNIT_EXTS)
CORE_HEADERS = _names("core", HEADER_EXTS)
CMD_UNITS = _names("cmd", UNIT_EXTS) + _names("cmd", HEADER_EXTS)

#: The three interfaces the migration removes rather than ports: the C ABI, the C++
#: binding, and the CPython extension.  Named separately from `core/` because their
#: presence means something different -- a surviving `libjsonnet.h` is usually a
#: port that kept the old public surface, not a port that kept the old engine.
ABI_HEADERS = _names("include", HEADER_EXTS)
BINDING_UNITS = _names("cpp", UNIT_EXTS) + _names("cpp", HEADER_EXTS)
PYTHON_EXT = _names("python", UNIT_EXTS)

#: The generator that turned std.jsonnet into a C array.  Data, not implementation
#: -- but its presence with no C# that reads std.jsonnet is worth a look.
STDLIB_TOOLS = _names("stdlib", UNIT_EXTS)

#: Directories and files whose purpose disappears with the C++, read from the
#: contract rather than restated.  Their presence is the finding; C++ *inside* one is
#: the same finding twice, so the per-unit checks above annotate rather than repeat.
FORBIDDEN_DIRS = tuple(srbscan.contract_section("forbidden_paths", "directories"))
FORBIDDEN_FILES = tuple(srbscan.contract_section("forbidden_paths", "files"))

#: Bazel is a second build system State B does not use.  Matched by basename at any
#: depth, which is what the contract says and is load-bearing: five of State A's
#: fourteen sit outside the forbidden directories, one of them inside `stdlib/`,
#: which must otherwise be preserved.
BAZEL_NAMES = tuple(srbscan.contract_section("forbidden_paths", "bazel_build_names"))
BAZEL_EXTS = tuple(
    srbscan.contract_section("forbidden_paths", "bazel_build_extensions"))

#: The native extensions the contract names, unioned with the wider set `srbscan`
#: knows about.  The contract's ten are the ones a submission is judged on; the
#: extras (`.m`, `.mm`, `.s`, `.sx`) are reported the same way because a hand-written
#: assembly file in a managed port is worth a look even though no rule names it.
NATIVE_EXTS = tuple(sorted(
    set(srbscan.contract_section("native_code_policy", "extensions"))
    | set(srbscan.CPP_SOURCE_SUFFIXES)))

#: Prefixes under which a native extension is not implementation source: golden
#: output whose names end in `.cpp`, an unrelated demo, documentation assets.
NATIVE_ALLOWED = tuple(
    srbscan.contract_section("native_code_policy", "allowed_prefixes"))


# ------------------------------------------------- the C++, unit by unit

@pytest.mark.parametrize("name", CORE_UNITS or ["<state-a-unreadable>"])
def test_core_translation_unit_is_gone(name):
    """One check per interpreter translation unit State A shipped, by name.

    Per-unit rather than "no .cpp files exist" because the answer is a list and the
    reviewer needs the list: three of these gone and eighteen present is a
    different submission from eighteen gone and three present, and the second is
    usually a migration that stalled rather than a cheat.
    """
    assert CORE_UNITS, "State A is not readable at /opt/original/core"
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "core/%s is still in the tree at %s. That is where State A's %s lived; "
        "whether it is still the implementation depends on what compiles it."
        % (name, ", ".join(sorted(hits)[:4]), name))


@pytest.mark.parametrize("name", CORE_HEADERS or ["<state-a-unreadable>"])
def test_core_header_is_gone(name):
    """The interpreter's own headers described its data layout.

    A surviving `ast.h` or `vm.h` is the interesting case: it usually means the C#
    is mirroring the C++ structs by hand rather than owning its own
    representation, which is a judgement for the reviewer and a fact worth handing
    over.
    """
    assert CORE_HEADERS, "State A is not readable at /opt/original/core"
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "core/%s is still in the tree at %s; it was internal to the C++ "
        "implementation and describes its layout"
        % (name, ", ".join(sorted(hits)[:4])))


@pytest.mark.parametrize("name", CMD_UNITS or ["<state-a-unreadable>"])
def test_cmd_translation_unit_is_gone(name):
    """`jsonnet.cpp`, `jsonnetfmt.cpp` and their shared argument helpers."""
    assert CMD_UNITS, "State A is not readable at /opt/original/cmd"
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "cmd/%s is still in the tree at %s; the two CLIs are supposed to be C# "
        "projects now" % (name, ", ".join(sorted(hits)[:4])))


@pytest.mark.parametrize("name", ABI_HEADERS or ["<state-a-unreadable>"])
def test_c_abi_header_is_gone(name):
    """The C ABI is out of scope and removed, not ported.

    Worth its own check rather than folding into the header sweep: a submission
    that kept `libjsonnet.h` has usually kept it *deliberately*, and the reviewer
    should see whether anything in the tree still claims to implement it.
    """
    assert ABI_HEADERS, "State A is not readable at /opt/original/include"
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "include/%s survives at %s. The C ABI is out of scope for this migration; "
        "check whether something in the tree still exports it."
        % (name, ", ".join(sorted(hits)[:4])))


@pytest.mark.parametrize("name", (BINDING_UNITS + PYTHON_EXT)
                         or ["<state-a-unreadable>"])
def test_binding_source_is_gone(name):
    """The C++ binding and the CPython extension, both out of scope."""
    assert BINDING_UNITS or PYTHON_EXT, "State A is not readable at /opt/original"
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "%s is still in the tree at %s; the bindings are removed rather than "
        "ported" % (name, ", ".join(sorted(hits)[:4])))


@pytest.mark.parametrize("name", STDLIB_TOOLS or ["<state-a-unreadable>"])
def test_stdlib_tool_is_gone(name):
    """`to_c_array.cpp` turned std.jsonnet into a C array at build time.

    Reported, not condemned on its own. What makes it interesting is the pair: this
    file present *and* a generated C# array of the stdlib's bytes means the port
    kept the embedding trick, which the reviewer should compare against
    `stdlib-is-interpreted`.
    """
    assert STDLIB_TOOLS, "State A is not readable at /opt/original/stdlib"
    hits = [srbscan.rel(REPO, p) for p in srbscan.walk_source(REPO)
            if p.name == name]
    assert not hits, (
        "stdlib/%s survives at %s. It existed to embed std.jsonnet as a C array; "
        "check how the C# reaches the stdlib instead."
        % (name, ", ".join(sorted(hits)[:4])))


# ------------------------------------------- C++ anywhere, by extension

@pytest.mark.parametrize("suffix", NATIVE_EXTS)
def test_no_native_source_with_suffix(suffix, files):
    """One check per native extension, over the whole tree.

    The by-name checks above cover State A's own files. This covers native source
    that *arrived*: a `port.cpp` nobody in State A had, a `.hpp` under a vendored
    path, a `.s` next to the C#. Paths the contract exempts are annotated rather
    than dropped -- `test_cmd/` holds golden output whose names end in `.cpp` -- so
    the reviewer sees both the count and which part of it is expected.
    """
    hits = sorted(srbscan.rel(REPO, p) for p in files
                  if p.suffix.lower() == suffix)
    if not hits:
        return
    inside = [h for h in hits if h.startswith(NATIVE_ALLOWED)]
    outside = [h for h in hits if not h.startswith(NATIVE_ALLOWED)]
    note = ""
    if inside:
        note = (" (%d under %s, which the contract exempts as data or "
                "documentation)" % (len(inside), ", ".join(NATIVE_ALLOWED)))
    assert not outside, (
        "%d file(s) with the native suffix %s: %s%s"
        % (len(hits), suffix, ", ".join(sorted(outside)[:6]), note))


@pytest.mark.parametrize("name", FORBIDDEN_DIRS)
def test_forbidden_directory_is_gone(name):
    """The directories the migration removes, one check each.

    A surviving directory is not automatically a defect -- an empty `core/` holding
    only a README is nothing -- so the report says what is *in* it. That is the
    difference between "the C++ is still here" and "a directory name survived",
    and it is the reviewer's call which one this is.
    """
    assert (ORIGINAL / name).is_dir(), (
        f"the contract lists {name}/ as forbidden but State A has no such "
        f"directory, so this check can only ever pass")
    target = REPO / name
    if not target.is_dir():
        return
    contents = sorted(srbscan.rel(REPO, p)
                      for p in srbscan.walk_source(target))
    assert not contents, (
        "%s/ still exists with %d file(s): %s. State A's %s held part of the C++ "
        "implementation." % (name, len(contents), ", ".join(contents[:6]), name))


@pytest.mark.parametrize("relative", FORBIDDEN_FILES)
def test_forbidden_file_is_gone(relative):
    """The build files and packaging of the system being left behind.

    `Makefile`, the two CMake files, `setup.py` and `MANIFEST.in` build or package
    the C++; `stdlib/to_c_array.cpp` embeds the stdlib as a C array; the
    `Dockerfile`, `.travis.yml` and `tests.sh` drive all of it. Each is checked at
    the path the contract names, and the assertion above it is that State A has
    that path -- a forbidden file State A never shipped is a rule that cannot fail.
    """
    assert (ORIGINAL / relative).exists(), (
        f"the contract lists {relative} as forbidden but State A has no such "
        f"file, so this check can only ever pass")
    target = REPO / relative
    assert not target.exists(), (
        "%s is still in the tree. It belongs to the C++ build and packaging, which "
        "State B does not use." % relative)


@pytest.mark.parametrize("name", BAZEL_NAMES + BAZEL_EXTS)
def test_no_bazel_build_file(name, files):
    """Bazel files by basename or extension, at any depth.

    By basename because that is how they are written: `stdlib/BUILD` is a Bazel
    package in a directory the migration must otherwise preserve, so a check keyed
    on the repository root would miss five of State A's fourteen. The count from
    State A is in the message, which is what makes a partial cleanup legible.
    """
    if name.startswith("."):
        matched = [p for p in files if p.suffix == name]
        in_original = [p for p in srbscan.walk_source(ORIGINAL)
                       if p.suffix == name]
    else:
        matched = [p for p in files if p.name == name]
        in_original = [p for p in srbscan.walk_source(ORIGINAL) if p.name == name]
    hits = sorted(srbscan.rel(REPO, p) for p in matched)
    assert not hits, (
        "%d Bazel file(s) named %s survive: %s. State A shipped %d of them; Bazel "
        "is a build system State B does not use."
        % (len(hits), name, ", ".join(hits[:8]), len(in_original)))


# ------------------------------------------------- is there C# where it was

def test_csharp_exists(cs_files):
    """There is C# in the tree at all.

    The floor a submission clears by having done anything. Reported here so that
    an empty tree and a tree with a stub are distinguishable in the findings, which
    is what the `is-an-implementation` gate needs.
    """
    assert cs_files, (
        "no .cs file anywhere in the submission. Nothing was ported.")


def test_csharp_logic_line_floor(cs_files):
    """A crude size floor, stated as a floor because it cannot judge quality.

    State A's implementation surface -- the nine `core/*.cpp`, its thirteen
    headers, and `cmd/` -- is 11,198 lines that are neither blank nor
    comment-only. A C# reimplementation is not required to match that, and a good
    one may well be shorter. What this distinguishes is 800 lines from 15,000: at
    800 nobody has reimplemented a language, whatever the code looks like.

    Advisory like everything here. The reviewer has the tree and can see whether a
    small port is a tight one or an empty one.
    """
    floor = 4000
    total = srbscan.count_logic_lines(cs_files)
    assert total >= floor, (
        "%d line(s) of C# logic across %d file(s), below the %d-line floor. "
        "State A's C++ implementation surface is 11,198 lines by the same count. "
        "A port this small is either incomplete or is calling something else."
        % (total, len(cs_files), floor))


@pytest.mark.srb_skip_ok
def test_every_csharp_file_is_in_a_project(cs_files, files):
    """C# that no project compiles is C# that does not run.

    MSBuild globs `**/*.cs` by default, so a file is normally included by being
    under a project directory. This reports the ones that are not under any
    directory holding a `.csproj`, which is how dead code and abandoned first
    attempts show up -- and occasionally how a second implementation hides.
    """
    project_dirs = {p.parent for p in files if p.suffix == ".csproj"}
    if not project_dirs:
        pytest.skip("no .csproj in the tree; the build-closure checks cover this")
    orphans = sorted(
        srbscan.rel(REPO, p) for p in cs_files
        if not any(d == p.parent or d in p.parents for d in project_dirs))
    assert not orphans, (
        "%d .cs file(s) under no directory containing a .csproj: %s. Nothing "
        "compiles them, so whatever they contain is not what runs."
        % (len(orphans), ", ".join(orphans[:8])))


def test_mount_is_a_repository():
    """The scan is pointed at something that looks like the task's tree.

    Not a finding about the submission -- a finding about the run. A scan pointed
    one level off derives empty lists and reports a clean tree, which is the one
    failure mode where silence is actively misleading.
    """
    assert REPO.is_dir(), f"no submission tree at {REPO}"
    assert ORIGINAL.is_dir(), f"no State A tree at {ORIGINAL}"
    markers = [m for m in srbscan.ROOT_MARKERS if (ORIGINAL / m).exists()]
    assert markers, (
        f"{ORIGINAL} holds none of {srbscan.ROOT_MARKERS}; State A is not "
        f"mounted where the scan expects it and every list derived from it is "
        f"empty")
