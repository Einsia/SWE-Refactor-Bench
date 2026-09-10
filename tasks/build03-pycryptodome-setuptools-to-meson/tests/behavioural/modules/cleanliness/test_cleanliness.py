"""What the build did besides producing a wheel.

Two builds can deliver the same 330 files and still not be the same build. One
corrupts the directory it was handed, bakes the path it ran in into the objects it
ships, and hands the user packaging debris. The other does not. None of that shows
up in a member list.

The checks here are each a consequence with a name:

*A build-tree RPATH on an installed object* means the object searches a directory
that exists on the build machine and nowhere else. When that directory happens to
exist on the user's machine, it loads whatever is in it.

*A baked build path* in a shipped `.py` or in an object's runtime strings is a
machine-specific path delivered to every user.

*A symlink leaving the install tree* is a file that stops resolving the moment the
build directory is cleaned up.

*Packaging debris in the payload* is `egg-info` and friends installed into
site-packages, where nothing reads them.

Those four are ways a build misbehaves whoever wrote it. State A does not do them,
and they are scored.

*Touching the source tree* is three questions rather than one, and only the third
is answered differently by the two states.

Modifying or deleting a delivered file is corruption under any build system: the
second build in that directory builds something else, and the stale result is
usually right, which is what makes it a bug that hides itself. Scored.

*Where* the build put whatever it did create is also scored, because State A passes
it -- all 382 of its files land under ``build/`` or ``lib/pycryptodome.egg-info/``.
A build that drops a generated `.py` into ``lib/Crypto/`` instead is writing into a
directory the next build reads as input.

*Whether* it created anything at all -- a fully out-of-tree build -- is what
``setup.py bdist_wheel`` cannot satisfy, so that one is recorded at weight 0.0 with
the count kept. The tree it happens in is a private copy the ``build`` module made,
and ``task.toml``'s artefact exclusions drop all of it before a submission is ever
collected.

*Which backend built the wheel* is likewise recorded at weight 0.0. Its subject is
whether the migration happened, which ``setuptools_retired`` decides in stage 1
over both trees with a reviewer, and State A -- the oracle every other number in
this stage is compared against -- answers it "setuptools" by construction. The
image installs both backends precisely so State A can be built in it; an image that
could not build the reference could not check a single recording in ``data/``.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import elfutil

#: The backends this image installs, one per state.  Both are present so the front
#: end -- `python -m build --wheel --no-isolation`, which never names a backend --
#: can reach whichever one a tree declares.
BACKENDS = ("setuptools", "mesonpy")

#: Where a build system is allowed to put its own scratch work if it puts any in
#: the tree at all.  Every one of these is in `environment/gitignore.baseline` and
#: in `task.toml`'s artefact exclusions, so none can reach a verifier as part of a
#: submission.
#:
#: Measured against State A rather than guessed: `setup.py bdist_wheel` on this
#: tree creates 382 files, 377 of them under `build/` (`lib.linux-x86_64-cpython-312`
#: and `temp.linux-x86_64-cpython-312`) and 5 under `lib/pycryptodome.egg-info/`,
#: which is in `lib/` because setup.py sets `package_dir={'': 'lib'}`.  Nothing
#: outside those two places.  The Meson names are here because a submission that
#: configures in-tree will use one of them.
#:
#: Deliberately no suffix clause: a `.so` or a `.o` written next to the `.c` it was
#: compiled from is a build with no scratch directory at all, and that is a finding
#: rather than something to excuse.  Used only to classify what was *created*, never
#: to excuse a modification or a deletion.
SCRATCH_PREFIXES = ("build/", "dist/", "_build/", "builddir/", "meson-logs/",
                    "meson-info/", "meson-private/", ".mesonpy-")


@pytest.fixture(scope="module")
def default(built):
    return built("default")


def _is_scratch(rel: str) -> bool:
    return (rel.startswith(SCRATCH_PREFIXES)
            or ".egg-info/" in rel
            or "/.mesonpy-" in rel)


def _manifest(root: Path) -> dict[str, str]:
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", "__pycache__/")) or "/__pycache__/" in rel:
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.mark.parametrize("name", BACKENDS)
def test_both_backends_are_available_in_this_image(name):
    """The premise every measurement in this stage rests on.

    A failure here is an image fault, not a submission's, and the two directions
    cost different things.  Without `setuptools` State A cannot be built in the
    image that grades every submission against State A's recorded numbers, so
    nothing in ``data/`` is checkable here.  Without `mesonpy` every migrated
    submission fails to build and is reported as broken.

    Checked with `python -I` so a stray `sitecustomize` or a `.pth` in the
    submission's install tree cannot change the answer.  The Dockerfile asserts the
    same thing at image-build time, and by building through each backend rather
    than importing it; this catches an image that changed under the suite's feet.
    """
    proc = subprocess.run(
        [sys.executable, "-I", "-c", f"import {name}"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"{name} is not importable in the behavioural image. The front end runs "
        f"`--no-isolation` and will not fetch a backend, so a tree that declares "
        f"{name} cannot be built here at all. This is a fault in the image, not in the "
        f"submission: {proc.stderr.strip()[:300]}"
    )


@pytest.mark.srb_weight(0.0)
@pytest.mark.migration
def test_the_wheel_was_built_with_the_backend_the_tree_declares(default):
    """Which backend produced the wheel, read out of the wheel rather than the tree.

    Recorded at weight 0.0 and scored either way.  Its subject is whether the
    migration happened, and State A -- the oracle every scored number in this stage
    compares against -- answers it "setuptools".  Charging for it would make the
    suite unsatisfiable by the tree that defines what passing means.
    ``setuptools_retired`` and ``meson_is_the_build`` in stage 1 own the judgement,
    over both trees, with a reviewer, and a gate failure there scores the submission
    zero before this image runs.

    Kept as a real verdict rather than a note because the evidence is worth having
    in the report: `WHEEL`'s `Generator` field is what the backend wrote about
    itself, and it is one line of ground truth about which build system ran that no
    reading of the source can contradict.
    """
    assert default.ok, (
        f"the default configuration produced no wheel to read a Generator out of "
        f"(rc={default.rc}, {default.note or 'no note'}, log: {default.log})"
    )
    assert Path(default.wheel).is_file(), f"the ledger names {default.wheel}, which is not there"

    import wheelutil

    wheel = wheelutil.Wheel(Path(default.wheel))
    try:
        generator = (wheel.wheel_metadata.get("Generator") or "").strip()
    finally:
        wheel.close()
    assert generator and "bdist_wheel" not in generator, (
        f"the wheel's WHEEL file names {generator!r} as its Generator, which is the "
        f"setuptools build path. State A's records `bdist_wheel (0.42.0)`; a migrated "
        f"tree records its own backend. Stage 1 decides what this means for the score."
    )


def test_the_build_did_not_modify_or_delete_a_delivered_file(default, pre_build_snapshot):
    """Corruption of the tree, as opposed to scratch work in it.

    A build that rewrites or removes one of the files it was handed makes the second
    build in that directory build something else, and the stale result is usually
    right, which is what makes it a bug that hides itself. No build system has any
    business doing this and State A does not, so it is scored.

    The tree compared here is the private copy the `build` module made, so this
    measures what building did to it -- not what the submission contains, which is
    stage 1's question.
    """
    tree = Path(default.tree)
    if not tree.is_dir():
        pytest.fail(f"the default configuration's source tree is not at {tree}")

    before = pre_build_snapshot["files"]
    after = _manifest(tree)
    changed = sorted(p for p in set(after) & set(before) if after[p] != before[p])
    removed = sorted(set(before) - set(after))

    assert not changed, (
        f"building modified {len(changed)} files in the source tree it was handed: "
        f"{', '.join(changed[:10])}. A second build in that directory would build "
        f"something else."
    )
    assert not removed, (
        f"building deleted {len(removed)} files from the source tree: "
        f"{', '.join(removed[:10])}"
    )


def test_the_build_wrote_nothing_outside_its_own_scratch_directories(default,
                                                                    pre_build_snapshot):
    """Where the build's leftovers landed, as opposed to whether there were any.

    Scored, and State A passes it: its 382 created files are all under `build/` or
    `lib/pycryptodome.egg-info/`. A build that instead drops a generated `.py` into
    `lib/Crypto/`, or compiles objects next to the `.c` files, is writing into a
    directory the next build reads as input -- and `SCRATCH_PREFIXES` deliberately
    does not excuse an object by its suffix, only by where it sits.

    The stronger property -- that the build put nothing in the tree at all -- is
    recorded rather than scored, in the check below.
    """
    tree = Path(default.tree)
    if not tree.is_dir():
        pytest.fail(f"the default configuration's source tree is not at {tree}")

    created = sorted(set(_manifest(tree)) - set(pre_build_snapshot["files"]))
    stray = [p for p in created if not _is_scratch(p)]
    assert not stray, (
        f"building wrote {len(stray)} of its {len(created)} new files outside any "
        f"recognised build directory: {', '.join(stray[:10])}. Those paths are read "
        f"as input by the next build, and none of them is excluded from a submission."
    )


@pytest.mark.srb_weight(0.0)
@pytest.mark.migration
def test_the_build_created_nothing_inside_the_tree_it_was_given(default, pre_build_snapshot):
    """A fully out-of-tree build. Recorded, with the count and the paths.

    Weight 0.0 because this is what the retired build system does: `setup.py
    bdist_wheel` writes `build/` and `lib/pycryptodome.egg-info/` into the directory
    it runs in, so State A cannot pass it, and instruction.md states no out-of-tree
    contract to the agent. Everything either build system creates here is in
    `environment/gitignore.baseline` and in `task.toml`'s artefact exclusions, so
    none of it can reach a verifier as part of a submission, and every configuration
    in this stage builds in its own copy so none of it can reach another build.

    Recorded rather than dropped because the number is worth reading next to the
    scored check above: 382 files says setuptools, none says a backend that
    configured somewhere else.
    """
    tree = Path(default.tree)
    if not tree.is_dir():
        pytest.fail(f"the default configuration's source tree is not at {tree}")

    created = sorted(set(_manifest(tree)) - set(pre_build_snapshot["files"]))
    assert not created, (
        f"building wrote {len(created)} new files into the source tree it was handed, "
        f"all of them inside recognised build directories: {', '.join(created[:6])}"
        + (f" ... and {len(created) - 6} more" if len(created) > 6 else "")
    )


def test_no_installed_object_searches_the_build_directory(default):
    """RPATH and RUNPATH, per object.

    Meson sets an RPATH on binaries in the build tree so they find their
    co-located libraries, and strips it when installing. An installed object that
    still carries one is one that was copied out of the build tree by hand, and it
    will search a path that means nothing on the user's machine -- or something
    unintended, if that path happens to exist.
    """
    install = Path(default.install)
    objects = sorted(install.rglob("*.so"))
    assert objects, f"no shared objects under {install}"

    offenders = []
    for obj in objects:
        paths = elfutil.run_paths(obj)
        if paths:
            offenders.append(f"{obj.relative_to(install).as_posix()} -> {':'.join(paths)}")
    assert not offenders, (
        f"{len(offenders)} installed objects carry a hard-coded library search path:\n  "
        + "\n  ".join(offenders[:8])
        + "\nEvery library these need is found through the normal search path in State A."
    )


def test_no_installed_file_names_the_directory_it_was_built_in(default):
    """The build path, searched for in what got shipped.

    A generated `.py` holding `/build/src/...`, or an object with the build root in
    a runtime string, ships a machine-specific path to every user. Debug info is
    not searched: DWARF legitimately records where compilation happened, and
    changing that needs `-ffile-prefix-map`, which is not part of migrating a build
    system.
    """
    install, tree = Path(default.install), Path(default.tree)
    needle = str(tree).encode()
    if len(needle) < 8:
        pytest.skip(f"the build tree path {tree} is too short to search for safely")

    offenders = []
    for path in sorted(install.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix in {".py", ".pyi"} or path.name == "py.typed":
            if needle in path.read_bytes():
                offenders.append(path.relative_to(install).as_posix())
        elif path.suffix == ".so":
            strings = elfutil.run_paths(path) + [elfutil.soname(path) or ""]
            if any(str(tree) in s for s in strings):
                offenders.append(path.relative_to(install).as_posix())
    assert not offenders, (
        f"{len(offenders)} installed files name the directory the build ran in "
        f"({tree}): {', '.join(offenders[:8])}"
    )


def test_the_install_tree_holds_no_symlink_pointing_outside_itself(default):
    """A symlink into the build tree is a file that disappears when the build dir does."""
    install = Path(default.install)
    dangling = []
    for path in sorted(install.rglob("*")):
        if not path.is_symlink():
            continue
        target = (path.parent / os.readlink(path)).resolve()
        if not str(target).startswith(str(install.resolve())) or not target.exists():
            dangling.append(f"{path.relative_to(install).as_posix()} -> {os.readlink(path)}")
    assert not dangling, (
        f"{len(dangling)} installed entries are symlinks leaving the install tree, so they "
        f"stop resolving once the build directory is cleaned up: {', '.join(dangling[:6])}"
    )


def test_the_build_did_not_leave_a_setuptools_footprint_in_the_wheel(default):
    """`.egg-info`, `.egg-link`, `setup.py` inside the delivered wheel's payload.

    Distinct from the tree checks above: this is about what a user receives. An
    `egg-info` directory inside the wheel is a build artifact of a tool this
    project no longer uses, and pip will happily install it into site-packages.
    """
    import wheelutil

    wheel = wheelutil.Wheel(Path(default.wheel))
    try:
        payload = wheel.payload
    finally:
        wheel.close()
    footprint = sorted(
        n for n in payload
        if ".egg-info" in n or n.endswith((".egg-link", ".pth"))
        or n.rsplit("/", 1)[-1] in {"setup.py", "setup.cfg", "MANIFEST.in"}
    )
    assert not footprint, (
        f"the wheel's payload carries {len(footprint)} packaging artifacts that would be "
        f"installed into site-packages: {', '.join(footprint[:8])}"
    )
