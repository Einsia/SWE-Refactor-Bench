"""Read the reference recording at import time, for parametrisation only.

Parametrisation happens during collection, before any fixture runs, so the id
lists cannot come from the session-scoped ``reference`` fixture. This module
exists to load the file once at import and expose only *keys* -- never expected
values.

That split is deliberate. A test that took its expectation from here would read
the recording twice by two different paths, and a test that took its expectation
from the fixture but its id list from here can only ever be parametrised on cases
that really were measured. The ``pair`` fixture fails loudly if those two views
ever disagree, which is the check that keeps this shortcut honest.
"""

from __future__ import annotations

import json
import os

#: ``SRB_SUITE_DIR`` is set by the runner for every module. The fallback is two
#: directories up from this file, which is the same place: ``lib/`` sits directly
#: under the suite root, and resolving it that way keeps ``--collect-only`` during
#: the image build working before any runner is involved.
_SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
_RECORDING = os.path.join(_SUITE, "data", "responses.json")

with open(_RECORDING) as _fh:
    _G = json.load(_fh)

PROFILES: dict = _G["profiles"]
CLI: dict = _G.get("cli", {})


#: Imported rather than redefined, and that is not fussiness. A second definition
#: here -- ``f"{pid}/{cid}"`` against the harness's ``::`` -- would never meet this
#: one at import time, so every test would still collect, and then every one would
#: error in the ``pair`` fixture with "not present in the golden file": the
#: parametrised key cannot match a key the fixture built under another spelling.
#: One definition, shared, is the only arrangement in which that cannot happen.
from harness.profiles import case_key as _key


#: Every measured HTTP case, sorted so the run order is stable across machines.
ALL_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items() for cid in p["cases"])

#: Case keys grouped by the body_mode they were recorded with. The mode decides
#: what a body test is allowed to assert, so the split happens at collection.
KEYS_BY_MODE: dict[str, list[str]] = {}
for _pid, _p in PROFILES.items():
    for _cid, _c in _p["cases"].items():
        KEYS_BY_MODE.setdefault(_c["body_mode"], []).append(_key(_pid, _cid))
for _m in KEYS_BY_MODE:
    KEYS_BY_MODE[_m].sort()

#: Cases whose body is a contract at all -- ``ALL_KEYS`` minus the 31 recorded
#: ``body_mode="ignore"``, whose body genuinely varies in State A.
#:
#: A body test parametrized over ``ALL_KEYS`` would take these and skip them in the
#: body, and every skip is charged: those ids would sit in ``corpus``'s denominator
#: forever, unanswerable by any submission because State A has no answer to compare
#: against. 93 checks -- 31 cases times the three tests that run across all modes --
#: on a corpus that is a transcript of State A's own answers. Filtering at
#: collection deletes the check instead of exempting it, which is what makes the
#: module's size the number of questions it can actually ask.
BODY_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if c["body_mode"] != "ignore")

#: Cases that recorded a parsed index. Only these can be asked about chart
#: structure, ordering or digests.
INDEX_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if "index" in c)

#: Cases that recorded a Prometheus exposition body.
METRICS_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if "metrics" in c)

#: (case key, family name) for every ``# HELP``/``# TYPE`` declaration recorded.
#:
#: Parametrised per family rather than per case, and that is the whole point of this
#: pair list. An exposition is not one fact, it is one declaration per family and
#: one signature per series, and a single body digest weighs all of it as one.
#:
#: Measured, by mutation, before this existed. Only a handful of the recorded cases
#: carry an exposition at all. Four mutants confirmed it: rewording one ``# HELP``,
#: changing the ``handler`` label, renaming one family, and renaming every family
#: all scored within 0.0004 of a perfect run. A uniform price for a graded surface
#: of that size is not a weight, and it put destroying the whole exposition of a
#: *retired* middleware two orders of magnitude below dropping one ``charset``. One
#: test per declared fact is the fix, and the same four mutants now cost 6, 6, 16
#: and 38 failures.
#:
#: Only the cases that actually recorded a family table contribute: at
#: ``--context-path``, State A serves 404 at ``/cm/metrics``, and those cases are
#: recorded with an empty table, so they yield no per-family tests.
METRICS_FAMILY_PAIRS: list[tuple[str, str]] = sorted(
    (_key(pid, cid), family)
    for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if "metrics" in c
    for family in set(c["metrics"]["help"]) | set(c["metrics"]["type"]))

#: (case key, series name) for every series recorded, so its label signature can
#: be compared on its own. The request counter carries a handler label whose value
#: is a Go symbol; this is where that is graded, against what was measured rather
#: than against a string written down somewhere.
METRICS_SERIES_PAIRS: list[tuple[str, str]] = sorted(
    (_key(pid, cid), name)
    for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if "metrics" in c
    for name in c["metrics"]["series"])

#: (case key, header name) for every header actually observed, so a value test
#: exists exactly where there is a value to compare.
HEADER_PAIRS: list[tuple[str, str]] = sorted(
    (_key(pid, cid), name)
    for pid, p in PROFILES.items()
    for cid, c in p["cases"].items()
    for name in c["header_names"])

#: Cases whose body parsed as JSON. ``json`` is present on every case (as null
#: where it did not parse), so presence is not the test -- non-null is.
JSON_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if c.get("json") is not None)

#: Of those, the 459 whose JSON is an *object*. A key-set test has nothing to
#: compare on the other 13, whose document is a list or a bare scalar, and it used
#: to say so with a skip inside the test body -- one charged check per case now.
JSON_OBJECT_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if isinstance(c.get("json"), dict))

#: Metrics cases carrying at least one gauge State A recorded as stable. Today
#: none of the six do: the only gauges in this exposition are
#: ``chartmuseum_charts_served_total`` and
#: ``chartmuseum_chart_versions_served_total``, and both move during a run. They
#: are graded as exact arithmetic over storage in the audit suite, which is
#: stronger than replaying a recorded number, so nothing is unmeasured by this
#: being empty -- and a test collected over an empty list is a check no submission
#: can answer.
METRICS_VALUE_KEYS: list[str] = sorted(
    _key(pid, cid) for pid, p in PROFILES.items()
    for cid, c in p["cases"].items() if c.get("metrics_values"))

CLI_IDS: list[str] = sorted(CLI)

PROFILE_IDS: list[str] = sorted(PROFILES)
