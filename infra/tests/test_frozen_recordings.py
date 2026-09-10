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
import hashlib
import json
import re
import subprocess
import sys
import tarfile
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


def test_fw01_recorded_bodies_are_the_length_they_claim():
    """httpbin echoes the request back, so a recorded body is self-checking.

    Every entry carries the byte count of the body as it left the server, taken
    before the capture placeholdered the authority it was reached on. Undo the
    placeholders and the two have to agree; where they do not, the recording has
    been edited since it was made.
    """
    suite = TASKS / "fw01-httpbin-flask-to-asgi" / "tests" / "behavioural"
    doc = _load(suite / "data" / "responses.json")
    host = f"localhost:{doc['captured_at_port']}"
    problems = []
    for case_id, rec in sorted(doc["responses"].items()):
        body = rec.get("body")
        if not isinstance(body, str) or body.startswith("b64:"):
            continue
        restored = body.replace("__HOST__", host).replace("__ORIGIN__", "127.0.0.1")
        if len(restored.encode("utf-8")) != rec["body_len"]:
            problems.append(f"{case_id}: body_len {rec['body_len']}, body is "
                            f"{len(restored.encode('utf-8'))}")
    assert not problems, (
        "fw01 data/responses.json disagrees with itself:\n  "
        + "\n  ".join(problems[:8]))


def test_fw02_recorded_static_bodies_are_state_a():
    """The pages json-server serves from ``public/`` are files in the tree."""
    task = TASKS / "fw02-jsonserver-express-to-fastify"
    with tarfile.open(task / "environment" / "original.tar.gz") as tf:
        handle = tf.extractfile("repo/public/index.html")
        want = handle.read().decode("utf-8")
    doc = _load(task / "tests" / "behavioural" / "data" / "recorded-responses.json")
    served = [(group, case, rec)
              for group, cases in doc["responses"].items()
              for case, rec in cases.items()
              if isinstance(rec, dict)
              and rec.get("body_len") == len(want.encode("utf-8"))]
    assert served, "no recorded response is index.html-sized any more"
    problems = [f"{group}/{case}" for group, case, rec in served
                if rec.get("body") != want]
    assert not problems, (
        "fw02 recorded a page that is not the one State A serves: "
        + ", ".join(problems))


def test_build01_probe_matches_its_baseline():
    """The consumer hashes a fixed string; the baseline records what it printed."""
    task = TASKS / "build01-libsodium-autotools-to-cmake" / "tests" / "behavioural"
    source = (task / "lib" / "consumers.py").read_text(encoding="utf-8")
    msg = re.search(r'const char \*msg = "([^"]*)";', source).group(1)
    digest = hashlib.blake2b(msg.encode(), digest_size=32).digest()
    want = _load(task / "data" / "baseline.json")["consumer_output"]
    assert want.endswith(f"{digest[0]:02x}{digest[1]:02x}"), (
        f"the consumer hashes {msg!r}, which prints "
        f"{digest[0]:02x}{digest[1]:02x}, but data/baseline.json recorded "
        f"{want!r}. State A's build did not change; the string the probe is "
        f"built from did.")


def test_fw05_listings_do_not_depend_on_the_filesystems_order():
    """Ties under a size or date sort are ordered by the filesystem, not the server.

    The sample tree gives every file the same size and every entry the same
    pinned mtime, so those two sorts leave most of a listing tied and the
    enumeration order decides the rest. The comparison puts tied rows in name
    order for that reason; this is the assertion that it does.
    """
    suite = TASKS / "fw05-miniserve-actix-to-axum" / "tests" / "behavioural"
    proc = subprocess.run(
        [sys.executable, "-c", _FW05_CHILD, str(suite / "lib")],
        capture_output=True, text=True, cwd=suite,
        env={"PYTHONPATH": str(INFRA), "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 0, f"the harness would not load:\n{proc.stderr}"
    got = json.loads(proc.stdout)
    assert got["sorted_pages"] > 50, got
    assert not got["order_dependent"], (
        "these pages compare differently when the same entries arrive in "
        f"another order: {got['order_dependent'][:6]}")
    assert not got["name_pages_touched"], (
        "a name-sorted listing was reordered, which loses a real difference: "
        f"{got['name_pages_touched'][:6]}")


#: Shuffles each page's tied rows, then asserts the comparison form is unchanged.
_FW05_CHILD = """\
import gzip, json, random, sys
sys.path.insert(0, sys.argv[1])
from harness import corpus, normalize

random.seed(0)
golden = json.load(gzip.open("data/responses.json.gz", "rt"))
order = {corpus.case_key(s.id, c.id): corpus.listing_order(s, c)
         for s, c in corpus.all_cases()}
out = {"sorted_pages": 0, "order_dependent": [], "name_pages_touched": []}

def shuffled(text, method, dirs_first):
    rows = list(normalize._LISTING_ROW.finditer(text))
    groups, run = [], []
    for i, row in enumerate(rows):
        name, cell, isdir = normalize._row_key(row.group(0), method)
        group = (isdir and dirs_first, cell)
        if run and group != run[0][0]:
            groups.append([i for _, i in run]); run = []
        run.append((group, i))
    groups.append([i for _, i in run])
    perm = []
    for group in groups:
        group = list(group); random.shuffle(group); perm += group
    parts, last = [], 0
    for slot, source in zip(rows, perm):
        parts.append(text[last:slot.start()])
        parts.append(rows[source].group(0))
        last = slot.end()
    parts.append(text[last:])
    return "".join(parts)

for session_id, session in golden["sessions"].items():
    for case_id, rec in session["cases"].items():
        if not isinstance(rec, dict) or not rec.get("html"):
            continue
        method, dirs_first = order.get(corpus.case_key(session_id, case_id),
                                       ("name", False))
        body = rec["body"]
        canonical = normalize.order_ties_by_name(body, method, dirs_first)
        if method == "name":
            if canonical != body:
                out["name_pages_touched"].append(case_id)
            continue
        out["sorted_pages"] += 1
        other = shuffled(body, method, dirs_first)
        if normalize.order_ties_by_name(other, method, dirs_first) != canonical:
            out["order_dependent"].append(f"{session_id}::{case_id}")
json.dump(out, sys.stdout)
"""


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
