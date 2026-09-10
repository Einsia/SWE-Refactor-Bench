"""Replay the corpus against a running server and emit comparable records.

Used three ways, all the same code path so the agent grades against exactly what
the verifier grades against:

  * to CAPTURE the golden records from the State A oracle (--out golden.json)
  * by the verifier, against the submission, comparing to those records
  * by the agent, via swerefactor-diff, to see its own diffs before submitting

Usage:
    replay.py --bin ./server --out records.json [--profile default] [--case id]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import corpus  # noqa: E402
import normalize  # noqa: E402
import wire  # noqa: E402

FIXTURE_ROOT = os.environ.get("SRB_FIXTURE_ROOT", "/opt/fixtures/docroot")
FIXED_MTIME = os.environ.get("SRB_FIXTURE_MTIME", "2026-01-01 00:00:00 UTC")

# The nine fixture paths.  A response's Last-Modified is GRADED for these (the
# mtimes are stamped to a constant) and MASKED for anything the run created (those
# carry a wall clock).  Deriving it from this set rather than from a hand-kept
# list of case ids means a newly added upload case cannot accidentally grade a
# timestamp that moves.
FIXTURE_PATHS = {
    "a.txt", "sub/b.txt", "data.json", "page.html", "tiny.png",
    "README", "big.bin", "sp ace.txt", "odd+name.txt",
}


def seed_docroot(docroot: str) -> None:
    """Reset the docroot to the frozen fixtures with stamped mtimes.

    Copied WITHOUT -p and then stamped: git records no mtimes, so a checkout's
    times are whenever the clone happened.  Inheriting them would make
    Last-Modified and If-Modified-Since differ between the environment image and
    the verifier image, and the resulting failures would look like the agent's.
    """
    if os.path.isdir(docroot):
        shutil.rmtree(docroot)
    os.makedirs(os.path.dirname(docroot.rstrip("/")) or "/", exist_ok=True)
    shutil.copytree(FIXTURE_ROOT, docroot)
    subprocess.run(
        ["find", docroot, "-depth", "-exec", "touch", "-h", "-d", FIXED_MTIME,
         "{}", "+"],
        check=True, capture_output=True,
    )


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(port: int, proc, timeout: float = 20.0) -> None:
    """Readiness is OPTIONS /upload -> exactly 204.

    Not a GET: under -enable_auth an unauthenticated GET is a 401, which would
    make the probe fail on a perfectly healthy server.  handleOptions writes
    Access-Control-Allow-Methods before auth can refuse, so 204 holds in every
    profile.  And it must be exactly 204 -- a 404 from a mis-built binary is also
    a "response", so a probe that accepts any status proves only that something
    is listening.
    """
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited with {proc.returncode} before becoming ready")
        try:
            r = wire.request("127.0.0.1", port, "OPTIONS", "/upload", timeout=2.0)
            if r.status == 204:
                return
            last = f"OPTIONS /upload -> {r.status}"
        except (OSError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"server not ready after {timeout}s: {last}")


def target_is_fixture(target: str) -> bool:
    """Does this request target one of the frozen fixture files?"""
    path = target.split("?", 1)[0]
    if not path.startswith("/files/"):
        return False
    rel = urllib.parse.unquote(path[len("/files/"):])
    return rel in FIXTURE_PATHS


class Server:
    def __init__(self, binary: str, docroot: str, flags: list[str]):
        self.binary = binary
        self.docroot = docroot
        self.flags = flags
        self.port = free_port()
        self.proc = None
        self.log = None

    def __enter__(self):
        self.log = open(os.path.join(
            os.path.dirname(self.docroot.rstrip("/")), "server.log"), "wb")
        self.proc = subprocess.Popen(
            [self.binary, "-document_root", self.docroot,
             "-addr", f"127.0.0.1:{self.port}"] + self.flags,
            stdout=self.log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            wait_ready(self.port, self.proc)
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.proc.kill()
        if self.log:
            self.log.close()
        return False


def run_profile(binary: str, workdir: str, profile: str, cases: list[dict],
                verbose: bool = False) -> dict:
    docroot = os.path.join(workdir, "docroot")
    seed_docroot(docroot)
    out = {}
    with Server(binary, docroot, corpus.PROFILES[profile]) as srv:
        for c in cases:
            # A mutating case gets a clean tree first, so it observes the fixtures
            # and not whatever an earlier case left.  A continuation deliberately
            # does NOT re-seed: its whole purpose is to observe the previous step.
            if c["mutates"] and not corpus.is_continuation(c["id"]):
                seed_docroot(docroot)
            body = c["body"]
            if isinstance(body, str):
                body = body.encode()
            resp = wire.request(
                "127.0.0.1", srv.port, c["method"], c["target"],
                headers=dict(c["headers"]), body=body,
            )
            rec = normalize.record(
                resp, mask_last_modified=not target_is_fixture(c["target"]))
            rec["case"] = c["id"]
            rec["profile"] = profile
            rec["suite"] = c["suite"]
            out[c["id"]] = rec
            if verbose:
                print(f"  {c['id']:32s} {normalize.describe(rec, 90)}")
    return out

def replay_all(binary: str, workdir: str, profiles=None, case_ids=None,
               verbose: bool = False) -> dict:
    cases = corpus.all_cases()
    if case_ids:
        keep = set(case_ids)
        # Keep a continuation's parent too: a continuation observes state the
        # previous step created and is meaningless without it.
        keep |= {c["id"].split(".", 1)[0] for c in cases if c["id"] in keep}
        cases = [c for c in cases if c["id"] in keep]
    records = {}
    # Grouped by profile so each flag set costs one launch, and in corpus order
    # within a profile so a continuation still follows its parent.
    for profile in corpus.PROFILES:
        if profiles and profile not in profiles:
            continue
        sel = [c for c in cases if c["profile"] == profile]
        if not sel:
            continue
        if verbose:
            print(f"--- profile {profile} "
                  f"({' '.join(corpus.PROFILES[profile]) or 'no flags'}): "
                  f"{len(sel)} cases")
        pdir = os.path.join(workdir, profile)
        os.makedirs(pdir, exist_ok=True)
        records.update(run_profile(binary, pdir, profile, sel, verbose))
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bin", required=True, help="server binary to exercise")
    ap.add_argument("--out", help="write records here as JSON")
    ap.add_argument("--workdir", default=None,
                    help="scratch root (default: a temp dir)")
    ap.add_argument("--profile", action="append", dest="profiles",
                    help="restrict to this profile (repeatable)")
    ap.add_argument("--case", action="append", dest="case_ids",
                    help="restrict to this case id (repeatable)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args()

    if not os.path.isfile(args.bin):
        print(f"no such binary: {args.bin}", file=sys.stderr)
        return 2
    if not os.path.isdir(FIXTURE_ROOT):
        print(f"no fixture docroot at {FIXTURE_ROOT}", file=sys.stderr)
        return 2

    import tempfile
    tmp = None
    workdir = args.workdir
    if workdir is None:
        tmp = tempfile.mkdtemp(prefix="srb-replay-")
        workdir = tmp
    os.makedirs(workdir, exist_ok=True)
    try:
        records = replay_all(os.path.abspath(args.bin), workdir,
                             args.profiles, args.case_ids,
                             verbose=not args.quiet)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    expected = len(corpus.all_cases())
    if not args.profiles and not args.case_ids and len(records) != expected:
        print(f"WARNING: recorded {len(records)} of {expected} cases",
              file=sys.stderr)

    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"version": 1, "records": records}, fh,
                      indent=1, sort_keys=True)
            fh.write("\n")
        print(f"\nwrote {len(records)} records to {args.out}")
    else:
        print(f"\n{len(records)} records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
