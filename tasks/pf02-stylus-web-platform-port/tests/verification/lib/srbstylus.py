"""What a stage-3 candidate is given to attack a migrated CSS compiler.

A candidate is a pytest file.  It may import this module and the standard library,
and nothing else.

    import srbstylus

    def test_unit_arithmetic():
        t = srbstylus.tree()
        r = t.render("a\\n  width 10px + 5px\\n")
        assert r.css == "a {\\n  width: 15px;\\n}\\n"

The difficulty this module exists to solve is specific to a platform port.  The
two trees do not have the same *shape*: the original is CommonJS with its whole
compiler in `lib/`, and the submission is an ES module core in a restricted realm
with a Node adapter beside it.  What they do have in common is the thing an
existing user consumes -- the published package -- so that is the only door this
module opens:

    the package's published entry point, reached as `import('stylus')`
    `bin/stylus`, the command line

Neither is addressed by a path into the tree.  A consumer directory is built with
`node_modules/stylus` pointing at the tree under test, and Node's own resolution
picks `exports["."]` when the tree publishes one and `main` when it does not.  The
original (CommonJS, `main: ./index.js`) and a submission (ESM, `exports` ->
`src/node/index.js`) are therefore reached by the same line of code, and a
candidate cannot tell which tree it is on from the way it asks a question.

WHAT IS DELIBERATELY NOT HERE

`src/core/`, the realm, the capability object, the `./web` export.  The original
has no web core at all, so a candidate that reached for one would fail on the
original and be discarded as "wrong about the original" -- however correct its
finding.  Those are stage 1's and stage 2's subject and both have already run.

Also not here: any way to read the tree's source.  What is graded is behaviour.

PATHS

A candidate writes stylesheets into a project and addresses them as `/proj/...`.
Every path that comes back -- an error's `.filename`, a `linenos` comment, a
source map's `sources` -- is mapped back to `/proj`, and any path inside the tree
itself is collapsed to `/tree/<internal>`.  The second half is not tidiness: the
harness stages the two trees into different directories by design, and the one
tree-internal path that reaches output is the built-in `.styl` library, which
instruction.md §2.1 requires to move and §1.4 declines to place.  Comparing it
would score a submission on the one thing the task left free.

INSTALLING

`tree()` installs the tree's dependencies offline from its own lockfile, once,
cached for every later candidate in every later round.  A tree whose lockfile does
not install raises `BuildFailed`, which is a stage-2 verdict rather than a finding
here -- the prompt says so, and the adjudicator knows the difference.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "VERSION", "TARGET_NAME", "SCRATCH", "VPROJ", "VTREE",
    "Result", "Output", "Project", "Tree", "BuildFailed", "DriverFailure", "tree",
]

#: The version both trees are of.  instruction.md §3 freezes the package identity,
#: so this is not a candidate's to discover: a submission that changed it fails a
#: stage-1 gate and a stage-2 module, not this stage.
VERSION = "0.63.0"

#: An opaque per-run label for the tree under test, for failure messages.
#:
#: Not "original" or "submission".  `assert TARGET_NAME == "original"` would pass
#: on the original, fail on the submission and reproduce perfectly while
#: establishing nothing about the migration, so the role does not reach a
#: candidate under any name.  This is a hash: it says *which* tree without saying
#: which side, so two messages from one comparison stay distinguishable.
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")

#: This candidate's own scratch directory, emptied between the two halves of a
#: comparison.  Write here and nowhere else.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

#: Where state shared across candidates and rounds lives -- chiefly the installed
#: dependency tree, which is why the first candidate to call `tree()` pays for it
#: and the rest do not.
_STATE = Path(os.environ.get("SRB_STATE", "/tmp/srb-state"))

#: The tree under test.  A copy: safe to install into, and not the artifact the
#: earlier stages measured.
_TARGET = Path(os.environ.get("SRB_TARGET", "/nonexistent"))

_NODE = os.environ.get("SRB_NODE", "node")
_NPM = os.environ.get("SRB_NPM", "npm")
_DRIVER = Path(__file__).resolve().parent / "js" / "candidate-driver.mjs"

#: How a candidate names its own project, and what a tree-internal path collapses
#: to.  Both are what the driver substitutes; they are here so a candidate can
#: write the string without guessing it.
VPROJ = "/proj"
VTREE = "/tree/<internal>"

_DEFAULT_TIMEOUT = float(os.environ.get("SRB_DRIVER_TIMEOUT", "300"))
_INSTALL_TIMEOUT = float(os.environ.get("SRB_INSTALL_TIMEOUT", "900"))


class BuildFailed(AssertionError):
    """The tree's dependencies would not install.

    An AssertionError so that pytest reports it as a failure rather than an error: a
    candidate that hits this failed against this tree, which is exactly what the
    harness needs to hear.  Whether that is a *finding* is the adjudicator's call,
    and it is not one -- a tree that does not install is stage 2's verdict, scored
    there out of 40.

    That answer is enforced rather than asked for.  lib/srbfault.py, registered by
    run-candidate.sh, turns an uncaught BuildFailed into exit 71, which the
    adjudicator reads as "this tree could not be tested" and answers `invalid` for on
    whichever tree it happened to be.  Telling every round in the prompt is not
    enough on its own: a submission that will not install passes on the original, so
    the round sees a divergence six times and charges up to 60 points for it.

    Catch it if the install itself is genuinely your subject -- srbfault does not
    fire for a BuildFailed you caught, and a candidate that catches it and passes
    has run.
    """


class DriverFailure(RuntimeError):
    """The driver process itself produced nothing readable.

    Distinct from a compile that failed.  A RuntimeError, so it surfaces as an
    error rather than as a verdict about either tree.

    A broken submission does not reach here: candidate-driver.mjs catches a failed
    `import('stylus')` and every op that throws, writes them as `ok:false` results
    and exits 0, so a tree whose entry point is unusable comes back as a Result a
    candidate can assert on.  This is the driver having produced no file at all --
    killed, or a native crash -- and so it is the harness having failed to obtain a
    measurement.  srbfault maps it to exit 70: a fault like BuildFailed's 71, kept a
    different code so the transcript says which of the two happened.
    """


# --------------------------------------------------------------------- results


@dataclass(frozen=True)
class Result:
    """One op's answer.  `ok` false means the compiler raised, which is often the
    interesting outcome rather than a problem -- `error` then carries the shape.
    """

    id: str
    ok: bool
    value: dict | None = None
    error: dict | None = None
    phase: str | None = None

    def _v(self, key, default=None):
        return default if self.value is None else self.value.get(key, default)

    @property
    def css(self) -> str | None:
        """The compiled CSS, or None if the compile raised."""
        return self._v("css")

    @property
    def deps(self) -> list[str]:
        """`@import`ed files, virtualised, in the order the compiler reported."""
        return list(self._v("deps") or ())

    @property
    def sourcemap(self) -> dict | None:
        return self._v("sourcemap")

    @property
    def keys(self) -> list[str]:
        """`api_surface()`: the published names, sorted."""
        return list(self._v("keys") or ())

    @property
    def was_promise(self) -> bool:
        """Whether the synchronous face returned a Promise.

        instruction.md §1.5 requires `stylus(src).render()` to return a String on
        top of a core that is fundamentally async, so this is a real question and
        the driver reports it instead of awaiting it away.
        """
        return bool(self._v("wasPromise"))

    @property
    def sync(self) -> bool:
        """Whether a `render(cb)` callback fired before `render()` returned.

        Measured on the line after the call, not claimed.  Upstream's fires
        synchronously.
        """
        return bool(self._v("sync"))

    @property
    def message(self) -> str:
        """The error's message, or "" when there was no error."""
        return "" if self.error is None else str(self.error.get("message") or "")

    @property
    def name(self) -> str:
        """The error's `.name` -- `ParseError`, `SyntaxError`, `TypeError`."""
        return "" if self.error is None else str(self.error.get("name") or "")

    def __str__(self) -> str:
        if self.ok:
            return f"[{self.id}] ok: {json.dumps(self.value)[:400]}"
        return f"[{self.id}] {self.name}: {self.message[:400]}"


# ------------------------------------------------------------ stream sanitising
#
# Everything below exists for one reason, and it is worth stating plainly because
# the stage is unfair without it.
#
# `bin/stylus` reports a compile failure by throwing from a callback, so Node
# prints an uncaught-exception report: the absolute path and line of the `throw`,
# a source excerpt, a caret, the error, its stack frames, then `Node.js vNN`.
# Measured on State A:
#
#     /<staged tree>/bin/stylus:615
#           if (err) throw err;
#
# Two things in that are not about the compile.  The path names the staged
# directory, which is `SRB_TARGET_TOKEN` and therefore different for the two
# trees by construction.  The frames name internal call structure, which a port
# is free to change and §1.5 in fact requires it to change.  So raw CLI stderr
# differs between the trees for every error, on a correct submission, always --
# a candidate diffing those bytes would break every submission for free and
# collect the whole 60 points for finding nothing.
#
# Both are removed here rather than forbidden in `probe.toml`, because a rule the
# mechanism enforces is a rule nobody has to notice.  What survives is what "the
# error is on stderr" actually asserts: the diagnosis Stylus wrote.

#: An ANSI colour escape.  `--help` is colourised when stderr is a tty and not
#: when it is a pipe; decolourising makes the two comparable.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: The first line of a thrown error, as V8 prints it.  Kept identical to the
#: behavioural stage's pattern (`harness/cli.py`) on purpose: one rule, two stages.
_ERR_LINE = re.compile(r"^\[?((?:[A-Z]\w*)?Error: .*?)\]?\s*\{?$")

#: An `at Parser.error (/path/to/parser.js:252:11)` stack frame.
_STACK_FRAME = re.compile(r"^at\s+\S+\s*\(?.*\)?$")

#: The `/path/to/bin/stylus:615` line that opens the report.
_THROW_SITE = re.compile(r"^\S+:\d+$")


def _diagnostic(stderr: str) -> str:
    """The one line naming the failure, with the Node crash framing removed."""
    for raw in stderr.splitlines():
        line = _ANSI.sub("", raw).strip()
        if not line or line.startswith("Node.js v"):
            if line:
                break
            continue
        m = _ERR_LINE.match(line)
        if m:
            return m.group(1).strip()
    return ""


def _message(stderr: str) -> str:
    """The diagnostic plus Stylus's own explanation under it, as one block."""
    lines: list[str] = []
    started = False
    for raw in stderr.splitlines():
        line = _ANSI.sub("", raw).rstrip()
        stripped = line.strip()
        if not started:
            m = _ERR_LINE.match(stripped)
            if m:
                if stripped.endswith("{"):
                    return ""  # a Node-inspected object, not a Stylus diagnosis
                started = True
                lines.append(m.group(1).strip())
            continue
        if stripped.startswith("Node.js v") or _STACK_FRAME.match(stripped):
            break
        lines.append(line)
    body = "\n".join(lines).strip()
    return body if "\n" in body else ""


def _strip_crash_framing(text: str) -> str:
    """Drop the V8 uncaught-exception framing, keeping the diagnosis.

    Applied only when stderr actually is a crash report -- an error line plus
    either a frame or the version footer.  `--help` also goes to stderr (§1.6)
    and carries neither, so it passes through untouched.
    """
    lines = [_ANSI.sub("", ln) for ln in text.splitlines()]
    has_err = any(_ERR_LINE.match(ln.strip()) for ln in lines)
    has_tail = any(
        ln.strip().startswith("Node.js v") or _STACK_FRAME.match(ln.strip())
        for ln in lines
    )
    if not (has_err and has_tail):
        return text

    kept: list[str] = []
    started = False
    for ln in lines:
        stripped = ln.strip()
        if not started:
            # the throw site, its source excerpt and caret, all before the error
            if _ERR_LINE.match(stripped):
                started = True
                kept.append(ln.rstrip())
            continue
        if stripped.startswith("Node.js v") or _STACK_FRAME.match(stripped):
            break
        kept.append(ln.rstrip())
    out = "\n".join(kept).rstrip()
    return out + "\n" if out else ""


def _virt_bytes(raw: bytes, tree_root: Path, cwd: Path) -> bytes:
    """Replace every path that names where this run happens to live.

    Three substitutions, longest first so a nested root is not half-replaced:
    the tree, the working directory, and -- as the catch-all that makes this
    airtight -- `SRB_TARGET_TOKEN` itself.  The token is the staged directory's
    name (`verification.py` derives both from one `sha256(salt:role)[:16]`), and
    it is the *only* component of any path under `SRB_WORK` that differs between
    the two trees.  So a token that survives nowhere means a leak that survives
    nowhere, whatever shape the path had.
    """
    subs: list[tuple[bytes, bytes]] = []
    for real, virt in ((str(tree_root), VTREE), (str(cwd), VPROJ)):
        if real and real != "/":
            subs.append((real.encode(), virt.encode()))
    token = os.environ.get("SRB_TARGET_TOKEN", "")
    if len(token) >= 8:  # a real token, not a placeholder
        subs.append((token.encode(), b"<token>"))
    out = raw
    for real, virt in sorted(subs, key=lambda p: -len(p[0])):
        out = out.replace(real, virt)
    return out


@dataclass(frozen=True)
class Output:
    """A finished process: `bin/stylus`, or npm.

    stdout and stderr are bytes.  Deliberately: this stage compares compiler
    output byte for byte, and decoding would hide a difference in exactly the
    place one is most likely to hide.  `.text()` decodes stdout when a str is
    what you want.
    """

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self, errors: str = "replace") -> str:
        return self.stdout.decode("utf-8", errors)

    def err_text(self, errors: str = "replace") -> str:
        return self.stderr.decode("utf-8", errors)

    def diagnostic(self) -> str:
        """The one line naming the failure: `ParseError: bad.styl:3:1`, or `""`.

        Same rule as the behavioural stage's `harness.cli.diagnostic`, so a break
        found here becomes a regression module there without being restated.
        """
        return _diagnostic(self.err_text())

    def message(self) -> str:
        """The diagnostic plus Stylus's own explanation under it, or `""`.

        What Stylus says about the failure -- the offending source, the caret,
        `expected ")", got "outdent"`.  This, not the raw stream, is what "the
        error is on stderr" means for comparison purposes: see `cli()`.
        """
        return _message(self.err_text())

    def __str__(self) -> str:
        head = " ".join(self.argv[:6])
        return (
            f"$ {head}\n exit {self.returncode}"
            f"{' (timed out)' if self.timed_out else ''}\n"
            f" stdout: {self.stdout[:800]!r}\n stderr: {self.stderr[:800]!r}"
        )


# --------------------------------------------------------------------- project


@dataclass(frozen=True)
class Project:
    """A directory of stylesheets the candidate wrote, addressed as `/proj`.

    Why a candidate writes its own inputs rather than pointing at the tree's
    `test/` tree: those stylesheets are frozen by instruction.md §3, every one of
    them is already compiled twice and compared by stage 2, and reaching into the
    tree to fetch one would make the input depend on which tree is under test.  A
    project written here is the same bytes for both halves of a comparison.
    """

    #: The real directory.  Pass it as `cwd` to `cli()`; do not put it in an
    #: assertion, because it names where the harness staged this run.
    path: Path

    def file(self, name: str) -> str:
        """The virtual path of one of this project's files, e.g. `/proj/a.styl`."""
        return f"{VPROJ}/{name}"

    def real(self, name: str) -> Path:
        return self.path / name

    def read(self, name: str) -> bytes:
        """A file in the project -- typically one the CLI just wrote."""
        return (self.path / name).read_bytes()

    def exists(self, name: str) -> bool:
        return (self.path / name).exists()

    def listing(self) -> list[str]:
        """Every file in the project, relative and sorted.

        For `-o`/`--out` and the middleware, where what was written and where is
        the question.
        """
        return sorted(
            str(p.relative_to(self.path))
            for p in self.path.rglob("*")
            if p.is_file()
        )


# ------------------------------------------------------------------------ tree


def _clean_env(**extra: str) -> dict[str, str]:
    """A fixed environment for everything this module starts.

    Two jobs.  It pins the things that would otherwise make a comparison
    non-reproducible -- locale, timezone, hash seed, npm's colour and progress
    output.  And it drops every `SRB_*` name except the ones a child genuinely
    needs, so a candidate cannot learn the role by reading an environment its
    parent forgot to clear.  `run-candidate.sh` unsets those too; doing it in both
    places costs nothing and means neither is load-bearing alone.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SRB_")}
    env.update(
        {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(SCRATCH),
            "LC_ALL": "C.UTF-8",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "PYTHONHASHSEED": "0",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "npm_config_color": "false",
            "npm_config_progress": "false",
            "npm_config_fund": "false",
            "npm_config_audit": "false",
            "npm_config_update_notifier": "false",
        }
    )
    # Left out on purpose: DEBUG.  The tree depends on `debug`, which writes to
    # stderr when DEBUG is set, and an inherited value would put the internals of
    # whichever compiler is running into the output being compared.
    env.pop("DEBUG", None)
    env.update(extra)
    return env


def _run(argv: list[str], *, cwd: Path | None = None, stdin: bytes = b"",
         timeout: float = 60.0, env: dict[str, str] | None = None) -> Output:
    """Start a process and come back with an Output, never an exception.

    A timeout is an answer here, not an error: "the original compiles this and the
    submission hangs" is a finding, and it can only be reported if the hang
    returns rather than raises.
    """
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            input=stdin,
            capture_output=True,
            timeout=timeout,
            env=env if env is not None else _clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return Output(
            argv=tuple(argv),
            returncode=124,
            stdout=exc.stdout or b"",
            stderr=exc.stderr or b"",
            timed_out=True,
        )
    except OSError as exc:
        return Output(argv=tuple(argv), returncode=127, stdout=b"", stderr=str(exc).encode())
    return Output(
        argv=tuple(argv), returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
    )


class _lock:
    """A file lock, so two candidates cannot install the same tree at once.

    Candidates within a round are sequential today, but the stage is allowed to
    parallelise and a half-written `node_modules` would be indistinguishable from
    a submission whose lockfile is broken.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "w")

    def __enter__(self):
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()
        return False


@dataclass
class Tree:
    """The tree under test, installed and ready to answer questions.

    Everything here goes through the published package or `bin/stylus`.  There is
    no method that reads a source file, and that is the design: what a source file
    says is stage 1's question and it has both trees and a reviewer for it.
    """

    root: Path
    _consumer: Path
    _cache: dict = field(default_factory=dict, repr=False)

    # --- inputs --------------------------------------------------------------

    def project(self, files: dict[str, str], *, name: str = "proj") -> Project:
        """Write a set of stylesheets and get back a handle addressed as `/proj`.

            p = t.project({"a.styl": "body\\n  color red\\n",
                           "sub/b.styl": "a\\n  b 1px\\n"})
            t.render(file=p.file("a.styl"))

        Subdirectories are created.  Values are text; write bytes yourself under
        `p.path` if a case needs an encoding.
        """
        base = SCRATCH / name
        shutil.rmtree(base, ignore_errors=True)
        for rel, text in files.items():
            dst = base / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
        base.mkdir(parents=True, exist_ok=True)
        return Project(path=base)

    # --- the published JS API ------------------------------------------------

    def ops(self, ops: list[dict], *, project: Project | None = None) -> dict[str, Result]:
        """Run a batch of ops through the published entry point.

        This is the vocabulary stage 2's own suite speaks, deliberately: a
        divergence found here can be handed to that suite as a regression module
        without being translated first.

        Batch.  One call is one Node process, and process startup is around a
        fifth of a second -- a candidate that asks two hundred questions one at a
        time spends most of its wall clock in Node's loader.  Answers are cached
        per batch signature, so asking the same batch twice in one file is free.

        Each op is a dict.  `id` and `kind` are required; the rest depends on the
        kind:

            {"id": "u1", "kind": "render", "source": "a\\n  b 1px\\n"}
            {"id": "u2", "kind": "render", "sourceFile": "/proj/a.styl",
             "set": {"filename": "/proj/a.styl", "compress": True}}

        kinds:
            render          `stylus(src, opts).render()` -- the SYNC face.
                            `.was_promise` says whether it returned a Promise.
            renderAsync     the same, awaited.
            renderCallback  `render(cb)`.  `.sync` says whether the callback fired
                            before render() returned.
            renderTopLevel  `stylus.render(src, opts)`.
            deps            `renderer.deps()`.
            sourcemap       render, then the source map object.
            convertCSS      `stylus.convertCSS(css)` -- needs `css`.
            version         `stylus.version`.
            apiSurface      the published names.
            callable        whether the entry point is callable at all.
            get             `renderer.get(k)` for each of `keys`.
            middleware      `stylus.middleware` driven as Connect drives it.

        modifiers, all optional:
            set            {k: v} -> `renderer.set(k, v)`
            options        the options object handed to the factory
            include        [path] -> `renderer.include(p)`
            import         [path] -> `renderer.import(p)`
            define         {name: value}
            defineRaw      {name: value}, raw
            defineFn       {name: "function(...){...}"} -- source text, compiled
                           by the driver with `stylus` in scope
            defineFnRaw    the same, raw
            use            ["function(style){...}"] -> `renderer.use(fn)`
            defineResolver / defineUrl  install the built-in resolvers
        """
        payload = {
            "treeRoot": str(self.root),
            "projRoot": str(project.path) if project is not None else None,
            "ops": ops,
        }
        sig = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        if sig in self._cache:
            return self._cache[sig]

        job = SCRATCH / f"{sig}.job.json"
        out = SCRATCH / f"{sig}.out.json"
        job.parent.mkdir(parents=True, exist_ok=True)
        job.write_text(json.dumps(payload), encoding="utf-8")
        if out.exists():
            out.unlink()

        proc = _run(
            [_NODE, "--no-warnings", "candidate-driver.mjs", str(job), str(out)],
            cwd=self._consumer,
            timeout=_DEFAULT_TIMEOUT,
        )
        if not out.is_file():
            raise DriverFailure(
                f"the driver produced no output for {len(ops)} op(s)\n{proc}"
            )
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DriverFailure(f"the driver's output was not JSON: {exc}\n{proc}") from None

        results = {
            r["id"]: Result(
                id=r["id"],
                ok=bool(r.get("ok")),
                value=r.get("value"),
                error=r.get("error"),
                phase=r.get("phase"),
            )
            for r in data.get("results", [])
        }
        self._cache[sig] = results
        return results

    def op(self, kind: str, *, project: Project | None = None, **fields) -> Result:
        """One op, by keyword.  A thin wrapper over `ops()` for the common case.

            t.op("render", source="a\\n  b 1px\\n")
            t.op("deps", sourceFile=p.file("a.styl"), set={"filename": ...})
        """
        payload = {"id": kind, "kind": kind}
        payload.update({k: v for k, v in fields.items() if v is not None})
        got = self.ops([payload], project=project)
        if kind not in got:
            raise DriverFailure(f"the driver returned no result for {kind!r}")
        return got[kind]

    # --- conveniences --------------------------------------------------------
    # Named for what they do rather than for the op they send, because a candidate
    # reads better that way.  Every one of them is `op()` underneath, so anything
    # they do not expose is still reachable.

    def render(self, source: str | None = None, **kw) -> Result:
        """The synchronous face: `stylus(src, opts).render()`.

        §1.5 requires this to return a String.  `.was_promise` is how you find out
        whether it did.
        """
        return self.op("render", source=source, **kw)

    def render_async(self, source: str | None = None, **kw) -> Result:
        return self.op("renderAsync", source=source, **kw)

    def render_callback(self, source: str | None = None, **kw) -> Result:
        return self.op("renderCallback", source=source, **kw)

    def render_top_level(self, source: str | None = None, **kw) -> Result:
        return self.op("renderTopLevel", source=source, **kw)

    def deps(self, **kw) -> Result:
        return self.op("deps", **kw)

    def sourcemap(self, **kw) -> Result:
        return self.op("sourcemap", **kw)

    def convert_css(self, css: str) -> Result:
        return self.op("convertCSS", css=css)

    def version(self) -> Result:
        return self.op("version")

    def api_surface(self) -> Result:
        """The published names.

        Five are filtered by the driver -- `length`, `name`, `prototype`,
        `arguments`, `caller`.  The last two exist on a function declared in
        sloppy mode and not on one declared in strict mode, which is to say on
        CommonJS and not on ESM: they differ between the two trees because of the
        module dialect the task REQUIRED, not because of anything the migration
        did, and asserting on them is denied by probe.toml as well.
        """
        return self.op("apiSurface")

    def callable_entry(self) -> Result:
        """Whether the published entry point can be called as a function.

        `stylus('a\\n  color red\\n')` is how every existing consumer starts, and
        §1.5 shows exactly that call, so a non-callable entry point is a real
        finding rather than a dialect artefact.
        """
        return self.op("callable")

    def get(self, keys: list[str], **kw) -> Result:
        return self.op("get", keys=keys, **kw)

    def middleware(self, project: Project, *, url: str = "/style.css", **kw) -> Result:
        """`stylus.middleware` driven the way Connect drives it.

        Named in §1.5's list of things that must keep working, and the one part of
        the published surface no stage-2 module exercises -- that suite only asks
        whether the name exists.  `.css` is what arrived on disk, `.wrote` whether
        anything did.
        """
        return self.op(
            "middleware", project=project, url=url,
            src=VPROJ, dest=VPROJ, **kw,
        )

    # --- the command line ----------------------------------------------------

    def cli(self, *args: str, stdin: bytes | str = b"", cwd: Path | Project | None = None,
            timeout: float = 120.0) -> Output:
        """Run `bin/stylus` with these arguments.

        Reached as the package's own `bin` entry, resolved from `package.json`, so
        the path is not something a candidate names.  §1.6 requires every flag
        State A accepts to keep its meaning and its output.

        stdout is the product and is compared byte for byte.  Both streams have
        run-specific paths replaced (`/tree/<internal>`, `/proj`, `<token>`), and
        stderr additionally has the Node uncaught-exception framing removed --
        the throw site, the source excerpt, the stack frames, the version footer.
        See the sanitising block above for why: that framing differs between the
        two trees on a *correct* submission, so comparing it finds nothing and
        breaks everything.  What is left is the diagnosis, which is what §1.6
        means by "the error is on stderr", and `.diagnostic()` / `.message()`
        name its two useful slices.

        `--help` carries no error line, so it passes through whole; it and
        `--version` contain no paths at all, which is measured, not assumed.
        """
        if isinstance(stdin, str):
            stdin = stdin.encode("utf-8")
        where = cwd.path if isinstance(cwd, Project) else (cwd or SCRATCH)
        got = _run(
            [_NODE, str(self._cli_path())] + list(args),
            cwd=where,
            stdin=stdin,
            timeout=timeout,
        )
        out = _virt_bytes(got.stdout, self.root, where)
        err = _virt_bytes(got.stderr, self.root, where)
        err = _strip_crash_framing(err.decode("utf-8", "replace")).encode("utf-8")
        return Output(
            argv=(_NODE, "<bin/stylus>") + tuple(args),  # the real path is a leak
            returncode=got.returncode,
            stdout=out,
            stderr=err,
            timed_out=got.timed_out,
        )

    def _cli_path(self) -> Path:
        """`bin/stylus`, as `package.json` publishes it.

        Read from the manifest rather than hardcoded, because "the CLI is where
        the package says it is" is part of what a consumer relies on -- and a
        submission that moved it without updating `bin` would produce a confusing
        `MODULE_NOT_FOUND` here instead of the plain failure it deserves.
        """
        manifest = json.loads((self.root / "package.json").read_text(encoding="utf-8"))
        declared = manifest.get("bin")
        if isinstance(declared, str):
            rel = declared
        elif isinstance(declared, dict) and declared:
            rel = declared.get("stylus") or next(iter(declared.values()))
        else:
            raise BuildFailed(
                "package.json declares no `bin`, so there is no command line to "
                "run.  §1.6 requires bin/stylus to still be there."
            )
        path = (self.root / rel).resolve()
        if not path.is_file():
            raise BuildFailed(f"package.json points `bin` at {rel}, which does not exist")
        return path


# --------------------------------------------------------------------- factory

_tree_cache: dict[str, Tree] = {}


def tree(*, timeout: float = _INSTALL_TIMEOUT) -> Tree:
    """The tree under test, with its dependencies installed.

    Installs once per tree and caches, so the first candidate in the stage pays
    and every later one does not.  Raises `BuildFailed` if the tree's own lockfile
    will not install offline -- which is a stage-2 verdict, not a finding here.
    """
    key = str(_TARGET)
    if key in _tree_cache:
        return _tree_cache[key]

    if not (_TARGET / "package.json").is_file():
        raise BuildFailed(f"{_TARGET} has no package.json, so it is not a package")

    SCRATCH.mkdir(parents=True, exist_ok=True)
    with _lock(_STATE / "install.lock"):
        _install(timeout)
        consumer = _consumer_dir()

    t = Tree(root=_TARGET, _consumer=consumer)
    _tree_cache[key] = t
    return t


def _install(timeout: float) -> None:
    """`npm ci --offline` in the tree, once, from the tree's own lockfile.

    `--offline` on purpose: the registry is unreachable in this image, and an
    install that could reach it would let a submission's lockfile pull something
    the benchmark never vetted.  `--ignore-scripts` for the same reason a package
    manager gives you the flag -- a lifecycle script in the tree under test would
    be the submission's code running as the harness.

    A failure raises rather than falling back to `npm install`.  Falling back
    would paper over a lockfile that does not install, which is a real defect in a
    repository and one stage 2 already scores; masking it here would move points
    from a submission that got it right to one that did not.
    """
    marker = _STATE / "installed"
    if marker.is_file():
        return
    if not (_TARGET / "package-lock.json").is_file():
        raise BuildFailed(
            "the tree has no package-lock.json, so there is no offline install. "
            "instruction.md §3 freezes the dependency list and the lockfile with it."
        )
    out = _run(
        [_NPM, "ci", "--offline", "--no-audit", "--no-fund", "--ignore-scripts"],
        cwd=_TARGET,
        timeout=timeout,
    )
    if not out.ok:
        raise BuildFailed(
            "the tree's dependencies would not install offline from its own "
            f"package-lock.json (npm exited {out.returncode}"
            f"{', timed out' if out.timed_out else ''}).\n"
            "Either the lockfile is out of step with package.json, or it asks for "
            "a package this image's cache does not have.\n"
            f"{out.err_text()[-3000:]}"
        )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok\n", encoding="utf-8")


def _consumer_dir() -> Path:
    """A directory that has the tree installed under `node_modules/stylus`.

    This is the whole trick of the stage, and it is worth being explicit about why
    it is a symlink into `node_modules` rather than a path to an entry file.

    `import('stylus')` from here makes Node do full package resolution: it reads
    the tree's `package.json`, honours `exports["."]` when there is one and falls
    back to `main` when there is not, and applies the tree's own `type` to decide
    the dialect.  The original publishes `main: ./index.js` and is CommonJS; a
    submission publishes `exports` -> `src/node/index.js` and is an ES module.
    Both are reached by the same line, with no branch and no path, which is what
    makes the two halves of a comparison indistinguishable to a candidate.

    It also happens to be exactly how a real consumer reaches the package, so what
    this stage measures is the published contract rather than an internal file that
    the task never promised would exist.
    """
    consumer = _STATE / "consumer"
    (consumer / "node_modules").mkdir(parents=True, exist_ok=True)
    link = consumer / "node_modules" / "stylus"
    if link.is_symlink() or link.exists():
        if link.is_symlink() and Path(os.readlink(link)) == _TARGET:
            pass
        else:
            if link.is_symlink() or link.is_file():
                link.unlink()
            else:
                shutil.rmtree(link, ignore_errors=True)
            link.symlink_to(_TARGET)
    else:
        link.symlink_to(_TARGET)

    # The driver is copied rather than symlinked: Node resolves a symlinked module
    # from its realpath by default, which would put the driver outside the
    # consumer directory and break `node_modules` resolution from it.
    shutil.copyfile(_DRIVER, consumer / "candidate-driver.mjs")

    # No package.json here, deliberately.  Without one this directory is CommonJS
    # by default, and `candidate-driver.mjs` is an ES module by its extension --
    # which is what it needs to be, and needs to be identically for both trees.
    return consumer
