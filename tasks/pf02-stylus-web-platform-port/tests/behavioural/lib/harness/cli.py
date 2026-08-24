"""Running `bin/stylus` as a subprocess, on both sides, and comparing what came out.

The CLI is the one part of the port that is still allowed to be Node -- it is the
published release artifact, and instruction.md 1.6 keeps it a `#!/usr/bin/env node`
executable.  What changes underneath is that it becomes a client of `src/node/`
instead of `lib/`.  So this dimension is a black-box comparison: same argv, same
input files, same stdin, and then exit code, stdout, stderr and the files left on
disk all have to agree with State A.

Three things had to be measured before any of it could be written down.

  * **Multi-file stdout order is nondeterministic.**  `stylus --print a b c` compiles
    concurrently and prints as each finishes; five consecutive runs of State A gave
    `a c b`, `a b c`, `a b c`, `a c b`, `a b c`.  So no case here compares multi-file
    stdout as text.  Where several inputs matter, the case writes files (`--out`) and
    compares those, which is order-free.

  * **`--help` goes to stderr, `--version` to stdout.**  Not a symmetry; both are
    exit 0.  A port that sent usage to stdout would look correct to a human.

  * **A compile failure is a raw Node throw.**  Upstream does `if (err) throw err`
    inside a callback, so stderr carries a V8 preamble -- the absolute path of
    `bin/stylus`, a source excerpt, a caret, and a trailing `Node.js v22.x` line --
    wrapped around the Stylus diagnostic.  Those framing lines name where the file
    happens to live and which Node built it, so they differ between the two sides by
    construction and cannot be compared.  `diagnostic()` below pulls out the part
    that is actually about the compile.

    That tolerance is deliberate and it is the right strictness: instruction.md 1.6
    promises "exit status 1 with the error on stderr", not that a port reproduce
    V8's uncaught-exception framing.  A port that catches the error and prints it
    cleanly satisfies the contract, and `diagnostic()` finds its diagnosis either
    way.

  * **`--line-numbers` and `--firebug` annotate the built-in library's real path.**
    Compiling anything pulls in `lib/functions/index.styl`, and its absolute path
    lands in the output -- as a comment for `linenos`, CSS-escaped inside a
    `file:///` URL for `firebug`.  The port keeps its built-in `.styl` somewhere
    else under a possibly different name, so that one path can never match.  It is
    collapsed to a fixed token in both spellings, which leaves the annotations for
    the *project's* files -- the part the flag is actually for -- compared exactly.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import layout

#: Long enough for a cold Node start plus a corpus-sized compile, short enough that
#: a submission whose CLI never exits fails the row instead of hanging the verifier.
TIMEOUT_S = 60

#: Cases are run in a scratch directory whose name appears in output (`linenos`,
#: error messages, `--deps`).  Both sides get a directory with the same *contents*
#: but a different name, so the name is rewritten to this before comparing.
VCWD = "/cwd"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@dataclass(frozen=True)
class CliCase:
    """One CLI invocation, described once and run against both sides."""

    id: str
    argv: tuple[str, ...]
    files: dict[str, str] = field(default_factory=dict)
    #: Binary inputs, for the one thing text cannot express: `--inline` on a real
    #: image, where `image-size` parses the header.  Kept separate from `files` so
    #: the common case stays readable.
    blobs: dict[str, bytes] = field(default_factory=dict)
    stdin: str | None = None
    #: Set when stdout carries progress notices (`  compiled in.css`) rather than
    #: CSS.  Two things follow, both measured rather than assumed:
    #:
    #:   * they are colourised -- `  \x1b[90mcompiled\x1b[0m in.css` -- and colour
    #:     is a terminal decision, not a port decision, so ANSI is stripped;
    #:   * they are **unordered**.  Each is printed from its own completion
    #:     callback, so eight consecutive runs of `stylus --sourcemap in.styl`
    #:     against State A gave `compiled generated` six times and `generated
    #:     compiled` twice.  The lines are therefore compared as a sorted set: the
    #:     content of every notice still has to match exactly, but the sequence
    #:     the event loop happened to produce them in does not.
    progress: bool = False
    #: Files State A leaves behind, measured at build time and written down here.
    #:
    #: Declared rather than read off the oracle at collection time, because the row
    #: count has to be the same whatever happens at verify time -- that is what
    #: `sealed.json` fixes.  Parametrising over what the oracle just produced would
    #: make the denominators depend on the run, and a case whose oracle output
    #: changed would silently lose its rows instead of failing.  A self-check
    #: (`test_declared_writes_match_state_a`) asserts the declaration is still true,
    #: so the two cannot drift unnoticed.
    writes: tuple[str, ...] = ()
    #: Exit status State A returns; nonzero means the diagnostic is compared and
    #: stdout is expected to be empty.
    exit_code: int = 0
    #: Whether State A's stderr carries a Stylus *explanation* under the error line
    #: -- the source excerpt, the caret, and `expected ")", got "outdent"`.  Only
    #: meaningful on a failing case, and false for the two whose error is a bare
    #: Node object that `util.inspect` printed: an ENOENT has an error line and
    #: nothing under it (see `message()`).
    #:
    #: Declared for the same reason as `writes`, and asserted the same way.  A skip
    #: is charged 0 rather than left out of the denominator, so
    #: `test_failure_explanation_matches_state_a` is not collected for the two cases
    #: with no explanation to reproduce -- that would charge every submission for a
    #: check none of them can answer -- and `test_declared_writes_match_state_a`
    #: asserts the declaration is still true.
    #:
    #: The error line itself is still compared, by
    #: `test_failure_diagnostic_matches_state_a`, on every failing case including
    #: these two.  What a port must reproduce there is the ENOENT; what it is not
    #: held to is V8's inspection of the object, which Stylus did not write.
    explains: bool = True
    note: str = ""

    @property
    def fails(self) -> bool:
        return self.exit_code != 0


@dataclass(frozen=True)
class CliResult:
    id: str
    code: int
    stdout: str
    stderr: str
    #: Everything under the scratch directory afterwards that was not an input,
    #: as relative path -> text.  Binary content is recorded as a byte count.
    produced: dict[str, str]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.code == 0 and not self.timed_out


def _devirt_argv(argv, root: Path) -> list[str]:
    """`/cwd`-relative arguments become real ones for the run about to happen."""
    return [a.replace(VCWD, str(root)) for a in argv]


#: What a path inside either implementation tree is rewritten to.  Both sides get
#: the same token, because which tree served the built-in library is exactly the
#: thing that is allowed to differ.
VTREE = "/-tree-"

#: `--firebug` escapes exactly `[.:/\\]`, per `compiler.js debugInfo()`, with a
#: backslash escaping a backslash into a forward slash.  Absolute paths therefore
#: have to be collapsed in two spellings, and the set has to match upstream's --
#: escaping one character too many silently stops the substitution from matching.
def _css_escape(p: str) -> str:
    return re.sub(r"([.:/\\])", lambda m: "\\" + ("/" if m.group(1) == "\\" else m.group(1)), p)


#: A `linenos` comment or a `firebug` block naming a file inside an implementation
#: tree.  Only the built-in `.styl` library can be in there, and instruction.md 1.4
#: leaves its filename and directory to the port ("ship the .styl sources under
#: src/core/"), so the path is collapsed to a single token while the line number --
#: which is fixed by the library's contents -- stays.
_BUILTIN_LINENO = re.compile(
    r"/\* line (\d+) : " + re.escape(VTREE) + r"\S* \*/")
_BUILTIN_FIREBUG = re.compile(
    r"filename\{font-family:file(?:\\.)*" + re.escape(_css_escape(VTREE)) + r"[^}]*\}")


def _collapse_builtin(text: str) -> str:
    text = _BUILTIN_LINENO.sub(r"/* line \1 : <runtime> */", text)
    return _BUILTIN_FIREBUG.sub("filename{font-family:<runtime>}", text)


def _revirt(text: str, root: Path, bin_path: Path) -> str:
    """Rewrite everything that names *this* run's locations back to a fixed spelling.

    Two classes of substitution, for two different reasons.

    The scratch directory is rewritten because it is a fresh mkdtemp per side: the
    two runs see the same file contents at different paths, and `linenos`, `firebug`
    and error messages all quote the path.  After this, `/cwd/in.styl` on both
    sides, so the annotation itself is still compared exactly.

    The implementation trees -- the oracle's and the submission's -- are collapsed
    entirely, because compiling anything imports the built-in `.styl` library and
    its absolute path lands in `linenos`/`firebug` output.  Upstream that file is
    `lib/functions/index.styl`; instruction.md 1.4 only says to ship the `.styl`
    sources somewhere under `src/core/` and point `runtimeRoot` at them, so neither
    the directory nor the filename is pinned.  Comparing it would fail every
    submission for having reorganised the tree, which is the thing the task asks
    for.  The annotations for the *project's* files -- what the flag is for -- are
    left exact.
    """
    out = text
    for real, virt in ((str(root), VCWD), (str(root.resolve()), VCWD),
                       (str(layout.STATE_A), VTREE), (str(layout.REPO), VTREE)):
        if not real or real == "/":
            continue
        out = out.replace(real, virt)
        out = out.replace(_css_escape(real), _css_escape(virt))
    return _collapse_builtin(out)


#: The first line of a thrown error: `ParseError: broken.styl:3:1`, `Error:
#: badextend.styl:2:12`, or `[Error: ENOENT: ..., stat 'nope.styl'] {` when Node
#: inspects an object with extra properties.
_ERR_LINE = re.compile(r"^\[?((?:[A-Z]\w*)?Error: .*?)\]?\s*\{?$")

#: A `    at Parser.error (/path/to/parser.js:252:11)` stack frame.  Dropped: it
#: names internal call structure, which a port is free to change.
_STACK_FRAME = re.compile(r"^at\s+\S+\s*\(?.*\)?$")


def diagnostic(stderr: str) -> str:
    """The one line naming the failure, with the Node crash framing removed.

    Upstream fails by throwing from a callback, so stderr is a V8 uncaught-exception
    report: the path and line of the `throw`, a source excerpt, a caret, a blank
    line, the error, its stack, then `Node.js vNN`.  Only the error line is about
    the compile; the framing names the interpreter version and where in the
    filesystem `bin/stylus` sits, both of which differ between the two sides for
    reasons a port does not control.

    Returns e.g. `ParseError: broken.styl:3:1`, or `""` if stderr carries no error.
    """
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


def message(stderr: str) -> str:
    """The diagnostic plus Stylus's own explanation under it, as one block.

    A `ParseError` prints the offending source, a caret, and then what it wanted --
    `expected ")", got "outdent"`.  That text is the diagnosis proper, and a port
    that gets the position right while saying something else about why has not
    reproduced the error.

    Returns `""` when there is no Stylus explanation to compare.  Two cases:
    stderr carries no error at all, or the error is a plain Node object that V8
    inspected -- `[Error: ENOENT: ...] {` followed by `errno:`, `code:`, `syscall:`.
    That trailing block is `util.inspect` output, not something Stylus wrote, so a
    port that catches the ENOENT and reports it cleanly is not wrong; the error
    line itself, which `diagnostic()` returns, is the part that must match.
    """
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


def _as_notice_set(text: str) -> str:
    """Progress notices, decolourised and sorted, one per line.

    Sorting is what makes a `--out`/`--sourcemap` case comparable at all -- see
    `CliCase.progress` for the eight-run measurement that forced it.  Nothing is
    dropped: an extra notice, a missing one, or one naming the wrong file still
    shows up as a difference.
    """
    lines = [_ANSI.sub("", ln).rstrip() for ln in text.splitlines()]
    return "\n".join(sorted(ln for ln in lines if ln.strip()))


def _snapshot(root: Path, inputs: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        if rel in inputs:
            continue
        try:
            out[rel] = p.read_text(encoding="utf8")
        except UnicodeDecodeError:
            out[rel] = f"<binary {p.stat().st_size} bytes>"
    return out


def run_case(case: CliCase, bin_path: Path, workdir: Path) -> CliResult:
    """Run one case in a fresh directory under `workdir` and collect everything."""
    root = workdir / case.id.replace("/", "_")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for name, text in case.files.items():
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf8")
    for name, data in case.blobs.items():
        dest = root / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    inputs = set(case.files) | set(case.blobs)

    # A clean, minimal environment: no inherited NODE_OPTIONS or NODE_PATH, and
    # FORCE_COLOR pinned off so the two sides agree on whether to emit ANSI.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(root),
        "NO_COLOR": "1",
        "FORCE_COLOR": "0",
        "TERM": "dumb",
    }

    try:
        proc = subprocess.run(
            [layout.NODE, str(bin_path), *_devirt_argv(case.argv, root)],
            cwd=root, env=env, input=case.stdin or "",
            capture_output=True, text=True, timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        return CliResult(case.id, -1,
                         _revirt(e.stdout or "", root, bin_path) if isinstance(e.stdout, str) else "",
                         f"timed out after {TIMEOUT_S}s", {}, timed_out=True)

    out = _revirt(proc.stdout, root, bin_path)
    err = _revirt(proc.stderr, root, bin_path)
    if case.progress:
        out = _as_notice_set(out)
    produced = {k: _revirt(v, root, bin_path) for k, v in _snapshot(root, inputs).items()}
    return CliResult(case.id, proc.returncode, out, err, produced)


class Batch:
    """The results of running every case against one side."""

    def __init__(self, results: dict[str, CliResult]):
        self._r = results

    def get(self, cid: str) -> CliResult:
        if cid not in self._r:
            raise KeyError(f"no CLI result for {cid!r}")
        return self._r[cid]

    def __contains__(self, cid: str) -> bool:
        return cid in self._r


def run_all(cases, bin_path: Path, label: str) -> Batch:
    root = layout.WORK / f"cli-{label}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    return Batch({c.id: run_case(c, bin_path, root) for c in cases})


def oracle_bin() -> Path:
    return layout.STATE_A / "bin" / "stylus"


def submission_bin() -> Path:
    return layout.CLI


# ------------------------------------------------------------------ the catalog
#
# Inputs are kept small and deliberately boring.  This dimension is about argument
# handling and process behaviour; whether the compiler gets `@extend` right is the
# corpus dimensions' job, and a complicated stylesheet here would only mean a
# compiler bug failed the CLI rows too.
#
# Four flags are excluded, each for a reason that would make the row untestable
# rather than merely inconvenient:
#
#   -w/--watch      never exits; there is nothing to compare.
#   -i/--interactive  reads a REPL from a TTY.
#   --use           loads a JS plugin from disk, so it asserts the plugin API
#                   through a second, indirect path (test_jsapi covers it directly).
#   help <prop>     opens a browser.  Bare `help` throws a TypeError upstream --
#                   a real upstream bug, and not one the port is asked to keep.

#: A 2x1 8-bit RGB PNG, built here rather than shipped as a fixture so its bytes
#: are visible and its dimensions are known.  `--inline` reads the IHDR for the
#: `width`/`height` that `image-size` reports, so the header has to be real.
def _png_2x1() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)  # 2x1, truecolour
    raw = b"\x00\xff\x00\x00\x00\x00\xff"                # filter 0, red, blue
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG_2X1 = _png_2x1()

_BASIC = ".a\n  color: red\n"
_NESTED = ".a\n  color: red\n  .b\n    padding: 1px 2px\n"
_EXTEND = ".a\n  color: red\n.b\n  @extend .a\n"
_VARS = "$c = #00f\n"
_USES = "@import 'vars'\n.c\n  color: $c\n"
_BROKEN = ".bad\n  color: rgba(\n"
_BAD_EXTEND = ".x\n  @extend .nothing\n"
_MEDIA = "@media screen\n  .a\n    color: red\n"

#: A stylesheet whose compressed form differs from its expanded one in more than
#: whitespace: hex shortening and the dropped final semicolon.
_COMPRESSIBLE = ".a\n  color: #ff0000\n  margin: 0px\n"


def _c(cid, argv, **kw) -> CliCase:
    return CliCase(id=cid, argv=tuple(argv), **kw)


def _catalog() -> list[CliCase]:
    one = {"in.styl": _BASIC}
    nested = {"in.styl": _NESTED}
    cases: list[CliCase] = [
        # --- printing to stdout ------------------------------------------------
        _c("print-basic", ["--print", "in.styl"], files=one,
           note="the shortest path through the whole program"),
        _c("print-short-flag", ["-p", "in.styl"], files=one,
           note="short and long spellings must not diverge"),
        _c("print-nested", ["--print", "in.styl"], files=nested),
        _c("print-two-inputs-to-files", ["a.styl", "b.styl"],
           files={"a.styl": ".a\n  color: red\n", "b.styl": ".b\n  color: blue\n"},
           progress=True,
           note="two inputs, compared as written files: stdout order is racy",
                      writes=('a.css', 'b.css')),

        # --- stdin -------------------------------------------------------------
        _c("stdin-basic", [], stdin=_BASIC,
           note="no arguments means stdin to stdout"),
        _c("stdin-compress", ["--compress"], stdin=_COMPRESSIBLE),
        _c("stdin-empty", [], stdin="",
           note="empty input is not an error"),
        _c("error-stdin", [], stdin=_BROKEN,
           note="a failure from stdin still exits nonzero",
                      exit_code=1),
        _c("stdin-with-print", ["--print"], stdin=_BASIC,
           note="--print is already implied; asking twice must not double the output"),

        # --- writing files -----------------------------------------------------
        _c("write-default", ["in.styl"], files=one, progress=True,
           note="in.styl -> in.css, next to the input",
                      writes=('in.css',)),
        _c("write-ext", ["--ext", ".out", "in.styl"], files=one, progress=True,
                   writes=('in.out',)),
        _c("write-out-dir", ["--out", "od", "in.styl"], files=one, progress=True,
                   writes=('od/in.css',)),
        _c("write-out-dir-short", ["-o", "od", "in.styl"], files=one, progress=True,
                   writes=('od/in.css',)),
        _c("write-out-dir-nested", ["--out", "od/deeper", "in.styl"], files=one,
           progress=True, note="a missing output directory is created",
                      writes=('od/deeper/in.css',)),
        _c("write-input-in-subdir", ["--out", ".", "sub/in.styl"],
           files={"sub/in.styl": _BASIC}, progress=True,
                      writes=('in.css',)),

        # --- compression -------------------------------------------------------
        _c("compress-print", ["--compress", "--print", "in.styl"],
           files={"in.styl": _COMPRESSIBLE}),
        _c("compress-short", ["-c", "-p", "in.styl"], files={"in.styl": _COMPRESSIBLE}),
        _c("compress-write", ["-c", "in.styl"], files={"in.styl": _COMPRESSIBLE},
           progress=True,
                      writes=('in.css',)),
        _c("compress-media", ["-c", "-p", "in.styl"], files={"in.styl": _MEDIA}),

        # --- imports and the include path --------------------------------------
        _c("import-relative", ["--print", "uses.styl"],
           files={"uses.styl": _USES, "vars.styl": _VARS}),
        _c("include-dir", ["-I", "inc", "--print", "uses.styl"],
           files={"uses.styl": _USES, "inc/vars.styl": _VARS},
           note="-I is how the import resolver is reached from outside"),
        _c("include-dir-long", ["--include", "inc", "--print", "uses.styl"],
           files={"uses.styl": _USES, "inc/vars.styl": _VARS}),
        _c("include-two-dirs-first-wins", ["-I", "one", "-I", "two", "--print", "uses.styl"],
           files={"uses.styl": _USES, "one/vars.styl": "$c = #111\n",
                  "two/vars.styl": "$c = #222\n"},
           note="both paths hold the file; which one answers is the whole question"),
        _c("include-index-styl", ["--print", "uses.styl"],
           files={"uses.styl": "@import 'pkg'\n.c\n  color: $c\n",
                  "pkg/index.styl": _VARS},
           note="a directory import resolves through index.styl"),
        _c("error-import-missing", ["--print", "uses.styl"],
           files={"uses.styl": _USES},
           note="the imported file is absent",
                      exit_code=1),
        _c("import-glob", ["--print", "uses.styl"],
           files={"uses.styl": "@import 'parts/*'\n",
                  "parts/a.styl": ".a\n  color: red\n",
                  "parts/b.styl": ".b\n  color: blue\n"},
           note="glob results must be ordered, or this output is racy"),

        # --- css handling ------------------------------------------------------
        _c("include-css", ["--include-css", "--print", "in.styl"],
           files={"in.styl": "@import 'plain.css'\n", "plain.css": ".x { color: red; }\n"}),
        _c("css-import-untouched", ["--print", "in.styl"],
           files={"in.styl": "@import 'plain.css'\n", "plain.css": ".x { color: red; }\n"},
           note="without --include-css the @import is left for the browser"),
        _c("css-to-styl", ["--css", "plain.css"],
           files={"plain.css": ".x { color: red; }\n"}, progress=True,
           note="the CSS->Stylus converter; writes plain.styl, prints nothing",
                      writes=('plain.styl',)),
        _c("css-to-styl-short", ["-C", "plain.css"],
           files={"plain.css": ".x { color: red; }\n"}, progress=True,
                      writes=('plain.styl',)),
        _c("css-to-styl-out", ["--css", "plain.css", "conv.styl"],
           files={"plain.css": ".x { color: red; }\n"}, progress=True,
                      writes=('conv.styl',)),
        _c("css-to-styl-rich", ["--css", "rich.css"],
           files={"rich.css": "@media screen {\n  .a, .b { color: red; margin: 0 }\n}\n"},
           progress=True, note="nesting and media, so the converter does real work",
                      writes=('rich.styl',)),

        # --- url handling ------------------------------------------------------
        _c("resolve-url", ["--resolve-url", "--print", "top.styl"],
           files={"top.styl": "@import 'sub/u'\n",
                  "sub/u.styl": ".u\n  background: url('img.png')\n"},
           note="the url is rewritten relative to the entry file, not the importer"),
        _c("resolve-url-nocheck", ["--resolve-url-nocheck", "--print", "top.styl"],
           files={"top.styl": "@import 'sub/u'\n",
                  "sub/u.styl": ".u\n  background: url('missing.png')\n"}),
        _c("inline-images", ["--inline", "--print", "in.styl"],
           files={"in.styl": ".i\n  background: url('t.svg')\n",
                  "t.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>\n"},
           note="-U base64-encodes the file, so it exercises binary reading"),
        _c("inline-images-short", ["-U", "--print", "in.styl"],
           files={"in.styl": ".i\n  background: url('t.svg')\n",
                  "t.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>\n"}),
        _c("inline-images-png", ["--inline", "--print", "in.styl"],
           files={"in.styl": ".i\n  background: url('p.png')\n"},
           blobs={"p.png": PNG_2X1},
           note="a real PNG: the base64 in the output is the file's actual bytes, "
                "so this is the row that catches a broken binary read path"),
        _c("image-size-builtin", ["--print", "in.styl"],
           files={"in.styl": ".i\n  width: image-size('p.png')[0]\n"},
           blobs={"p.png": PNG_2X1},
           note="image-size parses the IHDR; 2px is the answer"),

        # --- other output-shaping flags ----------------------------------------
        _c("hoist-atrules", ["--hoist-atrules", "--print", "in.styl"],
           files={"in.styl": ".a\n  color: red\n@charset \"utf-8\"\n"}),
        _c("prefix", ["--prefix", "pfx-", "--print", "in.styl"], files={"in.styl": _EXTEND}),
        _c("prefix-short", ["-P", "pfx-", "--print", "in.styl"], files={"in.styl": _EXTEND}),
        _c("prefix-greedy", ["--print", "--prefix", "in.styl"], files=one,
           note="upstream declares --prefix as taking an optional value and then "
                "consumes the next argument regardless -- so this prefixes with "
                "'in.styl' and reads no file.  A faithful port keeps the quirk"),

        # --- the remaining documented flags ------------------------------------
        _c("compare-stdin", ["-d"], stdin=_BASIC,
           note="-d/--compare echoes the input above the output, in bold, and only "
                "on the stdio path -- `if (compare)` appears solely inside "
                "compileStdio, so it does nothing for a named file"),
        _c("compare-with-file-is-inert", ["-d", "--print", "in.styl"], files=one,
           note="the same flag with a file argument: accepted, no effect"),
        _c("quiet-write", ["-q", "in.styl"], files=one, progress=True,
           note="-q only gates the `watching` notice inside watch(), so with no "
                "--watch it is accepted and changes nothing.  Kept because a port "
                "that rejects the flag, or lets it suppress `compiled`, diverges",
                           writes=('in.css',)),
        _c("import-flag", ["--import", "vars.styl", "--print", "in.styl"],
           files={"in.styl": ".c\n  color: $c\n", "vars.styl": _VARS},
           note="--import injects a stylesheet ahead of the entry file"),
        _c("error-import-flag-missing", ["--import", "nope.styl", "--print", "in.styl"],
           files=one,
                      exit_code=1),
        _c("disable-cache", ["--disable-cache", "--print", "uses.styl"],
           files={"uses.styl": _USES, "vars.styl": _VARS},
           note="turns off the import cache; the CSS is unchanged either way"),
        _c("ext-without-dot", ["--ext", "out", "in.styl"], files=one, progress=True,
           note="the flag takes whatever string it is given",
                      writes=('inout',)),
        _c("linenos", ["--line-numbers", "--print", "in.styl"], files=nested,
           note="annotates the project file's path and line; the built-in "
                "library's own path is collapsed, since 1.4 does not pin it"),
        _c("linenos-short", ["-l", "--print", "in.styl"], files=nested),
        _c("firebug", ["--firebug", "--print", "in.styl"], files=nested),
        _c("firebug-short", ["-f", "--print", "in.styl"], files=nested),
        _c("both-debug-flags", ["-l", "-f", "--print", "in.styl"], files=nested),

        # --- source maps -------------------------------------------------------
        _c("sourcemap-print", ["--sourcemap", "--print", "in.styl"], files=one,
           note="the map comment is appended even when the CSS goes to stdout",
                      writes=('in.css.map',)),
        _c("sourcemap-write", ["--sourcemap", "in.styl"], files=one, progress=True,
           note="in.css and in.css.map, both compared as text",
                      writes=('in.css', 'in.css.map')),
        _c("sourcemap-short", ["-m", "in.styl"], files=one, progress=True,
                   writes=('in.css', 'in.css.map')),
        _c("sourcemap-inline", ["--sourcemap-inline", "--print", "in.styl"], files=one,
           note="a base64 data URI, so the whole map is inside stdout"),
        _c("sourcemap-root-clobbered", ["--sourcemap-root", "/src", "--sourcemap", "in.styl"],
           files=one, progress=True,
           note="argument order matters, because `case '--sourcemap'` assigns a "
                "fresh object: given this order the sourceRoot is silently lost, "
                "and the map has no sourceRoot key",
                           writes=('in.css', 'in.css.map')),
        _c("sourcemap-root-kept", ["--sourcemap", "--sourcemap-root", "/src", "in.styl"],
           files=one, progress=True,
           note="the other order keeps it -- so a port that tidied the parser into "
                "order-independence fails the row above, correctly: that is a "
                "behaviour change on the published CLI",
                           writes=('in.css', 'in.css.map')),
        _c("sourcemap-basepath", ["--sourcemap", "--sourcemap-base", ".", "in.styl"],
           files=one, progress=True,
                      writes=('in.css', 'in.css.map')),
        _c("sourcemap-stdin-ignored", ["--sourcemap"], stdin=_BASIC,
           note="`if (sourcemap && !files.length) sourcemap = false` -- no map "
                "comment at all when reading stdin"),
        _c("sourcemap-inline-stdin", ["--sourcemap-inline"], stdin=_BASIC,
           note="dropped by the same guard, since --sourcemap-inline sets the "
                "same variable"),
        _c("sourcemap-with-import", ["--sourcemap", "uses.styl"],
           files={"uses.styl": _USES, "vars.styl": _VARS}, progress=True,
           note="two sources in the map, so `sources` ordering is load-bearing",
                      writes=('uses.css', 'uses.css.map')),
        _c("sourcemap-compress", ["--sourcemap", "--compress", "in.styl"],
           files={"in.styl": _NESTED}, progress=True,
                      writes=('in.css', 'in.css.map')),

        # --- dependency listing ------------------------------------------------
        _c("deps", ["--deps", "uses.styl"],
           files={"uses.styl": _USES, "vars.styl": _VARS}),
        _c("deps-short", ["-D", "uses.styl"],
           files={"uses.styl": _USES, "vars.styl": _VARS}),
        _c("deps-nested", ["--deps", "top.styl"],
           files={"top.styl": "@import 'mid'\n", "mid.styl": "@import 'vars'\n",
                  "vars.styl": _VARS},
           note="transitively, and in resolution order"),
        _c("deps-none", ["--deps", "in.styl"], files=one),

        # --- informational -----------------------------------------------------
        _c("version", ["--version"], note="0.63.0, on stdout, exit 0"),
        _c("version-short", ["-V"]),
        _c("help", ["--help"], note="usage on stderr -- not stdout -- and exit 0"),
        _c("help-short", ["-h"]),

        # --- failures ----------------------------------------------------------
        _c("error-parse", ["--print", "broken.styl"], files={"broken.styl": _BROKEN},
           note="exit 1, diagnosis on stderr",
                      exit_code=1),
        _c("error-parse-write", ["broken.styl"], files={"broken.styl": _BROKEN},
           progress=True, note="and nothing is written when the compile fails",
                      exit_code=1),
        _c("error-bad-extend", ["--print", "in.styl"], files={"in.styl": _BAD_EXTEND},
                   exit_code=1),
        _c("error-mixin-arity", ["--print", "in.styl"],
           files={"in.styl": "m(a, b)\n  x: a b\n.a\n  m(1)\n"},
                      exit_code=1),
        _c("error-require-missing", ["--print", "in.styl"],
           files={"in.styl": "@require 'gone'\n"},
                      exit_code=1),
        _c("error-empty-selector", ["--print", "in.styl"], files={"in.styl": "{\n"},
                   exit_code=1),
        _c("error-bad-unit-arg", ["--print", "in.styl"],
           files={"in.styl": ".a\n  color: unit(red, \"px\")\n"},
           note="a built-in given the wrong type: an eval error, not a parse one",
                      exit_code=1),

        # --- things that look like errors and are not ---------------------------
        #
        # Each of these compiles in State A, so a port that added validation the
        # original did not have fails the row.  Being right about what upstream
        # *accepts* is as much of the contract as being right about what it rejects.
        _c("passthru-unknown-fn", ["--print", "in.styl"],
           files={"in.styl": ".a\n  color: nosuchfn(1)\n"},
           note="an unknown call is emitted as literal CSS -- CSS has functions "
                "Stylus does not know about"),
        _c("passthru-undefined-var", ["--print", "in.styl"],
           files={"in.styl": ".a\n  color: $nope\n"}),
        _c("passthru-mixed-units", ["--print", "in.styl"],
           files={"in.styl": ".a\n  width: 1px + red\n"}),
        _c("passthru-divide-by-zero", ["--print", "in.styl"],
           files={"in.styl": ".a\n  width: 1px / 0\n"}),
        _c("passthru-property-no-value", ["--print", "in.styl"],
           files={"in.styl": ".a\n  color\n"},
           note="a property with no value is silently dropped"),
        _c("error-missing-input", ["--print", "nope.styl"],
           note="the input does not exist; upstream surfaces the raw ENOENT, so "
                "there is an error line and no Stylus explanation under it",
                      exit_code=1, explains=False),
        _c("error-unknown-flag", ["--definitely-not-a-flag", "--print", "in.styl"],
           files=one,
           note="upstream has no unknown-option check: it becomes a filename, and "
                "the failure is that filename's ENOENT",
                      exit_code=1, explains=False),
        _c("directory-as-input", ["adir"],
           files={"adir/a.styl": ".a\n  color: red\n",
                  "adir/b.styl": ".b\n  color: blue\n",
                  "adir/skipme.txt": "not stylus\n"},
           progress=True,
           note="a directory argument compiles the .styl files inside it and "
                "ignores everything else; readdir order is unsorted upstream, "
                "which is why this is compared as written files",
                           writes=('adir/a.css', 'adir/b.css')),
        _c("directory-with-print", ["--print", "adir"],
           files={"adir/only.styl": _BASIC},
           note="one file inside, so --print is deterministic here"),
    ]
    seen = set()
    for c in cases:
        if c.id in seen:
            raise AssertionError(f"duplicate CLI case id {c.id!r}")
        seen.add(c.id)
    return cases


CASES: list[CliCase] = _catalog()
IDS: list[str] = [c.id for c in CASES]


def by_id(cid: str) -> CliCase:
    for c in CASES:
        if c.id == cid:
            return c
    raise KeyError(cid)


def describe() -> dict[str, str]:
    out = {}
    for c in CASES:
        argv = " ".join(c.argv) or "(no arguments)"
        line = f"stylus {argv}"
        if c.stdin is not None:
            line += "  <stdin>"
        if c.note:
            line += f"   -- {c.note}"
        out[c.id] = line
    return out
