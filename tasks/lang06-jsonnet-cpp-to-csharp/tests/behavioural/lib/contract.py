"""source-contract.json: what the submission is told, rendered from one source.

This is the document handed to the agent at /opt/swerefactor/source-contract.json.  It
is the public half of the task -- what to build, what to preserve, what may not
remain -- and it is generated rather than typed so that it cannot drift from the
lists the rest of this directory uses.

It says nothing about grading: no aggregation formula, no per-family weight table,
no list of gate ids.

The weights and the formula would be a rubric, and a rubric tells a solver where
the cheap marks are.  A submission that read "upstream-eval: 0.18, parsejson: 0.01"
would learn that eighteen times more credit rides on replaying a directory of
golden files than on parsing JSON, which is not a fact about Jsonnet -- and stage
1's questions are not answerable by budgeting effort anyway.

A list of gates would be worse, because it would read as a list of *checks*.  Stage
1 is a reviewer reading both trees and answering nine questions in prose; there is
no regex whose evasion is a pass, and a checklist would invite exactly that
reading.

What the document carries is the requirements themselves, stated as requirements,
in the sections they belong to.  Nothing is enforced that is not stated here: the
substance of every stage-1 question appears somewhere in this document, because a
requirement that costs a submission its score while living only in the grader's
prompt is an authoring bug and not a hard task.

    forbidden_paths      <- FORBIDDEN_DIRS / FORBIDDEN_FILES / BAZEL_*  (below)
    native_code_policy   <- NATIVE_SOURCE_EXT / NATIVE_SOURCE_ALLOWED_PREFIXES
    conformance_data     <- prose, and the directory names it governs
    preserved_paths      <- PRESERVED_FILES / PRESERVED_DIRS
    behavioral_contract  <- spec.as_dict()

The path lists are declared here and shipped from here, so there is one copy of
each.  freeze.py audits both directions at image-build time: every path named here
exists in State A, so no requirement is vacuous, and the shipped JSON matches what
render() produces, so the document is never stale.
"""

from __future__ import annotations

import hashlib
import json

import spec

SCHEMA_VERSION = "swerefactor/source-contract/1.0"

# --------------------------------------------------------------------------- #
# The migration, stated as paths
# --------------------------------------------------------------------------- #
# Every entry exists in State A and must be absent from State B.  freeze.py
# asserts the first half against its own copy of State A: a forbidden path that
# upstream never shipped is a requirement no submission can fail, and a list of
# those looks exactly like a strict gate while checking nothing.

FORBIDDEN_DIRS = (
    "core",            # the evaluator, formatter, lexer, parser, desugarer
    "cmd",             # the two CLI drivers
    "cpp",             # the C++ binding (libjsonnet++)
    "include",         # the C ABI headers
    "python",          # the CPython extension
    "third_party",     # vendored json.hpp, md5 and rapidyaml, all compiled in
    "vs2017",          # the MSVC project files
)

FORBIDDEN_FILES = (
    "Makefile",
    "CMakeLists.txt",
    "CMakeLists.txt.in",
    "setup.py",
    "MANIFEST.in",
    "stdlib/to_c_array.cpp",   # embeds std.jsonnet as a C array
    "Dockerfile",              # builds the C++ image
    ".travis.yml",
    "tests.sh",                # runs the C++ test suite against the built binary
)

BAZEL_BUILD_NAMES = ("BUILD", "BUILD.bazel", "WORKSPACE", "WORKSPACE.bazel")
BAZEL_BUILD_EXT = (".bzl",)

# Extensions that indicate C or C++ source.
NATIVE_SOURCE_EXT = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
                     ".inc", ".ipp")

# Paths where a file with one of those extensions is *not* implementation source
# and may legitimately survive.  Narrow on purpose: each entry is a specific
# upstream artifact, not a category being waved through.
NATIVE_SOURCE_ALLOWED_PREFIXES = (
    # Golden *output* files whose names happen to end in .cpp: they hold the
    # expected stdout/stderr of the C++ binary and are read as text by the
    # upstream command tests.  test_cmd/ is graded input, so it stays.
    "test_cmd/",
    # An unrelated demonstration program: a Mandelbrot tile generator used by the
    # fractal case study.  Not part of jsonnet, not in its build graph.
    "case_studies/",
    # Documentation assets.
    "doc/",
)

# --------------------------------------------------------------------------- #
# What the port keeps
# --------------------------------------------------------------------------- #
PRESERVED_FILES = {
    "LICENSE": "the Apache-2.0 license",
    "README.md": "the project readme",
    "CONTRIBUTING": "contributor guide (upstream ships it without an extension)",
    "release_checklist.md": "the release procedure",
    "stdlib/std.jsonnet": "the Jsonnet standard library, embedded and evaluated",
}

PRESERVED_DIRS = {
    "doc": "the website and language reference",
    "examples": "the documented examples",
    "test_suite": "the upstream conformance suite",
}

#: Directories holding upstream's recorded expected output.  Named in the contract
#: so the requirement not to mine them is a stated requirement.
CONFORMANCE_DIRS = ("test_suite", "test_cmd", "examples")

# --------------------------------------------------------------------------- #
# Where each stage-1 question is stated to the submission
# --------------------------------------------------------------------------- #
# Keys are the gate ids in tests/evaluation.toml.  Values name the sections of
# this document where a submission is told about the requirement.
#
# This is the one place the two files are tied together, and the failure it
# prevents is a gate demanding something neither the contract nor the
# instruction mentions -- a csproj naming convention, say -- which costs a
# submission everything for a rule it was never given.  freeze.py checks the
# mapping in both directions against evaluation.toml: a gate with no section
# here, and a section named for a gate that is not declared.
GATE_SECTIONS = {
    "no-cpp-sources": ("forbidden_paths", "native_code_policy"),
    "no-cpp-build-system": ("forbidden_paths", "build_contract"),
    "no-prebuilt-binaries": ("native_code_policy",),
    "no-subprocess-oracle": ("native_code_policy",),
    "no-native-interop": ("native_code_policy",),
    "no-answer-lookup": ("conformance_data",),
    "stdlib-preserved": ("preserved_paths",),
    "stdlib-is-interpreted": ("preserved_paths",),
    "is-an-implementation": ("migration", "product"),
}

# Every section this document states is claimed by at least one *required* gate,
# which is what makes the mapping meaningful: `preserved_paths` by
# `stdlib-preserved` and `stdlib-is-interpreted`, `conformance_data` by
# `no-answer-lookup`.  An advisory gate would be read by nothing --
# `grade_audit` builds the verdict from the conjunction of the required checks
# -- so a section whose only gate was advisory would be a requirement a submission
# is told about and nothing can act on.

def build() -> dict:
    """The contract, as a document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "task": "lang06-jsonnet-cpp-to-csharp",
        "category": "language-rewrite",

        "migration": {
            "from": {
                "language": "C++ (C++11)",
                "upstream": spec.UPSTREAM,
                "version": spec.UPSTREAM_VERSION,
                "build_system": "GNU Make (with Bazel and CMake alternates)",
            },
            "to": {
                "language": "C#",
                "runtime": ".NET 8",
                "target_framework": "net8.0",
                "build_system": "MSBuild (dotnet build / dotnet publish)",
            },
            "unit_of_work": "the whole repository",
            "note":
                "Behavior, both command-line interfaces and the embedded "
                "standard library are preserved.  The C ABI (include/), the "
                "C++ binding (cpp/) and the CPython extension (python/) are "
                "out of scope and must be removed.",
        },

        "submission_root": "/workspace/repo",

        "product": {
            "evaluator": {
                "name": "jsonnet",
                "role": "evaluate a Jsonnet program and print the result",
                "flags": sorted(spec.EVAL_FLAGS),
                "filenames": spec.ARGV_RULES["eval_filenames"],
            },
            "formatter": {
                "name": "jsonnetfmt",
                "role": "reformat Jsonnet source, preserving comments",
                "flags": sorted(spec.FORMAT_FLAGS),
                "filenames": spec.ARGV_RULES["format_filenames"],
            },
            "publish":
                "Both must be produced by `dotnet publish` of a project in "
                "the tree, as managed .NET 8 assemblies.  The verifier "
                "publishes from source with PublishSingleFile, PublishAot, "
                "PublishTrimmed, PublishReadyToRun and SelfContained all "
                "forced off, so the assemblies must remain inspectable.",
        },

        "forbidden_paths": {
            "directories": list(FORBIDDEN_DIRS),
            "files": list(FORBIDDEN_FILES),
            "bazel_build_names": list(BAZEL_BUILD_NAMES),
            "bazel_build_extensions": list(BAZEL_BUILD_EXT),
            "bazel_note":
                "Bazel build files are matched by basename at any depth, not "
                "by exact path.  Five sit outside the forbidden directories -- "
                "including stdlib/BUILD, in a directory that must otherwise be "
                "preserved -- and all of them describe a build system State B "
                "does not use.",
            "note":
                "Every path listed here exists in State A and must be absent "
                "from State B.  This is the migration, stated as paths.",
        },

        "native_code_policy": {
            "rule": "no C or C++ source may remain in the implementation",
            "extensions": list(NATIVE_SOURCE_EXT),
            "allowed_prefixes": list(NATIVE_SOURCE_ALLOWED_PREFIXES),
            "allowed_prefixes_note":
                "Files under these prefixes with a native extension are not "
                "implementation source: test_cmd/ holds golden *output* whose "
                "names end in .cpp, case_studies/ holds an unrelated "
                "Mandelbrot demo, doc/ holds documentation assets.",
            "no_prebuilt_binary":
                "No compiled artefact may be retained in the source tree: no "
                "ELF or PE file, no shared object, static archive or object "
                "file, and no assembly checked in rather than built.  An "
                "out-of-source build or publish directory is exempt.",
            "no_pinvoke":
                "No assembly may declare a P/Invoke or reference a native "
                "module.  Read from the CLR ImplMap and ModuleRef metadata "
                "tables of the published output, not by scanning IL text.",
            "no_subprocess":
                "No assembly may reference the process-creation APIs, and "
                "nothing in the implementation may hand an input to another "
                "program and return what came back.  A port that shells out "
                "to a retained jsonnet is not a port.",
            "managed_implementation":
                "The evaluating and the formatting are done by managed C# in "
                "this repository.  Spans, pointers and unsafe blocks are "
                "ordinary C#; a call into libjsonnet is not.",
        },

        "conformance_data": {
            "directories": list(CONFORMANCE_DIRS),
            "rule":
                "These directories are graded *input*, and they also hold "
                "upstream's recorded expected output -- the .golden files, and "
                "the .stdout/.stderr files under test_cmd/.  Preserve them; do "
                "not mine them.",
            "no_answer_lookup":
                "The implementation computes its output.  It may not consult "
                "recorded expected output at run time, carry the text of it "
                "-- plain, encoded or compressed -- in a file you add or "
                "change, hold a table of outputs keyed by input, filename or "
                "digest, or behave differently on inputs it recognises as "
                "being under test.  Your own test projects are free to read "
                "the conformance data, which is what it is for.",
            "no_harness_awareness":
                "Nothing in the submission names the grading harness or its "
                "filesystem.",
            "note":
                "Grading materialises every case from its own copy of State A, "
                "so editing these files changes nothing that is measured.",
        },

        "preserved_paths": {
            "files": dict(PRESERVED_FILES),
            "directories": dict(PRESERVED_DIRS),
            "byte_identical": {
                "stdlib/std.jsonnet": spec.STDLIB_SHA256,
            },
            "note":
                "stdlib/std.jsonnet is the Jsonnet standard library written in "
                "Jsonnet.  It is data, not C++, and porting it to C# is not "
                "the task: it must be embedded unmodified and evaluated by the "
                "ported evaluator, the way State A lexes and parses it at "
                "startup and binds its native fields over the result.  Its "
                "sha256 is checked.",
        },

        "build_contract": {
            "toolchain": "the .NET 8 SDK, offline",
            "restore":
                "Offline, and closed rather than merely unreachable.  No "
                "package source is configured: the NuGet source list is "
                "cleared, and NUGET_PACKAGES points at a pre-seeded packages "
                "folder that restore resolves from directly.  A package that "
                "is not already in that folder fails with NU1100 -- including "
                "at grading time, which passes the cleared configuration "
                "explicitly, so a nuget.config added to the submission cannot "
                "put a source back.  The agent container also has no network, "
                "but the rule does not depend on that.",
            "external_packages":
                "None.  The implementation must build against the .NET base "
                "class library alone, and nothing else is resolvable.  The "
                "pre-seeded folder carries test frameworks only, so a "
                "submission that wants to ship its own xunit tests can.",
            "invariant_globalization":
                "Required.  Number and string formatting must not depend on "
                "ICU or on the ambient locale.",
            "publish_command":
                "dotnet publish -c Release, with single-file, AOT, trimming, "
                "ReadyToRun and self-contained publishing all forced off.",
            # Stated because it is enforced.  The verifier's project discovery
            # locates a CLI by the assembly its project produces; a requirement
            # that decides whether a submission scores at all cannot live only
            # in the grader's code.
            "cli_projects":
                "Each command-line program must be produced by an executable "
                "project (OutputType Exe or WinExe) whose assembly name is "
                "exactly `jsonnet` or `jsonnetfmt` -- <AssemblyName> when set, "
                "otherwise the .csproj filename.  Project file names, directory "
                "layout and the number of library projects are unconstrained.",
            "build_closure":
                "The tree builds with the .NET SDK alone.  No Makefile, CMake, "
                "Bazel or MSBuild call-out to a native compiler, and nothing "
                "fetched at build time.",
        },

        "behavioral_contract": spec.as_dict(),
    }


def render() -> bytes:
    """The exact bytes of the shipped file.

    One renderer, used by the generator and by the audit.  When the contract was
    rendered in two places -- once to write it, once to compare -- the two
    disagreed on indent and the audit failed on a file that was in fact current.
    """
    doc = build()
    text = json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    return text.encode("utf-8")


def digest() -> str:
    return hashlib.sha256(render()).hexdigest()


def audit(path: str) -> list[str]:
    """Problems with the shipped contract at `path`.  Empty means current."""
    want = render()
    try:
        with open(path, "rb") as f:
            have = f.read()
    except OSError as exc:
        return [f"{path}: cannot read ({exc})"]

    if have == want:
        return []

    errs = [f"{path}: stale, sha256 "
            f"{hashlib.sha256(have).hexdigest()[:16]} != {digest()[:16]}"]
    # Say *what* drifted, not just that something did.  A digest mismatch with no
    # detail sends the reader to a 17 KB diff.
    try:
        got = json.loads(have.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        errs.append(f"  and it does not parse as JSON: {exc}")
        return errs

    exp = build()
    for key in sorted(set(exp) | set(got)):
        if key not in got:
            errs.append(f"  missing key: {key}")
        elif key not in exp:
            errs.append(f"  unexpected key: {key}")
        elif got[key] != exp[key]:
            errs.append(f"  changed: {key}")
    return errs


def _self_check() -> None:
    doc = build()
    assert doc["schema_version"] == SCHEMA_VERSION

    # No rubric.  This is the check that keeps the docstring true: a later edit
    # that reintroduces weights or a gate list fails here rather than shipping.
    banned = ("grading", "scoring", "weights", "mandatory_gates", "gates")
    present = [k for k in banned if k in doc]
    assert not present, (
        f"source-contract.json must not describe grading; found {present}. "
        f"The contract states requirements; evaluation.toml scores them.")
    flat = json.dumps(doc)
    for word in ("behavioural_weight", "structural_weight", "mandatory_gate"):
        assert word not in flat, f"contract leaks a scoring term: {word}"

    # Every stage-1 question has to correspond to something stated here.  A gate
    # with no section is the vacuous-requirement bug with the arrow reversed.
    for gate, sections in sorted(GATE_SECTIONS.items()):
        for section in sections:
            assert section in doc, (
                f"gate {gate} is stated in contract section {section!r}, "
                f"which this document does not have")

    # render() must round-trip: the bytes shipped have to parse back to the
    # document they were rendered from.
    assert json.loads(render().decode("utf-8")) == doc, \
        "render() does not round-trip build()"


def gate_ids() -> tuple[str, ...]:
    """The stage-1 gate ids this contract states requirements for.

    freeze.py cross-checks these against tests/evaluation.toml, which is the only
    thing that makes GATE_SECTIONS more than a comment.
    """
    return tuple(sorted(GATE_SECTIONS))


if __name__ == "__main__":
    import sys

    _self_check()
    if len(sys.argv) > 1 and sys.argv[1] == "--digest":
        print(digest())
    elif len(sys.argv) > 2 and sys.argv[1] == "--audit":
        errs = audit(sys.argv[2])
        for e in errs:
            print(f"contract: {e}")
        raise SystemExit(1 if errs else 0)
    else:
        sys.stdout.write(render().decode("utf-8"))
