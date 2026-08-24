"""Write the per-surface module directories.

Run once, by hand, from ``lib/``.  Kept in the tree because the eleven surface
modules are the same file with a different import list, and a generator that
derives the list from the routing table cannot get the correspondence wrong --
which is the thing ``battery.self_check`` fails the build over.

    SRB_TREE_SPEC=../data/tree-spec.json PYTHONPATH=. python3 gen_surface_modules.py
"""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import battery
import replay
import routing

MODULES = Path(__file__).resolve().parent.parent / "modules"

#: Byte-for-byte what the thirteen surface modules ship, so that re-running this
#: generator is a no-op on the runners rather than a silent revert.  The pytest
#: invocation itself lives in ``lib/run-pytest.sh``: the flags that decide how a
#: module is collected -- which ini file, which plugins, where the junit lands --
#: are the same for all thirteen, and having them in one file means a change to
#: them cannot apply to twelve modules and miss the thirteenth.
RUNNER = """\
#!/bin/sh
# One module, one pytest process; the shared runner is in lib/.
exec bash "$SRB_SUITE_DIR/lib/run-pytest.sh" "$@"
"""

HEADER = '''\
"""{title}.

{blurb}

{routed}
"""

from battery import (            # noqa: F401
{imports}
)
'''

TAIL = ("The assertions are in ``lib/battery.py`` and the routing that decides "
        "which cases reach them is in ``lib/routing.py``; this file is the list "
        "of what this surface is held to.")

#: ``axis -> (singular, plural)`` for the one line that says what got routed here.
NOUNS = {
    "case": ("recorded case", "recorded cases"),
    "html_case": ("rendered page", "rendered pages"),
    "archive_case": ("archive download", "archive downloads"),
    "nonce_case": ("page with per-boot asset routes",
                   "pages with per-boot asset routes"),
    "decoded_case": ("negotiated encoding", "negotiated encodings"),
    "session": ("configuration", "configurations"),
}


def phrase(axis: str, count: int) -> str:
    singular, plural = NOUNS[axis]
    return f"{count} {singular if count == 1 else plural}"


def main() -> None:
    golden = replay.load_golden(os.environ.get(
        "SRB_GOLDEN", "../data/responses.json.gz"))
    batteries = {name: obj for name, obj in vars(battery).items()
                 if name.startswith("test_") and callable(obj)}
    by_axis: dict[str, list[str]] = {}
    for name, func in batteries.items():
        by_axis.setdefault(battery.axis_of(func), []).append(name)

    for module_id, surface in sorted(routing.SURFACE_BY_ID.items()):
        directory = MODULES / module_id
        directory.mkdir(parents=True, exist_ok=True)
        wanted: list[str] = []
        counts = []
        for axis, names in sorted(by_axis.items()):
            if axis == "session":
                population = len(routing.sessions_for(module_id))
            else:
                population = len(battery.AXES[axis](module_id, golden))
            if population:
                wanted.extend(sorted(names))
                counts.append(phrase(axis, population))
        imports = "\n".join(f"    {name}," for name in sorted(wanted))
        listed = (", ".join(counts[:-1]) + " and " + counts[-1]
                  if len(counts) > 1 else counts[0])
        routed = textwrap.fill(f"Graded by {listed}.  {TAIL}", width=79)
        title = surface.title[0].upper() + surface.title[1:]
        text = HEADER.format(title=title, blurb=surface.about.strip(),
                             routed=routed, imports=imports)
        (directory / f"test_{module_id}.py").write_text(text, encoding="utf-8")
        runner = directory / "run.sh"
        runner.write_text(RUNNER, encoding="utf-8")
        runner.chmod(0o755)
        print(f"{module_id:14s} {len(wanted):2d} batteries  ({', '.join(counts)})")


if __name__ == "__main__":
    main()
