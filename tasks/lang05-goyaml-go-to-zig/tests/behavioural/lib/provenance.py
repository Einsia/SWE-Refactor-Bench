#!/usr/bin/env python3
"""Four questions about the artefact that only the build could answer.

Everything else about whether the port happened is a reading of the submission's
source, and stage 1 does the reading -- with a model that can tell a comment
mentioning Go from a call into it, where this file would have a regular
expression.  What stays here is the part stage 1 cannot reach, because the
evidence does not exist until a build has run:

  - `shim-clean`      the ledger of every command the build attempted.  It exists
                      only while the build is running, and only because the
                      verifier put a tripwire on PATH.
  - `no-go-in-binary` the bytes of the installed executable.
  - `no-runtime-spawn` what the executable does when it answers a request with
                      nothing but tripwires reachable.
  - `binaries-are-zig` which compiler's fingerprints the executable carries.

None of the four reads a `.zig` file, and that is the boundary: a check that
reads the submission's source is asserting something about how the port was
written, which is stage 1's question and is asked there.

Each check returns `(passed, detail, evidence)`.  The detail is written for
someone reading the report who did not write the submission: what was looked
for, what was found, where.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import build as build_lib
import catalog
import elflib
import vlib
from build import BuildOutcome
from vlib import CaseOutcome, Log

# Directories whose contents are build output rather than source.  Only used for
# the baseline read below; the submission's own tree is not walked here.
GENERATED_DIRS = {
    "zig-out", ".zig-cache", "zig-cache", ".git", ".agit", "__pycache__",
    ".venv", "dist",
}

# Error-set names from Zig's standard library, as they appear in .rodata.  Used by
# `binaries-are-zig` as a provenance marker, and chosen by measuring which strings
# actually survive a release build of zig 0.14.1: these do, where symbols, panic
# text and debug producers do not.
#
# They are there because `@errorName` resolves at run time, so the names are data.
# Any Zig program that reads a file, writes stdout or decodes UTF-8 links some of
# them in -- and the spellings are Zig's, so a C or Go binary does not have them.
# Two are required rather than one: a YAML library could plausibly define its own
# `Overflow`, and `Utf8ExpectedContinuation` on its own is one grep away from being
# fabricated, whereas a consistent set of them is what a real link produces.
ZIG_STD_ERROR_NAMES = (
    b"Utf8ExpectedContinuation", b"Utf8OverlongEncoding",
    b"Utf8EncodesSurrogateHalf", b"Utf8CannotEncodeSurrogateHalf",
    b"Utf8InvalidStartByte", b"NotOpenForWriting", b"WouldBlock",
    b"ConnectionResetByPeer", b"SystemResources", b"NoSpaceLeft",
    b"DeviceBusy", b"LockViolation", b"InputOutput", b"BrokenPipe",
)

# Tokens that appear in Go control flow and not in go-yaml's data tables.  A
# needle must contain two distinct ones before it counts as a run of *code*.
#
# The reason is specific and load-bearing.  `resolve.go` holds a table of the
# YAML 1.1 boolean and null spellings, `yamlh.go` a table of the default tag
# strings, `emitterc.go` the indentation and line-break constants -- and an honest
# Zig port contains those same literals, in the same order, because they are the
# specification rather than an implementation choice.  A copy-detector that could
# not tell data from implementation would zero a correct port.
CODE_MARKERS = (
    "func ", ":=", "if ", "for ", "return", "nil", "err", "switch ", "case ",
    "range ", "struct", "interface", "!=", "==", "panic(", "len(", "append(",
    "&&", "||", "break", "continue", "yaml_parser_", "yaml_emitter_",
)


def read_text(path: Path, limit: int = 8_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def _strip_go_comments(text: str) -> str:
    """Comments out.

    Porting a comment across is not porting an implementation across -- it is good
    practice, and go-yaml's comments are in several places the only explanation of
    why a branch exists at all (they are libyaml's, carried over).  Stripping them
    keeps the copy-detector pointed at code.

    Crude on purpose: a `//` inside a string literal takes the rest of that line
    with it.  For needle generation that perturbs a few of several thousand
    needles and cannot produce a false positive.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)//[^\n]*$", " ", text)


def _is_code_run(needle: str) -> bool:
    return sum(1 for marker in CODE_MARKERS if marker in needle) >= 2


def baseline_needles(baseline: Path, width: int, stride: int) -> list[str]:
    """Whitespace-normalised runs of State A's implementation, code only.

    Normalised so reindenting or reflowing does not hide a copy.  Strided rather
    than exhaustive so the comparison stays affordable: at 160 characters wide, a
    copy of any real function is hit many times over.

    Only the graded implementation files.  The `_test.go` files are excluded
    deliberately: the instruction tells the agent to read them, they are 123 KB of
    YAML documents and expected values, and a Zig port that reuses those documents
    as its own test inputs is doing what it was told.
    """
    out: set[str] = set()
    for path in sorted(baseline.glob("*.go")):
        if path.name.endswith("_test.go"):
            continue
        body = _strip_go_comments(read_text(path))
        flat = " ".join(body.split())
        for at in range(0, len(flat) - width + 1, stride):
            needle = flat[at:at + width]
            if _is_code_run(needle):
                out.add(needle)
    return sorted(out)


class ProvenanceAuditor:
    """Answers the four post-build questions about one submission's artefact."""

    def __init__(
        self,
        repo: Path,
        outcome: BuildOutcome | None,
        baseline: Path,
        contract: dict,
        log: Log,
        *,
        spawn_probe=None,
    ) -> None:
        self.repo = repo
        self.outcome = outcome
        self.baseline = baseline
        self.contract = contract
        self.log = log
        # A callable returning `(events, note)`: the ledger entries the probe's own
        # run produced, and a sentence saying what it was asked.  Injected because
        # it needs a probe session and an environment, which the driver owns.
        self.spawn_probe = spawn_probe
        # The driver the build resolved.  It decides where the artefact is, and
        # which of these four checks are evidence about this tree at all.
        self.driver = build_lib.driver_of(contract, outcome)
        self._elf_cache: dict[Path, elflib.ElfFile | None] = {}
        self._needles: dict[tuple[int, int], list[str]] = {}

    # -- shared reads ------------------------------------------------------

    @property
    def declared_binaries(self) -> list[dict]:
        return (self.driver.get("binaries")
                or (self.contract.get("state_b") or {}).get("binaries") or [])

    @property
    def not_scored(self) -> set[str]:
        """Checks the resolved driver declares this tree cannot answer.

        Not a list of checks to skip quietly.  Each one still runs, still reports
        what it found, and is recorded with `skipped=True`, which is a distinct
        verdict from a fail: the report says the question was not askable of this
        tree rather than that the tree answered it wrongly.  The list is the
        contract's, so the reason a check is unscored is published in the same
        document the agent was handed.

        It is not relief from the arithmetic, and must not be read as any: a skip
        stays in the module's denominator and scores 0 (`result.pooled`), so a pool
        that skips entirely rates 0.0 rather than 1.0.  The two trees this matters
        for are the reference ones -- the resolved driver only unscores these three
        against a tree that is still Go, which stage 1's `no-go-sources` gate has
        already failed.  A submitted Zig port resolves to a driver that unscores
        nothing.
        """
        return set(self.driver.get("provenance_checks_not_scored") or [])

    @property
    def artifact(self) -> Path | None:
        """The declared executable, if the build produced one."""
        declared = self.declared_binaries
        if not declared:
            return None
        path = self.repo / declared[0]["path"]
        return path if path.is_file() else None

    def elf(self, path: Path) -> elflib.ElfFile | None:
        if path not in self._elf_cache:
            try:
                self._elf_cache[path] = elflib.load(path)
            except (OSError, elflib.ElfError):
                self._elf_cache[path] = None
        return self._elf_cache[path]

    def shipped_objects(self) -> list[tuple[str, Path, elflib.ElfFile]]:
        path = self.artifact
        if path is None:
            return []
        obj = self.elf(path)
        if obj is None:
            return []
        return [(self.artifact_label(), path, obj)]

    def artifact_label(self) -> str:
        declared = self.declared_binaries
        return declared[0]["path"] if declared else "<undeclared>"

    def non_elf_artifacts(self) -> list[str]:
        """The declared binary exists but is not a native object.

        Kept apart from "nothing was installed" because the two mean different
        things and only one of them is a cheat.  A shell script at
        `zig-out/bin/yaml-probe` that execs `go run` is a wrapper; an absent file
        is a build that did not finish.  Reporting both as "no ELF binary" would
        hide the wrapper behind the more innocent reading.
        """
        path = self.artifact
        if path is None or self.elf(path) is not None:
            return []
        head = " ".join(path.read_bytes()[:120].decode("utf-8", "replace").split())
        return [f"{self.artifact_label()}: not a native object; begins {head!r}"]

    def shim_events(self) -> list[dict]:
        """Every Go toolchain invocation the build attempted, from the ledger."""
        if self.outcome is None or self.outcome.shim_log is None:
            return []
        if not self.outcome.shim_log.is_file():
            return []
        events: list[dict] = []
        text = self.outcome.shim_log.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                events.append({"tool": "<unparseable>", "raw": line[:300]})
        return events

    def needles(self, *, width: int, stride: int) -> list[str]:
        key = (width, stride)
        if key not in self._needles:
            self._needles[key] = baseline_needles(self.baseline, width, stride)
            self.log.write(
                f"provenance: {len(self._needles[key])} baseline code needles at "
                f"width {width}, stride {stride}"
            )
        return self._needles[key]

    def baseline_go_basenames(self) -> set[str]:
        """The Go files State A shipped, by basename."""
        return {p.name for p in self.baseline.glob("*.go")}

    # -- the four checks ---------------------------------------------------

    def check_shim_clean(self) -> tuple[bool, str, list[str]]:
        """The build must not have been able to reach a Go tool, or have tried to.

        Two halves, and both are needed.  The tripwire on PATH refuses every
        invocation and records it, so a build that needed one has already failed and
        this is what turns that into a recorded verdict rather than an unexplained
        build error.  But an empty ledger only means "no Go tool was invoked through
        the tripwire" -- which is also what it says when there is no tripwire, or
        when a real toolchain was sitting somewhere on PATH and got used instead.
        So the absence of the toolchain itself is checked here too, from the record
        `Builder.retire_go_toolchain` wrote before the build.

        That second half is why this check is unscored on the Go driver: there, the
        toolchain is present on purpose and the ledger is empty because no tripwire
        was installed.  Neither fact is evidence about that tree, and the question
        it exists to answer -- did a Go compiler touch this -- is stage 1's
        `no-go-sources` gate, which is required.
        """
        events = self.shim_events()
        history = ((getattr(self.outcome, "observed", None) or {})
                   .get("go_toolchain") or {})
        before = history.get("before-build") or {}
        if not events:
            if not before:
                return (
                    False,
                    "the ledger is empty, but nothing recorded removing the Go "
                    "toolchain before the build, so an empty ledger is not evidence "
                    "that no Go compiler was reachable",
                    [f"go_toolchain record: {sorted(history) or 'absent'}"],
                )
            if not before.get("clean"):
                return (
                    False,
                    "a Go toolchain was still reachable when the build ran",
                    [f"surviving directories: {before.get('survivors')}",
                     f"on PATH: {before.get('on_real_path')}"],
                )
            return (
                True,
                "no Go toolchain was reachable when the build ran, and the build "
                "invoked none",
                [f"removed before the build: {', '.join(before.get('removed') or []) or 'already absent'}"],
            )
        summary: dict[str, int] = {}
        for event in events:
            tool = event.get("tool", "?")
            summary[tool] = summary.get(tool, 0) + 1
        evidence = [
            f"{e.get('tool')} {' '.join(str(a) for a in (e.get('argv') or [])[:6])} "
            f"(cwd {e.get('cwd', '?')})"
            for e in events[:20]
        ]
        return (
            False,
            f"the build attempted {len(events)} Go toolchain invocation(s): "
            + ", ".join(f"{k}x{v}" for k, v in sorted(summary.items())),
            evidence,
        )

    def check_no_go_in_binary(self) -> tuple[bool, str, list[str]]:
        """The shipped binary must not contain State A's Go sources.

        A submission that embedded the original to interpret or transpile at run
        time would leave it in `.rodata`.  The search is for long
        whitespace-normalised runs of State A's code, plus the file basenames,
        since an embedded bundle usually keeps its names.
        """
        objects = self.shipped_objects()
        if not objects:
            # Cannot be verified, so it does not pass: a check that waves through a
            # submission whose evidence is missing is a check that measures nothing.
            # The root cause belongs to `binaries-are-zig`, and the detail says so
            # rather than reporting one defect twice as if it were two.
            return (
                False,
                "no native object to inspect; see provenance/binaries-are-zig for "
                "why the declared binary could not be read",
                self.non_elf_artifacts(),
            )
        basenames = self.baseline_go_basenames()
        needles = self.needles(width=160, stride=400)
        offenders: list[str] = []
        for label, path, _obj in objects:
            try:
                blob = path.read_bytes()
            except OSError as exc:
                offenders.append(f"{label}: unreadable ({exc})")
                continue
            flat = " ".join(blob.decode("latin-1").split())
            hit = next((n for n in needles if n in flat), None)
            if hit:
                offenders.append(f"{label}: contains {hit[:120]!r} from State A")
            named = sorted(n for n in basenames if n.encode() in blob)
            if len(named) >= 3:
                offenders.append(
                    f"{label}: names {len(named)} State A files "
                    f"({', '.join(named[:6])})"
                )
        if offenders:
            return False, "a shipped binary contains State A's Go", offenders[:12]
        return True, "no shipped binary contains State A's Go", []

    def check_no_runtime_spawn(self) -> tuple[bool, str, list[str]]:
        """Answering a request must not spawn anything.

        A static reading of the source can be evaded by building the tool path at
        run time.  This one is empirical: the probe runs with a PATH containing only
        tripwires, answering requests that exercise the whole parser, and the ledger
        is read afterwards.  A submission that shells out has nowhere to shell out
        to, and says so in the ledger.
        """
        if self.spawn_probe is None:
            return (
                False,
                "the runtime spawn probe was not configured, so nothing was asked; "
                "this is a defect in the module, not in the submission",
                [],
            )
        try:
            events, detail = self.spawn_probe()
        except Exception as exc:  # noqa: BLE001
            return False, f"the runtime spawn probe failed: {exc}", []
        if events:
            evidence = [
                f"{e.get('tool')} {' '.join(str(a) for a in (e.get('argv') or [])[:6])}"
                for e in events[:12]
            ]
            return (
                False,
                f"the probe attempted {len(events)} Go toolchain invocation(s) while "
                f"answering requests",
                evidence,
            )
        return True, detail or "the probe spawned no Go tool while answering", []

    def check_binaries_are_zig(self) -> tuple[bool, str, list[str]]:
        """The shipped binary must be native ELF the Zig compiler produced.

        Several signals are accepted because a submission may build in ReleaseFast
        and strip in ways that erase any one of them.  Requiring one specific marker
        would fail honest work; requiring at least one keeps the claim meaningful.

        Which of them survive was measured against zig 0.14.1 rather than assumed,
        because the answer is narrower than it looks.  A Debug build carries all of
        them.  `-O ReleaseSmall` and `-O ReleaseFast` carry exactly two: the
        `.comment` string and the standard library's error-set names.  Symbols,
        panic text and debug producers are all gone -- so a check resting on those
        would fail an honest submission that shipped a release build, which is the
        ordinary thing to do.

        Of the two survivors, `.comment` is the weaker: it reads `Linker: LLD 19.1.7
        (https://github.com/ziglang/zig-bootstrap ...)`, which is the *linker*
        naming itself, and `zig cc` on a C file would produce the same string.  The
        error-set names are Zig's own, which is why they are checked too.

        The Go half of the same question is checked here rather than separately: a
        Go binary on x86-64 Linux is unmistakable -- `.note.go.buildid`, the
        `go:buildid` string, `runtime.` symbols -- and finding those means the
        `zig build` that produced this file installed something it did not compile.
        """
        objects = self.shipped_objects()
        if not objects:
            wrappers = self.non_elf_artifacts()
            if wrappers:
                return (
                    False,
                    "the declared binary is not a native object: something other "
                    "than a compiled Zig program was installed under its name",
                    wrappers,
                )
            return (
                False,
                f"no binary exists at {self.artifact_label()} after the build",
                [],
            )
        evidence: list[str] = []
        without: list[str] = []
        go_markers: list[str] = []
        for label, path, obj in objects:
            blob = path.read_bytes()
            signals: list[str] = []
            comments = " ".join(obj.strings_in(".comment"))
            if "zig" in comments.lower():
                signals.append(f"zig in .comment ({comments[:60]!r})")
            names = obj.section_names
            if any(n in (".zig", ".zigstack") for n in names):
                signals.append("zig section")
            # Zig mangles nothing: a symbol is its fully-qualified name, and dots
            # in a symbol name are a Zig namespace path.  `std.` is the one prefix
            # every Zig binary that allocates or writes will have.
            dotted = [s.name for s in obj.symtab
                      if s.name.startswith(("std.", "main.", "builtin."))]
            if dotted:
                signals.append(f"{len(dotted)} Zig-namespaced symbols")
            if any(s.name.startswith("__zig_") for s in obj.symtab):
                signals.append("zig runtime symbols")
            # The panic machinery's own strings, which survive stripping because
            # they are data.  Present in Debug and ReleaseSafe.
            for needle in (b"reached unreachable code", b"attempt to unwrap error",
                           b"index out of bounds", b"integer overflow"):
                if needle in blob:
                    signals.append(f"Zig panic text {needle.decode()!r}")
                    break
            if b"producer" in blob and b"zig 0.14" in blob:
                signals.append("zig producer string in debug info")
            errnames = [n for n in ZIG_STD_ERROR_NAMES if n in blob]
            if len(errnames) >= 2:
                signals.append(
                    f"{len(errnames)} Zig std error names "
                    f"({errnames[0].decode()}, ...)"
                )
            if signals:
                evidence.append(f"{label}: {', '.join(signals[:4])}")
            else:
                without.append(label)
            go_signals = [
                marker for marker, needle in (
                    (".note.go.buildid", None),
                    ("go:buildid", b"go:buildid"),
                    ("Go build ID", b"Go build ID"),
                    ("go.buildinfo", b"\xff Go buildinf:"),
                )
                if (needle is None and marker in names) or
                   (needle is not None and needle in blob)
            ]
            if go_signals:
                go_markers.append(f"{label}: {', '.join(go_signals)}")
            if any(s.name.startswith(("runtime.", "go.")) for s in obj.symtab):
                go_markers.append(f"{label}: Go runtime symbols")
        if go_markers:
            return (
                False,
                "the shipped binary carries Go provenance: the build installed "
                "something the Zig compiler did not produce",
                go_markers[:10],
            )
        if not evidence:
            return (
                False,
                "the shipped binary carries no Zig provenance marker (.comment "
                "producer, Zig-namespaced symbols, panic strings or debug producer)",
                without[:10],
            )
        return True, f"the binary carries Zig provenance", evidence

    # -- driver ------------------------------------------------------------

    def evaluate(self) -> list[CaseOutcome]:
        """Run the declared provenance cases, in declaration order.

        Every case runs, including the ones the resolved driver declares it cannot
        answer.  Those are recorded with their real finding and marked `skipped`,
        which keeps them in the module's denominator and out of its numerator: a
        question this tree could not be asked is reported as unanswered rather than
        as answered wrongly, and it is not paid for either.  Running them anyway
        costs a second and means the report says what was actually found rather
        than "not applicable".
        """
        not_scored = self.not_scored
        declared = {case[1] for case in catalog.PROVENANCE_CASES}
        unknown = sorted(not_scored - declared)
        if unknown:
            # A name in the contract that no case answers to would silently unscore
            # nothing while reading as though it had.
            raise RuntimeError(
                f"driver {self.driver.get('id')!r} lists {unknown} in "
                f"provenance_checks_not_scored, which are not declared provenance "
                f"checks: {sorted(declared)}"
            )
        if not declared - not_scored:
            # The other direction, and the one the arithmetic punishes: `rate()`
            # returns 0.0 for an all-skipped pool, so a driver that unscored every
            # case would score this module zero -- the opposite of what unscoring is
            # for -- while every case reported PASS in the log.
            raise RuntimeError(
                f"driver {self.driver.get('id')!r} unscores every declared "
                f"provenance check {sorted(declared)}; an all-skipped module rates "
                f"0.0, so at least one case must stay scored"
            )
        outcomes: list[CaseOutcome] = []
        for case_id, check, weight, required, note in catalog.PROVENANCE_CASES:
            method = getattr(self, f"check_{check.replace('-', '_')}", None)
            if method is None:
                # A declared case with no implementation is a defect in this file,
                # reported as a failure rather than skipped: skipping would award
                # the weight of a check that never ran.
                outcomes.append(CaseOutcome(
                    case_id=case_id, family="provenance", kind="provenance",
                    passed=False, weight=weight, required=required,
                    detail=f"provenance.py has no check for {check!r}",
                ))
                continue
            try:
                passed, detail, evidence = method()
            except Exception as exc:  # noqa: BLE001
                passed = False
                detail = f"the check raised {type(exc).__name__}: {exc}"[:400]
                evidence = []
                self.log.write(f"  {case_id} raised: {exc}")
            skipped = check in not_scored
            if skipped:
                detail = (
                    f"not scored on driver {self.driver.get('id')!r} "
                    f"({self.driver.get('language')}): "
                    f"{self.driver.get('provenance_note') or 'see graded_builds'} "
                    f"-- what it found: {detail}"
                )
            outcomes.append(CaseOutcome(
                case_id=case_id, family="provenance", kind="provenance",
                passed=passed, weight=weight, required=required,
                skipped=skipped,
                detail=(detail if (passed or skipped) else f"{note}: {detail}"),
                evidence=evidence,
            ))
            verdict = "SKIP" if skipped else ("PASS" if passed else "FAIL")
            self.log.write(f"  {verdict} {case_id}: {detail[:200]}")
        scored = [o for o in outcomes if o.scored]
        line = (f"{sum(1 for o in scored if o.passed)}/{len(scored)} cases, "
                f"{sum(o.weight for o in scored if o.passed):.1f}/"
                f"{sum(o.weight for o in scored):.1f} points")
        if len(scored) != len(outcomes):
            line += f" ({len(outcomes) - len(scored)} not scored on this driver)"
        self.log.write(line)
        return outcomes


def run_provenance_cases(
    repo: Path,
    outcome: BuildOutcome | None,
    baseline: Path,
    contract: dict,
    log: Log,
    *,
    spawn_probe=None,
) -> list[CaseOutcome]:
    """The provenance of what the build produced."""
    log.section("provenance")
    if len(catalog.PROVENANCE_CASES) < catalog.MIN_PROVENANCE_CASES:
        raise RuntimeError(
            f"{len(catalog.PROVENANCE_CASES)} provenance cases declared; the "
            f"catalog floor is {catalog.MIN_PROVENANCE_CASES}"
        )
    auditor = ProvenanceAuditor(repo, outcome, baseline, contract, log,
                                spawn_probe=spawn_probe)
    return auditor.evaluate()
