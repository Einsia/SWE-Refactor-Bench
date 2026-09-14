#!/usr/bin/env python3
"""The migration-audit gates for lang04-acorn-js-to-rust.

These answer a different question from the differential test.  The differential
asks whether the behaviour downstream depends on survived; a submission that
kept the JavaScript and shelled out to `node` would score 1.0 on it by being the
reference.  These gates ask whether the port actually happened.

That asymmetry is why they are a precondition and not a summand.  Any mandatory
gate that fails withholds the behavioural score entirely -- there is no partial
credit for a wrapper, because a wrapper is not 80% of a port.

Each gate returns `(passed, detail, evidence)`.  The detail is written for
someone reading the report who did not write the submission: it says what was
looked for, what was found, and where.  Evidence is the specific paths, symbols
or ledger lines that decided it, because a gate that says only "failed" is a gate
nobody can act on.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import elflib
import vlib
from build import BuildOutcome
from vlib import GateOutcome, Log

# Directories whose contents are build output, not source.  Excluded from the
# source-closure walk because `target/` legitimately contains anything cargo
# wants to put there, and it is discarded before the graded build in any case.
GENERATED_DIRS = {
    "target", "node_modules", ".git", ".agit", "__pycache__", ".cargo",
    "dist", ".rustup", ".venv",
}

# Fallbacks only.  The lists that decide the gate are read from
# source-contract.json, which is the copy the agent is given -- see
# `IntegrityAuditor.__init__`.  Nothing may be graded that was not declared, so
# these constants exist for the case where the contract is unreadable, and are
# expected to be identical to it.
JS_SUFFIXES = {
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx",
    ".node", ".wasm", ".map",
}

JS_NAMES = {
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock",
    "pnpm-lock.yaml", "bun.lockb", ".npmrc", ".npmignore", ".eslintignore",
    ".tern-project",
}

# Directory names in the contract's forbidden list.  They are handled through the
# build's discard record rather than the source walk, because the build deletes
# them before the gates run.
FORBIDDEN_DIRS = {"node_modules", "vendor", ".cargo"}

# Names an interpreter could be spawned under, for the source-level search.
INTERPRETER_NAMES = (
    "node", "nodejs", "npm", "npx", "yarn", "pnpm", "deno", "bun", "qjs",
    "d8", "jsc", "hermes", "graaljs", "ts-node",
)

# Rust's own process-spawning API surface.  Finding these is not a failure by
# itself -- the CLI is allowed to exist -- but combined with an interpreter name
# it is the wrapper pattern.
SPAWN_APIS = (
    "std::process::Command", "process::Command", "Command::new",
    "libc::execv", "libc::execvp", "libc::system", "libc::fork",
    "std::os::unix::process", "CommandExt",
)


@dataclass
class Artifacts:
    """The binaries one build produced, as installed."""

    prefix: Path
    build_dir: Path
    acorn: Path | None
    probe: Path | None
    shim_log: Path | None

    def present(self) -> list[tuple[str, Path]]:
        out = []
        if self.acorn is not None:
            out.append(("bin/acorn", self.acorn))
        if self.probe is not None:
            out.append(("bin/acorn-probe", self.probe))
        return out


def _is_generated_dir(path: Path) -> bool:
    return any(part in GENERATED_DIRS for part in path.parts)


def walk_source(root: Path) -> list[Path]:
    """Every regular file in the submission that is source rather than output."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if d not in GENERATED_DIRS)
        for name in sorted(filenames):
            path = here / name
            if path.is_symlink():
                out.append(path)
                continue
            if path.is_file():
                out.append(path)
    return out


def rel(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def read_text(path: Path, limit: int = 8_000_000) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


# Tokens that appear in JavaScript control flow and not in acorn's data tables.
# A needle must contain two distinct ones before it counts as a run of *code*.
# The reason is specific and load-bearing: `acorn/src/unicode-property-data.js`
# holds a 2,014-character space-separated list of Unicode property names, and the
# instruction tells the agent to port that data as data.  An honest Rust port
# therefore contains that literal byte for byte, and a copy-detector that did not
# know the difference between data and implementation would zero it.
CODE_MARKERS = (
    "function", "return", "this.", "if (", "for (", "while (", "=>", "||",
    "&&", "typeof", "throw ", "switch (", "case ", "break", "let ", "const ",
)


def _strip_js_comments(text: str) -> str:
    """Comments out.

    Porting a comment across is not porting an implementation across -- it is
    good practice, and several of acorn's comments are the only documentation of
    why a branch exists.  Stripping them keeps the copy-detector pointed at code.

    Crude on purpose: a `//` inside a string literal or a regex will take the
    rest of that line with it.  For needle generation that only perturbs a few of
    several thousand needles, and it cannot produce a false positive.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)//[^\n]*$", " ", text)


def _is_code_run(needle: str) -> bool:
    return sum(1 for marker in CODE_MARKERS if marker in needle) >= 2


def _is_test_path(relpath: Path) -> bool:
    """Whether a path is part of the submission's own test suite.

    `cargo build --release` does not compile any of it, so nothing here can
    answer a graded request.  A port should have tests, and its tests should
    contain expected ASTs -- that is what a test is.
    """
    parts = relpath.parts
    if "tests" in parts or "benches" in parts or "examples" in parts:
        return True
    stem = relpath.stem
    return stem.startswith("test_") or stem.endswith(("_test", "_tests"))


def _strip_cfg_test(text: str) -> str:
    """Remove `#[cfg(test)]` items, brace-matched.

    Written by hand rather than with a regex because the block nests, and a
    non-greedy match to the first `}` would leave most of a test module behind --
    which is the direction that produces false positives.
    """
    out: list[str] = []
    at = 0
    for match in re.finditer(r"#\[cfg\(test\)\]", text):
        if match.start() < at:
            continue
        out.append(text[at:match.start()])
        # Find the item's opening brace, then its match.
        brace = text.find("{", match.end())
        if brace < 0:
            at = match.end()
            continue
        depth = 0
        end = brace
        for i in range(brace, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        else:
            end = len(text)
        at = end
    out.append(text[at:])
    return "".join(out)


def baseline_needles(baseline: Path, width: int, stride: int) -> list[str]:
    """Whitespace-normalised runs of State A's implementation, code only.

    Whitespace-normalised so that reindenting or reflowing does not hide a copy.
    Strided rather than exhaustive so the comparison stays affordable: at 200
    characters wide, a copy of any real function is hit many times over.
    """
    out: set[str] = set()
    for sub in ("acorn/src", "acorn-loose/src", "acorn-walk/src"):
        directory = baseline / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.rglob("*.js")):
            # Past the licence header every file shares.
            body = _strip_js_comments(read_text(path)[400:])
            flat = " ".join(body.split())
            for at in range(0, len(flat) - width + 1, stride):
                needle = flat[at:at + width]
                if _is_code_run(needle):
                    out.add(needle)
    return sorted(out)


def count_rust_logic_lines(paths: list[Path]) -> int:
    """Lines of Rust that are neither blank nor comment-only.

    Deliberately crude -- it is a floor check, not a metric.  A port of a
    12,000-line parser cannot plausibly be 300 lines of Rust, and that is the
    only thing this number is asked to decide.
    """
    total = 0
    for path in paths:
        in_block = False
        for line in read_text(path).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if in_block:
                if "*/" in stripped:
                    in_block = False
                continue
            if stripped.startswith("/*"):
                if "*/" not in stripped:
                    in_block = True
                continue
            if stripped.startswith("//"):
                continue
            total += 1
    return total


class IntegrityAuditor:
    """Evaluates every declared audit gate against one submission."""

    def __init__(
        self,
        repo: Path,
        outcome: BuildOutcome | None,
        baseline: Path,
        contract: dict,
        scratch: Path,
        log: Log,
        *,
        probe_env_probe=None,
    ) -> None:
        self.repo = repo
        self.outcome = outcome
        self.baseline = baseline
        self.contract = contract
        self.scratch = scratch
        self.log = log
        self.probe_env_probe = probe_env_probe
        # The declared lists, not this module's opinion of them.  A gate that
        # fails a submission for keeping a file the instruction never asked it to
        # remove is a broken gate, so the contract handed to the agent is the
        # only thing allowed to decide.
        forbidden = contract.get("forbidden_paths") or {}
        self.forbidden_suffixes = {
            s.lower() for s in forbidden.get("extensions") or JS_SUFFIXES
        }
        declared_names = set(forbidden.get("names") or JS_NAMES)
        self.forbidden_dirs = declared_names & FORBIDDEN_DIRS
        self.forbidden_names = declared_names - self.forbidden_dirs
        self._files: list[Path] | None = None
        self._rust_files: list[Path] | None = None
        self._elf_cache: dict[Path, elflib.ElfFile | None] = {}
        self._metadata: dict | None = None
        self._needles: dict[tuple[int, int], list[str]] = {}
        scratch.mkdir(parents=True, exist_ok=True)

    # -- shared reads -----------------------------------------------------

    @property
    def files(self) -> list[Path]:
        if self._files is None:
            self._files = walk_source(self.repo)
        return self._files

    @property
    def rust_files(self) -> list[Path]:
        if self._rust_files is None:
            self._rust_files = [p for p in self.files if p.suffix == ".rs"]
        return self._rust_files

    def elf(self, path: Path) -> elflib.ElfFile | None:
        if path not in self._elf_cache:
            try:
                self._elf_cache[path] = elflib.load(path)
            except (OSError, elflib.ElfError):
                self._elf_cache[path] = None
        return self._elf_cache[path]

    @property
    def artifacts(self) -> Artifacts | None:
        if self.outcome is None:
            return None
        prefix = self.outcome.prefix
        acorn = prefix / "bin" / "acorn"
        probe = prefix / "bin" / "acorn-probe"
        return Artifacts(
            prefix=prefix,
            build_dir=self.outcome.build_dir,
            acorn=acorn if acorn.is_file() else None,
            probe=probe if probe.is_file() else None,
            shim_log=self.outcome.shim_log,
        )

    def shipped_objects(self) -> list[tuple[str, Path, elflib.ElfFile]]:
        arts = self.artifacts
        if arts is None:
            return []
        out = []
        for label, path in arts.present():
            obj = self.elf(path)
            if obj is not None:
                out.append((label, path, obj))
        return out

    def shim_events(self) -> list[dict]:
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

    def metadata(self) -> dict:
        """`cargo metadata` output, parsed once."""
        if self._metadata is None:
            self._metadata = {}
            if self.outcome is not None:
                if isinstance(self.outcome.metadata_json, dict):
                    self._metadata = self.outcome.metadata_json
                elif self.outcome.metadata is not None and self.outcome.metadata.ok:
                    try:
                        self._metadata = json.loads(self.outcome.metadata.stdout)
                    except ValueError:
                        self._metadata = {}
        return self._metadata

    def metadata_problem(self) -> str:
        """Why `metadata()` is empty: the command, or the document."""
        result = self.outcome.metadata if self.outcome is not None else None
        if result is None:
            return "cargo metadata did not run"
        if not result.ok:
            err = result.stderr.decode("utf-8", "replace").strip()[:600]
            return f"cargo metadata failed (rc={result.returncode}): {err}"
        return ("cargo metadata succeeded but its output did not parse as JSON "
                "-- a defect in this verifier, not in the submission")

    def needles(self, *, width: int, stride: int) -> list[str]:
        """Cached runs of State A's code, for the two copy-detection gates."""
        key = (width, stride)
        if key not in self._needles:
            self._needles[key] = baseline_needles(self.baseline, width, stride)
            self.log.write(
                f"audit: {len(self._needles[key])} baseline code needles "
                f"at width {width}, stride {stride}"
            )
        return self._needles[key]

    def discarded_forbidden(self) -> set[str]:
        """Forbidden directories the submission shipped, from the build's record.

        `target/` and `dist/` are discarded too and are not findings -- they are
        build output, and a submission that has them has merely been built.
        """
        if self.outcome is None:
            return set()
        return {
            name for name in self.outcome.discarded
            if name in self.forbidden_dirs or name in FORBIDDEN_DIRS
        }

    def baseline_js_basenames(self) -> set[str]:
        """The JavaScript modules State A shipped, by basename."""
        out: set[str] = set()
        for sub in ("acorn/src", "acorn-loose/src", "acorn-walk/src"):
            d = self.baseline / sub
            if d.is_dir():
                out |= {p.name for p in d.glob("*.js")}
        return out

    # -- the gate table ---------------------------------------------------

    def evaluate(self, cases: list[dict]) -> list[GateOutcome]:
        results: list[GateOutcome] = []
        for case in cases:
            check = case["check"]
            mandatory = bool((case.get("params") or {}).get("mandatory", True))
            handler = getattr(self, f"gate_{check.replace('-', '_')}", None)
            if handler is None:
                results.append(GateOutcome(
                    gate_id=case["id"], check=check, mandatory=mandatory,
                    passed=False,
                    detail=f"no handler implemented for gate '{check}'",
                ))
                continue
            try:
                passed, detail, evidence = handler()
            except Exception as exc:
                # A gate that raises is a broken gate, and must not be scored as
                # a passing one.  It fails, and says it was the gate that broke.
                passed = False
                detail = f"gate raised {type(exc).__name__}: {exc}"
                evidence = []
            results.append(GateOutcome(
                gate_id=case["id"], check=check, mandatory=mandatory,
                passed=passed, detail=detail, evidence=evidence,
            ))
        return results

    # == G1: the JavaScript implementation has left the source closure =====

    def gate_no_js_sources(self) -> tuple[bool, str, list[str]]:
        """No JavaScript, TypeScript, source map, .node or .wasm file remains.

        This includes `test/bench/fixtures/*.js`.  They are third-party bundles
        rather than acorn's own code, but they are State A's benchmark input and
        the verifier keeps its own copies -- a submission has no reason to carry
        1.8 MB of Ember to be graded on it.
        """
        offenders: list[str] = []
        for path in self.files:
            if _is_generated_dir(path.relative_to(self.repo)):
                continue
            name = rel(self.repo, path)
            if path.suffix.lower() in self.forbidden_suffixes:
                offenders.append(f"{name} ({path.suffix} source)")
            elif path.name in self.forbidden_names:
                offenders.append(f"{name} (Node package metadata)")
        # `node_modules/` is on the forbidden list too, but the build deletes it
        # before this walk, so the record of what was deleted is where it shows.
        for name in sorted(self.discarded_forbidden()):
            offenders.append(f"{name}/ (forbidden directory, present at submission)")
        if offenders:
            return (
                False,
                f"{len(offenders)} JavaScript-ecosystem path(s) are still in the "
                f"submission; the port is supposed to have replaced them, not "
                f"shipped alongside them",
                sorted(offenders)[:40],
            )
        return True, "no JavaScript, TypeScript or Node package path remains", []

    def gate_no_js_by_content(self) -> tuple[bool, str, list[str]]:
        """JavaScript renamed to escape the extension check is still JavaScript.

        Extension-based detection is trivially defeated by `mv index.js
        index.rs`, so the content is examined too.  The test is not "does this
        look like JavaScript" in general -- that would catch Rust files
        discussing JavaScript, which this port is full of.  It is "does this file
        contain acorn's own JavaScript", checked by looking for constructs that
        are simultaneously valid JS, invalid Rust, and specific to this codebase.
        """
        offenders: list[str] = []
        # Each pattern is a JS construct that will not compile as Rust.  A Rust
        # file containing any of them is not being compiled, whatever it is named.
        patterns = [
            (re.compile(r"^\s*(?:export|import)\s+(?:default\s+|\{|\*)", re.M),
             "ES module import/export"),
            (re.compile(r"\brequire\s*\(\s*['\"]"), "CommonJS require()"),
            (re.compile(r"^\s*(?:var|let|const)\s+\w+\s*=\s*function\s*\(", re.M),
             "function-expression assignment"),
            (re.compile(r"\bpp\s*\.\s*\w+\s*=\s*function\b"),
             "acorn's `pp.method = function` prototype extension"),
            (re.compile(r"===|!=="), "strict-equality operator"),
            (re.compile(r"\btypeof\s+\w+\s*===?\s*['\"]"), "typeof comparison"),
        ]
        for path in self.rust_files:
            text = read_text(path, 400_000)
            hits = [label for rx, label in patterns if rx.search(text)]
            # Two independent constructs, because `===` alone appears inside Rust
            # string literals and doc comments in an honest port -- it is in the
            # instruction's own examples.
            if len(hits) >= 2:
                offenders.append(f"{rel(self.repo, path)}: {', '.join(hits[:4])}")
        # Extensionless files get the same treatment with a lower bar: State A's
        # CLI is `acorn/bin/acorn`, JavaScript with a shebang and no extension,
        # and it is exactly what a submission would keep if it kept anything.
        # AUTHORS and the LICENSEs are extensionless too and trip none of this.
        for path in self.files:
            if path.suffix or path.name in self.forbidden_names:
                continue
            if _is_generated_dir(path.relative_to(self.repo)):
                continue
            text = read_text(path, 200_000)
            hits = [label for rx, label in patterns[:4] if rx.search(text)]
            first = text[:200].splitlines()[0] if text else ""
            if first.startswith("#!") and any(
                name in first for name in INTERPRETER_NAMES
            ):
                hits.insert(0, f"shebang {first.strip()[:60]!r}")
            if hits:
                offenders.append(f"{rel(self.repo, path)} (extensionless): {hits[0]}")
        if offenders:
            return (
                False,
                f"{len(offenders)} file(s) contain JavaScript that an extension "
                f"check would miss",
                sorted(offenders)[:30],
            )
        return True, "no file contains JavaScript under another name", []

    def gate_no_bundled_reference(self) -> tuple[bool, str, list[str]]:
        """No file may contain a recognisable chunk of State A's implementation.

        The check is over long literal runs, not over similarity: an honest port
        will resemble the original in structure and in every error message, and
        must not be penalised for that.  What it will not contain is 200
        consecutive bytes of the original file, whitespace-normalised.
        """
        needles = self.needles(width=200, stride=40)
        if not needles:
            return False, "the baseline JavaScript is unavailable to compare against", []
        # The retained files are State A's by definition and are checked for being
        # unchanged elsewhere; comparing them against State A here would fail the
        # submission for obeying the instruction.
        retained = set(self.contract["retained_paths"]["required"])
        offenders: list[str] = []
        for path in self.files:
            name = rel(self.repo, path)
            if _is_generated_dir(path.relative_to(self.repo)) or name in retained:
                continue
            if path.suffix.lower() in (".md", ".txt") or path.name == "AUTHORS":
                continue
            text = read_text(path, 4_000_000)
            if len(text) < 200:
                continue
            flat = " ".join(text.split())
            found = [n for n in needles if n in flat]
            if found:
                offenders.append(
                    f"{rel(self.repo, path)}: {len(found)} run(s) of >=200 chars "
                    f"from State A, e.g. {found[0][:120]!r}"
                )
        if offenders:
            return (
                False,
                f"{len(offenders)} file(s) contain verbatim runs of State A's "
                f"JavaScript",
                sorted(offenders)[:20],
            )
        return True, "no verbatim run of State A's implementation was found", []

    # == G2: Rust is actually the implementation ===========================

    def gate_rust_present(self) -> tuple[bool, str, list[str]]:
        floor = int(self.contract["native_code_policy"]["min_rust_logic_lines"])
        if not self.rust_files:
            return False, "the submission contains no .rs files at all", []
        lines = count_rust_logic_lines(self.rust_files)
        biggest = sorted(
            ((count_rust_logic_lines([p]), rel(self.repo, p)) for p in self.rust_files),
            reverse=True,
        )[:8]
        evidence = [f"{n} lines  {name}" for n, name in biggest]
        if lines < floor:
            return (
                False,
                f"{len(self.rust_files)} Rust file(s) contain {lines} lines of "
                f"logic, below the {floor} this port requires. A parser, "
                f"tokenizer, regexp validator, error-tolerant parser and walker "
                f"cannot be expressed in less",
                evidence,
            )
        return (
            True,
            f"{len(self.rust_files)} Rust file(s), {lines} lines of logic "
            f"(floor {floor})",
            evidence,
        )

    def gate_workspace_shape(self) -> tuple[bool, str, list[str]]:
        """The four crates exist, at the pinned versions, in one workspace.

        Read from `cargo metadata` rather than from Cargo.toml: a workspace that
        sets versions through `workspace.package` inheritance is correct and
        would look wrong to a hand parse.
        """
        meta = self.metadata()
        if not meta:
            return False, self.metadata_problem(), []
        packages = {p["name"]: p for p in meta.get("packages", [])}
        want = {c["name"]: c["version"] for c in self.contract["state_b"]["crates"]}
        problems: list[str] = []
        evidence: list[str] = []
        for name, version in sorted(want.items()):
            got = packages.get(name)
            if got is None:
                problems.append(f"{name}: absent from the workspace")
                continue
            evidence.append(f"{name} {got['version']}")
            if got["version"] != version:
                problems.append(
                    f"{name}: version is {got['version']}, the contract pins "
                    f"{version}"
                )
        members = set(meta.get("workspace_members", []))
        if len(members) < len(want):
            problems.append(
                f"the workspace has {len(members)} member(s) for {len(want)} "
                f"required crates"
            )
        if problems:
            return False, "; ".join(problems)[:1200], evidence
        return (
            True,
            f"all {len(want)} crates present at their pinned versions in one "
            f"workspace",
            evidence,
        )

    def gate_no_external_crates(self) -> tuple[bool, str, list[str]]:
        """std, core and alloc only, plus the workspace's own members.

        A dependency-free parser is the requirement, and it is checked three
        ways: the resolved dependency list from cargo, the presence of a vendor
        directory, and whether `cargo fetch --offline` was satisfiable.
        """
        meta = self.metadata()
        if not meta:
            return False, f"{self.metadata_problem()}; cannot enumerate dependencies", []
        own = {p["name"] for p in meta.get("packages", [])}
        offenders: list[str] = []
        for pkg in meta.get("packages", []):
            for dep in pkg.get("dependencies", []):
                name = dep.get("name", "")
                if name in own:
                    continue
                kind = dep.get("kind") or "normal"
                offenders.append(f"{pkg['name']} depends on {name} ({kind})")
        # Vendored crates and a checked-in .cargo/config.toml are deleted by the
        # build before this runs, so the record of the deletion is the evidence.
        for name in sorted(self.discarded_forbidden() & {"vendor", ".cargo"}):
            offenders.append(
                f"{name}/ was present at submission: dependencies were vendored "
                f"rather than avoided"
            )
        # A `[source]` replacement or a path dependency pointing outside the
        # workspace is the other way to arrive at a dependency cargo will resolve.
        for path in self.files:
            if path.name != "Cargo.toml":
                continue
            text = read_text(path, 400_000)
            if "[source." in text or "source.crates-io" in text:
                offenders.append(f"{rel(self.repo, path)}: declares a source replacement")
            for match in re.finditer(r'path\s*=\s*"([^"]+)"', text):
                # `path = "../acorn"` between workspace members is how this
                # workspace is supposed to be wired, so the test is whether the
                # target resolves inside the submission, not whether it is
                # relative.
                target = (path.parent / match.group(1)).resolve()
                try:
                    target.relative_to(self.repo.resolve())
                except ValueError:
                    offenders.append(
                        f"{rel(self.repo, path)}: path dependency "
                        f"{match.group(1)!r} resolves outside the submission"
                    )
        fetch = (self.outcome.extra.get("offline_fetch")
                 if self.outcome is not None else None)
        if fetch is not None and not fetch.ok:
            offenders.append(
                "cargo fetch --offline failed: "
                + fetch.stderr.decode("utf-8", "replace").strip()[:300]
            )
        if offenders:
            return (
                False,
                f"the workspace is not dependency-free: {len(offenders)} finding(s)",
                offenders[:20],
            )
        return True, "no dependency outside std, core, alloc and the workspace itself", []

    def gate_no_build_scripts(self) -> tuple[bool, str, list[str]]:
        """No build.rs, and no proc-macro crate.

        Both are legitimate Rust, and both are ways to generate the
        implementation at build time from something that is not in the
        submission.  The contract forbids them for that reason, so their absence
        is checked rather than their behaviour.

        Half the answer is the crate graph, so a graph that could not be read is
        no answer.  `no-external-crates` reads the same `cargo metadata` and
        fails when it did not run; this gate has to agree, or the two disagree
        about identical evidence and the more permissive one is believed.
        """
        offenders = [rel(self.repo, p) for p in self.files if p.name == "build.rs"]
        meta = self.metadata()
        if not meta:
            return False, f"{self.metadata_problem()}; cannot enumerate build targets", []
        for pkg in meta.get("packages", []):
            for target in pkg.get("targets", []):
                kinds = target.get("kind") or []
                if "custom-build" in kinds:
                    offenders.append(f"{pkg['name']}: has a build script target")
                if "proc-macro" in kinds:
                    offenders.append(f"{pkg['name']}: is a proc-macro crate")
        if offenders:
            return (
                False,
                "the workspace uses build-time code generation, which the "
                "contract forbids",
                sorted(set(offenders))[:20],
            )
        return True, "no build script and no proc-macro crate", []

    # == G3: no interpreter, at build time or at run time ==================

    def gate_shim_clean(self) -> tuple[bool, str, list[str]]:
        """The build must not have tried to run a JavaScript interpreter.

        The tripwire refuses every invocation, so a build that needed one has
        already failed; this gate is what turns that into a recorded verdict
        rather than an unexplained build error.

        An empty ledger is the correct pass, which is exactly why the build's own
        success has to be checked first: a build that never ran writes the same
        empty ledger as a build that never wanted an interpreter.  `BuildOutcome`
        records the ledger path for one half of that confusion (see its
        `summary`); this is the other half.
        """
        if self.outcome is None or not self.outcome.built:
            return (
                False,
                "the build did not succeed, so its tripwire ledger is empty for "
                "that reason and says nothing about interpreter use",
                [],
            )
        events = self.shim_events()
        if not events:
            return True, "the build invoked no JavaScript interpreter", []
        summary: dict[str, int] = {}
        for event in events:
            summary[event.get("tool", "?")] = summary.get(event.get("tool", "?"), 0) + 1
        evidence = [
            f"{e.get('tool')} {' '.join(e.get('argv', [])[:6])} (cwd {e.get('cwd', '?')})"
            for e in events[:20]
        ]
        return (
            False,
            f"the build attempted {len(events)} interpreter invocation(s): "
            + ", ".join(f"{k}x{v}" for k, v in sorted(summary.items())),
            evidence,
        )

    def gate_no_interpreter_in_source(self) -> tuple[bool, str, list[str]]:
        """No source file may name an interpreter in a way that could spawn it.

        Naming `node` is not itself a finding -- the CHANGELOG mentions Node, and
        so does the README.  What this looks for is an interpreter name in the
        same file as a process-spawning API, which is the wrapper pattern and has
        no other reason to appear in a parser.
        """
        offenders: list[str] = []
        name_rx = re.compile(
            r"""["'`/\\]\s*(""" + "|".join(INTERPRETER_NAMES) + r""")\s*["'`]"""
            r"""|/usr/(?:local/)?bin/(""" + "|".join(INTERPRETER_NAMES) + r")\b"
        )
        for path in self.rust_files:
            text = read_text(path)
            spawns = [api for api in SPAWN_APIS if api in text]
            if not spawns:
                continue
            hits = name_rx.findall(text)
            flat = [h for pair in hits for h in (pair if isinstance(pair, tuple) else (pair,)) if h]
            if flat:
                offenders.append(
                    f"{rel(self.repo, path)}: spawns via {spawns[0]} and names "
                    f"{sorted(set(flat))[:4]}"
                )
        if offenders:
            return (
                False,
                f"{len(offenders)} Rust file(s) both spawn a process and name a "
                f"JavaScript interpreter",
                offenders[:20],
            )
        return True, "no source file both spawns a process and names an interpreter", []

    def gate_binaries_are_static_rust(self) -> tuple[bool, str, list[str]]:
        """The shipped binaries must be native ELF from rustc.

        Several signals are accepted because a submission may strip symbols or
        enable LTO in ways that erase any one of them.  Requiring one specific
        marker would fail honest work; requiring at least one keeps the claim
        meaningful.
        """
        objects = self.shipped_objects()
        if not objects:
            arts = self.artifacts
            where = arts.prefix if arts else "<no build>"
            return False, f"no ELF binary was installed under {where}", []
        evidence: list[str] = []
        without: list[str] = []
        for label, path, obj in objects:
            signals: list[str] = []
            comments = " ".join(obj.strings_in(".comment"))
            if "rustc" in comments:
                signals.append("rustc in .comment")
            if any(n.startswith((".rustc", ".rmeta")) for n in obj.section_names):
                signals.append("rust metadata section")
            mangled = [
                s.name for s in obj.symtab
                if s.name.startswith("_ZN") and re.search(r"17h[0-9a-f]{16}E$", s.name)
            ]
            if mangled:
                signals.append(f"{len(mangled)} rust-mangled symbols")
            if any(s.name in ("rust_eh_personality", "__rust_alloc",
                              "__rust_dealloc", "__rust_probestack")
                   for s in obj.symtab):
                signals.append("rust runtime symbols")
            if signals:
                evidence.append(f"{label}: {', '.join(signals)}")
            else:
                without.append(label)
        if not evidence:
            return (
                False,
                "no shipped binary carries any Rust provenance marker (.comment "
                "producer, metadata section, mangled symbols or runtime symbols)",
                without[:10],
            )
        return True, f"{len(evidence)} binary/binaries carry Rust provenance", evidence

    def gate_no_js_in_binaries(self) -> tuple[bool, str, list[str]]:
        """No shipped binary may contain State A's JavaScript.

        A submission that embedded the original source to interpret at runtime
        would leave it in `.rodata`.  The search is for the same long
        whitespace-normalised runs the source gate looks for, plus the module
        basenames, since an embedded bundle usually keeps its file names.
        """
        objects = self.shipped_objects()
        if not objects:
            return False, "no shipped binary to inspect", []
        basenames = self.baseline_js_basenames()
        # Wider stride than the source gate: a binary that embedded the sources
        # embedded all of them, so a sparse set of needles finds it just as
        # surely and the scan over several megabytes of .rodata stays quick.
        needles = self.needles(width=160, stride=400)
        offenders: list[str] = []
        for label, path, _obj in objects:
            try:
                blob = path.read_bytes()
            except OSError as exc:
                offenders.append(f"{label}: unreadable ({exc})")
                continue
            text = blob.decode("latin-1")
            flat = " ".join(text.split())
            hit = next((n for n in needles if n in flat), None)
            if hit:
                offenders.append(f"{label}: contains {hit[:120]!r} from State A")
            # `.js` module names appearing together is the other signature.
            named = sorted(n for n in basenames if n.encode() in blob)
            if len(named) >= 3:
                offenders.append(
                    f"{label}: names {len(named)} State A modules "
                    f"({', '.join(named[:6])})"
                )
        if offenders:
            return (
                False,
                "a shipped binary contains State A's JavaScript",
                offenders[:12],
            )
        return True, "no shipped binary contains State A's JavaScript", []

    def gate_no_runtime_spawn(self) -> tuple[bool, str, list[str]]:
        """Running the probe must not spawn anything.

        The static checks above can be evaded by building the interpreter path at
        runtime.  This one is empirical: the probe is run with a PATH containing
        only tripwires, answering a request that exercises the whole parser, and
        the ledger is read afterwards.  A submission that shells out has nowhere
        to shell out to, and says so in the ledger.

        The empirical form is what makes the empty case dangerous: a tree with no
        binaries installed spawns nothing, for the reason that it runs nothing.
        The probe reports how many binaries it managed to exercise so that this
        gate can tell that apart from a clean run.
        """
        if self.probe_env_probe is None:
            return False, "the runtime spawn probe is not configured; nothing was measured", []
        try:
            events, detail, exercised = self.probe_env_probe()
        except Exception as exc:
            return False, f"the runtime spawn probe failed: {exc}", []
        if not exercised:
            return (
                False,
                "no installed binary could be run, so nothing was asked and the "
                "empty tripwire ledger is not evidence of anything",
                [detail] if detail else [],
            )
        if events:
            evidence = [
                f"{e.get('tool')} {' '.join(e.get('argv', [])[:6])}"
                for e in events[:12]
            ]
            return (
                False,
                f"the probe attempted {len(events)} interpreter invocation(s) "
                f"while answering requests",
                evidence,
            )
        return True, detail or "the probe spawned no interpreter while answering", []

    # == G4: nothing that tells the verifier apart from a user ==============

    def gate_no_verifier_awareness(self) -> tuple[bool, str, list[str]]:
        """No source file may branch on being graded.

        Every name here is one this verifier actually sets or uses.  A submission
        reading any of them is deciding its behaviour from the fact that it is
        being tested, which invalidates every comparison downstream of it.
        """
        markers = (
            "ACORN_PROBE_UNDER_TEST", "JSSHIM_LOG", "SWEREFACTOR", "swerefactor",
            "VERIFIER", "/logs/verifier", "reward.json", "ACORN_REFERENCE_REPO",
            "expected.ndjson", "requests.ndjson", "fixture-digests",
            # A submission that read the exclusion list would know which cases it
            # is not asked, which is the same kind of knowledge as reading the
            # answers: it is a fact about the grader, not about acorn.
            "excluded.json",
        )
        offenders: list[str] = []
        for path in self.files:
            if _is_generated_dir(path.relative_to(self.repo)):
                continue
            if path.suffix.lower() not in (".rs", ".toml", ".mk", "") \
                    and path.name not in ("Makefile", "makefile", "GNUmakefile"):
                continue
            text = read_text(path, 2_000_000)
            found = [m for m in markers if m in text]
            if found:
                offenders.append(f"{rel(self.repo, path)}: mentions {found[:4]}")
        if offenders:
            return (
                False,
                "the submission refers to the grading environment; behaviour "
                "conditioned on being graded is not behaviour",
                offenders[:20],
            )
        return True, "no source file refers to the grading environment", []

    def gate_no_env_dispatch(self) -> tuple[bool, str, list[str]]:
        """No environment variable may change what the parser produces.

        Reading the environment is not itself wrong -- a CLI may want `NO_COLOR`
        or `TERM`. What this looks for is an environment read in the same
        function-sized window as a parser entry point, which is how a submission
        would arrange to behave one way when graded and another way otherwise.
        """
        offenders: list[str] = []
        env_rx = re.compile(r"(?:std::)?env::(?:var|var_os|vars)\s*\(")
        parser_rx = re.compile(
            r"\bfn\s+(parse|parse_expression_at|tokenize|loose_parse|walk_full)\b"
        )
        for path in self.rust_files:
            text = read_text(path)
            if not env_rx.search(text):
                continue
            lines = text.splitlines()
            env_lines = {i for i, line in enumerate(lines) if env_rx.search(line)}
            for i, line in enumerate(lines):
                if not parser_rx.search(line):
                    continue
                near = [j for j in env_lines if abs(j - i) <= 60]
                if near:
                    offenders.append(
                        f"{rel(self.repo, path)}:{i + 1} a parser entry point is "
                        f"within 60 lines of an environment read at line "
                        f"{min(near) + 1}"
                    )
        if offenders:
            return (
                False,
                "an environment read sits next to a parser entry point",
                sorted(set(offenders))[:20],
            )
        return True, "no environment read is adjacent to a parser entry point", []

    def gate_no_network(self) -> tuple[bool, str, list[str]]:
        """No source file may open a socket or name a URL to fetch.

        Documentation URLs are everywhere in this codebase and are fine.  What is
        not fine is the networking API surface: a parser has no use for it, and
        its presence means something is being fetched.
        """
        apis = (
            "std::net::", "TcpStream", "TcpListener", "UdpSocket", "UnixStream",
            "libc::socket", "libc::connect", "getaddrinfo", "reqwest", "hyper::",
            "curl_easy", "http_get",
        )
        offenders: list[str] = []
        for path in self.rust_files:
            text = read_text(path)
            found = [a for a in apis if a in text]
            if found:
                offenders.append(f"{rel(self.repo, path)}: uses {found[:3]}")
        if offenders:
            return (
                False,
                "the submission contains networking code, which a parser has no "
                "use for",
                offenders[:20],
            )
        return True, "no networking API appears in the submission", []

    # == G5: the things that had to survive ================================

    def gate_licenses_preserved(self) -> tuple[bool, str, list[str]]:
        """The licence and attribution files must survive, byte for byte.

        A rewrite does not relicense the work.  These files are compared against
        State A rather than merely required to exist, because a LICENSE that has
        been edited is worse than one that is missing: it looks fine.
        """
        required = self.contract["retained_paths"]["required"]
        problems: list[str] = []
        evidence: list[str] = []
        for name in required:
            here = self.repo / name
            there = self.baseline / name
            if not here.is_file():
                problems.append(f"{name}: missing")
                continue
            if not there.is_file():
                evidence.append(f"{name}: present (no baseline to compare)")
                continue
            got = vlib.sha256_file(here)
            want = vlib.sha256_file(there)
            if name.endswith("LICENSE") or name == "AUTHORS":
                if got != want:
                    problems.append(
                        f"{name}: content changed (sha {got[:12]} vs {want[:12]})"
                    )
                else:
                    evidence.append(f"{name}: identical to State A")
            else:
                # READMEs and CHANGELOGs must survive but are expected to be
                # edited: a README that still documents `npm install` would be
                # wrong after this port.
                evidence.append(
                    f"{name}: present, {'identical' if got == want else 'edited'}"
                )
        if problems:
            return False, "; ".join(problems)[:1000], evidence[:20]
        return (
            True,
            f"all {len(required)} retained path(s) present, licences byte-identical",
            evidence[:20],
        )

    def gate_no_frozen_answers(self) -> tuple[bool, str, list[str]]:
        """The submission must not contain the answers.

        A table of expected outputs keyed by input is not a parser.  This looks
        for the shapes such a table takes: a large map from source text to AST
        JSON, or an unusual mass of embedded JSON that mentions ESTree node
        types.
        """
        offenders: list[str] = []
        # Serialised ESTree, not Rust.  A port emitting JSON writes the node name
        # as `"Program"` in a match arm; it has no reason to contain the pair
        # `"type": "Program"`, which only exists in output that has already been
        # produced.
        estree_rx = re.compile(
            r'"type"\s*:\s*\\?"(Program|ExpressionStatement|BinaryExpression|'
            r'VariableDeclaration|CallExpression|MemberExpression|Identifier|'
            r'Literal)\\?"'
        )
        for path in self.files:
            relpath = path.relative_to(self.repo)
            if _is_generated_dir(relpath):
                continue
            if path.suffix.lower() not in (".rs", ".json", ".txt", ".ndjson", ".bin"):
                continue
            # A port's own test suite legitimately contains expected ASTs, and it
            # should have one.  Tests are not on the graded path: `cargo build
            # --release` does not compile them, so they cannot answer a request.
            if _is_test_path(relpath):
                continue
            text = read_text(path, 8_000_000)
            if path.suffix.lower() == ".rs":
                text = _strip_cfg_test(text)
            hits = len(estree_rx.findall(text))
            # No size floor.  The threshold that matters is how many answers are
            # in the file, and a 44 KB table of 400 answers is the same cheat as a
            # 4 MB one.
            if hits >= 40:
                offenders.append(
                    f"{rel(self.repo, path)}: {len(text)} bytes of non-test code "
                    f"containing {hits} serialised ESTree nodes"
                )
        if offenders:
            return (
                False,
                "the submission embeds what looks like a table of expected "
                "outputs rather than code that computes them",
                offenders[:12],
            )
        return True, "no embedded table of expected outputs was found", []

    def gate_single_implementation(self) -> tuple[bool, str, list[str]]:
        """The graded parser must be the one a default build produces.

        The failure this exists for is a submission that keeps a second parser
        and selects it under some condition: every behavioural case then passes
        through whichever branch the verifier happens to take, and nothing about
        the result describes the code a user would get.

        What it does *not* treat as a finding is more than one function named
        `parse`.  State A has `acorn.parse`, `acornLoose.parse` and
        `walk.findNodeAt`; a port with `acorn::parse` and `acorn_loose::parse` has
        reproduced the public API, not duplicated the implementation.  So the
        signal is conditional compilation around a parser item, plus a cargo
        feature that could turn one on.
        """
        offenders: list[str] = []
        # `test`, `doc`, `target_*`, `unix` and `windows` are all ordinary and
        # excluded.  What is left is a condition that selects code for a reason
        # the platform did not ask for.
        cfg_rx = re.compile(
            r"#\[cfg(?:_attr)?\(\s*"
            r'(?!test\b|doc\b|doctest\b|target|unix\b|windows\b|miri\b|'
            r'clippy\b|rustfmt\b|not\s*\(\s*(?:test|target|unix|windows)\b)'
            r"([^)]{0,120})\)\]"
        )
        item_rx = re.compile(r"\b(?:pub\s+)?(?:mod|fn|use|impl|struct|enum)\b")
        for path in self.rust_files:
            text = read_text(path)
            for match in cfg_rx.finditer(text):
                cond = match.group(1).strip()
                after = text[match.end():match.end() + 240]
                if not item_rx.search(after):
                    continue
                offenders.append(
                    f"{rel(self.repo, path)}: an item is compiled only under "
                    f"cfg({cond[:70]})"
                )
        # A feature that gates anything at all is the switch such a cfg needs.
        for path in self.files:
            if path.name != "Cargo.toml":
                continue
            text = read_text(path, 400_000)
            section = re.search(r"(?ms)^\[features\](.*?)(?=^\[|\Z)", text)
            if not section:
                continue
            names = re.findall(r"(?m)^\s*([A-Za-z0-9_-]+)\s*=", section.group(1))
            extra = [n for n in names if n != "default"]
            if extra:
                offenders.append(
                    f"{rel(self.repo, path)}: declares cargo feature(s) "
                    f"{extra[:5]}; the graded build enables only the defaults"
                )
        if offenders:
            return (
                False,
                f"{len(offenders)} finding(s): part of the submission is compiled "
                f"conditionally, so what was graded is not what a default build "
                f"produces",
                sorted(set(offenders))[:20],
            )
        return True, "no conditionally-compiled item and no cargo feature", []
