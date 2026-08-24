"""The wheel as an archive: what it claims, what it holds, and whether the two agree.

Three questions, all answerable from the file alone.

*What it claims.* The filename is a PEP 425 tag triple, and the interesting part
here is the ABI. pycryptodome's 41 compiled objects use no Python C API at all --
no `PyInit_`, no libpython, nothing but libc -- so there is nothing in this wheel
that can be interpreter-specific. State A ships `cp35-abi3`: one build, every
CPython from 3.5 up. A wheel tagged `cp312-cp312` installs on exactly one minor
version for no benefit, and every other interpreter falls back to building from
source. That is a behavioural regression measurable from the name.

The interpreter component of the tag is *not* required to match. meson-python
derives it from the interpreter that built the wheel, so a correct migration
produces `cp312-abi3` where State A produced `cp35-abi3`, and both mean "stable
ABI". The check is on the ABI component.

*What it holds.* The payload, as a set of names, against State A's 330. Three
`.dist-info` members are excluded by name -- `top_level.txt`, `AUTHORS.rst`,
`LICENSE.rst` -- because they are `bdist_wheel` conventions rather than the
project's content, and a backend that does not write them is not shipping less
library.

*Whether the two agree.* RECORD, verified: every member listed, every listed
member present, every digest recomputed. This is what makes the name comparison
mean something -- otherwise a wheel could hold 330 correctly-named empty files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import wheelutil


def _ground_truth() -> dict:
    root = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data"
    return json.loads((root / "wheel.json").read_text(encoding="utf-8"))


WHEEL = _ground_truth()
DIST_INFO_ROOT = WHEEL["dist_info_members"][0].split("/")[0]
EXPECTED_PAYLOAD = sorted(
    n for n in WHEEL["members"] if not n.startswith(DIST_INFO_ROOT + "/")
)
EXPECTED_DIST_INFO = sorted(
    n for n in WHEEL["dist_info_members"]
    if n.rsplit("/", 1)[-1] not in wheelutil.PACKAGING_ONLY
)


@pytest.fixture(scope="module")
def wheel(built):
    w = wheelutil.Wheel(Path(built("default").wheel))
    yield w
    w.close()


def test_exactly_one_wheel_was_produced(ledger):
    """Two wheels in one output directory means the ledger picked one arbitrarily."""
    rec = ledger.need("default")
    dist = Path(rec.wheel).parent
    wheels = sorted(dist.glob("*.whl"))
    assert len(wheels) == 1, (
        f"the default configuration produced {len(wheels)} wheels in {dist}: "
        f"{', '.join(p.name for p in wheels)}. Which one gets installed is then a "
        f"question about sort order."
    )


def test_the_filename_is_a_valid_wheel_name(wheel):
    """PEP 425. An installer that cannot parse the name will not look inside it."""
    tags = wheel.tags  # raises with the filename if it does not parse
    assert tags["distribution"] == WHEEL["filename"].split("-")[0], (
        f"the wheel's distribution component is {tags['distribution']!r}; State A's is "
        f"{WHEEL['filename'].split('-')[0]!r}"
    )


def test_the_wheel_is_tagged_for_the_stable_abi(wheel):
    """`abi3`, because nothing in this wheel touches the Python C API.

    Not a style preference. The 41 objects here are opened with ctypes and link
    only against libc, so a version-specific ABI tag communicates a constraint
    that does not exist: pip will refuse the wheel on 3.13 and build the whole
    thing from source instead.
    """
    abi = wheel.tags["abi"]
    assert abi == "abi3", (
        f"the wheel is tagged {abi!r}. State A ships "
        f"{WHEEL['filename'].split('-')[3]!r}: one build for every CPython from 3.5 up. "
        f"None of these 41 objects uses the Python C API, so a version-specific ABI tag "
        f"restricts where the wheel installs without making anything about it safer."
    )


def test_the_platform_tag_is_specific(wheel):
    """`linux_x86_64`, not `any`. There are 41 compiled objects in here.

    A purelib wheel tagged `any` that contains shared objects installs on
    architectures where those objects cannot load.
    """
    platform = wheel.tags["platform"]
    assert platform != "any", (
        "the wheel is tagged `any`, which claims it is pure Python; it contains 41 "
        "compiled shared objects"
    )
    assert wheel.wheel_metadata.get("Root-Is-Purelib", "").strip().lower() == "false", (
        "WHEEL declares Root-Is-Purelib: true for an archive containing compiled objects, "
        "so an installer will place them in the wrong directory"
    )


def test_the_payload_is_exactly_state_as(wheel):
    """330 names, as a set. This is the file list a user gets."""
    got, want = set(wheel.payload), set(EXPECTED_PAYLOAD)
    missing, extra = sorted(want - got), sorted(got - want)
    assert not missing, (
        f"{len(missing)} of State A's {len(want)} payload members are missing from the "
        f"wheel: {', '.join(missing[:10])}"
    )
    assert not extra, (
        f"the wheel ships {len(extra)} members State A does not: {', '.join(extra[:10])}"
    )


def test_the_dist_info_holds_what_an_installer_needs(wheel):
    """METADATA, WHEEL, RECORD. The three an installer will not proceed without.

    Compared after removing the names `bdist_wheel` adds by convention, which is
    the exclusion list in wheelutil.PACKAGING_ONLY and is applied to both sides.
    """
    got, want = set(wheel.dist_info_payload), set(EXPECTED_DIST_INFO)
    missing = sorted(want - got)
    assert not missing, (
        f"the wheel's .dist-info is missing {', '.join(missing)}, which an installer reads"
    )
    unexpected = sorted(got - want)
    assert not unexpected, (
        f"the wheel's .dist-info holds {', '.join(unexpected)}, which State A does not ship "
        f"and which an installer will copy into site-packages"
    )


def test_the_record_describes_the_archive(wheel):
    """Every digest recomputed. The check that makes every name comparison mean bytes."""
    problems = wheel.verify_record()
    assert not problems, (
        f"RECORD and the archive disagree in {len(problems)} places, so what pip verifies "
        f"on install is not what is in the wheel: " + "; ".join(problems[:8])
    )


def test_the_archive_holds_no_absolute_or_escaping_paths(wheel):
    """A member named `../` or `/etc/` writes outside the install directory."""
    bad = sorted(n for n in wheel.names if n.startswith("/") or ".." in Path(n).parts)
    assert not bad, (
        f"{len(bad)} archive members would be written outside the target directory: "
        f"{', '.join(bad[:6])}"
    )


def test_the_archive_holds_no_build_directory(wheel):
    """`build/`, `.mesonpy-*`, `meson-info/`, `*.egg-info/` are the build's, not the user's."""
    leaked = sorted(
        n for n in wheel.names
        if n.split("/")[0] in {"build", "meson-info", "meson-logs", "meson-private"}
        or ".egg-info/" in n
        or n.startswith(".mesonpy")
    )
    assert not leaked, (
        f"the wheel ships {len(leaked)} members from the build's own working directories: "
        f"{', '.join(leaked[:8])}"
    )


def test_the_wheel_is_deterministic_enough_to_compare(wheel):
    """WHEEL is well formed and names one tag.

    A wheel whose WHEEL file lists several tags is claiming compatibility with
    combinations that were never built or tested.
    """
    tags = [t.strip() for t in wheel.wheel_metadata.get_all("Tag") or []]
    assert len(tags) == 1, (
        f"WHEEL declares {len(tags)} tags ({tags}); the filename can only carry one, so "
        f"an installer and the archive disagree about where this wheel may go"
    )
    assert tags[0].split("-")[1] == "abi3", (
        f"WHEEL's tag is {tags[0]!r}, whose ABI component is not abi3"
    )
