"""The configuration contract: the same YAML still configures the service.

The eight pytest modules ask what the service answers.  This one asks whether it
still LISTENS to the file it is given, which is a different property and one a
replay corpus cannot see: every case in the capture was asked of a server started
from one config, so a submission that hard-coded that config's values would answer
all 88 of them correctly.

So the method here is to vary the file and observe that the service varies with it.
Four variants, each changing one thing, each launched on both sides and compared:

  ports          the connectors move.  If the submission binds where the file says
                 rather than where its own defaults say, the launch succeeds at all
                 — and a submission with the ports compiled in fails to be found.
  profiles       only `car` is configured, where the base profile configures three.
                 /info has to report one, and a request for `bike` has to fail.
                 This is the variant a hard-coded profile list cannot survive.
  header-limit   `max_request_header_size` is lowered.  A request that fits under
                 the base config must now be refused — which proves the value
                 reached the embedded container, not just the configuration object.
  bad-graph      `datareader.file` names a file that does not exist.  Startup must
                 fail rather than serve an empty graph, and both sides must agree
                 that it fails.

Nothing here asserts HOW the file was read.  A submission may bind it with its
framework's own configuration mechanism, a hand-written loader, or anything else;
what is graded is that the same file produces the same service.  That is the
contract an operator has with this repository, and it is the one thing about
configuration that survives a framework change.

Each request is asked twice per side and compared through harness.diff, exactly
as the eight replay modules compare theirs.  This module started out asking once
and comparing the two sides' parsed bodies with `==`, which is the same question
with none of the discipline: no measured-volatility mask, no wall-clock mask, no
authority fold, and no header comparison either.  It failed three of its four
variants on /info's import_date -- the moment each JVM imported its graph, which
cannot agree between two sequential launches -- while never once looking at a
header.  Going through diff means every one of those is applied here, and that a
fix to any of them reaches this module too.

Every variant imports its own graph, and that is deliberate rather than an
oversight to be optimised away.  `_materialise` points each one at
`target/srb-cfg-<variant>`, which is a different directory from the
`target/srb-graph-<profile>` that `build` imports into, so nothing here starts
warm: eight launches, four variants times two sides, at roughly forty seconds
each -- less `bad-graph`, which is supposed to fail before it imports anything.
Sharing one directory would be faster and would break, because these variants do
not declare the same profiles: `ports` and `header-limit` declare car, foot and
bike, while `profiles` and `bad-graph` declare car alone, and GraphHopper refuses
to open a graph whose stored profiles are not the ones configured (`Profiles do
not match`).  A shared location would therefore make each variant's result depend
on which variant happened to run before it.  The cost is bounded and paid for:
`ready_timeout` is 300s per launch against a measured ~40s import of the Andorra
extract, and the whole module took 316s of stage 2's 707s on a run where both
sides launched.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/lib")

import toolchain as tc                                    # noqa: E402
from harness import capture, diff, maven, normalize, server  # noqa: E402

VARIANTS = tc.DATA / "configs" / "variants"

#: Ports for the variant launches.  Distinct from the ports `build` used, so that
#: a JVM from the capture phase which ignored SIGTERM cannot be mistaken for the
#: server this module started.
PORTS = {
    "reference": {"app": 18991, "admin": 18992},
    "submission": {"app": 19991, "admin": 19992},
}


def _materialise(tree: Path, variant: str, side: str) -> Path:
    ports = PORTS[side]
    graph = tree / "target" / f"srb-cfg-{variant}"
    text = ((VARIANTS / f"{variant}.yml").read_text()
            .replace("@APP_PORT@", str(ports["app"]))
            .replace("@ADMIN_PORT@", str(ports["admin"]))
            .replace("@GRAPH_DIR@", str(graph))
            .replace("@REPO_ROOT@", str(tree)))
    out = tree / f"srb-cfg-{variant}.yml"
    out.write_text(text)
    return out


def req(method: str, port: str, target: str, why: str, *,
        body_mode: str = "json") -> dict:
    """One request inside a variant, in the same shape a corpus case has.

    Same shape on purpose: it is what lets this module compare through
    harness.diff instead of by its own rules.  `why` is printed on failure, like
    a corpus case's.
    """
    assert body_mode in ("json", "exact", "ignore"), body_mode
    return {"method": method, "port": port, "target": target, "why": why,
            "body_mode": body_mode}


def _probe(srv: server.Server, requests: list[dict],
           repeats: int = 2) -> list[list]:
    """Ask each request `repeats` times and keep every answer.

    Twice, for the same reason the capture asks twice: it is what makes
    volatility measurable instead of declared.
    """
    out = []
    for r in requests:
        answers = []
        for _ in range(repeats):
            try:
                answers.append(srv.transport(r["port"]).send(
                    r["method"], r["target"], {}, None))
            except Exception as exc:                       # noqa: BLE001
                answers.append({"error": f"{type(exc).__name__}: {exc}"})
        out.append(answers)
    return out


def _launch(side: str, tree: Path, jar: Path, variant: str,
            requests: list[dict]):
    """Start one side under one variant, probe it, stop it.

    Returns (observations, error).  A launch failure is an observation too — for
    `bad-graph` it is the expected one — so it is returned rather than raised.
    """
    cfg = _materialise(tree, variant, side)
    ports = PORTS[side]
    try:
        srv = server.launch(f"cfg-{side}-{variant}", jar, cfg, tree,
                            tc.WORK / "server-logs",
                            ports["app"], ports["admin"],
                            ready_timeout=300.0)
    except server.LaunchError as exc:
        return None, str(exc)
    try:
        return _probe(srv, requests), None
    finally:
        srv.stop()


def _render(observations: list[list]) -> str:
    """One side's probe results, for a failure detail.

    Truncated per body.  The reason is specific: /info carries GraphHopper's
    country enum, which is several hundred ISO-3166 codes, and a detail that
    dumped it in full buried every finding this module makes underneath it.
    """
    out = []
    for answers in observations:
        first = answers[0] if answers else None
        if first is None:
            out.append("  (no answer recorded)")
        elif isinstance(first, dict):
            out.append(f"  transport error: {first['error']}")
        else:
            body = first.body[:300]
            try:
                text = body.decode("utf-8")
            except UnicodeDecodeError:
                text = f"<{len(first.body)} bytes, not UTF-8>"
            out.append(f"  status {first.status}, "
                       f"{len(first.body)} byte(s): {text}"
                       + ("..." if len(first.body) > 300 else ""))
    return "\n".join(out)


def _decoded(resp) -> bytes:
    """One answer's body with its Content-Encoding undone.

    None of the requests below asks for gzip, but a submission may enable response
    compression by default — Spring Boot has a one-line switch for it — and then
    the bytes here are compressed while diff compares them decoded.  Measuring
    volatility on different bytes than the comparison reads is the bug this
    function exists to not have.
    """
    body, err = normalize.decode_body(normalize.header_map(resp.headers),
                                      resp.body)
    return resp.body if err else body


def _volatility(answers: list) -> dict:
    """One side's measured volatility for one request, in diff's shape."""
    if len(answers) < 2 or any(isinstance(a, dict) for a in answers):
        return {"paths": set(), "unstable_bytes": False}
    return normalize.body_volatility(_decoded(answers[0]),
                                     _decoded(answers[1]))


def _compare_one(cid: str, r: dict, ref: list, sub: list) -> str | None:
    """Compare one request across the two sides; return a message or None.

    Through harness.diff, which is the point of this function existing.  Going
    through diff means it gets the measured-volatility mask, the declared
    wall-clock mask, the authority fold and the header comparison — and that a fix
    to any of those reaches here too, which a private comparison would not.
    """
    ref_err = next((a["error"] for a in ref if isinstance(a, dict)), None)
    sub_err = next((a["error"] for a in sub if isinstance(a, dict)), None)
    if ref_err and sub_err:
        # Both refused to answer.  That IS the agreement for a request whose
        # point is to be refused — a header limit can be enforced by closing the
        # connection rather than by answering 431 — so the two exception TYPES
        # are compared and their messages are not: a message carries the address
        # it failed to reach, which differs by construction.
        if ref_err.split(":")[0] == sub_err.split(":")[0]:
            return None
        return (f"{cid}: both sides refused `{r['target'][:60]}` but in "
                f"different ways\n  reference: {ref_err}\n  submission: "
                f"{sub_err}")
    if ref_err or sub_err:
        which = "reference" if ref_err else "submission"
        other = "submission" if ref_err else "reference"
        return (f"{cid}: the {which} could not answer `{r['target'][:60]}` and "
                f"the {other} did\n  {ref_err or sub_err}")

    entry = {
        "why": r["why"],
        "body_mode": r["body_mode"],
        "reference_volatility": _volatility(ref),
        "submission_volatility": _volatility(sub),
    }
    try:
        diff.compare_case(f"{cid}:{r['target'][:48]}", ref[0], sub[0], set(),
                          entry,
                          (PORTS["reference"][r["port"]],
                           PORTS["submission"][r["port"]]))
    except diff.Mismatch as exc:
        return str(exc)
    return None


def _compare(report: tc.Report, cid: str, subject: str, claim: str,
             requests: list[dict], ref: list, sub: list, weight: float) -> None:
    """One variant's verdict: the two sides answered every request alike.

    Every request is compared before the verdict is recorded, rather than
    stopping at the first difference: one check carries the variant's whole
    weight, so a report that named only the first of three problems would cost
    the same points and say less.

    Two summaries rather than one, because they are different sentences.  `claim`
    is what a pass established and is only ever printed when the check passed.
    `subject` is the noun phrase naming what the variant put to the test, and the
    failure line leads with the disagreement and uses it as context.  Appending
    the failure to the affirmative claim -- which is what this did -- produced
    `both sides bound the ports the config file named and answered on each -- the
    two sides differ on 2 of 2 request(s)`: a headline asserting the property the
    check had just failed to establish, and worse, one that was TRUE and
    irrelevant, since both sides did bind the moved ports and the disagreement was
    about a response header this variant does not exist to measure.  Some renderings
    keep only a finding's first line, so the leading clause is the part that has to
    carry the verdict.
    """
    problems = [m for m in (_compare_one(cid, r, ref[i], sub[i])
                            for i, r in enumerate(requests)) if m]
    report.record(
        cid, not problems,
        claim if not problems else
        f"the two sides differ on {len(problems)} of {len(requests)} request(s) "
        f"under {subject} — so this variant does not establish that {claim}",
        detail="\n\n".join(problems), weight=weight)


def main() -> int:
    report = tc.Report()
    doc_meta = {}
    try:
        doc = capture.load()
        doc_meta = doc.get("meta", {})
    except RuntimeError as exc:
        return tc.grader_failure(report, "capture-present", str(exc))

    trees = {"reference": tc.REFERENCE_TREE, "submission": tc.REPO}
    jars = {}
    for side, tree in trees.items():
        recorded = (doc_meta.get("jars") or {}).get(side)
        if recorded and Path(recorded).exists():
            jars[side] = Path(recorded)
            continue
        try:
            jars[side], _ = maven.find_app_jar(tree)
        except RuntimeError as exc:
            if side == "reference":
                return tc.grader_failure(report, "reference-jar", str(exc))
            report.record("submission-jar", False,
                          "no launchable jar in the submission, so its "
                          "configuration cannot be observed",
                          detail=str(exc), weight=6.0)
            return report.finish()

    # --- ports: the connectors are where the file says ------------------------
    # Reaching the server at all is the observation.  `wait_ready` in the ladder
    # only returns once both ports answer, so a successful launch under moved
    # ports is itself the evidence — and a submission with its ports compiled in
    # fails to be found no matter how well it routes.
    reqs = [
        req("GET", "app", "/info",
            "/info on the moved application port: that it answers at all is the "
            "evidence the connector went where the file said"),
        req("GET", "admin", "/ping",
            "/ping on the moved admin port, which is a second connector "
            "configured by a second stanza of the same file",
            body_mode="exact"),
    ]
    ref, ref_err = _launch("reference", trees["reference"], jars["reference"],
                           "ports", reqs)
    if ref_err:
        return tc.grader_failure(
            report, "reference-ports",
            f"the reference would not start with the ports moved: {ref_err}")
    sub, sub_err = _launch("submission", trees["submission"], jars["submission"],
                           "ports", reqs)
    if sub_err:
        report.record("ports", False,
                      "the submission would not start with the connectors moved "
                      "to the ports the config file names",
                      detail=sub_err, weight=2.0)
    else:
        _compare(report, "ports",
                 "a config file that moves both connectors",
                 "both sides bound the ports the config file named and answered "
                 "on each", reqs, ref, sub, weight=2.0)

    # --- profiles: the routable set comes from the file -----------------------
    # /info reports what is configured, and a request for a profile the file does
    # not list has to fail.  A submission with three profiles compiled in answers
    # the bike request successfully and fails here — which is the whole point of
    # asking twice with different files.
    reqs = [
        req("GET", "app", "/info",
            "/info has to report the profile the file lists and only that one; a "
            "compiled-in profile list is visible here and nowhere else"),
        req("GET", "app",
            "/route?point=42.5063,1.5218&point=42.5432,1.5906&profile=car",
            "the profile the file DOES list still routes, so a submission "
            "cannot pass the next request by refusing everything"),
        req("GET", "app",
            "/route?point=42.5063,1.5218&point=42.5432,1.5906&profile=bike",
            "the profile the file OMITS has to fail, with the same status and "
            "the same error envelope as upstream"),
    ]
    ref, ref_err = _launch("reference", trees["reference"], jars["reference"],
                           "profiles", reqs)
    if ref_err:
        return tc.grader_failure(
            report, "reference-profiles",
            f"the reference would not start with one profile: {ref_err}")
    sub, sub_err = _launch("submission", trees["submission"], jars["submission"],
                           "profiles", reqs)
    if sub_err:
        report.record("profiles", False,
                      "the submission would not start with a single profile "
                      "configured",
                      detail=sub_err, weight=3.0)
    else:
        _compare(report, "profiles",
                 "a config file that lists one profile where the base lists three",
                 "both sides route on exactly the profiles the file lists, and "
                 "both refuse the one it omits", reqs, ref, sub, weight=3.0)

    # --- header limit: the value reached the container ------------------------
    # A configuration object that holds the right number and an embedded container
    # that was never told about it is a real and common outcome of this kind of
    # migration.  The only way to see the difference is to send a request that the
    # limit decides the fate of.
    reqs = [
        req("GET", "app", "/info",
            "a request that fits under the lowered limit still answers, which "
            "is what makes the next one's refusal mean something"),
        req("GET", "app", "/info?pad=" + "x" * 9000,
            "a request over the configured limit has to meet the same fate on "
            "both sides.  The graded observation is the STATUS -- or, if the "
            "container enforces the limit by closing the connection instead of "
            "answering, that both sides close it.  The body is the container's "
            "own error page and is not compared: it names the servlet instance "
            "with a per-JVM identity hash in it",
            body_mode="ignore"),
    ]
    ref, ref_err = _launch("reference", trees["reference"], jars["reference"],
                           "header-limit", reqs)
    if ref_err:
        return tc.grader_failure(
            report, "reference-header-limit",
            f"the reference would not start with a lowered header limit: "
            f"{ref_err}")
    sub, sub_err = _launch("submission", trees["submission"], jars["submission"],
                           "header-limit", reqs)
    if sub_err:
        report.record("header-limit", False,
                      "the submission would not start with a lowered "
                      "max_request_header_size",
                      detail=sub_err, weight=2.0)
    else:
        _compare(report, "header-limit",
                 "a config file that lowers max_request_header_size",
                 "the configured request-size limit reached the embedded "
                 "container on both sides: the oversized request meets the same "
                 "fate", reqs, ref, sub, weight=2.0)

    # --- bad graph: startup fails rather than serving nothing -----------------
    # Both sides must refuse.  A submission that starts anyway and serves an empty
    # graph has turned a configuration error into wrong answers, which is worse
    # than a crash and much harder to notice.
    bad_reqs = [req("GET", "app", "/info",
                    "asked only if the launch wrongly succeeded, to show what a "
                    "server with no graph behind it answers")]
    ref_obs, ref_err = _launch("reference", trees["reference"], jars["reference"],
                               "bad-graph", bad_reqs)
    sub_obs, sub_err = _launch("submission", trees["submission"],
                               jars["submission"], "bad-graph", bad_reqs)
    if ref_err is None:
        return tc.grader_failure(
            report, "reference-bad-graph",
            "the reference STARTED with datareader.file naming a file that does "
            "not exist, so this variant does not test what it claims to and no "
            "verdict from it would be meaningful.")
    report.record(
        "bad-graph", sub_err is not None,
        "both sides refuse to start when the configured OSM input does not exist"
        if sub_err is not None else
        "the submission started with datareader.file naming a file that does not "
        "exist — a configuration error became a running service with no data in "
        "it, which answers requests wrongly instead of refusing to start",
        detail="" if sub_err is not None else _render(sub_obs), weight=2.0)

    return report.finish({"variants": ["ports", "profiles", "header-limit",
                                       "bad-graph"]})


if __name__ == "__main__":
    sys.exit(main())
