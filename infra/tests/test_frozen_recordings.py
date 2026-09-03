"""Every frozen recording still answers the corpus its suite sends.

A recording cannot notice being asked different questions.  Each of these suites
therefore stamps the fingerprint of the request set it was captured against, and
each stage image re-computes that fingerprint at build time and refuses to build
when the two disagree.

That check works, but it only runs during ``docker build``.  A project-wide
rename swept an opaque multipart boundary out of fw06's corpus -- a string that
feeds the fingerprint and appears nowhere in the recording, so there was nothing
on the recording side for the rename to rewrite -- and the drift reached a
release before anything ran that noticed.  The same class of drift had already
been fixed once on fw05.

So the check is repeated here, where it costs a second and runs on every push.
The point is not to duplicate the Dockerfile but to move it earlier: these
fingerprints cover request inputs, and request inputs are exactly what a rename
or a tidy-up walks over without knowing they are frozen.
"""

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INFRA = REPO / "infra"
TASKS = REPO / "tasks"

# task -> the recording it ships, the stored fingerprint keys and the module that
# recomputes each, and optionally the key holding one record per case id.
FROZEN: dict[str, dict] = {
    "fw02-jsonserver-express-to-fastify": {
        "recording": "data/recorded-responses.json",
        "fingerprints": {"exchanges_fingerprint": "harness.exchanges"},
    },
    "fw04-chartmuseum-gin-to-chi": {
        "recording": "data/responses.json",
        "fingerprints": {"corpus_fingerprint": "harness.corpus",
                         "cli_fingerprint": "harness.cli"},
    },
    "fw05-miniserve-actix-to-axum": {
        "recording": "data/responses.json.gz",
        "fingerprints": {"fingerprint": "harness.corpus"},
    },
    "fw06-uploadserver-gorillamux-to-nethttp": {
        "recording": "data/golden-statea.json",
        "fingerprints": {"corpus_fingerprint": "harness.corpus"},
        "records": ("records", "harness.corpus"),
    },
}

# Computed in a child interpreter: every suite names its package ``harness``, so
# importing two of them into one process would serve the first one twice.
_CHILD = """\
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
out = {m: importlib.import_module(m).fingerprint() for m in sys.argv[2].split(",")}
if sys.argv[3]:
    out["ids"] = sorted(c["id"] for c in importlib.import_module(sys.argv[3]).all_cases())
json.dump(out, sys.stdout)
"""


def _load(path: Path) -> dict:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(path.read_text(encoding="utf-8"))


def _computed(suite: Path, modules: list[str], ids_module: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(suite / "lib"), ",".join(modules), ids_module],
        capture_output=True, text=True, cwd=suite,
        env={"PYTHONPATH": str(INFRA), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, f"the harness would not load:\n{proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("task", sorted(FROZEN))
def test_the_recording_answers_the_corpus_the_suite_sends(task):
    spec = FROZEN[task]
    suite = TASKS / task / "tests" / "behavioural"
    recording = _load(suite / spec["recording"])
    records_key, ids_module = spec.get("records", ("", ""))
    got = _computed(suite, list(spec["fingerprints"].values()), ids_module)

    problems = []
    for key, module in spec["fingerprints"].items():
        if recording.get(key) != got[module]:
            problems.append(f"{key}: the recording answers {recording.get(key)}, "
                            f"{module} is {got[module]}")
    if records_key:
        stored, ids = set(recording[records_key]), set(got["ids"])
        if missing := sorted(ids - stored):
            problems.append(f"{len(missing)} case(s) have no record: {missing[:6]}")
        if orphan := sorted(stored - ids):
            problems.append(f"{len(orphan)} record(s) match no case: {orphan[:6]}")
    assert not problems, (
        f"{task}: {spec['recording']} was captured against a different corpus.\n  "
        + "\n  ".join(problems)
        + "\n\nRestore the request inputs the capture was made against, or re-capture "
          "and review the recording.  Editing the stored fingerprint would pair the "
          "recorded answers with questions nobody asked."
    )


def test_every_stamped_recording_is_covered():
    """A new suite must not be able to stamp a fingerprint nothing re-computes."""
    stamped = set()
    for data in sorted(TASKS.glob("*/tests/behavioural/data")):
        for path in sorted(data.iterdir()):
            if path.suffix not in (".json", ".gz"):
                continue
            try:
                doc = _load(path)
            except (ValueError, OSError, EOFError):
                continue
            if isinstance(doc, dict) and any(
                    "fingerprint" in k for k in doc if isinstance(doc[k], str)):
                stamped.add(path.parents[3].name)
    assert stamped == set(FROZEN), (
        "the suites that stamp a corpus fingerprint and the suites checked here "
        f"have diverged: unchecked {sorted(stamped - set(FROZEN))}, "
        f"listed but no longer stamping {sorted(set(FROZEN) - stamped)}"
    )
