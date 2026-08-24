"""Accessors that turn a missing probe observation into a clear grader failure.

These live in :mod:`harness` rather than in ``audit/conftest.py`` so the test
modules can import them absolutely. ``audit/`` deliberately has no
``__init__.py`` -- pytest's rootdir-based import mode gives each test module a
top-level name, and a relative ``from .conftest import ...`` would raise
``ImportError: attempted relative import with no known parent package`` at
collection time.

Every function here fails rather than raising ``KeyError``, and says which launch
and battery was missing. A grader that dies with a traceback out of a dict lookup
tells whoever reads the log nothing about whether the submission or the grader is
at fault.
"""

from __future__ import annotations

import pytest


def launch_or_fail(probed: dict, launch_id: str) -> dict:
    """The named launch's batteries, or a failure naming why it did not start."""
    rec = probed["launches"].get(launch_id)
    if rec is None:
        pytest.fail(f"probe launch {launch_id!r} was never attempted")
    if rec.get("__failed__"):
        tail = rec.get("log", "")[-2500:] or "(no output)"
        pytest.fail(f"probe launch {launch_id!r} did not start: "
                    f"{rec['__failed__']}\n--- process output ---\n{tail}")
    return rec["batteries"]


def battery_or_fail(probed: dict, launch_id: str, name: str) -> dict:
    """One battery's observations, or a failure naming the launch and battery."""
    b = launch_or_fail(probed, launch_id)
    if name not in b:
        pytest.fail(f"probe battery {launch_id}/{name} produced no observations")
    return b[name]


def obs_or_fail(battery: dict, key: str, *, what: str = "observation") -> dict:
    """One observation out of a battery, with the available keys on failure.

    The key list is truncated: some batteries hold a few hundred observations and a
    failure message that long is unreadable.
    """
    o = battery.get(key)
    if o is None:
        have = sorted(k for k, v in battery.items() if isinstance(v, dict))
        pytest.fail(f"no {what} for {key!r} (have {have[:8]}"
                    f"{' ...' if len(have) > 8 else ''})")
    return o
