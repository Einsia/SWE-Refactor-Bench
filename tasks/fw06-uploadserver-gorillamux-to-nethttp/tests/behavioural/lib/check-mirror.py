#!/usr/bin/env python3
"""Check a Go module mirror against what the task's own lockfile requires of it.

The two mirrors -- the agent image's and this one -- are built independently, at
different times, from the same go.mod and go.sum. That is on purpose: a copy would
hide drift instead of exposing it. But it means "the verifier stocks what the agent
stocked" is an assertion someone has to make, and this is where it is made.

The authority is `go.sum`, which is committed and identical in both build contexts,
and which says precisely what each module was fetched FOR:

  `<mod> <ver> h1:...`          the module's ZIP was verified, so State A's own
                                resolution downloaded its code. The mirror must
                                carry that .zip -- unless the module is retired.

  `<mod> <ver>/go.mod h1:...`   only the .mod was verified. The mirror must carry
                                the .mod; a .zip is welcome and not required.

Deriving the expectation this way rather than from a manifest recorded out of the
agent image is what makes this check runnable at all: the manifest would be a build
output of the other image, and a check that needs an artefact from the thing it is
checking against cannot run in a fresh build. `--manifest` is still accepted, and
when it is supplied the byte digests are compared too, which is strictly stronger --
it catches a module republished between the two builds, which the lockfile cannot
see. Nothing requires it.

Three questions, and they are not the same question:

  present   every module@version the lockfile verified is here, with the file kind
            it was verified for. A missing one is fatal: the submission resolved it
            during development and would fail to resolve it here, and the failure
            would look like the submission's fault.

  retired   depends on the mirror's ROLE, which is why `--retired-role` is not
            optional when `--retired` is given.

            `pruned`   -- the agent's mirror, reproduced. No .zip for anything on
            the retired list, and the .mod still there. The .mod half is equally
            load-bearing: without it `go mod tidy` cannot compute the graph while
            a stale require is still present, and a correct migration's first
            command fails.

            `complete` -- what stage 2 builds against. The .zip must be PRESENT.
            State A imports the retired router, State A is the behavioural oracle
            stage 2 replays, and a mirror that cannot compile the oracle can never
            show the suite recorded from it is satisfiable.

            Checked HERE as well as in the builder, in both directions, because
            the builder is what deletes and copies and a checker that trusts the
            deleter checks nothing. Both directions have failed silently in this
            ladder's history.

  banned    no compilable .zip for any module on the router ban list, including
            ones the lockfile never mentions. This is the screen on the extras.
            The retired module is excluded from this sweep: it is on both lists,
            and under `--retired-role complete` its archive is the one thing the
            mirror is required to carry.

Extra entries are reported, not failed. The go.sum sweep reaches module versions
`go mod download all` alone does not, and a superset is the safe direction: it can
only let a correct submission resolve something the agent could also resolve.
"""
import argparse
import hashlib
import json
import os
import sys


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root):
    """Return {"<module>@<version>": {ext: digest}} for a proxy layout."""
    found = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            module, sep, tail = rel.partition("/@v/")
            if not sep:
                continue
            version, ext = os.path.splitext(tail)
            ext = ext.lstrip(".")
            if ext not in ("mod", "zip", "info"):
                continue
            found.setdefault(f"{module}@{version}", {})[ext] = path
    return found


def read_list(path):
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def parse_gomod(path):
    """Return {"<module>@<version>": None} for every require line.

    For a `go 1.17`-or-later module -- and State A declares `go 1.25.0` -- the
    require blocks list the COMPLETE build list, direct and indirect alike. That
    makes go.mod, on its own, the set of versions minimal version selection will
    actually choose, which is the set a submission can actually need to compile.
    go.sum is a wider and less useful thing: it accumulates every version the
    graph has ever mentioned, superseded ones included.
    """
    want = {}
    in_block = False
    with open(path) as fh:
        for raw in fh:
            line = raw.split("//", 1)[0].strip()
            if not line:
                continue
            if in_block:
                if line == ")":
                    in_block = False
                    continue
            elif line == "require (":
                in_block = True
                continue
            elif line.startswith("require "):
                line = line[len("require "):].strip()
            else:
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1].startswith("v"):
                want[f"{parts[0]}@{parts[1]}"] = None
    return want


def parse_gosum(path):
    """Return {"<module>@<version>": {"zip", "mod"}} recording what was verified."""
    seen = {}
    with open(path) as fh:
        for raw in fh:
            parts = raw.split()
            if len(parts) < 3 or not parts[1].startswith("v"):
                continue
            module, version = parts[0], parts[1]
            kind = "mod" if version.endswith("/go.mod") else "zip"
            version = version[: -len("/go.mod")] if kind == "mod" else version
            seen.setdefault(f"{module}@{version}", set()).add(kind)
    return seen


def covers(listed, module):
    """True when `module` is `listed` or a package path nested under it."""
    return module == listed or module.startswith(listed + "/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mirror", required=True)
    # The lockfile pair. --gomod is what makes a demand ("this must be here, or a
    # correct submission cannot build"); --gosum only informs one ("this was
    # verified once, so its absence is worth a line of output").
    ap.add_argument("--gomod")
    ap.add_argument("--gosum")
    # Optional, and strictly stronger where it is available: digests recorded out
    # of the agent image catch a module republished between the two builds, which
    # no lockfile can see. Not required, because it is a build output of the image
    # this one is being compared against, and a check that cannot run without an
    # artefact from its own subject cannot run in a fresh build.
    ap.add_argument("--manifest")
    ap.add_argument("--retired")
    # What the retired module's archive is supposed to be doing in THIS mirror.
    # No default: the two roles assert opposite things, so a caller that forgot to
    # say which one it meant would silently get whichever was convenient to code.
    ap.add_argument("--retired-role", choices=("pruned", "complete"))
    # Screens the EXTRAS this tool otherwise permits. The go.sum sweep reaches
    # module versions the agent image never fetched, and "extra is harmless" holds
    # only while none of the extras is a router: one router .zip here and the
    # verifier would compile a submission the agent image could not.
    ap.add_argument("--banned")
    # .info holds the module's commit timestamp as published; it is stable for a
    # published version, but it is metadata rather than code and nothing compiles
    # from it. Kept checkable, off by default, so a proxy that normalises it
    # cannot fail an otherwise byte-identical mirror.
    ap.add_argument("--check-info", action="store_true")
    args = ap.parse_args()

    if not (args.gomod or args.manifest):
        ap.error("need --gomod (or --manifest) to know what the mirror should hold")
    if args.retired and not args.retired_role:
        ap.error("--retired needs --retired-role {pruned,complete}: the two roles "
                 "assert opposite things about the same file")

    have = scan(args.mirror)
    retired = read_list(args.retired) if args.retired else []
    keep_archive = args.retired_role == "complete"
    problems = []
    notes = []
    checked = 0

    # ---- the demand: every version go.mod selects, with the file kind that
    # version's role requires. A retired module inverts: .mod yes, .zip no.
    selected = parse_gomod(args.gomod) if args.gomod else {}
    for key in sorted(selected):
        module = key.split("@")[0]
        is_retired = any(covers(r, module) for r in retired)
        files = have.get(key)
        if files is None:
            problems.append(
                f"MISSING  {key} is required by go.mod but is not in {args.mirror} "
                f"at all -- a submission's first `go mod tidy` fails offline"
            )
            continue
        if "mod" not in files:
            problems.append(
                f"MISSING  {key} has no .mod, so the module graph cannot be "
                f"computed offline"
            )
        if is_retired and not keep_archive:
            if "zip" in files:
                problems.append(
                    f"UNRETIRED {key} is on the retired list and still has a .zip: "
                    f"a surviving import of it would compile here"
                )
        elif "zip" not in files:
            problems.append(
                f"MISSING  {key} has no .zip, so code that imports it cannot compile"
            )

    # ---- the advisory: go.sum records every version the graph ever verified,
    # superseded ones included. MVS will not select those, so their absence
    # cannot fail a correct submission and is reported rather than failed.
    recorded = parse_gosum(args.gosum) if args.gosum else {}
    for key in sorted(recorded):
        if key in selected or key in have:
            continue
        notes.append(f"absent   {key} (in go.sum, not selected by go.mod)")

    # ---- optional digest comparison against the agent image.
    manifest_entries = {}
    if args.manifest:
        with open(args.manifest) as fh:
            manifest_entries = json.load(fh)["entries"]
        for key in sorted(manifest_entries):
            entry = manifest_entries[key]
            if key not in have:
                problems.append(f"MISSING  {key} is in the manifest but not in {args.mirror}")
                continue
            for ext in ("mod", "zip", "info"):
                expected = entry.get(ext)
                if expected is None:
                    continue
                if ext == "info" and not args.check_info:
                    continue
                path = have[key].get(ext)
                if path is None:
                    problems.append(f"MISSING  {key} has no .{ext}")
                    continue
                actual = sha256_file(path)
                checked += 1
                if actual != expected:
                    problems.append(
                        f"DIGEST   {key} .{ext}\n"
                        f"           agent image {expected}\n"
                        f"           this mirror {actual}"
                    )
            # A manifest entry with no zip records a RETIRED module. If a zip
            # appeared here, the retirement did not take in this image -- which is
            # a problem for a `pruned` mirror and the whole point of a `complete`
            # one, since the manifest is recorded out of the agent image and the
            # agent image is pruned by design.
            if "zip" not in entry and "zip" in have[key] and not keep_archive:
                problems.append(f"UNRETIRED {key} has a .zip that the agent image does not")

    known = set(selected) | set(recorded) | set(manifest_entries)
    extra = sorted(set(have) - known)

    # ---- the screen on the extras. Both lists are swept across the WHOLE mirror,
    # not just the selected versions: the sweep and the graph passes reach module
    # versions go.mod never names, and "extra is harmless" holds only while none of
    # the extras is a router someone could import.
    if args.banned:
        for mod in read_list(args.banned):
            # The retired module is on both lists. Excluded here so the sweep asks
            # only about ALTERNATIVE routers -- "was mux swapped for chi" -- which
            # is a different question from "is mux still here" and is asked of both
            # mirror roles identically.
            if any(covers(r, mod) or covers(mod, r) for r in retired):
                continue
            hits = sorted(
                k for k in have
                if covers(mod, k.split("@")[0]) and "zip" in have[k]
            )
            if hits:
                problems.append(
                    f"ROUTER   {mod} has a compilable .zip in the mirror: {hits}"
                )

    for mod in retired:
        entries = sorted(k for k in have if covers(mod, k.split("@")[0]))
        zips = [k for k in entries if "zip" in have[k]]
        if not entries:
            problems.append(
                f"ABSENT   retired module {mod} is not in the mirror at all -- its "
                f".mod is what lets a correct submission's `go mod tidy` resolve "
                f"the stale require it is in the middle of removing"
            )
        elif keep_archive and not zips:
            problems.append(
                f"NOZIP    retired module {mod} has no .zip in a mirror declared "
                f"`complete`. State A imports it, so State A cannot be built from "
                f"this mirror -- and State A is the oracle every behavioural module "
                f"here replays. The suite would be unsatisfiable by its own baseline."
            )
        elif not keep_archive and zips:
            problems.append(f"UNRETIRED {mod} still has a .zip: {zips}")
        elif not any("mod" in have[k] for k in entries):
            problems.append(f"NOMOD    retired module {mod} has no .mod left: tidy will break")

    print(f"mirror   {args.mirror}: {len(have)} module version(s), "
          f"{sum(1 for k in have if 'zip' in have[k])} with a .zip")
    if selected:
        print(f"go.mod   {len(selected)} selected version(s) required to be present")
    if recorded:
        print(f"go.sum   {len(recorded)} recorded version(s) (advisory)")
    if args.manifest:
        print(f"manifest {len(manifest_entries)} entries, {checked} digests compared")
    for line in notes:
        print(line)
    if extra:
        print(f"extra    {len(extra)} module version(s) named by neither lockfile "
              f"(allowed, superset): {extra[:8]}")
    for line in problems:
        print(line)
    if problems:
        print(f"FAIL: {len(problems)} mirror problem(s)")
        return 1
    print("OK: the mirror holds every version go.mod selects, and no retired or "
          "banned router can be compiled from it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
