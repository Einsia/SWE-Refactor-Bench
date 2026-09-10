"""The 41 compiled libraries: are they there, and can the library load them.

pycryptodome does not import its C code. Every one of these objects is opened at
runtime by ctypes, through `Crypto.Util._raw_api.load_pycryptodome_raw_lib`,
which walks a list of suffixes and gives up with `OSError` if none of them
resolves. So an object placed one directory too high, or named with the wrong
suffix, produces a library that imports fine and raises the moment anything
tries to encrypt.

That loader is why the check here goes through it rather than around it. A test
that only listed files would pass on a tree where all 41 objects exist under
paths ctypes will never look at.

The 47-to-41 gap is real and expected: six translation units are compiled into
other objects rather than into one of their own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import elfutil

#: Every logical library name the delivered .py files ask ctypes for, derived at
#: import time from State A's frozen artefact list rather than typed out.
def _expected_objects():
    path = Path(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")) / "data/elf.json"
    if not path.is_file():
        return []
    return sorted(json.loads(path.read_text(encoding="utf-8")))


EXPECTED_OBJECTS = _expected_objects()

#: The loader's own suffix list, in the order it tries them. Not a guess: this is
#: what `_raw_api` does with `importlib.machinery.EXTENSION_SUFFIXES`.
LOADER_PROBE = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from Crypto.Util._raw_api import load_pycryptodome_raw_lib
out = {}
for spec in json.loads(sys.argv[2]):
    module, symbol = spec
    try:
        lib = load_pycryptodome_raw_lib(module, "")
        out[module] = "loaded" if lib is not None else "returned None"
    except Exception as exc:
        out[module] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def install(built) -> Path:
    return Path(built("default").install)


@pytest.fixture(scope="module")
def delivered(install) -> dict:
    """{relative path: absolute path} for every shared object under the install tree."""
    return {
        p.relative_to(install).as_posix(): p
        for p in install.rglob("*.so")
        if p.is_file()
    }


def test_the_install_tree_exists(install):
    assert install.is_dir(), f"the default configuration installed nothing at {install}"
    assert (install / "Crypto").is_dir(), (
        f"{install} holds no Crypto package, so the wheel installed something else"
    )


def test_the_expected_artefact_list_is_present():
    assert len(EXPECTED_OBJECTS) == 41, (
        f"State A's frozen artefact list holds {len(EXPECTED_OBJECTS)} objects, expected 41; "
        f"without it every check in this module would pass vacuously"
    )


def test_the_artefact_count_matches(delivered):
    """41, exactly. Both directions matter.

    Missing objects mean capabilities that will raise at runtime. Extra ones
    usually mean a build that compiled a translation unit into its own library
    when State A folded it into another -- a different loading surface, and on
    this project the difference between one AES implementation and two.
    """
    got, want = set(delivered), set(EXPECTED_OBJECTS)
    missing, extra = sorted(want - got), sorted(got - want)
    assert not missing and not extra, (
        f"the installed tree holds {len(got)} shared objects, State A installs {len(want)}."
        + (f" Missing: {', '.join(missing[:8])}." if missing else "")
        + (f" Unexpected: {', '.join(extra[:8])}." if extra else "")
    )


@pytest.mark.parametrize("rel", EXPECTED_OBJECTS)
def test_artefact_is_an_elf_shared_object(rel, delivered):
    """Present, a real ELF DYN, and not a stub.

    A 200-byte file with the right name satisfies a file-existence check and
    fails the first ctypes call, so the size floor is part of the measurement.
    """
    path = delivered.get(rel)
    assert path is not None, f"{rel} was not installed"
    assert path.stat().st_size > 4096, (
        f"{rel} is {path.stat().st_size} bytes, too small to be a compiled "
        f"implementation of anything"
    )
    assert elfutil.elf_type(path) == "DYN", (
        f"{rel} is an ELF {elfutil.elf_type(path)}, not a shared object ctypes can open"
    )


def test_every_library_loads_through_the_ctypes_loader(install, data):
    """The project's own loader, on the installed tree, for all 41 names.

    This is the check that a file listing cannot replace: it asks the question
    the library asks at runtime, using the library's own code to ask it.
    """
    modules = sorted({
        # `Crypto/Cipher/_raw_aes.abi3.so` -> `Crypto.Cipher._raw_aes`
        rel.rsplit("/", 1)[0].replace("/", ".") + "." + rel.rsplit("/", 1)[1].split(".")[0]
        for rel in EXPECTED_OBJECTS
    })
    env = {k: v for k, v in os.environ.items() if not k.startswith("SRB_")}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    proc = subprocess.run(
        [sys.executable, "-c", LOADER_PROBE, str(install),
         json.dumps([[m, ""] for m in modules])],
        capture_output=True, env=env, timeout=600,
    )
    assert proc.returncode == 0, (
        "the installed library's own ctypes loader could not be exercised: "
        + proc.stderr.decode("utf-8", "replace")[-800:]
    )
    outcomes = json.loads(proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    broken = {m: r for m, r in outcomes.items() if r != "loaded"}
    assert not broken, (
        f"{len(broken)} of {len(modules)} compiled libraries do not resolve through "
        f"load_pycryptodome_raw_lib, so the installed library imports and then fails at "
        f"first use: " + "; ".join(f"{m} -> {r}" for m, r in sorted(broken.items())[:6])
    )


def test_no_compiled_artefact_landed_outside_the_package(install):
    """Everything compiled belongs under Crypto/. Nothing beside it.

    A build that installs its objects into the target root, or into a
    `pycryptodome.libs` directory next to the package, produces a wheel whose
    files ctypes will not find and whose uninstall leaves them behind.
    """
    stray = sorted(
        p.relative_to(install).as_posix()
        for p in install.rglob("*.so")
        if p.is_file() and not p.relative_to(install).as_posix().startswith("Crypto/")
    )
    assert not stray, (
        f"{len(stray)} shared objects were installed outside the Crypto package: "
        f"{', '.join(stray[:8])}"
    )


def test_no_static_archive_or_object_file_was_installed(install):
    """A wheel ships loadable code, not build leftovers.

    .a and .o under the install tree mean the build's intermediate directory was
    packaged. Harmless to import and about a megabyte of dead weight per copy.
    """
    leftovers = sorted(
        p.relative_to(install).as_posix()
        for p in install.rglob("*")
        if p.is_file() and p.suffix in {".a", ".o", ".obj", ".lo", ".la"}
    )
    assert not leftovers, (
        f"{len(leftovers)} build intermediates were installed: {', '.join(leftovers[:8])}"
    )
