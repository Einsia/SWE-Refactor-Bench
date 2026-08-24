#!/usr/bin/env python3
"""Build-time self-check for the lang06-jsonnet-cpp-to-csharp agent environment.

Runs inside the environment image build.  Four things must hold before the image
is allowed to exist:

1. the workspace really is State A -- the C++ implementation is present and
   intact, so the solver starts at the migration's beginning;
2. the workspace carries no repository-management data, so upstream history
   cannot be mined for the answer;
3. the published migration contract is internally consistent, and every path it
   forbids or preserves is a path this tree actually has -- a contract that
   forbids a directory State A never shipped is a requirement met for free, and
   one that preserves a file State A lacks is a requirement nobody can meet;
4. nothing in the tree already looks like the answer.

Exit status is non-zero on the first violation, which fails the image build.
That is the point: a broken environment must not reach a solver, because every
hour it wastes is an hour lost to a defect of mine rather than to the task.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# State A landmarks.  If any is missing, the snapshot is not the calibrated
# upstream tree and every downstream expectation is void.  These are the C++
# translation units that make up the evaluator, the formatter and the two CLIs.
STATE_A_SOURCES = (
    "core/desugarer.cpp",
    "core/formatter.cpp",
    "core/lexer.cpp",
    "core/libjsonnet.cpp",
    "core/parser.cpp",
    "core/pass.cpp",
    "core/static_analysis.cpp",
    "core/string_utils.cpp",
    "core/vm.cpp",
    "cmd/jsonnet.cpp",
    "cmd/jsonnetfmt.cpp",
    "core/ast.h",
    "core/lexer.h",
    "core/parser.h",
    "core/state.h",
    "core/vm.h",
)

# The build systems State A ships.  All must be present now and absent in State B.
#
# Note the bazel paths: upstream has no BUILD or BUILD.bazel at the repo root, it
# puts them in subdirectories.  Listing a root BUILD here -- which an earlier
# draft did -- asserts something that was never true, and the same mistake in
# audit.FORBIDDEN_FILES made two gate entries dead while stdlib/BUILD went
# unnoticed.  These are the paths that actually exist.
STATE_A_BUILD = (
    "Makefile",
    "CMakeLists.txt",
    "CMakeLists.txt.in",
    "WORKSPACE",
    "core/BUILD",
    "cmd/BUILD",
    "stdlib/BUILD",
    "platform_defs/BUILD",
    "tools/build_defs/python_repo.bzl",
    "setup.py",
    "MANIFEST.in",
    "tests.sh",
)

# Data and documentation that must survive the migration untouched.
STATE_A_PRESERVED = (
    "stdlib/std.jsonnet",
    "README.md",
    "LICENSE",
    "CONTRIBUTING",
    "release_checklist.md",
)

STATE_A_PRESERVED_DIRS = ("test_suite", "examples", "doc", "test_cmd")

# The Jsonnet standard library, byte-identical.  Gated by the verifier; asserted
# here so a corrupted archive fails the environment build rather than every
# submission's stdlib-preserved gate.
STDLIB_SHA256 = \
    "006e2051f3db3bc8311dbd98b421b01740cfc078f7432bfda715669e664fff5f"

# Repository-management data.  `.git*` covers .gitignore, .gitattributes and
# .gitmodules as well as .git itself.
VCS_GLOBS = (".git*", ".hg", ".svn", ".bzr", "_darcs", "CVS")

# If any of these exists, the tree already contains part of the answer.  A
# solution left in the image by an authoring mistake would be found immediately.
ANSWER_SHAPED = ("*.csproj", "*.sln", "*.cs", "Directory.Build.props",
                 "global.json", "nuget.config", "NuGet.config")

# Directories the migration deletes.  A match under one of these is C++ build
# data, not a half-finished port: upstream ships vs2017/Jsonnet.sln, an MSVC
# solution for the C++ build, and *.sln is otherwise a good signal that a .NET
# solution was left behind.  Scoping the search to where State B will live keeps
# the check meaningful instead of permanently failing on an upstream file.
ANSWER_EXEMPT_DIRS = ("vs2017", "third_party", "java_comparison")


def fail(msg: str) -> None:
    print(f"environment check FAILED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def check_state_a(repo: Path) -> None:
    for rel in STATE_A_SOURCES:
        p = repo / rel
        if not p.is_file():
            fail(f"State A source missing: {rel}")
        if p.stat().st_size == 0:
            fail(f"State A source is empty: {rel}")
    print(f"  state A: {len(STATE_A_SOURCES)} C++ landmarks present")

    for rel in STATE_A_BUILD:
        if not (repo / rel).is_file():
            fail(f"State A build file missing: {rel}")
    print(f"  state A: {len(STATE_A_BUILD)} build systems present")

    for rel in STATE_A_PRESERVED:
        if not (repo / rel).is_file():
            fail(f"file the migration must preserve is missing: {rel}")
    for rel in STATE_A_PRESERVED_DIRS:
        if not (repo / rel).is_dir():
            fail(f"directory the migration must preserve is missing: {rel}")
    print(f"  state A: {len(STATE_A_PRESERVED)} preserved files, "
          f"{len(STATE_A_PRESERVED_DIRS)} preserved directories")

    blob = (repo / "stdlib" / "std.jsonnet").read_bytes()
    got = hashlib.sha256(blob).hexdigest()
    if got != STDLIB_SHA256:
        fail(f"stdlib/std.jsonnet sha256 is {got}, expected {STDLIB_SHA256}")
    print(f"  state A: stdlib/std.jsonnet {len(blob)} bytes, sha256 matches")


def check_no_vcs(repo: Path) -> None:
    for pattern in VCS_GLOBS:
        hits = sorted(str(p.relative_to(repo)) for p in repo.rglob(pattern))
        if hits:
            fail(f"repository-management data in the workspace: {hits[:10]}")
    # Nothing but regular files and directories: a symlink or device node in the
    # delivered tree is either an archive defect or an escape.
    odd = [str(p.relative_to(repo)) for p in repo.rglob("*")
           if not p.is_dir() and not p.is_file()]
    odd += [str(p.relative_to(repo)) for p in repo.rglob("*") if p.is_symlink()]
    if odd:
        fail(f"non-regular entries in the workspace: {sorted(set(odd))[:10]}")
    print("  no VCS metadata, no symlinks, no special files")


def check_no_answer(repo: Path) -> None:
    exempt = 0
    for pattern in ANSWER_SHAPED:
        hits = []
        for p in repo.rglob(pattern):
            rel = p.relative_to(repo)
            if rel.parts and rel.parts[0] in ANSWER_EXEMPT_DIRS:
                exempt += 1
                continue
            hits.append(str(rel))
        if hits:
            fail(f"the workspace already contains .NET project data "
                 f"({pattern}): {sorted(hits)[:10]}")
    print(f"  no .NET project data outside the deleted directories "
          f"({exempt} upstream MSVC/vendored file(s) exempt): "
          f"State A is not partly ported")


def check_contract(repo: Path, contract_path: Path) -> None:
    try:
        doc = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read the contract at {contract_path}: {exc}")

    for key in ("schema_version", "task", "migration", "submission_root",
                "product", "forbidden_paths", "native_code_policy",
                "preserved_paths", "build_contract", "behavioral_contract"):
        if key not in doc:
            fail(f"contract is missing the {key!r} section")

    if doc["submission_root"] != "/workspace/repo":
        fail(f"contract submission_root is {doc['submission_root']!r}, "
             f"expected /workspace/repo")

    # The contract states requirements; tests/evaluation.toml scores them, and the
    # agent never sees that file.  So this document must not carry a rubric: no
    # weights, no gate list, nothing that says how many points a section is worth.
    #
    # This is asserted here and not only in the generator because this is the copy
    # the agent is handed.  The generator's own check (tests/behavioural/lib/
    # contract.py:_self_check) runs where the agent cannot reach, which makes it a
    # check on the writer; this one is a check on what was delivered.  The two can
    # disagree -- a generator rule tightened after an image was built reaches the
    # image only on the next rebuild -- and when they do, the delivered copy is the
    # one the agent reads, so it is the one that has to be right.
    banned = [k for k in ("grading", "scoring", "weights", "mandatory_gates",
                          "gates") if k in doc]
    if banned:
        fail(f"contract describes grading, which belongs to the verifier and "
             f"not to the delivered environment: {banned}")
    # Identifiers, not English words: a bare "points" matches "NUGET_PACKAGES
    # points at a pre-seeded feed", which is prose about an environment variable
    # and not a rubric.  Each term below is one that only appears if a scoring
    # key really was copied in.
    flat = json.dumps(doc)
    for word in ("behavioural_weight", "structural_weight", "mandatory_gate",
                 "max_score", "points_per", "behavioural_points",
                 "verification_points", "verification_models"):
        if word in flat:
            fail(f"contract leaks a scoring term: {word}")

    # Every forbidden path must exist right now.  One that does not is a
    # requirement a submission satisfies without doing anything, which makes the
    # gate look like it passed when it never applied.
    missing = [d for d in doc["forbidden_paths"]["directories"]
               if not (repo / d).is_dir()]
    if missing:
        fail(f"contract forbids directories State A does not ship, so the "
             f"requirement is vacuous: {missing}")
    missing = [f for f in doc["forbidden_paths"]["files"]
               if not (repo / f).exists()]
    if missing:
        fail(f"contract forbids files State A does not ship, so the "
             f"requirement is vacuous: {missing}")

    # And every preserved path must exist, or it is a requirement nobody can meet.
    absent = [f for f in doc["preserved_paths"]["files"]
              if not (repo / f).is_file()]
    if absent:
        fail(f"contract requires preserving files State A does not ship: {absent}")
    absent = [d for d in doc["preserved_paths"]["directories"]
              if not (repo / d).is_dir()]
    if absent:
        fail(f"contract requires preserving directories State A does not "
             f"ship: {absent}")

    want = doc["preserved_paths"]["byte_identical"].get("stdlib/std.jsonnet")
    if want != STDLIB_SHA256:
        fail(f"contract's std.jsonnet digest {want} disagrees with this "
             f"tree's {STDLIB_SHA256}")

    bc = doc["behavioral_contract"]
    n_eval = len(bc["eval_flags"])
    n_fmt = len(bc["format_flags"])
    n_std = len(bc["stdlib"]["public"])
    if n_eval < 15 or n_fmt < 12 or n_std < 100:
        fail(f"behavioral contract looks truncated: {n_eval} eval flags, "
             f"{n_fmt} format flags, {n_std} stdlib members")

    print(f"  contract: no rubric, "
          f"{len(doc['forbidden_paths']['directories'])} forbidden dirs "
          f"(all present), {len(doc['preserved_paths']['files'])} preserved "
          f"files (all present)")
    # "members", not "functions": std.thisFile is a string field, and the contract
    # exports it under stdlib.non_function for exactly that reason.  A count
    # labelled "functions" here would contradict the document it is auditing.
    print(f"  contract: {n_eval} eval flags, {n_fmt} format flags, "
          f"{n_std} stdlib members")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--contract", required=True)
    ap.add_argument("--expect-files", type=int, required=True,
                    help="exact regular-file count of the delivered tree")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        fail(f"{repo} is not a directory")

    print(f"auditing the delivered environment at {repo}")
    check_state_a(repo)
    check_no_vcs(repo)
    check_no_answer(repo)
    check_contract(repo, Path(args.contract).resolve())

    n = sum(1 for p in repo.rglob("*") if p.is_file())
    if n != args.expect_files:
        fail(f"the tree holds {n} files, expected {args.expect_files}")
    print(f"  file count: {n}")

    print("environment check PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
