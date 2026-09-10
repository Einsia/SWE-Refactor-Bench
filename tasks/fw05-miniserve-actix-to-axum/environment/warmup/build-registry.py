#!/usr/bin/env python3
"""Build a cargo local-registry from a Cargo.lock, with an optional ban list.

A cargo local-registry is the offline analogue of a package mirror: a directory
holding `<name>-<version>.crate` files plus an `index/` tree carrying the exact
same newline-delimited JSON the real crates.io index serves. Cargo resolves
against it with no network and no HTTP server:

    [source.crates-io]
    replace-with = "srb-mirror"
    [source.srb-mirror]
    local-registry = "/opt/srb/registry"

The index entries are copied verbatim from index.crates.io rather than
regenerated from each crate's manifest. Feature tables, optional/default-feature
flags, dependency kinds and target predicates all have to match what cargo
expects byte-for-byte or resolution silently diverges; the registry is the only
authoritative source for them.

Banning is by omission. A crate with no index entry does not exist as far as
cargo is concerned -- `cargo build` fails with "no matching package named
`actix-web` found", which is a resolver error the submission cannot route
around. There is no flag that re-enables it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

INDEX_BASE = "https://index.crates.io"
DL_BASE = "https://static.crates.io/crates"
UA = "swerefactorbench-registry-builder/1.0"


def index_path(name: str) -> str:
    """crates.io index sharding: 1-char, 2-char, 3-char and 4+-char names."""
    n = name.lower()
    if len(n) == 1:
        return f"1/{n}"
    if len(n) == 2:
        return f"2/{n}"
    if len(n) == 3:
        return f"3/{n[0]}/{n}"
    return f"{n[:2]}/{n[2:4]}/{n}"


def fetch(url: str, retries: int = 5) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as exc:  # noqa: PERF203
            last = exc
    raise RuntimeError(f"failed to fetch {url}: {last}")


def parse_lock(lock: Path) -> list[tuple[str, str]]:
    """Pull (name, version) pairs out of Cargo.lock without a TOML dependency."""
    out, name, version, source = [], None, None, None
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line == "[[package]]":
            if name and version and source is not None:
                out.append((name, version))
            name = version = None
            source = None
            continue
        if line.startswith("name = "):
            name = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("version = "):
            version = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("source = "):
            source = line.split("=", 1)[1].strip().strip('"')
    if name and version and source is not None:
        out.append((name, version))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", required=True, type=Path, action="append",
                    help="Cargo.lock to read; repeatable, closures are unioned")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--ban", type=Path, help="file of crate names to omit")
    ap.add_argument("--crate-cache", type=Path, action="append", default=[],
                    help="directory of already-downloaded .crate files")
    ap.add_argument("--pin", type=Path,
                    help="manifest of {name-version.crate: sha256} this registry "
                         "must equal; extra payloads are pruned and any "
                         "missing or mismatched one fails the build")
    ap.add_argument("--allow-missing", action="store_true")
    args = ap.parse_args()

    banned = set()
    if args.ban and args.ban.exists():
        for line in args.ban.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                banned.add(line)

    wanted: dict[str, set[str]] = {}
    for lock in args.lock:
        for name, version in parse_lock(lock):
            wanted.setdefault(name, set()).add(version)

    # A registry-sourced package in the lock is one crates.io actually serves;
    # path/git packages (miniserve itself) have no `source` and never appear.
    skipped = sorted(n for n in wanted if n in banned)
    for n in skipped:
        del wanted[n]

    cache: dict[str, Path] = {}
    for d in args.crate_cache:
        if d.is_dir():
            for p in d.rglob("*.crate"):
                cache.setdefault(p.name, p)

    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "index").mkdir(parents=True)

    n_crates = n_cached = n_downloaded = 0
    missing: list[str] = []

    for name in sorted(wanted):
        versions = wanted[name]
        try:
            raw = fetch(f"{INDEX_BASE}/{index_path(name)}")
        except RuntimeError as exc:
            missing.append(f"{name}: index: {exc}")
            continue

        keep, by_version, unyanked = [], {}, []
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("vers") not in versions:
                continue
            # Clear the yank flag. Yanking is a crates.io-side signal meaning
            # "stop picking this in new resolutions"; cargo honours it only for a
            # fresh resolve, not for a version already in a lock. Left set, the
            # closure would be self-inconsistent: State A builds (its lock pins
            # the yanked crate) while the target stack does not, because
            # axum -> multer -> `spin ^0.9` has exactly one candidate and 0.9.8
            # is yanked. This mirror is a frozen snapshot chosen deliberately,
            # so yank status carries no information here.
            if entry.get("yanked"):
                entry["yanked"] = False
                unyanked.append(entry["vers"])
                line = json.dumps(entry, separators=(",", ":"), sort_keys=False)
            keep.append(line)
            by_version[entry["vers"]] = entry
        if unyanked:
            print(f"  unyanked {name}: {', '.join(unyanked)}")

        absent = versions - set(by_version)
        if absent:
            missing.append(f"{name}: versions not in index: {sorted(absent)}")
            if not args.allow_missing:
                continue

        dest = out / "index" / index_path(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(keep) + "\n", encoding="utf-8")

        for version, entry in by_version.items():
            fname = f"{name}-{version}.crate"
            target = out / fname
            src = cache.get(fname)
            if src is not None:
                shutil.copyfile(src, target)
                n_cached += 1
            else:
                try:
                    target.write_bytes(fetch(f"{DL_BASE}/{name}/{name}-{version}.crate"))
                    n_downloaded += 1
                except RuntimeError as exc:
                    missing.append(f"{fname}: download: {exc}")
                    target.unlink(missing_ok=True)
                    continue

            # The index cksum is the contract cargo checks on extract. A
            # mismatch here means a corrupt mirror, and it is far cheaper to
            # find out now than as an opaque failure inside a trial.
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest != entry.get("cksum"):
                missing.append(f"{fname}: cksum mismatch")
                target.unlink(missing_ok=True)
                continue
            n_crates += 1

    # --- the pin manifest, if one was given ---------------------------------
    # The index cksum check above proves each payload is the one crates.io serves
    # *today*. This proves it is the one this task was built against, which is a
    # different claim and the one that matters: the closure is a frozen snapshot,
    # and a crate republished under the same version, or a lock that resolved a
    # newer patch, would pass the first check and fail this one.
    #
    # Pruning is part of it. The lock files name Windows-only crates that a Linux
    # target never downloads; their index entries have to stay (the resolver reads
    # them to walk the graph) but their payloads are not in the closure and would
    # make the registry differ from its manifest for no reason.
    n_pruned = 0
    if args.pin:
        pins: dict[str, str] = json.loads(
            args.pin.read_text(encoding="utf-8"))["crates"]
        have = {p.name: p for p in out.glob("*.crate")}
        for name, path in sorted(have.items()):
            if name not in pins:
                path.unlink()
                n_pruned += 1
        absent = sorted(set(pins) - set(have))
        wrong = sorted(
            n for n, p in have.items()
            if n in pins
            and hashlib.sha256(p.read_bytes()).hexdigest() != pins[n])
        if absent or wrong:
            print(f"  PIN MISMATCH against {args.pin}:", file=sys.stderr)
            for n in absent[:20]:
                print(f"    missing: {n}", file=sys.stderr)
            for n in wrong[:20]:
                print(f"    digest differs: {n}", file=sys.stderr)
            return 1
        print(f"  pins verified  : {len(pins)} crates match {args.pin.name}"
              + (f", {n_pruned} off-platform payload(s) pruned" if n_pruned else ""))

    print(f"registry: {out}")
    print(f"  crates written : {n_crates}  (from cache {n_cached}, downloaded {n_downloaded})")
    print(f"  names indexed  : {len(list((out / 'index').rglob('*')) )} index paths")
    if banned:
        print(f"  banned omitted : {len(skipped)} of {len(banned)} listed -> {', '.join(skipped[:8])}"
              + (" ..." if len(skipped) > 8 else ""))
    if missing:
        print(f"  PROBLEMS ({len(missing)}):", file=sys.stderr)
        for m in missing[:40]:
            print(f"    {m}", file=sys.stderr)
        if not args.allow_missing:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
