"""Build both states through the one front end they share, and record what came out.

The contract this suite measures is a PEP 517 one, so this builder only ever says:

    python -m build --wheel --no-isolation

State A answers that with setuptools; State B answers it with whatever backend
the submission declared. Neither is named here. `meson` and `ninja` are never
invoked directly and `compile_commands.json` is never read -- both would make
this file a Meson harness, and a Meson harness cannot build State A, which means
it could not compare the two. Every measurement downstream reads the wheel, the
installed tree and the delivered objects: artefacts, not build description.

What this file does provide is the one dimension that turns "it built" into a
question about the build system: a compiler that refuses a flag. pycryptodome's
setup.py probes for AES-NI and PCLMUL support before it decides whether to
compile `_raw_aesni` and `_ghash_clmul` at all, so a build system that asks the
same question drops those two objects when the compiler cannot answer, and one
that hardcoded State A's answer either fails to compile or produces an artefact
that dies on hardware without the instruction. That is a behavioural difference,
visible in the wheel's member list, and it is why the unit of work here is a
*named configuration* rather than a single build.

Timeouts and OSErrors become return codes 124 and 127. Nothing here raises: a
build that fails is a measurement with a log attached, and the module that reads
this ledger is responsible for saying so with the log in the failure message.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

WORK = Path(os.environ.get("SRB_SUITE_WORK", "/tmp/srb-suite"))
LEDGER_PATH = Path(os.environ.get("SRB_BUILD_LEDGER", str(WORK / "builds.json")))

#: The front end. Both states answer it; neither backend is named.
#
#: There is no `--sdist` here, and its absence is a decision rather than a gap.
#: meson-python implements `build_sdist` by calling `meson dist`, which refuses to
#: run outside a Git or Mercurial checkout, and the delivered snapshot carries no
#: VCS metadata at all. So `python -m build --sdist` cannot succeed for any Meson
#: submission in this environment, whatever the submission did -- which makes it a
#: fact about the toolchain and the delivery, not a measurement of the migration.
#: instruction.md says the same thing to the agent, in the out-of-scope list.
BUILD_ARGV = [sys.executable, "-m", "build", "--wheel", "--no-isolation"]

#: Reproducibility for anything the backend stamps into the archive.
SOURCE_DATE_EPOCH = "1704758400"


@dataclass(frozen=True)
class Config:
    """One named build. `rejects` is what the compiler pretends not to support."""

    name: str
    rejects: tuple[str, ...] = ()
    about: str = ""


#: The matrix. `default` is the one every artefact comparison grades against; the
#: three crippled configurations are the probe-honesty measurement, and `repeat` is
#: the same inputs a second time, which is what makes a difference between the
#: others attributable to the change rather than to the build.
MATRIX: tuple[Config, ...] = (
    Config("default", about="the wheel, built as shipped"),
    Config("repeat", about="the same sources built a second time, in a separate tree"),
    Config("no-aesni", rejects=("-maes",), about="compiler rejects -maes"),
    Config("no-clmul", rejects=("-mpclmul", "-mssse3"), about="compiler rejects -mpclmul/-mssse3"),
    Config("no-isa", rejects=("-maes", "-mpclmul", "-mssse3"), about="compiler rejects all three"),
)


@dataclass
class Build:
    """The record one configuration leaves behind."""

    name: str
    ok: bool = False
    rc: int | None = None
    tree: str = ""
    wheel: str = ""
    install: str = ""
    log: str = ""
    rejects: list[str] = field(default_factory=list)
    seconds: float = 0.0
    note: str = ""


# --------------------------------------------------------------------------- #
# running things
# --------------------------------------------------------------------------- #

def run(argv, *, cwd=None, env=None, log: Path | None = None, timeout: int = 2400):
    """Run argv, append everything to `log`, return the exit status.

    Never raises. A timeout is 124 and a missing executable is 127, matching the
    shell, so a caller can tell "the build failed" from "there was nothing to
    run" without catching anything.
    """
    started = time.time()
    header = f"$ {' '.join(str(a) for a in argv)}\n"
    try:
        p = subprocess.run(
            [str(a) for a in argv],
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        out, rc = p.stdout.decode("utf-8", "replace"), p.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.output or b"").decode("utf-8", "replace") + f"\n[timed out after {timeout}s]\n"
        rc = 124
    except OSError as exc:
        out, rc = f"[OSError: {exc}]\n", 127
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(header + out + f"[rc={rc} in {time.time() - started:.1f}s]\n\n")
    return rc


def base_env(extra: dict | None = None) -> dict:
    """A build environment with nothing of the harness left in it.

    SRB_* is removed because the submission's build runs inside it and must not
    be able to read the role it is playing or where the other tree is. The
    scrub is by exact prefix over a copy, not a filtered PATH: a build that
    cannot find gcc is a different failure from a build that read SRB_ORIGINAL.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SRB_")}
    env.update(
        SOURCE_DATE_EPOCH=SOURCE_DATE_EPOCH,
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONHASHSEED="0",
        LC_ALL="C.UTF-8",
        TZ="UTC",
    )
    env.pop("PYTHONPATH", None)  # the suite's lib/ is not the build's business
    if extra:
        env.update(extra)
    return env


# --------------------------------------------------------------------------- #
# the crippled compiler
# --------------------------------------------------------------------------- #

_WRAPPER = """#!/bin/sh
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


def make_cc_wrapper(directory: Path, rejects, real: str = "") -> Path:
    """Write a cc/gcc shim that rejects `rejects` and self-test it.

    The self-test is not defensive padding. A shim with a shell syntax error
    still exists and is still executable, and a build system asked to use it
    reports `Unknown compiler` -- which arrives in the log looking exactly like
    a submission whose build is broken. So the shim has to answer --version and
    compile a trivial program *before* any configuration is allowed to use it,
    and a shim that cannot do both raises here, where it is a harness error.

    The rejection is per-argument rather than per-pattern for the same reason
    State A's probe needs it: pycryptodome asks for `-mpclmul -mssse3` as a
    pair, and a real gcc stops at the first option it does not recognise. A shim
    that only fires when it sees both would let a build that passes them
    separately look supported.
    """
    real = real or shutil.which("gcc") or "/usr/bin/cc"
    directory.mkdir(parents=True, exist_ok=True)
    cases = "\n".join(
        f"    {flag}) echo \"error: unrecognized command-line option '{flag}'\" >&2; exit 1;;"
        for flag in rejects
    )
    body = _WRAPPER.format(flags=" ".join(rejects) or "nothing", cases=cases, real=real)
    cc = directory / "gcc"
    cc.write_text(body, encoding="utf-8")
    cc.chmod(0o755)
    for alias in ("cc", "x86_64-linux-gnu-gcc"):
        link = directory / alias
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to("gcc")

    probe = subprocess.run([str(cc), "--version"], capture_output=True, timeout=60)
    if probe.returncode != 0:
        raise RuntimeError(
            f"compiler shim in {directory} cannot answer --version "
            f"(rc={probe.returncode}): {probe.stderr.decode('utf-8', 'replace')[:400]}"
        )
    src = directory / "_shimcheck.c"
    src.write_text("int main(void){return 0;}\n", encoding="utf-8")
    compiled = subprocess.run(
        [str(cc), str(src), "-o", str(directory / "_shimcheck")], capture_output=True, timeout=120
    )
    if compiled.returncode != 0:
        raise RuntimeError(
            f"compiler shim in {directory} cannot compile a trivial program: "
            f"{compiled.stderr.decode('utf-8', 'replace')[:400]}"
        )
    for flag in rejects:
        rejected = subprocess.run(
            [str(cc), flag, str(src), "-o", str(directory / "_shimcheck")],
            capture_output=True,
            timeout=120,
        )
        if rejected.returncode == 0:
            raise RuntimeError(f"compiler shim in {directory} accepted {flag}, which it must reject")
    return cc


def with_wrapper(env: dict, wrapper_dir: Path) -> dict:
    """Put the shim first on PATH and name it as CC.

    Both, because a build system may honour either: `CC` is the documented
    channel and PATH is what a probe that shells out to `cc` finds.

    Raises if handed the compiler instead of the directory holding it. That is a
    one-character caller mistake whose symptom is `Unknown compiler` in the build
    log -- indistinguishable, from the report, from a submission that cannot
    build. Cheaper to refuse here than to debug there.
    """
    if not wrapper_dir.is_dir():
        raise RuntimeError(
            f"with_wrapper wants the directory holding the shim, got {wrapper_dir} "
            f"({'a file' if wrapper_dir.is_file() else 'nonexistent'})"
        )
    env = dict(env)
    env["PATH"] = f"{wrapper_dir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["CC"] = str(wrapper_dir / "gcc")
    return env


# --------------------------------------------------------------------------- #
# one configuration, start to finish
# --------------------------------------------------------------------------- #

def _newest_wheel(outdir: Path) -> Path | None:
    wheels = sorted(outdir.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    return wheels[-1] if wheels else None


def build_one(source: Path, config: Config, root: Path, *, timeout: int = 2400) -> Build:
    """Copy `source`, build a wheel from the copy, install it, record all of it.

    The copy exists because a build writes into its tree -- .mesonpy-*,
    build/, *.egg-info -- and four configurations sharing one tree would let the
    first one's leftovers decide the fourth one's result. It also means the
    delivered tree the other modules read is never the tree anything was built
    in.
    """
    out = root / config.name
    if out.exists():
        shutil.rmtree(out)
    tree = out / "src"
    log = out / "build.log"
    rec = Build(name=config.name, tree=str(tree), log=str(log), rejects=list(config.rejects))
    started = time.time()

    shutil.copytree(source, tree, symlinks=True)
    env = base_env()
    if config.rejects:
        # `make_cc_wrapper` returns the compiler, `with_wrapper` wants the directory
        # it lives in. Kept as two statements because collapsing them reads fine and
        # is wrong: it puts a *file* on PATH and sets CC to `<file>/gcc`, and meson
        # answers that with `Unknown compiler`, which arrives in the log looking
        # exactly like a submission whose build is broken.
        wrapper_dir = out / "cc"
        make_cc_wrapper(wrapper_dir, config.rejects)
        env = with_wrapper(env, wrapper_dir)

    dist = out / "dist"
    rec.rc = run(BUILD_ARGV + ["-o", str(dist)], cwd=tree, env=env, log=log, timeout=timeout)
    wheel = _newest_wheel(dist) if dist.is_dir() else None
    if wheel is None:
        rec.note = "the build produced no wheel"
        rec.seconds = time.time() - started
        return rec
    rec.wheel = str(wheel)

    target = out / "install"
    rec.rc = run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--no-compile", "--target", str(target), str(wheel)],
        cwd=out, env=env, log=log, timeout=900,
    )
    if rec.rc == 0:
        rec.install = str(target)
        rec.ok = True
    else:
        rec.note = "the wheel did not install"
    rec.seconds = time.time() - started
    return rec


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #

class Ledger:
    """What the `build` module published, read by every module after it.

    `need(name)` is the accessor the modules use, and it raises with the
    configuration's own note and log path rather than returning None. A check
    that cannot find its tree has to *fail* naming the configuration -- skipping
    would score a submission that never built the same as one that built
    perfectly, and this suite is the part of the ladder that is allowed to say
    "the wheel is not there".
    """

    def __init__(self, payload: dict):
        self.payload = payload
        self.builds = {name: Build(**rec) for name, rec in payload.get("builds", {}).items()}

    @classmethod
    def load(cls, path: Path | str = LEDGER_PATH) -> "Ledger":
        path = Path(path)
        if not path.is_file():
            return cls({"builds": {}, "note": f"no ledger at {path}"})
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def get(self, name: str) -> Build | None:
        return self.builds.get(name)

    def need(self, name: str) -> Build:
        rec = self.builds.get(name)
        if rec is None:
            have = ", ".join(sorted(self.builds)) or "nothing"
            raise AssertionError(
                f"configuration {name!r} is not in the build ledger (have: {have}). "
                f"{self.payload.get('note', '')}".strip()
            )
        if not rec.ok:
            raise AssertionError(
                f"configuration {name!r} did not build: {rec.note or 'no note'} "
                f"(rc={rec.rc}, log: {rec.log})"
            )
        return rec

    def write(self, path: Path | str = LEDGER_PATH) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.payload, indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def publish(cls, root: Path, records) -> "Ledger":
        return cls({
            "schema": "swerefactor.build-ledger/1",
            "root": str(root),
            "front_end": " ".join(BUILD_ARGV[1:]),
            "builds": {r.name: asdict(r) for r in records},
        })
