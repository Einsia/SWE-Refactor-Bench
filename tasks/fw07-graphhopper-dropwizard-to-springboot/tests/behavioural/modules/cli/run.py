"""Command-line dispatch, by exit code and on-disk effect.

The jar is not only a server.  Its argv selects a command — `server`, `check`,
`import`, `match` — and that dispatch is documented interface: `import` is how a
deployment builds its graph cache without serving traffic, and it is the first
thing every deployment guide tells an operator to run.  A rewrite that keeps the
HTTP surface and loses the CLI has dropped a feature its users have in their shell
history.

Six invocations, run on both sides in isolated directories, compared on exit code
and on whether a graph cache appeared.  Nothing else — the reasoning for that
narrowness is in harness/cli.py, and it comes down to this: an exit code is what a
shell script branches on, and the prose above it is the framework's to write.

`cli-no-args` deserves a note.  A launcher that dispatches on argv refuses and
exits non-zero; one that ignores argv starts a server and hangs.  The harness
records a timeout as exit -1, which makes those two outcomes comparable values
rather than one value and one crash — so a submission that turned the CLI into an
always-server passes nothing here and fails with a reason rather than with a stack
trace from the grader.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/lib")

import toolchain as tc                                        # noqa: E402
from harness import capture, cli, maven                        # noqa: E402


def _config_for(tree: Path, side: str) -> Path:
    """Materialise the CLI config into the side's tree.

    `graph.location` in this template is relative on purpose — see the file — so
    only @REPO_ROOT@ needs substituting.
    """
    text = ((tc.DATA / "configs" / "variants" / "cli.yml").read_text()
            .replace("@REPO_ROOT@", str(tree)))
    out = tree / "srb-cli.yml"
    out.write_text(text)
    return out


def main() -> int:
    report = tc.Report()

    meta = {}
    try:
        meta = capture.load().get("meta", {})
    except RuntimeError as exc:
        return tc.grader_failure(report, "capture-present", str(exc))

    trees = {"reference": tc.REFERENCE_TREE, "submission": tc.REPO}
    jars: dict[str, Path] = {}
    for side, tree in trees.items():
        recorded = (meta.get("jars") or {}).get(side)
        if recorded and Path(recorded).exists():
            jars[side] = Path(recorded)
            continue
        try:
            jars[side], _ = maven.find_app_jar(tree)
        except RuntimeError as exc:
            if side == "reference":
                return tc.grader_failure(report, "reference-jar", str(exc))
            report.record(
                "submission-jar", False,
                "no launchable jar in the submission, so its command line cannot "
                "be exercised", detail=str(exc), weight=6.0)
            return report.finish()

    configs = {s: _config_for(t, s) for s, t in trees.items()}
    invocations = cli.invocations("srb-cli.yml")
    observed: dict[str, dict] = {}

    for inv in invocations:
        results = {}
        for side in ("reference", "submission"):
            results[side] = cli.run(
                jars[side], inv, tc.WORK / f"cli-{side}", configs[side],
                tc.WORK / "cli-logs" / side)
        observed[inv.id] = {
            side: {"code": r.code, "effect": r.effect}
            for side, r in results.items()}
        try:
            note = cli.compare(inv, results["reference"], results["submission"])
        except AssertionError as exc:
            report.record(inv.id, False, f"{inv.id}: the two sides differ",
                          detail=str(exc), weight=1.0)
        else:
            report.record(inv.id, True, note, weight=1.0)

    # An unweighted observation, because it is worth reading and is not the
    # submission's fault if it is false: whether the reference's own CLI produced
    # distinguishable exit codes at all.  If every invocation on the reference
    # exited the same way, the six cases above are comparing one value to itself
    # and are weaker than they look.
    codes = {inv.id: observed[inv.id]["reference"]["code"] for inv in invocations}
    report.note(
        "reference-discriminates",
        f"the reference produced {len(set(codes.values()))} distinct exit "
        f"code(s) across {len(invocations)} invocation(s): "
        + ", ".join(f"{k}={v}" for k, v in codes.items()))

    return report.finish({"observed": observed})


if __name__ == "__main__":
    sys.exit(main())
