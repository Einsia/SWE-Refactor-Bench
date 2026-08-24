#!/usr/bin/env python3
"""The `types` module: is the type surface a description or a decoration?

A port that evaluates every expression correctly and ships

    declare const jsonata: any;
    export = jsonata;

has not done this task, and no amount of behavioural corpus would say so -- `any`
behaves identically at runtime, so all 13,940 cases pass. This module is the half of
"ported to TypeScript" that the corpus structurally cannot reach.

It is measured the same way the behaviour is: by compiling programs against the
declarations and reading what the compiler says. Nothing here looks at the text of a
`.d.ts`. That distinction is the whole design -- a token scan for `: any` would
punish the `any` upstream legitimately uses for JSONata *values*, which really are
arbitrary JSON, while missing a declaration that types the values precisely and the
API loosely. Twelve programs that must compile and twelve that must be rejected
answer the question the scan was reaching for, and they answer it in the compiler's
vocabulary rather than in a regex.

The twelve rejections are where the care went
---------------------------------------------
A negative that is merely "rejected" proves nothing: a declaration missing
`errors()` entirely rejects `n12-errors-arity` too, for a reason `p08-errors`
already charges. Scoring that would pay twice for one defect. So every negative
declares the diagnostic codes that mean *rejected for the stated reason*, and the
rejection must land on the line the consumer marks -- `// @expect-error`, the line
before the offending one. Wrong line or wrong code is not a pass, and the report says
which of the two it was.

The accept-sets hold alternatives because TypeScript has more than one correct answer
here: an overload set reports TS2769 where a single signature reports TS2345. That
was measured rather than guessed -- see `catalog.TYPE_CASES`, where an independently
written declaration using overloads, `unknown` in place of `any`, and
`...args: never[]` produced identical verdicts and identical codes on all 24 rows.

How the module gets resolved
---------------------------
The harness's own `tsconfig.json` maps `jsonata` to a fixed local name, and this
module links that name to whatever declaration file the submission's *manifest*
points at. Reading the manifest is how a consumer finds types, so this resolves the
package the way `npm` and `tsc` would; it is not a check on what the manifest says.
A port free to emit its declarations as `dist/index.d.ts` and say so stays free to.
What it cannot do is emit declarations no consumer can find -- that fails all 24
rows, which is where it should be charged and not in a manifest assertion.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import catalog
import vlib

CONSUMER_DIR_ENV = "SWEREFACTOR_ASSETS"
CONSUMER_SUBDIR = "consumers"

# The name `tsconfig.json`'s `paths` maps `jsonata` to. A fixed local name inside the
# per-case work directory, so no path into the submission's tree ever appears in the
# project the compiler is handed.
LINK_NAME = "jsonata.d.ts"

# One compile of one small file against one small declaration file. Generous because
# a timeout here would read as a submission failure; `skipLibCheck` is off, so a
# declaration that pulls in half the standard library is slower, and that is allowed.
TSC_TIMEOUT = 180.0

# `case.ts(5,25): error TS2322: ...`  Column ignored: it moves with how an expression
# is spelled, and the line is what the marker pins.
DIAGNOSTIC = re.compile(
    r"^(?P<file>[^(\n]+)\((?P<line>\d+),(?P<col>\d+)\):\s+error\s+TS(?P<code>\d+):"
    r"\s*(?P<text>.*)$")

MARKER = "@expect-error"


def _weights() -> dict[str, float]:
    return catalog.type_weights()


def _tsc(log) -> str:
    """The compiler, resolved absolutely off the PATH `base_env` pins.

    A harness fault if it is missing, and raised as one: the image vendors
    typescript, and if it did not, this module could not distinguish a bad
    declaration from an absent compiler -- every case would fail identically.
    """
    path = vlib.base_env()["PATH"]
    found = shutil.which("tsc", path=path)
    if not found:
        raise SystemExit(
            f"types: tsc is not on the PATH base_env pins ({path}); the verifier "
            f"image vendors typescript, so this is a harness fault and says nothing "
            f"about the submission")
    log.write(f"types: tsc resolved to {found}")
    return found


def _declarations(build_dir: Path) -> tuple[Path | None, str]:
    """The declaration file a consumer of this package would be given, and how.

    `package.json`'s `types` (or its older spelling `typings`) is where a consumer
    looks, so it is where this looks. The fallbacks are the two node and tsc would
    themselves try -- the declaration sitting beside `main`, and `index.d.ts` in the
    package root -- and nothing beyond them, because a fallback invented here would
    let a port whose declarations no real consumer can resolve still be graded as
    though they were resolvable.
    """
    manifest_path = build_dir / "package.json"
    manifest: dict = {}
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8",
                                                        errors="replace"))
            manifest = loaded if isinstance(loaded, dict) else {}
        except ValueError as exc:
            return None, f"package.json is not valid JSON ({exc})"

    for field in ("types", "typings"):
        declared = manifest.get(field)
        if isinstance(declared, str) and declared.strip():
            candidate = (build_dir / declared).resolve()
            if candidate.is_file():
                return candidate, f"package.json {field} = {declared!r}"
            with_suffix = Path(str(candidate) + ".d.ts")
            if with_suffix.is_file():
                return with_suffix, f"package.json {field} = {declared!r} (+.d.ts)"
            return None, (f"package.json declares {field} = {declared!r}, and "
                          f"nothing is there")

    main = manifest.get("main")
    if isinstance(main, str) and main.strip():
        beside = (build_dir / main).resolve().with_suffix("")
        candidate = Path(str(beside) + ".d.ts")
        if candidate.is_file():
            return candidate, (f"no types field; the declaration beside main "
                               f"({main!r})")
    root_index = build_dir / "dist" / "index.d.ts"
    if root_index.is_file():
        return root_index, "no types field; dist/index.d.ts"
    return None, ("package.json names no declaration file and none of the paths a "
                  "consumer would try holds one")


def _marker_line(source: str) -> int:
    """The 1-based line the marked error must be reported on: marker line + 1.

    Exactly one marker per negative consumer, asserted rather than assumed. A second
    marker would make "the marked line" ambiguous, and the ambiguity would be
    resolved silently in favour of whichever came first.
    """
    lines = source.splitlines()
    found = [i + 1 for i, line in enumerate(lines) if MARKER in line]
    if len(found) != 1:
        raise AssertionError(
            f"a negative consumer carries {len(found)} {MARKER} marker(s); "
            f"exactly one is required, on the line before the offending one")
    if found[0] >= len(lines):
        raise AssertionError(
            f"the {MARKER} marker is on the last line ({found[0]}), so there is no "
            f"line after it for the error to be reported on")
    return found[0] + 1


def _diagnostics(result: vlib.Result) -> list[dict]:
    """Every `error TSxxxx` line tsc wrote, parsed.

    Both streams are read. tsc writes diagnostics to stdout, but a crash, a bad
    `--project`, or an out-of-memory kill goes to stderr, and a module that read only
    stdout would report those as "compiled clean".
    """
    out: list[dict] = []
    blob = (result.stdout + b"\n" + result.stderr).decode("utf-8", "replace")
    for line in blob.splitlines():
        hit = DIAGNOSTIC.match(line.strip())
        if hit:
            out.append({
                "file": hit.group("file"),
                "line": int(hit.group("line")),
                "code": int(hit.group("code")),
                "text": hit.group("text")[:300],
            })
    return out


def _describe(diags: list[dict], limit: int = 8) -> str:
    return "\n".join(
        f"  {d['file']}:{d['line']} TS{d['code']}: {d['text']}"
        for d in diags[:limit]) or "  (no diagnostics)"


def _compile_case(driver, tsc: str, env: dict[str, str], case_id: str,
                  consumer: Path, tsconfig: Path, declarations: Path
                  ) -> tuple[vlib.Result, list[dict], Path]:
    """One case in its own directory: the config, the consumer, the declarations.

    A directory per case rather than one reused: `tsc` writes `.tsbuildinfo`-shaped
    state under some configurations, and two cases sharing a directory would let the
    first one's cache decide the second one's verdict.

    The declarations are copied, not symlinked. A symlink into the shared build tree
    would be followed by `tsc` for reading -- which is all that is needed -- but it
    also means the path in every diagnostic points into `$SRB_SUITE_WORK`, and those
    paths end up in the report. Copying keeps every diagnostic anchored to a path
    inside this module's own work directory, and it costs one small file per case.
    """
    work = driver.work / "types" / case_id
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tsconfig, work / "tsconfig.json")
    shutil.copy2(consumer, work / "case.ts")
    shutil.copy2(declarations, work / LINK_NAME)
    result = vlib.run([tsc, "--pretty", "false", "--project", "tsconfig.json"],
                      cwd=work, env=env, timeout=TSC_TIMEOUT, log=driver.log,
                      label=f"tsc {case_id}")
    return result, _diagnostics(result), work


def _grade_positive(driver, weights: dict[str, float], case_id: str, note: str,
                    result: vlib.Result, diags: list[dict]) -> bool:
    passed = result.ok and not diags
    if passed:
        summary = "compiles clean against the submission's declarations"
    elif result.timed_out:
        summary = f"tsc did not finish within {TSC_TIMEOUT:.0f}s"
    elif diags:
        first = diags[0]
        summary = (f"rejected: TS{first['code']} at line {first['line']} "
                   f"({len(diags)} diagnostic(s))")
    else:
        summary = f"tsc exited {result.returncode} without writing a diagnostic"
    driver.add(case_id, passed, summary, weight=weights[case_id],
               detail=(f"what this consumer asserts: {note}\n"
                       f"tsc exit {result.returncode}\n"
                       f"{_describe(diags)}\n"
                       + (result.tail(lines=8, limit=800) if not diags else "")))
    return passed


def _grade_negative(driver, weights: dict[str, float], case_id: str, note: str,
                    accept: tuple[int, ...], want_line: int,
                    result: vlib.Result, diags: list[dict]) -> bool:
    on_line = [d for d in diags if d["line"] == want_line
               and d["file"].startswith("case.ts")]
    accepted = [d for d in on_line if d["code"] in accept]
    elsewhere = [d for d in diags if d not in on_line]

    if accepted:
        passed = True
        summary = (f"rejected at the marked line with TS{accepted[0]['code']}")
    elif on_line:
        passed = False
        summary = (f"rejected at the marked line, but with "
                   f"TS{on_line[0]['code']}, which is not one of the codes that "
                   f"mean rejected for this reason ({list(accept)})")
    elif not diags:
        passed = False
        summary = "compiled clean; the declarations accept this misuse"
    else:
        passed = False
        summary = (f"{len(diags)} diagnostic(s), none at the marked line "
                   f"{want_line}")
    driver.add(case_id, passed, summary, weight=weights[case_id],
               detail=(f"what this consumer asserts: {note}\n"
                       f"the marked line is {want_line} (the line after "
                       f"`{MARKER}`); accepted codes: {list(accept)}\n"
                       f"tsc exit {result.returncode}\n"
                       f"{_describe(diags)}\n"
                       + (f"diagnostics away from the marked line are reported and "
                          f"not charged here: {len(elsewhere)}\n"
                          if elsewhere else "")))
    return passed


def run(driver) -> int:
    """Compile all 24 consumers and emit. No corpus slice; see the docstring."""
    weights = _weights()
    try:
        _prefix, _cwd, ledger = driver.probe_prefix()
    except SystemExit as exc:
        driver.add("types/build-available", False, str(exc.code)[:400],
                   required=True, verdict="error")
        return driver.emit("error", "no build to read declarations from")

    build_dir = Path(ledger["build_dir"])
    assets = Path(os.environ.get(CONSUMER_DIR_ENV, "/opt/assets")) / CONSUMER_SUBDIR
    tsconfig = assets / "tsconfig.json"
    if not tsconfig.is_file():
        driver.add("types/consumers-present", False,
                   f"the harness tsconfig is missing at {tsconfig}",
                   required=True, verdict="error")
        return driver.emit("error", "no consumer tsconfig")

    tsc = _tsc(driver.log)
    env = vlib.base_env()
    declarations, how = _declarations(build_dir)
    driver.metadata["declarations"] = {"resolved": str(declarations or ""),
                                       "how": how}
    driver.log.section("type surface")
    driver.log.write(f"types: declarations {how}")

    if declarations is None:
        # Every case fails, and each says the same reason. Recorded per case rather
        # than as one summary line because the module's weight is spread across the
        # 24 rows: collapsing them into a single failure would leave 23 rows
        # unscored, and an unscored row is a row worth nothing rather than a row
        # worth zero.
        for case_id, _kind, _weight, _accept, note in catalog.TYPE_CASES:
            driver.add(case_id, False,
                       "no declaration file could be resolved for the package",
                       weight=weights[case_id],
                       detail=f"{how}\nwhat this consumer asserts: {note}")
        return driver.emit("ok", f"0/{len(catalog.TYPE_CASES)} type cases: {how}")

    passed = 0
    for case_id, kind, _weight, accept, note in catalog.TYPE_CASES:
        consumer = assets / f"{case_id}.ts"
        if not consumer.is_file():
            driver.add(case_id, False,
                       f"the consumer program {case_id}.ts is missing from the "
                       f"image", weight=weights[case_id], verdict="error",
                       detail=f"expected at {consumer}; `check-assets.py` asserts "
                              f"every case in catalog.TYPE_CASES has a file at "
                              f"image build time")
            continue
        source = consumer.read_text(encoding="utf-8")
        result, diags, work = _compile_case(driver, tsc, env, case_id, consumer,
                                            tsconfig, declarations)
        if kind == "positive":
            ok = _grade_positive(driver, weights, case_id, note, result, diags)
        else:
            ok = _grade_negative(driver, weights, case_id, note, accept,
                                 _marker_line(source), result, diags)
        passed += ok
        driver.log.write(f"types: {case_id} ({kind}): "
                         f"{'pass' if ok else 'fail'}, {len(diags)} diagnostic(s)"
                         f" [{work.name}]")

    total = len(catalog.TYPE_CASES)
    positives = catalog.type_cases_of("positive")
    negatives = catalog.type_cases_of("negative")
    scored = {c["id"]: c["verdict"] == "pass" for c in driver.checks}
    driver.metadata["types"] = {
        "cases": total,
        "passed": passed,
        "positives_passed": sum(1 for c in positives if scored.get(c)),
        "negatives_passed": sum(1 for c in negatives if scored.get(c)),
        "budget": catalog.TYPES_BUDGET,
        "tsc": tsc,
    }
    if len(driver.checks) != total:
        # Every row emits exactly once, whatever happened. A short count shrinks the
        # module's denominator, which reads in the report as a smaller module rather
        # than as a missing measurement.
        raise AssertionError(
            f"types: {len(driver.checks)} checks recorded for {total} cases: "
            f"{sorted(set(weights) - {c['id'] for c in driver.checks})}")
    return driver.emit(
        "ok",
        f"{passed}/{total} type cases "
        f"({driver.metadata['types']['positives_passed']}/{len(positives)} "
        f"compile, {driver.metadata['types']['negatives_passed']}/"
        f"{len(negatives)} rejected)")


def main(argv: list[str]) -> int:
    """`--check`: assert the consumer files and the case table agree.

    Runs at image build time, where the assets are still beside this file. It cannot
    check what a compiler says about them -- that needs a declaration file, and the
    only one available at that point is the reference's, which `identity.py` compiles
    against as part of proving the frozen target is reachable.
    """
    if "--check" not in argv:
        print("types-surface: nothing to do; --check asserts the consumer programs "
              "against catalog.TYPE_CASES. The module runs through driver.py.")
        return 0

    here = Path(__file__).resolve().parent
    roots = [Path(os.environ.get(CONSUMER_DIR_ENV, "")) / CONSUMER_SUBDIR,
             here.parent / "data" / CONSUMER_SUBDIR]
    assets = next((r for r in roots if r.is_dir()), None)
    if assets is None:
        print(f"types-surface: no consumer directory found; tried "
              f"{[str(r) for r in roots]}")
        return 1

    problems: list[str] = []
    if not (assets / "tsconfig.json").is_file():
        problems.append("tsconfig.json is missing from the consumer directory")
    seen: set[str] = set()
    for case_id, kind, _weight, accept, _note in catalog.TYPE_CASES:
        seen.add(f"{case_id}.ts")
        path = assets / f"{case_id}.ts"
        if not path.is_file():
            problems.append(f"{case_id}: no consumer program at {path.name}")
            continue
        source = path.read_text(encoding="utf-8")
        markers = source.count(MARKER)
        if kind == "negative":
            if markers != 1:
                problems.append(
                    f"{case_id}: {markers} {MARKER} marker(s); a negative needs "
                    f"exactly one, on the line before the offending line")
            elif _marker_line(source) > len(source.splitlines()):
                problems.append(f"{case_id}: nothing follows the marker")
            if not accept:
                problems.append(f"{case_id}: negative with an empty accept-set, so "
                                f"any rejection would pass")
        else:
            if markers:
                problems.append(
                    f"{case_id}: a positive carries {markers} {MARKER} marker(s); "
                    f"positives must compile clean")
            if accept:
                problems.append(f"{case_id}: positive with a non-empty accept-set")
        if "require('jsonata')" not in source and 'require("jsonata")' not in source:
            problems.append(
                f"{case_id}: the consumer does not import the package under the name "
                f"tsconfig.json maps, so it would not resolve the declarations")

    extra = sorted(p.name for p in assets.glob("*.ts") if p.name not in seen)
    if extra:
        problems.append(f"consumer program(s) with no row in TYPE_CASES: {extra}")

    for problem in problems:
        print(f"types-surface: {problem}")
    if problems:
        return 1
    print(f"types-surface: {len(catalog.TYPE_CASES)} consumer(s) "
          f"({len(catalog.type_cases_of('positive'))} positive, "
          f"{len(catalog.type_cases_of('negative'))} negative), "
          f"weights sum to {catalog.TYPES_BUDGET:g}")
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main(sys.argv[1:]))
