"""Fixtures and helpers for the read-only scan.

Deliberately small. A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, hash bytes. It cannot configure, build
or run the submission, and there is nothing here that would let it -- the stage-1
image fails its own build if `meson`, `ninja`, `gcc`, `cc`, `make` or a PEP 517
front end is present, so a module that tried would die on a missing binary rather
than quietly grading a build.

Nothing here is scored, and the reason is that these checks are not one kind of
thing.

Some are measurements: a sha256 over `src/AES.c` is not a matter of opinion, and a
submission that edited that file to make its Meson code work has changed the thing
under test.

Others only look like measurements, and they are the reason the split exists. A
check that walks the delivered tree looking for `setup.py` is not comparing
artifacts, it is reading the repository -- and a stage that builds both sides and
compares what they produce should be doing exactly that. Scored side by side, a
check on whether a path exists and a check on whether 277 symbols match are the
same kind of claim as far as the score is concerned, which is the confusion this
stage exists to undo: reading the repository happens here, where it may read and
may not execute, and comparing what was built happens in stage 2.

Nothing here gates on its own
-----------------------------
`swerefactor.scan` sets `required = False` on every check it emits and
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
    what its name is. A `meson.build` that mentions `setuptools` in a comment
    explaining what the old build did is not a finding; a `meson.build` whose
    `custom_target` runs `setup.py` is. The scan says where to look; the review,
    which has both trees open, says what it means.

One shape peculiar to this task
-------------------------------
Meson does not glob. 192 `.py` files, 96 `.pyi` files and a `py.typed` have to be
named somewhere, so a correct submission contains long explicit source lists in 20
`meson.build` files, and any check that treats "an enumerated list of Python files"
as suspicious will fire on every correct submission. Nothing here counts lines or
measures how a source list is spelled; the prompt tells the reviewer the same
thing.
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
               ".mypy_cache", ".ruff_cache", "node_modules", ".tox",
               ".mesonpy-native-file", "meson-logs", "meson-info",
               "meson-private")

#: Prose and binary. A migration is documented in Markdown and pycryptodome ships
#: test vectors and PDFs under Doc/; neither is a place to look for a live
#: setuptools dependency.
EXEMPT_SUFFIXES = (".md", ".markdown", ".rst", ".png", ".jpg", ".jpeg", ".gif",
                   ".ico", ".svg", ".pdf", ".gz", ".bz2", ".xz", ".zip",
                   ".tar", ".whl", ".woff", ".woff2", ".der", ".pem", ".txt.gz")

#: Suffixes a text scan will open at all. The empty string catches `AUTHORS`,
#: `Changelog`, `meson.options` and friends.
TEXT_SUFFIXES = (".c", ".h", ".S", ".py", ".pyi", ".pyx", ".build", ".options",
                 ".txt", ".in", ".sh", ".bash", ".cmd", ".yml", ".yaml",
                 ".json", ".toml", ".cfg", ".ini", ".mk", ".m4", ".ac", ".am",
                 ".pth", "")

#: Where the Python payload lives, and where the C lives. Two roots, because the
#: contract freezes both and the reasons differ: `lib/` is what the wheel installs,
#: `src/` is what the compiler reads.
PAYLOAD_ROOT = "lib"
CSOURCE_ROOT = "src"


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
    """The files that describe a build: Meson files, the PEP 517 manifest, shell
    and any Python that is not part of the library's payload.

    This is the set a question about *the build system* is entitled to read; a
    question about it that reads `lib/Crypto/Cipher/AES.py` is lost. The
    `lib/`-prefix exclusion is what keeps 288 payload files out of it -- they are
    the thing being built, not the thing doing the building.
    """
    for path, rel in text_files(root):
        if rel.split("/")[0] == PAYLOAD_ROOT:
            continue
        name = path.name
        if (name in ("meson.build", "meson.options", "meson_options.txt",
                     "pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in")
                or path.suffix in (".build", ".options", ".sh", ".bash", ".mk",
                                   ".m4", ".ac", ".am", ".cmd")
                or (path.suffix == ".py" and rel.split("/")[0] != CSOURCE_ROOT)):
            yield path, rel


def strip_prose(path: Path, text: str) -> str:
    """Blank out comments and docstrings, keeping every line number intact.

    A token search over a build file that counts comments is a token search that
    fires on every careful migration, because a careful migration says in a
    comment what it replaced. This removes the prose and leaves the code, so what
    is left is the retired toolchain named where the build language would evaluate
    it.

    Line numbers are preserved rather than lines removed: a citation the reviewer
    cannot open at the line named is discarded by the adjudicator, so a search
    that reports line 9 for something on line 14 is worse than no search.

    `#` to end-of-line is the comment syntax of all three languages that matter
    here -- Meson, TOML, shell. Python docstrings need the parser, because a
    module docstring is a string expression and no lexical rule distinguishes it
    from a string that is an argument. Python that will not parse falls back to
    comment-stripping alone; a `meson/version.py` with a syntax error is a finding
    the build stage will make far more loudly than this one could.
    """
    lines = text.splitlines()

    if path.suffix == ".py":
        import ast

        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            blank: set[int] = set()
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(body, list) or not body:
                    continue
                first = body[0]
                if (isinstance(first, ast.Expr)
                        and isinstance(first.value, ast.Constant)
                        and isinstance(first.value.value, str)):
                    end = getattr(first, "end_lineno", first.lineno)
                    blank.update(range(first.lineno, (end or first.lineno) + 1))
            lines = ["" if n in blank else line
                     for n, line in enumerate(lines, 1)]

    out = []
    for line in lines:
        # Not a full lexer: a `#` inside a string literal is cut here too. That
        # errs toward silence, which is the right direction -- this search decides
        # whether to *report*, and a missed report costs the reviewer a lead it
        # can still find by reading, while a false one costs its attention.
        out.append(line.split("#", 1)[0] if "#" in line else line)
    return "\n".join(out)


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
# `baseline.json` copied into this image would be a second file claiming to be
# State A's checksums, and `_shared_input_drift` (cli.py:291) only compares
# duplicates whose basename appears in `environment/` -- so the two would drift
# silently. This stage has State A mounted at /opt/original and hashes it directly.
# Stage 2 needs the frozen manifest because by the time it runs it holds five build
# trees and no reference tree; that is its constraint, not one to inherit.


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
