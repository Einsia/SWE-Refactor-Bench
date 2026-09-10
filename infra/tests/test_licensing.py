"""NOTICE has to keep telling the truth about twenty archives it does not contain.

Every task ships one upstream release verbatim in ``environment/original.tar.gz``
and the repository redistributes it, so NOTICE at the root states, per task, the
licence and where its text lives.  Typed once, that table is wrong the first time a
task is added, retired or re-pinned -- and wrong invisibly, because nothing reads
it.  So it is re-derived here from the archives on every run.

Three measurements shaped how the matching works, and each one is a way this file
could have passed while being wrong:

* Needles lose to line wraps.  SQLite's blessing breaks across "In place of\\na
  legal notice"; PyCryptodome's Unlicense across "free and unencumbered software\\n
  released into the public domain".  Both read as UNIDENTIFIED until matching moved
  to whitespace-normalised text.
* Needles lose to abbreviations.  cmark's COPYING says "Creative Commons CC-BY-SA
  4.0", so a pattern that wants the words "Attribution" and "ShareAlike" reports it
  clean -- which it did, on a section already read by eye.
* BSD-3-Clause contains BSD-2-Clause.  A fingerprinter that reports every hit calls
  sqlparse both, so the more specific licence has to subsume the weaker one.

What this does not check is fidelity to upstream: that each vendored licence file
is byte-identical to the project's own at the recorded ref.  That needs the
network, and this suite must pass without it, so it lives in
``tools/verify_upstream_licences.py`` -- which imports the parsing below rather
than restating it, and is the thing to run when a task is added or re-pinned.
Last run: 25 of 25 reachable files matched; SQLite and QuickJS are not on GitHub
and are recorded from their archives alone.
"""

from __future__ import annotations

import re
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import tomlcompat

REPO = Path(__file__).resolve().parents[2]
NOTICE = REPO / "NOTICE"
LICENSE = REPO / "LICENSE"
TASKS = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir()) \
    if (REPO / "tasks").is_dir() else []

#: An SPDX id followed by its grant, as a phrase that survives a line wrap.  Order
#: matters only for readability; subsumption below decides which hits survive.
GRANTS: list[tuple[str, str]] = [
    ("Apache-2.0", "Licensed under the Apache License, Version 2.0"),
    ("Apache-2.0", "Apache License Version 2.0, January 2004"),
    ("ISC", "Permission to use, copy, modify, and/or distribute this software"),
    ("MIT", "Permission is hereby granted, free of charge"),
    ("BSD-3-Clause", "Neither the name of"),
    ("BSD-2-Clause", "Redistributions in binary form must reproduce"),
    ("Zlib", "altered source versions must be plainly marked"),
    ("Unlicense", "free and unencumbered software released into the public domain"),
    ("CC-BY-SA-4.0", "CC-BY-SA 4.0"),
    ("blessing", "In place of a legal notice, here is a blessing"),
]

#: A licence whose text contains another's needle.  Reporting both is not wrong so
#: much as useless: it says "this file is BSD-3 and also the BSD-2 inside it".
SUBSUMES = {"BSD-3-Clause": {"BSD-2-Clause"}}

#: Where a release states its terms in source headers rather than in a file of its
#: own, with the header that carries them.  QuickJS publishes no licence file at
#: all; NOTICE says so and points at this line instead of naming a missing path.
#: Paths here, and in NOTICE, are relative to the archive's single root directory
#: -- `quickjs.h`, not `repo/quickjs.h` -- because that is the path a reader sees
#: once the archive is unpacked into a tree of their own.
HEADER_ONLY = {
    "pf03-quickjs-byteorder-port": ("quickjs.h", "MIT"),
}


def flat(text: str) -> str:
    """Whitespace-normalised, for matching a phrase that may be wrapped."""
    return " ".join(text.split())


def identify(text: str) -> set[str]:
    """Every SPDX id whose grant appears in ``text``, minus the subsumed ones."""
    hay = flat(text).lower()
    hits = {spdx for spdx, needle in GRANTS if flat(needle).lower() in hay}
    for spdx, weaker in SUBSUMES.items():
        if spdx in hits:
            hits -= weaker
    return hits


def tar_root(names: list[str]) -> str:
    """The archive's single top-level directory, or "" when it is flat.

    Not ``names[0].split("/")[0]``: an archive whose first member is a file --
    build01's AUTHORS, lang03's .flake8 -- would name that file as the root.
    """
    if not names:
        return ""
    first = names[0].split("/")[0]
    if all(n == first or n.startswith(first + "/") for n in names):
        if any(n.startswith(first + "/") for n in names):
            return first
    return ""


def archive_files(task: Path, wanted: set[str]) -> dict[str, str]:
    """Read exactly the named paths out of the task's archive, root stripped."""
    env = task / "environment" / "original.tar.gz"
    out: dict[str, str] = {}
    with tarfile.open(env) as tar:
        members = [m for m in tar.getmembers() if m.isfile()]
        root = tar_root([m.name for m in tar.getmembers()])
        for m in members:
            rel = m.name[len(root) + 1:] if root and m.name.startswith(root + "/") \
                else m.name
            if rel not in wanted:
                continue
            fh = tar.extractfile(m)
            if fh is not None:
                out[rel] = fh.read().decode("utf-8", "replace")
    return out


# --------------------------------------------------------------------------- #
# parsing NOTICE's own table
# --------------------------------------------------------------------------- #

#: A task entry: the name flush left, then indented lines until the next one.
ENTRY = re.compile(r"^(?P<task>[a-z]+\d\d-[a-z0-9-]+)\n"
                   r"(?P<body>(?:[ \t]+\S.*\n|\n(?=[ \t]))*)", re.M)
LICENCE_LINE = re.compile(r"^\s*Licence text:\s*(?P<rest>.*)$", re.M)
SPDX_LINE = re.compile(r"^\s{4}(?P<expr>[A-Za-z0-9.+-]+(?: (?:AND|OR) "
                       r"[A-Za-z0-9.+-]+)*)\.\s", re.M)


def notice_entries() -> dict[str, dict[str, object]]:
    """task -> {"spdx": set, "expr": str, "paths": [str], "body": str}."""
    text = NOTICE.read_text()
    out: dict[str, dict[str, object]] = {}
    for m in ENTRY.finditer(text):
        body = m.group("body")
        spdx_m = SPDX_LINE.search(body)
        expr = spdx_m.group("expr") if spdx_m else ""
        lic = LICENCE_LINE.search(body)
        paths: list[str] = []
        if lic:
            rest = lic.group("rest")
            # "LICENSE, gson/LICENSE" -- but also prose like "no standalone
            # licence file.  Upstream states ...", which names no path.
            if not rest.lower().startswith("no standalone"):
                head = re.split(r"\.\s|,\s*and the project", rest)[0]
                paths = [p.strip() for p in head.split(",")
                         if p.strip() and " " not in p.strip()]
        out[m.group("task")] = {
            "spdx": {t for t in re.split(r" AND | OR ", expr) if t},
            "expr": expr, "paths": paths, "body": body,
        }
    return out


# --------------------------------------------------------------------------- #
# the repository's own licensing
# --------------------------------------------------------------------------- #


def test_the_repository_carries_a_licence_and_a_notice():
    """A benchmark that asks a submission about licensing ships its own.

    This test is about the repository's own licensing rather than any submission's,
    which is why it asserts on the root files and not on any task's gates: the
    obligation is the benchmark's, and it holds whatever the tasks ask.
    """
    assert LICENSE.is_file(), "no LICENSE at the repository root"
    assert NOTICE.is_file(), "no NOTICE at the repository root"
    assert "Apache-2.0" in identify(LICENSE.read_text())


def test_the_notice_points_at_the_licence_rather_than_restating_it():
    head = NOTICE.read_text()[:600]
    assert "LICENSE" in head
    assert "Apache License" in head


# --------------------------------------------------------------------------- #
# every task appears, and says what the archive says
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
def test_every_task_has_a_notice_entry_and_every_entry_a_task():
    """The failure this exists for: a task added, NOTICE not touched."""
    entries = set(notice_entries())
    shipped = {t.name for t in TASKS}
    assert shipped - entries == set(), \
        f"redistributed with no NOTICE entry: {sorted(shipped - entries)}"
    assert entries - shipped == set(), \
        f"NOTICE names tasks that are gone: {sorted(entries - shipped)}"


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_the_notice_licence_paths_exist_in_the_archive(task: Path):
    entry = notice_entries().get(task.name)
    assert entry is not None
    paths = list(entry["paths"])
    if task.name in HEADER_ONLY:
        header, _ = HEADER_ONLY[task.name]
        assert not paths, f"{task.name} ships no licence file; NOTICE names {paths}"
        assert archive_files(task, {header}), f"{header} missing from the archive"
        return
    assert paths, f"NOTICE names no licence file for {task.name}"
    have = archive_files(task, set(paths))
    assert set(paths) == set(have), \
        f"NOTICE names {sorted(set(paths) - set(have))}, not in the archive"


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_the_declared_licence_is_the_one_the_archive_grants(task: Path):
    """task.toml's `license` against the grants actually present in the files.

    This is what caught two wrong declarations: build03 claimed Apache-2.0 from a
    stray setup.py classifier no file in the tree backs, and lang01 claimed only
    BSD-2-Clause while shipping MIT sections and a CC-BY-SA-4.0 specification.
    """
    entry = notice_entries().get(task.name)
    assert entry is not None
    declared = tomlcompat.load(task / "task.toml").get("metadata", {}).get("license")
    assert declared, f"{task.name} declares no [metadata].license"
    assert declared == entry["expr"], \
        f"task.toml says {declared!r}, NOTICE says {entry['expr']!r}"

    if task.name in HEADER_ONLY:
        header, spdx = HEADER_ONLY[task.name]
        body = archive_files(task, {header})[header]
        assert identify(body) == {spdx}, \
            f"{header} grants {sorted(identify(body))}, not {{{spdx}}}"
        return

    granted: set[str] = set()
    for body in archive_files(task, set(entry["paths"])).values():
        granted |= identify(body)
    for spdx in SUBSUMES:
        if spdx in granted:
            granted -= SUBSUMES[spdx]
    # Set equality, not containment, and the difference is the whole point.  A
    # subset check ("is everything declared also granted?") catches a licence
    # claimed out of thin air -- build03's Apache-2.0 -- and passes one that is
    # simply left out, which is the more likely error and the more expensive: it
    # was lang01's, whose declaration named BSD-2-Clause while the archive also
    # carried MIT sections and a CC-BY-SA-4.0 specification.  Under-declaring a
    # share-alike licence is exactly what a redistributor must not do.
    assert granted == set(entry["spdx"]), \
        f"{task.name}: NOTICE and task.toml say {sorted(entry['spdx'])}, the " \
        f"licence files grant {sorted(granted)}"


@pytest.mark.skipif(not TASKS, reason="no tasks/ tree beside infra/")
@pytest.mark.parametrize("task", TASKS, ids=lambda p: p.name)
def test_the_notice_names_the_version_the_task_ships(task: Path):
    meta = tomlcompat.load(task / "task.toml").get("metadata", {})
    entry = notice_entries().get(task.name)
    assert entry is not None
    body = flat(str(entry["body"]))
    version = str(meta.get("source_version") or meta.get("upstream_version") or "")
    if version:
        assert version.lstrip("v") in body or version in body, \
            f"NOTICE's {task.name} entry does not name {version}"
        return
    # fw03 pins a commit rather than a version: upstream tagged no release, so
    # there is no version to name and the commit is the only identifier there is.
    commit = str(meta.get("source_commit") or meta.get("upstream_commit") or "")
    assert commit, f"{task.name} declares neither a version nor a commit"
    assert commit[:8] in body, \
        f"NOTICE's {task.name} entry names neither a version nor {commit[:8]}"


# --------------------------------------------------------------------------- #
# the disclosures a redistributor needs
# --------------------------------------------------------------------------- #

#: Files inside a release whose terms differ from the release's own, found by
#: walking every member of every archive.  Each is named in NOTICE because a
#: share-alike or weak-copyleft file cannot be redistributed silently.
EMBEDDED = [
    ("fw07-graphhopper-dropwizard-to-springboot",
     "core/src/main/java/com/graphhopper/isochrone/algorithm/ContourBuilder.java",
     "GNU Lesser General Public"),
    ("fw07-graphhopper-dropwizard-to-springboot",
     "core/src/main/resources/com/graphhopper/routing/util/legal_default_speeds.json",
     "Attribution-ShareAlike"),
    ("lang01-cmark-c-to-rust", "test/spec.txt", "CC-BY-SA"),
]


@pytest.mark.parametrize("task,path,needle", EMBEDDED,
                         ids=lambda v: str(v).rsplit("/", 1)[-1])
def test_a_differently_licensed_file_is_still_there_and_still_disclosed(
        task: str, path: str, needle: str):
    """Both halves matter: the file may move, and NOTICE may stop naming it."""
    root = REPO / "tasks" / task
    if not root.is_dir():
        pytest.skip(f"{task} is not in this tree")
    body = archive_files(root, {path}).get(path)
    assert body is not None, f"{path} is no longer in {task}'s archive"
    assert flat(needle).lower() in flat(body).lower(), \
        f"{path} no longer states {needle}"
    assert path in NOTICE.read_text(), f"NOTICE does not name {path}"


def test_the_notice_reproduces_the_upstream_notices_it_must_carry():
    """Apache-2.0 4(d): a NOTICE inside a redistributed Work travels with it."""
    text = NOTICE.read_text()
    goyaml = archive_files(REPO / "tasks/lang05-goyaml-go-to-zig", {"NOTICE"})
    assert goyaml, "lang05's upstream NOTICE is gone from the archive"
    for line in goyaml["NOTICE"].splitlines():
        if line.strip():
            assert flat(line) in flat(text), \
                f"lang05's NOTICE line is not carried: {line.strip()[:60]!r}"
    fw07 = archive_files(REPO / "tasks/fw07-graphhopper-dropwizard-to-springboot",
                         {"NOTICE.md"})
    assert fw07, "fw07's upstream NOTICE.md is gone from the archive"
    holder = [l for l in fw07["NOTICE.md"].splitlines() if "Copyright" in l][:1]
    assert holder and flat(holder[0]) in flat(text), \
        "fw07's copyright line is not carried into NOTICE"
