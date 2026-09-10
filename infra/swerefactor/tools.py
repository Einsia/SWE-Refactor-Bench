"""The toolbox a reviewing model is given: read, list, search, diff, run.

Both model-driven stages face the same question -- did the old implementation
really leave, and did the new one really take over -- and neither can answer it
from a summary.  So the model gets the repository pair itself: ``/opt/original``
frozen at State A, ``/opt/workspace`` as submitted, and enough tooling to move
around both.

Three decisions are worth stating.

*Paths resolve against named roots, and the check is on the realpath.*  A
submission may contain a symlink to ``/``, so validating how a path is spelled
proves nothing; only where it lands does.

*Commands are allowlisted per task, with no shell.*  Some evidence has to be
run for rather than read: whether the built artifact still imports the old
module, what ``nm`` says about a symbol.  But the workspace is untrusted code,
and a grader that will run anything is a grader that can be made to run
anything.  So each task names the inspection tools its review needs, argv[0]
must be one of them, and there is no shell to smuggle a second command through.

*Truncation is announced.*  A model that silently receives the first 40 KiB of a
file reasons confidently about a fragment; one that is told it was truncated
asks for the rest.  Every cap here says so in the output.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

MAX_READ_BYTES = 60_000
MAX_SEARCH_HITS = 200
MAX_DIR_ENTRIES = 400
MAX_DIFF_LINES = 1_200
MAX_RUN_OUTPUT = 20_000
DEFAULT_RUN_TIMEOUT = 120.0

#: Skipped when walking, unless a path names them directly.  These are where the
#: interesting signal is *not*, and letting a search wander into a vendored
#: node_modules is how a review runs out of budget before it reads the source.
NOISE_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".venv", "venv", ".tox", ".gradle",
    ".idea", ".cargo", ".rustup",
})

class ToolError(Exception):
    """A tool call the model got wrong: a path that does not exist, a bad regex.

    Raised, caught, and returned to the model as the tool's result.  The model
    correcting itself is the intended path; aborting the stage because a model
    guessed a filename would throw away a review over nothing.
    """


@dataclass
class Root:
    """One directory the model may reach, under a short name it can type."""

    name: str
    path: Path
    writable: bool = False

    def __post_init__(self) -> None:
        self.path = Path(self.path).resolve()


@dataclass
class Toolbox:
    """Filesystem access for one model run, scoped to a set of roots."""

    roots: dict[str, Root]
    #: argv[0] values the task permits.  Empty means ``run`` is not offered at
    #: all, which is the default: a review that does not need to execute the
    #: submission should not be able to.
    allowed_commands: tuple[str, ...] = ()
    run_timeout_sec: float = DEFAULT_RUN_TIMEOUT
    default_root: str = "workspace"
    #: Every file the model actually read, for the grounding check later.
    reads: set[str] = field(default_factory=set)
    calls: int = 0
    #: Calls whose arguments arrived under a gateway's names and were translated.
    #: Reported in the stage metadata: a round that only worked because this
    #: fired is not the same measurement as one that never needed it.
    rewritten_args: int = 0

    # -- path handling ------------------------------------------------------ #

    def resolve(self, raw: str, *, must_exist: bool = True) -> tuple[Root, Path]:
        """``workspace:src/main.rs``, ``/opt/original/src``, or ``src/main.rs``."""
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path must be a non-empty string")
        text = raw.strip()
        root: Root | None = None
        rel = text

        if ":" in text and not text.startswith("/"):
            name, _, rest = text.partition(":")
            if name in self.roots:
                root, rel = self.roots[name], rest.lstrip("/")
        if root is None and text.startswith("/"):
            candidate = Path(text)
            for r in self.roots.values():
                if candidate == r.path or r.path in candidate.parents:
                    root = r
                    rel = str(candidate.relative_to(r.path))
                    break
            if root is None:
                raise ToolError(
                    f"{text!r} is outside every readable root; the roots are "
                    + ", ".join(f"{r.name}={r.path}" for r in self.roots.values())
                )
        if root is None:
            # A rootless toolbox is a real configuration -- the scope adjudicator
            # gets one, because it judges a candidate's assertion and has no tree
            # to look at.  `next(iter(...))` on it raises StopIteration, which is
            # not an error any caller here is looking for: dispatch() lets it
            # through, agentloop lets it through, and it ends the stage from
            # inside adjudication.  Fail as a ToolError like every other bad path.
            root = self.roots.get(self.default_root)
            if root is None:
                if not self.roots:
                    raise ToolError(
                        "this toolbox has no readable roots, so no path can be "
                        "resolved; it offers no filesystem tools")
                root = next(iter(self.roots.values()))

        rel = "" if rel in (".", "./") else rel
        target = (root.path / rel).resolve() if rel else root.path
        # The realpath, not the spelling: a symlink in the submission is the
        # obvious way to try to read the grader's own files.
        if target != root.path and root.path not in target.parents:
            raise ToolError(
                f"{raw!r} resolves to {target}, which escapes root "
                f"{root.name}={root.path} (a symlink, most likely)"
            )
        if must_exist and not target.exists():
            raise ToolError(f"{raw!r} does not exist (resolved to {target})")
        return root, target

    def display(self, root: Root, target: Path) -> str:
        rel = target.relative_to(root.path)
        return f"{root.name}:{rel}" if str(rel) != "." else f"{root.name}:"


    # -- what the model is offered ------------------------------------------ #

    def specs(self) -> list["ToolSpecLike"]:
        from .models import ToolSpec

        # No roots, no filesystem tools.  Advertising `list_dir` to a model that
        # has nothing to list invites a call that can only fail, and the scope
        # adjudicator -- the one caller with an empty toolbox -- is meant to judge
        # a candidate's assertion from the text it was given, not by browsing.
        if not self.roots:
            return []

        root_names = ", ".join(sorted(self.roots))
        prefix = (f"Paths may be written 'root:relative/path' (roots: {root_names}), "
                  f"an absolute path inside a root, or a bare relative path, which "
                  f"is taken as {self.default_root}:.")
        specs = [
            ToolSpec(
                name="list_dir",
                description=(
                    f"List a directory. {prefix} Noise directories (.git, "
                    f"node_modules, __pycache__, build caches) are skipped unless "
                    f"named directly."),
                schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "depth": {"type": "integer", "minimum": 1, "maximum": 4,
                                  "description": "Recursion depth, default 1."},
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                # NOT `read_file`.  A tool by that name reaches an Anthropic model
                # through a Claude Code gateway carrying Claude Code's own Read
                # schema instead of this one: the model is shown
                # {file_path, limit, offset}, sends those, and every call fails
                # against the schema below.  Measured 2026-08-04 -- same schema,
                # three names, five graded models: `read_file` is rewritten on
                # claude-opus-5 and claude-sonnet-5 and clean on gpt-5.6-*, while
                # `fetch_source` and `inspect_file` are clean on all five.  It is
                # the name, not the schema, and the model cannot work around it
                # because the substitution happens before it sees the tool.
                name="fetch_source",
                description=(
                    f"Read a file, with line numbers. {prefix} Use start_line and "
                    f"end_line for a slice of a long file; output is capped at "
                    f"{MAX_READ_BYTES} bytes and says so when it truncates."),
                schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                name="search",
                description=(
                    f"Search file contents with a Python regex, reporting "
                    f"path:line for each hit (up to {MAX_SEARCH_HITS}). {prefix} "
                    f"Restrict with 'glob' (e.g. '*.rs', 'CMakeLists.txt')."),
                schema={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string"},
                        "path": {"type": "string",
                                 "description": "Directory or file to search. "
                                                "Defaults to the whole workspace."},
                        "glob": {"type": "string",
                                 "description": "Filename pattern filter."},
                        "ignore_case": {"type": "boolean"},
                    },
                    "required": ["pattern"],
                },
            ),
            ToolSpec(
                name="diff",
                description=(
                    "Unified diff of the same relative path in two roots -- the "
                    "fastest way to see what the migration did to one file. "
                    "Give the relative path once, plus the two root names."),
                schema={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string",
                                 "description": "Relative path, e.g. src/main.c"},
                        "left": {"type": "string", "description": "Root name, "
                                 "default 'original'."},
                        "right": {"type": "string", "description": "Root name, "
                                  "default 'workspace'."},
                    },
                    "required": ["path"],
                },
            ),
        ]
        if self.allowed_commands:
            specs.append(ToolSpec(
                name="run",
                description=(
                    "Run one inspection command. No shell: pass argv as a list. "
                    "Permitted programs for this task: "
                    + ", ".join(self.allowed_commands)
                    + f". Timeout {self.run_timeout_sec:g}s."),
                schema={
                    "type": "object",
                    "properties": {
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "cwd": {"type": "string",
                                "description": "Working directory, default the "
                                               "workspace root."},
                    },
                    "required": ["argv"],
                },
            ))
        return specs

    def handles(self, name: str) -> bool:
        if not self.roots:
            return False
        return name in {"list_dir", "fetch_source", "search", "diff"} or (
            name == "run" and bool(self.allowed_commands))

    def _unknown_args(self, name: str, args: dict[str, Any]) -> str | None:
        """Name an argument this tool does not take, or return None.

        Reading an argument with ``args.get("path", "")`` treats a name the tool
        does not know as an absent value, which produces a true but undiagnosable
        complaint: a model that sent ``file_path`` is told ``path must be a
        non-empty string`` and cannot see from that which of the two words was
        the problem.  In one graded stage 3 this cost three of six adversaries
        their entire turn budget -- 61%, 72% and 56% of their tool calls failed,
        every one of them on ``file_path``/``limit``/``offset``, the argument
        names of a different tool the models had evidently learned elsewhere.

        Retrying an identical call a dozen times is not the behaviour of a model
        that has lost its context; it is the only move left to one that is being
        refused without being told what to change.  So say what to change.
        """
        try:
            allowed = {
                spec.name: set((spec.schema.get("properties") or {}).keys())
                for spec in self.specs()
            }.get(name)
        except Exception:  # noqa: BLE001 -- a mis-built spec must not break dispatch
            return None
        if not allowed:
            return None
        unknown = [k for k in args if k not in allowed]
        if not unknown:
            return None
        return (f"ERROR: {name} does not take "
                f"{', '.join(repr(u) for u in sorted(unknown))}. "
                f"Its arguments are: {', '.join(sorted(allowed))}. "
                f"Nothing ran; re-send the call using those names.")

    def _translate_gateway_args(self, name: str,
                                args: dict[str, Any]) -> dict[str, Any]:
        """Accept the argument names a gateway's own schema substitutes in.

        Measured, on an Anthropic-compatible relay: a tool named ``read_file``
        reaches the model carrying Claude Code's ``Read`` schema instead of the
        one declared here, so the model sends ``{file_path, limit, offset}``.  It
        is the *name* that triggers it -- the identical schema under
        ``fetch_source`` or ``inspect_file`` arrives intact, on claude-opus-5 and
        claude-sonnet-5 alike, while all three gpt models are unaffected because
        the openai dialect reads ``function.parameters``.

        This cannot be fixed by complaining more clearly.  ``_unknown_args`` above
        already names the wrong word and the right ones, and the model still
        re-sends ``file_path`` every turn: it is complying with the schema it was
        given, so being told that schema is wrong leaves it no compliant move.
        Measured cost of leaving it: three of six adversaries in one graded stage
        3 spent their whole turn budget on refused calls -- 90/90, 21/21 -- and
        ``verification.py`` pays a turn-exhausted round as SURVIVED, so a round
        that read zero files still collected ``points_per_survived_model``.

        ``offset`` is a 1-based first line and ``limit`` is a count of lines --
        both probed against both models rather than assumed, because ``end_line``
        is a position and an off-by-one here returns a wrong slice, which is worse
        than a refusal: it is wrong evidence a reviewer will cite.

        Why this still exists now that the tool *has* been renamed, which two
        branches proposed as alternatives to each other: the rename stops the
        gateway from claiming the name, and this stops a model that sends those
        arguments anyway.  They are not the same defence and only one of them is
        the model's problem -- ``Read``'s argument names are learned, not only
        substituted, so a model reaching for a file reaches for ``file_path``
        whatever the tool is called.  The rename's own objection (that
        ``read_file`` was named in 20 task Dockerfiles and ``docs/SCHEMA.md``, and
        ``infra/`` is vendored per clone so the edits could not follow) did not
        survive contact: those 24 files say ``fetch_source``, and
        ``test_layout.py`` fails if a prompt drifts back.

        Gated on ``fetch_source`` and not on ``read_file`` because that is the
        name that exists.  Left on ``read_file`` it would be unreachable code that
        reads as a live mitigation -- ``handles()`` refuses the old name before
        dispatch could ever call this.
        """
        if name != "fetch_source" or not isinstance(args, dict):
            return args
        if not ({"file_path", "offset", "limit"} & set(args)):
            return args

        out = dict(args)
        # Never clobber a correctly-named argument that is already present; a
        # mixed call keeps what it got right.
        if "file_path" in out:
            substituted = out.pop("file_path")
            if "path" not in out:
                out["path"] = substituted
        offset, limit = out.pop("offset", None), out.pop("limit", None)
        try:
            if offset is not None and "start_line" not in out:
                out["start_line"] = int(offset)
            if limit is not None and "end_line" not in out:
                base = int(out.get("start_line") or 1)
                out["end_line"] = base + int(limit) - 1
        except (TypeError, ValueError):
            # A slice we cannot read is dropped, not guessed: the whole file is
            # the honest fallback, and fetch_source announces its own truncation.
            out.pop("start_line", None)
            out.pop("end_line", None)
        self.rewritten_args += 1
        return out

    def dispatch(self, name: str, args: dict[str, Any]) -> str:
        """Run one tool call.  Never raises for model error -- returns the text."""
        self.calls += 1
        # Before _unknown_args and not after, or the translation never runs: those
        # names are exactly the ones it is built to reject, so a substituted
        # argument that can be mapped produces the file rather than a complaint
        # about its name.  Two branches wrote this line, one calling its function
        # `_canonical_args` and one `_translate_gateway_args`; they are one fix and
        # only the second name survives, because `audit.py` reports its counter
        # as `gateway_rewritten_args`.
        args = self._translate_gateway_args(name, args)
        complaint = self._unknown_args(name, args)
        if complaint:
            return complaint
        try:
            if name == "list_dir":
                return self.list_dir(str(args.get("path", ".")),
                                     int(args.get("depth", 1) or 1))
            if name == "fetch_source":
                return self.fetch_source(
                    str(args.get("path", "")),
                    args.get("start_line") and int(args["start_line"]),
                    args.get("end_line") and int(args["end_line"]),
                )
            if name == "search":
                return self.search(
                    str(args.get("pattern", "")),
                    str(args.get("path") or f"{self.default_root}:"),
                    str(args.get("glob") or ""),
                    bool(args.get("ignore_case")),
                )
            if name == "diff":
                return self.diff(str(args.get("path", "")),
                                 str(args.get("left") or "original"),
                                 str(args.get("right") or "workspace"))
            if name == "run":
                return self.run(args.get("argv") or [], args.get("cwd"))
        except ToolError as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            # "Never raises for model error" has to hold for every error, not for
            # the three types that were foreseen.  A StopIteration from a rootless
            # toolbox used to escape this handler and end the stage from inside
            # adjudication, taking with it every round that had already passed.
            return f"ERROR: {type(exc).__name__}: {exc}"
        return f"ERROR: no such tool {name!r}"


    # -- the operations ----------------------------------------------------- #

    def list_dir(self, path: str, depth: int = 1) -> str:
        root, target = self.resolve(path)
        if target.is_file():
            return self._stat_line(root, target)
        depth = max(1, min(4, depth))
        lines: list[str] = [f"{self.display(root, target)}/"]
        truncated = False
        for entry, level in _walk(target, depth):
            if len(lines) > MAX_DIR_ENTRIES:
                truncated = True
                break
            indent = "  " * level
            if entry.is_dir():
                mark = "/" + ("  [skipped]" if entry.name in NOISE_DIRS else "")
                lines.append(f"{indent}{entry.name}{mark}")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = -1
                link = " -> " + os.readlink(entry) if entry.is_symlink() else ""
                lines.append(f"{indent}{entry.name}  ({size} B){link}")
        if truncated:
            lines.append(f"... truncated at {MAX_DIR_ENTRIES} entries; list a "
                         f"subdirectory or use search to narrow this down")
        return "\n".join(lines)

    def _stat_line(self, root: Root, target: Path) -> str:
        st = target.stat()
        return (f"{self.display(root, target)}  ({st.st_size} B, mode "
                f"{oct(st.st_mode & 0o777)})")

    def _binary_line(self, root: Root, target: Path, raw: bytes) -> str:
        """What to say about a file this tool cannot decode.

        It used to say "use run with a suitable inspection tool", unconditionally.
        ``run`` is offered only when the stage declares ``allowed_commands``, and a
        stage that declares none -- which is every review whose question is about
        source rather than about artifacts -- sent its reviewers after a tool that
        was not in their schema.  A model that takes the advice spends a turn on an
        unknown-tool error; one that does not is left thinking the observation was
        available and it failed to make it.  Neither is a reading of the file.

        So the sentence now depends on the toolbox, and in the no-``run`` case it
        says the thing that is actually true: nothing here will decode this, and
        the decodable evidence is elsewhere.  The magic-byte label is included
        because "binary" and "an ELF executable" support different conclusions, and
        the first four bytes are something this tool can honestly report.
        """
        kind = ""
        for magic, label in ((b"\x7fELF", "ELF"), (b"!<arch>\n", "ar archive"),
                             (b"\x1f\x8b", "gzip"), (b"PK\x03\x04", "zip"),
                             (b"\x89PNG", "PNG"), (b"\x00asm", "wasm")):
            if raw.startswith(magic):
                kind = f", first bytes say {label}"
                break
        head = f"{self.display(root, target)} is binary ({len(raw)} B{kind})."
        if self.allowed_commands:
            return (f"{head} Use run with a suitable inspection tool if this "
                    f"matters.")
        return (f"{head} No tool in this stage's toolbox decodes it -- there is no "
                f"run here, and search matches literal bytes only. That it exists "
                f"with this size and this type is the whole of what can be read "
                f"from it; a conclusion about what it contains has to come from "
                f"the text that produces it.")

    def fetch_source(self, path: str, start: int | None = None,
                     end: int | None = None) -> str:
        root, target = self.resolve(path)
        if target.is_dir():
            raise ToolError(f"{path!r} is a directory; use list_dir")
        raw = target.read_bytes()
        if b"\0" in raw[:8000]:
            return self._binary_line(root, target, raw)
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        lo = max(1, start or 1)
        hi = min(len(lines), end or len(lines))
        if lo > len(lines):
            raise ToolError(f"{path!r} has {len(lines)} lines; start_line {lo} "
                            f"is past the end")
        chunk = lines[lo - 1:hi]
        body, clipped = _cap("\n".join(
            f"{lo + i:6d}\t{line}" for i, line in enumerate(chunk)
        ), MAX_READ_BYTES)
        self.reads.add(str(target))
        header = f"{self.display(root, target)}  lines {lo}-{hi} of {len(lines)}"
        if clipped:
            header += (f"  [TRUNCATED at {MAX_READ_BYTES} B -- ask for a "
                       f"narrower line range to see the rest]")
        return f"{header}\n{body}"

    def search(self, pattern: str, path: str, glob: str = "",
               ignore_case: bool = False) -> str:
        if not pattern:
            raise ToolError("pattern is required")
        try:
            rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ToolError(f"bad regex {pattern!r}: {exc}") from exc
        root, target = self.resolve(path)
        hits: list[str] = []
        scanned = 0
        for file in _files(target, glob):
            scanned += 1
            try:
                raw = file.read_bytes()
            except OSError:
                continue
            if b"\0" in raw[:4000]:
                continue
            for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    rel = file.relative_to(root.path)
                    hits.append(f"{root.name}:{rel}:{n}: {line.strip()[:220]}")
                    if len(hits) >= MAX_SEARCH_HITS:
                        break
            if len(hits) >= MAX_SEARCH_HITS:
                break
        if not hits:
            return (f"no match for {pattern!r} under {self.display(root, target)}"
                    + (f" (glob {glob})" if glob else "")
                    + f"; {scanned} files scanned")
        head = f"{len(hits)} hit(s) for {pattern!r} in {scanned} files scanned"
        if len(hits) >= MAX_SEARCH_HITS:
            head += f" [capped at {MAX_SEARCH_HITS}; narrow the pattern or path]"
        return head + "\n" + "\n".join(hits)

    def diff(self, path: str, left: str = "original", right: str = "workspace") -> str:
        for name in (left, right):
            if name not in self.roots:
                raise ToolError(f"unknown root {name!r}; roots are "
                                f"{', '.join(sorted(self.roots))}")
        rel = path.strip().lstrip("/")
        if ":" in rel:
            rel = rel.partition(":")[2].lstrip("/")
        lpath = (self.roots[left].path / rel)
        rpath = (self.roots[right].path / rel)
        ltext = _text_or_empty(lpath)
        rtext = _text_or_empty(rpath)
        if ltext is None and rtext is None:
            raise ToolError(f"{rel!r} exists in neither {left} nor {right}")
        if ltext is None:
            return (f"{rel} does not exist in {left}; it is new in {right} "
                    f"({len((rtext or '').splitlines())} lines). Use fetch_source "
                    f"to see it.")
        if rtext is None:
            return (f"{rel} exists in {left} "
                    f"({len(ltext.splitlines())} lines) but is ABSENT from "
                    f"{right} -- it was deleted or moved.")
        out = list(difflib.unified_diff(
            ltext.splitlines(), rtext.splitlines(),
            fromfile=f"{left}/{rel}", tofile=f"{right}/{rel}", lineterm="", n=3,
        ))
        self.reads.add(str(rpath))
        if not out:
            return f"{rel} is byte-identical in {left} and {right}."
        if len(out) > MAX_DIFF_LINES:
            out = out[:MAX_DIFF_LINES] + [
                f"... diff truncated at {MAX_DIFF_LINES} lines of "
                f"{len(out)}; fetch_source the region you care about"]
        return "\n".join(out)

    def run(self, argv: Any, cwd: str | None = None) -> str:
        if not self.allowed_commands:
            raise ToolError("this task's review does not permit running commands")
        if isinstance(argv, str):
            raise ToolError("argv must be a list of strings -- there is no shell, "
                            "so a command line in one string will not work")
        if not isinstance(argv, list) or not argv:
            raise ToolError("argv must be a non-empty list of strings")
        argv = [str(a) for a in argv]
        program = os.path.basename(argv[0])
        if program not in self.allowed_commands and argv[0] not in self.allowed_commands:
            raise ToolError(
                f"{program!r} is not permitted for this task. Permitted: "
                f"{', '.join(self.allowed_commands)}"
            )
        if cwd:
            _, workdir = self.resolve(cwd)
        else:
            workdir = self.roots[self.default_root].path
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        try:
            proc = subprocess.run(
                argv, cwd=str(workdir), env=env, capture_output=True, text=True,
                timeout=self.run_timeout_sec,
            )
        except subprocess.TimeoutExpired:
            return (f"$ {' '.join(argv)}\nTIMEOUT after "
                    f"{self.run_timeout_sec:g}s -- no output is trustworthy")
        except OSError as exc:
            raise ToolError(f"cannot run {argv[0]!r}: {exc}") from exc
        body, clipped = _cap(
            (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else ""),
            MAX_RUN_OUTPUT,
        )
        note = "  [output truncated]" if clipped else ""
        return (f"$ {' '.join(argv)}  (cwd {workdir})\nexit {proc.returncode}"
                f"{note}\n{body}")


    # -- evidence grounding ------------------------------------------------- #

    def ground(self, path: str, line: int | None = None,
               quote: str = "") -> tuple[bool, str]:
        """Confirm a citation points at something real.

        A model asked for evidence will sometimes produce a plausible citation
        rather than a true one -- the right *kind* of filename, a line number in
        the right range.  Since the finding is what zeroes a submission, each
        citation is checked against the tree before it is allowed to count:

        * the path resolves inside a root and exists;
        * the line number, if given, is within the file;
        * the quoted text, if given, appears at or near that line.

        Returns ``(grounded, note)``.  A finding whose citations are all
        ungrounded is downgraded by the caller rather than deleted, because the
        observation may still be true -- but it is no longer decisive.
        """
        try:
            root, target = self.resolve(path)
        except ToolError as exc:
            return False, str(exc)
        if target.is_dir():
            return (line is None), ("cited a directory with a line number"
                                    if line is not None else "directory exists")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return False, f"unreadable: {exc}"
        if b"\0" in raw[:8000]:
            return line is None, "binary file"
        lines = raw.decode("utf-8", "replace").splitlines()
        if line is not None:
            if line < 1 or line > len(lines):
                return False, (f"cited line {line} but {self.display(root, target)} "
                               f"has {len(lines)} lines")
            if quote:
                window = "\n".join(lines[max(0, line - 4):line + 3])
                if _loose(quote) not in _loose(window):
                    return False, (f"quoted text not found within 3 lines of "
                                   f"line {line}")
            return True, "verified"
        if quote:
            body = raw.decode("utf-8", "replace")
            if _loose(quote) not in _loose(body):
                return False, "quoted text not found in file"
        return True, "verified"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _walk(base: Path, depth: int, level: int = 0) -> Iterator[tuple[Path, int]]:
    try:
        entries = sorted(base.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError:
        return
    for entry in entries:
        yield entry, level
        if (entry.is_dir() and not entry.is_symlink()
                and entry.name not in NOISE_DIRS and level + 1 < depth):
            yield from _walk(entry, depth, level + 1)


def _files(base: Path, glob: str = "") -> Iterator[Path]:
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in NOISE_DIRS)
        for name in sorted(filenames):
            if glob and not Path(name).match(glob):
                continue
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            yield path


def _text_or_empty(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except (OSError, IsADirectoryError):
        return None
    if b"\0" in raw[:8000]:
        return None
    return raw.decode("utf-8", "replace")


def _cap(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", "ignore"), True


def _loose(text: str) -> str:
    """Whitespace-insensitive, for comparing a quote against a file.

    A model quoting a line will reflow it, and rejecting a true citation over an
    indentation difference would make grounding useless.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def toolbox_for(original: Path, workspace: Path, *,
                allowed_commands: Any = (),
                run_timeout_sec: float = DEFAULT_RUN_TIMEOUT,
                extra: dict[str, Path] | None = None) -> Toolbox:
    """The standard pair of roots: read-only original, submitted workspace."""
    roots = {
        "original": Root("original", original, writable=False),
        "workspace": Root("workspace", workspace, writable=False),
    }
    for name, path in (extra or {}).items():
        roots[name] = Root(name, path, writable=False)
    return Toolbox(
        roots=roots,
        allowed_commands=tuple(str(c) for c in (allowed_commands or ())),
        run_timeout_sec=run_timeout_sec,
    )


# Imported lazily in ``specs`` to keep this module importable without a driver.
ToolSpecLike = Any
