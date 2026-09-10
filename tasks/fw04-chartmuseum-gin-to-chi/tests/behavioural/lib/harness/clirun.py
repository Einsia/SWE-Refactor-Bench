"""Execute a :class:`~harness.cli.CliCase` and reduce its output to comparable form.

Separate from :mod:`harness.cli` for the same reason :mod:`harness.wire` is
separate from :mod:`harness.corpus`: the corpus files are data, and keeping
execution out of them means a case can be read as a statement of what was asked
rather than of what happens.

The reductions here mirror :mod:`harness.normalize`'s job for HTTP responses, and
exist for one reason: State A's stdout carries text that a correct migration is
not obliged to reproduce character for character, next to text it is. Comparing
the whole stream would fail correct ports; comparing nothing would grade nothing.
So each ``stdout_mode`` names which part is the contract.

``flaglist`` is the sharpest case. ``--help`` is rendered by urfave/cli, which
aligns the usage column to the longest flag name -- so adding one flag reflows
every line in the block, and a byte comparison would report 60 failures for one
change. What matters is that the same flags exist, take the same arguments, and
carry the same descriptions. That is what this extracts.
"""

from __future__ import annotations

import os
import re
import subprocess

from harness import normalize
from harness.cli import CliCase
from harness.launch import FIXTURES, free_port, seed_storage

from swerefactor.contract import submission_env

#: A run that has not finished by now is a hang, not a slow start.
RUN_TIMEOUT_S = 60.0

#: How long a ``serves=True`` case is given to prove it stays up. Long enough that
#: a slow boot on a loaded box is not mistaken for a crash; short enough that 20
#: such cases do not dominate the capture.
SERVE_GRACE_S = 3.0

#: ``--version`` prints "x.y.z (build <revision>)". The revision is baked in with
#: -ldflags at build time and differs between the oracle and any rebuild, so it is
#: masked; the version and the shape of the line are the contract.
_VERSION_RE = re.compile(r"\(build\s+[^)]*\)")

#: One entry in urfave/cli's flag block: leading whitespace, the flag spelling
#: (possibly "--a, -b"), an optional argument placeholder, then the usage text.
_FLAG_LINE = re.compile(r"^\s+(--?[^\s,]+(?:,\s*--?[^\s,]+)*)\s*(\S*)\s*(.*)$")


def _seed_root(case: CliCase, rundir: str) -> str:
    root = os.path.join(rundir, "storage")
    if os.path.isdir(root):
        import shutil
        shutil.rmtree(root)
    seed_storage(root, case.seeds)
    return root


def _write_config(case: CliCase, rundir: str) -> str | None:
    if case.config_file is None:
        return None
    path = os.path.join(rundir, case.config_name)
    with open(path, "w") as fh:
        fh.write(case.config_file)
    return path


def _clean_env(case: CliCase) -> dict:
    """The process environment: the harness's own, minus anything ChartMuseum reads.

    Every ChartMuseum flag has an EnvVar counterpart, so a variable left over from
    the surrounding shell would configure the binary behind the case's back -- and
    would do it identically during capture and grading, making the resulting
    expectation quietly wrong rather than visibly broken. The case's ``env`` is
    then the only environment it has.
    """
    env = submission_env()
    for k in list(env):
        if k.startswith(("CM_", "STORAGE", "BASIC_AUTH", "BEARER", "DEPTH",
                         "CONTEXT_PATH", "CHART_", "INDEX_", "TLS_", "PORT",
                         "ALLOW_", "DISABLE_", "AUTH_", "LOG_", "DEBUG",
                         "ARTIFACT_HUB", "CACHE", "MAX_", "PER_CHART",
                         "WEB_TEMPLATE", "ENFORCE_", "ANONYMOUS_", "CORS_")):
            env.pop(k, None)
    env.update(dict(case.env))
    return env


def reduce_stdout(mode: str, text: str) -> dict:
    """Reduce stdout to the form ``mode`` says is the contract."""
    if mode == "ignore":
        return {}
    if mode == "exact":
        return {"text": normalize.scrub_text(text)}
    if mode == "version":
        return {"version_line": _VERSION_RE.sub("(build <rev>)", text.strip())}
    if mode == "index":
        # Same treatment as an index.yaml served over HTTP: the masked text
        # catches a serialisation change, the parsed structure catches a semantic
        # one and names the field.
        return {
            "index_text": normalize.mask_index_text(text),
            "index": normalize.parse_index(text.encode()),
        }
    if mode == "flaglist":
        return {"flags": extract_flags(text)}
    raise ValueError(f"unknown stdout_mode {mode!r}")


def extract_flags(text: str) -> list[dict]:
    """Pull (spelling, argument shape, usage) out of a urfave/cli help screen.

    Sorted by spelling, because the block's order is a rendering detail while its
    contents are the CLI surface. Lines that continue a previous flag's usage text
    are folded into it rather than dropped -- several ChartMuseum flags document
    their environment variable on a continuation line, and losing that would stop
    the suite noticing if a port forgot one.
    """
    flags: list[dict] = []
    for raw in text.splitlines():
        if not raw.strip() or not raw.startswith((" ", "\t")):
            continue
        m = _FLAG_LINE.match(raw)
        if not m:
            if flags:
                flags[-1]["usage"] = f"{flags[-1]['usage']} {raw.strip()}".strip()
            continue
        spelling, arg, usage = m.group(1), m.group(2), m.group(3)
        if not spelling.startswith("-"):
            continue
        names = tuple(s.strip() for s in spelling.split(","))
        flags.append({"names": list(names), "arg": arg, "usage": usage.strip()})
    # Fold duplicates that a reflow could have split, then sort.
    flags.sort(key=lambda f: f["names"][0])
    return flags


def run_case(case: CliCase, binary: str, rundir: str) -> dict:
    """Run one CLI case; return its recorded form.

    ``stdout_raw`` and ``stderr_raw`` are included so the caller can assert the
    needles against both streams. :mod:`harness.capture` drops them before writing
    the golden file: raw output is full of timestamps and reflowing help screens,
    and keeping it would put per-run noise into a file whose whole purpose is to
    hold only what is stable.

    ``needles_on`` is the reduced form that survives: for each needle, the sorted
    list of streams that carried it. Measured, never declared -- see the module
    docstring in :mod:`harness.cli` for why the stream cannot be guessed.
    """
    os.makedirs(rundir, exist_ok=True)
    root = _seed_root(case, rundir)
    conf = _write_config(case, rundir)

    argv = [binary]
    for a in case.argv:
        a = a.replace("{root}", root)
        if "{conf}" in a:
            if conf is None:
                raise ValueError(f"{case.id}: argv uses {{conf}} with no config_file")
            a = a.replace("{conf}", conf)
        argv.append(a)

    if case.serves:
        # The launcher owns the port here exactly as it does for an HTTP profile,
        # so two cases can never contend for one socket.
        argv.append(f"--port={free_port()}")

    env = _clean_env(case)
    # argv is recorded as the case *declared* it, not as it was expanded. The
    # expansion carries a per-run scratch directory and a per-run free port, so a
    # literal record would be volatile in every case that uses either -- and argv
    # is an input, not an observation: nothing about the port under test is
    # learned from it. Keeping the placeholders also makes the golden file
    # readable, since ``--storage-local-rootdir={root}`` says what the flag is for
    # in a way ``/tmp/srb-fw04-capture-8x1k2/cli/boots-plain/run2/storage`` does
    # not.
    declared = list(case.argv)
    if case.serves:
        declared.append("--port={free}")
    rec: dict = {"argv": declared, "mode": case.stdout_mode}

    if case.serves:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=rundir, env=env,
                                start_new_session=True)
        try:
            proc.wait(timeout=SERVE_GRACE_S)
            # Exited on its own: this configuration does NOT serve. That is a real
            # observation, recorded rather than raised, because whether a given
            # flag combination boots is exactly what these cases are asking.
            out, err = proc.communicate()
            rec["served"] = False
            rec["exit"] = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            rec["served"] = True
            rec["exit"] = None
    else:
        proc = subprocess.run(argv, capture_output=True, cwd=rundir, env=env,
                              timeout=RUN_TIMEOUT_S)
        out, err = proc.stdout, proc.stderr
        rec["served"] = False
        rec["exit"] = proc.returncode

    stdout = out.decode("utf-8", "replace")
    stderr = err.decode("utf-8", "replace")
    rec.update(reduce_stdout(case.stdout_mode, stdout))
    if case.stderr_mode == "exact":
        rec["stderr"] = normalize.scrub_text(stderr)
    if case.needles:
        rec["needles_on"] = {
            n: [s for s, text in (("stdout", stdout), ("stderr", stderr)) if n in text]
            for n in case.needles
        }
    rec["stdout_raw"] = stdout
    rec["stderr_raw"] = stderr
    return rec
