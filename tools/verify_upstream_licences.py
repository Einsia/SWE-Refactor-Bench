#!/usr/bin/env python3
"""Is each vendored licence file byte-identical to upstream at the recorded ref?

NOTICE asserts, for twenty projects, "these are the upstream terms, and we
compared them".  That is a checkable claim for the GitHub-hosted ones.  It cannot
be checked by ``infra/tests/test_licensing.py``, which must pass offline, so it
lives here and is run by hand when a task is added or re-pinned.

Nothing is hardcoded.  The repository slug and the ref come out of each
``task.toml``; the files to check are *discovered* in the archive rather than
listed, so a licence file added upstream and never disclosed is a finding rather
than a silent omission.  Both matter: an earlier draft of this tool carried a
static table, and within an hour it was stale in two places -- it pinned lang04
at the annotated tag object rather than the commit, and it named fw03's upstream
org as `gothinkster`, which now redirects to `realworld-apps`.

Two measurements to keep in mind when reading a report:

* build01's LICENSE says "Copyright (c) 2013-2026" on a release that shipped in
  2024.  That looks exactly like a freeze artifact and is upstream's own text at
  the 1.0.20 tag.  MATCH is the answer to trust, not the date.
* A version is not always a tag.  fw01 pins httpbin by commit because 0.10.2 was
  never tagged, so refs are tried in order and the first that resolves wins.

Exit status, which is the whole point of running it:

    0   every reachable file matched, and at least one was fetched
    1   something differs, is missing from an archive, or is undisclosed
    2   nothing could be fetched -- no network, or every ref is gone

Status 2 exists so that a run with no egress cannot be read as a pass.  A check
that made no measurement has to be distinguishable from one that made twenty.

    PYTHONPATH=infra tools/verify_upstream_licences.py [--task NAME ...]
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "infra"))
sys.path.insert(0, str(REPO / "infra" / "tests"))

try:
    # The NOTICE parser and the archive-root rule, imported rather than copied:
    # two implementations of "which directory is the archive's root" would drift,
    # and the one in the test tree is the one under test.
    import test_licensing as licensing
except ImportError as exc:  # pytest absent, or run from outside a checkout
    sys.exit(f"cannot import infra/tests/test_licensing.py ({exc}).  This tool "
             f"runs from a checkout with the test dependencies installed.")

from swerefactor import tomlcompat

#: A file that states terms.  Matched on the basename, case-insensitively, at the
#: archive root or one level below it -- deep enough for gson/LICENSE and
#: acorn-walk/LICENSE, shallow enough not to sweep up a vendored dependency's.
LICENCE_GLOBS = ("LICENSE*", "LICENCE*", "COPYING*", "NOTICE*")

RAW = "https://raw.githubusercontent.com/{slug}/{ref}/{path}"


def licence_files(task: Path) -> dict[str, bytes]:
    """Discover the terms-stating files in a task's archive, root stripped."""
    out: dict[str, bytes] = {}
    with tarfile.open(task / "environment" / "original.tar.gz") as tar:
        members = tar.getmembers()
        root = licensing.tar_root([m.name for m in members])
        for m in members:
            if not m.isfile():
                continue
            rel = m.name[len(root) + 1:] \
                if root and m.name.startswith(root + "/") else m.name
            base = rel.rsplit("/", 1)[-1].upper()
            if rel.count("/") <= 1 and any(fnmatch.fnmatch(base, g)
                                           for g in LICENCE_GLOBS):
                fh = tar.extractfile(m)
                if fh is not None:
                    out[rel] = fh.read()
    return out


def github_slug(meta: dict) -> str | None:
    """`owner/name` from whichever declared URL is a GitHub one, else None.

    Two fields can hold it and they disagree by design: build03 records
    source_repository as a PyPI release page, because that is what the snapshot
    was built from, and names GitHub under upstream.
    """
    for key in ("source_repository", "upstream"):
        url = str(meta.get(key) or "")
        if "github.com/" not in url:
            continue
        parts = url.split("github.com/", 1)[1].strip("/").split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1].removesuffix('.git')}"
    return None


def candidate_refs(meta: dict) -> list[str]:
    """Every ref worth trying, most specific first.

    A commit is unambiguous, so it leads.  A version may or may not be a tag, and
    may or may not be spelled with the `v` -- lang05 records `v3.0.1`, lang02
    records `1.3.1` for the tag `v1.3.1` -- so both spellings are tried.
    """
    refs: list[str] = []
    for key in ("source_commit", "upstream_commit"):
        if meta.get(key):
            refs.append(str(meta[key]))
    for key in ("source_version", "upstream_version"):
        v = str(meta.get(key) or "")
        if not v:
            continue
        for spelling in (v, f"v{v.lstrip('v')}", v.lstrip("v")):
            if spelling not in refs:
                refs.append(spelling)
    return refs


def fetch(slug: str, ref: str, path: str) -> bytes | None:
    """One read-only GET.  None on any failure, including 404 for a bad ref."""
    req = urllib.request.Request(RAW.format(slug=slug, ref=ref, path=path),
                                 headers={"User-Agent": "curl/8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            return fh.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", action="append", default=[],
                    help="check only these tasks (default: all of tasks/)")
    args = ap.parse_args()

    tasks = sorted(p for p in (REPO / "tasks").iterdir() if p.is_dir())
    if args.task:
        wanted = set(args.task)
        tasks = [t for t in tasks if t.name in wanted]
        missing = wanted - {t.name for t in tasks}
        if missing:
            sys.exit(f"no such task: {', '.join(sorted(missing))}")

    notice = (REPO / "NOTICE").read_text()
    entries = licensing.notice_entries()
    tally = {"match": 0, "differ": 0, "undisclosed": 0,
             "unreachable": 0, "not-on-github": 0}

    for task in tasks:
        meta = tomlcompat.load(task / "task.toml").get("metadata", {})
        slug, refs = github_slug(meta), candidate_refs(meta)
        found = licence_files(task)
        print(f"\n=== {task.name}  [{slug or 'not on GitHub'}]")

        if not found:
            # pf03 QuickJS publishes no licence file: the terms are in every
            # source header.  NOTICE says so, and the test asserts the header.
            named = entries.get(task.name, {}).get("paths") or []
            print(f"    no licence file in the archive; NOTICE names {named or 'none'}")
            continue

        for path, mine in sorted(found.items()):
            # Disclosure, checked before fidelity.  Substring, so it is only
            # decisive for a distinctively named file -- LICENSE.ISC, NOTICE.md,
            # gson/LICENSE.  A bare LICENSE always matches something in NOTICE.
            if path not in notice:
                print(f"    {path:28s} IN ARCHIVE, NOT NAMED IN NOTICE")
                tally["undisclosed"] += 1
                continue
            if slug is None:
                print(f"    {path:28s} upstream is not on GitHub -- not fetched")
                tally["not-on-github"] += 1
                continue
            for ref in refs:
                theirs = fetch(slug, ref, path)
                if theirs is None:
                    continue
                same = hashlib.sha256(mine).digest() == hashlib.sha256(theirs).digest()
                tally["match" if same else "differ"] += 1
                print(f"    {path:28s} {ref[:12]:13s} "
                      f"{'MATCH' if same else 'DIFFER'}"
                      f"  ({len(mine)}b vs {len(theirs)}b)")
                break
            else:
                print(f"    {path:28s} unreachable at any of {refs}")
                tally["unreachable"] += 1

    print("\n" + "=" * 78)
    print("  ".join(f"{k}={v}" for k, v in tally.items()))
    fetched = tally["match"] + tally["differ"]
    if fetched == 0:
        print("nothing was fetched: no measurement was made, which is not a pass")
        return 2
    if tally["differ"] or tally["undisclosed"]:
        return 1
    print(f"{tally['match']} of {tally['match']} fetched files match upstream")
    return 0


if __name__ == "__main__":
    sys.exit(main())
