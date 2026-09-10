"""Helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, hash bytes, report a line number.  It
cannot build or run the submission, and there is nothing here that would let it --
the stage-1 image fails its own build if `clang`, `gcc`, `make`, `tclsh` or
`wasmtime` is on PATH, so a module that tried would die on a missing binary rather
than quietly grading a build.

Nothing here gates on its own
-----------------------------
`swerefactor.scan` sets `required = False` on every check it emits and
`scoring.grade_audit` gates on the required checks, which are the six prose
gates and only those.  That is uniform on purpose: in a suite where some mechanical
observations gate and others do not, the next person to add a pattern has to guess
which kind theirs is, and guesses wrong in the direction that costs a correct
submission its score.

It costs nothing because the findings are addressed to a reviewer that can act on
them.  Two shapes:

  - **Certain.**  A file State A shipped whose sha256 no longer matches, or a
    delivered file naming `SRB_MODULE_ID`.  The `engine_untouched` gate's prompt
    says what to do with the first: open it, read the diff, and decide whether the
    file is platform layer or engine.  The finding carries the path, so that is one
    read rather than 1,856 hashes.

  - **A lead.**  Everything whose meaning depends on what the file says rather than
    on what its name is.  `unixOpen` in a comment explaining what the port replaced
    is not a finding; the same token inside a function body is the POSIX layer still
    being there.  The scan says where to look; the review says what it means.

Why the symbol lists rather than the file names
-----------------------------------------------
This task's subject is a file that must be deleted, which makes "is it deleted"
the obvious check and the wrong one -- `git mv src/os_unix.c src/platform.c` passes
it.  So the POSIX layer is looked for by the names its own code uses, in every
delivered text file whatever it is called.  A 4,700-line file full of `unixShmMap`
is that file under another name regardless of the name.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/audit"))

#: Directories no scan walks into.  `.git` should not be in a submission at all,
#: but a scan that reported every object in a stray one would bury its own
#: findings; `delivered_state` reports its presence once, as one check.
EXEMPT_DIRS = (".git", ".svn", ".hg", ".fossil-settings", "__pycache__",
               ".pytest_cache", ".mypy_cache", "node_modules", ".tox")

#: Binary and prose.  SQLite ships test vectors, .db files and images; a port is
#: documented in Markdown.  Neither is a place to look for a live POSIX layer.
EXEMPT_SUFFIXES = (".md", ".markdown", ".rst", ".png", ".jpg", ".jpeg", ".gif",
                   ".ico", ".svg", ".pdf", ".gz", ".bz2", ".xz", ".zip", ".tar",
                   ".db", ".sqlite", ".o", ".a", ".so", ".wasm", ".lo", ".la")

#: Suffixes a text scan opens at all.  The empty string catches `Makefile`,
#: `manifest`, `VERSION` and `configure`.
TEXT_SUFFIXES = (".c", ".h", ".cc", ".cpp", ".S", ".s", ".sh", ".bash", ".py",
                 ".tcl", ".test", ".in", ".mk", ".am", ".ac", ".m4", ".txt",
                 ".json", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".pc", "")

#: How much of a file a content scan reads.  os_unix.c is 271 KB and the
#: amalgamation is 7 MB; a scan that read every byte of every file would spend its
#: timeout on generated output.  Anything hiding past 512 KB of one file is past
#: what a string match was going to settle anyway.
READ_LIMIT = 512 * 1024

# --------------------------------------------------------------------------- #
# The POSIX layer, by its own vocabulary
# --------------------------------------------------------------------------- #
# Function names from State A's src/os_unix.c, chosen because they are unique to
# it: `unixOpen` appears in no other file in the payload, and nothing outside that
# translation unit has a reason to define it.  A delivered file carrying several of
# them is that code, whatever the file is called.
#
# Not a completeness claim.  Sixty-three `unix[A-Z]*` identifiers are in State A's
# copy; these are the ones whose presence is hardest to explain away.
UNIX_VFS_SYMBOLS = (
    "unixOpen", "unixClose", "unixRead", "unixWrite", "unixTruncate",
    "unixSync", "unixFileSize", "unixLock", "unixUnlock", "unixFileControl",
    "unixSectorSize", "unixDeviceCharacteristics", "unixShmMap", "unixShmLock",
    "unixShmBarrier", "unixShmUnmap", "unixFetch", "unixUnfetch",
    "unixFullPathname", "unixDlOpen", "unixDlError", "unixDlSym", "unixDlClose",
    "unixRandomness", "unixSleep", "unixCurrentTime", "unixGetLastError",
    "unixEnterMutex", "unixLeaveMutex", "unixMapfile", "unixRemapfile",
    "unixCheckReservedLock", "unixGetTempname", "unixLogErrorAtLine",
    "posixIoFinderImpl", "posixOpen", "posixFchown",
)

#: The same for src/os_win.c.  A port that deleted the POSIX layer and left the
#: Windows one has not finished; a port that renamed it has done something worse.
WIN_VFS_SYMBOLS = (
    "winOpen", "winClose", "winRead", "winWrite", "winTruncate", "winSync",
    "winFileSize", "winLock", "winUnlock", "winFileControl", "winShmMap",
    "winFullPathname", "winDlOpen", "winRandomness", "winSleep",
    "winCurrentTime", "winGetLastError", "winMapfile",
)

#: The first lines of State A's os_unix.c and os_win.c descriptions, verbatim.  A
#: renamed copy keeps its banner: nobody who is hiding a file edits the comment.
UNIX_BANNER = "This file contains the VFS implementation for unix-like operating systems"
WIN_BANNER = "This file contains code that is specific to Windows"

#: POSIX interfaces the platform layer used and a WASI guest does not have.  Their
#: presence in a delivered *platform* file is a lead, not a finding: `#ifndef
#: SQLITE_OS_OTHER` around dead code, a comment naming what was replaced, and a
#: live call all look the same to a scan.  The engine mentions some of these in
#: files that are not the platform layer at all.
POSIX_INTERFACES = (
    "fcntl(", "flock(", "mmap(", "munmap(", "getcwd(", "fork(", "pthread_",
    "sys/mman.h", "sys/file.h", "dlfcn.h", "pwd.h", "sys/ioctl.h",
    "F_SETLK", "F_GETLK", "PTHREAD_MUTEX", "gethostuuid",
)

#: Names that only exist because a grader exists.  A delivered file mentioning one
#: is not ambiguous in the way the rest of this file is.
HARNESS_TOKENS = (
    "SRB_REPO", "SRB_ORIGINAL", "SRB_MODULE_DIR", "SRB_SUITE_DIR", "SRB_WORK",
    "SRB_SUITE_WORK", "SRB_RESULT", "SRB_MODULE_ID", "SRB_SCAN", "SRB_INFRA",
    "SRB_RESULTS", "SRB_TARGET_ROLE", "SRB_TARGET_NAME", "SRB_REFERENCE",
    "swerefactor", "/opt/reference", "/opt/swerefactor", "/logs/verifier",
    "/tests/behavioural", "/tests/audit", "/tests/verification",
    "PF02_BUILD_TIMEOUT", "PF02_STATE_A_TAG",
)

#: What a wasm module gets its host functions from.  Declaring an import outside
#: `wasi_snapshot_preview1` is how a "port" calls back into native code, and the
#: clang spellings for it are these.  Stage 2 proves the absence from the module's
#: own import section, which is stronger -- but stage 2 only runs if this gate
#: passes, so the source-level version is worth reporting here.
IMPORT_ATTRIBUTES = (
    "import_module", "import_name", "__wasm_import_module__",
    "wasm-import-module", "wasm_import_module",
)


def is_exempt(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXEMPT_DIRS for p in parts):
        return True
    return rel.endswith(EXEMPT_SUFFIXES)


def read(path: Path, limit: int = READ_LIMIT) -> str:
    try:
        with path.open("r", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


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
    out: list[str] = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        for name in files:
            out.append(str((Path(dirpath) / name).relative_to(root)))
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
            if path.suffix in TEXT_SUFFIXES:
                yield path, rel


def c_files(root: Path):
    """Delivered C and header files.  The set a question about the platform layer
    is entitled to read; a question about it that reads `manifest` is lost."""
    for path, rel in text_files(root):
        if path.suffix in (".c", ".h", ".cc", ".cpp"):
            yield path, rel


def build_files(root: Path):
    """The files that describe a build.  `configure` is included by name because
    State A ships a generated one and a port may keep, edit or delete it."""
    for path, rel in text_files(root):
        name = path.name
        if (name.startswith("Makefile") or name in ("configure", "configure.ac",
                                                    "main.mk", "Makefile.in")
                or path.suffix in (".mk", ".sh", ".bash", ".am", ".ac", ".m4")):
            yield path, rel


def cite(path: Path, rel: str, token: str, limit: int = 6) -> list[str]:
    """Every line ``token`` appears on, as ``rel:lineno: text`` citations.

    A finding a reviewer cannot open is not evidence, so no check here reports a
    bare boolean: it reports where to look.  ``limit`` keeps a token that appears
    two hundred times from filling the prompt.
    """
    out: list[str] = []
    for n, line in enumerate(read(path).splitlines(), 1):
        if token in line:
            out.append(f"{rel}:{n}: {line.strip()[:160]}")
            if len(out) >= limit:
                out.append(f"{rel}: ... more occurrences not listed")
                break
    return out


def hits(path: Path, tokens) -> list[str]:
    """Which of ``tokens`` appear in ``path`` at all.  One read, not one per
    token: this runs over ~1,800 files and the naive version reads each of them
    forty times."""
    body = read(path)
    return [t for t in tokens if t in body]


def first_line_of(path: Path, token: str) -> int | None:
    for n, line in enumerate(read(path).splitlines(), 1):
        if token in line:
            return n
    return None


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
#
# How a finding reaches the reviewer, and why it is not a bare `assert`.
#
# `swerefactor.scan.digest` renders a flagged check as two lines: the check's
# `summary`, then the first 240 characters of its `detail` if that differs.
# `pytest_module._summary` fills `summary` from the *last* `E ` line of the failure,
# or -- when there is no traceback -- from the last line of the message.  A bare
# `assert cond, "some sentence"` therefore reports pytest's assertion rewrite as the
# headline:
#
#     - [fail] ...::test_state_a_layer_files_by_name: assert not ['src/os_unix.c']
#
# which was measured, not predicted.  `assert []` and `assert not [...]` were the
# summaries of three of this suite's four findings on a do-nothing tree.  A reviewer
# with sixty of those has sixty check names and no locations, and the digest exists
# precisely so it does not have to open sixty result files to get them.
#
# So findings are raised through `flag`, which assembles the message in the order
# those two readers consume it: the interpretation first, because `detail` is read
# from the front, and the headline with its paths last, because `summary` is read
# from the back.  The ordering is deliberate and is the reason this is a helper
# rather than eleven hand-written strings -- it is the kind of detail that a later
# edit appending "one more sentence" to a message would silently break.

def render(items, *, limit: int = 6) -> str:
    """Paths, or paths and what was found in them, as one line.

    A mapping renders as ``src/a.c (unixOpen, unixLock); src/b.c (unixShmMap)`` and
    a sequence as ``src/a.c; src/b.c``.  Capped, because a token that appears in two
    hundred files is one finding and not two hundred, and because everything past
    the summary's 300 characters is dropped by the renderer anyway.
    """
    if isinstance(items, dict):
        shown = [f"{k} ({', '.join(str(v) for v in vals)})" if not isinstance(vals, str)
                 else f"{k} ({vals})"
                 for k, vals in list(items.items())[:limit]]
        hidden = len(items) - len(shown)
    else:
        items = list(items)
        shown = [str(i) for i in items[:limit]]
        hidden = len(items) - len(shown)
    line = "; ".join(shown)
    if hidden > 0:
        line += f"; ... and {hidden} more"
    return line


def flag(headline: str, where=(), note: str = "") -> None:
    """Report this check as flagged, with ``headline`` as the reviewer's headline.

    ``where`` is rendered onto the end of the headline, so the summary the reviewer
    reads carries the locations.  ``note`` is what to make of it, and lands in the
    detail line beneath.  Raises, so a check body reads as a guard clause:

        if found:
            S.flag("delivered files carry the POSIX vocabulary", found, note=...)

    ``pytrace=False`` keeps the module's own source out of the failure text.  With a
    traceback, `detail` is 1,800 characters of pytest echoing this file back, and the
    240 the reviewer sees are the `for` loop above the `flag` call rather than the
    finding.
    """
    __tracebackhide__ = True
    headline = " ".join(headline.split())
    if where:
        rendered = render(where) if not isinstance(where, str) else where
        headline = f"{headline}: {rendered}"
    note = " ".join(note.split())
    pytest.fail(f"{note}\n{headline}" if note else headline, pytrace=False)


def is_authored(rel: str) -> bool:
    """Did the submission write this file, or did it arrive with State A unchanged?

    Every content check in this scan is scoped by this, and the reason is the
    do-nothing submission.  Run the unscoped version against a tree nobody touched
    and it reports `fcntl(` in `src/os_unix.c`, `pthread_` in
    `ext/async/sqlite3async.c`, `http://` in `configure`, and `sqlite3.c` in five
    makefiles -- forty lines of leads, every one of them a quotation of SQLite
    3.31.1 rather than of the submission.  A reviewer handed that list learns
    nothing about the submission and has forty citations to re-open before finding
    out.

    Worse, it is noise that scales the wrong way: the more faithfully a port leaves
    the engine alone, the more of State A's own text the scan quotes back.  A
    submission that deleted half the tree would produce a *shorter* findings block
    than one that deleted the two files it was asked to.

    So a byte-identical file is not the submission's statement about anything.  The
    exception is the platform layer's own filenames, which are checked by *name*
    elsewhere in this scan: `src/os_unix.c` surviving byte-identical is precisely
    the finding, and that check does not go through here.
    """
    if not ORIGINAL.is_dir():
        # No reference mounted: treat everything as authored rather than nothing.
        # The alternative silently empties every content check, and an empty
        # findings block is indistinguishable from a clean tree in the prompt.
        return True
    theirs = ORIGINAL / rel
    if not theirs.is_file():
        return True
    mine = REPO / rel
    try:
        if mine.stat().st_size != theirs.stat().st_size:
            return True
    except OSError:
        return True
    return sha256(mine) != sha256(theirs)


def authored_text_files(root: Path):
    """``text_files`` minus the files State A shipped and nobody touched."""
    for path, rel in text_files(root):
        if is_authored(rel):
            yield path, rel


def authored_c_files(root: Path):
    """``c_files`` minus the ones that arrived unchanged."""
    for path, rel in c_files(root):
        if is_authored(rel):
            yield path, rel


def authored_build_files(root: Path):
    """``build_files`` minus the ones that arrived unchanged."""
    for path, rel in build_files(root):
        if is_authored(rel):
            yield path, rel


def introduced(rel: str, tokens) -> list[str]:
    """Which of ``tokens`` the submission put in ``rel`` that were not there before.

    The finer instrument of the two.  ``is_authored`` asks whether a file was
    touched; this asks whether a *string* is the submission's.  The difference shows
    up on inherited text in an edited file: `src/os_win.h` was to be kept, it opens
    with the same banner comment `src/os_win.c` does, and a port that edits one line
    of it has not thereby confessed to hiding the Windows layer.  Scoping by file
    would report that banner; scoping by token does not, because the banner is in
    State A's copy too.

    A file State A never shipped has everything in it introduced.
    """
    mine = read(REPO / rel)
    present = [t for t in tokens if t in mine]
    if not present:
        return []
    if not ORIGINAL.is_dir() or not (ORIGINAL / rel).is_file():
        return present
    theirs = read(ORIGINAL / rel)
    return [t for t in present if t not in theirs]


#: What makes a file look like a platform layer rather than a file that merely
#: mentions one.  Any of these beside a ``sqlite3_vfs`` is enough.
#:
#: The names below are the ones a VFS cannot avoid: it has to be registered, and
#: SQLITE_OS_OTHER makes ``sqlite3_os_init`` the entry point where that happens.
VFS_EVIDENCE = ("sqlite3_vfs_register", "sqlite3_os_init", "sqlite3_vfs_find",
                "xOpen", "iVersion", "sqlite3_io_methods")


def vfs_files(root: Path, authored_only: bool = True):
    """Files that look like they define or register a ``sqlite3_vfs``.

    State A ships four of these besides its two platform layers -- a demo VFS, two
    test harnesses and the async extension -- so a question about *the new platform
    layer* has to exclude the ones that arrived with the tree, or it is a question
    about SQLite's own test suite.
    """
    walk = authored_c_files if authored_only else c_files
    for path, rel in walk(root):
        body = read(path)
        if "sqlite3_vfs" in body and any(t in body for t in VFS_EVIDENCE):
            yield path, rel


# There is no frozen manifest of State A's checksums in this image, and the absence
# is deliberate.  A `sources.json` copied in here would be a second file claiming to
# be State A's contents, and the shared-input drift check only compares duplicates
# whose basename appears in `environment/` -- so the two could drift silently.  This
# stage has State A mounted at /opt/original and hashes it directly, which is both
# one fewer duplicated input and a stronger claim: the comparison is against the
# same tree the reviewer is reading, not against a file asserting what that tree
# used to hold.


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def repo() -> Path:
    """The submitted tree, read-only.

    Named `repo` without apology.  In stage 2 a fixture handing out the source
    tree would be a mistake -- that stage measures a built module, and a check in
    it that reads the source is asserting on the implementation.  Here the source
    is the input.
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
def delivered(repo) -> list[str]:
    """Every delivered file, root-relative.  Session-scoped: four modules ask for
    it and the tree is ~1,900 files."""
    return rel_files(repo)


@pytest.fixture(scope="session")
def state_a(original) -> list[str]:
    """Every file State A shipped, root-relative."""
    return rel_files(original)
