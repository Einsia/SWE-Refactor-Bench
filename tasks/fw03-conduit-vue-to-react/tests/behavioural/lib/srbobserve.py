"""What every measuring module in this stage is handed, loaded as a pytest plugin.

Each module in ``modules/`` is its own process with its own pytest run, so this
cannot live in a conftest.py that only one of them can see. It is a plugin on
``PYTHONPATH`` instead, and every module's ``run.sh`` loads it::

    pytest -p srbobserve -p swerefactor.pytest_module ...

What a module gets
------------------
``REFERENCE``   what the State A bundle did this run, keyed by scenario id
``CANDIDATE``   what the submission's bundle did this run, same keys
``SCENARIOS``   the ids this module is responsible for, in corpus order
``observed()``  the submission's observation, or a failure explaining why not

Both sides come from ``$SRB_SUITE_WORK``, where the ``build`` module put them
after installing the submission offline, building it, and driving the same 100
scenarios through the same driver against both bundles under one nonce. That is
the only thing shared between modules, and it is why ``build`` is declared first
and ``required = true``.

There is deliberately no ``repo`` fixture. This stage compares two running
bundles; a test in it that opened a source file was asserting on an
implementation. Every check that wanted to read the tree lives in
``tests/audit/``, which reads both trees and can build neither.
Removing the fixture is what stops the next one being written.
"""

from __future__ import annotations

import gzip
import json
import os
from pathlib import Path

import pytest

SUITE_WORK = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb/shared"))
MODULE_ID = os.environ.get("SRB_MODULE_ID", "")

#: The scenario group this module measures. Set per module in suite.toml; the
#: cross-cutting modules (traffic, session) leave it unset and take all 100.
GROUP = os.environ.get("SRB_GROUP", "").strip()

REFERENCE_FILE = SUITE_WORK / "reference-observation.json"
CANDIDATE_FILE = SUITE_WORK / "candidate-observation.json"


def _load(path: Path) -> dict:
    """Read an observation file, gzipped or not."""
    for candidate in (path, path.with_suffix(path.suffix + ".gz")):
        if not candidate.exists():
            continue
        if candidate.suffix == ".gz":
            with gzip.open(candidate, "rt", encoding="utf-8") as fh:
                return json.load(fh)
        with open(candidate, encoding="utf-8") as fh:
            return json.load(fh)
    raise FileNotFoundError(path)


def _index(payload: dict) -> dict[str, dict]:
    return {s["id"]: s for s in payload.get("scenarios") or []}


def _read_side(path: Path, what: str) -> tuple[dict[str, dict], dict]:
    try:
        payload = _load(path)
    except FileNotFoundError:
        # Collection-time, so the module reports one clear error rather than
        # 3,000 confusing ones. `build` is required, so this is only reachable
        # when someone runs a module by hand.
        raise RuntimeError(
            f"no {what} observation at {path}. The `build` module writes both "
            f"sides into $SRB_SUITE_WORK; it is declared first and required, so "
            f"this module cannot report on the submission without it."
        ) from None
    return _index(payload), payload


REFERENCE, _REF_RAW = _read_side(REFERENCE_FILE, "State A")
CANDIDATE, _CAND_RAW = _read_side(CANDIDATE_FILE, "submission")

#: The run's nonce, recorded so a failure can be reproduced:
#: ``SRB_NONCE=<this> swerefactor behavioural ...`` drives the same fixture data.
NONCE = _REF_RAW.get("nonce")

#: Every scenario State A produced an observation for, in corpus order.
ALL_SCENARIOS: list[str] = [
    s["id"] for s in _REF_RAW.get("scenarios") or [] if s.get("observed")
]

#: The ones this module is responsible for.
SCENARIOS: list[str] = [
    sid for sid in ALL_SCENARIOS
    if not GROUP or REFERENCE[sid].get("group") == GROUP
]


# --------------------------------------------------------------------------- #
# Reading the two sides
# --------------------------------------------------------------------------- #


def reference(sid: str) -> dict:
    """State A's observation for this scenario. Present by construction."""
    return REFERENCE[sid]


def observed(sid: str) -> dict:
    """The submission's observation, or a failure that says what went wrong.

    A scenario the submission could not be driven through fails every check that
    depends on it, which is the intended arithmetic: a screen that does not
    render is worth nothing rather than being quietly skipped.
    """
    got = CANDIDATE.get(sid)
    if got is None:
        pytest.fail(
            f"the submission was not driven through '{sid}' at all; the "
            f"observation file has no entry for it"
        )
    if got.get("stepErrors"):
        pytest.fail(
            "the scenario could not be driven to the end against the "
            f"submission:\n  " + "\n  ".join(got["stepErrors"][:3])
        )
    if not got.get("observed"):
        pytest.fail(
            "no snapshot was taken against the submission -- the page never "
            "reached a settled state"
        )
    return got


# --------------------------------------------------------------------------- #
# Rendering a comparison
# --------------------------------------------------------------------------- #


def node_line(node: dict) -> str:
    """One node as a single readable line: tag, classes, attributes, text."""
    if node.get("text") is not None:
        return f"#text {node['text']!r}"
    bits = [f"<{node.get('tag')}"]
    classes = node.get("classes") or []
    if classes:
        bits.append(f'class="{" ".join(classes)}"')
    for key in sorted(node.get("attrs") or {}):
        bits.append(f'{key}="{node["attrs"][key]}"')
    return " ".join(bits) + ">"


def probe_line(entry: dict) -> str:
    parts = [f"count={entry.get('count')}"]
    if entry.get("texts"):
        parts.append(f"texts={entry['texts']}")
    if entry.get("hrefs"):
        parts.append(f"hrefs={entry['hrefs']}")
    return "  ".join(parts)


def control_line(control: dict) -> str:
    bits = [str(control.get("tag"))]
    for field in ("type", "value", "checked", "disabled", "placeholder"):
        if control.get(field) not in (None, "", False):
            bits.append(f"{field}={control[field]!r}")
    return " ".join(bits)


def request_line(req: dict) -> str:
    auth = f" auth={req.get('auth')}" if req.get("auth") else ""
    body = ""
    if req.get("body") not in (None, ""):
        body = f" body={json.dumps(req['body'], sort_keys=True)}"
    return (
        f"{req.get('method')} {req.get('path')}{req.get('search') or ''}"
        f" -> {req.get('status')}{auth}{body}"
    )


# --------------------------------------------------------------------------- #
# One header line per module run, so a log says what was measured
# --------------------------------------------------------------------------- #


def pytest_report_header(config) -> list[str]:
    scope = GROUP or "all groups"
    return [
        f"module {MODULE_ID or '?'}: {len(SCENARIOS)} scenario(s) ({scope}), "
        f"nonce={NONCE or 'none'}",
        f"reference={REFERENCE_FILE}  candidate={CANDIDATE_FILE}",
    ]


@pytest.fixture(scope="session")
def nonce() -> str | None:
    return NONCE
