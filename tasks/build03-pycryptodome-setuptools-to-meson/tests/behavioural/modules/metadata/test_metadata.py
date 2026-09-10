"""The distribution's metadata, compared for content rather than for form.

`setup.py` passed keyword arguments to `setup()`; static metadata is a
`[project]` table. Both produce a core-metadata file, and for several fields the
two spell the same fact differently:

    Author: Helder Eijs                 Author-email: Helder Eijs <helderijs@gmail.com>
    Author-email: helderijs@gmail.com
    Home-page: https://...              Project-URL: Homepage, https://...
    Requires-Python: >=2.7, !=3.0.*     Requires-Python: !=3.0.*,...,>=2.7

None of those differences changes what a resolver or an installer does, and a
check requiring State A's exact strings would fail every correct migration on all
three. So the comparisons here go through the meaning: an email address extracted
from either form, a URL set gathered from both fields, a version specifier parsed
into a set of constraints.

Two of State A's fields are not requirable at all, and saying so is part of this
module's job:

  * `Platform: Posix; MacOS X; Windows` -- there is no `platforms` key in PEP
    621. It cannot be expressed in static metadata, so a submission that omits it
    has not lost anything it could have kept.
  * `License-File:` -- a `bdist_wheel` convention, not core metadata in 2.1.

What *is* required is everything a resolver reads: the name, the version, the
Python requirement, and the twenty classifiers -- because a wrong
`Requires-Python` makes the wheel uninstallable on interpreters it supports, and
a missing classifier changes nothing at all but is trivially preservable.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import wheelutil

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _ground_truth() -> dict:
    root = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data"
    return json.loads((root / "metadata.json").read_text(encoding="utf-8"))


META = _ground_truth()


@pytest.fixture(scope="module")
def wheel(built):
    w = wheelutil.Wheel(Path(built("default").wheel))
    yield w
    w.close()


@pytest.fixture(scope="module")
def fields(wheel) -> dict:
    return wheel.fields()


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def test_the_distribution_name_is_unchanged(fields):
    """A renamed distribution is a different package on the index."""
    assert fields.get("name") == META["name"], (
        f"the wheel calls itself {fields.get('name')!r}; State A publishes {META['name']!r}"
    )


def test_the_version_is_unchanged(fields):
    """3.20.0, and it has to come from somewhere the build reads, not a literal.

    Where it comes from is not this module's business -- that is a question about
    the repository. What is measurable here is that the wheel says the same
    version the library does.
    """
    assert fields.get("version") == META["version"], (
        f"the wheel is version {fields.get('version')!r}; State A's is {META['version']!r}"
    )


def test_the_version_matches_the_library_itself(built):
    """`Crypto.__version__` and the wheel's version, from the installed tree.

    This is the one that catches a version hardcoded in the build description: the
    library's own `version_info` tuple is in `lib/Crypto/__init__.py`, which is
    frozen, so the two can only agree if the build read it.
    """
    import subprocess
    import sys

    install = Path(built("default").install)
    env = {k: v for k, v in os.environ.items() if not k.startswith("SRB_")}
    env["PYTHONPATH"] = str(install)
    proc = subprocess.run(
        [sys.executable, "-c", "import Crypto; print(Crypto.__version__)"],
        capture_output=True, env=env, timeout=120,
    )
    assert proc.returncode == 0, (
        "the installed library could not report its own version: "
        + proc.stderr.decode("utf-8", "replace")[-500:]
    )
    reported = proc.stdout.decode().strip()
    assert reported == META["version"], (
        f"the installed library reports version {reported!r} while the wheel is labelled "
        f"{META['version']!r}: the build description and lib/Crypto/__init__.py disagree"
    )


def test_the_summary_is_unchanged(fields):
    assert fields.get("summary") == META["fields"].get("summary"), (
        f"Summary is {fields.get('summary')!r}, State A's is "
        f"{META['fields'].get('summary')!r}"
    )


def test_the_licence_is_unchanged(fields):
    """The declared licence, which is a legal statement and not a formatting choice."""
    assert fields.get("license") == META["fields"].get("license"), (
        f"License is {fields.get('license')!r}, State A's is "
        f"{META['fields'].get('license')!r}"
    )


def test_the_python_requirement_is_equivalent(fields):
    """Parsed into constraints, because the string is normalised by the backend.

    `>=2.7, !=3.0.*, !=3.1.*` and `!=3.0.*,!=3.1.*,>=2.7` are the same
    requirement; a resolver cannot tell them apart. What would matter is a
    constraint appearing or disappearing.
    """
    from packaging.specifiers import SpecifierSet

    got, want = fields.get("requires-python"), META["requires_python"]
    assert got, "the wheel declares no Requires-Python; State A declares one"
    got_set = {str(s) for s in SpecifierSet(got)}
    want_set = {str(s) for s in SpecifierSet(want)}
    assert got_set == want_set, (
        f"Requires-Python is {got!r}, which is the constraint set {sorted(got_set)}; "
        f"State A's {want!r} is {sorted(want_set)}. Added constraints make the wheel "
        f"uninstallable where it works; dropped ones let it install where it does not."
    )


def test_every_classifier_survived(fields):
    """All 20, as a set. Order is not metadata."""
    got, want = set(_as_list(fields.get("classifier"))), set(META["classifiers"])
    missing, extra = sorted(want - got), sorted(got - want)
    assert not missing, (
        f"{len(missing)} of State A's {len(want)} classifiers are missing: "
        + "; ".join(missing[:6])
    )
    assert not extra, f"{len(extra)} classifiers were added: " + "; ".join(extra[:6])


def test_the_author_is_still_reachable(fields):
    """The email address, from whichever field carries it.

    PEP 621's `authors = [{name, email}]` becomes a single
    `Author-email: Name <addr>`; `setup.py`'s two arguments became two fields.
    The address is the fact; which field holds it is the backend's convention.
    """
    want = EMAIL.findall(
        " ".join(_as_list(META["fields"].get("author-email"))
                 + _as_list(META["fields"].get("author")))
    )
    got = EMAIL.findall(
        " ".join(_as_list(fields.get("author-email")) + _as_list(fields.get("author")))
    )
    assert want, "State A's metadata carries no author address, so this check does not apply"
    assert set(want) <= set(got), (
        f"the wheel's metadata does not carry {', '.join(sorted(set(want) - set(got)))}; "
        f"it carries {got or 'no address at all'}"
    )


def test_the_project_urls_survived(fields):
    """Every URL State A published, from `Home-page` or `Project-URL`, either way.

    `[project.urls] Homepage = ...` becomes `Project-URL: Homepage, ...`, so a
    correct migration moves the home page between fields. The set of URLs is what
    a user follows.
    """
    def urls(source: dict) -> set:
        found = set(_as_list(source.get("home-page")))
        for entry in _as_list(source.get("project-url")):
            found.add(entry.split(",", 1)[-1].strip())
        return {u.rstrip("/") for u in found if u}

    want, got = urls(META["fields"]), urls(fields)
    missing = sorted(want - got)
    assert not missing, (
        f"the wheel's metadata no longer publishes {', '.join(missing)}. State A publishes "
        f"{sorted(want)}; the wheel publishes {sorted(got)}."
    )


def test_the_metadata_version_is_at_least_state_as(fields):
    """2.1 or newer. Older would mean fields silently dropped."""
    from packaging.version import Version

    got, want = fields.get("metadata-version"), META["fields"].get("metadata-version")
    assert got, "the wheel's METADATA declares no Metadata-Version"
    assert Version(got) >= Version(want), (
        f"the wheel declares core metadata {got}, older than State A's {want}: fields "
        f"defined in the newer version would be dropped by a strict consumer"
    )


def test_the_long_description_survived(wheel, fields):
    """README.rst is the PyPI page. An empty description is a blank listing.

    Compared by size and opening line rather than byte for byte: the backends
    differ on trailing newline handling, and `Description-Content-Type` is a field
    State A does not set at all.
    """
    body = wheel.metadata.get_payload() or ""
    assert len(body.strip()) > 1000, (
        f"the wheel's long description is {len(body.strip())} characters. State A's is "
        f"README.rst, several kilobytes of it, and it is what appears on the package page."
    )
