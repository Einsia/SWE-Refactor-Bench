#!/usr/bin/env python3
"""Prove the candidate venv holds the pinned five and nothing that could answer YAML.

Run at image build time by the venv's own interpreter, so what it inspects is the
environment a candidate test will actually import from.

Two checks, because they fail differently.

The first is an exact comparison of installed distributions against the pins.
`--require-hashes` already refuses to install an unpinned package, so a sixth
distribution cannot arrive through pip -- but it can arrive by being present in
the base image and inherited, or by a later `RUN` in this Dockerfile, and neither
of those is something the requirements file constrains.  An exact set is also the
only form of this check that does not depend on my guessing which packages to look
for: it names the five that belong and reports anything else, rather than naming
the parsers I happened to think of.

The second is importability of a YAML implementation, which is the property that
actually matters -- a candidate writes `import yaml`, and if that succeeds it can
compare the submission against PyYAML rather than against the original, which is
not a defect in the port and would make the round's disagreement meaningless.

The importability check replaces an inline version that could not pass.  It read

    found = [m for m in ('yaml', 'ruamel', 'ruamel.yaml') if u.find_spec(m)]

and `find_spec` on a dotted name imports the parent package first, so
`find_spec('ruamel.yaml')` raises ModuleNotFoundError when `ruamel` is absent
instead of returning None.  The absence it was written to confirm was the one
input that made it crash, and it crashed at the build step that installs the venv
-- so the check that was supposed to guard the stage instead prevented the image
from existing at all.  Which is the harmless direction of that mistake; a check
that cannot pass gets found the first time it runs, whereas one that cannot fail
does not.
"""
from __future__ import annotations

import importlib.metadata as md
import importlib.util
import sys

#: Exactly what requirements-candidate.txt pins, by distribution name as
#: importlib.metadata reports it (normalised below, so `pytest-timeout` and
#: `pytest_timeout` compare equal).  pip itself, setuptools and wheel are excluded
#: because `python3 -m venv` puts them there before pip runs; they are part of the
#: interpreter, not of the candidate's stack.
PINNED = {"iniconfig", "packaging", "pluggy", "pytest", "pytest-timeout"}
VENV_BUILTINS = {"pip", "setuptools", "wheel", "pkg-resources"}

#: Top-level import names that would give a candidate a second YAML
#: implementation.  Only top-level names: a `ruamel.yaml` install brings the
#: `ruamel` namespace package with it, so the parent is sufficient to detect the
#: child, and asking for the child is what broke the previous version of this.
YAML_MODULES = ("yaml", "ruamel", "syck", "oyaml", "strictyaml", "yamlcore")


def normalise(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def installed() -> dict[str, str]:
    found: dict[str, str] = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        found[normalise(name)] = dist.version or "?"
    return found


def importable(module: str) -> bool:
    """True if `import module` would find something.

    ModuleNotFoundError is an absence, not an error: find_spec raises it when a
    parent package is missing, and a missing parent is exactly the state this
    wants to report as clean.  ValueError covers a module already in sys.modules
    with a None spec, which no import of these names produces but which would
    otherwise be an unhandled traceback.
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def main() -> int:
    problems: list[str] = []

    have = installed()
    extra = sorted(set(have) - PINNED - VENV_BUILTINS)
    missing = sorted(PINNED - set(have))
    if missing:
        problems.append(
            f"the candidate venv is missing pinned distribution(s): {missing}"
        )
    if extra:
        problems.append(
            f"the candidate venv holds distribution(s) nothing pins: "
            f"{[f'{n}=={have[n]}' for n in extra]}; requirements-candidate.txt is "
            f"the whole intended stack, so either it or this image gained something"
        )

    found = [m for m in YAML_MODULES if importable(m)]
    if found:
        problems.append(
            f"a YAML parser is importable in the candidate venv: {found}. A "
            f"candidate could compare the submission against it instead of against "
            f"the original, and a disagreement with a different YAML "
            f"implementation is not a defect in the port"
        )

    for problem in problems:
        print(f"check-venv: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(
        f"candidate venv: {len(PINNED)} pinned distributions, "
        f"no YAML implementation among {len(YAML_MODULES)} names checked"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
