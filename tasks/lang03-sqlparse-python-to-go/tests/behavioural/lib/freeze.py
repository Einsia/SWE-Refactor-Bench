#!/usr/bin/env python3
"""Freezes the grading inputs into the verifier image, at image build time.

Everything a submission is measured against is produced here, once, from the
pinned upstream sqlparse -- the same 0.5.3 the agent's workspace was unpacked
from.  By the time any submission exists, the document set, the catalog, the spec and
the expected answers are already files in a read-only layer.

That ordering is the design.  If the document set were generated at grading time from
the submitted tree, a submission could shrink it by deleting the files it is built
from; if the expectations were computed at grading time, the submission would be
in the container while the answers were being decided.  Neither is possible when
both are frozen in the image.

What is specific to this task is what the reference is *for*.  In a same-language
migration the reference install is also what downstream code links against, so it
has to survive into the grading image.  Here nothing links against it: State B is
a Go module, and the only program ever run against the reference is the Python
half of the probe, which runs here and never again.  So the reference is a
freeze-time instrument, and what survives into the image is the expectation blob
it produced.  The graded run imports no sqlparse and needs no interpreter beyond
its own.

Nine things are asserted, seven before any expectation is written and two after,
each of them a defect that is silent at authoring time and expensive at grading
time:

  * the contract carries every key the verifier dereferences without asking.  This
    is first because it is the cheapest, and because of what the omission costs
    without it: a contract missing `go_contract.standalone_closures` fails 140
    lines into Expectations.load with a bare KeyError naming the key and nothing
    else -- not the reader, not the file, and not the fact that the cause was a
    generator producing a partial contract.  The structure module reads the same
    key at grading time, so the same omission would reach grading.
  * the pin, end to end -- the unpacked reference must report 0.5.3.  Checked by
    asking the package rather than by trusting the tarball's name, because a
    re-pinned tarball would otherwise produce an oracle for a version the task
    does not claim.
  * the matched pair (paircheck) -- every op a case uses must be answerable by
    both halves.  An op the Python half lacks has no expected answer, so every
    submission fails those cases; an op a Go tier lacks answers `defect`, which
    turns a whole trial into a non-result.
  * the accessor scope table -- checked against the reference's real class
    hierarchy, because it is the one thing the Go half is told rather than
    discovers, and a stale entry grades the two halves against different
    questions.
  * the contract's own consistency (Expectations.load) -- it cross-checks the
    module path, the install inventory and the symbol lists against each other.
  * the round trip (skeleton + apidump + roundtrip) -- the contract is emitted as
    a Go module, compiled, read back by the API dumper, and the two symbol sets
    must be equal.  This is what keeps `structure.py` from reporting a symbol as
    both missing and extra because the contract spelled it differently.
  * the four probe tiers must compile against that skeleton.  A probe that does
    not build against the published API is a probe that can never run, and
    without a reference Go implementation the skeleton is the only thing to try
    it against.
  * and the one that runs afterwards, because it needs the answers themselves:
    no frozen payload may contain a CPython traceback.  A case whose expected
    stderr is an interpreter traceback is unpassable rather than hard -- the
    answer names the stdlib's paths and the reference's location inside this
    build container -- and it is indistinguishable, in a report, from a case a
    port got wrong.  spec.py names the two cases where the reference has an
    unhandled path and forces them onto a shape-based grade; this scan is the
    other direction, and catches an unhandled path nobody knew about.
  * and the second of the two afterwards: the freshness family, run against the
    reference.  That family is graded without an oracle -- it composes SQL after
    the reference is gone and asserts the round trip is lossless -- so whether its
    assertion is true of the reference has to be settled here, while the reference
    exists.  It does not hold unconditionally; see check_fresh_premise.

Nothing the round trip builds survives: the skeleton is written to the build
container's workspace and never staged, because shipping it would be shipping a
third of the answer to anyone who could read the image.

The manifest this writes records the digest of each frozen input.  The driver
checks those digests before grading anything, so a mismatch is reported as a
broken verifier rather than as a failing submission.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# `python3 -I` implies -P, which drops the script's own directory from sys.path,
# and every entry point in this suite is run that way -- so the sibling imports
# below need the directory put back explicitly.  driver.py does the same thing for
# the same reason.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build as buildmod  # noqa: E402
import catalog as catalogmod  # noqa: E402
import documents  # noqa: E402
import executor as executormod  # noqa: E402
import oracle  # noqa: E402
import paircheck  # noqa: E402
import provenance  # noqa: E402
import roundtrip  # noqa: E402
import skeleton as skeletonmod  # noqa: E402
import spec as specmod  # noqa: E402
import structure  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
TASK = "lang03-sqlparse-python-to-go"
UPSTREAM_VERSION = "0.5.3"

# Copied into the assets so grading has one directory to mount read-only.  The
# Python half of the probe is staged as well as the Go half: it is what produced
# the expectations, and an image that cannot show what its oracle was computed
# from is an image whose oracle cannot be audited.
# The consumer is absent on purpose: build.py generates it from the staged
# source-contract.json at grading time, so there is no copy here to drift.
STAGED_DIRS = ("probe", "apidump", "shim")
STAGED_FILES = ("source-contract.json",)

# The keyword tables, read out of the reference package rather than transcribed.
# Nine tables, 809 entries; the `keywords` tier's whole-table cases dump every one
# of them, so a transcription error here would be graded as a submission defect
# in the family that carries the heaviest weight in the suite.
KEYWORD_TABLES = (
    "KEYWORDS", "KEYWORDS_COMMON", "KEYWORDS_ORACLE", "KEYWORDS_MYSQL",
    "KEYWORDS_PLPGSQL", "KEYWORDS_HQL", "KEYWORDS_MSACCESS",
    "KEYWORDS_SNOWFLAKE", "KEYWORDS_BIGQUERY",
)

# The reference CLI is reached through a launcher rather than `python -m
# sqlparse.cli`, and the recipe lives in build.py: the graded path writes the same
# launcher over a pre-migration submission, and a second copy of the text here is a
# second thing that can drift from the one the frozen answers were computed with.
# build.launcher_text says why a launcher and not a module invocation.

# How many independent draws the freshness premise is checked over.  Twenty draws of
# forty is 800 documents, against the 40 a grading run composes: the check is looking
# for a document the generator emits rarely and the reference mishandles, and one
# draw's worth of confidence is not enough to ship an assertion on.  Each is a probe
# round trip on an already-running session, so the whole check is seconds.
FRESH_PREMISE_DRAWS = 20


def read_keyword_tables(reference_root: Path, log: Log) -> dict:
    """Read the nine keyword tables out of the reference, through an interpreter.

    Executed rather than parsed.  The tables are module-level dict literals built
    partly by comprehension in upstream's keywords.py, so a text scan would have to
    reimplement the parts of Python that produce them; running the module is the
    only way to get what the reference actually has.  This runs in a subprocess
    with the reference on PYTHONPATH so freeze.py itself never imports sqlparse --
    the one process that does is this child, and it exits before anything is
    graded.
    """
    program = (
        "import json\n"
        "from sqlparse import keywords as k\n"
        "names = %r\n"
        "out = {}\n"
        "for name in names:\n"
        "    table = getattr(k, name)\n"
        "    out[name] = sorted(table.keys())\n"
        "print(json.dumps(out))\n"
    ) % (list(KEYWORD_TABLES),)
    result = vlib.run(
        [vlib.PYTHON, "-c", program],
        env=buildmod.reference_env(reference_root),
        timeout=120.0,
        log=log,
        label="read-keyword-tables",
        full_capture=True,
        check=True,
    )
    tables = json.loads(result.stdout)
    missing = [name for name in KEYWORD_TABLES if not tables.get(name)]
    if missing:
        raise SystemExit(
            f"verifier corrupted: keyword table(s) empty or absent in the "
            f"reference: {missing}")
    total = sum(len(v) for v in tables.values())
    log.write(f"keywords: {len(tables)} table(s), {total} entries")
    return tables


# Every contract path the shipped verifier subscripts without a .get() fallback.
# Derived, not typed: _work/lang03/contractscan.py walks the AST of every module in
# this directory for subscripts rooted at `contract`/`self.contract`, follows
# function-scoped aliasing (`go = contract["go_contract"]` is how five modules are
# written), and cross-checks each path it reports against the shipped contract.
# _work/lang03/contractkeys.py answers the same question by deleting keys and
# running Expectations.load for real; its 12 keys are a subset of these, which is
# the agreement worth having, because a subtree passed as an argument is invisible
# to the scan and visible to the deletion.
REQUIRED_CONTRACT_PATHS = (
    "build_contract",
    "build_contract.go_directive_max",
    "build_contract.go_directive_min",
    "build_contract.module_path",
    "cli_contract",
    "cli_contract.graded_diagnostic_bits",
    "forbidden_paths",
    "go_code_policy",
    "go_code_policy.min_go_logic_lines",
    "go_contract",
    "go_contract.doc_comment_policy",
    "go_contract.doc_comment_policy.min_documented_ratio",
    "go_contract.module_path",
    "go_contract.packages",
    "go_contract.probe_tiers",
    "go_contract.standalone_closures",
    "go_contract.standalone_closures.closures",
    "install_inventory",
    "preserved_paths",
    "python_policy",
    "python_policy.must_delete",
    "state_a",
)


def check_contract_keys(contract: dict, log: Log) -> None:
    """Every path the verifier dereferences bare must be in the contract.

    This says nothing about whether the values are right -- Expectations.load does
    that, and does it better.  It says the contract is *complete enough to read*,
    and it says so with the key, the count and the instruction to regenerate,
    before any reader is entered.

    It exists because the contract is generated, and a generated file can come out
    partial -- a key inserted by a later pass over the finished contract is a key a
    regeneration that ran only the assembler would drop.  The failure that follows
    lands inside a loop over closures, on a key the contract carried an hour
    earlier, with a message naming the reader rather than the generator.  Every
    block comes from the single assembler for that reason, and this check names the
    problem either way.
    """
    missing = []
    for path in REQUIRED_CONTRACT_PATHS:
        cur = contract
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                missing.append(path)
                break
            cur = cur[part]
    if missing:
        raise SystemExit(
            f"verifier corrupted: source-contract.json is missing "
            f"{len(missing)} path(s) the verifier reads without a fallback:\n  "
            + "\n  ".join(missing)
            + "\nThis is what a partially generated contract looks like. "
              "Regenerate it with _work/lang03/assemble_contract.py, which is the "
              "only writer, and re-copy it to environment/source-contract.json."
        )
    log.write(f"contract keys: {len(REQUIRED_CONTRACT_PATHS)} required path(s) "
              f"present")


def check_reference_version(reference_root: Path, log: Log) -> None:
    """Assert the unpacked reference really is the pinned version.

    The tarball is the same artifact the agent's workspace was unpacked from, so
    this is checking the pin end to end rather than trusting the filename.
    """
    result = vlib.run(
        [vlib.PYTHON, "-c", "import sqlparse; print(sqlparse.__version__)"],
        env=buildmod.reference_env(reference_root),
        timeout=60.0,
        log=log,
        label="reference-version",
        full_capture=True,
        check=True,
    )
    found = result.stdout.decode("utf-8", "replace").strip()
    if found != UPSTREAM_VERSION:
        raise SystemExit(
            f"verifier corrupted: reference reports sqlparse {found!r}, "
            f"the task is pinned to {UPSTREAM_VERSION!r}")
    log.write(f"reference version: {found}")


def check_accessor_scope(reference_root: Path, lib_dir: Path,
                         log: Log) -> None:
    """Run spec.verify_accessor_scope against the reference's class hierarchy.

    In a child for the same reason the keyword read is: that function does
    `from sqlparse import sql`, and freeze.py's own interpreter has no sqlparse on
    its path -- the reference is only ever on a child's PYTHONPATH.

    What it proves is worth the extra process.  ACCESSOR_SCOPE is the table that
    tells the Go half which accessors a given node kind answers at all, and the Go
    half has no way to discover that for itself.  If the table and the reference
    disagree, the two halves are graded against different questions and every
    disagreement is charged to the submission.
    """
    program = (
        "import sys\n"
        f"sys.path.insert(0, {str(lib_dir)!r})\n"
        "import spec\n"
        "spec.verify_accessor_scope()\n"
        "print('ok')\n"
    )
    result = vlib.run(
        [vlib.PYTHON, "-c", program],
        env=buildmod.reference_env(reference_root),
        timeout=120.0,
        log=log,
        label="accessor-scope",
        full_capture=True,
    )
    if not result.ok:
        raise SystemExit(
            "verifier corrupted: spec.ACCESSOR_SCOPE disagrees with the "
            f"reference's class hierarchy:\n{result.tail(lines=20)}")
    log.write("accessor scope: the table matches the reference's classes")


def round_trip(contract: dict, workspace: Path, assets: Path,
               log: Log) -> dict:
    """Emit the contract as Go, compile it, read it back, and compare.

    Reads the staged assets rather than lib/, so what is checked here is what will
    ship: the Builder is handed the same directory a graded run is handed, which is
    also the only arrangement in which `tests_dir=` below means one thing.

    Returns a summary. Four separate assertions live here, and the order matters:
    the skeleton has to exist before anything can be compiled against it, the API
    dump has to succeed before the round trip can compare symbol lists, and the
    probe tiers are compiled last because a tier that fails to build is the most
    expensive failure to diagnose and the least likely -- by then the contract is
    known to be expressible and readable.

    Nothing built here is staged into the image. The skeleton is the contract's
    whole public surface, so shipping it would hand a third of the port to anyone
    who could read the grading layer.
    """
    log.section("contract round trip")
    scratch = workspace / "roundtrip"
    if scratch.exists():
        shutil.rmtree(scratch)
    skel = scratch / "skeleton"
    packages, symbols = skeletonmod.generate(contract, skel)
    log.write(f"skeleton: {packages} package(s), {symbols} contract symbol(s)")

    builder = buildmod.Builder(
        skel, scratch / "work", scratch / "shim", log, tests_dir=assets)
    buildmod.install_shim(assets / "shim" / "pyshim.py", builder.shim_dir, log)
    builder.prepare()
    if not builder.build().ok:
        raise SystemExit(
            "verifier corrupted: the contract's own skeleton does not compile; "
            "source-contract.json declares an API that is not valid Go")

    dump = builder.dump_api()
    if dump is None:
        raise SystemExit(
            "verifier corrupted: apidump could not read the contract's own "
            "skeleton, so no submission's API could be read either")
    problems = roundtrip.check(contract, dump)
    if problems:
        raise SystemExit(
            "verifier corrupted: the contract does not round trip through "
            "apidump:\n  " + "\n  ".join(problems))
    rt_packages, rt_symbols, rt_fields = roundtrip.summary(contract)
    log.write(f"round trip: {rt_packages} package(s), {rt_symbols} symbol(s), "
              f"{rt_fields} field(s) agree in both directions")

    consumer = builder.compile_consumer()
    if not consumer.ok:
        raise SystemExit(
            "verifier corrupted: the generated conformance consumer does not "
            "compile against the contract's own skeleton, so its declared types "
            f"do not compose:\n{consumer.tail(lines=25)}")

    tiers = builder.compile_tiers()
    broken = sorted(name for name, result in tiers.items() if not result.ok)
    if broken:
        detail = "\n".join(
            f"-- {name}:\n{tiers[name].tail(lines=15)}" for name in broken)
        raise SystemExit(
            f"verifier corrupted: probe tier(s) {broken} do not compile against "
            f"the contract's own skeleton, so they could never run against a "
            f"submission:\n{detail}")
    log.write(f"probe tiers compiled: {', '.join(sorted(tiers))}")

    return {
        "packages": rt_packages,
        "symbols": rt_symbols,
        "fields": rt_fields,
        "tiers": sorted(tiers),
        "consumer_ok": True,
    }


def check_state_a(baseline: Path, expect, log: Log) -> None:
    """The contract's recorded State A digests against the tree that ships.

    These are two descriptions of the same thing and they are used at different
    times, which is exactly the shape that drifts.  The tree is what the agent's
    workspace is unpacked from; the digests in source-contract.json are what
    structure.py falls back to when no tree is available, and what several
    preserved-file cases are stated in terms of.  If the tarball were re-rolled
    without regenerating the contract, every byte-identical case would compare a
    submission against a file that was never in its workspace -- and the failure
    would read as the submission having modified a doc it never touched.

    Checked in both directions, because the two failures are different: a file in
    the contract but not the tree is a stale contract, and a file in the tree but
    not the contract is a document no case can be stated about.
    """
    recorded = expect.state_a["files"]
    actual: dict[str, str] = {}
    for path in sorted(baseline.rglob("*")):
        if path.is_file():
            actual[path.relative_to(baseline).as_posix()] = vlib.sha256_file(path)

    problems: list[str] = []
    for rel in sorted(set(recorded) - set(actual)):
        problems.append(f"{rel}: in the contract, absent from the State A tree")
    for rel in sorted(set(actual) - set(recorded)):
        problems.append(f"{rel}: in the State A tree, absent from the contract")
    for rel in sorted(set(recorded) & set(actual)):
        want = recorded[rel].get("sha256", "")
        if want != actual[rel]:
            problems.append(
                f"{rel}: contract records {want[:16]}, tree has {actual[rel][:16]}")
    declared = expect.state_a.get("file_count")
    if declared is not None and declared != len(actual):
        problems.append(
            f"state_a.file_count says {declared}, the tree has {len(actual)}")

    if problems:
        raise SystemExit(
            "verifier corrupted: source-contract.json's State A record disagrees "
            "with the State A tree:\n  " + "\n  ".join(problems[:20])
            + (f"\n  (+{len(problems) - 20} more)" if len(problems) > 20 else ""))
    log.write(f"State A: {len(actual)} file(s) match the contract's digests")


# Byte sequences that appear only in output CPython produced about itself.  A
# frozen answer containing any of them is an answer no Go port can reproduce.
#
# Each is decisive on its own.  The traceback header and the frame prefix are the
# interpreter's report of an uncaught exception; the caret run is how 3.11 marks
# the failing expression; and either absolute path is a location on the machine
# that built this image -- /tmp because the reference is unpacked into the build
# container's scratch, /usr/lib/python because a stdlib frame appeared.
TRACEBACK_MARKERS = (
    b"Traceback (most recent call last)",
    b'  File "',
    b"^^^^",
    b"/tmp/",
    b"/usr/lib/python",
)


def check_diagnostic_bits(contract: dict, log: Log) -> None:
    """The contract's `graded_diagnostic_bits` must be what the grader compares.

    The contract is the document the agent reads, and this part of it says which
    three properties of an error diagnostic are graded.  Nothing else in the
    verifier reads cli_contract -- the enforcement lives in spec.py's grades and in
    the frozen answers -- so without this the prose and the comparison could drift
    apart silently, and the drift would be invisible in both directions: a bit
    described but not compared is a requirement no submission has to meet, and a
    bit compared but not described is one no submission was told about.

    Both directions are checked, against a diagnostic the executor actually
    renders rather than against a parse of its source.
    """
    described = set(contract["cli_contract"]["graded_diagnostic_bits"])
    rendered = executormod.CliRunner.diagnostic(
        vlib.Result(argv=["sqlformat"], cwd="/", returncode=2, stdout=b"",
                    stderr=b"usage: x\n", duration=0.0))
    emitted = {field.split(b"=", 1)[0].decode() for field in rendered.split(b" ")}
    if described != emitted:
        raise SystemExit(
            "verifier corrupted: the contract describes diagnostic bits "
            f"{sorted(described)} but the grader compares {sorted(emitted)}. "
            "A bit in one list and not the other is either an unstated "
            "requirement or an unenforced promise.")
    log.write(f"diagnostic bits: contract and grader agree on {sorted(emitted)}")


def check_struct_applicability(catalog: dict, log: Log) -> None:
    """Every structural check is classified as askable or not of a Python tree.

    structure.py splits the family in two -- the checks a pre-migration tree answers
    on its own terms, and the ones that read a Go artifact and are therefore stage
    1's question -- and `evaluate` refuses a check in neither table.  That refusal
    only fires when a Python tree is actually graded, which on a normal grading run
    is never, so without this the hole would ship and surface as the oracle failing
    its own corpus.

    Checked in both directions against the catalog rather than against a list here.
    A name in the tables and not in the catalog is a stale entry that reads like
    coverage; a name in the catalog and not in the tables is a case whose
    applicability nobody decided.  The count is deliberately not the assertion: two
    sets of thirty-two that disagree on two names have the same length.
    """
    log.section("check structural applicability is total")
    declared = {case["check"] for case in catalog["cases"]
                if case.get("kind") == "struct"}
    classified = set(structure.PY_STRUCT_KEEP) | set(structure.PY_STRUCT_SKIP)
    unclassified = sorted(declared - classified)
    stale = sorted(classified - declared)
    if unclassified or stale:
        raise SystemExit(
            "verifier corrupted: structure.py's applicability tables and the "
            "catalog's structural cases disagree.\n"
            + (f"  no applicability decided for: {unclassified}\n"
               if unclassified else "")
            + (f"  named in the tables but not in the catalog: {stale}\n"
               if stale else "")
            + "  Every structural check has to be either answerable by a "
              "pre-migration tree or skipped with a reason; see "
              "structure.PY_STRUCT_SKIP_GROUPS.")
    log.write(f"applicability: {len(declared)} structural check(s), "
              f"{len(structure.PY_STRUCT_KEEP)} askable of a pre-migration tree, "
              f"{len(structure.PY_STRUCT_SKIP)} skipped with a reason")

    # The provenance side of the same question, and it needs only the one
    # direction: a name in PY_PROV_SKIP that the catalog does not declare is a skip
    # that never fires, and the case it was meant to cover would be answered
    # vacuously with nothing to say so.  The other direction is not a hole here --
    # provenance's default is to ask, so an unnamed case is asked, which is what
    # the 40 `fresh` cases want.
    prov = {case["check"] for case in catalog["cases"]
            if case.get("kind") == "provenance"}
    stale_prov = sorted(set(provenance.Provenance.PY_PROV_SKIP) - prov)
    if stale_prov:
        raise SystemExit(
            "verifier corrupted: provenance.Provenance.PY_PROV_SKIP names "
            f"{stale_prov}, which the catalog does not declare as a provenance "
            "check. The skip would never fire and the case it names would be "
            "answered against scaffolding the build does not install.")
    log.write(f"applicability: {len(prov)} provenance check(s), "
              f"{len(provenance.Provenance.PY_PROV_SKIP)} skipped on a "
              f"pre-migration tree")


def check_portable_answers(store: oracle.ExpectationStore, log: Log) -> None:
    """No frozen answer may contain something only CPython could have written.

    This runs after the freeze rather than before it, because it is a property of
    the answers and not of the cases.  The failure it catches is the worst-behaved
    kind this suite can have: the case runs, the comparison is fair, the report
    says the submission's stderr differed from the expected stderr -- and the
    expected stderr was `/tmp/freeze-work/reference/sqlparse/cli.py`, which is a
    path inside a container that no longer exists.  Nothing downstream can tell
    that from a port that mishandled the error.

    One case reached this state before the check existed, and it was the sibling of
    a case already fixed for the same reason.  The fix each time is to grade the
    diagnostic's shape rather than its text; see spec.py's `status-and-diag`.
    """
    log.section("check answers are portable")
    bad: list[str] = []
    for key in sorted(store.entries):
        payload = store.payload(key)
        found = [m.decode("ascii", "replace") for m in TRACEBACK_MARKERS
                 if m in payload]
        if found:
            bad.append(f"{key}: expected answer contains {found}")
    if bad:
        raise SystemExit(
            "verifier corrupted: {} case(s) were frozen with an answer only "
            "CPython could produce, so no submission can pass them. Grade the "
            "diagnostic's shape instead (spec.py: status-and-diag):\n  {}".format(
                len(bad), "\n  ".join(bad[:20])))
    log.write(f"portable: none of {len(store.entries)} answers carries a "
              f"CPython traceback or an absolute build path")


def check_fresh_premise(
    catalog: dict, assets: Path, out: Path, spec_json: Path, workspace: Path,
    env: dict, log: Log,
) -> dict:
    """The freshness family must pass against the reference, or it is unpassable.

    That family is the one thing in this stage graded without an oracle.  It composes
    SQL at grading time, after the reference is gone, and asserts that parsing then
    concatenating reproduces the input -- true of the reference, so true of a correct
    port.  If it is *not* true of the reference for some document the generator can
    emit, the family fails a correct submission on that document, and a report cannot
    tell that from a port that loses text.  It is not true unconditionally: the
    reference drops a newline following a terminating semicolon, and 119 of 3,200
    generated documents ended that way.  The draw removes trailing newlines because
    of that measurement.

    So the premise is measured here, where the reference still exists, against many
    more draws than one grading run makes.  What is exercised is `provenance` itself
    rather than a restatement of it: the same module, the same draw, the same filters,
    the same assertions, with the runner pointed at the reference probe instead of a
    submission's.  A copy of the logic would pass while the module failed.

    `outcome=None` on purpose -- only the fresh family is evaluated here, and the
    shim ledger it would read is a property of a graded build, which this is not.
    """
    log.section("check the freshness premise against the reference")
    import provenance as provenancemod

    probe_py = assets / "probe" / "probe.py"
    if not probe_py.is_file():
        raise SystemExit(
            f"verifier corrupted: no staged probe at {probe_py}; the freshness "
            f"premise cannot be checked without the reference probe")

    def runner(docs_dir: Path, docsmeta: Path, cases: list[dict]):
        driver = executormod.ProbeDriver(
            executormod.reference_argv(probe_py, docs_dir, docsmeta, spec_json),
            cwd=workspace, env=env, log=log, label="fresh-premise",
            progress_every=1000,
        )
        return driver.run(cases)

    # The cases come out of the catalog that will ship, not from a list written here.
    # A list written here would be a second declaration of the family, and the thing
    # most worth catching is the catalog and the module disagreeing.
    cases = [case for case in catalog["cases"]
             if case.get("kind") == "provenance" and case.get("family") == "fresh"]
    if len(cases) != provenancemod.FRESH_DOCUMENTS:
        raise SystemExit(
            f"verifier corrupted: the catalog declares {len(cases)} fresh "
            f"document case(s) and provenance.py composes "
            f"{provenancemod.FRESH_DOCUMENTS}. Every case past the draw fails for "
            f"every submission")
    checked = 0
    failures: list[str] = []
    for draw in range(FRESH_PREMISE_DRAWS):
        scratch = workspace / "fresh-premise" / f"draw-{draw:02d}"
        # `repo` and `outcome` are the submission's; the fresh family reads neither,
        # and there is no submission at freeze time.
        prov = provenancemod.Provenance(
            out, None, scratch, log, runner=runner, docs_root=out)
        for result in prov.evaluate(cases):
            checked += 1
            if not result.passed and len(failures) < 8:
                index = int(result.case_id.rsplit("-", 1)[1])
                doc_id, sql = prov.fresh_documents()[index]
                failures.append(
                    f"seed {prov.seed:#018x} {doc_id}: {result.detail}\n"
                    f"      document: {sql!r}")
            elif not result.passed:
                failures.append("...")
    if failures:
        raise SystemExit(
            "verifier corrupted: the freshness family does not hold for the "
            "reference, so it would fail a correct port. Either restrict the draw in "
            "provenance.fresh_documents or drop the assertion -- do not ship a case "
            "no submission can pass:\n  "
            + "\n  ".join(f for f in failures if f != "...")
            + (f"\n  (and {sum(1 for f in failures if f == '...')} more)"
               if any(f == "..." for f in failures) else ""))
    log.write(
        f"freshness: the reference round-trips losslessly on all {checked} "
        f"document(s) across {FRESH_PREMISE_DRAWS} draw(s) of "
        f"{provenancemod.FRESH_DOCUMENTS}")
    return {"draws": FRESH_PREMISE_DRAWS, "documents": checked}


def stage_assets(lib_dir: Path, data_dir: Path, assets: Path, log: Log) -> dict:
    """Copy the inputs grading reads into one directory, and digest each one.

    Two sources, because the suite keeps code and data apart: the probe halves and
    the shim are code and live under lib/, the contract is data and lives under
    data/.  Staging flattens them into one directory so grading has a single thing
    to mount read-only and a single digest table to check it against.

    The digests are what the driver checks before it grades anything, so this is
    also where the answer to "was this image built from the sources it claims"
    gets decided.  Directories are digested file by file rather than as an
    archive, because a tar of the same files differs run to run in mtimes and
    ownership and would make every image look modified.
    """
    log.section("stage grading assets")
    digests: dict[str, str] = {}
    for name in STAGED_DIRS:
        src = lib_dir / name
        if not src.is_dir():
            raise SystemExit(f"verifier corrupted: no lib/{name}/ to stage")
        dst = assets / name
        if dst.exists():
            shutil.rmtree(dst)
        count = vlib.copy_tree(src, dst)
        for path in sorted(dst.rglob("*")):
            if path.is_file():
                digests[str(path.relative_to(assets))] = vlib.sha256_file(path)
        log.write(f"staged {name}/ ({count} file(s))")
    for name in STAGED_FILES:
        src = data_dir / name
        if not src.is_file():
            raise SystemExit(f"verifier corrupted: no data/{name} to stage")
        dst = assets / name
        shutil.copy2(src, dst)
        digests[name] = vlib.sha256_file(dst)
        log.write(f"staged {name}")
    return digests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", required=True, type=Path,
                        help="the pinned upstream sqlparse tarball")
    parser.add_argument("--baseline", required=True, type=Path,
                        help="the unpacked State A tree the document set is built from")
    parser.add_argument("--out", required=True, type=Path,
                        help="where the frozen assets are written")
    parser.add_argument("--workspace", type=Path,
                        help="scratch; defaults to <out>/../freeze-work")
    parser.add_argument("--data", type=Path,
                        help="the suite's data/ directory; defaults to ../data")
    args = parser.parse_args()

    # Two directories, not one.  lib/ holds this file and the probe/apidump/shim
    # sources; data/ holds the contract and the pinned tarballs.  They were a single
    # `tests_dir` when the verifier was one flat directory, and keeping it that way
    # would have meant either putting data under lib/ or resolving each read against
    # a path that is right for half its uses.
    lib_dir = Path(__file__).resolve().parent
    data_dir = (args.data or lib_dir.parent / "data").resolve()
    if not data_dir.is_dir():
        raise SystemExit(f"verifier corrupted: no data directory at {data_dir}")
    out = args.out.resolve()
    workspace = (args.workspace or out.parent / "freeze-work").resolve()
    out.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)

    log = Log(out / "freeze.log")
    started = time.time()
    log.section(f"freeze {TASK} (upstream sqlparse {UPSTREAM_VERSION})")

    contract = json.loads(
        (data_dir / "source-contract.json").read_text(encoding="utf-8"))

    # 1. The contract's completeness, then its internal consistency.  In that
    #    order: Expectations.load dereferences a dozen of these paths bare, so an
    #    incomplete contract fails inside it with a KeyError that names a key and
    #    hides the cause.
    check_contract_keys(contract, log)
    expect = structure.Expectations.load(contract)
    log.write(f"contract: {len(expect.packages)} package(s), module "
              f"{contract['go_contract']['module_path']}")
    check_state_a(args.baseline.resolve(), expect, log)
    check_diagnostic_bits(contract, log)

    # 2. The reference, unpacked and version-checked.
    installer = buildmod.ReferenceInstaller(args.tarball, workspace, log)
    reference_root = installer.install()
    check_reference_version(reference_root, log)
    check_accessor_scope(reference_root, lib_dir, log)
    keywords = read_keyword_tables(reference_root, log)
    vlib.write_json(out / "keywords.json", keywords)

    # 3. The document set, from the baseline tree.
    log.section("build document set")
    docs_meta = documents.build(args.baseline, out)
    log.write(f"documents: {len(docs_meta['documents'])} document(s), "
              f"digest {docs_meta['digest']}")

    # 4. The spec both probe halves read, and the catalog of cases.
    spec_json = out / "spec.json"
    vlib.write_json(spec_json, specmod.as_json())
    catalog = catalogmod.build_catalog(docs_meta, keywords)
    vlib.write_json(out / "catalog.json", catalog)
    counts = catalog["counts"]
    log.write(f"catalog: {counts['total']} case(s) "
              f"({counts['behavioural']} behavioural), digest {catalog['digest']}")
    check_struct_applicability(catalog, log)

    # 5. The assets, staged and digested.
    #
    # Before the checks that read them, not after.  Everything below this line
    # reads the staged copy rather than the source directory, so the pair check and
    # the round trip exercise the files that will actually ship -- a probe that was
    # correct in lib/ and truncated by staging would otherwise pass every check
    # here and fail on the first submission.
    assets = out / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    asset_digests = stage_assets(lib_dir, data_dir, assets, log)

    # State A ships alongside them.  Grading needs the tree itself, not just the
    # digests: an "unchanged" case that fails should be able to say what the file
    # used to contain, and a diff needs both sides.
    baseline_dst = assets / "baseline"
    if baseline_dst.exists():
        shutil.rmtree(baseline_dst)
    baseline_files = vlib.copy_tree(args.baseline, baseline_dst)
    baseline_digests = {
        path.relative_to(assets).as_posix(): vlib.sha256_file(path)
        for path in sorted(baseline_dst.rglob("*")) if path.is_file()
    }
    asset_digests.update(baseline_digests)
    log.write(f"staged baseline/ ({baseline_files} file(s))")

    # 6. The matched pair: every op a case uses must be answerable by both halves.
    problems = paircheck.check(catalog, contract, assets / "probe")
    if problems:
        raise SystemExit(
            "verifier corrupted: the probe halves do not match:\n  "
            + "\n  ".join(problems))
    log.write("pair check: both probe halves answer every op the catalog uses")

    # 7. The contract round trip, and the compiles that depend on it.
    rt = round_trip(contract, workspace, assets, log)

    # 8. The expectations, from the reference.
    env = buildmod.reference_env(reference_root)
    driver = executormod.ProbeDriver(
        executormod.reference_argv(
            assets / "probe/probe.py", out / "docs", out / "documents.json",
            spec_json),
        cwd=workspace, env=env, log=log, label="reference", progress_every=500,
    )
    launcher = workspace / "sqlformat"
    # No `tree` argument: `env` already carries the reference on PYTHONPATH here,
    # and passing both would bake a path into a script that only exists for the
    # length of this build.  The graded path has no such environment and passes the
    # tree instead; build.launcher_text documents the difference.
    launcher.write_text(buildmod.launcher_text(vlib.PYTHON))
    launcher.chmod(0o755)
    cli = executormod.CliRunner(
        launcher, out / "docs", log, label="reference",
        work_dir=workspace / "cliwork", env=env,
    )
    store_digest, store_meta = oracle.freeze(
        catalog, docs_meta, driver, cli, out, log,
        reference_version=UPSTREAM_VERSION,
        extra_meta={"round_trip": rt},
    )

    # Re-read what was just written and check every answer is one a Go port could
    # in principle produce.  Loading it back rather than checking the records in
    # memory is deliberate: it is the file grading will read, and the load
    # re-verifies both digests on the way in.
    written = oracle.ExpectationStore(out / "expectations.json",
                                      out / "expectations.bin")
    written.load()
    check_portable_answers(written, log)

    # The one family graded without an oracle, checked against the oracle while there
    # still is one.  Last, because it needs the staged probe and the document set.
    fresh = check_fresh_premise(
        catalog, assets, out, spec_json, workspace, env, log)

    # 9. The manifest the driver checks before it grades anything.
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "task": TASK,
        "upstream_version": UPSTREAM_VERSION,
        "frozen_seconds": round(time.time() - started, 1),
        "catalog_digest": catalog["digest"],
        "documents_digest": docs_meta["digest"],
        "expectations_digest": store_digest,
        "spec_digest": vlib.sha256_file(spec_json),
        "keywords_digest": vlib.sha256_file(out / "keywords.json"),
        # The staged copy, which is the one grading opens.  Digesting data/ instead
        # would certify a file no graded run reads.
        "contract_digest": vlib.sha256_file(assets / "source-contract.json"),
        "counts": counts,
        "floors": catalog["floors"],
        "weight_share": catalog["weight_share"],
        "round_trip": rt,
        "fresh_premise": fresh,
        "expectations": store_meta,
        "assets": dict(sorted(asset_digests.items())),
    }
    vlib.write_json(out / "manifest.json", manifest)
    log.write(f"manifest: {len(asset_digests)} asset digest(s), "
              f"expectations {store_digest}")
    log.write(f"freeze complete in {manifest['frozen_seconds']}s")
    log.close()

    print(f"froze {TASK}: {counts['total']} cases, "
          f"{len(docs_meta['documents'])} documents, "
          f"expectations {store_digest[:16]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
