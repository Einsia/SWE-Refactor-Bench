#!/usr/bin/env python3
"""Build-time sanity check for probe.toml.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks the same things
while a task is being authored; this is the backstop for the case where a probe was
edited and only the image was rebuilt.  Failing here costs a build.  Failing at run
time costs six rounds of model time and produces a score that is wrong rather
than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ -- so
the adversary count is compared against a number recorded here at authoring time
rather than read from the scoring policy.  ``swerefactor validate`` compares against
the real thing.
"""
from __future__ import annotations

import sys
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: The configurations srbcrypto offers.  The prompt names them, a candidate asks for
#: them by name, and a name in the prose that the module does not have is a round
#: spent on `KeyError`.
EXPECTED_CONFIGURATIONS = ("default", "no-aesni", "no-clmul", "no-isa")


def main() -> int:
    root = Path(__file__).resolve().parent
    probe = config.Probe.load(root / "probe.toml")
    problems: list[str] = []

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        problems.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}"
        )
    if not probe.scope.allow and not probe.scope.deny:
        problems.append(
            "probe.toml [scope] states neither allow nor deny, so every divergence "
            "counts and no submission can survive a round"
        )
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")

    # An adversary with no budget is a round that pays ten points for nothing.
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")

    prompt = root / probe.prompt
    if not prompt.is_file():
        problems.append(f"probe.toml names a prompt that is not here: {probe.prompt}")
    else:
        # The prompt is what a round is briefed with, and a configuration named
        # there that srbcrypto does not have costs a candidate rather than
        # producing an error anyone would read.  Cheap to check, and this is the
        # only place both files are open at once.
        text = prompt.read_text(encoding="utf-8")
        for name in EXPECTED_CONFIGURATIONS:
            if name not in text:
                problems.append(
                    f"prompt.txt never mentions the {name!r} configuration")
        for template in ("{{original}}", "{{submission}}", "{{task}}"):
            if template not in text:
                problems.append(f"prompt.txt does not use {template}")

    problems.extend(check_fault_plugin(root))

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out")
    return 0


def check_fault_plugin(root: Path) -> list[str]:
    """lib/srbfault.py is this task's "the tree does not build" defence.

    Checked here because every way it can break is silent. Its table is keyed by
    (module, class): rename the lib, rename BuildFailed, or drop `-p srbfault` from
    run-candidate.sh, and the plugin simply never fires. Nothing errors -- an
    uncaught BuildFailed goes back to being pytest's exit 1, the adjudicator reads
    "fails on the submission", and six rounds charge up to 60 points for a tree that
    does not build. A build failing here costs a build; this failing at run time
    costs a score that is wrong rather than absent.
    """
    problems: list[str] = []
    # No .pyc for the imports below.  Run from a checkout rather than from the
    # image, this would otherwise leave a __pycache__/ in the stage directory, and
    # every stage Dockerfile ends in `COPY . /tests/<stage>` -- which is exactly the
    # thing `swerefactor validate` rejects a stage directory for.
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root / "lib"))
    try:
        import srbfault
    except Exception as exc:                       # noqa: BLE001
        return [f"lib/srbfault.py does not import ({exc!r}), so the `-p srbfault`"
                f" in run-candidate.sh would fail every candidate run"]

    from swerefactor.verification import FAULT_EXIT_CODES

    if not srbfault.FAULTS:
        problems.append("srbfault.FAULTS is empty, so no exception is a fault")
    for (mod_name, cls_name), code in sorted(srbfault.FAULTS.items()):
        try:
            mod = __import__(mod_name)
        except Exception as exc:                   # noqa: BLE001
            problems.append(
                f"srbfault.FAULTS names module {mod_name!r}, which does not import "
                f"({exc!r}); the plugin would never fire")
            continue
        cls = getattr(mod, cls_name, None)
        if cls is None:
            problems.append(
                f"srbfault.FAULTS names {mod_name}.{cls_name}, which does not "
                f"exist; the plugin would never fire")
        elif not (isinstance(cls, type) and issubclass(cls, BaseException)):
            problems.append(f"{mod_name}.{cls_name} is not an exception class")
        if code not in FAULT_EXIT_CODES:
            problems.append(
                f"srbfault maps {mod_name}.{cls_name} to exit {code}, which is "
                f"outside FAULT_EXIT_CODES ({min(FAULT_EXIT_CODES)}-"
                f"{max(FAULT_EXIT_CODES)}); the adjudicator would read it as a "
                f"verdict about behaviour rather than as a fault")

    runner = root / "run-candidate.sh"
    if runner.is_file() and "-p srbfault" not in runner.read_text(encoding="utf-8"):
        problems.append(
            "run-candidate.sh does not register the plugin with `-p srbfault`, so a "
            "build failure would reach the adjudicator as pytest's exit 1")
    return problems


if __name__ == "__main__":
    sys.exit(main())
