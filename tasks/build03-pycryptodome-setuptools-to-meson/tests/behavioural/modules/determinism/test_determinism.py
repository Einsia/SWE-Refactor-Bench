"""Two builds of the same sources, compared to each other.

Every other module in this stage reads one build and compares it to State A's
frozen record. That is only meaningful if a second build would have produced the
same thing -- otherwise a passing suite means "this build happened to match" and a
failing one means nothing in particular.

So the `repeat` configuration builds the same tree a second time, in a separate
directory, and this module asks whether the two agree. What it is looking for is
the class of build system that works and is still not finished: a file list that
depends on directory iteration order, a generated header written from a dict, a
version stamped from the wall clock, an object whose contents shift with the path
it was compiled in. Each of those produces a build that passes today and a
different build tomorrow, and each is invisible to a suite that builds once.

The comparison is over the payload, not the archive bytes. Zip entry order and
timestamps are the packaging tool's business and meson-python already pins them
from SOURCE_DATE_EPOCH; what has to be stable is the set of files, their contents,
and what the compiled objects export.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import elfutil
import wheelutil


@pytest.fixture(scope="module")
def pair(built):
    """The two builds, or an AssertionError naming whichever one did not happen."""
    return built("default"), built("repeat")


def _payload_digests(wheel_path: Path) -> dict[str, str]:
    wheel = wheelutil.Wheel(wheel_path)
    try:
        return {
            name: hashlib.sha256(wheel.zip.read(name)).hexdigest()
            for name in wheel.payload
        }
    finally:
        wheel.close()


def test_both_builds_of_the_same_sources_succeeded(pair):
    """A build that works once and fails once is the most expensive kind to debug."""
    first, second = pair
    assert first.ok and second.ok, (
        f"the same sources built {'once' if first.ok or second.ok else 'never'} out of "
        f"twice: default rc={first.rc} ({first.note or 'ok'}), repeat rc={second.rc} "
        f"({second.note or 'ok'}). Nothing else in this stage can be trusted until the "
        f"same input gives the same outcome."
    )


def test_the_two_wheels_ship_the_same_files(pair):
    """The member list, as a set. A build ordered by `os.listdir` drifts here."""
    first, second = pair
    a = wheelutil.Wheel(Path(first.wheel))
    b = wheelutil.Wheel(Path(second.wheel))
    try:
        one, two = set(a.payload), set(b.payload)
    finally:
        a.close()
        b.close()
    only_first, only_second = sorted(one - two), sorted(two - one)
    assert not only_first and not only_second, (
        f"two builds of the same sources shipped different files.\n"
        f"  only in the first: {', '.join(only_first[:8]) or 'none'}\n"
        f"  only in the second: {', '.join(only_second[:8]) or 'none'}\n"
        f"A file list that changes between builds means the one every other module in "
        f"this stage graded was one of several possible answers."
    )


def test_the_pure_python_files_are_byte_identical(pair):
    """`.py`, `.pyi`, `py.typed`: copied, not generated, so they have no excuse to differ.

    A difference here is a build system rewriting sources on the way through --
    substituting a version, stripping a line, normalising an encoding -- which
    makes the installed library something other than what was submitted.
    """
    first, second = pair
    one, two = _payload_digests(Path(first.wheel)), _payload_digests(Path(second.wheel))
    shared = sorted(set(one) & set(two))
    differing = [
        name for name in shared
        if name.endswith((".py", ".pyi")) or name.endswith("py.typed")
        if one[name] != two[name]
    ]
    assert not differing, (
        f"{len(differing)} text files differ between two builds of the same sources: "
        f"{', '.join(differing[:8])}. These are copied into the wheel, so something in the "
        f"build is rewriting them."
    )


def test_the_compiled_objects_export_the_same_symbols(pair):
    """`.dynsym`, per object. This is the ABI, and it must not depend on the build.

    Byte-identical objects are a stronger property and not one this asks for:
    reproducible builds need `-ffile-prefix-map` and a fixed build path, and
    neither is part of migrating a build system. What must hold is that the
    interface Python's ctypes looks up is the same one twice.
    """
    first, second = pair
    one, two = Path(first.install), Path(second.install)
    objects = sorted(p.relative_to(one).as_posix() for p in one.rglob("*.so"))
    assert objects, f"the first build installed no shared objects under {one}"

    problems = []
    for rel in objects:
        other = two / rel
        if not other.is_file():
            problems.append(f"{rel} is missing from the second build")
            continue
        a = {s for s in elfutil.dynamic_symbols(one / rel) if not s.startswith("_")}
        b = {s for s in elfutil.dynamic_symbols(other) if not s.startswith("_")}
        if a != b:
            problems.append(
                f"{rel} exports {len(a)} symbols in one build and {len(b)} in the other "
                f"(differing: {', '.join(sorted(a ^ b)[:4])})"
            )
    assert not problems, (
        f"the compiled interface is not stable across builds:\n  " + "\n  ".join(problems[:8])
    )


def test_the_two_wheels_carry_the_same_metadata(pair):
    """METADATA, field by field. A version from `git describe` or the clock drifts here."""
    first, second = pair
    a = wheelutil.Wheel(Path(first.wheel))
    b = wheelutil.Wheel(Path(second.wheel))
    try:
        one, two = a.fields(), b.fields()
    finally:
        a.close()
        b.close()
    differing = sorted(
        key for key in set(one) | set(two)
        if key != "description" and one.get(key) != two.get(key)
    )
    assert not differing, (
        f"METADATA differs between two builds of the same sources in {differing}: "
        + "; ".join(f"{k}: {one.get(k)!r} vs {two.get(k)!r}" for k in differing[:4])
    )


def test_the_two_wheels_are_named_the_same(pair):
    """Same filename. A tag computed from the environment rather than the project drifts."""
    first, second = pair
    a, b = Path(first.wheel).name, Path(second.wheel).name
    assert a == b, (
        f"two builds of the same sources produced differently named wheels, {a} and {b}, so "
        f"which one an installer picks depends on which build ran"
    )


def test_the_same_number_of_objects_was_compiled_both_times(pair):
    """A count, separately from the name comparison, because it localises the fault.

    Same names but a different count cannot happen; a different count with the same
    names means one build installed something outside the wheel's payload.
    """
    first, second = pair
    one = sorted(p.name for p in Path(first.install).rglob("*.so"))
    two = sorted(p.name for p in Path(second.install).rglob("*.so"))
    assert len(one) == len(two), (
        f"the first build installed {len(one)} shared objects and the second installed "
        f"{len(two)}; a build whose artifact count varies has a race or an ordering "
        f"dependency in it"
    )
