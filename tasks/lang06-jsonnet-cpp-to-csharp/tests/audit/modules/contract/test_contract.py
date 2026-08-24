"""What the release owes downstream, and what State A said correct behaviour was.

Three claims live here, none of them settled by an artefact a build produces.

The first hashes `LICENSE` against State A.  Jsonnet is Apache-2.0 and a rewrite is a
derivative work, so dropping the file or replacing the attribution makes the result
undistributable however well it evaluates Jsonnet.  That is a fact about a file in the
repository, and stage 2 is where two published CLIs are diffed against a reference
build -- nothing it measures gets closer to this question by being measured after a
publish.  The hash runs here, and its finding reaches the review in the scan digest.

`stdlib-unmodified` hashes `stdlib/std.jsonnet`.  This one is the reason the module
exists.  `std.jsonnet` is 1,713 lines of Jsonnet, not C++, and the contract is
explicit that porting it is not the task: it must be carried unmodified and
*evaluated* by the ported evaluator, the way State A lexes and parses it at startup
and binds thirty-nine native fields over the result.  The hash answers half of that
-- whether the bytes are upstream's -- and cannot answer the other half.  A
submission that transliterates every `std.` function into C# and leaves the file in
the tree as scenery passes this check and has not done the task.  So the reviewer is
told where to look, and stage 2 measures 39 native builtins and the whole `stdlib`
family on inputs composed at grading time.

`no-conformance-mutation` hashes the three directories recording what upstream
considers correct.  Grading never reads the submitted copies; the verifier holds its
own frozen ones, materialised from its own State A.  That is exactly why an edit is
worth reporting -- the only thing weakening the project's own record accomplishes is
making a partial implementation look finished to a human reader -- and also why it is
not a behavioural failure.

Deleting the conformance data is a different act from editing it.  The contract lists
these three directories under *preserved* paths, so unlike lang01's, a deletion here
is a finding: `examples/` and `test_suite/` are things the release ships.
"""

from __future__ import annotations

import re

import pytest
import srbscan
from srbscan import ORIGINAL, REPO

pytestmark = pytest.mark.scan

#: From the contract's `preserved_paths`.  Read rather than restated: these are the
#: files that *are* the release's public face, and a second list of them in a scan
#: module is free to drift from the one the submitter was handed.
PRESERVED_FILES = tuple(srbscan.contract_section("preserved_paths", "files"))
PRESERVED_DIRS = tuple(srbscan.contract_section("preserved_paths", "directories"))

#: `stdlib/std.jsonnet` and its sha256, both from the contract.  The digest is
#: recorded there and checked here, which is the whole of the byte-identity claim.
BYTE_IDENTICAL = dict(srbscan.contract_section("preserved_paths", "byte_identical"))

#: The three directories that are graded *input* and also hold upstream's recorded
#: expected output -- the `.golden` files, and the `.stdout`/`.stderr` files under
#: `test_cmd/`.
CONFORMANCE_DIRS = tuple(srbscan.contract_section("conformance_data", "directories"))

#: The upstream release the port is pinned to.
VERSION = str(srbscan.contract_section("behavioral_contract", "upstream_version"))


def test_the_contract_records_the_stdlib_digest_once():
    """The two places the contract states std.jsonnet's sha256 agree.

    Not a check on the submission at all -- a check on the task.  The digest appears
    under `preserved_paths.byte_identical` and again as
    `behavioral_contract.stdlib_sha256`, and two copies of a pin are two things that
    can disagree.  If they ever do, every submission fails one of them for a reason
    that is nobody's fault but mine, so the disagreement is reported here as a
    verifier problem rather than discovered as a mysterious uniform failure.
    """
    recorded = BYTE_IDENTICAL.get("stdlib/std.jsonnet")
    behavioural = srbscan.contract_section("behavioral_contract", "stdlib_sha256")
    assert recorded == behavioural, (
        "source-contract.json states std.jsonnet's sha256 twice and the two "
        "disagree: preserved_paths.byte_identical says %r, "
        "behavioral_contract.stdlib_sha256 says %r" % (recorded, behavioural))


def _copyright_lines() -> list[str]:
    """Every copyright line State A's LICENSE carries, in file order.

    Derived from the file rather than typed here, for the same reason the closure
    module derives its unit names from `original/core`: a list written in a scan
    module is a second description of the release, and the two are free to drift.
    """
    text = srbscan.read_text(ORIGINAL / "LICENSE")
    seen: list[str] = []
    for match in re.finditer(r"(?im)^\s*copyright[^\n]{0,90}", text):
        line = " ".join(match.group(0).split())
        if line not in seen:
            seen.append(line)
    return seen


COPYRIGHT_LINES = _copyright_lines()


# ------------------------------------------------------------------- licence

def test_license_file_is_present():
    """`LICENSE` exists at the root of the submission."""
    assert (REPO / "LICENSE").is_file(), (
        "LICENSE is absent. Jsonnet is Apache-2.0; a port is a derivative work and "
        "the licence text has to travel with it.")


@pytest.mark.srb_skip_ok
def test_license_file_is_byte_identical_to_state_a():
    """And its bytes are State A's bytes.

    Hashed rather than pattern-matched so that a subtle edit -- a year changed, a
    holder dropped, the appendix trimmed -- is caught as readily as a wholesale
    replacement.  When this fails, the two checks below say which part changed.
    """
    submitted, original = REPO / "LICENSE", ORIGINAL / "LICENSE"
    if not submitted.is_file() or not original.is_file():
        pytest.skip("one side is absent; the presence check reports that")
    got, want = srbscan.sha256(submitted), srbscan.sha256(original)
    assert got == want, (
        f"LICENSE was modified: sha256 {got[:16]} != {want[:16]} (State A). "
        f"{submitted.stat().st_size} bytes vs {original.stat().st_size}.")


@pytest.mark.parametrize("line", COPYRIGHT_LINES or ["<state-a-unreadable>"])
def test_copyright_line_survives(line):
    """One check per copyright line State A carries.

    A submission may add its own holder -- writing an interpreter earns one -- so
    additions are not examined.  Removing an existing holder is the finding, and
    naming the missing line tells the reviewer whether the file was replaced or
    trimmed.
    """
    assert COPYRIGHT_LINES, "State A's LICENSE is not readable at /opt/original"
    text = srbscan.read_text(REPO / "LICENSE")
    normalised = " ".join(text.split())
    assert line in normalised, f"LICENSE no longer carries {line!r}"


@pytest.mark.parametrize("clause", ("Apache License", "Version 2.0",
                                    "WITHOUT WARRANTIES OR CONDITIONS",
                                    "Licensed under the Apache License"))
def test_license_clause_survives(clause):
    """The operative phrases, separately from the hash.

    Cheap and specific: these four distinguish "LICENSE was reformatted" from
    "LICENSE was replaced with something that is not Apache-2.0", and a hash alone
    cannot tell the reviewer which happened.
    """
    text = " ".join(srbscan.read_text(REPO / "LICENSE").split()).upper()
    assert clause.upper() in text, (
        f"LICENSE no longer contains {clause!r}; it may have been replaced rather "
        f"than carried through.")


def test_no_source_file_claims_a_different_license(text_files):
    """A licence identifier in the tree that is not Apache-2.0.

    A port that stamps `SPDX-License-Identifier: MIT` across its C# has relicensed
    an Apache-2.0 derivative work.  Reported with the path and the identifier; a
    file legitimately carrying `Apache-2.0` does not match, and neither does one
    under `third_party/`, which upstream vendored under its own licences and which
    the closure module reports as C++ anyway.
    """
    hits = []
    for path in text_files:
        rel = srbscan.rel(REPO, path)
        if rel.startswith("third_party/"):
            continue
        text = srbscan.read_text(path)
        for match in re.finditer(r"SPDX-License-Identifier:\s*([A-Za-z0-9.\-+ ]+)",
                                 text):
            ident = match.group(1).strip()
            if "Apache-2" in ident or "Apache 2" in ident:
                continue
            hits.append("%s:%d declares %s"
                        % (rel, srbscan.line_of(text, match.start()), ident))
    assert not hits, "; ".join(sorted(hits)[:10])


# ----------------------------------------------------------- the standard library

@pytest.mark.parametrize("relative", sorted(BYTE_IDENTICAL))
def test_byte_identical_path_matches_the_recorded_digest(relative):
    """`stdlib/std.jsonnet` against the sha256 the contract records.

    Against the recorded digest rather than against State A's copy, because the
    contract states a digest and that statement is the thing to hold the submission
    to.  Both are checked, in fact: the assertion below confirms State A's own copy
    still hashes to the recorded value, so a contract that recorded the wrong digest
    fails here rather than failing every submission.
    """
    expected = BYTE_IDENTICAL[relative]
    original = ORIGINAL / relative
    if original.is_file():
        assert srbscan.sha256(original) == expected, (
            "the contract records sha256 %s for %s but State A's copy hashes to "
            "%s; the contract and the release disagree and this is not the "
            "submission's fault"
            % (expected[:16], relative, srbscan.sha256(original)[:16]))
    submitted = REPO / relative
    assert submitted.is_file(), (
        f"{relative} is absent. It is the Jsonnet standard library, written in "
        f"Jsonnet; the contract requires it carried unmodified and evaluated.")
    got = srbscan.sha256(submitted)
    assert got == expected, (
        "%s was modified: sha256 %s != %s (recorded), %d bytes vs %d. It is data, "
        "not C++, and porting it to C# is not the task."
        % (relative, got[:16], expected[:16], submitted.stat().st_size,
           original.stat().st_size if original.is_file() else -1))


def test_stdlib_is_reachable_from_the_csharp(cs_files, files):
    """Something in the tree refers to `std.jsonnet` by name, or embeds it.

    The weakest useful form of "the stdlib is interpreted, not transliterated", and
    deliberately weak: a `.csproj` `EmbeddedResource`, a file read by relative path,
    a generated resource name would all satisfy it, and so would a comment.  What it
    is good for is the negative -- a tree with 6,000 lines of C# and *no* reference
    to `std.jsonnet` anywhere has almost certainly reimplemented the standard
    library in C# and left the file sitting there, which is the specific thing the
    contract rules out and the hash above cannot see.

    Reported, not decided.  Stage 2 evaluates 39 native builtins and the whole
    `stdlib` family against a reference build, which is where a transliteration that
    got a corner wrong actually fails.
    """
    needle = "std.jsonnet"
    referring = []
    for path in list(cs_files) + [p for p in files
                                  if p.suffix.lower() in (".csproj", ".props",
                                                          ".targets", ".json",
                                                          ".resx")]:
        if needle in srbscan.read_text(path):
            referring.append(srbscan.rel(REPO, path))
    assert referring, (
        "no .cs or project file in the tree mentions %r. The contract requires the "
        "standard library to be embedded unmodified and evaluated by the ported "
        "evaluator; check whether its functions were rewritten in C# instead, with "
        "the file left in place." % needle)


# -------------------------------------------------------- conformance record

@pytest.mark.parametrize("relative", CONFORMANCE_DIRS)
def test_conformance_directory_is_present(relative):
    """One check per directory: `test_suite/`, `test_cmd/`, `examples/`.

    A finding when absent, which is where this differs from a task whose
    conformance data was a C harness's private input.  These three are listed under
    the contract's *preserved* paths -- `examples/` is documented in the language
    reference and `test_suite/` is what upstream ships as its conformance suite --
    so deleting them removes part of the release.
    """
    assert (ORIGINAL / relative).is_dir(), (
        f"the contract lists {relative}/ as conformance data but State A has no "
        f"such directory, so this check can only ever pass")
    assert (REPO / relative).is_dir(), (
        f"{relative}/ is absent. The contract preserves it: it is upstream's own "
        f"record of correct behaviour, and grading materialises its own copy, so "
        f"nothing was gained by removing it.")


@pytest.mark.parametrize("relative", CONFORMANCE_DIRS)
@pytest.mark.srb_skip_ok
def test_conformance_directory_is_unedited(relative):
    """Every file in it, hashed against State A's copy.

    Reported as three counts -- changed, missing, added -- because they are three
    different acts.  A changed `.golden` is a rewritten expectation; a missing case
    is a deleted one; an added file is usually harmless and occasionally is where an
    answer table went.  Grading uses the verifier's own frozen copies, so none of
    this can change the score, which is why it is worth reading.
    """
    src, dst = ORIGINAL / relative, REPO / relative
    if not src.is_dir():
        pytest.skip(f"{relative} is not in State A")
    if not dst.is_dir():
        pytest.skip("the directory is absent; the presence check reports that")
    changed, missing = [], []
    for path in srbscan.walk_source(src):
        rel = path.relative_to(src)
        other = dst / rel
        if not other.is_file():
            missing.append(str(rel))
        elif srbscan.sha256(other) != srbscan.sha256(path):
            changed.append(str(rel))
    original_names = {str(p.relative_to(src)) for p in srbscan.walk_source(src)}
    added = [str(p.relative_to(dst)) for p in srbscan.walk_source(dst)
             if str(p.relative_to(dst)) not in original_names]
    assert not (changed or missing), (
        "%s/: %d file(s) changed (%s), %d missing (%s), %d added (%s), out of %d in "
        "State A."
        % (relative, len(changed), ", ".join(sorted(changed)[:6]),
           len(missing), ", ".join(sorted(missing)[:6]),
           len(added), ", ".join(sorted(added)[:6]), len(original_names)))


@pytest.mark.parametrize("relative", CONFORMANCE_DIRS)
@pytest.mark.srb_skip_ok
def test_conformance_directory_did_not_shrink(relative):
    """A file count, separately from the hashes.

    Separate because a truncation and a correction look the same to a hash and are
    different acts.  A directory that lost forty `.jsonnet` cases has had its
    conformance suite trimmed, and the count says so in one number even when the
    per-file list above is too long to read.
    """
    src, dst = ORIGINAL / relative, REPO / relative
    if not src.is_dir() or not dst.is_dir():
        pytest.skip("one side is absent; the checks above report that")
    want = len(srbscan.walk_source(src))
    got = len(srbscan.walk_source(dst))
    assert got >= want, (
        "%s/ holds %d file(s), down from %d in State A: %d gone."
        % (relative, got, want, want - got))


# --------------------------------------------------------- the public contract

@pytest.mark.parametrize("relative", PRESERVED_FILES)
def test_preserved_file_is_present(relative):
    """One check per file the contract declares preserved.

    `LICENSE`, `README.md`, `CONTRIBUTING` (upstream ships it without an
    extension), `release_checklist.md` and `stdlib/std.jsonnet`.  The extensionless
    `CONTRIBUTING` is the one worth naming: it is easy to "preserve" as
    `CONTRIBUTING.md` and that is a renamed file, not a preserved one.
    """
    assert (ORIGINAL / relative).exists(), (
        f"the contract lists {relative} as preserved but State A has no such "
        f"path, so this check can only ever pass")
    assert (REPO / relative).is_file(), (
        f"{relative} is absent; the contract lists it under preserved_paths.")


@pytest.mark.parametrize("relative", PRESERVED_DIRS)
def test_preserved_directory_is_present(relative):
    """`doc/`, `examples/`, `test_suite/` -- the website, the documented examples,
    the conformance suite.

    Present and non-empty.  An empty directory that survives a `git` round trip is
    no directory at all, and "the port deleted the language reference" is a
    compatibility break with nothing to do with evaluating Jsonnet.
    """
    assert (ORIGINAL / relative).is_dir(), (
        f"the contract lists {relative}/ as preserved but State A has no such "
        f"directory, so this check can only ever pass")
    target = REPO / relative
    assert target.is_dir(), (
        f"{relative}/ is absent; the contract lists it under preserved_paths.")
    assert srbscan.walk_source(target), f"{relative}/ exists but is empty"


@pytest.mark.parametrize("relative", PRESERVED_FILES)
@pytest.mark.srb_skip_ok
def test_preserved_file_is_unchanged_or_reported(relative):
    """And what changed in it, when something did.

    Not every edit here is a defect.  `README.md` describes how to build the
    project, and a port that now builds with `dotnet` has a reason to say so;
    `CONTRIBUTING` points at the C++ style guide.  Which is why this reports the
    diff rather than deciding -- with one exception, `stdlib/std.jsonnet`, whose
    bytes are checked against a recorded digest above because the contract states
    one.
    """
    submitted, original = REPO / relative, ORIGINAL / relative
    if not original.is_file() or not submitted.is_file():
        pytest.skip("one side is absent; the presence check reports that")
    got, want = srbscan.read_text(submitted), srbscan.read_text(original)
    if got == want:
        return
    got_lines, want_lines = got.splitlines(), want.splitlines()
    removed = [ln for ln in want_lines if ln not in got_lines]
    added = [ln for ln in got_lines if ln not in want_lines]
    pytest.fail(
        "%s differs from State A: %d line(s) removed, %d added. Removed: %s. "
        "Added: %s."
        % (relative, len(removed), len(added),
           [ln.strip()[:70] for ln in removed[:5]],
           [ln.strip()[:70] for ln in added[:5]]),
        pytrace=False)


def test_version_is_still_the_pinned_release(text_files):
    """No build or package file declares a version other than the pinned one.

    A migration is not a release.  Distribution packages and every downstream pin
    read this number, and bumping it to signal "now in C#" invalidates all of them
    silently.  Only a lead: an internal library project's `<Version>` is not the
    release version, and the message says which file declared what so the reviewer
    can tell those apart.  Stage 2 measures `jsonnet --version` on the published
    CLI, which is the surface that matters.
    """
    # Vendored trees carry their own versions and are entitled to them.  State A's
    # own `doc/` ships MathJax 2.7.2 as a documentation asset, and reporting it as
    # a version bump of jsonnet is noise that buries the finding this is for.
    vendored = ("doc/", "third_party/", "case_studies/")
    hits = []
    for path in text_files:
        if path.suffix.lower() not in (".csproj", ".props", ".targets", ".json"):
            continue
        if path.name == "source-contract.json":
            continue
        if srbscan.rel(REPO, path).startswith(vendored):
            continue
        text = srbscan.read_text(path)
        for pattern in (r"<(?:Version|AssemblyVersion|FileVersion|"
                        r"VersionPrefix)>\s*v?(\d+\.\d+\.\d+)",
                        r'"version"\s*:\s*"v?(\d+\.\d+\.\d+)"'):
            for match in re.finditer(pattern, text):
                if match.group(1) != VERSION:
                    hits.append("%s:%d declares version %s"
                                % (srbscan.rel(REPO, path),
                                   srbscan.line_of(text, match.start()),
                                   match.group(1)))
    assert not hits, (
        "%s; the pinned release is %s."
        % ("; ".join(sorted(set(hits))[:8]), VERSION))


@pytest.mark.srb_skip_ok
def test_no_state_a_source_survives_under_a_new_name(files):
    """A State A C++ file present with its name or extension changed.

    The one check here that no per-path comparison can make: `core/vm.cpp` renamed
    to `vm.cpp.bak`, or moved to `attic/vm`, has the same content and a name none of
    the closure module's suffix checks look at.  Compared by hash against every
    native source file State A shipped, so a relocated copy is found wherever it
    went -- and an *edited* one is not reported here at all, which is what the
    closure module's per-unit checks are for.

    A file still at its State A path is not a rename, and the closure module reports
    it by name, so only the moved and the renamed reach this message.
    """
    exts = set(srbscan.contract_section("native_code_policy", "extensions"))
    exempt = tuple(srbscan.contract_section("native_code_policy",
                                            "allowed_prefixes"))
    baseline: dict[str, list[str]] = {}
    for path in srbscan.walk_source(ORIGINAL):
        rel = srbscan.rel(ORIGINAL, path)
        if path.suffix.lower() not in exts or rel.startswith(exempt):
            continue
        # An empty file has no content to relocate, and State A ships five of
        # them.  They all hash alike, so leaving them in the baseline made every
        # empty file in the submission a byte-for-byte copy of whichever
        # zero-length `.cpp` happened to be indexed first -- which is how this
        # check first reported `python/__init__.py` as a relocated C++ file.
        if path.stat().st_size == 0:
            continue
        baseline.setdefault(srbscan.sha256(path), []).append(rel)
    if not baseline:
        pytest.skip("State A is not readable")
    hits = []
    for path in files:
        rel = srbscan.rel(REPO, path)
        if path.stat().st_size == 0 or rel.startswith(exempt):
            continue
        origins = baseline.get(srbscan.sha256(path))
        if not origins or rel in origins:
            continue
        # State A may itself ship the same bytes twice.  Naming every origin
        # keeps the report honest: "this is one of these two" is what the
        # evidence supports, and picking one would invent a provenance.
        hits.append("%s is State A's %s, byte for byte"
                    % (rel, " or ".join(origins)))
    assert not hits, (
        "%d relocated native source file(s), against %d distinct State A source "
        "digests: %s" % (len(hits), len(baseline), "; ".join(sorted(hits)[:10])))
