"""What a publish would actually ship, installed somewhere else and started there.

Everything else in this stage runs the submission out of a directory that contains
the whole repository. A published package is not that: it is whatever ``npm pack``
decided to include, unpacked into someone else's ``node_modules`` with only its
runtime dependencies beside it.

Two defects live in the gap and are invisible everywhere else:

*   a file the package forgot to include -- a ``files`` list that misses the
    static directory, or the launcher, or a module added during the migration;
*   a package needed at runtime but declared under ``devDependencies``, which is
    installed in the working tree and is not installed for a consumer.

So: pack it, install the tarball into an empty directory, and serve from there.
Nothing from the working tree, nothing from the build directory, no path outside
the fresh install. If it answers, the package is publishable.
"""

from __future__ import annotations

import json as jsonlib
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from srbfixtures import BUILD, SEED, WORK

from swerefactor.contract import submission_env

pytestmark = [pytest.mark.behaviour, pytest.mark.slow]

NPM = os.environ.get("SRB_NPM", "npm")
READY_TIMEOUT = 45.0


def scratch(name: str) -> Path:
    base = WORK if WORK.is_dir() else Path(tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix=f"release-{name}-", dir=str(base)))
    return path


def npm(args: list[str], cwd: Path, timeout: int = 900):
    return subprocess.run([NPM, *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout, check=False)


class Release:
    """A packed, installed and started copy of the submission."""

    def __init__(self):
        self.tarball: Path | None = None
        self.pack_output = ""
        self.install_output = ""
        self.root: Path | None = None       # the fresh install directory
        self.package: Path | None = None    # node_modules/<name> inside it
        self.name = ""
        self.failure: str | None = None
        self.proc = None
        self.port = 0
        self.logpath: Path | None = None

    def log(self) -> str:
        if self.logpath and self.logpath.exists():
            return self.logpath.read_text(errors="replace")[-8000:]
        return ""

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def stop(self) -> None:
        import signal
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


def build_release() -> Release:
    """Pack, install into an empty directory, start from there.

    Every step records what it did, and a step that fails leaves ``failure`` set
    rather than raising, so each assertion below can report the step it depends on
    instead of forty tests erroring out on the same missing tarball.
    """
    rel = Release()
    manifest = jsonlib.loads((BUILD / "package.json").read_text())
    rel.name = manifest.get("name") or ""

    # -- pack ------------------------------------------------------------
    # Packed from the *built* tree, because a publish runs the build first. The
    # build directory is not otherwise involved: npm pack never includes
    # node_modules, and what comes out is the file list the package declares.
    out = scratch("pack")
    packed = npm(["pack", "--pack-destination", str(out)], BUILD)
    rel.pack_output = (packed.stdout + packed.stderr)[-4000:]
    if packed.returncode != 0:
        rel.failure = f"npm pack exited {packed.returncode}"
        return rel
    tarballs = sorted(out.glob("*.tgz"))
    if not tarballs:
        rel.failure = "npm pack produced no tarball"
        return rel
    rel.tarball = tarballs[0]

    # -- install into an empty directory ---------------------------------
    root = scratch("install")
    (root / "package.json").write_text(jsonlib.dumps(
        {"name": "srb-release-consumer", "version": "0.0.0", "private": True},
        indent=2))
    installed = npm(["install", str(rel.tarball)], root, timeout=1200)
    rel.install_output = (installed.stdout + installed.stderr)[-4000:]
    if installed.returncode != 0:
        rel.failure = f"installing the tarball exited {installed.returncode}"
        rel.root = root
        return rel
    rel.root = root
    if rel.name:
        candidate = root / "node_modules" / rel.name
        if candidate.is_dir():
            rel.package = candidate
    if rel.package is None:
        rel.failure = (f"the tarball installed but {rel.name!r} is not under "
                       f"{root / 'node_modules'}")
    return rel


def start_release(rel: Release) -> None:
    """Start the installed copy, from inside the fresh install."""
    if rel.failure or rel.package is None:
        return
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        rel.port = sock.getsockname()[1]

    argv: list[str] | None = None
    launcher = rel.package / os.environ.get("SRB_LAUNCHER", "serve.sh")
    db = (rel.root or Path(tempfile.gettempdir())) / "release-db.json"
    db.write_text(SEED.read_text())
    seed = (rel.root or Path(tempfile.gettempdir())) / "release-seed.json"
    seed.write_text(SEED.read_text())

    env = submission_env()
    env["NODE_ENV"] = ""
    if launcher.is_file() and os.access(launcher, os.X_OK):
        argv = [str(launcher)]
        env.update({"JSON_SERVER_SEED": str(seed), "JSON_SERVER_DB": str(db),
                    "JSON_SERVER_PORT": str(rel.port),
                    "JSON_SERVER_HOST": "127.0.0.1"})
    else:
        manifest = jsonlib.loads((rel.package / "package.json").read_text())
        bin_field = manifest.get("bin")
        rel_path = None
        if isinstance(bin_field, str):
            rel_path = bin_field
        elif isinstance(bin_field, dict) and bin_field:
            rel_path = sorted(bin_field.items())[0][1]
        if rel_path is None:
            rel.failure = ("the installed package ships neither an executable "
                           "launcher nor a bin, so a consumer cannot start it")
            return
        argv = ["node", str(rel.package / rel_path), "--host", "127.0.0.1",
                "--port", str(rel.port), str(db)]

    rel.logpath = (rel.root or Path(tempfile.gettempdir())) / "release.log"
    with open(rel.logpath, "wb") as log:
        rel.proc = subprocess.Popen(
            argv, cwd=str(rel.package), env=env, stdout=log,
            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True)

    deadline = time.monotonic() + READY_TIMEOUT
    last = ""
    while time.monotonic() < deadline:
        if rel.proc.poll() is not None:
            rel.failure = (f"the installed copy exited with "
                           f"{rel.proc.returncode} before answering")
            return
        try:
            status, _h, _b = rel.get("/posts")
            if status < 500:
                return
            last = f"GET /posts -> {status}"
        except OSError as exc:
            last = str(exc)
        time.sleep(0.25)
    rel.failure = (f"the installed copy never answered on {rel.port} within "
                   f"{READY_TIMEOUT:.0f}s (last: {last})")


@pytest.fixture(scope="module")
def release(request) -> Release:
    rel = build_release()
    start_release(rel)
    request.addfinalizer(rel.stop)
    return rel


def ready(rel: Release) -> Release:
    if rel.failure:
        pytest.fail(f"{rel.failure}\n\n--- npm pack ---\n{rel.pack_output}\n"
                    f"--- npm install ---\n{rel.install_output}\n"
                    f"--- server ---\n{rel.log()}")
    return rel


# ---------------------------------------------------------------------------
# The three steps
# ---------------------------------------------------------------------------

def test_the_package_can_be_packed(release):
    """``npm pack`` succeeds and produces a tarball."""
    assert release.tarball is not None, (
        f"{release.failure}\n--- npm pack ---\n{release.pack_output}")
    assert release.tarball.stat().st_size > 0, "the tarball is empty"


def test_the_tarball_installs_offline_into_an_empty_directory(release):
    """The consumer's install. Only runtime dependencies, only the mirror.

    ``npm install <tarball>`` installs the package's ``dependencies`` and not its
    ``devDependencies``, which is what makes this the check that finds a runtime
    need declared in the wrong table. The mirror is the same one the working-tree
    install used, so a failure here is about the manifest and not about the
    network.
    """
    assert release.package is not None, (
        f"{release.failure}\n--- npm install ---\n{release.install_output}")
    assert (release.package / "package.json").is_file()


def test_the_installed_copy_serves(release):
    """Started from inside the fresh install, and it answers."""
    ready(release)
    status, _headers, raw = release.get("/posts/1")
    assert status == 200, f"the published package answered {status}: {raw[:200]!r}"
    assert jsonlib.loads(raw)["id"] == 1


# ---------------------------------------------------------------------------
# What the published copy can and cannot do
# ---------------------------------------------------------------------------

def test_the_published_copy_answers_the_shapes_the_recording_covers(release):
    """A handful of representative reads, from the published copy.

    Not the whole recording -- that is the comparison modules' job against the
    working tree. This is looking for the file that did not ship, which shows up
    as one class of request failing while the rest are fine.
    """
    ready(release)
    for path, want in (("/posts", 200), ("/posts/1", 200), ("/comments", 200),
                       ("/posts?_page=1&_limit=2", 200), ("/db", 200),
                       ("/posts/999999", 404)):
        status, _headers, raw = release.get(path)
        assert status == want, (
            f"{path} answered {status}, expected {want}: {raw[:200]!r}")


def test_the_static_directory_was_included_in_the_package(release):
    """``GET /`` from the published copy.

    The static files are a directory of assets that a ``files`` list has to name
    explicitly. Forgetting it is the most common packaging mistake in a package
    like this one, it is invisible in the working tree, and it turns the landing
    page into a 404.
    """
    ready(release)
    status, headers, raw = release.get("/")
    assert status == 200, (
        f"GET / from the published package answered {status}. The static "
        f"directory was probably not included in the tarball.")
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "")
    assert "html" in ctype.lower(), f"GET / served {ctype!r}"
    assert raw, "GET / served an empty body"


def test_the_published_copy_can_write_and_read_back(release):
    """A write, from a copy that has only its runtime dependencies.

    The data layer is a runtime dependency, and it is the one most likely to have
    drifted into ``devDependencies`` during a migration that was iterating on the
    read path first.
    """
    ready(release)
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", release.port, timeout=15)
    try:
        conn.request("POST", "/posts",
                     body=jsonlib.dumps({"title": "published", "authorId": 1,
                                         "views": 1}),
                     headers={"Content-Type": "application/json",
                              "Host": f"127.0.0.1:{release.port}"})
        resp = conn.getresponse()
        created = resp.read()
        status = resp.status
    finally:
        conn.close()
    assert status == 201, f"POST answered {status}: {created[:200]!r}"
    new_id = jsonlib.loads(created)["id"]
    status, _headers, raw = release.get(f"/posts/{new_id}")
    assert status == 200, f"the written record was not readable back: {status}"


def test_the_published_copy_does_not_reach_back_into_the_working_tree(release):
    """The install is self-contained.

    A package that resolves something out of the repository it was built in works
    on the build machine and nowhere else. The check is structural: nothing the
    fresh install needs may live outside it, so no symlink under its
    ``node_modules`` may point out of the install root.
    """
    ready(release)
    root = release.root.resolve()
    escapes: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for entry in list(dirnames) + list(filenames):
            full = Path(dirpath) / entry
            if not full.is_symlink():
                continue
            try:
                target = full.resolve()
            except OSError:
                escapes.append(f"{full} -> (unresolvable)")
                continue
            if root not in target.parents and target != root:
                escapes.append(f"{full.relative_to(root)} -> {target}")
    # npm's own bin shims are relative and stay inside the root, so anything
    # pointing out is something the package arranged.
    assert not escapes, ("the fresh install depends on paths outside itself:\n  "
                         + "\n  ".join(escapes[:15]))


@pytest.mark.srb_weight(0.0)
def test_the_published_copy_does_not_announce_the_retired_framework(release):
    """Asked once more of the packaged artefact, which is what a user would run.

    Recorded at weight 0.0. State A's packaged artefact is Express, so it answers
    with `x-powered-by: Express` here as everywhere -- an absence State A cannot
    produce, which makes it a question about whether the migration happened rather
    than about behaviour a rewrite must preserve. Stage 1's `old_stack_retired`
    gate asks it over both trees, where a failure scores the submission zero.

    Still recorded, because of where it asks: `npm pack` output, installed fresh
    into a directory of its own. A tree that stripped the header from its source
    but shipped a `files` list that publishes something else answers the wire
    checks and shows up here.
    """
    ready(release)
    import re
    pattern = re.compile(r"\b(express|connect|middie|serve-static|body-parser)\b",
                         re.I)
    offences: list[str] = []
    for path in ("/posts", "/", "/posts/999999"):
        _status, headers, _raw = release.get(path)
        for name, value in headers.items():
            if pattern.search(name) or pattern.search(value):
                offences.append(f"{path}: {name}: {value}")
    assert not offences, ("the published package named the retired stack:\n  "
                          + "\n  ".join(offences))
