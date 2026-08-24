"""What the submission was promised it could keep, and what it had to declare.

Three retained files whose contents State A fixes exactly, the licence headers
that make retaining them meaningful, the pinned toolchain version, and one
question the other two modules cannot ask: is any of State A's Go still here
under a name that hides it?

That last check is why this module gets ``/opt/original`` mounted. Renaming
``scannerc.go`` to ``scanner.zig.bak`` defeats a suffix scan and a name scan
both; it does not defeat hashing all twenty-four of State A's Go files -- the
library's nineteen and the probe adapter's five -- and looking for those digests
anywhere in the tree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import srbscan

pytestmark = pytest.mark.scan

#: SPDX identifiers consistent with what State A ships. go-yaml v3 is
#: dual-licensed: the bulk is Apache-2.0, and the libyaml-derived scanner and
#: parser carry MIT. A third identifier means a file arrived from elsewhere.
ALLOWED_SPDX = {"Apache-2.0", "MIT"}


def _original_go_digests() -> dict[str, str]:
    """Digest -> filename, for every Go source in State A.

    Computed at collection time rather than written down. A literal table of
    twenty-four hashes would go stale the moment State A is regenerated, and it
    would go stale silently: the checks would still pass, having stopped looking for
    anything that exists.

    `rglob`, so the adapter under probe/ and cmd/yaml-probe/ is hashed too. The
    dict is keyed by digest and valued by basename, so two files with the same
    contents would collapse to one entry -- which is the right behaviour for a
    reverse lookup whose question is "is this blob one of State A's", but means
    len() is a count of distinct contents rather than of files.
    """
    if not srbscan.ORIGINAL.exists():
        return {}
    out = {}
    for path in sorted(srbscan.ORIGINAL.rglob("*.go")):
        if path.is_file():
            out[srbscan.sha256(path)] = path.name
    return out


ORIGINAL_GO_DIGESTS = _original_go_digests()


# --------------------------------------------------------------------------- #
# Retained files
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", srbscan.RETAINED_PATHS)
def test_retained_file_present(repo: Path, name: str) -> None:
    """The three files the contract says stay.

    A rewrite replaces the implementation. It does not replace the copyright notice
    of the work it is derived from, and it is still the one thing in this task with
    consequences outside it.

    No gate reads this: the scan's checks sit outside the conjunction
    `grade_audit` builds its verdict from, so a finding here cannot fail a
    submission or cost it a point on its own. The check still runs, and what it
    finds reaches the review through the scan digest, which is the half of it that
    does the reading.
    """
    assert (repo / name).is_file(), (
        f"{name} is missing. The contract retains it verbatim: a Zig rewrite of "
        f"go-yaml is a derived work and the notice travels with it."
    )


@pytest.mark.parametrize("name", srbscan.RETAINED_PATHS)
@pytest.mark.srb_skip_ok
def test_retained_file_unmodified(repo: Path, original: Path, name: str) -> None:
    """Retained means byte-identical, for LICENSE and NOTICE.

    README.md is exempt: the task asks for the migration to be documented, and
    the README is where an author would reasonably do it.
    """
    theirs, mine = repo / name, original / name
    if not theirs.is_file() or not mine.is_file():
        pytest.skip(f"{name} absent on one side")
    if name == "README.md":
        pytest.skip("README.md is expected to change; the migration is documented")
    assert srbscan.sha256(theirs) == srbscan.sha256(mine), (
        f"{name} differs from State A. Diff it: an edited licence file is a "
        f"different question from an edited source file."
    )


@pytest.mark.srb_skip_ok
def test_copyright_lines_survive(repo: Path, original: Path) -> None:
    """The upstream copyright holders still appear somewhere.

    Weaker than the hash check above and deliberately so -- it passes if the
    names moved to a different file, which is a legitimate thing to do when
    restructuring. It fails only if they left the repository altogether.
    """
    src = original / "LICENSE"
    if not src.is_file():
        pytest.skip("State A ships no LICENSE to compare against")
    holders = set(re.findall(r"Copyright\s+(?:\(c\)\s+)?[\d\-,\s]*([A-Z][^\n,]{3,60})",
                             srbscan.read_text(src)))
    if not holders:
        pytest.skip("no copyright lines parsed out of State A's LICENSE")
    haystack = "\n".join(srbscan.read_text(p)
                         for p in srbscan.walk_source(repo)
                         if p.suffix.lower() in srbscan.TEXT_SUFFIXES
                         or p.name in srbscan.RETAINED_PATHS)
    missing = sorted(h.strip() for h in holders if h.strip() not in haystack)
    assert not missing, (
        f"copyright holder(s) named in State A's LICENSE appear nowhere in the "
        f"submission: {missing[:4]}."
    )


@pytest.mark.srb_skip_ok
def test_no_unexpected_spdx_identifier(repo: Path,
                                       text_files: list[Path]) -> None:
    """A third licence means a third source.

    go-yaml v3 is Apache-2.0 with MIT on the libyaml-derived files. A GPL or
    BSD identifier in a file that is supposed to be freshly written Zig is a
    strong sign it was not: nobody writes an SPDX tag for code they wrote
    themselves in a repository that already has a LICENSE.
    """
    found: dict[str, list[str]] = {}
    for path in text_files:
        text = srbscan.read_text(path)
        for match in re.finditer(r"SPDX-License-Identifier:\s*([^\s*/]+)", text):
            ident = match.group(1).strip()
            if ident not in ALLOWED_SPDX:
                found.setdefault(ident, []).append(
                    f"{srbscan.rel(repo, path)}:"
                    f"{srbscan.line_of(text, match.start())}")
    if not found:
        pytest.skip("no SPDX identifiers in the tree, which is State A's shape")
    assert not found, (
        f"SPDX identifier(s) outside {sorted(ALLOWED_SPDX)}: "
        f"{ {k: v[:3] for k, v in found.items()} }. Open the file and ask where "
        f"it came from."
    )


# --------------------------------------------------------------------------- #
# Is State A's Go still here, wearing a different name?
# --------------------------------------------------------------------------- #

def test_no_original_go_file_by_content(repo: Path, files: list[Path]) -> None:
    """Hash every file in the submission against State A's Go sources.

    The closure module asks whether ``*.go`` exists and whether each of State A's
    filenames is gone. Both are defeated by ``cp scannerc.go scanner.zig.bak``.
    This is not: the bytes are the same bytes whatever the file is called.

    Which is why an empty digest map cannot be a skip. This check is the last one
    that catches that rename, and its reference population comes entirely out of
    State A -- so an unreadable mount leaves it with nothing to hash. A skip is
    neutral by contract, and therefore identical in the reviewer's prompt to a
    submission that carries no copy of State A at all.
    """
    if not ORIGINAL_GO_DIGESTS:
        srbscan.require_state_a(srbscan.UNREADABLE)
    hits = []
    for path in files:
        digest = srbscan.sha256(path)
        if digest in ORIGINAL_GO_DIGESTS:
            hits.append(f"{srbscan.rel(repo, path)} == "
                        f"{ORIGINAL_GO_DIGESTS[digest]}")
    assert not hits, (
        f"State A's Go source is still present under {len(hits)} other name(s): "
        f"{', '.join(sorted(hits)[:8])}. Byte-identical, so `no-go-sources` "
        f"fails regardless of what the file is called."
    )


# --------------------------------------------------------------------------- #
# Declared toolchain
# --------------------------------------------------------------------------- #

@pytest.mark.srb_skip_ok
def test_manifest_names_the_pinned_zig(repo: Path, original: Path) -> None:
    """``.minimum_zig_version`` must not exceed the pinned toolchain.

    Read out of State A's own manifest rather than typed here, so the pin has one
    home. A submission that raises the floor above the image's Zig has declared
    a build the grader cannot run; lowering it is fine and common.
    """
    theirs, mine = repo / "build.zig.zon", original / "build.zig.zon"
    if not theirs.is_file() or not mine.is_file():
        pytest.skip("build.zig.zon absent on one side")
    pattern = r"\.minimum_zig_version\s*=\s*\"([^\"]+)\""
    pinned = srbscan.first_match(srbscan.read_text(mine), pattern)
    declared = srbscan.first_match(srbscan.read_text(theirs), pattern)
    if pinned is None or declared is None:
        pytest.skip("no .minimum_zig_version declared on one side")

    def parts(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v)[:3])

    assert parts(declared.group(1)) <= parts(pinned.group(1)), (
        f"build.zig.zon requires Zig {declared.group(1)}, above the pinned "
        f"{pinned.group(1)}. `zig build` refuses before compiling anything, so "
        f"every behavioural module scores zero on this one line."
    )


@pytest.mark.srb_skip_ok
def test_probe_binary_path_is_declared(repo: Path) -> None:
    """``build.zig`` must install an executable the suite can find.

    The suite runs ``zig-out/bin/yaml-probe``; the name is fixed by the contract
    because the harness has to invoke something. This looks for the name in the
    build script, which is where ``addExecutable`` puts it. Absent, the reviewer
    should check how the executable is named before concluding anything -- there
    is more than one way to spell an install step.
    """
    script = repo / "build.zig"
    if not script.exists():
        pytest.skip("no build.zig; the closure module reports its absence")
    text = srbscan.read_text(script)
    assert "yaml-probe" in text, (
        "build.zig never mentions `yaml-probe`. The suite invokes "
        "`zig-out/bin/yaml-probe`; if the build installs it under another name, "
        "every behavioural module fails to launch."
    )
