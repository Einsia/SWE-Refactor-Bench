#!/usr/bin/env python3
"""Build the deterministic, history-free repository snapshot for a task.

The snapshot is the agent's State A.  It must be byte-reproducible so the
environment image digest is stable, and it must not carry any
repository-management data: a `.git` directory, VCS dotfiles or CI workflow
definitions would let a solver recover upstream history and read the answer
straight out of a later commit.

Usage:
    build_repo_snapshot.py --source DIR --out repo.tar.gz --manifest repo.json
                           [--exclude PATTERN ...] [--prefix NAME]
"""

from __future__ import annotations

import argparse
import fnmatch
import gzip
import hashlib
import io
import json
import os
import stat
import sys
import tarfile
from pathlib import Path
from typing import Iterable

# Repository-management data. Present in upstream, must never reach the agent.
DEFAULT_EXCLUDES = (
    ".git",
    ".git/*",
    ".gitignore",
    ".gitattributes",
    ".gitmodules",
    ".github",
    ".github/*",
    ".gitlab",
    ".gitlab/*",
    ".hg",
    ".hg/*",
    ".svn",
    ".svn/*",
    ".bzr",
    ".bzr/*",
    "_darcs",
    "_darcs/*",
    "CVS",
    "CVS/*",
    ".agit",
    ".agit/*",
)

# Fixed epoch for every entry so the archive bytes depend only on content.
FIXED_MTIME = 0


def excluded(rel: str, patterns: Iterable[str]) -> bool:
    parts = rel.split("/")
    for pattern in patterns:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if any(fnmatch.fnmatch(part, pattern) for part in parts):
            return True
    return False


def collect(source: Path, patterns: tuple[str, ...]) -> list[tuple[str, Path]]:
    entries: list[tuple[str, Path]] = []
    for path in source.rglob("*"):
        rel = path.relative_to(source).as_posix()
        if excluded(rel, patterns):
            continue
        if path.is_symlink():
            raise SystemExit(f"symlink in source tree: {rel}")
        if not (path.is_dir() or path.is_file()):
            raise SystemExit(f"irregular file in source tree: {rel}")
        entries.append((rel, path))
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    return entries


def normalize(info: tarfile.TarInfo, is_exec: bool) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = FIXED_MTIME
    if info.isdir():
        info.mode = 0o755
    else:
        info.mode = 0o755 if is_exec else 0o644
    return info


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--manifest", required=True, type=Path)
    ap.add_argument("--prefix", default="repo")
    ap.add_argument("--exclude", action="append", default=[])
    args = ap.parse_args(argv)

    source = args.source.resolve()
    if not source.is_dir():
        raise SystemExit(f"not a directory: {source}")
    patterns = DEFAULT_EXCLUDES + tuple(args.exclude)
    entries = collect(source, patterns)

    files: list[dict[str, object]] = []
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for rel, path in entries:
            arcname = f"{args.prefix}/{rel}"
            st = path.lstat()
            is_exec = bool(st.st_mode & stat.S_IXUSR)
            if path.is_dir():
                info = tarfile.TarInfo(arcname)
                info.type = tarfile.DIRTYPE
                tar.addfile(normalize(info, False))
                continue
            payload = path.read_bytes()
            info = tarfile.TarInfo(arcname)
            info.size = len(payload)
            tar.addfile(normalize(info, is_exec), io.BytesIO(payload))
            files.append(
                {
                    "path": rel,
                    "size": len(payload),
                    "mode": "0755" if is_exec else "0644",
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )

    tar_bytes = raw.getvalue()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as gz:
        gz.write(tar_bytes)
    archive = buf.getvalue()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(archive)

    manifest = {
        "schema_version": "swerefactor-source-snapshot-v1",
        "prefix": args.prefix,
        "exclusions": sorted(patterns),
        "file_count": len(files),
        "directory_count": sum(1 for rel, p in entries if p.is_dir()),
        "content_bytes": sum(int(f["size"]) for f in files),
        "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "archive_size": len(archive),
        "files": files,
    }
    payload = json.dumps(manifest, ensure_ascii=True, sort_keys=True, indent=2)
    args.manifest.write_text(payload + "\n", encoding="utf-8")

    print(f"archive      {args.out}")
    print(f"archive_size {len(archive)}")
    print(f"archive_sha  {manifest['archive_sha256']}")
    print(f"files        {len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
