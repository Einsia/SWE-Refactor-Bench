#!/usr/bin/env python3
"""Build-time check for stage 3's configuration and its candidate library.

Runs inside the stage-3 image build.  `swerefactor validate` and `tests/check-task.py`
check more, from the host, where they can see the whole task; this is the backstop
for the case where something here was edited and only the image was rebuilt.
Failing here costs a build.  Failing at run time costs six rounds of model time
and produces a score that is wrong rather than absent.

Two things are recorded rather than derived, because they live outside this build
context -- the context is `tests/verification/`, so `tests/evaluation.toml` and
`environment/source-contract.json` are both invisible from in here.  Each is
checked against the real file by `tests/check-task.py` on the host; the numbers
below exist so that deleting half the deny list, or paying for a thirteenth round,
does not get all the way to a grading run.

The interesting check is the last one: every public name in `srbyaml` is mentioned
in `prompt.txt`, and every `srbyaml.x` and `probe().x` that `prompt.txt` promises
exists in `srbyaml`.  An adversary calling a method the prompt invented spends a
candidate on a TypeError, and it reads as "this test fails on both trees" -- which
is the most expensive possible way to learn about a documentation bug.  That
happened once while this was being written: the seven operations existed as module
functions, `prompt.txt` documented them as `probe().node(...)`, and `Probe` had no
`node`.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from swerefactor import config

HERE = Path("/tests/verification")

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

#: How many surfaces environment/source-contract.json marks `not_graded`.  Each
#: one must appear in [scope] deny or the round is unwinnable in the wrong
#: direction -- an adversary would break the submission on a surface the
#: instruction told the agent not to port.  Which is which is checked on the host;
#: this only notices the list getting shorter.
NOT_GRADED_SURFACES = 7

#: Files this stage cannot run without.  A missing one is a broken COPY, and the
#: failure it produces at run time ("candidate_command not found") does not say so.
REQUIRED = ("probe.toml", "prompt.txt", "run-candidate.sh", "screen-candidate.py",
            "lib/srbyaml.py")

#: Placeholders audit.render_prompt fills in.  A prompt that never mentions the
#: two trees is a prompt that sends the adversary looking for them.
REQUIRED_PLACEHOLDERS = ("{{task}}", "{{original}}", "{{submission}}", "{{roots}}",
                         "{{commands}}")


def public_names(path: Path) -> set[str]:
    """Every name `srbyaml` exports, by reading it rather than importing it.

    Parsed rather than imported so this check cannot be affected by an import
    side effect, and so it works whether or not SRB_PROBE is set in the build.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return {n for n in names if not n.startswith("_")}


def class_members(path: Path, name: str) -> set[str]:
    """One class's public members, including the ones `__init__` assigns.

    Per class, not merged across classes.  `Probe` and `Response` both have a
    `node`, so a merged set makes `probe().node` satisfiable by `Response.node`,
    and renaming the method the prompt documents stays green.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or cls.name != name:
            continue
        for node in ast.walk(cls):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(node.name)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                found.add(node.target.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id == "self" and isinstance(node.ctx, ast.Store):
                found.add(node.attr)
    return {n for n in found if not n.startswith("_")}


def prompt_promises(text: str) -> tuple[set[str], set[str]]:
    """Names prompt.txt attributes to the library: (module-level, on a Probe).

    Two sets rather than one, and checked against two namespaces, because the
    union does not catch the bug this exists for.  `srbyaml.node` and
    `probe().node` are different promises: moving `node` from `Probe` to the module
    while the prompt says `probe().node(...)` leaves the name exported and the
    documented call a TypeError.  That mutation passes a check that asks only
    "does this name exist somewhere".

    Only the two unambiguous forms are matched.  Bare mentions in prose are not:
    the prompt lists the node helpers as `doc_root(r)`, `kids(n)`, and a regex
    loose enough to catch those also catches every builtin the prompt mentions.
    """
    module = set(re.findall(r"\bsrbyaml\.([A-Za-z_][A-Za-z_0-9]*)", text))
    on_probe = set(re.findall(r"\bprobe\(\)\.([A-Za-z_][A-Za-z_0-9]*)", text))
    # `with srbyaml.Probe() as p:` -- the prompt's own example binds it to `p`.
    on_probe |= set(re.findall(r"\bp\.([A-Za-z_][A-Za-z_0-9]*)", text))
    return module, on_probe


def main() -> int:
    problems: list[str] = []

    for name in REQUIRED:
        if not (HERE / name).is_file():
            problems.append(f"{name} is missing from the image")
    if problems:                      # nothing below can run without these
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    probe = config.Probe.load(str(HERE / "probe.toml"))

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        problems.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}")
    if not probe.scope.allow:
        problems.append(
            "probe.toml [scope] states no allow list, so nothing is in scope and "
            "no candidate can be upheld")
    if len(probe.scope.deny) < NOT_GRADED_SURFACES:
        problems.append(
            f"probe.toml [scope] deny has {len(probe.scope.deny)} entries; "
            f"source-contract.json marks {NOT_GRADED_SURFACES} surfaces not "
            f"graded and every one of them has to be denied")
    if probe.scope.reruns < 2:
        problems.append(
            f"reruns = {probe.scope.reruns}: one run cannot tell a divergence "
            f"from a flake, and a flake costs the submission ten points")
    if probe.scope.network_allowed:
        problems.append("network_allowed = true, in a stage that grades a library")
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")
    else:
        # The script the harness will actually exec.  A candidate_command naming a
        # path that is not in the image fails once per round, six times.
        script = next((c for c in probe.candidate_command if c.endswith(".sh")), None)
        if script and not Path(script).is_file():
            problems.append(
                f"candidate_command names {script}, which is not in the image")

    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        if not adv.metadata.get("focus"):
            problems.append(
                f"adversary {adv.id!r} has no focus; six unguided rounds all "
                f"attack the same three scalars")

    meta = probe.metadata
    for key in ("candidate_timeout_sec", "run_timeout_sec"):
        if float(meta.get(key, 0)) <= 0:
            problems.append(f"[metadata] {key} is missing or non-positive")
    if float(meta.get("candidate_timeout_sec", 0)) <= \
            float(meta.get("run_timeout_sec", 0)):
        problems.append(
            "[metadata] candidate_timeout_sec must exceed run_timeout_sec: the "
            "first candidate on a tree pays for the build as well as the run")

    prompt_path = HERE / probe.prompt
    if not prompt_path.is_file():
        problems.append(f"probe.toml names prompt {probe.prompt!r}, which is missing")
        prompt_text = ""
    else:
        prompt_text = prompt_path.read_text(encoding="utf-8")
        for placeholder in REQUIRED_PLACEHOLDERS:
            if placeholder not in prompt_text:
                problems.append(f"{probe.prompt} never uses {placeholder}")

    # --- the library and the prompt have to describe the same API -------------
    lib = HERE / "lib" / "srbyaml.py"
    exported = public_names(lib)
    on_probe = class_members(lib, "Probe")
    promised_module, promised_probe = prompt_promises(prompt_text)

    invented = sorted(f"srbyaml.{n}" for n in promised_module if n not in exported)
    invented += sorted(f"probe().{n}" for n in promised_probe if n not in on_probe)
    if invented:
        problems.append(
            f"prompt.txt promises {', '.join(invented)}, which srbyaml does not "
            f"have. An adversary calling one spends a candidate on a TypeError, "
            f"and it reads as a test that fails on both trees.")

    # The other direction: something a candidate is given but never told about is
    # something no candidate will use.
    undocumented = sorted(
        n for n in exported
        if n not in prompt_text and not n.isupper() and n != "OPERATIONS")
    if undocumented:
        problems.append(
            f"srbyaml exports {', '.join(undocumented)}, which prompt.txt never "
            f"mentions")

    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, {probe.scope.reruns} "
          f"reruns, {len(probe.scope.allow)} in scope / {len(probe.scope.deny)} "
          f"out; srbyaml exports {len(exported)} names, all documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())
