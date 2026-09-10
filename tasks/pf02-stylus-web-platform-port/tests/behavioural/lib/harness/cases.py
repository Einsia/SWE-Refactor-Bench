"""The case catalog: which stylesheets to compile, and with which options.

Read from State A's `test/` tree, never from the submission, so that a submission
cannot change what it is asked to compile.  The option rules are the ones
upstream's own `test/run.js` applies by filename, reproduced here so that a case
means the same thing to State A and to the realm.
"""
from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path

from . import layout

#: Upstream excludes this from its integration suite: it is the index of the
#: others, not a case, and has no expected `.css`.
EXCLUDED = frozenset({"index"})


@dataclass(frozen=True)
class Case:
    """One compilation, described independently of who runs it."""

    name: str
    source: str
    #: `renderer.set(k, v)` pairs.
    settings: dict = field(default_factory=dict)
    #: `renderer.include(p)` arguments, as virtual paths.
    includes: tuple = ()
    #: Install the built-in `url()` resolver, as upstream does for `resolver` cases.
    define_resolver: bool = False

    @property
    def virtual_filename(self) -> str:
        return f"{layout.VPROJ}/test/cases/{self.name}.styl"


def _settings_for(name: str) -> dict:
    """Upstream's filename-driven option rules (test/run.js)."""
    s: dict = {}
    if "compress" in name:
        s["compress"] = True
    if "include" in name:
        s["include css"] = True
    if "prefix." in name:
        s["prefix"] = "prefix-"
    if "hoist." in name:
        s["hoist atrules"] = True
    return s


@functools.lru_cache(maxsize=1)
def cases() -> tuple[Case, ...]:
    root = layout.INPUTS / "cases"
    out = []
    for styl in sorted(root.glob("*.styl")):
        name = styl.stem
        if name in EXCLUDED:
            continue
        out.append(
            Case(
                name=name,
                source=styl.read_text(encoding="utf8"),
                settings=_settings_for(name),
                includes=(
                    f"{layout.VPROJ}/test/images",
                    f"{layout.VPROJ}/test/cases/import.basic",
                ),
                define_resolver="resolver" in name,
            )
        )
    return tuple(out)


@functools.lru_cache(maxsize=1)
def case_names() -> tuple[str, ...]:
    return tuple(c.name for c in cases())


def case(name: str) -> Case:
    for c in cases():
        if c.name == name:
            return c
    raise KeyError(name)


@functools.lru_cache(maxsize=1)
def converter_cases() -> tuple[tuple[str, str], ...]:
    """`(name, css)` pairs for the CSS -> Stylus converter."""
    root = layout.INPUTS / "converter"
    return tuple(
        (p.stem, p.read_text(encoding="utf8")) for p in sorted(root.glob("*.css"))
    )


@functools.lru_cache(maxsize=1)
def deps_cases() -> tuple[str, ...]:
    root = layout.INPUTS / "deps-resolver"
    return tuple(sorted(p.stem for p in root.glob("*.styl")))


@functools.lru_cache(maxsize=1)
def sourcemap_cases() -> tuple[str, ...]:
    root = layout.INPUTS / "sourcemap"
    return tuple(sorted(p.stem for p in root.glob("*.styl")))


def op_for(case_obj: Case, *, kind: str = "render", sync: bool = False, op_id: str | None = None) -> dict:
    """Render one Case into the driver op vocabulary shared by both runners."""
    settings = dict(case_obj.settings)
    settings["filename"] = case_obj.virtual_filename
    op = {
        "id": op_id or case_obj.name,
        "kind": kind,
        "source": case_obj.source,
        "set": settings,
        "include": list(case_obj.includes),
        "options": {},
    }
    if sync:
        op["sync"] = True
    if case_obj.define_resolver:
        op["defineResolver"] = True
    return op
