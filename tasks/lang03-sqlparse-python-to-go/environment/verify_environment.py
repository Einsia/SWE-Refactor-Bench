#!/usr/bin/env python3
"""Build-time self-check for the lang03-sqlparse-python-to-go agent environment.

Runs inside the environment image build, and fails it on the first violation.
Four things must hold before the image is allowed to exist:

1. the workspace really is State A -- the Python implementation is present and
   intact, file for file, against the digests the contract records;
2. the workspace carries no repository-management data, so upstream history
   cannot be mined for "what came next";
3. the published contract is internally consistent, and the facts it states
   about State A are true of the tree that shipped;
4. the toolchain is the pinned one, and Go can build offline.

Check 3 is the one that earns its keep.  The contract is a generated file, and
the solver reads it as the specification of the target: every claim in it about
State A is checkable against the tree in the same image, so a contract that
drifted from the snapshot it describes fails the build rather than misleading a
solver for tens of hours.

The tree is not modified.  Everything here reads.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# State A landmarks: the Python modules the port has to replace, and the
# non-source files the release is made of.  If any is missing, the snapshot is
# not the calibrated tree and every frozen expectation is void.
STATE_A_MODULES = (
    "sqlparse/__init__.py",
    "sqlparse/__main__.py",
    "sqlparse/cli.py",
    "sqlparse/exceptions.py",
    "sqlparse/formatter.py",
    "sqlparse/keywords.py",
    "sqlparse/lexer.py",
    "sqlparse/sql.py",
    "sqlparse/tokens.py",
    "sqlparse/utils.py",
    "sqlparse/engine/__init__.py",
    "sqlparse/engine/filter_stack.py",
    "sqlparse/engine/grouping.py",
    "sqlparse/engine/statement_splitter.py",
    "sqlparse/filters/__init__.py",
    "sqlparse/filters/aligned_indent.py",
    "sqlparse/filters/others.py",
    "sqlparse/filters/output.py",
    "sqlparse/filters/reindent.py",
    "sqlparse/filters/right_margin.py",
    "sqlparse/filters/tokens.py",
)

STATE_A_RELEASE_FILES = (
    "AUTHORS",
    "CHANGELOG",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.rst",
    "SECURITY.md",
    "docs/sqlformat.1",
    "pyproject.toml",
    ".flake8",
)

VCS_NAMES = (
    ".git", ".github", ".gitlab", ".gitignore", ".gitattributes",
    ".gitmodules", ".hg", ".svn", ".bzr", "_darcs", "CVS", ".agit",
    ".idea", ".vscode", ".readthedocs.yaml", ".python-version", "PKG-INFO",
)

REQUIRED_TOOLS = ("go", "gofmt", "python3", "git", "tar")


class Failure(Exception):
    pass


def check_state_a(repo: Path, expect_files: int) -> None:
    actual = sorted(p for p in repo.rglob("*") if p.is_file())
    if len(actual) != expect_files:
        raise Failure(f"expected {expect_files} files in State A, "
                      f"found {len(actual)}")
    for rel in STATE_A_MODULES + STATE_A_RELEASE_FILES:
        if not (repo / rel).is_file():
            raise Failure(f"State A landmark missing: {rel}")

    version = (repo / "sqlparse/__init__.py").read_text(encoding="utf-8")
    match = re.search(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]",
                      version, re.MULTILINE)
    if match is None:
        raise Failure("sqlparse/__init__.py declares no __version__")
    if match.group(1) != "0.5.3":
        raise Failure(f"State A is sqlparse {match.group(1)}, not 0.5.3")


def check_history_free(repo: Path) -> None:
    for path in repo.rglob("*"):
        if path.name in VCS_NAMES:
            raise Failure(f"repository-management data present: {path}")
        if path.is_symlink():
            raise Failure(f"symlink in workspace: {path}")
        if not (path.is_file() or path.is_dir()):
            raise Failure(f"irregular file in workspace: {path}")
        if path.suffix in (".pyc", ".pyo", ".pyd"):
            raise Failure(f"build residue in workspace: {path}")


def check_contract(contract_path: Path, repo: Path) -> None:
    """The contract's claims about State A, checked against State A.

    Nothing here is a spot check.  The per-file digest table is the whole tree,
    and the four table-shaped claims (keyword tables, token types, format
    options, public API) are each recomputed from the source rather than
    compared against a second copy of the same number.
    """
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != "swerefactor-source-contract-v1":
        raise Failure("unexpected contract schema_version")
    if contract.get("task") != "lang03-sqlparse-python-to-go":
        raise Failure(f"contract is for task {contract.get('task')!r}")

    state_a = contract["state_a"]
    if state_a["upstream_version"] != "0.5.3":
        raise Failure("contract does not describe sqlparse 0.5.3")

    # 1. Every file, by digest.  This is the claim the solver's diff surface and
    #    the verifier's "unchanged vs rewritten" decision both rest on.
    declared = state_a["files"]
    if len(declared) != state_a["file_count"]:
        raise Failure(f"contract file_count {state_a['file_count']} disagrees "
                      f"with its own table of {len(declared)} entries")
    present = {p.relative_to(repo).as_posix()
               for p in repo.rglob("*") if p.is_file()}
    if present != set(declared):
        missing = sorted(set(declared) - present)
        extra = sorted(present - set(declared))
        raise Failure(f"contract file table disagrees with the tree: "
                      f"missing={missing[:5]} unexpected={extra[:5]}")
    for rel, meta in sorted(declared.items()):
        data = (repo / rel).read_bytes()
        if len(data) != meta["bytes"]:
            raise Failure(f"{rel}: contract says {meta['bytes']} bytes, "
                          f"tree has {len(data)}")
        actual = hashlib.sha256(data).hexdigest()
        if actual != meta["sha256"]:
            raise Failure(f"{rel}: digest {actual} != contract {meta['sha256']}")

    # 2. The keyword tables, counted out of keywords.py.  The port has to carry
    #    809 entries across nine tables; a contract that had the split wrong
    #    would send a solver looking for keywords that are not there.
    check_keyword_tables(repo, state_a)

    # 3. The token-type lattice and the format options, from their own modules.
    check_token_types(repo, state_a)
    check_format_options(repo, state_a)

    # 4. The public Python API the port's root package has to correspond to.
    #    Not `__all__`: in this package that names the six submodules, not the
    #    four entry points.  The claim being checked is that each function the
    #    contract publishes a signature for is a module-level def in
    #    sqlparse/__init__.py with the parameters the signature states -- so a
    #    signature that drifted from the code names a parameter the solver would
    #    port and the reference would not have.
    init = ast.parse((repo / "sqlparse/__init__.py").read_text(encoding="utf-8"),
                     filename="__init__.py")
    defined = {node.name: node for node in init.body
               if isinstance(node, ast.FunctionDef)}
    for name, signature in sorted(state_a["public_api_python"].items()):
        if name == "__version__":
            if signature != "0.5.3":
                raise Failure(f"contract's public __version__ is {signature!r}")
            continue
        if name not in defined:
            raise Failure(f"contract publishes {name}(), which sqlparse/"
                          f"__init__.py does not define")
        params = [a.arg for a in defined[name].args.args]
        stated = re.findall(r"[(,]\s*(\w+)", signature.split("->")[0])
        if params != stated:
            raise Failure(f"{name}: contract signature says {stated}, "
                          f"def says {params}")

    # 5. The paths the two policies name have to exist to be removable or
    #    preserved.  A policy naming a path that is not in State A is vacuous,
    #    and a vacuous rule is worse than no rule: it reads as enforced.
    for rel in contract["python_policy"]["must_delete"]:
        if not (repo / rel.rstrip("/")).exists():
            raise Failure(f"must_delete names {rel}, which is not in State A")
    for rel in contract["python_policy"]["removable_paths"]:
        if not (repo / rel.rstrip("/")).exists():
            raise Failure(f"removable_paths names {rel}, not in State A")
    for rel in contract["preserved_paths"]:
        if not (repo / rel).exists():
            raise Failure(f"preserved_paths names {rel}, not in State A")

    # 6. The two halves of the build contract that a solver can violate by
    #    accident: the module path the whole public surface is written against,
    #    and the `go` directive window.  Checked for agreement, not for value --
    #    structure.py reads go_contract.module_path and build.py reads
    #    build_contract.module_path, and they have to be one path.
    if contract["go_contract"]["module_path"] != \
            contract["build_contract"]["module_path"]:
        raise Failure("go_contract and build_contract disagree on module_path")
    lo = contract["build_contract"]["go_directive_min"]
    hi = contract["build_contract"]["go_directive_max"]
    if _version_key(lo) > _version_key(hi):
        raise Failure(f"go directive window is empty: [{lo}, {hi}]")

    # 7. Every package in the closed-world surface must name a Python origin
    #    that exists, which is what makes the surface traceable to the code it
    #    replaces rather than to a design document.
    for name, pkg in contract["go_contract"]["packages"].items():
        for rel in pkg["python_origin"]:
            if not (repo / rel).is_file():
                raise Failure(f"package {name} claims origin {rel}, "
                              f"which is not in State A")


def _assigned(path: Path, name: str) -> str:
    """The source of the module-level assignment to `name`.

    Parsed rather than regexed: the tables in keywords.py span hundreds of lines
    and contain every kind of quoting, and a regex that got one of them wrong
    would fail the build for the wrong reason.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.unparse(node.value)
    raise Failure(f"{path.name} has no module-level assignment to {name}")


def _version_key(text: str) -> tuple:
    return tuple(int(part) for part in text.split("."))


def check_keyword_tables(repo: Path, state_a: dict) -> None:
    source = (repo / "sqlparse/keywords.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="keywords.py")
    sizes = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.startswith("KEYWORDS"):
            continue
        if not isinstance(node.value, ast.Dict):
            raise Failure(f"{target.id} is not a dict literal")
        sizes[target.id] = len(node.value.keys)

    declared = state_a["keyword_tables"]
    if sizes != declared:
        raise Failure(f"keyword tables in State A are {sizes}, "
                      f"contract says {declared}")
    total = sum(sizes.values())
    if total != state_a["keyword_entries_total"]:
        raise Failure(f"keyword entries total {total}, contract says "
                      f"{state_a['keyword_entries_total']}")


def check_token_types(repo: Path, state_a: dict) -> None:
    """The token-type names, read out of tokens.py.

    The contract's list is every module-level name in `sqlparse.tokens` that
    holds a `_TokenType`, which is what `dir(tokens)` plus an isinstance filter
    reports.  Reproducing that statically means propagating: the lattice is not
    flat, and only six of the 22 names are assigned directly off `Token`.
    `Whitespace = Text.Whitespace` and `String = Literal.String` are token types
    because `Text` and `Literal` already are, and `Token` itself is one because
    `_TokenType()` is called.  A scan that only accepted `Token.<Name>` reports
    12 of 22 -- measured, before this comment existed.

    One forward pass over the module body is enough because Python executes it
    that way: a name is a token type only if its base was already bound to one.
    """
    tree = ast.parse((repo / "sqlparse/tokens.py").read_text(encoding="utf-8"),
                     filename="tokens.py")
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target, value = node.targets[0], node.value
        # `Token.String = String` and friends re-bind an attribute on an
        # existing type; they introduce no new module-level name.
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) \
                and value.func.id == "_TokenType":
            names.add(target.id)
        elif isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) \
                and value.value.id in names:
            names.add(target.id)
        elif isinstance(value, ast.Name) and value.id in names:
            names.add(target.id)

    declared = set(state_a["token_type_names"])
    if names != declared:
        raise Failure(f"token types in State A are {sorted(names)}, "
                      f"contract lists {sorted(declared)}")
    if len(declared) != state_a["token_type_count"]:
        raise Failure(f"{len(declared)} token types, contract says "
                      f"{state_a['token_type_count']}")


def check_format_options(repo: Path, state_a: dict) -> None:
    """The option keys validate_options reads, out of formatter.py.

    The reference's option surface is the set of keys its validator pops from
    the kwargs dict.  Every one of them is a flag the port's ValidateOptions has
    to accept, so the contract publishes the list -- and this recomputes it,
    because a list typed from a docstring is a list that can be short by one.
    """
    source = (repo / "sqlparse/formatter.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="formatter.py")
    keys = set()
    for node in ast.walk(tree):
        # options.get('x'), options.pop('x'), options['x'] = ...
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("get", "pop") \
                and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == "options" and node.args \
                and isinstance(node.args[0], ast.Constant) \
                and isinstance(node.args[0].value, str):
            keys.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == "options" \
                and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            keys.add(node.slice.value)

    declared = set(state_a["format_option_keys"])
    if not declared <= keys:
        raise Failure(f"contract lists format options absent from "
                      f"formatter.py: {sorted(declared - keys)}")
    if len(declared) != len(state_a["format_option_keys"]):
        raise Failure("format_option_keys contains duplicates")


def check_toolchain() -> None:
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            raise Failure(f"required tool missing from image: {tool}")
    out = subprocess.run(["go", "version"], capture_output=True, text=True,
                         check=False)
    if out.returncode != 0:
        raise Failure("go is not behavioural in the image")
    if "go1.25.12" not in out.stdout:
        raise Failure(f"go is not pinned to 1.25.12: {out.stdout.strip()}")
    # GOTOOLCHAIN=local is what stops a `toolchain` line in a submission's
    # go.mod from reaching for a Go this offline image does not have.  The
    # verifier passes it explicitly; the environment must agree, or a solver
    # calibrates against a toolchain that will not be there at grading time.
    out = subprocess.run(["go", "env", "GOTOOLCHAIN"], capture_output=True,
                         text=True, check=False)
    if out.stdout.strip() != "local":
        raise Failure(f"GOTOOLCHAIN is {out.stdout.strip()!r}, not 'local'")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--contract", required=True, type=Path)
    ap.add_argument("--expect-files", required=True, type=int)
    args = ap.parse_args(argv)

    # history-free runs first, and the order is load-bearing.  Every way of
    # leaving VCS data in the workspace also adds a file, so with the count check
    # ahead of it, `.git/HEAD`, a stray `.pyc` and a symlink were all reported as
    # "expected 73 files, found 74" -- measured, on all three.  That diagnosis
    # names the symptom and buries the one failure here that is a benchmark
    # audit problem rather than snapshot drift: if history reaches the
    # workspace, the task leaks its own answer, and that has to be what the build
    # log says.  The count check keeps its place as the catch-all behind it.
    checks = (
        ("history-free", lambda: check_history_free(args.repo)),
        ("state-a-intact", lambda: check_state_a(args.repo, args.expect_files)),
        ("contract-consistent", lambda: check_contract(args.contract, args.repo)),
        ("toolchain-pinned", check_toolchain),
    )
    for name, fn in checks:
        try:
            fn()
        except Failure as exc:
            print(f"FAIL {name}: {exc}", file=sys.stderr)
            return 1
        print(f"ok   {name}")
    print("environment self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
