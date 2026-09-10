"""What a stage-3 candidate is given to attack a ported QuickJS.

A candidate is a pytest file.  It may import this module and the standard library,
and nothing else.  Everything here runs a *built* artifact: a migration is graded
on what it computes, so there is deliberately no way from here to read the tree
under test, inspect a build log, or look at a source file.

The tree under test arrives built for three targets, with the argv instruction.md
publishes:

    x86_64   native, and the platform the original already worked on
    s390x    cross, big-endian, and the platform the port is about
    armhf    cross, little-endian, 32-bit -- a second little-endian target, which
             is how a change that is really about word size gets told apart from
             one that is about byte order

Four ways in:

    run_js(...)        a source string on one target's interpreter
    compiled(...)      a source string through the blob boundary: this build's
                       host compiler writes the bytecode, this build's target
                       interpreter reads it.  The mechanism of the whole task
    artifact(...)      one of the binaries the build produced, run as shipped
    CProgram(...)      a C program of your own, linked against libquickjs.a for a
                       target -- the only route to the C API

and one reference:

    reference(...)     the pristine original, built for x86-64 at image build
                       time.  The ground truth for any expression's value

WHY reference() TAKES NO TARGET

Because the original is WRONG on s390x.  That is the defect this task exists to
remove.  A claim of the form "the tree under test agrees with the original on
s390x" therefore passes on the original and fails on every correct submission --
it satisfies every mechanical condition for a break while asserting the bug -- so
there is no supported way to ask for it.  x86-64 is where the original is right,
and it is the only place its answers are evidence.

Two sound shapes, and everything here is built to make them the easy ones:

    the specification fixes this answer, so all three targets must agree
    the original computes this on x86-64, so the submission must too

WHAT IS DELIBERATELY ABSENT

No access to the bytes of a compiled blob.  A port may legally fix the transport
by making the writer emit the target's order, or by making the reader accept
either -- those ship different bytes and both are correct.  What is answerable is
whether the binary runs and computes the right answer, which is what `compiled`
and `artifact` do.

No access to the tree's sources from inside a test.  Read them as much as you like
while writing the candidate; a test that reads them is testing the implementation
rather than what it computes, and is out of scope.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "TARGETS", "BIG_ENDIAN", "WORD_BITS", "TARGET_TOKEN", "SCRATCH",
    "SUITES", "SUITE_FLAGS", "ARTIFACTS",
    "Output", "run_js", "eval_js", "run_all", "reference", "reference_eval",
    "compiled", "target_compiled", "artifact", "suite", "reference_suite",
    "bjson", "CProgram", "agree", "tree_for",
]

#: The three targets the tree under test is built for.
TARGETS = ("x86_64", "s390x", "armhf")

#: Facts about each ABI, from the target triple and never from the tree.  s390x is
#: big-endian whatever any code in the tree believes.
BIG_ENDIAN = {"x86_64": False, "s390x": True, "armhf": False}
WORD_BITS = {"x86_64": 64, "s390x": 64, "armhf": 32}

_CROSS_PREFIX = {"x86_64": "", "s390x": "s390x-linux-gnu-",
                 "armhf": "arm-linux-gnueabihf-"}
_EMULATOR = {
    "x86_64": [],
    "s390x": ["qemu-s390x-static", "-L", "/usr/s390x-linux-gnu"],
    "armhf": ["qemu-arm-static", "-L", "/usr/arm-linux-gnueabihf"],
}
#: qemu costs roughly an order of magnitude.  Give an emulated run room without
#: turning a hang into a stage timeout; a candidate wanting to assert a hang should
#: pass an explicit, generous timeout and check `timed_out`.
_DEFAULT_TIMEOUT = {"x86_64": 120, "s390x": 600, "armhf": 600}

#: The test files the repository ships, as the Makefile's `test` target names them:
#: relative to the tree root, run from the tree root.  `suite()` will run any path
#: in the tree; these are the ones that exist.
#:
#: Two things worth knowing before spending a candidate here.  Upstream's suites
#: were written on x86-64 and never run anywhere else, so they are byte-order
#: silent almost everywhere -- a failure in one is interesting and a pass proves
#: less than it looks.  And two of them fail in BOTH trees for reasons that have
#: nothing to do with the port: test_std.js fails under a pipe at `os.isatty(0)`,
#: and test_builtin.js fails on the big-endian target at an upstream assertion with
#: a little-endian byte string written into it.  A candidate asserting either
#: passes will fail on the original and be discarded.
SUITES = ("tests/test_bjson.js", "tests/test_builtin.js",
          "tests/test_closure.js", "tests/test_loop.js",
          "tests/test_language.js", "tests/test_std.js",
          "tests/test_qjscalc.js", "tests/test_bignum.js",
          "tests/test_op_overloading.js", "tests/test_worker.js",
          "examples/test_point.js", "tests/microbench.js")

#: The flags upstream's `test` target passes.  A suite run without them fails for a
#: reason that is not about the tree.
SUITE_FLAGS = {
    "tests/test_qjscalc.js": ("--qjscalc",),
    "tests/test_bignum.js": ("--bignum",),
    "tests/test_op_overloading.js": ("--bignum",),
    "tests/test_bjson.js": ("--bignum",),
}

#: Binaries the published build produces, runnable through `artifact()`.
ARTIFACTS = ("qjs", "qjsc", "examples/hello", "examples/test_fib")


def _env_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"{name} is not set.  This module only works inside a stage-3 "
            f"candidate run; run-candidate.sh sets it."
        )
    return Path(value)


_TREES = _env_path("SRB_TREES")
_REFERENCE = _env_path("SRB_REFERENCE")

#: An opaque per-run label for the tree under test, for diagnostics.  It is a hash
#: rather than "original" or "submission" because `assert TARGET_TOKEN ==
#: "original"` would be a break that meets every mechanical condition -- passes on
#: one tree, fails on the other, reproduces exactly -- and establishes nothing.
TARGET_TOKEN = os.environ.get("SRB_TARGET_TOKEN", "?")

#: Scratch for generated sources and compiled helpers.  Emptied between
#: (candidate, tree) pairs, so nothing a candidate leaves behind can change the
#: answer the other half of its own comparison gets.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))


def _check_target(target: str) -> str:
    if target not in TARGETS:
        raise ValueError(f"unknown target {target!r}; expected one of {TARGETS}")
    return target


def tree_for(target: str) -> Path:
    """The built tree for one target.

    Exposed for diagnostics and for `CProgram`, which needs the include path and
    the static library.  Reading source files under it from a test is out of
    scope -- see the module docstring.
    """
    return _TREES / _check_target(target)


# --------------------------------------------------------------------------- #
# Running things
# --------------------------------------------------------------------------- #

@dataclass
class Output:
    """What one run produced.  Never raises on a non-zero exit.

    That is deliberate: "the interpreter crashed", "it exited 1", "it hung" are
    all findings on this task -- a misread length is far more likely to abort than
    to print a wrong number -- so they have to be things a candidate can assert
    rather than exceptions that abort the candidate.
    """

    argv: list[str] = field(default_factory=list)
    returncode: int = 0
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def text(self) -> str:
        """stdout as text, for the common case of a program that prints values."""
        return self.stdout.decode("utf-8", "replace")

    def __str__(self) -> str:
        head = "timed out" if self.timed_out else f"exit {self.returncode}"
        return (f"<{head} after {self.seconds:.1f}s: {shlex.join(self.argv)}\n"
                f"--- stdout ---\n{self.stdout.decode('utf-8', 'replace')[:4000]}\n"
                f"--- stderr ---\n{self.stderr.decode('utf-8', 'replace')[:2000]}>")


def _run(argv, cwd, timeout, stdin=b"") -> Output:
    import time
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(SCRATCH),
        "TZ": "UTC",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(a) for a in argv], cwd=str(cwd), env=env, input=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return Output(argv=[str(a) for a in argv], returncode=-1,
                      stdout=exc.stdout or b"", stderr=exc.stderr or b"",
                      timed_out=True, seconds=time.monotonic() - started)
    except OSError as exc:
        return Output(argv=[str(a) for a in argv], returncode=-2,
                      stderr=str(exc).encode(),
                      seconds=time.monotonic() - started)
    return Output(argv=[str(a) for a in argv], returncode=proc.returncode,
                  stdout=proc.stdout, stderr=proc.stderr,
                  seconds=time.monotonic() - started)


def _launch(target: str, argv):
    """Wrap argv in the emulator, if this target needs one."""
    return _EMULATOR[target] + [str(a) for a in argv]


_counter = [0]


def _tempfile(suffix: str, content: bytes) -> Path:
    _counter[0] += 1
    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"c{_counter[0]:04d}{suffix}"
    path.write_bytes(content)
    return path


def _as_bytes(source) -> bytes:
    return source if isinstance(source, bytes) else str(source).encode("utf-8")


# --------------------------------------------------------------------------- #
# The interpreter
# --------------------------------------------------------------------------- #

def run_js(source, target: str = "x86_64", *, args=(), stdin=b"",
           timeout=None, module: bool = False, cwd=None) -> Output:
    """Run a JS source string on one target's `qjs`.

    ``module`` passes -m, for source using import/export.  ``cwd`` is relative to
    the tree, and sets where a *runtime* file read resolves from -- std.open("x")
    and the like.

    It does not help an import.  A relative module specifier is resolved against
    the importing file, which lives in scratch, so `cwd="tests"` does not reach
    ./bjson.so.  Use `bjson()` for that; it puts the .so beside the source.
    """
    _check_target(target)
    tree = tree_for(target)
    path = _tempfile(".js", _as_bytes(source))
    argv = [tree / "qjs"]
    if module:
        argv.append("-m")
    argv.append(path)
    argv.extend(args)
    return _run(_launch(target, argv), tree / cwd if cwd else tree,
                timeout or _DEFAULT_TIMEOUT[target], _as_bytes(stdin))


def eval_js(expr, target: str = "x86_64", *, timeout=None) -> Output:
    """`qjs -e <expr>`.

    Note that this never loads the compiled repl blob, so it answers questions
    about the interpreter without involving the bytecode reader at all.  That
    makes it a useful control and a poor probe of the transport.
    """
    _check_target(target)
    tree = tree_for(target)
    argv = [tree / "qjs", "-e", _as_bytes(expr).decode("utf-8")]
    return _run(_launch(target, argv), tree, timeout or _DEFAULT_TIMEOUT[target])


def run_all(source, *, args=(), stdin=b"", timeout=None,
            module: bool = False, cwd=None) -> dict:
    """`run_js` on all three targets.  Returns {target: Output}."""
    return {t: run_js(source, t, args=args, stdin=stdin, timeout=timeout,
                      module=module, cwd=cwd)
            for t in TARGETS}


def suite(name: str, target: str = "x86_64", *, args=None, timeout=None) -> Output:
    """Run one of the repository's own test files, the way its Makefile does.

    ``name`` is relative to the tree root -- "tests/test_bignum.js" -- and the run
    happens from the tree root, because that is what the `test` target does and
    some of these load a file beside them.  The flags upstream passes for that
    suite are supplied unless you override ``args``.
    """
    _check_target(target)
    tree = tree_for(target)
    flags = SUITE_FLAGS.get(name, ()) if args is None else tuple(args)
    argv = [tree / "qjs", *flags, name]
    return _run(_launch(target, argv), tree,
                timeout or _DEFAULT_TIMEOUT[target] * 3)


def reference_suite(name: str, *, args=None, timeout=None) -> Output:
    """The same suite on the pristine original, built for x86-64.

    Useful because a suite that fails in both trees for an upstream reason is a
    wasted candidate, and this is the cheap way to find that out.
    """
    flags = SUITE_FLAGS.get(name, ()) if args is None else tuple(args)
    argv = [_REFERENCE / "qjs", *flags, name]
    return _run(argv, _REFERENCE, timeout or _DEFAULT_TIMEOUT["x86_64"] * 3)


def bjson(source, target: str = "x86_64", *, timeout=None) -> Output:
    """Run source as a module with the tree's bjson.so beside it.

    So `import * as bjson from "./bjson.so"` resolves.  bjson exposes
    JS_WriteObject and JS_ReadObject to JavaScript, and is the only place a JS
    program reaches the serialiser without going through a compiled unit.

    The .so is copied next to the generated source rather than the source being
    dropped into the tree, for two reasons: a relative module specifier is
    resolved against the importing file and not against the working directory, so
    `cwd="tests"` alone does not find it; and the tree is shared by every
    candidate that runs against this tree, so nothing writes into it.

    Note what a round trip inside one build does and does not show: the writer and
    the reader carry the same defect, so a value returns intact even when both are
    wrong.  A round trip is a control.  The measurement is what `compiled()` does,
    where the writer and the reader are on different sides of a byte-order
    boundary.
    """
    _check_target(target)
    tree = tree_for(target)
    so = tree / "tests" / "bjson.so"
    if not so.exists():
        return Output(argv=[str(so)], returncode=-2,
                      stderr=f"tests/bjson.so was not produced by the {target} "
                             f"build".encode())
    _counter[0] += 1
    here = SCRATCH / f"bjson{_counter[0]:04d}"
    here.mkdir(parents=True, exist_ok=True)
    shutil.copy2(so, here / "bjson.so")
    path = here / "main.js"
    path.write_bytes(_as_bytes(source))
    argv = [tree / "qjs", "-m", path]
    return _run(_launch(target, argv), here,
                timeout or _DEFAULT_TIMEOUT[target])


def artifact(name: str, target: str = "x86_64", *, args=(), stdin=b"",
             timeout=None) -> Output:
    """Run one of the binaries the published build produced, as shipped.

    ``examples/hello`` and ``examples/test_fib`` are the interesting ones: each
    embeds a blob that this build's *host* bytecode compiler wrote, inside a binary
    whose reader runs on the *target*.  ``qjs`` links the compiled repl and
    qjscalc the same way, so `artifact("qjs")` with no arguments enters the repl
    and `artifact("qjs", args=["--qjscalc"])`, where the build supports it, loads
    the other.
    """
    _check_target(target)
    tree = tree_for(target)
    path = tree / name
    if not path.exists():
        return Output(argv=[str(path)], returncode=-2,
                      stderr=f"{name} was not produced by the {target} build"
                             .encode())
    return _run(_launch(target, [path, *args]), tree,
                timeout or _DEFAULT_TIMEOUT[target], _as_bytes(stdin))


# --------------------------------------------------------------------------- #
# The reference
# --------------------------------------------------------------------------- #

def reference(source, *, args=(), stdin=b"", timeout=None,
              module: bool = False, cwd=None) -> Output:
    """The pristine original, built for x86-64, on the same source.

    The ground truth for what any expression is worth.  Built into the image
    before any submission existed, read-only, identical for every candidate and
    every round.

    There is no target argument, and the module docstring says why: on s390x the
    original is wrong, so its cross answers are not evidence of anything, and a
    candidate asserting the tree under test reproduces them asserts the defect.
    """
    path = _tempfile(".js", _as_bytes(source))
    argv = [_REFERENCE / "qjs"]
    if module:
        argv.append("-m")
    argv.append(path)
    argv.extend(args)
    return _run(argv, _REFERENCE / cwd if cwd else _REFERENCE,
                timeout or _DEFAULT_TIMEOUT["x86_64"], _as_bytes(stdin))


def reference_eval(expr, *, timeout=None) -> Output:
    """`qjs -e <expr>` on the pristine original, built for x86-64."""
    argv = [_REFERENCE / "qjs", "-e", _as_bytes(expr).decode("utf-8")]
    return _run(argv, _REFERENCE, timeout or _DEFAULT_TIMEOUT["x86_64"])


def agree(source, *, targets=TARGETS, against_reference: bool = True,
          **kwargs) -> tuple:
    """Convenience: run source everywhere and report whether the answers match.

    Returns ``(bool, {name: Output})``, with the reference under the key
    ``"reference"`` when ``against_reference``.  A run that failed counts as
    disagreement, so a crash on one target is not silently equal to a crash on
    another.

    This is the shape of a sound candidate on this task, and it is a helper rather
    than a rule: use it where the specification fixes the answer, and do the
    comparison yourself where it does not.
    """
    outs = {t: run_js(source, t, **kwargs) for t in targets}
    if against_reference:
        outs["reference"] = reference(source, **kwargs)
    values = [(o.ok, o.stdout) for o in outs.values()]
    return (len(set(values)) == 1 and values[0][0]), outs


# --------------------------------------------------------------------------- #
# The blob boundary
# --------------------------------------------------------------------------- #
#
# This is the mechanism of the task, so it gets two functions rather than one.
#
# On a cross build upstream's Makefile builds a SECOND bytecode compiler with the
# host compiler -- `host-qjsc` -- and points every blob-emitting rule at it.  So
# the blob inside `examples/hello` is written by a binary running here, on x86-64,
# and read by an interpreter running on the target.  `compiled()` is that path,
# applied to whatever source you give it.
#
# `target_compiled()` is the other one: the `qjsc` built FOR the target, run under
# the emulator, writing a blob in the target's own order for the target's own
# interpreter.  A build where only one of the two works is a partial port, and
# comparing them is the only way to see which half is missing.

_CFLAGS = ("-O2", "-D_GNU_SOURCE", "-DCONFIG_BIGNUM")
_LIBS = ("-lm", "-ldl", "-lpthread")


def _cc_for(target: str) -> str:
    return _CROSS_PREFIX[_check_target(target)] + "gcc"


def _link_blob(target: str, csource: Path, tag: str) -> tuple:
    """Compile a qjsc-emitted C file against the target's libquickjs.a."""
    tree = tree_for(target)
    exe = SCRATCH / f"{tag}.bin"
    argv = [_cc_for(target), *_CFLAGS, f"-I{tree}", "-o", str(exe),
            str(csource), str(tree / "libquickjs.a"), *_LIBS]
    return exe, _run(argv, SCRATCH, 300)


def _rejected_header(out: Output) -> bool:
    """Did the reader refuse the blob before running any of it?

    Upstream says "invalid version" and names the two numbers.  A tree that
    reworded it is still recognisable by refusing at a version, so both halves
    are matched loosely.  This only chooses which of two failures to report, so a
    tree that words it differently loses nothing but the nicer message.
    """
    text = out.stderr.decode("utf-8", "replace").lower()
    return "version" in text and ("invalid" in text or "unsupported" in text
                                  or "mismatch" in text or "expected" in text)


def compiled(source, target: str = "x86_64", *, args=(), stdin=b"",
             timeout=None, qjsc_args=("-e",), bswap=None) -> Output:
    """Compile source to a self-contained binary the way `examples/hello` is built.

    Three steps, all of them the Makefile's own:

      1. this build's HOST bytecode compiler emits C containing the blob
      2. the TARGET's compiler builds that C against the target's libquickjs.a
      3. the result runs under the target's emulator

    Step 1 happens on x86-64 and step 3 on the target, which is the byte-order
    boundary the whole task is about.  A tree that has not ported the transport
    produces a binary that misreads its own embedded bytecode.

    If step 1 or step 2 fails, the returned Output carries that failure -- a
    bytecode compiler that cannot emit, or emitted C that will not compile, is
    itself a finding, and it should not arrive as an exception in the middle of a
    candidate.

    ``qjsc_args`` defaults to ``("-e",)``, which is `hello.c`'s recipe: emit a C
    file with its own main().  Pass ``("-c",)`` for a bare byte array, or add
    ``-m`` for module source, or ``-fbignum`` for what `qjscalc.c` uses.

    ABOUT THE SWAP FLAG

    Upstream's bytecode compiler takes `-x`, which tells the writer to emit for a
    reader of the opposite byte order.  Whether a cross-endian blob needs it is a
    decision the port makes -- a tree may teach its writer to swap, or teach its
    reader to accept either order, or change the format so the question does not
    arise -- and the trees you are comparing may have decided differently.

    So this does not pick a side.  On a target whose order differs from the host
    it builds the blob both ways and returns the run that the target's own reader
    accepted, preferring a run that succeeded.  ``bswap=True`` or ``False`` pins
    it if you want to compare the two conventions yourself.

    What follows from that: "this tree needs -x and that one does not" is a claim
    about how the port was built, not about what it computes, and the adjudicator
    treats it as out of scope.  Assert on the value the program printed.
    """
    _check_target(target)
    tree = tree_for(target)
    js = _tempfile(".js", _as_bytes(source))
    tag = js.stem

    # The host compiler.  Falls back to `qjsc` so a submission that reorganised
    # the build into one compiler handling both orders is measured on what it
    # produces rather than failed for not having a file with this name.
    host_qjsc = tree / "host-qjsc"
    if not host_qjsc.exists():
        host_qjsc = tree / "qjsc"
    if not host_qjsc.exists():
        return Output(argv=[str(host_qjsc)], returncode=-2,
                      stderr=b"this build produced no bytecode compiler")

    def attempt(swap: bool, suffix: str) -> Output:
        flags = list(qjsc_args) + (["-x"] if swap else [])
        cfile = SCRATCH / f"{tag}{suffix}.c"
        emit = _run([host_qjsc, *flags, "-o", str(cfile), str(js)], tree, 300)
        if not emit.ok or not cfile.exists():
            emit.stderr += b"\n[srbqjs] the host bytecode compiler did not emit C"
            return emit
        exe, link = _link_blob(target, cfile, tag + suffix)
        if not link.ok or not exe.exists():
            link.stderr += b"\n[srbqjs] the emitted C did not compile for " \
                           + target.encode()
            return link
        return _run(_launch(target, [exe, *args]), SCRATCH,
                    timeout or _DEFAULT_TIMEOUT[target], _as_bytes(stdin))

    # Same order on both ends: there is nothing to negotiate.  And if the caller
    # already asked for the swap in qjsc_args, that is the pin.
    if bswap is None and BIG_ENDIAN[target] == BIG_ENDIAN["x86_64"]:
        bswap = False
    if bswap is None and "-x" in qjsc_args:
        bswap = False  # already there; do not pass it twice
    if bswap is not None:
        return attempt(bool(bswap), "x" if bswap else "")

    swapped = attempt(True, "x")
    if swapped.ok:
        return swapped
    plain = attempt(False, "")
    if plain.ok:
        return plain
    # Neither convention produced a working binary.  Report the one the reader
    # did not reject outright, so the failure shown is about the value rather
    # than about the header, and say that both were tried.
    chosen = plain if _rejected_header(swapped) else swapped
    chosen.stderr += (b"\n[srbqjs] tried the blob both swapped and unswapped; "
                      b"neither ran on " + target.encode())
    return chosen


def target_compiled(source, target: str = "x86_64", *, args=(), stdin=b"",
                    timeout=None, qjsc_args=("-e",)) -> Output:
    """As `compiled`, but the bytecode compiler built FOR the target writes the blob.

    It runs under the emulator, so writer and reader share a byte order and the
    transport question does not arise.  The comparison between this and `compiled`
    is what separates "the host path is unported" from "nothing is ported".
    """
    _check_target(target)
    tree = tree_for(target)
    js = _tempfile(".js", _as_bytes(source))
    tag = js.stem + "t"

    qjsc = tree / "qjsc"
    if not qjsc.exists():
        return Output(argv=[str(qjsc)], returncode=-2,
                      stderr=b"this build produced no target qjsc")

    cfile = SCRATCH / f"{tag}.c"
    emit = _run(_launch(target, [qjsc, *qjsc_args, "-o", str(cfile), str(js)]),
                tree, _DEFAULT_TIMEOUT[target])
    if not emit.ok or not cfile.exists():
        emit.stderr += b"\n[srbqjs] the target bytecode compiler did not emit C"
        return emit

    exe, link = _link_blob(target, cfile, tag)
    if not link.ok or not exe.exists():
        link.stderr += b"\n[srbqjs] the emitted C did not compile for " \
                       + target.encode()
        return link

    return _run(_launch(target, [exe, *args]), SCRATCH,
                timeout or _DEFAULT_TIMEOUT[target], _as_bytes(stdin))


# --------------------------------------------------------------------------- #
# The C API
# --------------------------------------------------------------------------- #

class CProgram:
    """A C program of yours, linked against the tree's libquickjs.a for a target.

    The only route to the C API, and the API is where the serialiser lives:
    JS_WriteObject and JS_ReadObject with their flags, JS_WRITE_OBJ_BSWAP,
    JS_GetTypedArrayBuffer, JS_ParseJSON.  Nothing in stage 2 calls any of them, so
    a port that fixed the qjsc path and left a flag unhandled is visible here and
    nowhere else.

    The compiler here is for YOUR C, never for the tree under test: all three
    trees are built before any candidate runs, and nothing in this module rebuilds
    one.  It cannot be used to supply C that a submission is missing.

        p = srbqjs.CProgram(src, target="s390x")
        build = p.build()
        assert build.ok, build          # a program that will not compile is a
        out = p.run()                   # finding about the header, not a crash
        assert out.ok, out

    `build()` returns the compiler's Output rather than raising, because "this
    compiles against the original and not against the submission" is itself a
    finding about the declared API.
    """

    #: quickjs.h and quickjs-libc.h, which is what a candidate's C will include.
    def __init__(self, source, target: str = "x86_64", *, extra_cflags=(),
                 libc: bool = True):
        self.target = _check_target(target)
        self.source = _as_bytes(source)
        self.extra_cflags = list(extra_cflags)
        self.libc = libc
        self._built = None
        _counter[0] += 1
        self.tag = f"p{_counter[0]:04d}"
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.path = SCRATCH / f"{self.tag}.c"
        self.path.write_bytes(self.source)
        self.exe = SCRATCH / f"{self.tag}.bin"

    @property
    def tree(self) -> Path:
        return tree_for(self.target)

    def build(self) -> Output:
        """Compile and link.  Cached; returns the compiler's Output."""
        if self._built is not None:
            return self._built
        argv = [_cc_for(self.target), *_CFLAGS, *self.extra_cflags,
                f"-I{self.tree}", "-o", str(self.exe), str(self.path),
                str(self.tree / "libquickjs.a"), *_LIBS]
        self._built = _run(argv, SCRATCH, 300)
        return self._built

    def run(self, *args, stdin=b"", timeout=None) -> Output:
        """Build if needed, then run under the target's emulator."""
        built = self.build()
        if not built.ok or not self.exe.exists():
            return built
        return _run(_launch(self.target, [self.exe, *args]), SCRATCH,
                    timeout or _DEFAULT_TIMEOUT[self.target], _as_bytes(stdin))
