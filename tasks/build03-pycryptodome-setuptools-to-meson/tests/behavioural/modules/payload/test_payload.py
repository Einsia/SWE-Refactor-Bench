"""The pure-Python half of the wheel: 192 modules, 96 stubs, py.typed, 20 packages.

Most of pycryptodome is Python. Migrating the build system is a chance to lose
some of it silently: `setup.py` listed its packages explicitly, and a build system
that lists them again can miss one, while one that globs can pick up
`Crypto/SelfTest`'s data or a stray `conftest.py`.

Neither failure shows up in a test run. A missing `.pyi` breaks type checking for
everyone downstream and nothing else. A missing package under `Crypto.SelfTest`
means the suite silently collects fewer vectors -- which the selftest module sees
as a smaller collection, but only because this project happens to ship its tests.
The check that catches it directly is this one: the installed file set, against
State A's, name by name.

`py.typed` gets its own check because it is a zero-byte file whose absence turns
all 96 stubs off at once, and because a build system that copies only `*.py` and
`*.pyi` will drop it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _ground_truth() -> dict:
    root = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data"
    return json.loads((root / "stubs.json").read_text(encoding="utf-8"))


STUBS = _ground_truth()
EXPECTED_PACKAGES = tuple(STUBS["packages"])


@pytest.fixture(scope="module")
def install(built) -> Path:
    return Path(built("default").install)


@pytest.fixture(scope="module")
def installed(install) -> dict:
    """Every installed file under Crypto/, by suffix, as relative posix paths."""
    out: dict[str, set] = {".py": set(), ".pyi": set(), "other": set()}
    for path in (install / "Crypto").rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(install).as_posix()
        if "__pycache__/" in rel:
            continue
        out[path.suffix if path.suffix in out else "other"].add(rel)
    return out


def test_the_payload_ground_truth_is_present():
    assert STUBS["n_py"] == 192 and STUBS["n_pyi"] == 96 and STUBS["n_packages"] == 20, (
        f"State A's frozen payload record says {STUBS['n_py']} modules, {STUBS['n_pyi']} stubs, "
        f"{STUBS['n_packages']} packages; this module was written for 192/96/20"
    )


def test_every_python_module_was_installed(installed):
    """192 files, as a set. An extra one is as visible as a missing one."""
    want, got = set(STUBS["py"]), installed[".py"]
    missing, extra = sorted(want - got), sorted(got - want)
    assert not missing, (
        f"{len(missing)} of State A's {len(want)} Python modules were not installed: "
        f"{', '.join(missing[:8])}"
    )
    assert not extra, (
        f"{len(extra)} Python files were installed that State A does not ship: "
        f"{', '.join(extra[:8])}. A build that globs the source tree picks up whatever "
        f"is sitting in it."
    )


def test_every_type_stub_was_installed(installed):
    """96 .pyi files. Their absence is invisible at runtime and total for a type checker."""
    want, got = set(STUBS["pyi"]), installed[".pyi"]
    missing, extra = sorted(want - got), sorted(got - want)
    assert not missing, (
        f"{len(missing)} of State A's {len(want)} type stubs were not installed: "
        f"{', '.join(missing[:8])}. Nothing at runtime notices; every downstream "
        f"`mypy` run does."
    )
    assert not extra, f"{len(extra)} unexpected stub files were installed: {', '.join(extra[:8])}"


def test_the_typing_marker_was_installed(install):
    """PEP 561. One empty file; without it the 96 stubs are ignored entirely."""
    marker = install / "Crypto/py.typed"
    assert STUBS["py_typed"], "State A does not ship py.typed, so this check does not apply"
    assert marker.is_file(), (
        "Crypto/py.typed was not installed. PEP 561 says a type checker must ignore the "
        "stubs of a package without it, so all 96 .pyi files stop counting."
    )


@pytest.mark.parametrize("package", EXPECTED_PACKAGES)
def test_package_is_importable_from_the_install_tree(package, install):
    """Each of the 20 packages has its `__init__.py` where an import will look.

    Parametrised by package rather than checked as a set, because the fix is
    per-package: a build system's package list is where one goes missing, and the
    failing check names it.
    """
    rel = package.replace(".", "/") + "/__init__.py"
    assert (install / rel).is_file(), (
        f"{package} has no {rel} in the install tree, so `import {package}` fails "
        f"even though its modules may be present"
    )


def test_no_test_vectors_or_caches_were_packaged(installed, install):
    """State A ships no vectors, no bytecode, no build leftovers under Crypto/.

    The vectors come from `pycryptodome_test_vectors`, a separate distribution.
    A build that globbed `lib/` into the wheel would ship whatever the working
    tree happened to contain -- and on a machine where the suite had been run,
    that is several hundred megabytes of response files.
    """
    unexpected = sorted(
        rel for rel in installed["other"]
        if not rel.endswith((".so", "py.typed"))
    )
    assert not unexpected, (
        f"{len(unexpected)} files were installed under Crypto/ that are neither Python, "
        f"stubs, compiled objects nor py.typed: {', '.join(unexpected[:10])}"
    )


def test_no_bytecode_was_shipped_in_the_wheel(built):
    """`.pyc` in a wheel is stale the moment the interpreter differs.

    Checked on the archive rather than the install tree, because pip is entitled
    to compile on install -- and does, unless told not to. What matters is
    whether the *wheel* carries them.
    """
    import wheelutil

    wheel = wheelutil.Wheel(Path(built("default").wheel))
    try:
        pyc = [n for n in wheel.payload if n.endswith((".pyc", ".pyo")) or "__pycache__/" in n]
    finally:
        wheel.close()
    assert not pyc, (
        f"the wheel ships {len(pyc)} bytecode files: {', '.join(pyc[:6])}. They are keyed to "
        f"one interpreter version and one source timestamp, and a wheel tagged for the "
        f"stable ABI is installed on interpreters they do not match."
    )
