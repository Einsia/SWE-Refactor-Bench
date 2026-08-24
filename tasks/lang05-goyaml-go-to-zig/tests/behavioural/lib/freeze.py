#!/usr/bin/env python3
"""Freezes the grading inputs into the verifier image, at image build time.

Everything a submission is measured against is produced here, once, from the
pinned upstream module -- the same v3.0.1 the agent's workspace was unpacked
from.  By the time any submission exists, the cases, the expectations, the
document digests and the protocol answers are already files in a read-only layer.

That ordering is the design.  If the cases were generated at grading time from
the submitted tree, a submission could shrink it by deleting what it is built
from; if the expectations were computed at grading time, the submission would be
in the container while the answers were being decided.  Neither is possible when
both are frozen here.

What is specific to this task is the Go toolchain's lifetime.  The reference is
Go, and Go is the one thing no submission may reach: the whole migration is out
of it.  So the toolchain exists in this stage and nowhere else.  The reference
binary it produces does survive into the grading image -- unlike lang02's, where
nothing needed the reference at run time -- because two thousand fresh cases are
generated and answered live, which is the anti-memorisation term and cannot be
precomputed by definition.  What does not survive is `go` itself.  driver.py
never has a Go compiler on PATH, and `build.py`'s shim is there to prove it: the
submission's build runs with fake `go`, `gofmt` and `cgo` ahead of a PATH that
has none of them anyway, so an attempt is recorded rather than merely failing.

The fresh cases are the reason the two instruments are digested alongside the
data.  `reference` and `generator` decide 2,000 of the 10,255 cases at grading
time, so they are graded inputs in exactly the sense the frozen files are, and
they sit under the same seal.

Three things are checked here rather than trusted.

Every case is answered in full before anything is written: a case the reference
cannot answer has no correct answer, and one that crashes it is a defective case,
not a defective submission.  Either fails the image build.

The fresh generator is run once against the shipped seed, and its census is
recorded in the manifest.  driver.py re-runs it at grading time and compares --
so a generator that is not deterministic across runs is caught at grading with
the manifest to point at, and one that is not deterministic across *builds* is
caught here.

The frozen cases are checked against catalog.py's weight table by the same
`check_catalog` driver.py calls.  A family the catalog does not know would
otherwise be graded at a fallback weight and say nothing about it.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# See the note in driver.py: isolated mode drops the script's directory, and both
# entry points are run that way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog as catalogmod  # noqa: E402
import probe as probemod  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
TASK = "lang05-goyaml-go-to-zig"
UPSTREAM_VERSION = "v3.0.1"
ZIG_VERSION = "0.14.1"

# The seed the fresh cases are generated from, and how many.  Both are
# recorded in the manifest and both are read back by driver.py rather than
# duplicated there: the fresh families have to be declared before the file that
# would let them be counted exists.
FRESH_SEED = 12345
FRESH_COUNT = 2000

# The package inside the repository that implements the wire protocol.  The
# reference is a driver around this package rather than a second implementation
# of it; see build_instruments.
PROBE_PACKAGE = "probe"

#: The harvested upstream inputs.  One name, used everywhere this file needs it: as
#: the build-context source under `data/`, as the staged copy under the assets root
#: -- staging preserves the basename, which is what makes the rest correct -- in the
#: digest table, and in each generator invocation.  driver.py spells it once more at
#: grading time as `Assets.harvest`, checked there by the digest rather than by an
#: import.
#:
#:
#: The failure was not confined to the census, which is what makes one constant the
#: right fix rather than a tidier one.  `--harvest` is optional in gen.go and the
#: fresh mode reuses the systematic cases as mutation seeds, so a harvest the
#: generator cannot open changes which cases exist to mutate.  Had it been optional
#: on the path that also silently continues, freeze.py would have recorded a census
#: for a 16-family seed pool and driver.py would have regenerated from a 17-family
#: one, and every grading run would have failed the determinism check with "the
#: generator produced different counts" -- pointing at the generator, which would
#: have been right both times.
HARVEST_NAME = "upstream-cases.ndjson"

# The two source trees this stage reads, and where each lives.
#
#   lib/   the engines and the Go instruments' source.  Code.
#   data/  the inputs.  The twelve real-world documents, the harvested upstream
#          cases, the handwritten protocol lines, the contract.
#
# The split is by what a file is, not by who reads it: `lib/shim/goshim.py` is
# code that ships as an asset, and `data/protocol.ndjson` is data that ships as an
# asset, and putting them in one directory because they share a destination is how
# a stage directory becomes a pile.
STAGED_DIRS = (("lib", "shim"), ("data", "documents"))
STAGED_FILES = (("data", "source-contract.json"),
                ("data", HARVEST_NAME))

#: The declared case counts, read from the build context and deliberately not
#: staged.  It is checked once, here, at image build time -- nothing at grading
#: time reads a count out of a file when the frozen suite is right there to be
#: measured -- so staging it would put a description of the answers next to the
#: answers for no reader.  It lives verifier-side rather than in the contract
#: because the contract is shipped to the agent, and the size of the graded suite
#: is not something the solver is owed; see the file's own `_why_here`.
COUNTS_NAME = "case-counts.json"

# Handwritten, not generated.  The protocol family includes lines that are not
# valid JSON -- a truncated object, a bare `[`, 40 KB of one key -- so they cannot
# come out of a JSON encoder, and they are carried as build context instead.
PROTOCOL_FILES = ("protocol.ndjson", "protocol-cases.json")

# The two files that describe the twelve real-world documents: what the generator
# writes, and what the reference turns it into.  Both names are compiled into Go
# -- gen.go writes the first, reference/main.go reads it and writes the second --
# and are spelled here so the Python that reads them both is checkable against
# them.  A drift is a missing file at image build time, which is where the two Go
# sources can still be edited.
DOCUMENT_MANIFEST = "document-manifest.json"
DOCUMENT_DIGESTS = "document-digests.json"


def sh(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
       label: str, log: Log, stdin: bytes | None = None,
       timeout: float = 900.0) -> subprocess.CompletedProcess:
    """Run one freeze-time command, aborting the image build on failure.

    Deliberately not vlib.run: nothing here is being graded, so there is no
    outcome to record and no reason to tolerate a failure.  Anything that goes
    wrong at freeze time is a broken image, and the useful behaviour is to stop
    with the compiler's own words in the build log.
    """
    log.write(f"$ {' '.join(argv)}")
    proc = subprocess.run(
        argv, cwd=str(cwd) if cwd else None, env=env, input=stdin,
        capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-4000:]
        raise SystemExit(f"freeze: {label} failed ({proc.returncode}):\n{tail}")
    err = proc.stderr.decode("utf-8", "replace").strip()
    if err:
        for line in err.splitlines()[:40]:
            log.write(f"  {line}")
    return proc


def stage(baseline: Path, suite: Path, assets: Path, log: Log) -> int:
    """Copy the verifier's fixed inputs into the assets tree.

    The baseline copy is what tells a rewritten file from an untouched one, and it
    comes from the verifier's own tarball rather than anything a submission could
    reach.  It is the *whole* archive, scaffolding included -- `build.zig`,
    `build.zig.zon`, `README.swerefactor.md`, `go.sum` -- because the contract's file
    count and byte total are of the tree the agent was actually given, and
    `check_baseline_usable` compares against those.
    """
    log.write("staging fixed inputs")
    dest_baseline = assets / "baseline"
    if dest_baseline.exists():
        shutil.rmtree(dest_baseline)
    files = vlib.copy_tree(baseline, dest_baseline)
    for where, name in STAGED_DIRS:
        src = suite / where / name
        if not src.is_dir():
            raise SystemExit(f"missing verifier input directory: {src}")
        dest = assets / name
        if dest.exists():
            shutil.rmtree(dest)
        vlib.copy_tree(src, dest)
    for where, name in STAGED_FILES:
        src = suite / where / name
        if not src.is_file():
            raise SystemExit(f"missing verifier input: {src}")
        (assets / name).write_bytes(src.read_bytes())
    # Named explicitly because a staging bug here surfaces as every case failing
    # to run, which reads like a broken submission.
    for required in (assets / "shim" / "goshim.py",
                     assets / HARVEST_NAME,
                     assets / "source-contract.json"):
        if not required.is_file():
            raise SystemExit(f"staging did not produce {required}")
    documents = sorted(p.name for p in (assets / "documents").iterdir()
                       if p.is_file())
    if not documents:
        raise SystemExit(f"no documents were staged into {assets}/documents")
    staged = [n for _, n in STAGED_DIRS] + [n for _, n in STAGED_FILES]
    log.write(
        f"staged baseline ({files} files), {len(documents)} documents, "
        f"{', '.join(staged)}"
    )
    return files


def go_env(goroot: Path, workspace: Path) -> dict:
    """The environment the two Go builds run in.

    Offline and hermetic: the module graph is a `replace` onto the unpacked
    upstream, so `GOPROXY=off` is not a restriction being worked around but a
    statement that nothing is fetched.  A build that needed the network would fail
    here, which is the point -- the grading image is built without one.
    """
    env = dict(os.environ)
    env.update({
        "GOROOT": str(goroot),
        "GOPATH": str(workspace / "gopath"),
        "GOCACHE": str(workspace / "gocache"),
        "GOPROXY": "off",
        "GOFLAGS": "-mod=mod",
        "CGO_ENABLED": "0",
        "PATH": f"{goroot / 'bin'}:{env.get('PATH', '')}",
    })
    return env


def check_baseline_matches_upstream(baseline: Path, upstream: Path,
                                    log: Log) -> int:
    """The library sources State A ships must be upstream's, byte for byte.

    This is what licenses the line below it.  The reference's protocol adapter is
    taken from the baseline -- from `probe/`, a package of the repository the
    agent is given -- while its parser and emitter are taken from the pinned
    upstream tarball.  Mixing two sources like that is only safe if they are the
    same source, so it is checked rather than assumed: every `*.go` at the root of
    the upstream module must exist in the baseline with identical bytes.

    A mismatch here would mean the agent was handed a library that is not v3.0.1
    and then graded against one that is.  Nothing downstream could tell that from
    a submission's own defect, so it fails the image build.
    """
    problems: list[str] = []
    checked = 0
    for src in sorted(upstream.glob("*.go")):
        want = src.read_bytes()
        got_path = baseline / src.name
        if not got_path.is_file():
            problems.append(f"{src.name} is in upstream but not in the baseline")
            continue
        got = got_path.read_bytes()
        if got != want:
            problems.append(
                f"{src.name} differs from upstream ({len(got)} bytes in the "
                f"baseline, {len(want)} upstream)"
            )
            continue
        checked += 1
    if problems:
        raise SystemExit(
            "the baseline's library sources are not the pinned upstream:\n  "
            + "\n  ".join(problems)
        )
    if checked < 12:
        raise SystemExit(
            f"only {checked} upstream source(s) were compared against the "
            f"baseline; the upstream tarball at {upstream} is not go-yaml"
        )
    log.write(f"baseline library sources match upstream byte for byte ({checked} files)")
    return checked


def build_instruments(suite: Path, upstream: Path, goroot: Path,
                      workspace: Path, assets: Path, log: Log) -> tuple[Path, Path]:
    """Compile the reference and the case generator against the pinned upstream.

    Both are the verifier's own Go.  The reference imports `gopkg.in/yaml.v3` and
    a `replace` points that at the unpacked v3.0.1, so what answers every case is
    upstream's own parser and emitter rather than a reimplementation of it -- which
    is what makes every case a differential against the real library.

    The protocol adapter is not the verifier's own, and that is the point.  It is
    `gopkg.in/yaml.v3/probe`, a package of the repository State A ships, copied in
    from the staged baseline; `lib/reference/main.go` is a driver around it.  So
    the rule that turns a node into response bytes has exactly one
    implementation, the agent can read it, and it cannot drift from what the
    expectations were frozen with.  `check_baseline_matches_upstream` is what
    makes taking the adapter from one tree and the library from another safe.

    `CGO_ENABLED=0` is set for a reason beyond hermeticity: it makes both binaries
    static, so they keep working in the grading stage, which has no Go toolchain
    and need not have the same libc.
    """
    src = workspace / "gosrc"
    if src.exists():
        shutil.rmtree(src)
    vlib.copy_tree(suite / "lib" / "reference", src)
    # The upstream module, as the `replace` target.  Copied rather than
    # referenced in place so the tree the reference builds from is inside the
    # workspace and cannot be confused with the baseline the gates compare
    # against -- the same separation lang02 needed for a different reason.
    vlib.copy_tree(upstream, src / "vendor-yaml")
    if not (src / "vendor-yaml" / "parserc.go").is_file():
        raise SystemExit(
            f"the upstream module at {upstream} has no parserc.go; the reference "
            f"would build against something that is not go-yaml"
        )
    baseline = assets / "baseline"
    check_baseline_matches_upstream(baseline, src / "vendor-yaml", log)
    adapter = baseline / PROBE_PACKAGE
    if not (adapter / "probe.go").is_file():
        raise SystemExit(
            f"the baseline has no {PROBE_PACKAGE}/probe.go; the reference has no "
            f"protocol adapter to build against"
        )
    # Into the replace target, so `gopkg.in/yaml.v3/probe` resolves to it.  The
    # `_test.go` files come too: `go build` ignores them and nothing here runs
    # them, but leaving them behind would make the copied package differ from the
    # one that ships.
    vlib.copy_tree(adapter, src / "vendor-yaml" / PROBE_PACKAGE)
    gen_src = suite / "lib" / "gen"
    if not (gen_src / "gen.go").is_file():
        raise SystemExit(f"generator source missing at {gen_src}/gen.go")
    vlib.copy_tree(gen_src, src / "gen")

    env = go_env(goroot, workspace)
    bindir = assets / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    reference = bindir / "reference"
    generator = bindir / "generator"
    # `-trimpath` and `-buildvcs=false` for the same reason: the manifest digests
    # both binaries, and anything host-specific in them would make that digest a
    # fact about the build machine.  VCS stamping also fails outright when the
    # source is copied out of a repository the builder does not own, which is what
    # `go build` does by default and what it did on the first run of this.
    go = str(goroot / "bin" / "go")
    flags = ["-trimpath", "-buildvcs=false"]
    sh([go, "build", *flags, "-o", str(reference), "."],
       cwd=src, env=env, label="go build reference", log=log)
    sh([go, "build", *flags, "-o", str(generator), "."],
       cwd=src / "gen", env=env, label="go build generator", log=log)
    for path in (reference, generator):
        path.chmod(0o755)

    # What the reference says it is, asked through the protocol rather than a
    # side channel.  Every frozen answer below is this binary's opinion, and a
    # reference built against the wrong module would freeze answers that are
    # internally consistent and wrong.
    #
    # `hello` is the right question because it is the one the submission is also
    # graded on: two structural cases compare a submission's handshake against
    # these exact three fields.  Reading them from the reference here is what
    # keeps the expected values in `structure.py` from being a transcription.
    out = sh([str(reference), "serve"], env=vlib.base_env(),
             stdin=b'{"id":1,"op":"hello"}\n', label="reference hello", log=log)
    try:
        hello = json.loads(out.stdout.decode("utf-8").splitlines()[0])["result"]
    except (ValueError, KeyError, IndexError) as exc:
        raise SystemExit(
            f"the reference answered `hello` with something unreadable "
            f"({exc}): {out.stdout[:200]!r}"
        ) from exc
    if hello.get("upstream") != UPSTREAM_VERSION:
        raise SystemExit(
            f"the reference reports upstream {hello.get('upstream')!r}, but this "
            f"image is built from {UPSTREAM_VERSION!r}; it was built against the "
            f"wrong module"
        )
    log.write(
        f"reference: {hello} ({reference.stat().st_size} bytes, static), "
        f"generator {generator.stat().st_size} bytes"
    )
    return reference, generator


def generate_cases(generator: Path, suite: Path, assets: Path,
                 log: Log) -> tuple[Path, dict]:
    """Generate the systematic cases and carry in the handwritten protocol half.

    The generator is deterministic and takes no input but the harvest file, so the
    result is a function of the binary that was just built.  It is generated rather
    than shipped for that reason: cases checked in as data could drift from the
    generator that is also shipped and used live for the fresh families, and then
    two of the four grading sources would disagree about what a case even is.
    """
    cases_dir = assets / "cases"
    if cases_dir.exists():
        shutil.rmtree(cases_dir)
    cases_dir.mkdir(parents=True)
    sh([str(generator), "--out", str(cases_dir),
        "--harvest", str(assets / HARVEST_NAME)],
       env=vlib.base_env(), label="generate cases", log=log)
    if not (cases_dir / DOCUMENT_MANIFEST).is_file():
        # The name is compiled into gen.go and read back by four Python modules.
        # Checked here rather than left to the first reader, because the first
        # reader is the reference and its error would be a bare open() failure on a
        # path nobody wrote.
        raise SystemExit(
            f"the generator wrote no {DOCUMENT_MANIFEST}; gen.go and freeze.py "
            f"disagree about its name"
        )
    for name in PROTOCOL_FILES:
        src = suite / "data" / name
        if not src.is_file():
            raise SystemExit(
                f"the handwritten protocol input {name} is missing from "
                f"{suite / 'data'}; it cannot be generated"
            )
        (cases_dir / name).write_bytes(src.read_bytes())
    cases = json.loads((cases_dir / "cases.json").read_text(encoding="utf-8"))
    log.write(
        f"generated: {cases['count']} systematic cases in "
        f"{len(cases['families'])} families"
    )
    return cases_dir, cases


def shard_cases(cases_dir: Path, log: Log) -> dict[str, int]:
    """Split the frozen cases into one request/expectation pair per module.

    Seventeen modules grade the frozen cases, each in its own process, and each
    must see only its own families.  The alternative -- one file and a filter in
    every module -- means seventeen processes each streaming a 40 MB expectation
    file to discard most of it, and a family that no module claims being silently
    skipped by all of them rather than reported by one.

    Sharded by `frozen_family_module`, not `family_module`: the fresh module claims
    two families too, and they have no cases here.

    Sharding here instead makes the split a property of the image: a shard that is
    empty, or a case that lands in no shard, fails the build with the family named.
    Requests and expectations are written in lockstep because that is how
    `Runner.run_cases` reads them back -- position for position, not by id.
    """
    index = json.loads((cases_dir / "cases.json").read_text(encoding="utf-8"))
    by_id = {int(c["id"]): c for c in index["cases"]}
    owner = catalogmod.frozen_family_module()

    shards = sorted({m for m in owner.values()})
    dest = cases_dir / "modules"
    if dest.exists():
        shutil.rmtree(dest)
    handles: dict[str, tuple] = {}
    for name in shards:
        (dest / name).mkdir(parents=True)
        handles[name] = (
            (dest / name / "requests.ndjson").open("wb"),
            (dest / name / "expected.ndjson").open("wb"),
            [],
        )

    counts: dict[str, int] = {name: 0 for name in shards}
    unclaimed: dict[str, int] = {}
    written = 0
    try:
        with (cases_dir / "requests.ndjson").open("rb") as reqs, \
             (cases_dir / "expected.ndjson").open("rb") as exps:
            for raw_req in reqs:
                raw_exp = vlib.read_ndjson_line(exps)
                if raw_exp is None:
                    raise SystemExit(
                        f"there are more requests than expectations at line "
                        f"{written + 1}; it must not be sharded"
                    )
                stripped = raw_req.strip()
                if not stripped:
                    continue
                case_id = probemod.request_id(stripped)
                meta = by_id.get(case_id)
                if meta is None:
                    raise SystemExit(
                        f"request id {case_id} was generated but is not in "
                        f"cases.json; the generator's index is incomplete"
                    )
                family = meta.get("family", "")
                name = owner.get(family)
                if name is None:
                    unclaimed[family] = unclaimed.get(family, 0) + 1
                    continue
                req_fh, exp_fh, ids = handles[name]
                req_fh.write(stripped + b"\n")
                exp_fh.write(raw_exp.rstrip(b"\n") + b"\n")
                ids.append(meta)
                counts[name] += 1
                written += 1
            if vlib.read_ndjson_line(exps) is not None:
                raise SystemExit(
                    "there are more expectations than requests; they must not "
                    "be sharded"
                )
    finally:
        for req_fh, exp_fh, _ in handles.values():
            req_fh.close()
            exp_fh.close()

    if unclaimed:
        raise SystemExit(
            "these families were generated and no module grades them: "
            + ", ".join(f"{k} ({v} cases)" for k, v in sorted(unclaimed.items()))
            + ". Add them to catalog.MODULES or they are generated and never read."
        )
    empty = [name for name, n in counts.items() if n == 0]
    if empty:
        raise SystemExit(
            f"module shard(s) {empty} got no cases; a module with an empty shard "
            f"scores zero on a submission that is correct"
        )

    # Each shard's own case index, so a module reads a file of its own size rather
    # than the whole 10,000-case index to look up 400 of them.
    for name in shards:
        _req, _exp, metas = handles[name]
        vlib.write_json(dest / name / "cases.json", {
            "module": name,
            "count": len(metas),
            "families": {f: sum(1 for m in metas if m.get("family") == f)
                         for f in sorted({m.get("family", "") for m in metas})},
            "cases": metas,
        })
    if written != index["count"]:
        raise SystemExit(
            f"sharding wrote {written} of {index['count']} frozen cases; the "
            f"difference is cases nothing would grade"
        )
    log.write(
        f"sharded {written} cases into {len(shards)} module shards: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    return counts


def freeze_answers(reference: Path, cases_dir: Path, documents: Path,
                   log: Log) -> dict[str, int]:
    """Answer every frozen case, and refuse to write a partial set.

    Three sources are answered here and each is answered whole.  A request the
    reference cannot answer has no correct answer, and one that crashes it is a
    defective case rather than a defective submission -- so a short output aborts
    the image build, where the cause is visible, rather than becoming a case that
    every submission fails for a reason no report can explain.

    The protocol family is answered through `serve` rather than `--batch` because
    that is the whole point of it: the malformed lines are about what the transport
    does with a line it cannot parse, and a batch mode that reads the file with a
    JSON decoder would never see them.
    """
    requests = cases_dir / "requests.ndjson"
    expected = cases_dir / "expected.ndjson"
    sh([str(reference), "--batch", str(requests), str(expected)],
       env=vlib.base_env(), label="reference --batch", timeout=1800.0, log=log)
    want = sum(1 for _ in requests.open("rb"))
    got = sum(1 for _ in expected.open("rb"))
    if got != want:
        raise SystemExit(
            f"the reference answered {got} of {want} frozen requests; an "
            f"unanswerable case must not be frozen"
        )

    # `--documents` is the reference's compiled-in subcommand name; the three paths
    # after it are ours.  See GENERATED_MANIFEST above for why the flag keeps the
    # old word and the files do not.
    sh([str(reference), "--documents", str(cases_dir / DOCUMENT_MANIFEST),
        str(cases_dir / DOCUMENT_DIGESTS), str(documents)],
       env=vlib.base_env(), label="reference document digests", timeout=900.0,
       log=log)
    manifest = json.loads(
        (cases_dir / DOCUMENT_MANIFEST).read_text(encoding="utf-8"))["documents"]
    digests = json.loads(
        (cases_dir / DOCUMENT_DIGESTS).read_text(encoding="utf-8"))["digests"]
    missing = [e["key"] for e in manifest if e["key"] not in digests]
    if missing:
        raise SystemExit(
            f"{len(missing)} document case(s) got no digest from the reference: "
            f"{missing[:6]}"
        )

    proto_in = (cases_dir / "protocol.ndjson").read_bytes()
    out = sh([str(reference), "serve"], env=vlib.base_env(), stdin=proto_in,
             label="reference serve (protocol)", timeout=600.0, log=log)
    (cases_dir / "protocol-expected.ndjson").write_bytes(out.stdout)
    # Loaded through the same function driver.py uses, so a protocol file the
    # grader could not load fails here instead.  Position-matched, not id-matched:
    # a malformed line is answered with id 0, so ids do not identify a case.
    lines, answers, pcases = probemod.load_protocol(
        cases_dir / "protocol-cases.json", cases_dir / "protocol.ndjson",
        cases_dir / "protocol-expected.ndjson",
    )
    log.write(
        f"answers: {got} frozen, {len(digests)} document digests, "
        f"{len(pcases)} protocol cases from {len(lines)} transport lines"
    )
    return {"expected": got, "document_digests": len(digests),
            "protocol": len(pcases), "protocol_lines": len(lines),
            "protocol_answers": len(answers)}


def fresh_census(generator: Path, assets: Path, workspace: Path,
                 log: Log) -> dict[str, int]:
    """Run the fresh generator once and record what it produced, by family.

    Nothing from this run is shipped.  What is shipped is the census, which
    driver.py compares against its own run of the same binary with the same seed
    -- so the manifest is what turns "the generator is deterministic" from an
    assumption into something the grader checks every time.

    The output directory is created here because the generator does not create it,
    and driver.py mkdirs for the same reason.
    """
    out = workspace / "fresh-census"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    sh([str(generator), "--out", str(out),
        "--harvest", str(assets / HARVEST_NAME),
        "--seed", str(FRESH_SEED), "--count", str(FRESH_COUNT)],
       env=vlib.base_env(), label="generator (fresh census)", timeout=900.0,
       log=log)
    payload = json.loads((out / "fresh-cases.json").read_text(encoding="utf-8"))
    families = dict(payload["families"])
    total = sum(families.values())
    if total != FRESH_COUNT:
        raise SystemExit(
            f"the fresh generator produced {total} cases for seed {FRESH_SEED} "
            f"and count {FRESH_COUNT}; the census would not match the grading run"
        )
    log.write(f"fresh census: {total} cases, {families}")
    return families


def check_shape(cases_dir: Path, cases: dict, fresh: dict[str, int],
                error_texts: int, log: Log) -> None:
    """Run the grader's own catalog check over the cases that were just frozen.

    `check_catalog` is the function driver.py calls at grading time.  Calling it
    here as well is what makes a coverage regression an image-build failure: a
    family the weight table does not know, a family the table knows and the
    generator lost, a document or protocol count that moved, an error-text count
    below the floor.  Each of those would otherwise be graded quietly at a
    fallback weight.
    """
    manifest = json.loads(
        (cases_dir / DOCUMENT_MANIFEST).read_text(encoding="utf-8"))["documents"]
    _, _, pcases = probemod.load_protocol(
        cases_dir / "protocol-cases.json", cases_dir / "protocol.ndjson",
        cases_dir / "protocol-expected.ndjson",
    )
    problems = catalogmod.check_catalog(
        dict(cases["families"]), fresh, len(manifest), len(pcases),
        error_texts=error_texts,
    )
    if problems:
        raise SystemExit(
            "the frozen cases and this image's catalog disagree:\n  "
            + "\n  ".join(problems)
        )
    log.write(
        f"catalog agrees: {len(cases['families'])} frozen families, "
        f"{len(fresh)} fresh, {len(manifest)} documents, {len(pcases)} protocol, "
        f"{error_texts} distinct error texts"
    )


def check_contract(assets: Path, contract: dict, log: Log) -> dict:
    """Assert the contract against itself and against the baseline it describes.

    Every figure in here is graded against, so every figure is re-derived from the
    tree rather than trusted.  The failure being prevented is the one that keeps
    recurring in this benchmark: a requirement stated in two places, drifting in
    one of them, and grading every correct submission against a number nobody
    published.  Doing it at freeze time makes that an image-build failure instead.

    Six things are checked, and each is checked because something downstream reads
    it as fact:

      - the twelve graded sources exist in the baseline, and each has the declared
        logic-line count.  `check_baseline_usable` compares against these at
        grading time, so a wrong count there withholds every score.
      - their counts sum to the declared total.  The Zig floor is justified in
        `min_zig_logic_lines_note` by that total, and the note is prose.
      - the file count and byte total match the staged tree.
      - the declared Zig floor is below the Go total.  A floor above it would
        demand a port longer than the original for no stated reason.
      - the probe binary's `name` and the basename of its `path` agree.  The
        structural cases read one and the build reads the other.
      - the anchor files are a subset of the graded sources, since a gate that
        searches for a file nobody grades searches for nothing.
    """
    baseline = assets / "baseline"
    state_a = contract.get("state_a") or {}
    declared = dict(state_a.get("go_logic_lines_per_file") or {})
    graded = list(state_a.get("graded_sources") or [])
    problems: list[str] = []

    if sorted(declared) != sorted(graded):
        only_counts = sorted(set(declared) - set(graded))
        only_graded = sorted(set(graded) - set(declared))
        problems.append(
            f"go_logic_lines_per_file and graded_sources name different files "
            f"(counts-only {only_counts}, graded-only {only_graded})"
        )
    total = 0
    for name in sorted(declared):
        path = baseline / name
        if not path.is_file():
            problems.append(f"graded source {name} is not in the baseline")
            continue
        # `zig_logic_lines` on Go, deliberately: the same non-blank / not-`//`
        # rule produced the contract's Go figures, and one counter for both sides
        # is what makes the Zig floor comparable to State A's total at all.
        got = vlib.zig_logic_lines(path.read_text(encoding="utf-8", errors="replace"))
        if got != declared[name]:
            problems.append(
                f"{name}: contract declares {declared[name]} logic lines, the "
                f"baseline has {got}"
            )
        total += got
    want_total = state_a.get("go_logic_lines")
    if want_total is not None and total != want_total:
        problems.append(
            f"graded sources total {total} logic lines, the contract declares "
            f"{want_total}"
        )

    files = [p for p in baseline.rglob("*") if p.is_file()]
    want_files = state_a.get("file_count")
    if want_files is not None and len(files) != want_files:
        problems.append(
            f"the baseline holds {len(files)} files, the contract declares "
            f"{want_files}"
        )
    want_bytes = state_a.get("content_bytes")
    got_bytes = sum(p.stat().st_size for p in files)
    if want_bytes is not None and got_bytes != want_bytes:
        problems.append(
            f"the baseline is {got_bytes} bytes, the contract declares {want_bytes}"
        )

    policy = contract.get("native_code_policy") or {}
    floor = policy.get("min_zig_logic_lines")
    if floor is not None and want_total is not None and floor >= want_total:
        problems.append(
            f"the Zig floor ({floor}) is not below State A's {want_total} Go logic "
            f"lines; the floor would demand a port longer than the original"
        )
    if policy.get("zig_toolchain") != ZIG_VERSION:
        problems.append(
            f"the contract pins Zig {policy.get('zig_toolchain')!r} but this image "
            f"is built for {ZIG_VERSION!r}"
        )
    state_b = contract.get("state_b") or {}
    if state_b.get("zig_version") != ZIG_VERSION:
        problems.append(
            f"state_b pins Zig {state_b.get('zig_version')!r} but this image is "
            f"built for {ZIG_VERSION!r}"
        )
    if (contract.get("product") or {}).get("upstream_version") != UPSTREAM_VERSION:
        problems.append(
            f"the contract names upstream "
            f"{(contract.get('product') or {}).get('upstream_version')!r} but this "
            f"image is built from {UPSTREAM_VERSION!r}"
        )

    binaries = state_b.get("binaries") or []
    if not binaries:
        problems.append("state_b declares no binary; there would be nothing to grade")
    else:
        entry = binaries[0]
        if Path(entry.get("path", "")).name != entry.get("name"):
            problems.append(
                f"the probe is named {entry.get('name')!r} but installed at "
                f"{entry.get('path')!r}; the structural cases read the name and the "
                f"build reads the path"
            )
        declared_probe = (contract.get("probe_protocol") or {}).get("binary")
        if declared_probe != entry.get("name"):
            problems.append(
                f"probe_protocol.binary is {declared_probe!r} and "
                f"state_b.binaries names {entry.get('name')!r}"
            )

    anchors = set(state_a.get("anchor_files") or [])
    stray = sorted(anchors - set(graded))
    if stray:
        problems.append(
            f"anchor_files names {stray}, which are not graded sources; a gate "
            f"searching for them would be searching for nothing"
        )
    for name in (contract.get("retained_paths") or {}).get("required") or []:
        if not (baseline / name).is_file():
            problems.append(
                f"retained path {name} is required to survive unchanged but is not "
                f"in the baseline to compare against"
            )

    if problems:
        raise SystemExit(
            "the contract does not describe the tree it ships with:\n  "
            + "\n  ".join(problems)
        )
    log.write(
        f"contract self-consistent: {len(graded)} graded sources, {total} Go logic "
        f"lines, Zig floor {floor}, {len(files)} baseline files, "
        f"{got_bytes} bytes, probe {binaries[0]['path']}"
    )
    return {"graded_sources": len(graded), "go_logic_lines": total,
            "zig_line_floor": floor, "baseline_files": len(files),
            "baseline_bytes": got_bytes,
            "probe_path": binaries[0]["path"] if binaries else None}


def digest_all(assets: Path, cases_dir: Path, reference: Path,
               generator: Path) -> dict[str, str]:
    """Digest every input the grader will check before it grades anything.

    The key names are `driver.Assets`'s, not this file's: every read there goes
    through `verify(key, path)`, which looks the key up here and aborts on a
    mismatch or an absence.  They are named there and here and nowhere else, which
    is the one duplication in this pipeline that cannot be removed -- so it is a
    flat table in both places rather than something derived, because a missing key
    is then a one-line diff instead of a control-flow puzzle.  The shard keys are
    the exception and are derived on both sides: there are 51 of them, and
    `driver.self_check` reads every one at image build time, so a drift in the
    derivation fails the build rather than a grading run.
    """
    files = {
        "contract": assets / "source-contract.json",
        "requests": cases_dir / "requests.ndjson",
        "expected": cases_dir / "expected.ndjson",
        "cases": cases_dir / "cases.json",
        "documents": cases_dir / DOCUMENT_MANIFEST,
        "document_digests": cases_dir / DOCUMENT_DIGESTS,
        "protocol_requests": cases_dir / "protocol.ndjson",
        "protocol_expected": cases_dir / "protocol-expected.ndjson",
        "protocol_cases": cases_dir / "protocol-cases.json",
        "reference": reference,
        "generator": generator,
        "upstream_cases": assets / HARVEST_NAME,
        # The shim is a graded input in the strictest sense here: it is the file
        # copied onto the build PATH as `go`, and the ledger it writes is the whole
        # evidence base of the `shim-clean` module.  It was missing from this table
        # while `driver.py:393` verified it, so `verify` found no digest for the key
        # and aborted the build module -- and every module downstream of it reads
        # the state file that module writes.  The self-check did not catch it
        # because it enumerated the keys it expected by hand; it now derives them
        # from `driver.Assets.frozen`, so this table and that one are compared
        # rather than both maintained.
        "shim": assets / "shim" / "goshim.py",
    }
    # And every module shard, under a derived key.  A shard is a graded input in
    # exactly the sense every other graded input is -- seventeen modules read
    # nothing else -- so leaving them outside the seal would mean the one file
    # each module actually grades against is the one file nothing verifies.
    for shard in sorted((cases_dir / "modules").iterdir()):
        if not shard.is_dir():
            continue
        for name in ("requests.ndjson", "expected.ndjson", "cases.json"):
            files[f"module/{shard.name}/{name}"] = shard / name
    digests = {}
    for key, path in sorted(files.items()):
        if not path.is_file():
            raise SystemExit(f"freeze produced no {key} at {path}")
        digests[key] = vlib.sha256_file(path)
    return digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path,
                        help="the pristine State A tree, unpacked from the tarball")
    parser.add_argument("--upstream", required=True, type=Path,
                        help="the unpacked go-yaml v3.0.1 module, the replace target")
    parser.add_argument("--goroot", required=True, type=Path,
                        help="the Go toolchain, which exists in this stage only")
    parser.add_argument("--assets", required=True, type=Path,
                        help="where the frozen inputs are written")
    parser.add_argument("--suite", type=Path,
                        default=Path(__file__).resolve().parent.parent,
                        help="tests/behavioural, holding lib/ and data/")
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/freeze"))
    parser.add_argument("--image", default="")
    args = parser.parse_args(argv)
    # Resolved for the same reason driver.py resolves its four: every path here is
    # handed to a subprocess that runs with a different cwd, and a relative one
    # would resolve somewhere else entirely.
    for name in ("baseline", "upstream", "goroot", "assets", "suite", "workspace"):
        setattr(args, name, getattr(args, name).resolve())

    assets: Path = args.assets
    assets.mkdir(parents=True, exist_ok=True)
    args.workspace.mkdir(parents=True, exist_ok=True)
    log = Log(assets / "freeze.log")
    log.section("freeze verifier assets")

    # 1. The fixed inputs, including State A itself.  Everything below reads the
    #    staged copy rather than the argument, so the assets tree is what gets
    #    checked and what gets shipped.
    baseline_files = stage(args.baseline, args.suite, assets, log)
    contract = vlib.read_json(assets / "source-contract.json")

    # 2. The contract against the tree it describes, before anything is built from
    #    either.  A contract that contradicts its own baseline would otherwise
    #    withhold every score at grading time.
    contract_facts = check_contract(assets, contract, log)

    # 3. The two Go instruments.  This is the only stage in the whole system with a
    #    Go toolchain, and the reason the image has one at all.
    reference, generator = build_instruments(
        args.suite, args.upstream, args.goroot, args.workspace, assets, log
    )

    # 4. The cases, from the generator that was just built.
    cases_dir, cases = generate_cases(generator, args.suite, assets, log)

    # 5. The answers, all three sources, each in full.
    counts = freeze_answers(reference, cases_dir, assets / "documents", log)

    # 6. One shard per module.  After the answers, because it splits both files in
    #    lockstep, and before the digests, because each shard is sealed.
    shards = shard_cases(cases_dir, log)

    # 7. The fresh census.  Nothing from the run is shipped; the census is, and the
    #    grader compares its own run against it.
    fresh = fresh_census(generator, assets, args.workspace, log)

    # 8. How many distinct problem texts the frozen expectations contain, using the
    #    shipped counter rather than a second one here.  instruction.md states this
    #    number to the agent and catalog.py holds the floor, so the derivation and
    #    the assertion have to be the same code.
    error_texts = len(catalogmod.distinct_problem_texts(cases_dir / "expected.ndjson"))

    # 9. The cases against this image's weight table.  Last, because it needs
    #    every count above.
    check_shape(cases_dir, cases, fresh, error_texts, log)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "task": TASK,
        "image": args.image,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream_version": UPSTREAM_VERSION,
        "zig_version": ZIG_VERSION,
        "digests": digest_all(assets, cases_dir, reference, generator),
        # Read back by driver.py, which re-runs the generator with this seed and
        # compares the census.  The families have to be declared rather than
        # counted because the file that would let them be counted does not exist
        # until grading time.
        "fresh": {"seed": FRESH_SEED, "count": FRESH_COUNT, "families": fresh},
        "counts": {
            "frozen_cases": cases["count"],
            "frozen_families": len(cases["families"]),
            "families": dict(cases["families"]),
            "document_cases": counts["document_digests"],
            "protocol_cases": counts["protocol"],
            "protocol_lines": counts["protocol_lines"],
            "fresh_cases": sum(fresh.values()),
            "graded_total": (cases["count"] + counts["document_digests"]
                             + counts["protocol"] + sum(fresh.values())),
            "error_texts": error_texts,
            "baseline_files": baseline_files,
            # The two extremes of `families` above, as scalars.  instruction.md
            # explains the weighting to the author by naming them -- "a family with
            # 38 cases and one with 2,484 carry comparable weight" -- and that
            # sentence is the only thing telling them why passing 2,484 easy cases
            # is not most of what the report says.  Flattened out of the nested dict because
            # the contract declares scalars and the drift check below compares
            # scalars; a figure only reachable by min()-ing a sub-dict is a figure
            # nothing on the host will ever compare.
            "smallest_family_cases": min(cases["families"].values()),
            "largest_family_cases": max(cases["families"].values()),
            # Per-module shard sizes.  A frozen module reads only its
            # own shard, so this is the count it checks itself against; the sum is
            # asserted equal to frozen_cases inside shard_cases.
            "module_cases": shards,
        },
        "contract": contract_facts,
        # Recorded for auditability.  The toolchain itself does not survive into
        # the grading stage; these paths exist in the build container alone.
        "reference": {
            "goroot": str(args.goroot),
            "upstream": str(args.upstream),
            "source": str(args.workspace / "gosrc"),
        },
    }
    vlib.write_json(assets / "verifier-manifest.json", manifest)

    graded = manifest["counts"]["graded_total"]
    # Read from the build context, not from the assets tree: these figures are the
    # verifier's, they are checked here and nowhere later, and `data/` on this side
    # of the image is exactly where a file nothing ships belongs.
    counts_path = args.suite / "data" / COUNTS_NAME
    if not counts_path.is_file():
        raise SystemExit(
            f"missing {counts_path}, which declares the case counts every host-side "
            f"document quotes; without it this build would freeze whatever the "
            f"generator produced and no prose copy would be checked against it"
        )
    declared_counts = vlib.read_json(counts_path)
    floor = declared_counts.get("min_graded_cases")
    if floor is not None and graded < floor:
        raise SystemExit(
            f"the frozen suite grades {graded} cases and case-counts.json promises "
            f"at least {floor}; the generator regressed"
        )

    # The declared counts, as equalities rather than floors.
    #
    # These numbers are stated to a reader in five files -- instruction.md tells
    # the agent what its port is measured against, evaluation.toml explains the
    # stage-3 gate with them, catalog.py and probe.py use them in comments about
    # why a budget or a floor is what it is.  None of those files can compute one:
    # the count exists only after this script runs, inside an image the author
    # never opens.  So data/case-counts.json declares them, this asserts the
    # declaration against what was just frozen, and tests/check-task.py asserts the
    # prose against that file.  A generator change moves a count, the build fails
    # here, and the fix is one edit in one file rather than five that drift.
    declared = declared_counts.get("case_counts") or {}
    drift = [
        f"{key}: case-counts.json declares {value}, the frozen suite has "
        f"{manifest['counts'][key]}"
        for key, value in sorted(declared.items())
        if not key.startswith("_") and key in manifest["counts"]
        and manifest["counts"][key] != value
    ]
    unknown = sorted(k for k in declared
                     if not k.startswith("_") and k not in manifest["counts"])
    if unknown:
        drift.append(f"case-counts.json declares counts this script does not "
                     f"produce: {unknown}; it produces "
                     f"{sorted(manifest['counts'])}")
    if drift:
        raise SystemExit(
            "the declared case_counts and the frozen suite disagree:\n  "
            + "\n  ".join(drift)
            + "\n\nUpdate tests/behavioural/data/case-counts.json to the frozen "
              "figures, then run tests/check-task.py --only figures to find the "
              "prose that still states the old ones."
        )
    log.write(f"declared case_counts agree: "
              f"{', '.join(f'{k}={declared[k]}' for k in sorted(declared) if not k.startswith('_'))}")
    # The anti-memorisation term is a published property of this task, so it is
    # asserted rather than described: at least a tenth of the behavioural total has
    # to be generated after the submission exists.
    share = sum(fresh.values()) / graded if graded else 0.0
    if share < 0.10:
        raise SystemExit(
            f"the fresh families are {share:.1%} of the {graded} graded cases; the "
            f"task promises at least 10% generated at grading time"
        )
    log.write(
        f"frozen: {graded} graded cases "
        f"({cases['count']} frozen, {sum(fresh.values())} fresh, "
        f"{counts['document_digests']} document, {counts['protocol']} protocol), "
        f"{error_texts} distinct error texts, fresh share {share:.1%}"
    )
    print(json.dumps(manifest["counts"], indent=1, sort_keys=True))
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
