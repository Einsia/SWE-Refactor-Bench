"""The capability surface of stage 3: two wheels, and what they install.

A candidate here is trying to find one thing the setuptools build delivers and the
Meson build does not. What it is handed to do that with is a *wheel* and the
*install* that wheel produces -- never the tree those came out of. That boundary is
the whole design, and it is why this module exports no path into the source.

WHY BOTH TREES CAN BE DRIVEN BY ONE COMMAND

Unlike a migration between two build systems with two command lines, both trees
here are PEP 517 packages:

    original     [build-system] requires = ["setuptools"]
    submission   [build-system] requires = ["meson-python"]  (backend: mesonpy)

so `python -m build --wheel --no-isolation` builds either one, and the backend it
dispatches to is the only difference. There is no dialect to switch on, which
removes the usual way a candidate learns which tree it is on: the command, the
flags and the front end are identical on both halves of every comparison.

`--no-isolation` because this container has no network. It is also the honest
setting: an isolated build would fetch its own backend and grade a resolver rather
than a repository. The two backends are installed in the image instead, which is
what makes this the only one of the three stage images with both.

CONFIGURATIONS ARE COMPILER SHIMS, NOT BUILD-SYSTEM FLAGS

The four configurations differ by what the *compiler* refuses, not by anything
either build system is asked for:

    default     a working gcc
    no-aesni    a gcc that rejects -maes
    no-clmul    a gcc that rejects -mpclmul and -mssse3
    no-isa      a gcc that rejects all three

That is deliberately external to both backends. A configuration expressed as a
setuptools option and a Meson option would be two different requests, and a
divergence between them would be this stage's own doing rather than the
migration's. A shim on PATH is one request, and what each build system does with a
compiler that says no is exactly the thing worth comparing: State A probes with
`compiler_opt.py`, and a correct Meson port probes with `cc.has_argument`.

WHAT A CANDIDATE SEES

    t = srbcrypto.tree("default")
    t.wheel              the wheel that came out
    t.prefix             where it was installed
    t.installed()        every installed path
    t.python(source)     run code against that install
    t.selftest("Cipher") run the library's own suite against it
    t.build_again()      the same sources built a second time

and the binutils helpers for the compiled objects. Nothing addresses the
repository. `t.root` exists because build failures have to be explainable, and
reading it from a test is out of scope -- what is graded is what the build ships.

Builds are cached across every candidate and every round, keyed by the tree token
and the configuration name. A pycryptodome wheel takes about seven seconds, so the
cache matters less here than in a C project, but the *install* and the self-test
runs on top of it are what a round actually spends its budget on.
"""
from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path

__all__ = [
    "CONFIGURATIONS", "TARGET_NAME", "SCRATCH", "VERSION", "DISTRIBUTION",
    "Output", "Tree", "SelfTestRun", "BuildFailed", "tree",
    "wheel_members", "wheel_read", "nm_defined", "nm_undefined", "objdump_isa",
    "elf_dynamic", "elf_needed",
]

#: What this migration is of. Not a candidate's to discover, and not a subject:
#: a submission that changed the version fails a stage-2 metadata check.
VERSION = "3.20.0"
DISTRIBUTION = "pycryptodome"

#: An opaque per-run label for the tree under test, for failure messages.
#:
#: It is a token rather than "original" | "submission" on purpose. With the role
#: readable, `assert TARGET_NAME == "original"` is a ten-point break in one line:
#: it passes on the original, fails on the submission and reproduces perfectly,
#: while establishing nothing whatsoever about the migration. The token says
#: *which* tree without saying *which side*, so two failure messages from one
#: comparison stay distinguishable and branching on it buys nothing.
TARGET_NAME = os.environ.get("SRB_TARGET_TOKEN", "?")

#: Per-(candidate, tree) scratch, emptied between the two halves of a comparison.
#: A file a candidate wrote while testing one tree must not still be here when the
#: same candidate runs against the other.
SCRATCH = Path(os.environ.get("SRB_SCRATCH", "/tmp/srb-candidate"))

#: Where builds and installs live. Shared across candidates and rounds.
_STATE = Path(os.environ.get("SRB_STATE", "/tmp/srb-verification/state"))

#: The tree under test, already copied by run-candidate.sh. Not exported: a
#: candidate that reads the repository is asserting about an implementation, which
#: is stage 1's subject and explicitly out of scope here.
_TARGET = Path(os.environ.get("SRB_TARGET", "/nonexistent"))

#: The interpreter that builds, installs and runs. Deliberately the system one
#: rather than the venv the candidate's pytest runs in: it is where BOTH backends
#: and the test vectors are installed, and keeping the orchestration interpreter
#: separate from the one executing adversary-written code is the same choice
#: build01's stage 3 makes.
#:
#: Named by the image rather than searched for. `shutil.which("python3")` would
#: answer correctly today and silently wrongly the moment anything put
#: /opt/venv/bin on PATH -- the candidate's venv has pytest and neither backend, so
#: a build driven by it would fail on both trees, in both cases reported as "the
#: tree will not build". A whole stage of that reads as six rounds finding
#: nothing. The image sets the variable and asserts the interpreter it names can
#: import both backends.
_PYTHON = os.environ.get("SRB_BUILD_PYTHON") or shutil.which("python3") or sys.executable

#: Fixed epoch for anything a backend stamps into an archive, so a second build of
#: the same sources is comparable with the first.
_EPOCH = "1704758400"

_JOBS = str(max(1, min((os.cpu_count() or 2), 8)))

# --------------------------------------------------------------------------- #
# The configurations
# --------------------------------------------------------------------------- #
# Each is a property of the compiler, not of either build system, so asking for one
# is the same request on both trees. See the module docstring.
#
# Deliberately absent:
#
#   repeat    "the same sources built twice" is not a configuration, it is a
#             question -- `t.build_again()` answers it and returns the second
#             wheel, so a candidate can compare the two itself.
#
#   sdist     `meson dist` hard-requires a git or hg checkout and this snapshot has
#             neither, so the submission cannot produce one however correct it is.
#             instruction.md says so, which puts it out of scope by construction.
#
#   no-sse2   -msse2 is in the x86-64 baseline ABI. A compiler that rejects it
#             cannot build the interpreter's own headers, so the configuration
#             would fail on both trees and measure nothing.


@dataclass(frozen=True)
class Configuration:
    name: str
    rejects: tuple[str, ...]
    about: str


CONFIGURATIONS = {
    c.name: c for c in [
        Configuration("default", (), "a working compiler: the wheel as shipped"),
        Configuration(
            "no-aesni", ("-maes",),
            "a compiler that rejects -maes, so the AES-NI unit cannot be built "
            "with its own instruction set"),
        Configuration(
            "no-clmul", ("-mpclmul", "-mssse3"),
            "a compiler that rejects -mpclmul and -mssse3, the pair the "
            "carry-less-multiply GHASH unit needs"),
        Configuration(
            "no-isa", ("-maes", "-mpclmul", "-mssse3"),
            "a compiler that rejects all three, so every specialised unit falls "
            "back to a portable one"),
    ]
}


# --------------------------------------------------------------------------- #
# Running things
# --------------------------------------------------------------------------- #

@dataclass
class Output:
    """The result of running something. Streams are bytes, deliberately.

    A cryptographic answer is a byte string, and most of what a candidate compares
    here is one. Decoding at the boundary would turn a real difference into a
    replacement character, or raise on output that is legitimately not UTF-8. Use
    `.text()` when you want a str and are sure you want one.
    """

    argv: list[str]
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def text(self, errors: str = "replace") -> str:
        return self.stdout.decode("utf-8", errors)

    def json(self):
        """stdout parsed as JSON, or None if it is not JSON.

        Returning None rather than raising: a candidate whose snippet crashed
        should see its own traceback in an assertion message, not a
        JSONDecodeError from the helper that was trying to read the output.
        """
        try:
            return json.loads(self.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def __str__(self) -> str:
        head = f"{' '.join(self.argv)} -> {self.returncode}"
        if self.timed_out:
            head += " (timed out)"
        tail = self.stderr.decode("utf-8", "replace").strip()
        return f"{head}\n{tail[:2000]}" if tail else head


def _clean_env(**extra: str) -> dict[str, str]:
    """The environment every subprocess here gets: fixed, and fixed identically
    for both trees.

    A build that consulted CFLAGS from the ambient environment, or a test that
    consulted the locale, would otherwise answer differently on the two halves of a
    comparison for a reason that is not the migration.

    Every SRB_* variable is dropped rather than passed through. Some of them name
    the tree, and a snippet run by `t.python()` inherits this environment -- so
    leaving them in would put the answer to "am I on the original?" inside the
    child process of every candidate.
    """
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "HOME": str(SCRATCH),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "SOURCE_DATE_EPOCH": _EPOCH,
    }
    env.update(extra)
    return env


def _run(argv, *, stdin: bytes = b"", timeout: float = 120.0,
         cwd: Path | None = None, env: dict[str, str] | None = None) -> Output:
    argv = [str(a) for a in argv]
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True, timeout=timeout,
            cwd=str(cwd) if cwd else None, env=env or _clean_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return Output(argv, 124, exc.stdout or b"", exc.stderr or b"", timed_out=True)
    except OSError as exc:
        return Output(argv, 127, b"", str(exc).encode())
    return Output(argv, proc.returncode, proc.stdout, proc.stderr)


# --------------------------------------------------------------------------- #
# The compiler shim
# --------------------------------------------------------------------------- #
# Same shim stage 2 uses, for the same reason and with the same self-test. A shim
# with a shell syntax error is still a file and still executable, and a build
# system handed one reports something that looks exactly like a submission whose
# build is broken. So it has to answer --version, compile a trivial program, and
# actually reject what it claims to reject, before any build is allowed to use it.
#
# The rejection is per-argument rather than per-pattern because that is how a real
# gcc behaves and how State A's probe depends on it behaving: pycryptodome offers
# `-mpclmul -mssse3` as a pair, and a real compiler stops at the first option it
# does not recognise.

_SHIM = """#!/bin/sh
# A compiler that does not support {flags}. Everything else passes through, so
# --version, the sanity check and every other probe answer exactly as the real
# compiler does.
for a in "$@"; do
  case "$a" in
{cases}
  esac
done
exec {real} "$@"
"""


def _make_shim(directory: Path, rejects: tuple[str, ...]) -> Path:
    """Write the shim into `directory` and prove it works. Returns the compiler."""
    real = shutil.which("gcc") or "/usr/bin/cc"
    directory.mkdir(parents=True, exist_ok=True)
    cases = "\n".join(
        f"    {flag}) echo \"error: unrecognized command-line option '{flag}'\" >&2; exit 1;;"
        for flag in rejects
    )
    cc = directory / "gcc"
    cc.write_text(_SHIM.format(flags=" ".join(rejects) or "nothing", cases=cases, real=real),
                  encoding="utf-8")
    cc.chmod(0o755)
    for alias in ("cc", "x86_64-linux-gnu-gcc"):
        link = directory / alias
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("gcc")

    probe = _run([cc, "--version"], timeout=60.0)
    if not probe.ok:
        raise RuntimeError(f"the compiler shim cannot answer --version: {probe}")
    src = directory / "_shimcheck.c"
    src.write_text("int main(void){return 0;}\n", encoding="utf-8")
    built = _run([cc, src, "-o", directory / "_shimcheck"], timeout=120.0)
    if not built.ok:
        raise RuntimeError(f"the compiler shim cannot compile a trivial program: {built}")
    for flag in rejects:
        got = _run([cc, flag, src, "-o", directory / "_shimcheck"], timeout=120.0)
        if got.ok:
            raise RuntimeError(f"the compiler shim accepted {flag}, which it must reject")
    return cc


def _with_shim(env: dict[str, str], directory: Path) -> dict[str, str]:
    """Point CC and PATH at the shim.

    Both channels, because a build system may honour either: CC is the documented
    one and PATH is what a probe that shells out to `cc` finds.

    Takes the DIRECTORY, not the compiler inside it. Getting that backwards sets
    `CC=<file>/gcc` and puts a file on PATH, which Meson answers with `Unknown
    compiler` -- a message indistinguishable, from the report, from a submission
    that cannot build. It cost three of stage 2's five configurations once, so the
    check is here rather than in a comment.
    """
    if not directory.is_dir():
        raise RuntimeError(
            f"_with_shim wants the directory holding the shim, got {directory} "
            f"({'a file' if directory.is_file() else 'nonexistent'})")
    env = dict(env)
    env["PATH"] = f"{directory}:{env.get('PATH', '/usr/bin:/bin')}"
    env["CC"] = str(directory / "gcc")
    return env


# --------------------------------------------------------------------------- #
# A built and installed tree
# --------------------------------------------------------------------------- #

class BuildFailed(AssertionError):
    """The tree under test would not build, or would not install, in this
    configuration.

    Raised rather than returned, because it is not a finding. A submission that
    does not build is stage 2's verdict, measured in an image built for it, and it
    is scored there out of 40.

    Letting it escape does not report a defect. lib/srbfault.py, registered by
    run-candidate.sh, turns an uncaught BuildFailed into exit 71 -- which the
    adjudicator reads as "this tree could not be tested" and answers `invalid` for,
    on whichever tree it happened to be. Passing on the original is not what stops
    "it did not build" from being a free finding, because a submission that will
    not build passes on the original: the round would see a divergence, six times,
    and report a defect that stage 2 owns.

    Catch it if a configuration genuinely is your subject: the message names which
    step failed and carries the tail of the log, and a candidate that catches it and
    passes has run -- srbfault does not fire for a BuildFailed you caught.
    """


@dataclass
class Tree:
    """One configuration of the tree under test: built into a wheel, installed.

    Everything a candidate should touch hangs off `wheel` and `prefix`. `root` is
    the copy that was built and is here so a build failure can be explained --
    reading it from a test asserts about the repository, which is stage 1's
    subject and out of scope here.
    """

    config: Configuration
    root: Path
    wheel: Path
    prefix: Path
    log: Path

    # -- the wheel ---------------------------------------------------------- #

    def wheel_entries(self) -> list[str]:
        """Every member of the wheel, sorted. Directory entries excluded."""
        return wheel_members(self.wheel)

    def wheel_read(self, member: str) -> bytes:
        """One member of the wheel, by exact name."""
        return wheel_read(self.wheel, member)

    def wheel_tag(self) -> tuple[str, str, str]:
        """The wheel's compatibility tag, split: (interpreter, abi, platform).

        Read the ABI field. Do not read the interpreter field.

        State A tags `cp35-abi3` because its setup.py declares py_limited_api with
        a floor of 3.5; a meson-python build with `limited-api = true` tags
        `cp312-abi3`, naming the interpreter that built it. Both mean the same
        thing -- one binary, every CPython from its floor upward -- and
        instruction.md told the agent in as many words that the ABI component is
        required and the interpreter component is free.

        So `assert t.wheel_tag()[0] == "cp35"` passes on the original, fails on the
        submission, reproduces every time, and is worth nothing: it asserts the
        submission failed to do something it was told it did not have to do. It is
        named in probe.toml's deny list and the adjudicator rejects it on sight.
        """
        stem = self.wheel.name[:-len(".whl")]
        parts = stem.split("-")
        if len(parts) < 5:
            raise AssertionError(
                f"{TARGET_NAME}'s wheel is named {self.wheel.name!r}, which is not "
                f"name-version-interpreter-abi-platform")
        return (parts[-3], parts[-2], parts[-1])

    def record(self) -> dict[str, tuple[str, int | None]]:
        """RECORD as a mapping: installed path -> (hash, size).

        The hash is the string as RECORD spells it -- `sha256=<urlsafe-b64>` -- and
        not decoded, because a candidate comparing RECORD entries between two
        wheels is comparing what the installers see. An entry with no size (RECORD
        itself) gets None.
        """
        dist_info = self.dist_info()
        raw = self.wheel_read(f"{dist_info}/RECORD").decode("utf-8")
        out: dict[str, tuple[str, int | None]] = {}
        for row in csv.reader(io.StringIO(raw)):
            if not row or not row[0]:
                continue
            digest = row[1] if len(row) > 1 else ""
            try:
                size = int(row[2]) if len(row) > 2 and row[2] else None
            except ValueError:
                size = None
            out[row[0]] = (digest, size)
        return out

    def dist_info(self) -> str:
        """The `.dist-info` directory's name, without a trailing slash.

        Found rather than assumed: it is `<name>-<version>.dist-info`, and a
        submission that got either wrong should fail an assertion about the name
        rather than make every other helper raise KeyError.
        """
        names = {e.split("/", 1)[0] for e in self.wheel_entries()
                 if e.split("/", 1)[0].endswith(".dist-info")}
        if len(names) != 1:
            raise AssertionError(
                f"{TARGET_NAME}'s wheel has {len(names)} .dist-info directories "
                f"({sorted(names)}); a wheel has exactly one")
        return names.pop()

    def metadata(self) -> dict[str, list[str]]:
        """METADATA as field -> list of values, in file order.

        A list per field because several are legitimately repeated -- Classifier
        appears a dozen times -- and flattening them would make a comparison
        between two wheels depend on which one happened to be read first. Field
        names keep their case as written.
        """
        return _headers(self.wheel_read(f"{self.dist_info()}/METADATA"))

    def wheel_file(self) -> dict[str, list[str]]:
        """The WHEEL file as field -> list of values. `Tag` is the repeated one."""
        return _headers(self.wheel_read(f"{self.dist_info()}/WHEEL"))

    def metadata_body(self) -> bytes:
        """The long description: everything after METADATA's header block.

        Bytes, and split on the first blank line rather than parsed, because the
        body is reStructuredText that the email parser would hand back as a decoded
        str -- and the question a candidate asks about it is whether the two wheels
        carry the same bytes.
        """
        raw = self.wheel_read(f"{self.dist_info()}/METADATA")
        for sep in (b"\r\n\r\n", b"\n\n"):
            if sep in raw:
                return raw.split(sep, 1)[1]
        return b""

    # -- the install -------------------------------------------------------- #

    def installed(self) -> list[str]:
        """Every installed path, prefix-relative, sorted, files only.

        This is what a consumer gets. `__pycache__` is excluded: whether a byte
        cache exists depends on how the install was invoked rather than on what
        the wheel contains, and both trees are installed the same way here.
        """
        out = []
        for dirpath, dirnames, filenames in os.walk(self.prefix):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in filenames:
                out.append(str((Path(dirpath) / name).relative_to(self.prefix)))
        return sorted(out)

    def read(self, relpath: str) -> bytes:
        """One installed file, by prefix-relative path."""
        target = self.prefix / relpath
        if not target.is_file():
            raise AssertionError(
                f"{TARGET_NAME} did not install {relpath!r} in the "
                f"{self.config.name} configuration")
        return target.read_bytes()

    def extensions(self) -> dict[str, Path]:
        """Installed compiled extensions: dotted module name -> the file.

        `Crypto/Cipher/_raw_aes.abi3.so` becomes `Crypto.Cipher._raw_aes`. Keyed by
        module name rather than by filename because the module name is the thing
        with meaning to an importer, and because it makes the two trees comparable
        without either one's file-naming convention being the reference.
        """
        out: dict[str, Path] = {}
        for path in sorted(self.prefix.rglob("*.so")):
            rel = path.relative_to(self.prefix)
            name = rel.name
            for suffix in (".abi3.so", ".so"):
                if name.endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            # A cpython-312-x86_64-linux-gnu tag, if the build used one instead of
            # abi3. Stripped so the dotted name is the module either way.
            name = re.sub(r"\.cpython-\d+[^.]*$", "", name)
            dotted = ".".join(list(rel.parts[:-1]) + [name])
            out[dotted] = path
        return out

    # -- asking the install a question -------------------------------------- #

    def python(self, source: str, *args: str, stdin: bytes | str = b"",
               timeout: float = 600.0) -> Output:
        """Run `source` against this install and return what it printed.

        This is the stage's main instrument. It is the analogue of compiling a C
        program against an installed library: the snippet imports the library the
        wheel put on disk and reports what it computed.

            out = t.python("from Crypto.Cipher import AES; print(AES.block_size)")
            assert out.ok and out.text().strip() == "16"

        Not raised on a non-zero exit: "this runs against the original and crashes
        against the submission" is exactly a finding, so it is returned. Check
        `.ok` yourself, and put `out` in the assertion message -- its str carries
        the child's stderr, which is where the traceback is.

        The interpreter runs with `-P` and `-s`, so neither the working directory
        nor a user site directory is on sys.path. That matters more than it looks:
        the repository has a `lib/Crypto/` of its own, so without `-P` a snippet run
        from the wrong directory would import the source tree's pure-Python modules
        instead of the install's, find no compiled extensions beside them, and fail
        on both trees for a reason that has nothing to do with either build.

        sys.path is therefore: this install, then the image's site-packages. The
        second is there for `pycryptodome_test_vectors` and the standard library
        only -- the image has no pycryptodome of its own, and the stage-3 image
        asserts that at build time, because a `Crypto` importable from
        site-packages would make every candidate silently test the image's copy on
        both trees and find nothing.
        """
        script = SCRATCH / "snippets" / f"s{hashlib.sha256(source.encode()).hexdigest()[:16]}.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(source, encoding="utf-8")
        env = _clean_env(PYTHONPATH=str(self.prefix))
        if isinstance(stdin, str):
            stdin = stdin.encode()
        return _run([_PYTHON, "-P", "-s", script, *args],
                    stdin=stdin, timeout=timeout, cwd=SCRATCH, env=env)

    def selftest(self, *only: str, fast: bool = False,
                 timeout: float = 3600.0) -> "SelfTestRun":
        """Run pycryptodome's own suite against this install.

            r = t.selftest("Cipher.AES")
            assert r.all_passed, r.failed

        `only` filters by id prefix, joined to `Crypto.SelfTest.`, so "Cipher" is
        every cipher test. With no argument the whole suite runs: 42,784 instances
        over 39,245 distinct ids, about eighty seconds. Collection is the same single
        call either way -- the filter is applied to the collected instances, not to
        what gets imported -- so a narrow run costs collection plus only the tests it
        kept.

        Check the prefix exists before you rely on it. An id is the module the
        instance's class was DEFINED in, which for most hashes and ciphers is a
        shared `common` helper rather than the per-algorithm module: there is a
        `Crypto/SelfTest/Hash/test_SHA256.py`, and "Hash.test_SHA256" matches no id,
        because its cases come from `Crypto.SelfTest.Hash.common`. A filter that
        matches nothing makes `all_passed` False on both trees and costs a candidate,
        so it is reported rather than obeyed: `r.filter_matched_nothing` says so and
        `r.available_prefixes` lists what exists. Measured prefixes with the most ids
        are "Hash.test_keccak", "Protocol.test_KDF", "Hash.test_SHAKE",
        "PublicKey.test_ECC_NIST", "Cipher.test_EAX" and "Math.test_Numbers".

        `fast=True` turns `slow_tests` off, which collects 3,603 instances instead
        of 42,784. The difference is almost entirely known-answer cipher vectors,
        which is the part that would notice a wrong compiler flag, so `fast` is for
        a smoke check and not for a comparison you intend to rely on.

        An id can be produced by many instances -- pycryptodome builds one TestCase
        per vector -- so the recorded outcome for an id is the worst any instance of
        it produced. One failing vector cannot be averaged away by the hundreds
        passing beside it.
        """
        out = SCRATCH / "selftest" / f"{self.config.name}-{'-'.join(only) or 'all'}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        env = _clean_env(PYTHONPATH=str(self.prefix))
        argv = [_PYTHON, "-P", "-s", str(Path(__file__).resolve()),
                "--selftest", str(self.prefix), str(out)]
        if fast:
            argv.append("--fast")
        argv.extend(only)
        proc = _run(argv, timeout=timeout, cwd=SCRATCH, env=env)
        if out.is_file():
            payload = json.loads(out.read_text(encoding="utf-8"))
            return SelfTestRun(config=self.config.name, payload=payload, output=proc)
        return SelfTestRun(config=self.config.name,
                           payload={"crashed": True, "tests": {}, "counts": {},
                                    "collected": 0, "recorded": 0},
                           output=proc)

    # -- building it twice --------------------------------------------------- #
    #
    # There is deliberately no `source_tree_dirty()` here, and the reason is worth
    # stating because a candidate will think of the question.
    #
    # "What did the build write into the source tree" has a different answer on the
    # two trees, and the difference is the migration working. setuptools leaves a
    # `build/lib.linux-x86_64-cpython-312/` full of objects and a
    # `pycryptodome.egg-info/`; meson-python builds in a temporary directory and
    # leaves neither. So every form of the assertion -- the tree is clean after a
    # build, the tree holds objects beside its sources, the tree holds an egg-info
    # -- passes on the original and fails on a correct submission, which is five
    # points for finding nothing. It is also the wrong kind of question for this
    # stage: what the build writes on its way to a wheel is how it works, and what
    # is graded here is what it ships.

    def build_again(self, *, timeout: float = 1800.0) -> "Tree":
        """Build this configuration a second time, from a fresh copy of the tree.

        Returns a Tree whose `wheel` is the second wheel, so a candidate can
        compare the two for itself:

            a, b = t, t.build_again()
            assert a.wheel_entries() == b.wheel_entries()

        A fresh copy rather than the same directory: what this answers is "do the
        same sources produce the same wheel", and building again in a tree that
        already holds the first build's output answers a different and much weaker
        question. Not cached -- asking twice builds twice, which is the point.
        """
        state = _STATE / TARGET_NAME / f"{self.config.name}-again"
        shutil.rmtree(state, ignore_errors=True)
        return _build(self.config, state, timeout)


@dataclass
class SelfTestRun:
    """The result of running the library's own suite against one install."""

    config: str
    payload: dict
    output: Output

    @property
    def crashed(self) -> bool:
        """The runner did not finish. Not the same as tests failing.

        A crash here is usually the install being unimportable, which is a real
        difference between two trees and worth asserting on -- but assert on it
        deliberately, because `all_passed` on a crashed run is False for a reason
        that has nothing to do with any individual test.
        """
        return bool(self.payload.get("crashed"))

    @property
    def outcomes(self) -> dict[str, str]:
        """test id -> the worst outcome any instance of it produced."""
        return self.payload.get("tests", {})

    @property
    def counts(self) -> dict[str, int]:
        """How many distinct ids ended in each of pass/fail/error/skip."""
        return self.payload.get("counts", {})

    @property
    def collected(self) -> int:
        """Instances loaded. Larger than the id count: ids repeat per vector."""
        return int(self.payload.get("collected", 0))

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.outcomes))

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(sorted(i for i, o in self.outcomes.items() if o in ("fail", "error")))

    @property
    def all_passed(self) -> bool:
        return (not self.crashed and bool(self.outcomes)
                and not self.failed)

    def note(self, test_id: str) -> str:
        """The traceback recorded for a failing id, if there is one."""
        return self.payload.get("notes", {}).get(test_id, "")

    @property
    def filter_matched_nothing(self) -> bool:
        """An `only` prefix was given and no collected id started with it.

        Distinct from "everything passed" and from "the run crashed", and worth
        checking before you believe an empty result: a filter that matches nothing
        makes `all_passed` False on BOTH trees, so the candidate is discarded as a
        broken test rather than counted.

        The trap is a real one rather than a theoretical one. `Hash.test_SHA256`
        looks like the obvious prefix and matches no id, because pycryptodome builds
        most hash tests from `Crypto.SelfTest.Hash.common` helpers and each instance
        carries the helper module's name. `available_prefixes` is what actually
        exists.
        """
        return bool(self.payload.get("filter_matched_nothing"))

    @property
    def available_prefixes(self) -> tuple[str, ...]:
        """The two-component prefixes that exist in this run, e.g. "Hash.test_keccak".

        What `only` can be given. Computed before the filter is applied, so it is the
        full list even on a run whose filter matched nothing.
        """
        return tuple(self.payload.get("available_prefixes", ()))

    @property
    def backend(self) -> str:
        """Which integer implementation the library chose: gmp, custom or native.

        Asked of the library rather than inferred from what imports:
        `Crypto.Math._IntegerGMP` imports successfully even where libgmp is absent,
        because it falls back internally, so an import probe would answer "gmp"
        unconditionally. This reads the class that actually won.
        """
        return self.payload.get("implementation", {}).get("library", "?")

    def __str__(self) -> str:
        if self.crashed:
            return (f"selftest[{self.config}] crashed on {TARGET_NAME}: "
                    f"{self.output}")
        if self.filter_matched_nothing:
            want = self.payload.get("filter", [])
            near = [p for p in self.available_prefixes
                    if any(w.split(".")[0] in p for w in want)]
            return (
                f"selftest[{self.config}] on {TARGET_NAME}: the filter {want} "
                f"matched none of the collected ids, so this run tested nothing and "
                f"will fail on both trees. "
                f"{'Nearest prefixes: ' + str(near[:10]) if near else ''} "
                f"All of them are in .available_prefixes "
                f"({len(self.available_prefixes)} entries).")
        bad = self.failed
        head = (f"selftest[{self.config}] on {TARGET_NAME}: {self.collected} "
                f"instances, {len(self.outcomes)} ids, {self.counts}")
        if bad:
            head += f"\nfirst failures: {bad[:5]}\n{self.note(bad[0])[:1200]}"
        return head


# --------------------------------------------------------------------------- #
# tree(): build, install, once
# --------------------------------------------------------------------------- #

_CACHE: dict[str, Tree] = {}


def tree(config: str = "default", *, timeout: float = 1800.0) -> Tree:
    """The tree under test, built into a wheel and installed, in this configuration.

        t = srbcrypto.tree("no-aesni")
        assert "Crypto.Cipher._raw_aes" in t.extensions()

    The first call for a configuration builds and installs it; every later call in
    this round and in every other round returns the same install. The tree does not
    change between candidates, so rebuilding per candidate would spend the stage's
    budget on compilation.

    Raises BuildFailed if the tree will not build or install in this configuration.
    That is not a finding -- see BuildFailed.
    """
    if config not in CONFIGURATIONS:
        raise AssertionError(
            f"unknown configuration {config!r}; this stage builds "
            f"{', '.join(sorted(CONFIGURATIONS))}. Each is a property of the "
            f"compiler rather than a flag either build system is passed, which is "
            f"what makes asking for one the same request on both trees.")
    if config in _CACHE:
        return _CACHE[config]

    cfg = CONFIGURATIONS[config]
    state = _STATE / TARGET_NAME / config
    state.mkdir(parents=True, exist_ok=True)

    # Candidates within a round run one at a time, but a round's reruns and the
    # rerun of an upheld candidate can overlap. The lock makes the first arrival
    # build and the rest wait, rather than two processes writing one tree.
    with _lock(state / ".lock"):
        t = _build(cfg, state, timeout)

    _CACHE[config] = t
    return t


def _build(cfg: Configuration, state: Path, timeout: float) -> Tree:
    """Copy, build, install. Idempotent: a finished state directory is reused."""
    src = state / "src"
    dist = state / "dist"
    prefix = state / "install"
    log = state / "build.log"
    marker = state / ".installed"

    if marker.is_file():
        wheel = _newest_wheel(dist)
        if wheel is not None and prefix.is_dir():
            return Tree(cfg, src, wheel, prefix, log)
        # A marker with no wheel behind it means a previous run was interrupted
        # between the two. Start over rather than return a Tree that lies.
        marker.unlink(missing_ok=True)

    for path in (src, dist, prefix):
        shutil.rmtree(path, ignore_errors=True)
    log.unlink(missing_ok=True)
    dist.mkdir(parents=True, exist_ok=True)

    # The build happens in a copy, never in the tree the harness handed over.
    # A Python build writes into its own source tree -- build/, an egg-info or a
    # Meson build directory -- so building in place would make the second
    # configuration inherit the first one's output, and four configurations of one
    # tree would stop being four independent builds.
    shutil.copytree(_TARGET, src, symlinks=True)

    env = _clean_env()
    if cfg.rejects:
        # `_make_shim` returns the compiler; `_with_shim` wants the directory it
        # lives in. Two statements because collapsing them reads fine and is wrong.
        shim_dir = state / "cc"
        _make_shim(shim_dir, cfg.rejects)
        env = _with_shim(env, shim_dir)

    out = _logged(log, [_PYTHON, "-m", "build", "--wheel", "--no-isolation",
                        "-o", str(dist)], cwd=src, timeout=timeout, env=env)
    if not out.ok:
        raise BuildFailed(_why("build a wheel", cfg, log, out))

    wheel = _newest_wheel(dist)
    if wheel is None:
        raise BuildFailed(_why("produce a wheel (the build reported success)",
                               cfg, log, out))

    # `--no-deps --no-index` so installing is an unpack and not a resolve: this
    # container has no network, and what is being measured is the wheel rather than
    # anything pip might have gone looking for. `--target` rather than a venv
    # because the result is a plain directory a candidate can walk.
    out = _logged(log, [_PYTHON, "-m", "pip", "install", "--no-deps", "--no-index",
                        "--no-compile", "--target", str(prefix), str(wheel)],
                  cwd=state, timeout=900.0, env=_clean_env())
    if not out.ok:
        raise BuildFailed(_why("install its own wheel", cfg, log, out))

    marker.write_text(f"{cfg.name}\n")
    return Tree(cfg, src, wheel, prefix, log)


def _newest_wheel(dist: Path) -> Path | None:
    if not dist.is_dir():
        return None
    wheels = sorted(dist.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    return wheels[-1] if wheels else None


def _why(step: str, cfg: Configuration, log: Path, out: Output) -> str:
    tail = log.read_text(errors="replace")[-3000:] if log.is_file() else ""
    return (f"{TARGET_NAME} failed to {step} in the {cfg.name} configuration "
            f"({cfg.about}).\nexit {out.returncode}"
            + (" (timed out)" if out.timed_out else "")
            + f"\n--- last of {log} ---\n{tail}")


def _logged(log: Path, argv, *, cwd: Path, timeout: float,
            env: dict[str, str] | None = None) -> Output:
    """Run a build step, appending everything to the log.

    On disk rather than in the returned Output's place in a report because a
    failing build produces a lot of text and a candidate needs its tail in an
    assertion message, not all of it.
    """
    out = _run(argv, cwd=cwd, timeout=timeout, env=env)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as fh:
        fh.write(f"\n$ {' '.join(str(a) for a in argv)}\n".encode())
        fh.write(out.stdout)
        fh.write(out.stderr)
        fh.write(f"[exit {out.returncode}]\n".encode())
    return out


def _headers(raw: bytes) -> dict[str, list[str]]:
    """An RFC 822 header block as field -> values, order preserved."""
    message = BytesParser().parsebytes(raw)
    out: dict[str, list[str]] = {}
    for key, value in message.items():
        out.setdefault(key, []).append(value)
    return out


class _lock:
    """A file lock, so two processes do not build one tree at once."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False


# --------------------------------------------------------------------------- #
# Reading a wheel
# --------------------------------------------------------------------------- #

def wheel_members(path: Path) -> list[str]:
    """Every file in a wheel, sorted. Directory entries excluded."""
    with zipfile.ZipFile(path) as zf:
        return sorted(i.filename for i in zf.infolist() if not i.is_dir())


def wheel_read(path: Path, member: str) -> bytes:
    """One member of a wheel, by exact name.

    Raises with the member list truncated into the message rather than letting
    zipfile's KeyError through, because "the wheel does not contain this" is
    usually the finding and the neighbouring names are how you tell a missing file
    from a moved one.
    """
    with zipfile.ZipFile(path) as zf:
        try:
            return zf.read(member)
        except KeyError:
            near = [n for n in zf.namelist() if member.rsplit("/", 1)[-1] in n]
            raise AssertionError(
                f"{TARGET_NAME}'s wheel has no member {member!r}"
                + (f"; similar names: {near[:8]}" if near else "")) from None


# --------------------------------------------------------------------------- #
# Reading a compiled object
# --------------------------------------------------------------------------- #
# Everything below shells out to binutils rather than parsing ELF, for the reason
# build01's stage 3 does: the questions are the ones a developer asks from a
# terminal, and a candidate that wants to check the helper's answer can run the
# same command by hand.

def nm_defined(path: Path) -> set[str]:
    """Symbols the object EXPORTS, from its dynamic symbol table.

    `nm -D --defined-only`. For a Python extension this is the interesting
    direction: State A's 41 objects export 277 symbols between them -- 217 distinct
    names, because 29 of them are arithmetic helpers statically linked into several
    objects at once -- and not one `PyInit_*`, because they are ctypes targets
    loaded by name rather than importable modules.
    """
    out = _run(["nm", "-D", "--defined-only", str(path)], timeout=120.0)
    return {line.split()[-1] for line in out.text().splitlines() if line.strip()}


def nm_undefined(path: Path) -> set[str]:
    """Symbols the object IMPORTS.

    Where compile flags become visible from outside: `_FORTIFY_SOURCE` turns
    `memcpy` into `__memcpy_chk`, and `-fstack-protector` introduces
    `__stack_chk_fail`. A migration that dropped a hardening flag shows up here in
    one call and nowhere else.
    """
    out = _run(["nm", "-D", "--undefined-only", str(path)], timeout=120.0)
    return {line.split()[-1] for line in out.text().splitlines() if line.strip()}


#: Instruction families, by mnemonics only that family has. Order matters only for
#: readability; membership is independent.
_ISA_FAMILIES = (
    ("aesni", r"\b(aesenc|aesenclast|aesdec|aesdeclast|aeskeygenassist|aesimc)\b"),
    # `pclmul[a-z]*` rather than `pclmulqdq`, and this is measured rather than
    # defensive. GNU objdump does not print the mnemonic the assembler accepts: for
    # the four standard immediates it prints the immediate in the mnemonic instead,
    # so State A's `_ghash_clmul` disassembles to pclmullqlqdq (18), pclmulhqlqdq
    # (8), pclmullqhqdq (8) and pclmulhqhqdq (8) -- and contains the literal string
    # "pclmulqdq" nowhere at all. The exact pattern matched none of them, which made
    # this family silently undetectable: `objdump_isa` answered {"sse2", "ssse3"}
    # for the one object in the library whose whole reason to exist is carry-less
    # multiply. A candidate asserting the elevated unit really got its instruction
    # set would have failed on the original and been discarded as a broken test.
    ("clmul", r"\b(v?pclmul[a-z]*)\b"),
    ("ssse3", r"\b(pshufb|palignr|phaddw|phaddd|pmaddubsw|pabsb|pabsw|pabsd)\b"),
    ("sse41", r"\b(pblendw|pblendvb|pmulld|ptest|pcmpeqq|packusdw)\b"),
    ("avx2", r"\b(vpbroadcast[bwdq]|vperm2i128|vpgatherdd)\b"),
    # SSE2 is in the x86-64 baseline ABI. It is reported for completeness and
    # proves nothing on its own: most of this library's objects contain it with no
    # flag involved.
    ("sse2", r"\b(movdqa|movdqu|paddq|pxor|pshufd|punpcklqdq)\b"),
)


def objdump_isa(path: Path) -> set[str]:
    """Which instruction families an object actually contains.

        assert "aesni" not in srbcrypto.objdump_isa(portable_object)

    Both directions are defects and both are in scope. A baseline object holding an
    AES-NI instruction is a SIGILL on hardware without it -- pycryptodome dispatches
    at import time by loading one object or another, so an unguarded instruction is
    a crash rather than a slow path. An object that was supposed to be specialised
    and contains none of its family means the elevated unit silently compiled
    generic code.

    Note what this cannot tell you: `-msse2` is the x86-64 baseline, so "sse2" in
    the answer is not evidence any flag was passed.
    """
    out = _run(["objdump", "-d", "--no-show-raw-insn", str(path)], timeout=300.0)
    text = out.text()
    return {name for name, pattern in _ISA_FAMILIES if re.search(pattern, text)}


def elf_dynamic(path: Path) -> dict[str, list[str]]:
    """The dynamic section as tag -> values, using readelf's own tag names.

    Keys are the ones readelf prints: "NEEDED", "SONAME", "RPATH", "RUNPATH",
    "FLAGS", "FLAGS_1". An RPATH pointing into the build machine's filesystem is
    the classic one here -- it works in the container that built it and nowhere
    else.
    """
    out = _run(["readelf", "-d", str(path)], timeout=120.0)
    found: dict[str, list[str]] = {}
    for line in out.text().splitlines():
        match = re.search(r"\(([A-Z_0-9]+)\)\s+(.*)$", line)
        if not match:
            continue
        tag, rest = match.group(1), match.group(2).strip()
        inner = re.search(r"\[(.*)\]", rest)
        found.setdefault(tag, []).append(inner.group(1) if inner else rest)
    return found


def elf_needed(path: Path) -> set[str]:
    """The shared libraries an object declares it needs. DT_NEEDED, as a set."""
    return set(elf_dynamic(path).get("NEEDED", ()))


# --------------------------------------------------------------------------- #
# The self-test child
# --------------------------------------------------------------------------- #
# Runs in its own interpreter with the install first on sys.path. Kept in this file
# rather than in a second one so the module a candidate imports and the code that
# runs the suite cannot drift apart.

def _selftest_child(install: str, out: str, fast: bool, only: list[str]) -> int:
    import time
    import unittest

    sys.path.insert(0, install)
    from Crypto import SelfTest  # noqa: E402 -- must follow the sys.path edit

    config = {"slow_tests": not fast, "wycheproof_warnings": False}
    suite = unittest.TestSuite(SelfTest.get_tests(config=config))

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                yield from walk(t)
            else:
                yield t

    instances = list(walk(suite))
    available = sorted({".".join(t.id().split(".")[2:4]) for t in instances})
    # A filter that matches nothing is recorded rather than silently obeyed.
    #
    # This is measured, not hypothetical: `Hash.test_SHA256` is the obvious prefix
    # and it matches no id at all, because pycryptodome builds most hash tests from
    # `Crypto.SelfTest.Hash.common` helpers and the instances carry the helper
    # module's name, not the per-algorithm module's. Left implicit, the run comes
    # back with zero collected, `all_passed` is False because there is nothing to
    # have passed, and the candidate fails on BOTH trees -- which the runner reports
    # as "does not pass on the original" and the author reads as a build problem.
    # One wasted candidate per adversary who guesses a plausible prefix.
    matched_nothing = False
    if only:
        prefixes = tuple(f"Crypto.SelfTest.{p}" for p in only)
        kept = [t for t in instances if t.id().startswith(prefixes)]
        matched_nothing = not kept
        instances = kept

    severity = {"pass": 0, "skip": 1, "fail": 2, "error": 3}
    outcomes: dict[str, str] = {}
    notes: dict[str, str] = {}

    def record(test, outcome):
        tid = test.id()
        if severity[outcome] >= severity.get(outcomes.get(tid, "pass"), 0):
            outcomes[tid] = outcome

    class Collector(unittest.TestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            record(test, "pass")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            record(test, "fail")
            self._note(test, err)

        def addError(self, test, err):
            super().addError(test, err)
            record(test, "error")
            self._note(test, err)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            record(test, "skip")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            record(test, "pass")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            record(test, "fail")

        def _note(self, test, err):
            import traceback
            notes.setdefault(test.id(),
                             "".join(traceback.format_exception(*err))[-1200:])

    started = time.time()
    unittest.TestSuite(instances).run(Collector())

    counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    for outcome in outcomes.values():
        counts[outcome] += 1

    try:
        from Crypto.Math import Numbers
        library = {
            "Crypto.Math._IntegerGMP": "gmp",
            "Crypto.Math._IntegerCustom": "custom",
            "Crypto.Math._IntegerNative": "native",
        }.get(Numbers.Integer.__module__, Numbers.Integer.__module__)
    except Exception as exc:  # noqa: BLE001 -- reported, not raised
        library = f"unavailable: {type(exc).__name__}"

    Path(out).write_text(json.dumps({
        "collected": len(instances),
        "recorded": len(outcomes),
        "counts": counts,
        "tests": outcomes,
        "notes": notes,
        "seconds": round(time.time() - started, 1),
        "implementation": {"library": library, "api": "ctypes"},
        "filter": only,
        "filter_matched_nothing": matched_nothing,
        "available_prefixes": available,
    }, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    if "--selftest" not in sys.argv:
        raise SystemExit("srbcrypto is a library; only --selftest runs as a script")
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    raise SystemExit(_selftest_child(argv[0], argv[1], "--fast" in sys.argv, argv[2:]))
