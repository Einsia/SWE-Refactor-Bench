"""Running one case against one pair of binaries.

This is the only place a case gets turned into a process.  The freeze step uses it
with the reference binaries, the grading step uses it with the submission's
published binaries, and the authoring probes in _work/ import it too -- so a probe
measures the same execution the verifier performs, and there is no second
implementation to drift.

Nothing here knows what the right answer is.  It materializes a directory, runs a
process, and reports what happened byte for byte.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

# Cases carry environment settings as a pseudo-argument rather than a Case field,
# because argv is part of the case key and an env dict would have to be folded
# into that hash separately.  The prefix is stripped here, in one place.
ENV_PREFIX = "@env:"

DEFAULT_TIMEOUT = 30.0

# The uid submission-authored processes run as, defined here because this module is
# the one that stays stdlib-only -- the authoring probes import it directly, without
# the rest of the suite.  vlib imports these two names rather than restating them:
# two copies of a uid is two things that can disagree, and the disagreement would be
# a boundary that holds in one half of the grade and not the other.
UNPRIV_UID = 65534
UNPRIV_GID = 65534


def _traversable(path) -> None:
    """+x for UNPRIV_UID on every directory above `path`.

    Owning the case directory is not enough to reach it: tempfile.mkdtemp() creates
    0700 root-owned parents, and a child that cannot traverse them fails in ways that
    look like the program is broken rather than unreachable -- a missing input file,
    or a cwd that does not exist.  Directories only, +x only: enough to walk to what
    the child was handed, not enough to list anything on the way.

    g+x as well as o+x: POSIX stops at the first class whose owner/group matches, so
    a child in group root gets the group bits of a 0701 root:root directory and never
    reaches the `other` bits that were set for it.
    """
    cur = os.path.dirname(os.path.abspath(path))
    while cur and cur != os.sep:
        try:
            mode = os.stat(cur).st_mode & 0o7777
            if mode & 0o011 != 0o011:
                os.chmod(cur, mode | 0o011)
        except OSError:
            pass
        cur = os.path.dirname(cur)


def _grant(path) -> None:
    """Hand `path` and everything under it to UNPRIV_UID.

    A case's directory is created by root (tempfile, as the grading process) and
    written by the dropped child, so ownership has to move before the child starts.
    Failures are ignored per-entry for the reason given in vlib.grant_unprivileged.
    """
    _traversable(path)
    try:
        os.chown(path, UNPRIV_UID, UNPRIV_GID)
    except OSError:
        return
    if not os.path.isdir(path):
        return
    for dirpath, dirnames, names in os.walk(path):
        for n in dirnames + names:
            try:
                os.chown(os.path.join(dirpath, n), UNPRIV_UID, UNPRIV_GID,
                         follow_symlinks=False)
            except OSError:
                pass

#: The environment every case runs under, on both sides of the comparison.
#:
#: Fixed rather than inherited.  The reference's expectations are frozen during
#: the image build and the submission is measured months later under a runner that
#: exports its own variables; if a case inherited the ambient environment, the two
#: sides would differ by construction and the difference would be invisible.  So
#: neither side sees the ambient environment at all.
#:
#: PATH and HOME are here because a published .NET program may be launched as
#: `dotnet foo.dll`, and the DOTNET_* entries because the CLI writes to $HOME on
#: first run and prints a telemetry banner to stdout -- which would be graded as
#: the program's output.  The original C++ binaries need none of it and are
#: unaffected by its presence, which is what makes one table usable for both.
BASE_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/tmp/case-home",
    "LC_ALL": "C",
    "LANG": "C",
    "TZ": "UTC",
    "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    "DOTNET_NOLOGO": "1",
    "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
    "DOTNET_CLI_HOME": "/tmp/case-home",
    "NUGET_PACKAGES": "/opt/nuget-offline",
    "TERM": "dumb",
}

# rc for "the process did not exit on its own".  Distinct from any real exit
# status so a timeout can never be confused with a graded failure.
RC_TIMEOUT = -1000


@dataclass
class Outcome:
    """What a process did.  Bytes, not text: encoding is a graded behavior."""
    rc: int
    stdout: bytes
    stderr: bytes
    # Files present after the run that were not written before it, as
    # relpath -> contents.  Sorted for determinism.  This is how -o, -m and
    # jsonnetfmt -i are graded: their observable effect is on the filesystem.
    created: dict[str, bytes] = field(default_factory=dict)
    # Inputs the run deleted.  A separate field rather than a sentinel value in
    # `created`, because any byte string chosen as a sentinel could also be a
    # file's real contents.  The reference never deletes an input, so this is
    # always empty upstream -- which is exactly what makes it worth recording:
    # otherwise "the input survived" is an assumption, and the cases whose
    # expected output is empty (jsonnetfmt -i on an already-clean file,
    # --test on a clean file) are passed by a port that removes its input.
    removed: tuple[str, ...] = ()
    timed_out: bool = False

    @property
    def aborted(self) -> bool:
        """Killed by a signal.  An abort is not a behavior and is never graded."""
        return self.rc < 0 and not self.timed_out


def split_env(argv: tuple[str, ...]) -> tuple[list[str], dict[str, str]]:
    """Separate real arguments from @env: pseudo-arguments."""
    real: list[str] = []
    env: dict[str, str] = {}
    for a in argv:
        if a.startswith(ENV_PREFIX):
            k, _, v = a[len(ENV_PREFIX):].partition("=")
            env[k] = v
        else:
            real.append(a)
    return real, env


def materialize(case, dest: str, upstream_root: str | None = None) -> set[str]:
    """Build the case's directory.  Returns the set of paths present afterwards.

    The upstream files land first and the case's own files second, so a case may
    deliberately shadow one.  The source_dir is copied from `upstream_root`, which the verifier
    points at its *own* extraction of State A -- never at the submission, or
    editing test_suite/ would edit the tests.
    """
    if case.source_dir:
        if not upstream_root:
            raise ValueError(
                f"case {case.cid} needs source_dir {case.source_dir!r} but no "
                f"upstream_root was given")
        src = os.path.join(upstream_root, case.source_dir)
        if not os.path.isdir(src):
            raise FileNotFoundError(f"source_dir {src} is missing")
        # dirs_exist_ok because dest already exists (mkdtemp made it).
        shutil.copytree(src, dest, dirs_exist_ok=True, symlinks=True)

    for path, content in case.files:
        full = os.path.join(dest, path)
        os.makedirs(os.path.dirname(full) or dest, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    return _snapshot(dest)


def _snapshot(root: str) -> set[str]:
    out = set()
    for dirpath, _, names in os.walk(root):
        for n in names:
            out.add(os.path.relpath(os.path.join(dirpath, n), root))
    return out


def run(case, bins: dict[str, str], *, upstream_root: str | None = None,
        workdir: str | None = None, timeout: float = DEFAULT_TIMEOUT,
        keep: bool = False, unprivileged: bool = False) -> Outcome:
    """Materialize `case`, run it, and report the outcome.

    `bins` maps a binary name ("jsonnet", "jsonnetfmt") to an executable path, so
    the same case runs against the reference or against a submission with no other
    difference.

    `unprivileged=True` runs the binary as UNPRIV_UID so it cannot read
    /opt/assets/expectations.json.  Off by default because this module is shared
    with freeze.py, which runs the *reference* at image-build time -- there is no
    answer key to protect yet, and the reference tree is root-owned.  Grading turns
    it on; see the note in driver.py where it does.
    """
    parent = workdir or tempfile.mkdtemp(prefix="case-")
    d = tempfile.mkdtemp(dir=parent)
    try:
        before = materialize(case, d, upstream_root)
        argv, extra_env = split_env(case.argv)

        # BASE_ENV, not os.environ: see the comment on BASE_ENV.  The case's own
        # @env: settings go on top, which is how the JSONNET_PATH cases work.
        env = dict(BASE_ENV)
        env.update(extra_env)
        os.makedirs(env["HOME"], exist_ok=True)

        priv: dict = {}
        if unprivileged:
            if os.geteuid() != 0:
                raise RuntimeError(
                    f"unprivileged=True needs root to drop from, running as uid "
                    f"{os.geteuid()}")
            # extra_groups=[] drops root's supplementary groups.  subprocess does not
            # call setgroups on its own, so without it the case runs in group root and
            # can read anything root-group readable -- including the answer key.
            priv = {"user": UNPRIV_UID, "group": UNPRIV_GID, "extra_groups": []}
            # The case's own directory and HOME have to be writable by the dropped
            # uid: cases create files, and `jsonnetfmt -i` rewrites its input in
            # place.  Grading stays root and reads the results back afterwards, so
            # the created/removed comparison below is unaffected.
            _grant(d)
            _grant(env["HOME"])

        # A bins value is normally a path, but it may be a list when the binary
        # needs a launcher -- `dotnet Foo.dll` for a publish with no apphost.  A
        # bare string is never split on whitespace, because a path may contain a
        # space and silently mis-splitting it would look like a broken submission.
        exe = bins[case.binary]
        launcher = list(exe) if isinstance(exe, (list, tuple)) else [exe]
        try:
            p = subprocess.run(
                launcher + argv, cwd=d, env=env, timeout=timeout,
                input=(case.stdin or "").encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **priv)
            rc, so, se, timed_out = p.returncode, p.stdout, p.stderr, False
        except subprocess.TimeoutExpired as e:
            rc, timed_out = RC_TIMEOUT, True
            so = e.stdout or b""
            se = e.stderr or b""

        created = {}
        for rel in sorted(_snapshot(d) - before):
            with open(os.path.join(d, rel), "rb") as f:
                created[rel] = f.read()
        # jsonnetfmt -i rewrites an existing file rather than creating one, so
        # also report inputs whose contents changed -- and inputs that are gone.
        removed: list[str] = []
        for path, content in case.files:
            full = os.path.join(d, path)
            if not os.path.exists(full):
                removed.append(path)
                continue
            with open(full, "rb") as f:
                now = f.read()
            if now != content.encode("utf-8"):
                created[path] = now

        return Outcome(rc=rc, stdout=so, stderr=se, created=created,
                       removed=tuple(sorted(removed)), timed_out=timed_out)
    finally:
        if not keep:
            shutil.rmtree(d, ignore_errors=True)
            if workdir is None:
                shutil.rmtree(parent, ignore_errors=True)


def run_all(cases, bins: dict[str, str], *, upstream_root: str | None = None,
            timeout: float = DEFAULT_TIMEOUT, on_result=None,
            unprivileged: bool = False):
    """Run many cases in one temporary parent directory.

    Yields (case, Outcome).  `on_result` is called with each pair as it lands, for
    progress reporting on a sweep that takes minutes.
    """
    parent = tempfile.mkdtemp(prefix="sweep-")
    try:
        # The shared parent must be traversable by the dropped uid, or every case
        # below fails to reach its own directory.  0o711 rather than a chown: root
        # keeps ownership of the sweep root and the child only needs to walk through.
        if unprivileged:
            os.chmod(parent, 0o711)
        for c in cases:
            outcome = run(c, bins, upstream_root=upstream_root, workdir=parent,
                          timeout=timeout, unprivileged=unprivileged)
            if on_result is not None:
                on_result(c, outcome)
            yield c, outcome
    finally:
        shutil.rmtree(parent, ignore_errors=True)
