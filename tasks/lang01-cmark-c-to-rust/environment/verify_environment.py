#!/usr/bin/env python3
"""Build-time self-check for the lang01-cmark-c-to-rust agent environment.

Runs inside the environment image build.  Three things must hold before the
image is allowed to exist:

1. the workspace really is State A - the C implementation is present and
   intact, so the solver is starting from the migration's beginning;
2. the workspace carries no repository-management data, so upstream history
   cannot be mined for the answer;
3. the published migration contract is internally consistent, so the solver
   and the verifier are reading the same rules.

Exit status is non-zero on the first violation, which fails the image build.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

# State A landmarks. If any of these is missing the snapshot is not the
# calibrated upstream tree and every downstream expectation is void.
STATE_A_SOURCES = (
    "src/blocks.c",
    "src/buffer.c",
    "src/cmark.c",
    "src/cmark_ctype.c",
    "src/commonmark.c",
    "src/houdini_href_e.c",
    "src/houdini_html_e.c",
    "src/houdini_html_u.c",
    "src/html.c",
    "src/inlines.c",
    "src/iterator.c",
    "src/latex.c",
    "src/main.c",
    "src/man.c",
    "src/node.c",
    "src/references.c",
    "src/render.c",
    "src/scanners.c",
    "src/utf8.c",
    "src/xml.c",
    "src/case_fold.inc",
    "src/entities.inc",
    "src/scanners.re",
)

STATE_A_CONTRACT_FILES = (
    "src/cmark.h",
    "src/libcmark.pc.in",
    "src/cmarkConfig.cmake.in",
    "src/cmark_version.h.in",
    "man/man1/cmark.1",
    "man/man3/cmark.3",
    "CMakeLists.txt",
    "src/CMakeLists.txt",
    "test/spec.txt",
    "test/smart_punct.txt",
    "test/regression.txt",
)

VCS_NAMES = (
    ".git",
    ".github",
    ".gitlab",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".hg",
    ".svn",
    ".bzr",
    "_darcs",
    "CVS",
    ".agit",
)

REQUIRED_TOOLS = ("cmake", "cargo", "rustc", "gcc", "python3", "git", "make")


class Failure(Exception):
    pass


def check_state_a(repo: Path, expect_files: int) -> None:
    actual = sum(1 for p in repo.rglob("*") if p.is_file())
    if actual != expect_files:
        raise Failure(f"expected {expect_files} files in State A, found {actual}")
    for rel in STATE_A_SOURCES + STATE_A_CONTRACT_FILES:
        if not (repo / rel).is_file():
            raise Failure(f"State A landmark missing: {rel}")
    exported = (repo / "src/cmark.h").read_text(encoding="utf-8").count("CMARK_EXPORT")
    if exported < 60:
        raise Failure(f"src/cmark.h declares only {exported} CMARK_EXPORT entries")


def check_history_free(repo: Path) -> None:
    for path in repo.rglob("*"):
        if path.name in VCS_NAMES:
            raise Failure(f"repository-management data present: {path}")
        if path.is_symlink():
            raise Failure(f"symlink in workspace: {path}")
        if not (path.is_file() or path.is_dir()):
            raise Failure(f"irregular file in workspace: {path}")
        if path.suffix == ".pyc":
            raise Failure(f"build residue in workspace: {path}")


def check_contract(contract_path: Path, repo: Path) -> None:
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract.get("schema_version") != "swerefactor-source-contract-v1":
        raise Failure("unexpected contract schema_version")
    if contract["product"]["upstream_version"] != "0.31.1":
        raise Failure("contract does not describe cmark 0.31.1")

    allowlist = set(contract["forbidden_paths"]["header_allowlist"])
    headers = {
        p.relative_to(repo).as_posix()
        for p in repo.rglob("*.h")
    }
    if not allowlist <= headers:
        raise Failure(f"header allowlist not present in State A: {allowlist - headers}")
    if len(headers) <= len(allowlist):
        raise Failure("State A should contain private C headers beyond the allowlist")

    for rel in contract["preserved_paths"]["paths"]:
        if not (repo / rel).exists():
            raise Failure(f"preserved path absent from State A: {rel}")

    forbidden_ext = set(contract["forbidden_paths"]["extensions"])
    for rel in ("src/blocks.c", "src/entities.inc", "src/scanners.re"):
        suffix = Path(rel).suffix
        if suffix not in forbidden_ext:
            raise Failure(f"contract does not forbid {suffix}")

    if contract["abi_contract"]["exported_symbol_count"] != 70:
        raise Failure("contract exported_symbol_count is not 70")
    if contract["abi_contract"]["soname"] != "libcmark.so.0.31.1":
        raise Failure("contract soname mismatch")

    ids = [c["id"] for c in contract["build_contract"]["configurations"]]
    if ids != ["shared", "static"]:
        raise Failure(f"unexpected build configurations: {ids}")


def check_toolchain() -> None:
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            raise Failure(f"required tool missing from image: {tool}")
    for tool, expected in (("rustc", "1.90.0"), ("cargo", "1.90.0")):
        out = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, check=False
        )
        if out.returncode != 0:
            raise Failure(f"{tool} is not behavioural in the image")
        if expected not in out.stdout:
            raise Failure(f"{tool} is not pinned to {expected}: {out.stdout.strip()}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--contract", required=True, type=Path)
    ap.add_argument("--expect-files", required=True, type=int)
    args = ap.parse_args(argv)

    checks = (
        ("state-a-intact", lambda: check_state_a(args.repo, args.expect_files)),
        ("history-free", lambda: check_history_free(args.repo)),
        ("contract-consistent", lambda: check_contract(args.contract, args.repo)),
        ("toolchain-present", check_toolchain),
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
