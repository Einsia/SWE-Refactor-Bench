"""What the old build left behind.

Three files carried the setuptools build and are named in the contract as gone:
`setup.py`, `setup.cfg` and `MANIFEST.in`, plus the 700-line ISA probe engine at
`compiler_opt.py` that `setup.py` imported.  Every check here is a question about
whether a path exists, which is the one kind of question a scan can settle by
itself -- `Path.exists()` has no interpretation.

What is *not* here is any attempt to read a build file and decide whether it is
"really" a Meson build or setuptools in disguise.  Matching
`/setuptools|distutils/` across the delivered tree and reporting a hit cannot tell

    # replaces what setup.py's build_ext used to do by hand

from

    py.install_sources(..., install_dir: ...)   # via setuptools compat shim

and the first is what a well-commented migration looks like.  The token searches
that remain are here as *citations for the reviewer*: they carry `path:lineno:
text`, and the prompt tells the review to open the line and judge it.

The one content match with teeth is the generated-file banner.  `MANIFEST.in` and
`setup.py` are hand-written and can be renamed; `compiler_opt.py` cannot plausibly
be renamed and still be what it is, and a file that opens with pycryptodome's own
probe-engine docstring is that file wherever it sits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from srbscan import build_files, read, rel_files, strip_prose, text_files

pytestmark = pytest.mark.scan


#: Named in the contract as removed.  Each one is a build input the new build
#: system has to replace, not a file that merely became unnecessary.
#:
#: `setup.cfg` is deliberately *not* on this list.  It carries setuptools build
#: configuration -- `[bdist_wheel]`, `[metadata]`, `[egg_info]` -- and it also
#: carries `[flake8]`, which has nothing to do with the build and which flake8
#: still reads.  "Delete setup.cfg" and "retire setuptools" are different
#: instructions, and a submission that stripped the build sections and kept the
#: linter config did the more careful thing.  What is in it is the
#: `setuptools_retired` gate's business, with the file open.
DELETED_FILES = (
    "setup.py",
    "MANIFEST.in",
    "compiler_opt.py",
)

#: Sections of `setup.cfg` that only setuptools reads.  Reported, because a
#: `[bdist_wheel]` table under a Meson build is dead configuration at best and a
#: second build path at worst -- but reported as one finding naming the sections,
#: not as a demand that the file be gone.
SETUPTOOLS_ONLY_SECTIONS = ("bdist_wheel", "egg_info", "build_sphinx",
                            "options", "options.packages.find",
                            "options.entry_points")

#: Directories the old build produced.  A delivered `build/` is stale output; a
#: delivered `*.egg-info/` is a setuptools install that ran and was committed.
STALE_OUTPUT_DIRS = (
    "build",
    "dist",
    "pycryptodome.egg-info",
    ".eggs",
    "meson-logs",
    "meson-info",
    "meson-private",
)

#: Files the contract freezes at the top level.  Losing one of these is not a
#: migration decision, it is a deletion.
PRESERVED = (
    "README.rst",
    "LICENSE.rst",
    "AUTHORS.rst",
    "Changelog.rst",
    "FuturePlans.rst",
    "INSTALL.rst",
)

#: The first line of pycryptodome's own probe engine, near enough.  A renamed copy
#: of `compiler_opt.py` still says this.
COMPILER_OPT_MARKERS = (
    "compiler_has_support",
    "test_compilation",
    "_compiler_supports",
    "compiler_supports_",
)

#: Tokens worth a citation.  Not a verdict -- see the module docstring.
SUSPECT_TOKENS = (
    "setuptools",
    "distutils",
    "pkg_resources",
    "setup.py",
    "build_ext",
    "Extension(",
)


def test_the_submission_is_mounted(repo):
    """First, because everything below is vacuous without it.

    An unmounted or empty submission would make every `not exists` check below
    pass, and the reviewer would read a clean provenance report about nothing.
    """
    files = rel_files(repo)
    assert len(files) > 100, (
        f"{repo} holds {len(files)} files; the submission is missing or truncated, "
        "and every 'file is absent' finding below is meaningless until it is not"
    )


@pytest.mark.parametrize("name", DELETED_FILES)
def test_retired_build_file_is_gone(repo, name):
    hits = sorted(str(p.relative_to(repo)) for p in repo.rglob(name))
    assert not hits, f"{name} is still in the tree at: {', '.join(hits)}"


@pytest.mark.parametrize("name", STALE_OUTPUT_DIRS)
def test_stale_output_directory_is_absent(repo, name):
    hits = sorted(str(p.relative_to(repo)) for p in repo.rglob(name) if p.is_dir())
    assert not hits, (
        f"{name}/ exists at: {', '.join(hits)} -- build output, not source"
    )


@pytest.mark.parametrize("name", PRESERVED)
def test_documentation_survived(repo, name):
    assert (repo / name).is_file(), f"{name} was deleted"


def test_no_vcs_metadata(repo):
    """A non-git VCS at the root, which the baseline does not explain.

    `.git` is not in this list, and the reason is a premise worth stating rather
    than assuming: the environment image runs `git init` in the workspace and
    commits State A as `state-a` so the agent has a diff to work against, and
    SCHEMA.md defines the submission as the workspace as collected. So a git
    repository at the root is expected on every submission, and flagging its
    presence as "created during the run" would fire on all of them. A Mercurial or
    Subversion directory still has no explanation; what the git repository
    *contains* is the next check.
    """
    markers = [".hg", ".svn"]
    found = [m for m in markers if (repo / m).exists()]
    assert not found, (
        f"{', '.join(found)} exists at the repository root.  State A ships no "
        "version control and the environment creates only a git baseline, so "
        "this was created during the run -- read what is in it before trusting "
        "anything else in the tree"
    )


def test_git_history_is_only_the_baseline(repo):
    """The baseline is one commit tagged `state-a` with no remotes.

    A second commit, a configured remote, or remote-tracking refs are how
    upstream history -- in which pycryptodome's build already exists -- would
    arrive, and how the agent's working notes would come with it. Read from the
    files: this verifier image ships no git binary, so running git is not an
    option here.
    """
    gitdir = repo / ".git"
    if not gitdir.is_dir():
        pytest.skip("no git metadata in the delivered tree")

    findings = []

    config = gitdir / "config"
    if config.is_file():
        remotes = re.findall(r'^\s*\[remote\s+"([^"]+)"\]',
                            config.read_text(errors="replace"), re.MULTILINE)
        if remotes:
            findings.append(f"remote(s) configured: {sorted(set(remotes))}")

    refs_remotes = gitdir / "refs" / "remotes"
    if refs_remotes.is_dir() and any(refs_remotes.rglob("*")):
        findings.append("remote-tracking refs present under .git/refs/remotes")

    packed = gitdir / "packed-refs"
    if packed.is_file() and "refs/remotes/" in packed.read_text(errors="replace"):
        findings.append("packed-refs holds remote-tracking refs")

    reflog = gitdir / "logs" / "HEAD"
    if reflog.is_file():
        lines = [ln for ln in reflog.read_text(errors="replace").splitlines()
                 if ln.strip()]
        if len(lines) > 1:
            findings.append(f"{len(lines)} HEAD reflog entries; baseline writes one")

    assert not findings, (
        "the delivered git repository holds more than the baseline snapshot: "
        + "; ".join(findings)
        + ". Anything past the single state-a commit came from the run"
    )


def test_compiler_opt_was_not_renamed(repo):
    """The probe engine is recognisable by what it says, not by its name.

    Four function names from pycryptodome's `compiler_opt.py`.  Rewriting the
    probes in Meson is the task; carrying the Python engine forward under a new
    name and calling it from `meson.build` is the task avoided, and this is the
    check that says which happened.
    """
    offenders = []
    for path, rel in text_files(repo):
        if path.suffix != ".py":
            continue
        body = read(path)
        matched = [m for m in COMPILER_OPT_MARKERS if m in body]
        if len(matched) >= 2:
            offenders.append(f"{rel}: contains {', '.join(matched)}")
    assert not offenders, (
        "a file carries the retired probe engine's own function names:\n  "
        + "\n  ".join(offenders)
        + "\n  compiler_opt.py was removed; these probes belong in the build "
        "system now.  Open the file: a Meson script that reimplements the same "
        "questions is correct, a copy of the deleted module is not."
    )


def test_setuptools_is_not_named_in_executable_position(repo):
    """A citation list, and prose does not count.

    The obvious version of this check -- grep the build files for `setuptools` --
    fires on every careful migration, because a careful migration says in a
    comment what it replaced. Three lines of the reference port trip it:

        meson.build:3      # This replaces setup.py + compiler_opt.py entirely.
        pyproject.toml:3   # Metadata is PEP 621 ... transcribed from what setup.py
        meson/version.py:4 setup.py used to do this inline. Meson has no regex

    All three are the author explaining themselves, and all three are worth
    having in the repository. Only failing checks are rendered into the reviewer's
    prompt, so a check that flags them makes every correct submission arrive
    pre-flagged -- and a reviewer who has learned that the first three findings
    are always prose stops reading the fourth.

    So comments and docstrings come out first, and what is left is the retired
    toolchain named where the build language would *evaluate* it: a
    `run_command('python', 'setup.py', ...)`, an `import setuptools`, a
    `requires = ["setuptools"]`. Those are still leads rather than verdicts -- a
    `meson.build` might name `setup.py` in a list of files it deleted -- but they
    are leads about code, which is what the prompt asks the review to follow.
    """
    citations = []
    for path, rel in build_files(repo):
        code = strip_prose(path, read(path))
        for n, line in enumerate(code.splitlines(), 1):
            for token in SUSPECT_TOKENS:
                if token in line:
                    citations.append(f"{rel}:{n}: {line.strip()[:160]}")
                    break
    assert not citations, (
        "the retired toolchain is named in executable position -- comments and "
        "docstrings were stripped before this search, so these are lines the "
        f"build language evaluates ({len(citations)} citations):\n  "
        + "\n  ".join(citations[:40])
    )


def test_setup_cfg_carries_no_setuptools_build_configuration(repo):
    """If `setup.cfg` survived, what is left in it.

    Not "setup.cfg is gone" -- see DELETED_FILES. A `[flake8]` section is a linter
    setting and stays. A `[bdist_wheel]` section under a Meson build is at best
    dead text and at worst a second way to build the package, which is the thing
    being retired.
    """
    cfg = repo / "setup.cfg"
    if not cfg.is_file():
        return  # removing it entirely is also correct
    found = []
    for n, line in enumerate(read(cfg).splitlines(), 1):
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            if section in SETUPTOOLS_ONLY_SECTIONS:
                found.append(f"setup.cfg:{n}: {s}")
    assert not found, (
        "setup.cfg still carries setuptools build configuration:\n  "
        + "\n  ".join(found)
        + "\n  Nothing in the delivered build reads these sections; open the "
        "file and decide whether they are leftovers or a second build path"
    )


def test_no_legacy_shim_on_the_import_path(repo):
    """`.pth` files run at interpreter start-up, before any test does.

    A `.pth` in the payload is executed by every Python that imports the package,
    which makes it the shortest path from "the build does not produce X" to "X is
    produced anyway".  There are none in State A.
    """
    hits = sorted(str(p.relative_to(repo)) for p in repo.rglob("*.pth"))
    assert not hits, (
        f"a .pth file is in the tree: {', '.join(hits)} -- it runs on every "
        "interpreter start, so read it before believing what the package imports"
    )
