"""Fixtures and helpers for the read-only scan.

Deliberately small. A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, hash bytes. It cannot configure, build
or run the submission, and there is nothing here that would let it -- the stage-1
image fails its own build if `cmake`, `make`, `gcc`, `autoconf`, `automake` or
`libtool` is on PATH, so a module that tried would die on a missing binary rather
than quietly grading a build.

These checks run in the stage that may read and may not execute. A sha256 over
`src/libsodium/crypto_box/crypto_box.c` is a measurement -- nothing about it is a
matter of opinion, and a submission that edited that file to make its CMake code
work has changed the thing under test. Walking the delivered tree for `Makefile.am`
is reading the repository rather than comparing artefacts, though, so it is not
stage 2's kind of claim and does not carry points here.

Nothing here gates on its own
-----------------------------
`swerefactor.scan` sets `required = False` on every check it emits (scan.py:105) and
`scoring.grade_audit` gates on the required checks, which are the six prose
gates and only those. That is uniform on purpose: a suite where some mechanical
observations gate and others do not is one where the next person to add a regex has
to guess which kind theirs is, and guesses wrong in the direction that costs a
correct submission its score.

It costs nothing, because the findings are addressed to a reviewer who can act on
them. Two shapes:

  - **Certain.** A sha256 mismatch, or a build file naming `SRB_TARGET_NAME`. The
    `sources_untouched` gate's prompt says so in as many words: if the scan reports
    a mismatch, open the file, confirm it differs, fail the gate. The finding
    carries the path, so confirming it is one read.

  - **A lead.** Everything whose meaning depends on what the file says rather than
    what its name is. `cmake/FindSodium.cmake` mentioning `configure` in a comment
    is not a finding; a `cmake/` macro that shells out to a configure script is.
    The scan says where to look; the review, which has both trees open, says what
    it means.
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
#: findings; the review is told to look for it separately.
EXEMPT_DIRS = (".git", ".svn", ".hg", "__pycache__", ".pytest_cache",
               ".mypy_cache", "node_modules", ".tox")

#: Prose and binary. A migration is documented in Markdown and libsodium ships
#: test vectors; neither is a place to look for a live Autotools dependency.
EXEMPT_SUFFIXES = (".md", ".markdown", ".rst", ".png", ".jpg", ".jpeg", ".gif",
                   ".ico", ".svg", ".pdf", ".gz", ".bz2", ".xz", ".zip",
                   ".tar", ".exp", ".woff", ".woff2")

#: Suffixes a text scan will open at all. The empty string catches `AUTHORS`,
#: `ChangeLog`, `Makefile` and friends.
TEXT_SUFFIXES = (".c", ".h", ".S", ".cmake", ".txt", ".in", ".sh", ".bash",
                 ".py", ".yml", ".yaml", ".json", ".toml", ".cfg", ".ini",
                 ".m4", ".am", ".ac", ".mk", ".pc", "")


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
    """Every file under ``root``, repo-relative, sorted, minus the noise dirs."""
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


def build_files(root: Path):
    """The files that describe a build: CMake, shell, and anything named like a
    makefile fragment. This is the set a question about *the build system* is
    entitled to read; a question about it that reads `crypto_box.c` is lost."""
    for path, rel in text_files(root):
        name = path.name
        if (name == "CMakeLists.txt" or path.suffix in (".cmake", ".sh",
                                                       ".bash", ".mk", ".am",
                                                       ".ac", ".m4")
                or name.startswith("Makefile")):
            yield path, rel


def cite(path: Path, rel: str, token: str, limit: int = 6) -> list[str]:
    """Every line ``token`` appears on, as ``rel:lineno: text`` citations.

    A finding a reviewer cannot open is not evidence, so no scan check reports a
    bare boolean: it reports where to look. ``limit`` keeps a token that appears
    two hundred times from filling the prompt.
    """
    out = []
    for n, line in enumerate(read(path).splitlines(), 1):
        if token in line:
            out.append(f"{rel}:{n}: {line.strip()[:160]}")
            if len(out) >= limit:
                out.append(f"{rel}: ... more occurrences not listed")
                break
    return out


# There is no `data()` helper here, and the absence is deliberate. A frozen
# `sources.json` copied into this image would be a second file claiming to be State
# A's checksums, and `_shared_input_drift` (cli.py:291) only compares duplicates
# whose basename appears in `environment/` -- so the two would drift silently. This
# stage has State A mounted at /opt/original and hashes it directly. Stage 2 needs
# the frozen manifest because by the time it runs it holds a matrix of build trees
# and no reference tree; that is its constraint, not one to inherit.


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def repo() -> Path:
    """The submitted tree, read-only.

    Named `repo` without apology. In stage 2 a fixture handing out the source
    tree is a mistake -- `infra/tests/test_stages.py` fails the task for having
    one -- because stage 2 measures a build. Here it is the input.
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
    # is indistinguishable from a correctly ported tree, and nothing downstream
    # can tell the two apart: the reviewer has to answer pass or fail, and it
    # would be answering about a comparison that had one side missing. This guard
    # is the only thing between those two facts, which is why it fails here
    # rather than skipping.
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
