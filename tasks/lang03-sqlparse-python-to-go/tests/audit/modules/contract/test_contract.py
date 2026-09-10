"""What the migration was required to keep, and what it was required to write down.

The mechanical half of `preserved-files`, `license-intact`, `documented-surface` and
`no-orphan-references`.  Everything here is a file that has to be present, a file
that has to be unchanged, or a sentence the tree has to contain -- questions with an
answer that does not depend on anything being built, which is what puts them in
stage 1 rather than stage 2.

The distinction this module has to keep straight is between a file whose bytes are
fixed and a file whose presence is fixed.  `preserved_paths` in the contract gives
one of two answers for each of its seven entries, and they are not the same
requirement: LICENSE and AUTHORS are `byte-identical`, while README.rst is expected
to change -- the install instructions are different now -- and only required not to
be deleted.  A check that hashed all seven would fail every honest submission, and a
check that hashed none of them would let the license be rewritten.  So the split is
read out of the contract's own wording rather than restated here.

Documentation checks are the softest thing in this scan and are reported as
measurements.  `min_documented_ratio` is 0.75 against a reference that manages 0.95,
and the number a scan can compute by matching `//` above a `func` is not the number
`go doc` would produce.  The measurement goes in the report so the reviewer sees it;
the reviewer decides.

That split is also why this module carries eleven `srb_skip_ok` markers, more than the
other two together, and why they matter most here.  The harness scores an unmarked skip
as a miss, so without them the five preserved paths the contract does *not* require to
be byte-identical arrive in the reviewer's prompt as five flagged findings -- README.rst
changed, CHANGELOG changed -- which is both false and the exact opposite of what the
contract says about them.  A reviewer who checks one false finding reads the next one
with less attention, and these would have been the first five it saw.
"""

from __future__ import annotations

import re

import pytest
import srbscan
from srbscan import CONTRACT, ORIGINAL, REPO

pytestmark = pytest.mark.scan

#: The seven preserved paths, and which of the two requirements each one carries.
#: Derived from the contract's own wording: an entry whose note says
#: `byte-identical` is hashed, and every other entry is only required to exist.
PRESERVED = tuple(sorted(CONTRACT.get("preserved_paths", {}).items()))
PRESERVED_IDS = tuple(p[0] for p in PRESERVED) or ("<contract-unreadable>",)

#: Clauses that have to survive verbatim for the file to still be the BSD 3-Clause
#: license sqlparse ships under.  Chosen to be the operative sentences rather than
#: the whole text: the hash check covers the whole text, and when the hash fails
#: these say *which part* changed, which is the difference between a reformatted
#: file and a relicensed one.
LICENSE_CLAUSES = (
    "Redistribution and use in source and binary forms",
    "Redistributions of source code must retain the above copyright notice",
    "Redistributions in binary form must reproduce the above copyright notice",
    "Neither the name of the authors nor the names of its contributors",
    'THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"',
)

#: Paths the contract says may go.  Removing them is expected; what this module
#: checks is that a path which stayed did not stay as Python.
REMOVABLE = tuple(CONTRACT.get("python_policy", {}).get("removable_paths", ())) or (
    "<contract-unreadable>",
)


def _state_a_digest(relative: str) -> str:
    return (CONTRACT.get("state_a", {}).get("files", {})
            .get(relative, {}).get("sha256", ""))


def _line_diff(left: str, right: str, limit: int = 6) -> list[str]:
    """The first few lines present in one text and not the other.

    Not a real diff: a scan reporting that a preserved file changed needs to say
    what changed enough that the reviewer can decide whether it matters, and the
    first handful of added and removed lines does that in one screen.
    """
    a, b = left.splitlines(), right.splitlines()
    removed = [ln for ln in a if ln.strip() and ln not in b][:limit]
    added = [ln for ln in b if ln.strip() and ln not in a][:limit]
    out = ["-%s" % ln.strip()[:110] for ln in removed]
    out += ["+%s" % ln.strip()[:110] for ln in added]
    return out


# --------------------------------------------------------- what has to stay

def test_contract_lists_the_preserved_paths():
    """The list this module parametrizes over is not empty.

    First for the reason the closure module's contract check is first: every
    parametrized check below substitutes a placeholder when the contract cannot be
    read, and a placeholder skips.  Without this, an unreadable contract would make
    the whole module quiet.
    """
    assert PRESERVED, (
        "source-contract.json has no preserved_paths, so every check in this module "
        "ran against a placeholder. This is a verifier defect, not a submission "
        "defect.")
    assert len(PRESERVED) >= 6, (
        f"preserved_paths has only {len(PRESERVED)} entries; sqlparse 0.5.3 carries "
        f"six root files plus the man page.")


@pytest.mark.parametrize("relative,requirement", PRESERVED or (("x", "y"),),
                         ids=PRESERVED_IDS)
@pytest.mark.srb_skip_ok
def test_preserved_path_is_present(relative, requirement, files):
    """One check per preserved path, presence only.

    The man page is the one entry with two legal locations: the contract lets it move
    to `share/man/man1/sqlformat.1`, because that is where `go install` of a command
    would put a man page and the install inventory names it there.  Accepting either
    is not laxity -- it is the contract's own wording, and a check that demanded
    `docs/sqlformat.1` would fail a submission that did the more correct thing.
    """
    if relative == "x":
        pytest.skip("the contract is unreadable; the check above reports that")
    candidates = [relative]
    if relative == "docs/sqlformat.1":
        candidates.append("share/man/man1/sqlformat.1")
    for candidate in candidates:
        if (REPO / candidate).is_file():
            return
    present = sorted(srbscan.rel(REPO, p) for p in files
                     if p.name == relative.rsplit("/", 1)[-1])
    assert False, (
        "%s is missing (contract: %s). Looked at %s. Files with that name elsewhere "
        "in the tree: %s"
        % (relative, requirement, " and ".join(candidates),
           ", ".join(present[:6]) or "none"))


@pytest.mark.parametrize("relative,requirement", PRESERVED or (("x", "y"),),
                         ids=PRESERVED_IDS)
@pytest.mark.srb_skip_ok
def test_byte_identical_preserved_path_is_unchanged(relative, requirement):
    """The hash check, and only for the entries whose note demands it.

    Which entries those are is read from the contract's wording rather than listed
    here, so that adding an entry to the contract adds a check without anyone
    remembering to.  A submission that reflowed LICENSE fails this and passes
    `test_license_still_carries_its_clauses`, and the pair of results is the
    reviewer's answer: the license was reformatted, not replaced.
    """
    if relative == "x":
        pytest.skip("the contract is unreadable; the first check reports that")
    if "byte-identical" not in requirement:
        pytest.skip(f"the contract asks only that {relative} still be present")
    path = REPO / relative
    if not path.is_file():
        pytest.skip("absent; the presence check reports that")
    expected = _state_a_digest(relative)
    assert expected, (
        f"state_a.files has no digest for {relative}, so this check could not run. "
        f"A verifier defect.")
    actual = srbscan.sha256(path)
    if actual == expected:
        return
    original = ORIGINAL / relative
    detail = ("; ".join(_line_diff(srbscan.read_text(original),
                                  srbscan.read_text(path)))
              if original.is_file() else "State A is not mounted, so no diff")
    assert False, (
        "%s changed (contract: %s).\n  expected sha256 %s\n  actual   sha256 %s\n  "
        "%s" % (relative, requirement, expected, actual, detail))


@pytest.mark.parametrize("clause", LICENSE_CLAUSES,
                         ids=[str(i) for i in range(len(LICENSE_CLAUSES))])
@pytest.mark.srb_skip_ok
def test_license_still_carries_its_clause(clause):
    """One check per operative clause of the BSD 3-Clause text.

    Redundant with the hash while the hash passes, and the reason to have it anyway
    is what happens when the hash fails.  `license-intact` asks whether the terms
    still hold, and the answer for a file with CRLF line endings is yes while the
    answer for a file with the third clause deleted is no.  These five checks
    separate those two failures, which one digest cannot.
    """
    path = REPO / "LICENSE"
    if not path.is_file():
        pytest.skip("LICENSE is absent; the presence check reports that")
    text = " ".join(srbscan.read_text(path).split())
    assert " ".join(clause.split()) in text, (
        f"LICENSE no longer contains {clause!r}. Whitespace is normalized before "
        f"the comparison, so this is a missing or altered clause, not reflowing.")


def test_license_declares_no_other_spdx_identifier(text_files):
    """An SPDX line naming a license that is not BSD-3-Clause.

    The way a relicense actually happens in a rewrite: nobody edits LICENSE, someone
    writes `// SPDX-License-Identifier: MIT` at the top of a new Go file because
    that is what their editor template does, and the repository now says two things.
    Every hit is reported with its file, including the ones that agree, since an
    `SPDX-License-Identifier: BSD-3-Clause` header is correct and worth confirming.
    """
    disagreeing = []
    for path in text_files:
        text = srbscan.read_text(path)
        for match in re.finditer(r"SPDX-License-Identifier:\s*([^\s*/]+)", text):
            if match.group(1).strip() != "BSD-3-Clause":
                disagreeing.append(
                    "%s:%d declares %s"
                    % (srbscan.rel(REPO, path),
                       srbscan.line_of(text, match.start()), match.group(1)))
    assert not disagreeing, (
        "%d SPDX identifier(s) that are not BSD-3-Clause: %s. sqlparse is BSD "
        "3-Clause and the license does not change because the language did."
        % (len(disagreeing), "; ".join(sorted(disagreeing)[:10])))


@pytest.mark.srb_skip_ok
def test_copyright_holders_survive_in_the_tree(files):
    """The copyright lines State A carried are still somewhere.

    Derived from State A rather than transcribed, and satisfied by any file: this is
    the attribution question, not a placement question, and a port that moved the
    copyright header from `sqlparse/__init__.py` into `doc.go` has done the right
    thing.  Reported per line, because "the copyright notice is gone" and "one of the
    two copyright holders is gone" are different findings.
    """
    if not ORIGINAL.is_dir():
        pytest.skip("State A is not mounted; the closure module reports that")
    wanted = set()
    for path in srbscan.walk_source(ORIGINAL):
        if path.suffix in (".py", "") or path.name == "LICENSE":
            for line in srbscan.read_text(path).splitlines():
                if re.match(r"^\s*(?:#\s*)?Copyright\s+\(C\)", line, re.IGNORECASE):
                    wanted.add(" ".join(line.strip().lstrip("# ").split()))
    if not wanted:
        pytest.skip("State A carries no copyright lines to look for")
    #: Python suffixes are searched too, which is not an oversight.  State A's
    #: `Copyright (C) 2009-2020` line lives in the header of every module under
    #: `sqlparse/`, so a haystack that skipped `.py` files would report the
    #: attribution missing from a submission that had not deleted a single one of
    #: them.  That finding would be false, and a reviewer who checks one false
    #: finding reads the next one with less attention.  The submission that kept
    #: its Python has a problem, and twenty other checks in this scan say so.
    haystack = "\n".join(
        " ".join(srbscan.read_text(p).split()) for p in files
        if p.suffix.lower() in srbscan.TEXT_SUFFIXES
        or p.suffix.lower() in srbscan.PYTHON_SUFFIXES)
    missing = sorted(w for w in wanted if w not in haystack)
    assert not missing, (
        "%d of State A's %d copyright line(s) appear nowhere in the submission: %s"
        % (len(missing), len(wanted), "; ".join(repr(m) for m in missing[:6])))


@pytest.mark.srb_skip_ok
def test_changelog_still_carries_the_release_entry():
    """CHANGELOG keeps its 0.5.3 entry.

    The contract's wording for this entry is "must still be present and must still
    contain the 0.5.3 entry; appending to it is expected", which is why it is not
    hashed: a submission documenting the port at the top of the file has done what
    was asked.  The version comes from the contract, so the check follows a version
    bump rather than pinning 0.5.3 into a second place.
    """
    path = REPO / "CHANGELOG"
    if not path.is_file():
        pytest.skip("CHANGELOG is absent; the presence check reports that")
    version = (CONTRACT.get("behavioral_contract", {}).get("version_string")
               or CONTRACT.get("state_a", {}).get("upstream_version", ""))
    assert version, "the contract carries no version string; a verifier defect"
    text = srbscan.read_text(path)
    assert version in text, (
        f"CHANGELOG no longer mentions {version}. Appending to this file is "
        f"expected; replacing its history is not.")


@pytest.mark.srb_skip_ok
def test_man_page_still_documents_the_command():
    """The man page describes real options rather than being an empty stub.

    Presence is the check above; this one opens it.  The contract requires that it
    "still describe the command's real options", and the cheapest way to satisfy
    presence is a file with a title line, so the check reads the flags out of State
    A's own man page and asks whether they are still documented.

    Advisory in one direction on purpose: a port that renamed nothing keeps all of
    them, and a submission that dropped one has either changed the CLI -- which
    stage 2 grades against the reference, with an artefact -- or truncated the page.
    """
    for candidate in ("docs/sqlformat.1", "share/man/man1/sqlformat.1"):
        path = REPO / candidate
        if path.is_file():
            break
    else:
        pytest.skip("the man page is absent; the presence check reports that")
    original = ORIGINAL / "docs/sqlformat.1"
    if not original.is_file():
        pytest.skip("State A is not mounted; the closure module reports that")
    text = srbscan.read_text(path)
    flags = sorted(set(re.findall(r"--[a-z][a-z0-9_-]{2,}",
                                  srbscan.read_text(original))))
    missing = [f for f in flags if f not in text]
    assert not missing, (
        "%s documents %d of State A's %d long options; missing %s"
        % (srbscan.rel(REPO, path), len(flags) - len(missing), len(flags),
           ", ".join(missing[:10])))


# ------------------------------------------------- what may go, and how

@pytest.mark.parametrize("relative", REMOVABLE)
@pytest.mark.srb_skip_ok
def test_removable_path_left_no_python_behind(relative, files):
    """One check per removable path: gone, or present without Python in it.

    The contract is explicit that removing these is expected, so their absence is
    not a finding and this check passes on it.  What it looks for is the halfway
    state -- `tests/` kept as a directory of Go tests is right, `tests/` kept with
    `test_parse.py` still in it is the old implementation's test suite surviving
    under the impression that only `sqlparse/` mattered.
    """
    if relative == "<contract-unreadable>":
        pytest.skip("the contract is unreadable; the first check reports that")
    prefix = relative.rstrip("/")
    inside = [p for p in files
              if srbscan.rel(REPO, p) == prefix
              or srbscan.rel(REPO, p).startswith(prefix + "/")]
    if not inside:
        return
    python = sorted(srbscan.rel(REPO, p) for p in inside
                    if p.suffix.lower() in srbscan.PYTHON_SUFFIXES)
    assert not python, (
        "%s survived and still holds %d Python file(s): %s. Keeping the path is "
        "allowed; keeping the Python in it is not."
        % (relative, len(python), ", ".join(python[:8])))


@pytest.mark.srb_skip_ok
def test_readme_is_not_still_the_python_readme():
    """README.rst says how to install the thing that now exists.

    The contract expects this file to change and does not say into what, so the check
    asks the narrowest question that has a wrong answer: a README whose install
    section is still `pip install sqlparse` documents a package this repository no
    longer builds.  A `pip` mention in prose about the project's history is fine, so
    the pattern is the install instruction rather than the word.
    """
    path = REPO / "README.rst"
    if not path.is_file():
        pytest.skip("README.rst is absent; the presence check reports that")
    text = srbscan.read_text(path)
    hits = [
        "line %d: %s" % (srbscan.line_of(text, m.start()), m.group(0).strip())
        for m in re.finditer(r"(?m)^\s*(?:\$\s*)?(?:pip|pip3|python -m pip)\s+"
                             r"install\s+.*$", text)
    ]
    assert not hits, (
        "README.rst still gives a pip install instruction: %s. The repository no "
        "longer builds a Python package." % "; ".join(hits[:4]))


@pytest.mark.srb_skip_ok
def test_no_document_points_at_a_deleted_python_module(text_files):
    """`no-orphan-references`: prose citing a file that is no longer here.

    Cross-references in a rewritten repository rot silently -- a CONTRIBUTING.md that
    says "the lexer lives in sqlparse/lexer.py" is wrong in a way no build catches
    and no test fails on.  Every State A module path is looked for in every text
    document, and each hit is reported with the line so the reviewer can see whether
    it is a stale instruction or a deliberate note about where the code came from.

    Two exclusions, both because the reference *should* be discussed: a file whose
    name says it documents the migration, and a line State A itself already carried.
    """
    modules = srbscan.state_a_python_modules()
    if not modules:
        pytest.skip("State A is not mounted; the closure module reports that")
    hits = []
    for path in text_files:
        name = path.name.lower()
        if any(word in name for word in ("migration", "porting", "changelog",
                                         "authors", "license", "history")):
            continue
        text = srbscan.read_text(path)
        for module in modules:
            index = text.find(module)
            if index < 0:
                continue
            line = srbscan.line_of(text, index)
            quote = text.splitlines()[line - 1].strip()[:120]
            if _inherited_line(path, quote):
                continue
            hits.append("%s:%d cites %s: %s"
                        % (srbscan.rel(REPO, path), line, module, quote))
    assert not hits, (
        "%d reference(s) to a Python module that no longer exists: %s"
        % (len(hits), "; ".join(sorted(hits)[:10])))


def _inherited_line(path, quote: str) -> bool:
    """Did State A's copy of this file already carry this line?

    Same reasoning as the provenance module's version: `docs/source/api.rst`
    documenting `sqlparse/sql.py` is upstream's file describing upstream's layout,
    and a submission that has not rewritten the docs yet has one problem rather than
    forty.
    """
    if not quote:
        return False
    other = ORIGINAL / srbscan.rel(REPO, path)
    return other.is_file() and quote in srbscan.read_text(other)


# ------------------------------------------------------ what was written down

@pytest.mark.parametrize(
    "package",
    tuple(sorted(CONTRACT.get("go_contract", {}).get("packages", {})))
    or ("<contract-unreadable>",))
@pytest.mark.srb_skip_ok
def test_package_carries_a_package_comment(package, go_impl_files):
    """`package_doc_required`: a comment immediately above the package clause.

    Go's own convention and what `go doc` prints, which is why the contract asks for
    it: a package with no package comment is a package whose role has to be inferred
    from its name.  Satisfied by any one file in the directory, including a `doc.go`
    that holds nothing else, and `cmd/sqlformat` is included because a command's
    package comment is what documents the command.

    Matched by finding a `//` or `/* */` block whose last line is directly above
    `package x`, with no blank line between -- Go's rule, and the reason a comment
    separated by a blank line does not count anywhere in the toolchain either.
    """
    if package == "<contract-unreadable>":
        pytest.skip("the contract is unreadable; the closure module reports that")
    directory = REPO / package
    if not directory.is_dir():
        pytest.skip(f"{package}/ is absent; the closure module reports that")
    candidates = [p for p in go_impl_files
                  if p.parent == directory]
    if not candidates:
        pytest.skip(f"{package}/ holds no non-test Go file")
    for path in candidates:
        lines = srbscan.read_text(path).splitlines()
        for index, line in enumerate(lines):
            if not re.match(r"^\s*package\s+\w+", line):
                continue
            above = lines[index - 1].strip() if index else ""
            if above.startswith("//") or above.endswith("*/"):
                return
            break
    assert False, (
        "no file in %s/ carries a package comment. Checked %s. The comment has to be "
        "on the line directly above `package %s`, which is Go's own rule for what "
        "`go doc` prints."
        % (package, ", ".join(sorted(srbscan.rel(REPO, p) for p in candidates)[:6]),
           package.rsplit("/", 1)[-1]))


@pytest.mark.srb_skip_ok
def test_exported_go_symbols_are_mostly_documented(go_impl_files):
    """The documented ratio, measured and reported rather than enforced.

    `min_documented_ratio` is 0.75 and the reference manages 0.95, so a port with a
    bare public surface is a real finding.  It is also a finding this scan cannot
    settle: what counts as an exported symbol here is what a regex sees at the start
    of a line, and the contract's own scope note excludes `value` symbols because a
    comment on an enclosing `var (...)` block documents every name inside it -- which
    is a structure this measurement does not resolve.

    So the number goes in the report with its own error bar.  A run at 0.9 is
    evidence; a run at 0.4 is a submission with no doc comments.
    """
    floor = (CONTRACT.get("go_contract", {}).get("doc_comment_policy", {})
             .get("min_documented_ratio", 0.75))
    documented = undocumented = 0
    bare: list[str] = []
    for path in go_impl_files:
        lines = srbscan.read_text(path).splitlines()
        for index, line in enumerate(lines):
            match = re.match(r"^(?:func|type)\s+(?:\([^)]*\)\s*)?([A-Z]\w*)", line)
            if not match:
                continue
            above = lines[index - 1].strip() if index else ""
            if above.startswith("//") or above.endswith("*/"):
                documented += 1
            else:
                undocumented += 1
                bare.append(f"{srbscan.rel(REPO, path)}:{index + 1} {match.group(1)}")
    total = documented + undocumented
    if not total:
        pytest.skip("no exported func or type declarations found to measure")
    ratio = documented / total
    assert ratio >= floor, (
        "%d of %d exported func/type declarations carry a doc comment (%.0f%%), "
        "below the contract's floor of %.0f%%. Undocumented: %s. Measured by "
        "matching a comment on the line above the declaration, which is not what "
        "`go doc` does -- read the ratio, not the verdict."
        % (documented, total, 100 * ratio, 100 * floor, ", ".join(bare[:12])))
