#!/usr/bin/env python3
"""Build-time self-check for the lang02-zlib-c-to-java agent environment.

Runs inside the environment image build.  Three things must hold before the
image is allowed to exist:

1. the workspace really is State A - the C implementation is present and
   intact, so the solver is starting from the migration's beginning;
2. the workspace carries no repository-management data, so upstream history
   cannot be mined for the answer;
3. the published migration contract is internally consistent with the tree it
   describes, so the solver and the verifier are reading the same rules.

Exit status is non-zero on the first violation, which fails the image build.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# State A landmarks: the fifteen library translation units plus the three
# upstream test drivers.  If any is missing the snapshot is not the calibrated
# tree and every downstream expectation is void.
STATE_A_SOURCES = (
    "adler32.c",
    "compress.c",
    "crc32.c",
    "deflate.c",
    "gzclose.c",
    "gzlib.c",
    "gzread.c",
    "gzwrite.c",
    "infback.c",
    "inffast.c",
    "inflate.c",
    "inftrees.c",
    "trees.c",
    "uncompr.c",
    "zutil.c",
    "test/example.c",
    "test/infcover.c",
    "test/minigzip.c",
)

# The private C headers.  Their presence is what makes State A a C tree; the
# migration is not done while any of them still exists.
STATE_A_PRIVATE_HEADERS = (
    "crc32.h",
    "deflate.h",
    "gzguts.h",
    "inffast.h",
    "inffixed.h",
    "inflate.h",
    "inftrees.h",
    "trees.h",
    "zutil.h",
)

STATE_A_CONTRACT_FILES = (
    "zlib.h",
    "zconf.h",
    "zconf.h.cmakein",
    "zlib.map",
    "zlib.pc.cmakein",
    "zlib.3",
    "LICENSE",
    "CMakeLists.txt",
    "win32/zlib1.rc",
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

# gcc and make are here because State A is C and has to be buildable in the
# delivered environment: an agent has to be able to run the thing it is
# replacing.  javac/java/jar are the migration target.  There is deliberately no
# second JDK -- see check_toolchain.
REQUIRED_TOOLS = ("cmake", "javac", "java", "jar", "gcc", "python3", "git", "make")

JDK_VERSION = "17.0.16"


class Failure(Exception):
    pass


def check_state_a(repo: Path, expect_files: int) -> None:
    actual = sum(1 for p in repo.rglob("*") if p.is_file())
    if actual != expect_files:
        raise Failure(f"expected {expect_files} files in State A, found {actual}")
    for rel in STATE_A_SOURCES + STATE_A_PRIVATE_HEADERS + STATE_A_CONTRACT_FILES:
        if not (repo / rel).is_file():
            raise Failure(f"State A landmark missing: {rel}")

    header = (repo / "zlib.h").read_text(encoding="utf-8")
    if 'ZLIB_VERSION "1.3.1"' not in header:
        raise Failure("zlib.h does not declare ZLIB_VERSION 1.3.1")
    declared = header.count("ZEXTERN")
    if declared < 100:
        raise Failure(f"zlib.h declares only {declared} ZEXTERN entries")

    # The version script is what State A's ABI surface was read from, and the
    # contract's symbol map is keyed on it.  A snapshot whose map file lost its
    # version nodes would silently relax the hardest part of the contract -- even
    # though the Java delivery replaces the map with a module descriptor, the
    # 88-symbol list the API contract maps *from* still comes from here.
    nodes = re.findall(r"^(ZLIB_[0-9.]+)\s*\{", (repo / "zlib.map").read_text(), re.M)
    if len(nodes) != 14:
        raise Failure(f"zlib.map declares {len(nodes)} version nodes, expected 14")

    # No Java in State A.  This is the assertion that catches a workspace built
    # from the wrong side of the migration: if a .java or a .jar were already
    # here, the task would be partly done before the agent started.
    for pattern in ("*.java", "*.class", "*.jar", "module-info.java"):
        found = sorted(p.relative_to(repo).as_posix() for p in repo.rglob(pattern))
        if found:
            raise Failure(f"State A already contains Java material: {found[:5]}")


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
    product = contract["product"]
    if product["upstream_version"] != "1.3.1":
        raise Failure("contract does not describe zlib 1.3.1")
    if (product["state_a_language"], product["state_b_language"]) != ("C", "Java"):
        raise Failure("contract does not describe a C-to-Java migration")

    # The allowlist must name files that exist, and State A must contain
    # private headers beyond it - otherwise "remove every header except the
    # allowlist" would be a no-op instruction.
    allowlist = set(contract["forbidden_paths"]["header_allowlist"])
    headers = {p.relative_to(repo).as_posix() for p in repo.rglob("*.h")}
    present = {h for h in allowlist if not h.endswith((".in", ".cmakein"))}
    if not present <= headers:
        raise Failure(f"header allowlist not present in State A: {present - headers}")
    if len(headers) <= len(present):
        raise Failure("State A should contain private C headers beyond the allowlist")

    for rel in contract["preserved_paths"]["paths"]:
        if not (repo / rel).exists():
            raise Failure(f"preserved path absent from State A: {rel}")

    # Headers are governed by the allowlist, not by the extension blocklist, so
    # only the compiled-source extensions are checked here.  Asserting that .h
    # is blocked would contradict the allowlist that has to keep zlib.h.
    forbidden_ext = set(contract["forbidden_paths"]["extensions"])
    for suffix in (".c", ".s", ".o", ".a"):
        if suffix not in forbidden_ext:
            raise Failure(f"contract does not forbid {suffix}")
    if ".h" in forbidden_ext:
        raise Failure("contract blocks .h outright, which would forbid zlib.h")
    # A compiled artifact in the *source tree* is a prebuilt binary, whichever
    # language it came from.  The jar the build produces lives under the build
    # directory and the install prefix, not here.
    for suffix in (".class", ".jar", ".jmod"):
        if suffix not in forbidden_ext:
            raise Failure(f"contract does not forbid {suffix} in the source tree")

    # Every path the contract says must be removed has to be there now.  A
    # contract that lists a file State A does not have is describing a
    # different tree.
    for rel in contract["forbidden_paths"]["c_sources_that_must_be_removed"]:
        if not (repo / rel).is_file():
            raise Failure(f"contract names a C source State A lacks: {rel}")
    for rel in contract["forbidden_paths"]["internal_headers_that_must_be_removed"]:
        if not (repo / rel).is_file():
            raise Failure(f"contract names a header State A lacks: {rel}")

    check_api_contract(contract, repo)
    check_jvm_contract(contract)

    ids = [c["id"] for c in contract["build_contract"]["configurations"]]
    if ids != ["shared", "static"]:
        raise Failure(f"unexpected build configurations: {ids}")
    if contract["build_contract"]["driver"] != "cmake":
        raise Failure("contract does not keep CMake as the build driver")
    if contract["build_contract"]["target_names"] != ["zlib", "zlibstatic"]:
        raise Failure("contract does not preserve both CMake target names")

    facts = contract["source_tree_facts"]
    if facts["file_count"] != sum(1 for p in repo.rglob("*") if p.is_file()):
        raise Failure("contract file_count disagrees with State A")
    if facts["c_source_count"] != len(list(repo.rglob("*.c"))):
        raise Failure("contract c_source_count disagrees with State A")
    if facts["internal_header_count"] != len(STATE_A_PRIVATE_HEADERS):
        raise Failure("contract internal_header_count disagrees with State A")


def check_api_contract(contract: dict, repo: Path) -> None:
    """The Java surface, and its correspondence to what State A exported.

    The C-to-C form of this check read a version script.  There is no version
    script in the delivery -- a module descriptor replaces it -- so what is
    checked instead is the mapping: every one of State A's 88 exported symbols
    has to land on a declared Java member, and every class named in that mapping
    has to be one the contract declares.  Without the second half the mapping
    could name a type nobody has to deliver.
    """
    api = contract["api_contract"]
    if api["package"] != "org.zlib":
        raise Failure(f"contract package is {api['package']}, expected org.zlib")
    if not api.get("closed_world"):
        raise Failure("contract does not declare the API a closed world")

    declared = {c["name"] for c in api["classes"]}
    if len(declared) != len(api["classes"]):
        raise Failure("contract declares a class twice")
    for name in declared:
        if not name.startswith("org.zlib."):
            raise Failure(f"declared class outside org.zlib: {name}")
    simple = {name.rsplit(".", 1)[1] for name in declared}

    symbol_map = api["symbol_map"]
    if api["state_a_symbol_count"] != 88:
        raise Failure("contract state_a_symbol_count is not 88")
    if len(symbol_map) != 88:
        raise Failure(
            f"symbol_map covers {len(symbol_map)} of State A's 88 exported symbols")

    # The mapping's targets have to be members of declared types.  A target of
    # "Whatever.foo(int)" would otherwise pass every other check while naming a
    # class no case ever looks for.  Targets are written unqualified, since the
    # package is stated once above.
    for symbol, target in symbol_map.items():
        head = str(target).split("(", 1)[0]
        owner = head.rsplit(".", 1)[0] if "." in head else ""
        if not owner:
            raise Failure(f"symbol_map entry for {symbol} names no class: {target}")
        if owner not in simple:
            raise Failure(
                f"symbol_map sends {symbol} to {owner}, which the contract "
                f"does not declare")

    # State A's own header has to actually export the symbols the map claims to
    # cover, or the map is describing a different library.
    header = (repo / "zlib.h").read_text(encoding="utf-8")
    missing = [s for s in ("deflateInit2_", "inflateBackInit_", "gzopen", "crc32_z")
               if s not in header]
    if missing:
        raise Failure(f"zlib.h does not declare {missing}")


def check_jvm_contract(contract: dict) -> None:
    """The rules that make this a port rather than a delegation.

    This is the half with no C-to-C analogue at all, and it is the half that
    matters most on this task: the JDK ships a bit-compatible deflate in
    java.util.zip, so "migrate zlib to Java" has a three-line wrong answer.  The
    contract has to forbid it in every form the platform offers -- the types, the
    strings a reflective call would use, the module that provides them -- and this
    check is what proves the contract still does.
    """
    cf = contract["classfile_contract"]
    if cf["major_version"] != 61 or cf["minor_version"] != 0:
        raise Failure("contract does not pin class file version 61.0")
    if not cf.get("forbidden_native_methods"):
        raise Failure("contract does not forbid native methods")

    prefixes = set(cf["forbidden_prefixes"])
    for required in ("java/util/zip/", "java/util/jar/", "jdk/internal/",
                     "java/lang/foreign/"):
        if required not in prefixes:
            raise Failure(f"contract does not forbid the prefix {required}")

    refs = set(cf["forbidden_type_references"])
    for required in ("java/util/zip/Deflater", "java/util/zip/Inflater",
                     "java/util/zip/CRC32"):
        if required not in refs:
            raise Failure(f"contract does not forbid the type {required}")
    # The string form as well as the type form.  A reflective Class.forName
    # leaves no type reference in the constant pool, only a string, so a contract
    # that forbade one and not the other would be trivially satisfiable.
    strings = set(cf["forbidden_string_constants"])
    for required in ("java.util.zip.Deflater", "java.util.zip.Inflater"):
        if required not in strings:
            raise Failure(f"contract does not forbid the string {required}")

    policy = contract["jvm_code_policy"]
    if policy["language"] != "Java":
        raise Failure("contract does not name Java as the target language")
    if policy["java_release"] != "17":
        raise Failure("contract does not pin --release 17")
    if policy["min_java_logic_lines"] < 3000:
        raise Failure(
            "contract's Java line floor is too low to describe a whole-library "
            "port")

    module = contract["module_contract"]
    if module["name"] != "org.zlib":
        raise Failure(f"contract module is {module['name']}, expected org.zlib")
    if module["exports"] != ["org.zlib"]:
        raise Failure("contract does not export exactly org.zlib")
    if module["requires"] != ["java.base"]:
        raise Failure("contract requires something other than java.base alone")
    if module.get("open"):
        raise Failure("contract permits an open module")
    if module["descriptor_path"] != "module-info.class":
        raise Failure("contract does not place module-info.class at the jar root")

    jar = contract["jar_contract"]
    if jar["artifact"] != "share/java/zlib-1.3.1.jar":
        raise Failure(f"contract jar artifact is {jar['artifact']}")
    if jar["packages_allowed"] != ["org/zlib/"]:
        raise Failure("contract permits jar entries outside org/zlib/")
    entries = [e["path"] for e in jar["required_entries"]]
    for required in ("META-INF/MANIFEST.MF", "module-info.class",
                     "org/zlib/Zlib.class", "org/zlib/Deflater.class",
                     "org/zlib/Inflater.class"):
        if required not in entries:
            raise Failure(f"contract does not require the jar entry {required}")

    inventory = [f"{i['kind']}:{i['path']}"
                 for i in contract["install_inventory"]["common"]]
    for required in ("file:share/java/zlib-1.3.1.jar",
                     "symlink:share/java/zlib.jar"):
        if required not in inventory:
            raise Failure(f"install inventory does not require {required}")


def check_toolchain() -> None:
    for tool in REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            raise Failure(f"required tool missing from image: {tool}")
    for tool in ("javac", "java", "jar"):
        out = subprocess.run([tool, "--version"], capture_output=True, text=True,
                             check=False)
        if out.returncode != 0:
            raise Failure(f"{tool} is not behavioural in the image")
        text = out.stdout + out.stderr
        if JDK_VERSION not in text:
            raise Failure(
                f"{tool} is not pinned to {JDK_VERSION}: "
                f"{text.strip().splitlines()[:1]}")

    # Exactly one JDK.  A second one is not a cosmetic difference: `javac
    # --release 17` under a JDK 21 produces class files this contract calls
    # correct while compiling against a different java.base signature set, and
    # its jar writes a different Created-By.  A submission that found a second
    # JDK on PATH could be graded against a surface the verifier never saw.
    jvm_dir = Path("/usr/lib/jvm")
    if jvm_dir.is_dir():
        homes = sorted(p.name for p in jvm_dir.iterdir()
                       if p.is_dir() and not p.is_symlink())
        if len(homes) != 1:
            raise Failure(f"expected exactly one JDK under /usr/lib/jvm, found {homes}")
        if "17" not in homes[0]:
            raise Failure(f"the installed JDK is not 17: {homes[0]}")

    # java.util.zip is *present*, and there is nothing this image can do about
    # it: it is part of java.base, which the contract requires.  So the one rule
    # that decides whether this task was done or dodged cannot be enforced by
    # the environment at all -- it is enforced by the verifier, statically in
    # the constant pool and dynamically by class-load observation.  This
    # assertion exists to record that, and to fail loudly if a future edit ever
    # convinces itself the environment is handling it.
    modules = subprocess.run(["java", "--list-modules"], capture_output=True,
                             text=True, check=False)
    if modules.returncode != 0:
        raise Failure("java --list-modules failed")
    names = {line.split("@", 1)[0] for line in modules.stdout.splitlines()}
    if "java.base" not in names:
        raise Failure("java.base is not present, so the contract is unbuildable")
    for banned in ("jdk.unsupported", "java.desktop", "java.logging"):
        if banned not in names:
            # Not a failure: the point is only that these are reachable and the
            # module contract's ban on requiring them is therefore load-bearing.
            # If a future JDK image dropped one, the corresponding ban would be
            # vacuous and worth knowing about.
            print(f"note: {banned} absent from this JDK; its ban is vacuous")


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
