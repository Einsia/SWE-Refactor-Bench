"""Fixtures and helpers for the read-only scan.

Deliberately small. A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, hash bytes. It cannot compile, link or
run the submission, and there is nothing here that would let it -- the stage-1
image fails its own build if `gcc`, `cc`, `make` or either cross compiler is on
PATH, so a module that tried would die on a missing binary rather than quietly
grading a build.

Why this stage reads and does not execute
-----------------------------------------
The subject of this task is a compile-time conditional, and there are two very
different questions about one:

  *What will the preprocessor conclude, and is that conclusion right for each
  target?*  This is settled by compiling for each target and comparing what the
  result computes. It is stage 2's, it needs three toolchains and two emulators,
  and no amount of reading substitutes for it.

  *Is the conclusion determined or asserted?*  A submission can compute the right
  answer for the three targets this benchmark builds and still have written
  `#define WORDS_BIGENDIAN 1` guarded by a check on a distribution name. Stage 2
  cannot see the difference -- both produce identical binaries here. A reader can.

So this stage reads. What it must not do is read *with points attached*, and the
distinction is the whole design of this file.

Nothing here gates on its own
-----------------------------
`swerefactor.scan` sets `required = False` on every check it emits and
`scoring.grade_audit` gates on the required checks, which are the six prose
gates and only those. That is uniform on purpose: a suite where some mechanical
observations gate and others do not is one where the next person to add a pattern
has to guess which kind theirs is, and guesses wrong in the direction that costs a
correct submission its score.

It costs nothing because the findings are addressed to a reviewer who can act on
them. Two shapes, and this task's balance between them is unusual:

  - **Certain.** A sha256 differs. A committed `.o` file. A build file reading
    `SRB_TARGET_NAME`. These are facts, and the gates whose subject they are say
    in as many words what to do with them.

  - **A lead, and here almost everything is one.** Every location this scan
    reports about byte-order handling is a lead. `byteorder-sites` lists where the
    conditionals live and requires nothing of the list, because there is no
    spelling a correct port must use: `__BYTE_ORDER__`, `<endian.h>`,
    `<sys/param.h>`, a `configure`-style probe, a `-DWORDS_BIGENDIAN` added by the
    build for one target -- all correct, and the last one leaves no source line to
    find at all. A check that required a spelling would fail correct ports for
    choosing another; one that rewarded the token would pass
    `/* #define WORDS_BIGENDIAN */`. So the module reports coordinates and the
    review reads them.

There is deliberately no `data()` helper and no frozen manifest. State A is
mounted at /opt/original and hashed directly, so the comparison is against the
same tree the reviewer is reading rather than against a file asserting what that
tree used to contain -- one fewer duplicated input, and nothing that can be
regenerated alone and drift.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/audit"))

#: Directories no scan should walk into. `.git` should not be in a submission at
#: all, but a scan that reported every object in a stray one would bury its own
#: findings; `delivered-state` reports the directory's existence once instead.
EXEMPT_DIRS = (".git", ".svn", ".hg", "__pycache__", ".pytest_cache",
               ".mypy_cache", "node_modules", ".tox", ".obj")

#: Binary and archive. QuickJS ships test262 expectation files and compressed
#: fixtures; neither is a place to look for a byte-order conditional.
EXEMPT_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg", ".pdf",
                   ".gz", ".bz2", ".xz", ".zip", ".o", ".a", ".so", ".lo",
                   ".obj", ".d")

#: Suffixes a text scan will open at all. The empty string catches `Makefile`,
#: `VERSION`, `Changelog`, `TODO` and friends.
TEXT_SUFFIXES = (".c", ".h", ".S", ".js", ".mk", ".m4", ".ac", ".am", ".sh",
                 ".bash", ".py", ".txt", ".in", ".cmake", ".json", ".toml",
                 ".yml", ".yaml", ".cfg", ".ini", ".md", "")

#: The source files this port is about. Used only to *group* a report -- no check
#: requires a change to be confined to them, because a legitimate port may add a
#: detection header or touch the build.
CORE_SOURCES = ("quickjs.c", "quickjs.h", "cutils.h", "cutils.c", "libbf.c",
                "libbf.h", "quickjs-atom.h", "quickjs-opcode.h", "qjs.c",
                "qjsc.c", "quickjs-libc.c", "libregexp.c", "libunicode.c")

#: Every spelling of "which way round is this machine" that a port might
#: reasonably use, plus the tree's own swap helpers and its bytecode version tag.
#:
#: This list exists to *locate*, never to require. A submission using none of
#: these tokens can be correct (the build can define the macro on the compile
#: line); a submission using all of them can be wrong. `byteorder-sites` reports
#: what it found and asserts nothing about the contents of the list.
ORDER_TOKENS = (
    # the tree's own macro
    "WORDS_BIGENDIAN",
    # the compiler's
    "__BYTE_ORDER__", "__ORDER_BIG_ENDIAN__", "__ORDER_LITTLE_ENDIAN__",
    "__BIG_ENDIAN__", "__LITTLE_ENDIAN__", "_BIG_ENDIAN", "__BYTE_ORDER",
    # the platform's
    "endian.h", "sys/param.h", "machine/endian.h", "byteswap.h",
    "BYTE_ORDER", "BIG_ENDIAN", "LITTLE_ENDIAN",
    # architecture predefines a detection might key on
    "__s390x__", "__s390__", "__powerpc__", "__sparc__", "__MIPSEB__",
    "__ARMEB__", "__x86_64__", "__i386__", "__aarch64__", "__arm__",
    # the swap helpers and the bytecode tag
    "bswap16", "bswap32", "bswap64", "byte_swap", "BSWAP",
    "BC_BE_VERSION", "BC_VERSION", "BC_BASE_VERSION",
    "JS_WRITE_OBJ_BSWAP", "is_swap", "htobe", "htole", "be32toh", "le32toh",
    "ntohl", "htonl",
)

#: Environment variables no source or build file has a legitimate reason to read.
#: The `SRB_` prefix is the harness's; code that consults it is reading the grader.
HARNESS_ENV = ("SRB_", "SWEREFACTOR", "SRB_TARGET_NAME", "SRB_TARGET_ROLE",
               "SRB_ORIGINAL", "SRB_RESULT", "SRB_SUITE_DIR", "SRB_WORK",
               "SRB_SUITE_WORK", "SRB_MODULE_DIR", "SRB_SCAN")

#: Paths belonging to the harness rather than to any build.
HARNESS_PATHS = ("/tests/behavioural", "/tests/verification",
                 "/tests/audit", "/logs/verifier", "/opt/original",
                 "/opt/workspace", "/opt/swerefactor")

#: VCS markers no State A in the suite ships, so a submission holding one added it.
#:
#: `.git` is not among them, and its absence is deliberate. The environment image
#: runs `git init` in the workspace, commits State A as `state-a`, and installs a
#: baseline `.gitignore`; SCHEMA.md defines the submission as the workspace as
#: collected, so both arrive in every submission by construction. A check that
#: fires on every submission carries no signal and spends reviewer attention that
#: the real cases need. `test_no_vcs_metadata` still reports a `.git` found
#: anywhere below the root -- a second repository -- and
#: `test_git_holds_only_the_baseline` reports what the root one contains.
VCS_MARKERS = (".gitmodules", ".svn", ".hg", ".github",
               ".gitattributes", ".bzr", "CVS")


def is_exempt(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXEMPT_DIRS for p in parts):
        return True
    return rel.endswith(EXEMPT_SUFFIXES)


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def read_bytes(path: Path, limit: int | None = None) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return b""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def rel_files(root: Path) -> list[str]:
    """Every file under ``root``, root-relative, sorted, minus the noise dirs."""
    out = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        for name in files:
            p = Path(dirpath) / name
            out.append(str(p.relative_to(root)))
    return sorted(out)


def text_files(root: Path):
    """Every text file worth reading, as ``(path, root-relative string)``."""
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        for name in sorted(files):
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if is_exempt(rel):
                continue
            if path.suffix in TEXT_SUFFIXES or path.suffix == "":
                yield path, rel


def cite(path: Path, rel: str, token: str, limit: int = 8) -> list[str]:
    """Every line ``token`` appears on, as ``rel:lineno: text`` citations.

    A finding a reviewer cannot open is not evidence, so no scan check here
    reports a bare boolean: it reports where to look. ``limit`` keeps a token that
    appears two hundred times from filling the prompt.
    """
    out = []
    for n, line in enumerate(read(path).splitlines(), 1):
        if token in line:
            out.append(f"{rel}:{n}: {line.strip()[:160]}")
            if len(out) >= limit:
                out.append(f"{rel}: ... more occurrences not listed")
                break
    return out


def order_sites(root: Path) -> dict[str, list[str]]:
    """``token -> citations`` for every byte-order spelling found under ``root``.

    Locations only. Nothing downstream requires a particular token to be present
    or absent, for the reason at the top of this file: there is no spelling a
    correct port must use, and the most defensible port of all -- one where the
    build defines the macro per target on the compile line -- adds no source line
    for this to find.
    """
    found: dict[str, list[str]] = {}
    for path, rel in text_files(root):
        text = read(path)
        if not text:
            continue
        for token in ORDER_TOKENS:
            if token in text:
                found.setdefault(token, []).extend(cite(path, rel, token))
    return found


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def repo() -> Path:
    """The submitted tree, read-only.

    Named `repo` without apology. In stage 2 a fixture handing out the source
    tree is a mistake -- `infra/tests/test_stages.py` fails the task for having
    one -- because stage 2 measures built executables. Here the tree is the input.
    """
    if not REPO.is_dir():
        pytest.fail(f"the submission is not mounted at {REPO}")
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    """State A, read-only, for the checks that are a comparison."""
    if not ORIGINAL.is_dir():
        pytest.fail(f"State A is not mounted at {ORIGINAL}")
    # An existing-but-empty mount is the more dangerous case. Docker materialises
    # a missing bind source as an empty directory instead of refusing, so a
    # harness pointed at the wrong path inside original.tar.gz -- whose top-level
    # layout is not uniform across tasks -- yields a State A that reads as present
    # and compares as absent. Every comparison then trivially finds nothing, which
    # is indistinguishable from a correctly ported tree.
    #
    # Nothing downstream would catch it. The review schema admits two answers, and
    # a gate the reviewer could not settle is answered `pass` by instruction
    # (`audit.py`'s re-ask says so in as many words), so every comparison gate
    # would come back `pass` on a comparison that had one side missing. This guard
    # is the only thing between those two facts, which is why it fails here rather
    # than skipping.
    if not any(ORIGINAL.iterdir()):
        pytest.fail(
            f"State A is mounted at {ORIGINAL} but is empty; the comparison "
            f"checks cannot run. This is an infrastructure fault in how the "
            f"stage was invoked, not a finding about the submission."
        )
    return ORIGINAL


@pytest.fixture(scope="session")
def delivered_files(repo) -> list[str]:
    return rel_files(repo)


@pytest.fixture(scope="session")
def original_files(original) -> list[str]:
    return rel_files(original)
