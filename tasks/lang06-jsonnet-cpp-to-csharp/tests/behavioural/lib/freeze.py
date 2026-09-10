"""Record what the reference does, once, at verifier-image build time.

This runs while the image is being built -- before any submission exists -- and
writes expectations.json.  Grading then compares a submission against that file and
never consults the reference again.  Two consequences, both deliberate:

  * The reference's quirks are graded as behavior.  I never have to decide which of
    its outputs are "correct", because I am not the one deciding.
  * A submission cannot influence its own expectations.  There is no path from the
    repo the model edited to the numbers it is graded against.

The freeze also *audits the spec*: every table in spec.py that claims something
about the reference is checked here, and a disagreement fails the image build.  A
claim I assert but never check is the failure mode that produced five gate bugs in
lang03 and a fabricated table before that.  If this file exits non-zero, the
verifier image does not exist, which is the correct outcome for a spec I got wrong.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

# The image build invokes this as `python3 -I -B lib/freeze.py`, and -I implies -P: the
# script's own directory is *not* placed on sys.path.  Without this line the four
# sibling imports below raise ModuleNotFoundError, the freeze produces no
# expectations, and the image build fails on a traceback that looks like a missing
# dependency rather than a missing path entry.  Running it from lib/ by hand would
# work, which is what makes this worth a comment.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cases  # noqa: E402
import contract  # noqa: E402
import executor  # noqa: E402
import spec  # noqa: E402

# Bumped when the *format* of expectations.json changes.  Grading refuses a file
# whose format it does not understand rather than misreading it.
# 1.1 added upstream_hashes; 1.2 added the per-case `removed` list.  Bumped even
# though 1.2 is read-compatible with a 1.1 key -- `removed` is absent there and an
# absent list means "nothing deleted", which is what 1.1 meant.  The point of the
# guard is that a key and a verifier which disagree about the record shape must not
# pair silently, and "it happens to work" is not the same as "they agree".
FREEZE_FORMAT = "1.2"


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def discover_upstream(tree: str) -> dict[str, list[str]]:
    """The .jsonnet files in each source_dir directory of State A.

    Discovered rather than hardcoded, so the case list cannot claim a file the tree
    does not ship.  Sorted at every level: this feeds the case list digest.
    """
    out: dict[str, list[str]] = {}
    for source_dir in cases.UPSTREAM_DIRS:
        base = os.path.join(tree, source_dir)
        if not os.path.isdir(base):
            raise SystemExit(f"freeze: State A has no {source_dir}/ at {base}")
        found: list[str] = []
        for dirpath, _, names in os.walk(base):
            for n in sorted(names):
                if n.endswith(".jsonnet"):
                    found.append(
                        os.path.relpath(os.path.join(dirpath, n), base))
        out[source_dir] = sorted(found)
    return out


def upstream_hashes(tree: str,
                   files_by_dir: dict[str, list[str]]) -> dict[str, str]:
    """sha256 of every graded source_dir file, keyed "source_dir/relpath".

    These files are the *input* to the two upstream families.  They are copied into
    the verifier image beside expectations.json, and the executor materializes each
    case from that copy -- never from the submitted tree, which is why nothing here
    is an assertion about the submission.  What the hashes guard is my own image: if
    the copy under /opt/assets/upstream drifted from the tree this freeze ran
    against, every upstream case would be graded against an expectation belonging to
    a different program, and the diffs would look like port defects.  driver.py
    checks all of them once at startup and refuses to grade if any disagrees.
    """
    out: dict[str, str] = {}
    for source_dir, files in sorted(files_by_dir.items()):
        for rel in files:
            p = os.path.join(tree, source_dir, rel)
            with open(p, "rb") as f:
                out[f"{source_dir}/{rel}"] = hashlib.sha256(f.read()).hexdigest()
    return out


# --------------------------------------------------------------------------
# auditing the spec against the reference
# --------------------------------------------------------------------------
def audit(bins: dict[str, str], tree: str) -> list[str]:
    """Check every claim spec.py makes about the reference.  Returns failures."""
    errs: list[str] = []

    def ev(src: str, *extra: str):
        c = cases.ev_exec("audit", "audit", src, *extra)
        return executor.run(c, bins, timeout=60)

    # 1. the number rule: NUMBER_CASES and NUMBER_NONFINITE
    for src, want in spec.NUMBER_CASES:
        o = ev(src)
        got = o.stdout.decode("utf-8", "replace").strip()
        if got != want:
            errs.append(f"NUMBER_CASES[{src!r}]: spec says {want!r}, "
                        f"reference says {got!r}")
    for src, want_fragment in sorted(spec.NUMBER_NONFINITE.items()):
        o = ev(src)
        blob = (o.stdout + o.stderr).decode("utf-8", "replace")
        if want_fragment not in blob:
            errs.append(f"NUMBER_NONFINITE[{src!r}]: {want_fragment!r} not in "
                        f"reference output {blob[:80]!r}")

    # 2. the parseYaml allowlist really does not abort
    for group, srcs in spec.ASSERTED_NO_ABORT.items():
        for s in srcs:
            o = ev("std.parseYaml(%s)" % json.dumps(s))
            if o.aborted or o.timed_out or o.rc not in (0, 1):
                errs.append(f"ASSERTED_NO_ABORT[{group}]: {s!r} gave rc={o.rc}; "
                            f"the allowlist is wrong")

    # 3. the overflow cliff, in both directions.  Checking only the aborting side
    #    would let the cliff move outward without anyone noticing.
    ov = spec.PARSER_NUMBER_OVERFLOW
    for s in ov["aborts"]:
        o = ev("std.parseJson(%s)" % json.dumps(s))
        if not o.aborted:
            errs.append(f"PARSER_NUMBER_OVERFLOW: {s!r} no longer aborts "
                        f"(rc={o.rc}); the exclusion is now too broad")
    for s in ov["fine"]:
        o = ev("std.parseJson(%s)" % json.dumps(s))
        if o.aborted:
            errs.append(f"PARSER_NUMBER_OVERFLOW: {s!r} aborts but is listed "
                        f"as fine; the exclusion is too narrow")
    for s in ov["source_literal_is_a_clean_error"]:
        o = ev(s)
        if o.aborted or o.rc != 1:
            errs.append(f"PARSER_NUMBER_OVERFLOW: source literal {s!r} gave "
                        f"rc={o.rc}, expected a clean exit 1")

    # 4. the reference bugs are still bugs.  If upstream behavior changed, an
    #    excluded surface may have become gradeable and should stop being excluded.
    for bug in spec.ASSERTED_REFERENCE_BUGS:
        c = cases.Case(cid="audit-bug", family="audit", binary=bug["binary"],
                        argv=tuple(bug["argv"]) + ("main.jsonnet",),
                        files=(("main.jsonnet", bug["source"]),))
        o = executor.run(c, bins, timeout=60)
        blob = (o.stdout + o.stderr).decode("utf-8", "replace")
        if o.rc != bug["expect_exit"]:
            errs.append(f"ASSERTED_REFERENCE_BUGS[{bug['id']}]: rc={o.rc}, "
                        f"expected {bug['expect_exit']}")
        if bug["expect_stderr_contains"] not in blob:
            errs.append(f"ASSERTED_REFERENCE_BUGS[{bug['id']}]: "
                        f"{bug['expect_stderr_contains']!r} not in {blob[:100]!r}")

    # 5. the preserved stdlib is the file the spec pins
    std = os.path.join(tree, "stdlib", "std.jsonnet")
    with open(std, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if got != spec.STDLIB_SHA256:
        errs.append(f"stdlib/std.jsonnet sha256 is {got}, spec pins "
                    f"{spec.STDLIB_SHA256}")

    # 6. every public stdlib field is actually public in the reference, and the
    #    native/Jsonnet split adds up.  This is the one table I transcribed from a
    #    measurement, so it is the one most likely to have drifted.
    o = ev("std.objectFieldsAll(std)")
    if o.rc != 0:
        errs.append(f"could not enumerate std fields: rc={o.rc}")
    else:
        got_fields = tuple(json.loads(o.stdout.decode()))
        # objectFieldsAll returns the dunder comparison helpers too: 135 fields =
        # 129 public + 6 dunder, disjoint.  The two tables are separate because
        # only the 129 need a case each -- the dunders are internal helpers -- but
        # a port must still expose all 135, so the audit compares the union.
        want_fields = tuple(sorted(
            tuple(spec.STDLIB_PUBLIC) + tuple(spec.STDLIB_DUNDER)))
        if got_fields != want_fields:
            only_spec = sorted(set(want_fields) - set(got_fields))
            only_ref = sorted(set(got_fields) - set(want_fields))
            errs.append(f"the std field list disagrees with the reference: "
                        f"only in spec={only_spec}, only in reference={only_ref}")

    # 7. every path the contract asks a submission to preserve is a path State A
    #    actually ships.  A requirement stated without being checked against the
    #    thing it describes is the same class as the five lang03 gate bugs, and it
    #    has misfired here before -- a grader asked for CONTRIBUTING.md when
    #    upstream ships CONTRIBUTING with no extension.  Checked here because here
    #    is where the reference tree exists.
    errs.extend(audit_preserved_paths(tree))

    # 8. every path the contract forbids is a path State A actually ships, and
    #    every exemption shelters something.  Same class as clause 7.
    errs.extend(audit_forbidden_paths(tree))

    return errs


def audit_forbidden_paths(tree: str) -> list[str]:
    """Every path the contract forbids must exist in State A.

    Stage 2 fails no submission for a forbidden path -- the question "did the C++
    leave" is a stage-1 gate, answered by a reader with both trees open.  What the
    lists do is *tell the reviewer what to look for*, via source-contract.json and
    the read-only scan, and a list naming a path State A never shipped misdirects
    that reader.

    The shape that makes this easy to get wrong is the exact-path match: "BUILD" at
    the repo root names nothing, because upstream keeps its bazel files in
    subdirectories, so such an entry can never fire while stdlib/BUILD goes
    unnoticed.

    Checked here because here is where the reference tree exists.
    """
    errs: list[str] = []

    for rel in contract.FORBIDDEN_DIRS:
        if not os.path.isdir(os.path.join(tree, rel)):
            errs.append(f"contract.FORBIDDEN_DIRS lists {rel!r} but State A "
                        f"ships no such directory, so the requirement is vacuous")

    for rel in contract.FORBIDDEN_FILES:
        if not os.path.exists(os.path.join(tree, rel)):
            errs.append(f"contract.FORBIDDEN_FILES lists {rel!r} but State A "
                        f"ships no such path, so the requirement is vacuous")

    # The bazel rule is a basename match, so it is vacuous only if *nothing* in
    # the tree matches it.  Report the count as well: the interesting number is
    # how many survive outside the forbidden directories, since those are the ones
    # a submission has to act on.
    matched, outside = [], []
    for root, _dirs, names in os.walk(tree):
        for n in names:
            full = os.path.join(root, n)
            rel = os.path.relpath(full, tree)
            if (n in contract.BAZEL_BUILD_NAMES
                    or rel.endswith(contract.BAZEL_BUILD_EXT)):
                matched.append(rel)
                if rel.split(os.sep)[0] not in contract.FORBIDDEN_DIRS:
                    outside.append(rel)
    if not matched:
        errs.append("contract.BAZEL_BUILD_NAMES matches nothing in State A, "
                    "so the bazel rule can never fire")
    else:
        print(f"  bazel files: {len(matched)} in State A, "
              f"{len(outside)} outside the forbidden dirs "
              f"({', '.join(sorted(outside)) or 'none'})")

    # And the mirror: every native-source allowed prefix must actually shelter a
    # file with a native extension.  A prefix that shelters nothing is a hole
    # waved through for no reason.
    for prefix in contract.NATIVE_SOURCE_ALLOWED_PREFIXES:
        base = os.path.join(tree, prefix.rstrip("/"))
        found = False
        for root, _dirs, names in os.walk(base):
            if any(n.endswith(contract.NATIVE_SOURCE_EXT) for n in names):
                found = True
                break
        if not found:
            errs.append(f"contract.NATIVE_SOURCE_ALLOWED_PREFIXES lists "
                        f"{prefix!r} but no file under it has a native "
                        f"extension, so the exemption shelters nothing")
    return errs


def audit_preserved_paths(tree: str) -> list[str]:
    """Every path the contract asks a submission to keep must exist in State A.

    "Keep this file" is the one requirement a submission cannot satisfy by
    guessing, so it is the one worth being sure about -- and the way it goes wrong
    is a requirement that names a path with the wrong spelling.  A grader
    demanding CONTRIBUTING.md where upstream ships CONTRIBUTING with no extension
    docks every honest port for not preserving a file that never existed, and
    nothing but a check against the reference tree can see it.

    The digests are recorded here too.  PRESERVED_FILES carries a note per path
    saying *why* it is preserved, and for std.jsonnet the reason is that its bytes
    are the interpreter's input -- so a hash that does not match the tree means
    either the pin is stale or the tarball moved, and both should stop the build.
    """
    errs: list[str] = []
    for rel, what in sorted(contract.PRESERVED_FILES.items()):
        if not os.path.isfile(os.path.join(tree, rel)):
            errs.append(f"contract.PRESERVED_FILES wants {rel!r} ({what}) but "
                        f"State A does not ship it as a file")
    for rel, what in sorted(contract.PRESERVED_DIRS.items()):
        if not os.path.isdir(os.path.join(tree, rel)):
            errs.append(f"contract.PRESERVED_DIRS wants {rel!r} ({what}) but "
                        f"State A does not ship it as a directory")
    for rel in contract.CONFORMANCE_DIRS:
        base = os.path.join(tree, rel)
        if not os.path.isdir(base):
            errs.append(f"contract.CONFORMANCE_DIRS lists {rel!r} but State A "
                        f"ships no such directory")
            continue
        # A conformance directory with no .jsonnet in it is not a conformance
        # record, and "do not rewrite the conformance data" would then be a
        # requirement about nothing.
        has_case = any(n.endswith(".jsonnet")
                       for _r, _d, names in os.walk(base) for n in names)
        if not has_case:
            errs.append(f"contract.CONFORMANCE_DIRS lists {rel!r} but no "
                        f".jsonnet file lives under it, so 'do not rewrite the "
                        f"conformance data' describes nothing")
    return errs


# --------------------------------------------------------------------------
# freezing
# --------------------------------------------------------------------------
def freeze_cases(case_list, bins: dict[str, str], tree: str, *,
                 verbose: bool = True) -> tuple[dict, dict[str, int]]:
    """Run every case against the reference and record the outcome."""
    frozen: dict[str, dict] = {}
    stats = {"ok": 0, "err": 0, "excluded_abort": 0, "excluded_timeout": 0}
    t0 = time.time()

    for i, (c, o) in enumerate(executor.run_all(
            case_list, bins, upstream_root=tree,
            timeout=executor.DEFAULT_TIMEOUT)):
        # An abort or a timeout is not a behavior.  Reaching one here means a case
        # slipped past the exclusion rules, so record it as excluded and let the
        # summary flag it -- do not bake a crash into the expectations.
        if o.aborted:
            stats["excluded_abort"] += 1
            frozen[c.cid] = {"excluded": "reference aborted", "rc": o.rc}
            continue
        if o.timed_out:
            stats["excluded_timeout"] += 1
            frozen[c.cid] = {"excluded": "reference timed out"}
            continue

        stats["ok" if o.rc == 0 else "err"] += 1
        rec: dict = {
            "family": c.family,
            "binary": c.binary,
            "key": c.key(),
            "rc": o.rc,
            "stdout": _b64(o.stdout),
            "stderr": _b64(o.stderr),
        }
        if o.created:
            rec["created"] = {k: _b64(v) for k, v in sorted(o.created.items())}
        # Omitted when empty, which upstream always is -- the reference never
        # deletes an input.  Recorded anyway so a submission that does is caught
        # rather than credited: see Outcome.removed.
        if o.removed:
            rec["removed"] = list(o.removed)
        frozen[c.cid] = rec

        if verbose and (i + 1) % 250 == 0:
            print(f"  frozen {i + 1}/{len(case_list)}  "
                  f"({time.time() - t0:.0f}s)", flush=True)

    return frozen, stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tree", required=True,
                    help="pristine State A tree, with jsonnet and jsonnetfmt built")
    ap.add_argument("--out", required=True, help="expectations.json to write")
    ap.add_argument("--skip-audit", action="store_true",
                    help="freeze without auditing the spec; for local iteration "
                         "only, never in the image build")
    ap.add_argument("--contract",
                    help="shipped source-contract.json to audit against the "
                         "graders; a stale contract fails the image build")
    args = ap.parse_args(argv)

    tree = os.path.abspath(args.tree)
    bins = {"jsonnet": os.path.join(tree, "jsonnet"),
            "jsonnetfmt": os.path.join(tree, "jsonnetfmt")}
    for name, path in bins.items():
        if not os.access(path, os.X_OK):
            print(f"freeze: {name} is not executable at {path}")
            return 2

    # `tree` is State A with the reference built in it, so this is the one place
    # the spec's tables can be checked against the C++ they describe rather than
    # only against each other.  That distinction is not academic: STDLIB_NATIVE was
    # internally consistent, length-asserted and wrong about seven builtins, and no
    # amount of cross-table checking would have found it.
    spec.check_or_die(tree)
    print(f"spec self-check: clean (native list agrees with "
          f"jsonnet_builtin_decl, {len(spec.STDLIB_NATIVE)} names)")

    # The shipped contract must still be the contract the graders enforce.  A
    # requirement stated in source-contract.json but no longer checked -- or
    # checked but no longer stated -- is an authoring bug that would otherwise be
    # discovered by a submission at grading time.  Fail the image build instead.
    if args.contract:
        contract._self_check()
        cerrs = contract.audit(args.contract)
        if cerrs:
            print(f"\nfreeze: the shipped contract is out of sync with the "
                  f"graders ({len(cerrs)} problems).  Not building expectations.")
            for e in cerrs:
                print(f"  - {e}")
            return 1
        print(f"contract audit: clean (sha256 {contract.digest()[:16]})")

    if not args.skip_audit:
        errs = audit(bins, tree)
        if errs:
            print(f"\nfreeze: the spec disagrees with the reference "
                  f"({len(errs)} problems).  Not building expectations.")
            for e in errs:
                print(f"  - {e}")
            return 1
        print("spec audit against the reference: clean")

    files_by_dir = discover_upstream(tree)
    for source_dir, files in files_by_dir.items():
        print(f"  {source_dir}: {len(files)} .jsonnet files")
    # No family/weight pairing check here on purpose: cases.assemble() calls
    # spec.check_family_coverage() and raises CaseError on any disagreement, so
    # the line above already fails the image build in both directions.
    case_list = cases.assemble(files_by_dir)
    print(f"cases: {len(case_list)}, digest {cases.digest(case_list)}")

    frozen, stats = freeze_cases(case_list, bins, tree)

    doc = {
        "freeze_format": FREEZE_FORMAT,
        "schema_version": spec.SCHEMA_VERSION,
        "upstream_version": spec.UPSTREAM_VERSION,
        "case_digest": cases.digest(case_list),
        "case_count": len(case_list),
        "family_counts": cases.family_counts(case_list),
        "family_weights": spec.FAMILY_WEIGHTS,
        "upstream_files": files_by_dir,
        "upstream_hashes": upstream_hashes(tree, files_by_dir),
        "cases": frozen,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True, separators=(",", ":"))

    print(f"\nfroze {len(frozen)} cases -> {args.out}")
    print(f"  exit 0: {stats['ok']}   nonzero: {stats['err']}")
    if stats["excluded_abort"] or stats["excluded_timeout"]:
        print(f"  EXCLUDED  aborts={stats['excluded_abort']} "
              f"timeouts={stats['excluded_timeout']}  <-- a case escaped the "
              f"exclusion rules; investigate before shipping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
