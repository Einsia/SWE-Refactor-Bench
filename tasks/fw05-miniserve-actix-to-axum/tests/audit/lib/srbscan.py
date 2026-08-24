"""Fixtures and helpers for the read-only scan.

Deliberately small.  A scan module gets two trees and a text editor's worth of
facility: open a file, walk a directory, split Rust into code and comments, read
a manifest, read a lock file.  It cannot build, install, start or link the
submission, and there is nothing here that would let it -- the stage-1 image has
no Rust toolchain and no crate registry, so a module that tried would fail on the
missing ``cargo`` rather than quietly grading a build.

The one thing this file does that fw01's equivalent does not is separate Rust
*code* from Rust *comments*, and it is the most load-bearing thing in it.  On this
subject the retired dependency is named legitimately in the tree in several
places, and one of them is a doc comment two lines above a function that a correct
migration rewrites but does not have to stop citing::

    /// Adapted from https://docs.rs/actix-web/0.7.13/src/actix_web/fs.rs.html#564
    pub fn directory_listing(

A scan that reported ``src/listing.rs`` names actix would be right about the
string and useless about the question.  A scan that reported *actix is named in a
comment on line 155 and nowhere in the code of this file* is telling the reviewer
something true and worth knowing, and pointing at the line that settles it.  So
``code_of`` and ``comments_of`` exist, every text check says which half it looked
in, and the module that scans prose says so in its own name.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO = Path(os.environ.get("SRB_REPO", "/opt/workspace"))
ORIGINAL = Path(os.environ.get("SRB_ORIGINAL", "/opt/original"))

#: Prose and binary assets.  A migration is documented in Markdown, ships a logo
#: and a screenshot, and styles itself with SCSS; none of those is a place to look
#: for a live dependency.  ``instruction.md`` publishes this list to the agent,
#: which is why it is here rather than repeated in each module: a scan that
#: searched ``data/`` for the word actix would flag the shipped assets of a
#: correct migration.
EXEMPT_SUFFIXES = (".md", ".scss", ".css", ".svg", ".png", ".jpg", ".jpeg",
                   ".webp", ".ico", ".woff", ".woff2", ".ttf", ".map", ".gz",
                   ".tar", ".zip", ".pem", ".crate")
EXEMPT_DIRS = ("data", "screenshots", ".git", "target", "vendor-doc",
               "node_modules", ".github/ISSUE_TEMPLATE")

#: Suffixes a text scan will open at all.  The empty string catches the
#: extensionless files this project ships that matter -- ``Makefile``,
#: ``Containerfile``, ``Containerfile.alpine`` is caught by its suffix rule below.
TEXT_SUFFIXES = (".rs", ".toml", ".lock", ".yml", ".yaml", ".sh", ".bash",
                 ".json", ".service", ".cfg", ".ini", ".mk", "")

#: Files with no useful suffix that are still worth reading.
TEXT_NAMES = ("Makefile", "Containerfile", "Containerfile.alpine", "Dockerfile",
              "rustfmt.toml", "release.toml", ".dockerignore", ".gitignore")


# ---------------------------------------------------------------------------
# Walking the trees
# ---------------------------------------------------------------------------

def is_exempt(rel: str) -> bool:
    if any(rel == d or rel.startswith(d + "/") for d in EXEMPT_DIRS):
        return True
    return rel.endswith(EXEMPT_SUFFIXES)


def read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def source_files(repo: Path) -> Iterator[tuple[Path, str]]:
    """Every text file worth reading, as ``(path, repo-relative string)``."""
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if is_exempt(rel):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            yield path, rel


def rust_files(repo: Path) -> Iterator[tuple[Path, str]]:
    """Every ``.rs`` file, including ``build.rs`` and the ported test suite."""
    for path, rel in source_files(repo):
        if path.suffix == ".rs":
            yield path, rel


def crate_sources(repo: Path) -> Iterator[tuple[Path, str]]:
    """The ``.rs`` files that end up in the binary: ``src/`` and ``build.rs``.

    Separate from :func:`rust_files` because ``tests/`` is a different question.
    The ported integration tests may legitimately name things the shipped binary
    must not -- a test asserting the *absence* of an old behaviour is the obvious
    case -- and a finding that cannot say which side of that line it is on is a
    finding the reviewer has to re-derive from scratch.
    """
    for path, rel in rust_files(repo):
        if rel == "build.rs" or rel.startswith("src/"):
            yield path, rel


# ---------------------------------------------------------------------------
# Rust: code vs comments
# ---------------------------------------------------------------------------

_LINE_COMMENT = re.compile(r"//.*?$", re.MULTILINE)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def _blank_matches(text: str, pattern: re.Pattern[str]) -> str:
    """Replace every match with same-length whitespace, newlines preserved.

    Line numbers have to survive this, because a citation with the wrong line
    number is discarded by the grounding check and the reviewer is told nothing.
    """
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in match.group(0))
    return pattern.sub(blank, text)


def code_of(text: str) -> str:
    """``text`` with comments blanked out, line numbering intact.

    Not a Rust parser: a ``//`` inside a string literal is blanked as though it
    began a comment.  That direction is the safe one for a scan whose output is
    advisory -- it can lose a lead, and every lead it keeps points at real code.
    Blanking rather than deleting is what lets :func:`locate` still be right.
    """
    return _blank_matches(_blank_matches(text, _BLOCK_COMMENT), _LINE_COMMENT)


_TEST_MOD = re.compile(r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]\s*(?:pub\s+)?mod\s+\w+\s*\{")


def strip_test_modules(text: str) -> str:
    """Blank out ``#[cfg(test)] mod ... { ... }`` blocks, line numbering intact.

    Rust keeps unit tests in the file they test, which makes the obvious
    projection wrong in a way that only shows up on a correct submission.
    ``src/renderer.rs`` ends in a ``#[cfg(test)] mod tests`` whose cases pass
    ``"-P '127.0.0.1:420'"`` to a helper, and a check looking for a hardcoded
    upstream address reported it -- a literal in a unit test, in a file that ships
    no such literal to any request.

    The braces are matched by counting, not by regex, and string literals and
    comments are excluded from the count first so that a ``{`` inside either does
    not close the block early.  An unbalanced block (a truncated file) is left
    alone rather than swallowing the rest of the tree.
    """
    out = text
    while True:
        match = _TEST_MOD.search(out)
        if not match:
            return out
        # Count braces over the comment-free projection so a `{` in a comment or
        # a string does not move the end of the block.
        neutral = _blank_matches(_blank_matches(out, _BLOCK_COMMENT),
                                 _LINE_COMMENT)
        neutral = re.sub(r'"(?:[^"\\]|\\.)*"', lambda m: " " * len(m.group(0)),
                         neutral)
        depth = 0
        end = None
        for index in range(match.end() - 1, len(neutral)):
            char = neutral[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            return out  # unbalanced; leave the file as it is
        blanked = "".join("\n" if ch == "\n" else " "
                          for ch in out[match.start():end])
        out = out[:match.start()] + blanked + out[end:]


def shipped_code_of(text: str) -> str:
    """The projection that answers "what does this program do to a request".

    Comments gone, unit-test modules gone.  Every check about the request path
    reads this rather than :func:`code_of`, because the two things it removes are
    exactly the two places a name can appear in a ``src/`` file without the
    shipped binary ever acting on it.
    """
    return strip_test_modules(code_of(text))


def comments_of(text: str) -> str:
    """The inverse: only the comments, line numbering intact."""
    kept: list[str] = []
    code = code_of(text)
    for code_line, raw_line in zip(code.splitlines(), text.splitlines()):
        # Whatever `code_of` blanked is what was commentary.
        kept.append("".join(
            raw if code == " " and raw != " " else " "
            for code, raw in zip(code_line.ljust(len(raw_line)), raw_line)
        ))
    return "\n".join(kept)


def uses(path: Path) -> set[str]:
    """Crate names a Rust file refers to, by the paths it writes.

    Comments are stripped first, so the ``actix-web`` cited in a doc comment does
    not count as a dependency, and both spellings of a reference are counted: the
    ``use`` declaration at the top of the file, and the inline ``actix_web::...``
    path that needs no ``use`` at all.  Underscores are folded to hyphens so the
    result is comparable with what a manifest declares.

    A grouped ``use`` -- ``use actix_web::{dev::fn_service, web, App}`` -- yields
    the crate once, which is what the question is about.  ``crate::``, ``self::``
    and ``super::`` are internal and are not reported.
    """
    code = code_of(read(path))
    names: set[str] = set()
    for match in re.finditer(r"\b([a-z][a-z0-9_]{2,})\s*::", code):
        name = match.group(1)
        if name in ("crate", "self", "super", "std", "core", "alloc"):
            continue
        names.add(name.replace("_", "-"))
    for match in re.finditer(r"^\s*(?:pub\s+)?use\s+(?:::)?([a-z][a-z0-9_]*)",
                             code, re.MULTILINE):
        name = match.group(1)
        if name not in ("crate", "self", "super", "std"):
            names.add(name.replace("_", "-"))
    return names


def locate(path: Path, needle: str, *, haystack: str | None = None,
           ignore_case: bool = False) -> int | None:
    """The 1-based line ``needle`` first appears on, for a citation.

    ``haystack`` lets a caller search the code-only or comment-only projection
    and still get a line number that is right about the file on disk, which is
    what the grounding check re-opens.

    ``ignore_case`` has to be passed by any caller that decided *whether* to
    report using a case-folded search, and it exists because forgetting it is
    silent.  ``src/main.rs:306`` reads ``/// Configures the Actix application``:
    a check that tested ``"actix" in text.lower()`` and then located ``"actix"``
    found the mention, failed to find the line, and cited the file with no line
    number at all.  The finding was still true and had become unusable, which is
    the one outcome :func:`cite` is written to prevent.
    """
    text = haystack if haystack is not None else read(path)
    if ignore_case:
        needle = needle.lower()
    for lineno, line in enumerate(text.splitlines(), 1):
        if needle in (line.lower() if ignore_case else line):
            return lineno
    return None


def quote_at(path: Path, lineno: int | None) -> str:
    """The real text of a line, for a citation the grounding check will accept."""
    if not lineno:
        return ""
    lines = read(path).splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()
    return ""


def cite(path: Path, rel: str, needle: str, *,
         haystack: str | None = None, ignore_case: bool = False) -> str:
    """``path:line  'the line'`` -- one lead, in the form the reviewer needs.

    Every finding this scan produces travels to the reviewer as text.  A message
    that names a file without a line makes the reviewer re-run the search; a
    message with the line and the text of it is a place to look, which is the most
    a mechanical observation is ever worth.

    How it actually arrives is worth knowing before rewriting one of these
    assertions.  ``scan.digest`` renders a structured evidence list if there is
    one, and ``swerefactor.pytest_module`` never writes one -- it records only
    ``summary`` (the last ``E`` line, 300 chars) and ``detail`` (the tail of the
    longrepr, cut to 240 by the digest).  The citations survive anyway, and by
    pytest's doing rather than mine: every check here is spelled
    ``assert not offenders, (prose)``, so pytest's assertion rewriting renders
    ``assert not {...}`` with the dict repr in it, and the dict is the citations.
    The reviewer reads them on the head line and the prose underneath, truncated.

    That is why the shape of the assertion matters.  Rewriting one as
    ``if offenders: pytest.fail(prose)`` would make the summary the prose's last
    line, and the citations -- which are at the end of the prose, in the dump --
    would be the part that got cut.  Keep the collection in the assert expression.

    The line number is always resolved against the file on disk, even when the
    search ran over the code-only or comment-only projection, because that is the
    number a reader will open.

    ``ignore_case`` must match how the caller decided to report; see
    :func:`locate` for what happens when it does not.
    """
    lineno = locate(path, needle, haystack=haystack, ignore_case=ignore_case)
    quote = quote_at(path, lineno)
    where = f"{rel}:{lineno}" if lineno else rel
    return f"{where}  {quote!r}" if quote else where


# ---------------------------------------------------------------------------
# Cargo
# ---------------------------------------------------------------------------

def manifest(repo: Path) -> dict[str, Any]:
    """``Cargo.toml`` as a dict, or ``{}`` if it is missing or unparseable.

    Returning ``{}`` rather than raising is deliberate: a submission with a
    broken manifest is a real thing that can arrive here, and every check that
    reads the manifest reporting "could not parse" separately is more useful to
    the reviewer than one erroring module and twelve that never ran.
    ``test_manifest_parses`` is the check that says so once.
    """
    from swerefactor import tomlcompat
    path = repo / "Cargo.toml"
    if not path.is_file():
        return {}
    try:
        return tomlcompat.loads(read(path))
    except Exception:
        return {}


def _dep_entries(repo: Path):
    """Walk all four dependency tables once, yielding (key, spec, label).

    ``dependencies``, ``dev-dependencies``, ``build-dependencies`` and everything
    under ``target.*`` -- because a retired crate reachable only as a
    dev-dependency is still a retired crate in the tree, and is worth a different
    sentence in the review than one in the binary.
    """
    def walk(table: Any, label: str):
        if isinstance(table, dict):
            for key, spec in table.items():
                yield key, spec, label

    data = manifest(repo)
    for key in ("dependencies", "dev-dependencies", "build-dependencies"):
        yield from walk(data.get(key), key)
    for triple, table in (data.get("target") or {}).items():
        if isinstance(table, dict):
            for key in ("dependencies", "dev-dependencies",
                        "build-dependencies"):
                yield from walk(table.get(key), f"target.{triple}.{key}")


def _crate_of(key: str, spec: Any) -> str:
    """The crate a dependency entry actually names.

    A manifest key is a *local alias*, not a crate name: `web = { package =
    "actix-web" }` declares actix-web under the name `web`.  Keying on the manifest
    key is therefore wrong in both directions, and this task exercises both:

      * the brief REQUIRES one alias -- `http1 = { package = "http", version =
        "=1.2.0" }`, because two `http` majors have to coexist -- and Cargo.lock
        records the crate, `http`.  Keyed on the alias,
        `test_lock_file_is_present_and_covers_the_manifest` reports `['http1']`
        missing from the lock file on every submission that followed the
        instruction, and hands the reviewer a note saying the manifest is not
        covered by its lock file about a tree that has just built `--offline
        --locked`.
      * `web = { package = "actix-web", version = "4" }` puts nothing named
        actix-web among the keys, so `test_manifest_no_longer_declares[actix-web]`
        and `test_no_retired_crate_is_declared_anywhere` both pass on a manifest
        declaring the retired stack.
    """
    if isinstance(spec, dict):
        renamed = spec.get("package")
        if isinstance(renamed, str) and renamed:
            return renamed
    return key


def declared_deps(repo: Path) -> dict[str, str]:
    """Every crate the manifest declares, to the requirement as written.

    Keyed on the CRATE, resolving ``package = "..."`` renames, so that a name in
    this mapping is a name cargo will resolve and a name Cargo.lock will carry.
    The value is a display string, not a parsed requirement: this is evidence for
    a reader, not input to a resolver -- so when the key came from an alias the
    alias is shown too, because "actix-web is declared" and "actix-web is declared
    under the name `web`" are different sentences in a review.
    """
    out: dict[str, str] = {}
    for key, spec, label in _dep_entries(repo):
        if isinstance(spec, dict):
            shown = spec.get("version") or spec.get("path") or \
                spec.get("git") or "(no version)"
            if spec.get("optional"):
                shown = f"{shown}, optional"
        else:
            shown = str(spec)
        crate = _crate_of(key, spec)
        if crate != key:
            shown = f"{shown}, as `{key}`"
        out[crate] = f"{shown} [{label}]"
    return out


def _compat_key(spec: Any) -> str:
    """The semver compatibility range a requirement selects, as a string.

    Cargo unifies two keys for one crate only when they resolve to the SAME
    package, and semver-incompatible majors are different packages.  So `0.2` and
    `=1.2.0` are two distinct `http` crates and coexist happily, while `=1.5.2`
    twice is one crate reached by two names.  Under 1.0 the minor carries
    compatibility, hence `0.2` rather than `0`.  A path or git dependency has no
    comparable range: it gets its own key so it is never merged with a version.
    """
    if isinstance(spec, dict):
        if spec.get("path") or spec.get("git"):
            return f"unversioned:{spec.get('path') or spec.get('git')}"
        req = spec.get("version")
    else:
        req = spec
    if not isinstance(req, str) or not req.strip():
        return "unknown"
    head = req.split(",")[0].strip().lstrip("=^~><* ").strip()
    parts = [p for p in head.split(".") if p]
    if not parts:
        return "unknown"
    if parts[0] == "0":
        return ".".join(parts[:2]) if len(parts) > 1 else "0"
    return parts[0]


def declared_aliases(repo: Path) -> dict[str, list[str]]:
    """Crates reached by two different keys in one table, crate to those keys.

    Separate from ``declared_deps`` because that mapping is keyed on the crate and
    so collapses exactly the case this reports.  The target manifest documents the
    trap: a manifest carrying both keys of one of its aliased pairs "resolves and
    locks without complaint and then fails at build with `depends on crate ...
    multiple times with different names`" -- invisible to every check that reads
    only the manifest or only the lock file, and landing instead as a required
    ``build.compiles`` failure that names cargo's error and not the paste.

    Two guards, both measured against trees that build:

      * the same compatibility range, per ``_compat_key``.  A submission may
        declare `http = "0.2"` and `http1 = { package = "http", version =
        "=1.2.0" }` side by side in ``[dependencies]`` -- the brief requires the
        second -- and compile 7/7.  A check keyed on the crate alone flags that
        tree, which is the false positive this helper removes.
      * one table at a time.  The same key in ``[dependencies]`` and
        ``[dev-dependencies]`` is ordinary and correct, `regex = "1"` being the
        usual example.
    """
    seen: dict[tuple[str, str, str], list[str]] = {}
    for key, spec, label in _dep_entries(repo):
        seen.setdefault((label, _crate_of(key, spec), _compat_key(spec)),
                        []).append(key)
    out: dict[str, list[str]] = {}
    for (_label, crate, _compat), keys in seen.items():
        if len(set(keys)) > 1:
            out.setdefault(crate, []).extend(sorted(set(keys)))
    return {crate: sorted(set(keys)) for crate, keys in out.items()}


def features(repo: Path) -> dict[str, list[str]]:
    """The ``[features]`` table, names to what each enables."""
    table = manifest(repo).get("features")
    if not isinstance(table, dict):
        return {}
    return {name: [str(v) for v in vals] if isinstance(vals, list) else []
            for name, vals in table.items()}


def locked_packages(repo: Path) -> dict[str, str]:
    """``Cargo.lock``'s package list, name to version.

    Read with a line scanner rather than a TOML parser.  The lock file is
    generated, it is large, and a submission that hand-edited it into something
    that no longer parses is exactly the case where this check has to still
    produce a finding instead of an error.
    """
    path = repo / "Cargo.lock"
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    name: str | None = None
    for line in read(path).splitlines():
        line = line.strip()
        if line == "[[package]]":
            name = None
        elif line.startswith("name = "):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("version = ") and name:
            out[name] = line.split("=", 1)[1].strip().strip('"')
            name = None
    return out


def retired_names(path: Path | None = None) -> list[str]:
    """The retired crate list, read from the file the registry was built from.

    The scan does not keep its own copy.  The environment image, the behavioural
    image and this scan all have to mean the same thing by "retired", and the
    way to guarantee that is one file -- so ``retired-crates.txt`` is copied into
    this image and read here.  A second list would be a second thing to update.
    """
    path = path or Path(os.environ.get(
        "SRB_RETIRED", "/opt/srb/retired-crates.txt"))
    if not path.is_file():
        return []
    names = []
    for line in read(path).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line)
    return names


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def repo() -> Path:
    return REPO


@pytest.fixture(scope="session")
def original() -> Path:
    return ORIGINAL


@pytest.fixture(scope="session")
def retired() -> list[str]:
    names = retired_names()
    if not names:
        pytest.skip("the retired crate list is not present in this image")
    return names
