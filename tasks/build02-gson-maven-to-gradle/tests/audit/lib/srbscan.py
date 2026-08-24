"""Fixtures and helpers for the read-only scan.

Deliberately small. A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, hash bytes, parse a POM or a Gradle
script as text. It cannot build or run the submission, and there is nothing here
that would let it -- the stage-1 image fails its own build if `mvn`, `gradle`,
`javac` or `java` is on PATH, so a module that tried would die on a missing binary
rather than quietly grading a build log.

Nothing here is scored, and the reason is that these checks are not one kind of
thing. Some are measurements: a sha256 over
`gson/src/main/java/com/google/gson/Gson.java` is not a matter of opinion, and a
submission that edited that file to make its build script work has changed the
thing under test.

Others only look like measurements. Whether a Gradle settings script declares four
subprojects is a question about a build, and a regex over the settings text --

    re.search(r'''["':]gson-extras['"\\s]''', settings_text)

-- passes on a comment, passes on the string in an unrelated task, and fails on a
perfectly valid `include(":extras")` plus a `project(":extras").name` rename.
Whether the OSGi manifest is generated is the same shape: a grep of the build
scripts for `Bundle-SymbolicName` passes a submission that checked in a
hand-written MANIFEST.MF outright, since the string is in the manifest it copied.
Those are a reviewer's questions asked by a regex, so they run here as advisory
findings addressed to a reviewer who can open the file, and the properties
themselves are measured from the outside in stage 2.

Nothing here gates on its own
-----------------------------
`swerefactor.scan` sets `required = False` on every check it emits and
`scoring.grade_audit` gates on the required checks, which are the six prose
gates in evaluation.toml and only those. That is uniform on purpose: a suite where
some mechanical observations gate and others do not is one where the next person to
add a regex has to guess which kind theirs is, and guesses wrong in the direction
that costs a correct submission its score.

It costs nothing, because the findings are addressed to a reviewer who can act on
them. Two shapes:

  - **Certain.** A sha256 mismatch against the tree mounted at /opt/original, or a
    build file naming `SRB_TARGET_NAME` / `SRB_TARGET_ROLE`. The prompt says so in
    as many words: open the file, confirm it, fail the gate. The finding carries
    the path and the line, so confirming it is one read.

  - **A lead.** Everything whose meaning depends on what the file says rather than
    what its name is. A build script mentioning `maven` in `mavenLocal()` is
    required by this task; a build script that parses the retired POMs is a
    finding. The scan says where to look; the review, which has both trees open,
    says what it means.

XML and Gradle without a parser dependency
------------------------------------------
`xml.etree.ElementTree` is in the standard library and the POMs are small, so POM
inspection here is stdlib only. Gradle scripts -- Groovy or Kotlin -- are read as
text, deliberately: there is no Gradle in this image to ask for a model, and a
half-parser for two dialects would produce findings whose meaning depended on which
dialect it guessed. Text plus a citation is honest about what it saw.
"""

from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

#: Files and directories that identify a Gson checkout, used to resolve the mount.
#: All three are State A content the migration does not touch -- `gson/src` holds
#: 202 of the 241 frozen files, `LICENSE` is frozen, and `examples/` is delivered
#: sample content -- so a submission cannot move the root out from under the scan
#: by deleting them.
ROOT_MARKERS = ("gson", "LICENSE", "examples")


def _resolve(path: Path) -> Path:
    """``path``, or its single child, whichever is the repository root.

    This exists because `original.tar.gz` unpacks with a `repo/` prefix in some of
    this benchmark's tasks and without one in others, so whether `/opt/original`
    *is* the tree or *contains* it depends on how the operator unpacked it. Getting
    that wrong is not a visible error: a scan pointed one level off walks a
    directory holding one entry, finds no `gson/`, derives an empty manifest, and
    reports a clean tree. Resolving it here costs a stat and removes the whole
    failure mode.

    Only one level, and only when the level below looks like the repository. A tree
    that matches nothing is returned unchanged so the check that says "this mount is
    wrong" is the one that reports it.
    """
    if not path.is_dir():
        return path
    if any((path / m).exists() for m in ROOT_MARKERS):
        return path
    children = [c for c in path.iterdir() if c.is_dir()]
    if len(children) == 1 and any((children[0] / m).exists() for m in ROOT_MARKERS):
        return children[0]
    return path


REPO = _resolve(Path(os.environ.get("SRB_REPO", "/opt/workspace")))
ORIGINAL = _resolve(Path(os.environ.get("SRB_ORIGINAL", "/opt/original")))
SUITE = Path(os.environ.get("SRB_SUITE_DIR", "/tests/audit"))

#: Directories no scan should walk into. `.git` should not be in a submission at
#: all, but a scan that reported every object in a stray one would bury its own
#: findings; the review is told to look for it separately. `.gradle` and `build`
#: are Gradle's own output and are excluded from the artifact collection anyway,
#: so anything found in them is debris rather than a submission.
EXEMPT_DIRS = (".git", ".svn", ".hg", "__pycache__", ".pytest_cache", ".mvn",
               "node_modules", ".idea", ".settings", ".gradle", "build",
               "target", "out")

#: Prose and binary. A migration is documented in Markdown and Gson ships a PNG,
#: a ZIP of benchmark data and a proguard config; none is a place to look for a
#: live Maven dependency.
EXEMPT_SUFFIXES = (".md", ".markdown", ".rst", ".html", ".png", ".jpg", ".jpeg",
                   ".gif", ".ico", ".svg", ".pdf", ".gz", ".bz2", ".xz", ".zip",
                   ".jar", ".class", ".ttf", ".woff", ".woff2")

#: Suffixes a text scan will open at all. The empty string catches `LICENSE`,
#: `gradlew` and friends.
TEXT_SUFFIXES = (".java", ".xml", ".gradle", ".kts", ".properties", ".txt",
                 ".in", ".sh", ".bash", ".bat", ".py", ".yml", ".yaml", ".json",
                 ".toml", ".cfg", ".ini", ".proto", ".conf", ".bnd", ".mf",
                 ".groovy", ".kt", "")

#: The Gradle build description, and the retired Maven one. A question about *the
#: build* is entitled to read these; a question about it that reads Gson.java is
#: lost.
BUILD_NAMES = ("pom.xml", "settings.xml", "toolchains.xml", "extensions.xml",
               "maven-wrapper.properties",
               "build.gradle", "build.gradle.kts",
               "settings.gradle", "settings.gradle.kts",
               "gradle.properties", "gradle-wrapper.properties",
               "libs.versions.toml", "bnd.bnd", "gradlew", "gradlew.bat")

#: Suffixes that make a file a build script wherever it sits, so a convention
#: plugin under `buildSrc/src/main/groovy/` is read as build code rather than as
#: an immutable source.
BUILD_SUFFIXES = (".gradle", ".gradle.kts", ".bnd")

#: Directory names whose whole contents are build code.
BUILD_DIRS = ("buildSrc", "gradle", "build-logic", ".mvn")

MAVEN_NS = "http://maven.apache.org/POM/4.0.0"


def is_exempt(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXEMPT_DIRS for p in parts):
        return True
    return rel.lower().endswith(EXEMPT_SUFFIXES)


def is_text(path: Path) -> bool:
    """Whether a text scan should open ``path``.

    Case-folded, and that is not a nicety. `Path("MANIFEST.MF").suffix` is `".MF"`,
    so a suffix list written in lowercase skips the single most interesting file a
    submission can check in -- which is how a manifest carrying `Bnd-LastModified`
    sailed past the transcription check the first time this suite was run against a
    deliberately cheating tree. The same hole would have hidden anything in a `.TXT`
    or a `.Gradle`.
    """
    return path.suffix.lower() in TEXT_SUFFIXES


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
            if is_text(path):
                yield path, rel


def build_files(root: Path):
    """The files that describe a build, either build system, as ``(path, rel)``.

    Three ways in, because a Gradle build spreads out in a way a Maven one does
    not: a known filename, a build-script suffix at any depth, or anything under a
    directory whose whole purpose is build code. A convention plugin in
    `buildSrc/src/main/groovy/gson.java-conventions.gradle` is caught by all three
    and a Kotlin one by the last two.
    """
    seen: set[str] = set()
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        rel_dir = os.path.relpath(dirpath, root)
        in_build_dir = rel_dir != "." and rel_dir.split(os.sep)[0] in BUILD_DIRS
        for name in sorted(files):
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if rel in seen:
                continue
            if (name in BUILD_NAMES
                    or name.lower().endswith(BUILD_SUFFIXES)
                    or (in_build_dir and not is_exempt(rel) and is_text(path))):
                seen.add(rel)
                yield path, rel


def poms(root: Path):
    """Every pom.xml under ``root``, as ``(path, rel)``, shallowest first.

    Sorted by depth so a finding in the aggregator is reported before the same
    finding in four children, which reads better in a digest.
    """
    found = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXEMPT_DIRS]
        if "pom.xml" in files:
            path = Path(dirpath) / "pom.xml"
            rel = str(path.relative_to(root))
            found.append((rel.count("/"), rel, path))
    return [(p, rel) for _d, rel, p in sorted(found)]


def gradle_scripts(root: Path):
    """Every Gradle script under ``root``, as ``(path, rel)``, shallowest first.

    The counterpart of ``poms``: what a reviewer opens first when asking what
    produces the four artifacts.
    """
    found = []
    for path, rel in build_files(root):
        if rel.endswith((".gradle", ".gradle.kts")):
            found.append((rel.count("/"), rel, path))
    return [(p, rel) for _d, rel, p in sorted(found)]


def parse_pom(path: Path):
    """A POM's root element, or ``None`` if it does not parse.

    Returning None rather than raising: "this POM is not well-formed XML" is a
    finding a specific check should make with a good message, not a traceback that
    takes the rest of the module's checks down with it.
    """
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None


def q(tag: str) -> str:
    """A tag in Maven's namespace, for ElementTree's find/findall."""
    return f"{{{MAVEN_NS}}}{tag}"


def pom_text(root, *path_parts, default=""):
    """``<a><b>text</b></a>`` by tag path, namespace-agnostic.

    Namespace-agnostic on purpose: a POM that declares no default namespace is
    still a POM Maven builds, and a helper that only found namespaced tags would
    report every check as clean against one.
    """
    node = root
    for part in path_parts:
        if node is None:
            return default
        nxt = node.find(q(part))
        if nxt is None:
            nxt = node.find(part)
        node = nxt
    if node is None or node.text is None:
        return default
    return node.text.strip()


def pom_findall(root, tag: str):
    """Every descendant with ``tag``, namespaced or not."""
    return list(root.iter(q(tag))) + [e for e in root.iter(tag)]


def strip_comments(text: str) -> str:
    """``text`` with `//`, `/* */` and `#` comment bodies blanked, lines kept.

    Line count is preserved so a citation's line number still points at the right
    place. Used by the checks whose question is "does the build *do* this", where a
    comment explaining what the retired POM used to do is not a finding -- the
    single largest source of false leads in the regex suite this replaced.

    A lexer this simple gets a `//` inside a string literal wrong. That is the
    right trade here: the output is a lead with a citation, and over-blanking
    loses a lead while under-blanking manufactures one.
    """
    out = []
    in_block = False
    for line in text.splitlines():
        if in_block:
            end = line.find("*/")
            if end == -1:
                out.append("")
                continue
            line = " " * (end + 2) + line[end + 2:]
            in_block = False
        while True:
            start = line.find("/*")
            if start == -1:
                break
            end = line.find("*/", start + 2)
            if end == -1:
                line = line[:start]
                in_block = True
                break
            line = line[:start] + " " * (end + 2 - start) + line[end + 2:]
        for marker in ("//", "#"):
            idx = line.find(marker)
            if idx != -1:
                line = line[:idx]
        out.append(line)
    return "\n".join(out)


def cite(path: Path, rel: str, token: str, limit: int = 6,
         skip_comments: bool = False) -> list[str]:
    """Every line ``token`` appears on, as ``rel:lineno: text`` citations.

    A finding a reviewer cannot open is not evidence, so no scan check reports a
    bare boolean: it reports where to look. ``limit`` keeps a token that appears
    two hundred times from filling the prompt.

    ``skip_comments`` matches against the comment-blanked text and quotes the
    original line, so the citation reads as written while the match is on code.
    """
    body = read(path)
    haystack = strip_comments(body) if skip_comments else body
    original = body.splitlines()
    out = []
    for n, line in enumerate(haystack.splitlines(), 1):
        if token in line:
            shown = original[n - 1] if n <= len(original) else line
            out.append(f"{rel}:{n}: {shown.strip()[:160]}")
            if len(out) >= limit:
                out.append(f"{rel}: ... more occurrences not listed")
                break
    return out


def cite_re(path: Path, rel: str, pattern, limit: int = 6,
            skip_comments: bool = False) -> list[str]:
    """``cite`` for a compiled regex, for the checks whose token is a shape."""
    rx = re.compile(pattern) if isinstance(pattern, str) else pattern
    body = read(path)
    haystack = strip_comments(body) if skip_comments else body
    original = body.splitlines()
    out = []
    for n, line in enumerate(haystack.splitlines(), 1):
        if rx.search(line):
            shown = original[n - 1] if n <= len(original) else line
            out.append(f"{rel}:{n}: {shown.strip()[:160]}")
            if len(out) >= limit:
                out.append(f"{rel}: ... more occurrences not listed")
                break
    return out


def unchanged(rel: str) -> bool:
    """True when the delivered ``rel`` is byte-identical to State A's.

    The exemption every content-sniffing check needs, and the reason it is a hash
    rather than a path prefix. `OSGiTest.java` -- a frozen test source Gson has
    shipped for years -- asserts on the string `resolution:=optional`, which is one
    of the tokens that identifies a transcribed OSGi manifest. Exempting
    `gson/src/test/` by prefix would suppress that false positive and also stop
    anyone noticing a manifest dropped into `gson/src/main/resources/`. Exempting
    *unmodified files* suppresses exactly the false positive: a file whose bytes are
    State A's cannot be something the submission brought.

    Cheap enough to call per file. The hash is computed on both sides only for paths
    whose sizes agree, so the common case is a stat.
    """
    delivered = REPO / rel
    reference = ORIGINAL / rel
    if not reference.is_file() or not delivered.is_file():
        return False
    try:
        if delivered.stat().st_size != reference.stat().st_size:
            return False
    except OSError:
        return False
    return sha256(delivered) == sha256(reference)


# There is no `data()` helper here, and the absence is deliberate. A frozen
# `inventory.json` copied into this image would be a second file claiming to be
# State A's checksums, and `_shared_input_drift` only compares duplicates whose
# basename appears in `environment/` -- so the two would drift silently. This stage
# has State A mounted at /opt/original and hashes it directly. Stage 2 needs its
# frozen manifest because by the time it runs it holds eight build trees and no
# reference tree; that is its constraint, not one to inherit.


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="session")
def repo() -> Path:
    """The submitted tree, read-only.

    Named `repo` without apology. In stage 2 a fixture handing out the source tree
    is a mistake -- `infra/tests/test_stages.py` fails the task for having one --
    because stage 2 measures a build. Here it is the input.
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
def delivered_build_files(repo):
    return list(build_files(repo))


@pytest.fixture(scope="session")
def delivered_poms(repo):
    return poms(repo)


@pytest.fixture(scope="session")
def delivered_gradle(repo):
    return gradle_scripts(repo)
