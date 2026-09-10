#!/usr/bin/env python3
"""Build-time sanity check for stage 3's configuration and its brief.

Runs inside the stage-3 image build.  ``swerefactor validate`` checks the probe.toml
half while a task is being authored; this is the backstop for the case where a
file was edited and only the image was rebuilt.  Failing here costs a build.
Failing at run time costs six rounds of model time and produces a score that is
wrong rather than absent.

evaluation.toml is not in this build context -- it is one level up, in tests/ --
so the adversary count is compared against a number recorded here at authoring
time rather than read from the scoring policy.  ``swerefactor validate`` compares
against the real thing.

WHY THE PROMPT IS CHECKED TOO

The adversary's tools reach the two repository trees and nothing else, so it
cannot read probe.c to learn how to call the probe.  The op table in prompt.txt is
therefore the only statement of that grammar it will ever see, which makes the
table load bearing rather than documentary: an op named there that the probe does
not have spends a round's budget collecting `badop` records, and a verb named
there that the dispatch chain does not recognise silently runs the default branch
instead -- which looks like a passing test on both trees and is in fact two tests
of the same thing.

So the table is derived from three files and they have to agree: probe.c's `g_ops`
array and its `strcmp` chains, Probe.java's `case` labels and its `.equals`
chains, and srbzlib's OPS.  A verb the prompt lists must be either a branch in
both dispatch chains or the documented default the code falls through to; a branch
in the chains that the prompt does not list is also an error, because it is a
capability the round cannot reach.

This is a coverage claim about prose, and the only way to keep one honest is to
compute it.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from swerefactor import config

#: What [scoring] verification_models says in tests/evaluation.toml.
EXPECTED_ADVERSARIES = 6

# Derived from this file rather than written as /tests/verification so the check is
# runnable where the files are authored as well as where they are installed.  The
# Dockerfile does `COPY . /tests/verification`, so in the image the two are the same
# directory.
ROOT = Path(__file__).resolve().parent
PROMPT = ROOT / "prompt.txt"
PROBE_C = ROOT / "lib" / "probe" / "probe.c"
PROBE_JAVA = ROOT / "lib" / "probe" / "Probe.java"
SRBZLIB = ROOT / "lib" / "srbzlib.py"


def _ops_from_c(text: str) -> set[str]:
    """The op names probe.c dispatches on."""
    return set(re.findall(r'strcmp\(op, "([a-z]+)"\) == 0', text))


def _ops_from_c_listing(text: str) -> set[str]:
    """The op names probe.c reports for --list-keys.

    Read separately from the dispatch chain on purpose: these are the two places
    the C half states its own op set, and structure.py compares the listing
    against the catalog, so a name in one and not the other is a real defect.
    """
    match = re.search(r"static const char \*const g_ops\[\] = \{(.*?)\};",
                      text, re.S)
    if not match:
        return set()
    return set(re.findall(r'"([a-z]+)"', match.group(1)))


def _ops_from_java(text: str) -> set[str]:
    return set(re.findall(r'case "([a-z]+)"', text))


def _ops_from_srbzlib(text: str) -> set[str]:
    match = re.search(r"^OPS = \((.*?)\)", text, re.S | re.M)
    if not match:
        return set()
    return set(re.findall(r'"([a-z]+)"', match.group(1)))


def _arity_from_c(text: str) -> dict[str, int]:
    """How many arguments each op actually reads.

    Fields are `<case-id> <op> <arg>...`, so the highest field index an op touches
    minus one is its argument count.  Both ways the C half reads a field count:
    `arg_long(f, nf, N, ...)` and `nf > N ? f[N] : "..."`.

    Worth checking because an argument named in the brief that the probe does not
    read is worse than a missing one: the round varies it, sees no change on either
    tree, and concludes the two agree about something neither one was asked.
    """
    out: dict[str, int] = {}
    starts = [(m.group(1), m.start()) for m in
              re.finditer(r"static void op_([a-z]+)\(buf \*out", text)]
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        body = text[pos:end]
        indices = [int(n) for n in
                   re.findall(r"arg_long\(f, *nf, *(\d+)", body)]
        indices += [int(n) for n in
                    re.findall(r"nf > (\d+) \? f\[\d+\]", body)]
        if indices:
            out[name] = max(indices) - 1
    return out


def _verbs_from_c(text: str) -> dict[str, list[str]]:
    """Per-op dispatch verbs, from probe.c's own function bodies.

    Sliced by function rather than searched globally so a verb is attributed to
    the op that actually branches on it -- `getdict`, `copy`, `prime` and `reset`
    each appear in two different ops with different meanings.
    """
    out: dict[str, list[str]] = {}
    starts = [(m.group(1), m.start()) for m in
              re.finditer(r"static void op_([a-z]+)\(buf \*out", text)]
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        body = text[pos:end]
        verbs: list[str] = []
        for var in ("what", "kind", "damage"):
            verbs += re.findall(r'strcmp\(%s, "([A-Za-z0-9_]+)"\)' % var, body)
        if verbs:
            out[name] = verbs
    return out


def _defaults_from_c(text: str) -> dict[str, str]:
    """Each op's fall-through verb: `const char *what = nf > 3 ? f[3] : "reset"`.

    A verb the prompt names that is not a dispatch branch is legitimate only if it
    is one of these -- `roundtrip` for gzfile is the real case, and it is the op's
    most useful setting rather than an omission.
    """
    out: dict[str, str] = {}
    starts = [(m.group(1), m.start()) for m in
              re.finditer(r"static void op_([a-z]+)\(buf \*out", text)]
    for i, (name, pos) in enumerate(starts):
        end = starts[i + 1][1] if i + 1 < len(starts) else len(text)
        body = text[pos:end]
        match = re.search(
            r'const char \*(?:what|kind|damage) = nf > \d+ \? f\[\d+\] : "([a-z0-9_]+)"',
            body)
        if match:
            out[name] = match.group(1)
    return out


def _verbs_from_java(text: str) -> list[str]:
    """Every dispatch verb Probe.java branches on, in file order.

    Not sliced per op: the Java half's method boundaries do not line up with the C
    half's function boundaries closely enough to slice reliably, and what is being
    checked here is that the two halves recognise the same *set* -- a verb one half
    handles and the other does not is a case that cannot be compared at all.
    """
    return re.findall(r'(?:what|kind|damage)\.equals\("([A-Za-z0-9_]+)"\)', text)


#: A line in the table that opens an op: two spaces, the name, then its first
#: argument.  A continuation line is indented further, so it cannot match.
_OP_LINE = re.compile(r"^ {2}([a-z]+) {2,}([a-z_].*)$")

#: A continuation line that is more argument names rather than prose.  deflate has
#: ten arguments and its list wraps; the wrapped half has to be folded in, or the
#: arity check below reads deflate as taking seven.
_ARG_CONT = re.compile(r"^ {3,}[a-z][a-z_]*(?: +[a-z][a-z_]*)*$")


def _prompt_table(text: str) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse the op table out of the brief.

    Shape, which this depends on:

        <2 spaces><op><spaces><arg names...>
        <more deeply indented><more arg names, if the list wrapped>
                -- prose, possibly including "what is one of: a b c."

    Argument continuations are distinguished from prose by shape and by position:
    they are bare lowercase words, and they stop at the op's first `--`.
    """
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines)
                     if ln.startswith("THE OPS AND THEIR ARGUMENTS"))
    except StopIteration:
        return {}, {}
    ops: dict[str, list[str]] = {}
    notes: dict[str, str] = {}
    current: str | None = None
    in_prose = False
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            if re.match(r"^[A-Z][A-Z ]+$", line):
                break
            continue
        match = _OP_LINE.match(line)
        if match:
            current = match.group(1)
            ops[current] = match.group(2).split()
            notes[current] = ""
            in_prose = False
        elif current and line.strip().startswith("--"):
            in_prose = True
            notes[current] += " " + line.strip()
        elif current and not in_prose and _ARG_CONT.match(line):
            ops[current] += line.split()
        elif current and line.strip():
            notes[current] += " " + line.strip()
    return ops, notes


def _prompt_verbs(note: str) -> list[str]:
    """The verb list from one op's note, if it states one."""
    match = re.search(r"(?:what|kind|damage) is one of:([^.]*)\.", note)
    if not match:
        return []
    return match.group(1).split()


def main() -> int:
    problems: list[str] = []

    # ---- probe.toml ---------------------------------------------------------
    probe = config.Probe.load(str(ROOT / "probe.toml"))

    if len(probe.adversaries) != EXPECTED_ADVERSARIES:
        problems.append(
            f"probe.toml declares {len(probe.adversaries)} adversaries; the "
            f"scoring policy pays for {EXPECTED_ADVERSARIES}"
        )
    if not probe.scope.allow and not probe.scope.deny:
        problems.append(
            "probe.toml [scope] states neither allow nor deny, so every "
            "divergence counts and no submission can survive a round"
        )
    if not probe.candidate_command:
        problems.append("probe.toml declares no candidate_command")

    # An adversary with no budget is a round that pays ten points for nothing.
    for adv in probe.adversaries:
        if adv.budget_sec <= 0:
            problems.append(f"adversary {adv.id!r} has a non-positive budget")
        if not str((adv.metadata or {}).get("focus") or "").strip():
            # Not fatal to the harness, but six unguided rounds converge on the
            # same three obvious cases, which is a weaker measurement bought at the
            # same price.
            problems.append(f"adversary {adv.id!r} has no focus")

    # ---- the op grammar, across four files ---------------------------------
    c_text = PROBE_C.read_text(encoding="utf-8")
    java_text = PROBE_JAVA.read_text(encoding="utf-8")
    prompt_text = PROMPT.read_text(encoding="utf-8")
    lib_text = SRBZLIB.read_text(encoding="utf-8")

    c_ops = _ops_from_c(c_text)
    c_listed = _ops_from_c_listing(c_text)
    java_ops = _ops_from_java(java_text)
    lib_ops = _ops_from_srbzlib(lib_text)
    table, notes = _prompt_table(prompt_text)
    prompt_ops = set(table)

    if not c_ops:
        problems.append("no op dispatch chain found in probe.c; this check is "
                        "reading the wrong file or the wrong shape")
    for label, got in (("probe.c --list-keys", c_listed),
                       ("Probe.java", java_ops),
                       ("srbzlib.OPS", lib_ops),
                       ("prompt.txt's op table", prompt_ops)):
        if got != c_ops:
            missing = sorted(c_ops - got)
            extra = sorted(got - c_ops)
            detail = []
            if missing:
                detail.append(f"missing {missing}")
            if extra:
                detail.append(f"has {extra} which probe.c does not dispatch")
            problems.append(f"{label} disagrees with probe.c's ops: "
                            + "; ".join(detail))

    # Every op in the table must name every argument the probe reads, and no
    # others.  Too few and the round cannot reach a knob that exists; too many and
    # it spends candidates varying one that does not.
    c_arity = _arity_from_c(c_text)
    for op in sorted(prompt_ops & set(c_arity)):
        stated, real = len(table[op]), c_arity[op]
        if stated != real:
            problems.append(
                f"prompt.txt's table gives {op} {stated} argument(s) "
                f"({' '.join(table[op])}) and probe.c reads {real}"
            )
    for op in sorted(prompt_ops - set(c_arity)):
        problems.append(f"could not find probe.c's argument reads for op {op!r}")

    # ---- the verb lists ----------------------------------------------------
    c_verbs = _verbs_from_c(c_text)
    c_defaults = _defaults_from_c(c_text)
    java_verbs = set(_verbs_from_java(java_text))

    all_c_verbs = {v for verbs in c_verbs.values() for v in verbs}
    if all_c_verbs != java_verbs:
        problems.append(
            "the two probe halves branch on different verbs: "
            f"C-only {sorted(all_c_verbs - java_verbs)}, "
            f"Java-only {sorted(java_verbs - all_c_verbs)}"
        )

    for op, verbs in sorted(c_verbs.items()):
        stated = _prompt_verbs(notes.get(op, ""))
        if not stated:
            problems.append(
                f"probe.c's {op} branches on {len(verbs)} verbs and prompt.txt's "
                f"table states none, so the round cannot reach any of them"
            )
            continue
        allowed = set(verbs) | {c_defaults[op]} if op in c_defaults else set(verbs)
        unknown = sorted(set(stated) - allowed)
        unstated = sorted(set(verbs) - set(stated))
        if unknown:
            problems.append(
                f"prompt.txt states verb(s) {unknown} for {op}, which are neither "
                f"a dispatch branch nor its default -- a round using one gets the "
                f"default branch and does not know it"
            )
        if unstated:
            problems.append(
                f"prompt.txt omits verb(s) {unstated} for {op}, which the probe "
                f"can do and the round therefore cannot ask for"
            )

    # ---- report ------------------------------------------------------------
    if problems:
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"probe: {len(probe.adversaries)} adversaries, "
          f"{probe.scope.reruns} reruns, {len(probe.scope.allow)} in scope / "
          f"{len(probe.scope.deny)} out")
    print(f"grammar: {len(c_ops)} ops and "
          f"{sum(len(v) for v in c_verbs.values())} verbs, agreeing across "
          f"probe.c, Probe.java, srbzlib and the brief")
    return 0


if __name__ == "__main__":
    sys.exit(main())
