#!/usr/bin/env python3
"""Freezes the grading inputs into the verifier image, at image build time.

Everything a submission is measured against is produced here, once, from the
pinned upstream release -- the same acorn 8.14.0 tarball the agent's workspace was
unpacked from.  By the time any submission exists, the corpus, the expectations
and the fixture digests are already files in a read-only layer.

That ordering is the design.  If the corpus were generated at grading time from
the submitted tree, a submission could shrink it by deleting what it is built
from; if the expectations were computed at grading time, the submission would be
in the container while the answers were being decided.  Neither is possible when
both are frozen here.

What is specific to this task is that the reference is JavaScript, and JavaScript
is the thing the migration is *out of*.  So the two are separated by role rather
than by presence:

  - `node` and the reference tree survive into the grading image, because the CLI
    differential runs both sides live, argv for argv.  driver.py resolves node
    once, by absolute path, off `build.py`'s REAL_PATH.
  - the submission's build never reaches them.  `build.py` puts a tripwire
    directory ahead of a PATH that has no interpreter on it anyway, so an attempt
    is recorded rather than merely failing.
  - rollup and buble, which build the reference's `dist/`, exist in the freeze
    stage and nowhere else.  Nothing after this file needs a JavaScript build.

Three things are checked here rather than trusted.

The corpus is answered in full before anything is written: a case the reference
cannot answer has no correct answer, and one that crashes it is a defective case,
not a defective submission.  Either fails the image build.

The reference is proved to be the real acorn by running State A's own test suite
against the `dist/` that was just built -- 6,763 tests, the number instruction.md
quotes to the agent.  A reference that builds but misparses would otherwise freeze
a corpus that is internally consistent and wrong.

The corpus is checked against catalog.py's weight table by the same
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
import audit  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"
TASK = "lang04-acorn-js-to-rust"
UPSTREAM_VERSION = "8.14.0"
RUST_TOOLCHAIN = "1.90.0"

# State A's own suite, run against the reference this file builds.  Both halves
# are asserted because instruction.md quotes the total to the agent as the thing
# the reference tree is good for.
UPSTREAM_TESTS = 6763

# The floor the frozen corpus has to clear, asserted at the end of main().  It is
# a constant here rather than a `grading.min_graded_cases` key in
# source-contract.json.  Nothing else reads it and no document quotes it, so
# there is no drift to guard against -- unlike UPSTREAM_TESTS above, which
# instruction.md states and which is therefore checked rather than declared.
MIN_GRADED_CASES = 2000

# The frozen cases whose expected answer is an *engine* crash inside the
# reference rather than a diagnostic acorn produces, listed by the (op, message)
# pair `check_unportable` derives from expected.ndjson.  Typed here so the scan
# has something independent to disagree with; the count is summed from the table
# rather than written beside it, because a hand-typed total next to a hand-typed
# table is two copies of one claim (see the note in catalog.py).
#
# Why they cannot be graded, and why that is not a corpus generator to fix:
#
#   acorn constructs exactly one error class deliberately -- `new SyntaxError`,
#   acorn/src/location.js:15 -- and `check_unportable` asserts that is still the
#   only one.  Every other throw in the three trees is either acorn-walk's
#   internal `Found` sentinel or a rethrow.  So an expectation whose `kind` is
#   not "SyntaxError" is V8 reporting that the reference did something illegal,
#   and its `message` is V8's wording *for the code shape that did it*.
#
#   Two shapes are in the corpus.  `walk.full` and friends over `import` or
#   `export *`: acorn-walk 8.3.4's `base.ImportDeclaration` and
#   `base.ExportAllDeclaration` iterate `node.attributes` unguarded -- their two
#   `Export*Declaration` siblings guard it with `if (node.attributes)` -- and
#   acorn 8.14.0 never sets the property.  `parseExpressionAt` with `pos` past
#   end of source: `strictDirective` indexes a regexp match that did not match.
#
#   The message is part of the contract (instruction.md's response schema
#   requires `message` for any throw), and it is not a property of acorn: the
#   same acorn-walk source crashing the same way renders "Cannot read properties
#   of undefined (reading 'length')" through the reference's rollup+buble ES5
#   bundle and "node.attributes is not iterable" as an ES2020 for-of.
#   `check_unportable` measures both, and fails the build if they ever agree.
#   A port that reproduced either would have to reproduce a bundling artifact;
#   a port that did the sane thing -- iterate an empty list -- answers `ok:true`
#   and is marked wrong.  There is no answer to grade, so these cases are not
#   graded: `driver.run_differential` emits no check for them, and the modules'
#   rates are over the cases they asked.  They are not skipped-with-a-reason any
#   more, because a skip is charged 0 now and charging an id no submission can
#   ever pass would put it in every submission's denominator forever.  The record
#   is kept where it costs nothing -- a module note and `unportable_cases` in the
#   result metadata -- and the premise is still proved here before any of it.
UNPORTABLE_SHAPES = {
    ("walk_full", "Cannot read properties of undefined (reading 'length')"): 11,
    ("walk_full_ancestor",
     "Cannot read properties of undefined (reading 'length')"): 11,
    ("find_node_around",
     "Cannot read properties of undefined (reading 'length')"): 3,
    ("walk_simple", "Cannot read properties of undefined (reading 'length')"): 2,
    ("walk_recursive",
     "Cannot read properties of undefined (reading 'length')"): 2,
    ("parse_expression_at", "Cannot read properties of null (reading '0')"): 2,
}
UNPORTABLE_CASES = sum(UNPORTABLE_SHAPES.values())

# The three bundles State A's package.json builds, and the entry point each one
# has to produce for `require(<pkg>)` to resolve through its package.json `main`.
# Listed rather than globbed: a rollup config that silently stopped emitting one
# of these would leave a reference that loads and answers a subset of the corpus.
DIST_ENTRIES = {
    "acorn": "acorn/dist/acorn.js",
    "acorn-walk": "acorn-walk/dist/walk.js",
    "acorn-loose": "acorn-loose/dist/acorn-loose.js",
}

# Copied into the assets so grading has one directory to mount read-only.  The
# two JavaScript instruments go with them: `reference.js` is run live by the CLI
# phase and by `--identity`, and `jsshim.py` is the tripwire build.py installs.
STAGED_DIRS = ("shim",)
# From `lib/`: code this suite executes.
STAGED_FILES = ("reference.js",)
# From `data/`: inputs shared with the other stages, which is why they live in a
# directory of their own.  `source-contract.json` is the same file the agent was
# handed, and the verification stage reads its own copy of it; a check that graded
# against a constant written into a Python module here could drift from the
# document the submission was given.
STAGED_DATA_FILES = ("source-contract.json",)


def sh(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
       label: str, log: Log, stdin: bytes | None = None,
       timeout: float = 1800.0) -> subprocess.CompletedProcess:
    """Run one freeze-time command, aborting the image build on failure.

    Deliberately not vlib.run: nothing here is being graded, so there is no
    outcome to record and no reason to tolerate a failure.  Anything that goes
    wrong at freeze time is a broken image, and the useful behaviour is to stop
    with the tool's own words in the build log.
    """
    log.write(f"$ {' '.join(argv)}")
    proc = subprocess.run(
        argv, cwd=str(cwd) if cwd else None, env=env, input=stdin,
        capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-4000:]
        out = proc.stdout.decode("utf-8", "replace")[-2000:]
        raise SystemExit(
            f"freeze: {label} failed ({proc.returncode}):\n{tail}\n{out}"
        )
    err = proc.stderr.decode("utf-8", "replace").strip()
    if err:
        for line in err.splitlines()[-40:]:
            log.write(f"  {line}")
    return proc


def node_env(node_bin: Path, **overrides) -> dict:
    """The environment every JavaScript step here runs in.

    `ACORN_REFERENCE_REPO` is how both instruments find the reference tree; they
    default to the grading image's path, and at freeze time the tree is still
    being assembled, so it is passed explicitly rather than assumed.

    The heap ceiling is not tuning.  One fixture response -- ember.js through
    `walk_full` -- is 56 MB of JSON, and it is built as a single string before it
    is hashed; the default old-space on a small container is below that, and the
    failure mode is an OOM abort partway through the fixture digests.
    """
    env = vlib.base_env(
        PATH=f"{node_bin.parent}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        NODE_OPTIONS="--max-old-space-size=8192",
    )
    env.update({k: v for k, v in overrides.items() if v is not None})
    return env


def stage(baseline: Path, tests: Path, data: Path, assets: Path, log: Log) -> int:
    """Copy the verifier's fixed inputs into the assets tree.

    The baseline copy is what tells a rewritten file from an untouched one, and it
    comes from the verifier's own tarball rather than anything a submission could
    reach.  It is the *whole* archive -- `test/`, the fixtures, the changelogs --
    because the contract's file count and byte total are of the tree the agent was
    actually given, and audit.py compares retained files against it byte for
    byte.
    """
    log.write("staging fixed inputs")
    dest_baseline = assets / "baseline"
    if dest_baseline.exists():
        shutil.rmtree(dest_baseline)
    files = vlib.copy_tree(baseline, dest_baseline)
    for name in STAGED_DIRS:
        src = tests / name
        if not src.is_dir():
            raise SystemExit(f"missing verifier input directory: {src}")
        dest = assets / name
        if dest.exists():
            shutil.rmtree(dest)
        vlib.copy_tree(src, dest)
    for root, names in ((tests, STAGED_FILES), (data, STAGED_DATA_FILES)):
        for name in names:
            src = root / name
            if not src.is_file():
                raise SystemExit(f"missing verifier input: {src}")
            (assets / name).write_bytes(src.read_bytes())
    # Named explicitly because a staging bug here surfaces as every case failing
    # to run, which reads like a broken submission.  `check_reference_tree` looks
    # for these exact two paths before it grades anything.
    for required in (assets / "shim" / "jsshim.py",
                     assets / "reference.js",
                     assets / "source-contract.json"):
        if not required.is_file():
            raise SystemExit(f"staging did not produce {required}")
    log.write(
        f"staged baseline ({files} files), {', '.join(STAGED_DIRS)}, "
        f"{', '.join(STAGED_FILES + STAGED_DATA_FILES)}"
    )
    return files


def build_reference(baseline: Path, buildtools: Path, node_bin: Path,
                    assets: Path, log: Log) -> Path:
    """Build State A's `dist/` and prove the result is really acorn.

    The reference is a second copy of the baseline rather than the baseline
    itself.  Building in place would leave `dist/` inside the tree the gates
    compare against, and then a submission that shipped a `dist/acorn.js` would
    be compared against a baseline that had one too.

    `node_modules` is a sibling of the tree with three symlinks into it, which is
    what upstream does with in-repo links and what the agent environment does out
    of tree.  It matters at run time, not build time: the configs mark `acorn`
    external, so the emitted UMD bundles `require("acorn")` by bare name, and
    `acorn-loose/dist` can only resolve that through a `node_modules` above it.

    Then State A's own suite runs against what was built.  This is the step that
    makes the frozen corpus a differential against real acorn: rollup will happily
    emit a bundle from sources that parse and produce wrong trees, and every
    expectation below is this bundle's opinion.
    """
    reference = assets / "reference"
    if reference.exists():
        shutil.rmtree(reference)
    vlib.copy_tree(baseline, reference)

    # Inside the reference tree, and named `node_modules` exactly: node's
    # resolution only recognises directories by that name, and it finds them by
    # walking up from the importing file.  A differently-named directory beside the
    # tree resolves nothing -- not the repository's own packages, and not buble
    # from the rollup plugin that imports it.
    #
    # buble's own dependency is acorn 6.4.2, which would take the top-level
    # `acorn` name and shadow the repository's.  Nesting it under its only
    # consumer is what npm does for a version conflict, and it frees the name --
    # the same move the agent environment makes, for the same reason.
    modules = reference / "node_modules"
    if modules.exists():
        shutil.rmtree(modules)
    vlib.copy_tree(buildtools / "node_modules", modules)
    nested = modules / "buble" / "node_modules"
    nested.mkdir(parents=True, exist_ok=True)
    for name in ("acorn", "acorn-dynamic-import", "acorn-jsx"):
        stray = modules / name
        if stray.is_dir():
            shutil.move(str(stray), str(nested / name))
    if (modules / "acorn").exists():
        raise SystemExit(
            f"{modules}/acorn still exists after nesting buble's copy; the "
            f"reference would build and load against the wrong acorn"
        )
    # The three packages, linked to the reference's own copies.  This is what makes
    # the emitted bundles loadable: the configs mark `acorn` external, so
    # `acorn-loose/dist/acorn-loose.js` requires it by bare name, and node resolves
    # that by walking up to this directory.
    #
    # Relative targets, so the reference tree can be copied or mounted at another
    # path without every link dangling.
    for name in DIST_ENTRIES:
        link = modules / name
        if link.is_symlink() or link.exists():
            link.unlink()
        os.symlink(os.path.join("..", name), link)

    env = node_env(node_bin)
    rollup = modules / "rollup" / "dist" / "bin" / "rollup"
    if not rollup.is_file():
        raise SystemExit(f"no rollup at {rollup}; the buildtools were not staged")
    # The three configs State A's own `npm run build` runs, in its order.  Invoked
    # directly rather than through `npm run` so the freeze does not depend on npm
    # existing in this stage, and so a failure names the config that failed.
    for name, entry in DIST_ENTRIES.items():
        sh([str(node_bin), str(rollup), "-c", f"{name}/rollup.config.mjs"],
           cwd=reference, env=env, label=f"rollup {name}", log=log, timeout=900.0)
        built = reference / entry
        if not built.is_file() or built.stat().st_size == 0:
            raise SystemExit(f"rollup {name} left no {entry}")

    # The CLI phase runs this file directly, so it has to be executable as well as
    # present.  It is a two-line shim over dist/bin.js in State A, and the tarball
    # carries its mode, but the copy is what gets run.
    binary = reference / "acorn" / "bin" / "acorn"
    if not binary.is_file():
        raise SystemExit(f"the reference has no CLI at {binary}")
    binary.chmod(0o755)

    out = sh([str(node_bin), "test/run.js"], cwd=reference, env=env,
             label="State A test suite", log=log, timeout=1800.0)
    report = out.stdout.decode("utf-8", "replace")
    if "all passed" not in report:
        raise SystemExit(
            f"State A's own suite did not pass against the reference that was "
            f"just built; the corpus would be frozen from a broken parser:\n"
            f"{report[-3000:]}"
        )
    if f"Total: {UPSTREAM_TESTS} tests run" not in report:
        raise SystemExit(
            f"State A's suite ran a different number of tests than the "
            f"{UPSTREAM_TESTS} instruction.md promises the agent:\n"
            f"{report[-1500:]}"
        )
    log.write(
        f"reference built and verified: {report.strip().splitlines()[-1].strip()}"
    )
    return reference


def build_corpus(tests: Path, reference: Path, node_bin: Path, assets: Path,
                 seed: int, generated: int, log: Log) -> tuple[Path, dict]:
    """Generate the corpus from the reference that was just built.

    The corpus is a function of the generator, the seed and the reference tree:
    half of it is harvested out of `test/tests-*.js` by loading the suite's own
    driver, and half is walked out of a seeded grammar.  It is generated rather
    than checked in because a corpus shipped as data would drift from the
    generator that produced it, and then nothing in the image would know which of
    the two was the corpus.

    The harvested half is, deliberately, inputs the agent can read: upstream's
    test suite is in the tarball, and passing it is the job.  What the agent
    cannot read is the generated half -- the seed lives in this image only, and
    the corpus is never copied to the agent's side of the task.
    """
    corpus = assets / "corpus"
    if corpus.exists():
        shutil.rmtree(corpus)
    corpus.mkdir(parents=True)
    generator = tests / "gen_corpus.js"
    if not generator.is_file():
        raise SystemExit(f"the corpus generator is missing at {generator}")
    sh([str(node_bin), str(generator), "--out", str(corpus),
        "--seed", str(seed), "--generated", str(generated)],
       env=node_env(node_bin, ACORN_REFERENCE_REPO=str(reference)),
       label="gen_corpus.js", log=log, timeout=1800.0)

    cases = json.loads((corpus / "cases.json").read_text(encoding="utf-8"))
    requests = sum(1 for _ in (corpus / "requests.ndjson").open("rb"))
    if requests != cases["count"]:
        raise SystemExit(
            f"cases.json declares {cases['count']} cases and requests.ndjson has "
            f"{requests} lines"
        )
    if cases.get("seed") != seed:
        raise SystemExit(
            f"the corpus records seed {cases.get('seed')!r} but was generated "
            f"with {seed!r}; driver.py reports the recorded one"
        )
    fixtures = json.loads(
        (corpus / "fixtures.json").read_text(encoding="utf-8"))["fixtures"]
    log.write(
        f"corpus: {cases['count']} cases in {len(cases['families'])} families, "
        f"{len(fixtures)} fixture cases, seed {seed}, generated {generated}"
    )
    return corpus, cases


def freeze_answers(reference: Path, corpus: Path, assets: Path, node_bin: Path,
                   log: Log) -> dict[str, int]:
    """Answer every frozen case, and refuse to write a partial set.

    Both sources are answered whole.  A request the reference cannot answer has no
    correct answer, and one that crashes it is a defective case rather than a
    defective submission -- so a short output aborts the image build, where the
    cause is visible, rather than becoming a case every submission fails for a
    reason no report can explain.

    The fixtures are answered into digests rather than bytes.  Six bundles through
    six operations is 176 MB of JSON; what is kept is a whole-response hash, a byte
    length, and a hash per 32 KB block so a failing submission can still be told
    where its answer first diverged.
    """
    driver = assets / "reference.js"
    env = node_env(node_bin, ACORN_REFERENCE_REPO=str(reference))
    requests = corpus / "requests.ndjson"
    expected = corpus / "expected.ndjson"
    sh([str(node_bin), str(driver), "--batch", str(requests), str(expected)],
       env=env, label="reference.js --batch", log=log, timeout=3600.0)
    want = sum(1 for _ in requests.open("rb"))
    got = sum(1 for _ in expected.open("rb"))
    if got != want:
        raise SystemExit(
            f"the reference answered {got} of {want} frozen requests; a corpus "
            f"with an unanswerable case must not be frozen"
        )

    fixture_dir = reference / "test" / "bench" / "fixtures"
    if not fixture_dir.is_dir():
        raise SystemExit(f"the reference has no fixture sources at {fixture_dir}")
    sh([str(node_bin), str(driver), "--fixtures", str(corpus / "fixtures.json"),
        str(corpus / "fixture-digests.json"), str(fixture_dir)],
       env=env, label="reference.js --fixtures", log=log, timeout=3600.0)
    manifest = json.loads(
        (corpus / "fixtures.json").read_text(encoding="utf-8"))["fixtures"]
    digests = json.loads(
        (corpus / "fixture-digests.json").read_text(encoding="utf-8"))["digests"]
    missing = [e["key"] for e in manifest if e["key"] not in digests]
    if missing:
        raise SystemExit(
            f"{len(missing)} fixture case(s) got no digest from the reference: "
            f"{missing[:6]}"
        )
    # The block digests are what `probe.py::_fixture_diff` reports a divergence
    # from.  A record without them still grades correctly and tells a failing
    # submission nothing, so their absence is a build failure rather than a
    # degraded message.
    for key, record in sorted(digests.items()):
        if not record.get("blocks"):
            raise SystemExit(f"fixture {key} was frozen without block digests")
    total = sum(r["bytes"] for r in digests.values())
    log.write(
        f"answers: {got} frozen responses, {len(digests)} fixture digests "
        f"over {total} bytes of reference output"
    )
    return {"expected": got, "fixture_digests": len(digests),
            "fixture_bytes": total}


def _prove_deliberate_errors(reference: Path, log: Log) -> str:
    """Assert acorn still constructs exactly one error class on purpose.

    The exclusion rule below reads `kind != "SyntaxError"` as "the reference
    crashed", and that inference is only sound while acorn never raises anything
    else deliberately.  If a future release throws a `TypeError` at an API
    misuse, that would be a diagnostic a port must reproduce, and this rule would
    start excluding real cases -- so the premise is measured here, against the
    tree the corpus is about to be frozen from, rather than assumed.
    """
    pattern = (r"new (SyntaxError|TypeError|RangeError|Error|EvalError|"
               r"ReferenceError)\(")
    srcs = [reference / pkg / "src" for pkg in DIST_ENTRIES]
    missing = [str(p) for p in srcs if not p.is_dir()]
    if missing:
        raise SystemExit(f"the reference has no sources at {missing}")
    found = subprocess.run(
        ["grep", "-rnE", pattern, *[str(p) for p in srcs]],
        capture_output=True, text=True,
    )
    # grep exits 1 for "no matches", which is itself a failure here: acorn raises
    # its syntax errors somewhere, and finding none means the grep is wrong.
    if found.returncode not in (0, 1):
        raise SystemExit(f"grep over the reference sources failed: {found.stderr}")
    hits = [ln for ln in found.stdout.splitlines() if ln.strip()]
    others = [h for h in hits if "new SyntaxError(" not in h]
    if others:
        raise SystemExit(
            "the reference constructs an error class other than SyntaxError:\n  "
            + "\n  ".join(h[:160] for h in others)
            + "\nfreeze.UNPORTABLE_SHAPES reads a non-SyntaxError expectation as "
              "an engine crash, which is only sound while SyntaxError is the only "
              "one acorn raises on purpose. Re-derive the rule."
        )
    if not hits:
        raise SystemExit(
            "no error construction found in the reference sources at all; the "
            "premise behind the unportable-case rule cannot be checked"
        )
    where = ", ".join(h.split(":", 2)[0].replace(str(reference) + "/", "")
                      + ":" + h.split(":", 2)[1] for h in hits)
    log.write(f"reference raises one error class deliberately: SyntaxError at {where}")
    return where


def _prove_message_is_a_bundling_artifact(reference: Path, node_bin: Path,
                                          log: Log) -> tuple[str, str]:
    """Crash the same acorn-walk source two ways and assert the words differ.

    This is the whole argument for excluding these cases in one measurement: if
    the ES5 bundle and the ES2020 source produced the same message, the message
    would be a property of the crash and a port could be asked for it.  They do
    not, so it is a property of how `dist/` was built -- and the build tools that
    produced it are deleted from the grading image.  If a future toolchain makes
    them agree, this fails and the exclusion has to be re-argued rather than
    inherited.
    """
    script = """
const acorn = require(process.argv[1]);
const ast = acorn.parse("import a from 'm';",
                        {ecmaVersion: 2022, sourceType: "module"});
const out = {};
for (const [label, mod] of [["dist", process.argv[2]], ["src", process.argv[3]]]) {
  try { require(mod).full(ast, () => {}); out[label] = null }
  catch (e) { out[label] = {kind: e.constructor.name, message: e.message} }
}
process.stdout.write(JSON.stringify(out));
"""
    walk = reference / "acorn-walk"
    proc = subprocess.run(
        [str(node_bin), "-e", script, str(reference / "acorn"),
         str(walk / "dist" / "walk.js"), str(walk / "src" / "index.js")],
        capture_output=True, text=True,
        env=node_env(node_bin, ACORN_REFERENCE_REPO=str(reference)),
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"could not measure the two acorn-walk renderings: {proc.stderr[:400]}")
    got = json.loads(proc.stdout)
    for label in ("dist", "src"):
        entry = got.get(label)
        if entry is None:
            raise SystemExit(
                f"acorn-walk's {label} rendering walked an `import` without "
                f"throwing. The unguarded `node.attributes` iteration is gone, so "
                f"the frozen expectations that record it as a crash are stale: "
                f"regenerate the corpus instead of excluding them."
            )
        if entry["kind"] != "TypeError":
            raise SystemExit(
                f"acorn-walk's {label} rendering threw {entry['kind']}, not "
                f"TypeError; the unportable-case rule was derived from TypeError"
            )
    if got["dist"]["message"] == got["src"]["message"]:
        raise SystemExit(
            "both acorn-walk renderings report the same message "
            f"({got['dist']['message']!r}), so the message is a property of the "
            "crash rather than of the bundle. A port could then be asked for it "
            "and these cases should be graded, not excluded."
        )
    log.write(
        f"the crash message is a bundling artifact: dist says "
        f"{got['dist']['message']!r}, the same source as ES2020 says "
        f"{got['src']['message']!r}"
    )
    return got["dist"]["message"], got["src"]["message"]


def check_unportable(reference: Path, corpus: Path, cases: dict, node_bin: Path,
                     log: Log) -> dict:
    """Find the cases no port can answer, having first proved they exist.

    Returns the record written to `corpus/excluded.json`, which is digested into
    the manifest like every other frozen input.  Grading reads that file; it does
    not re-derive the rule, because the reference's sources are what the rule is
    about and a submission's tree is not them.
    """
    dist_msg, src_msg = _prove_message_is_a_bundling_artifact(
        reference, node_bin, log)
    raises_at = _prove_deliberate_errors(reference, log)

    family_of = {int(c["id"]): str(c.get("family") or "unknown")
                 for c in cases["cases"]}
    found: dict[int, dict] = {}
    shapes: dict[tuple[str, str], int] = {}
    with (corpus / "requests.ndjson").open("rb") as reqs, \
         (corpus / "expected.ndjson").open("rb") as exps:
        for raw_req in reqs:
            raw_req = raw_req.rstrip(b"\n")
            if not raw_req:
                continue
            raw_exp = vlib.read_ndjson_line(exps)
            if raw_exp is None:
                raise SystemExit(
                    "expectations ran out while scanning for unportable cases")
            err = (json.loads(raw_exp).get("error") or {})
            kind = err.get("kind")
            if not kind or kind == "SyntaxError":
                continue
            req = json.loads(raw_req)
            cid = int(req["id"])
            op = req.get("op", "?")
            message = err.get("message", "")
            if cid not in family_of:
                raise SystemExit(
                    f"case {cid} is in requests.ndjson and not in cases.json")
            found[cid] = {
                "id": cid,
                "op": op,
                "family": family_of[cid],
                "kind": kind,
                "message": message,
            }
            shapes[(op, message)] = shapes.get((op, message), 0) + 1

    if shapes != UNPORTABLE_SHAPES:
        extra = {k: v for k, v in shapes.items() if UNPORTABLE_SHAPES.get(k) != v}
        gone = {k: v for k, v in UNPORTABLE_SHAPES.items() if shapes.get(k) != v}
        raise SystemExit(
            f"the corpus holds {len(found)} unportable case(s) in shapes that are "
            f"not the ones freeze.UNPORTABLE_SHAPES declares.\n"
            f"  measured but not declared (or a different count): {extra}\n"
            f"  declared but not measured (or a different count): {gone}\n"
            f"Every one of these is excluded from grading, so the set is reviewed "
            f"by hand rather than followed."
        )
    if len(found) != UNPORTABLE_CASES:
        raise SystemExit(
            f"{len(found)} unportable case(s) found and UNPORTABLE_SHAPES sums to "
            f"{UNPORTABLE_CASES}; two ids share an (op, message) shape"
        )

    # No family may be excluded whole.  A family reduced to nothing would score
    # 0.0 over an empty pool and take its weight with it, which is the opposite of
    # what a skip is for -- and it would mean an operation the port has to
    # implement is no longer measured anywhere.
    per_family: dict[str, int] = {}
    for rec in found.values():
        per_family[rec["family"]] = per_family.get(rec["family"], 0) + 1
    emptied = [f for f, n in per_family.items()
               if n >= int(dict(cases["families"]).get(f, 0))]
    if emptied:
        raise SystemExit(
            f"excluding the unportable cases would empty {emptied}; a family with "
            f"nothing left to ask is not measured at all"
        )

    reason = (
        "the reference's own answer is an internal crash, and the message the "
        "response schema requires is V8's wording for the shape of the code that "
        "crashed, not anything acorn defines: the same acorn-walk source renders "
        f"{dist_msg!r} through the reference's ES5 bundle and {src_msg!r} as "
        "ES2020. No port can be asked for it, and a port that handles the input "
        "sanely answers ok:true and would be marked wrong."
    )
    record = {
        "schema": "lang04-unportable-cases-v1",
        "reason": reason,
        "rule": (
            "expected error.kind != 'SyntaxError'. acorn constructs exactly one "
            f"error class deliberately ({raises_at}); every other throw in the "
            "three packages is acorn-walk's internal Found sentinel or a rethrow, "
            "so any other kind is the engine reporting that the reference did "
            "something illegal."
        ),
        "causes": [
            "acorn-walk 8.3.4's base.ImportDeclaration and "
            "base.ExportAllDeclaration iterate node.attributes unguarded -- "
            "base.ExportNamedDeclaration and base.ExportDefaultDeclaration guard "
            "it with `if (node.attributes)` -- and acorn 8.14.0 never sets the "
            "property, so every walk over `import` or `export *` crashes.",
            "acorn's strictDirective indexes a regexp match that did not match "
            "when parseExpressionAt is given a pos past end of source.",
        ],
        "renderings": {"es5_bundle": dist_msg, "es2020_source": src_msg},
        "count": len(found),
        "by_op": {f"{op} :: {msg}": n for (op, msg), n in sorted(shapes.items())},
        "by_family": dict(sorted(per_family.items())),
        "cases": [found[cid] for cid in sorted(found)],
    }
    vlib.write_json(corpus / "excluded.json", record)
    log.write(
        f"unportable: {len(found)} case(s) over {len(per_family)} families are "
        f"excluded from grading and skipped with a reason "
        f"({', '.join(f'{f} {n}' for f, n in sorted(per_family.items()))})"
    )
    return record


def check_contract(assets: Path, contract: dict, archive: Path, log: Log) -> dict:
    """Assert the contract against itself and against the baseline it describes.

    Every figure in here is graded against, so every figure is re-derived from the
    tree rather than trusted.  The failure being prevented is the one that keeps
    recurring in this benchmark: a requirement stated in two places, drifting in
    one of them, and grading every correct submission against a number nobody
    published.  Doing it at freeze time makes that an image-build failure instead.

    Seven things are checked, and each is checked because something downstream
    reads it as fact:

      - the archive digest, against the tarball this image actually unpacked.
      - the file count and byte total, against the staged baseline.
      - every anchor file exists.  audit.py's source-closure gates look for
        these by name, and a gate searching for a file that never existed searches
        for nothing.
      - every retained path exists in the baseline.  audit.py compares the
        submission's copy against it byte for byte, and a missing baseline file
        turns that into "present (no baseline to compare)" -- a check that passes
        by having nothing to say.
      - the Rust floor is below State A's own logic-line total, counted with the
        same function audit.py grades with.  A floor above it would demand a
        port longer than the original for no stated reason.
      - the two binaries' names match the basenames of their paths, and the probe's
        name matches `probe_protocol.binary`.  structure.py reads one and build.py
        reads the other.
      - the install inventory's native entries are exactly those two binaries.
    """
    baseline = assets / "baseline"
    state_a = contract.get("state_a") or {}
    problems: list[str] = []

    want_digest = state_a.get("archive_sha256")
    if want_digest:
        got_digest = vlib.sha256_file(archive)
        if got_digest != want_digest:
            problems.append(
                f"the contract declares archive sha256 {want_digest[:16]} and the "
                f"tarball this image unpacked is {got_digest[:16]}"
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

    for name in state_a.get("anchor_files") or []:
        if not (baseline / name).is_file():
            problems.append(
                f"anchor file {name} is not in the baseline; a gate searching for "
                f"it would be searching for nothing"
            )
    for name in (contract.get("retained_paths") or {}).get("required") or []:
        if not (baseline / name).is_file():
            problems.append(
                f"retained path {name} is required to survive unchanged but is not "
                f"in the baseline to compare against"
            )

    # State A's own size, on the rule audit.py grades the port with.  One
    # counter for both sides is what makes the floor comparable to the original at
    # all -- a different rule here would compare two different measurements.
    js_total = 0
    js_files = 0
    for sub in ("acorn/src", "acorn-loose/src", "acorn-walk/src"):
        sources = sorted((baseline / sub).rglob("*.js"))
        js_files += len(sources)
        js_total += audit.count_rust_logic_lines(sources)
    policy = contract.get("native_code_policy") or {}
    floor = policy.get("min_rust_logic_lines")
    if floor is not None and js_total and floor >= js_total:
        problems.append(
            f"the Rust floor ({floor}) is not below State A's {js_total} "
            f"JavaScript logic lines; the floor would demand a port longer than "
            f"the original"
        )
    if policy.get("rust_toolchain") != RUST_TOOLCHAIN:
        problems.append(
            f"the contract pins Rust {policy.get('rust_toolchain')!r} but this "
            f"image is built for {RUST_TOOLCHAIN!r}"
        )
    product = contract.get("product") or {}
    if product.get("upstream_version") != UPSTREAM_VERSION:
        problems.append(
            f"the contract names upstream {product.get('upstream_version')!r} but "
            f"this image is built from {UPSTREAM_VERSION!r}"
        )

    state_b = contract.get("state_b") or {}
    binaries = state_b.get("binaries") or []
    if not binaries:
        problems.append("state_b declares no binary; there would be nothing to grade")
    for entry in binaries:
        if Path(entry.get("path", "")).name != entry.get("name"):
            problems.append(
                f"a binary is named {entry.get('name')!r} but built at "
                f"{entry.get('path')!r}; structure.py reads the name and build.py "
                f"reads the path"
            )
    declared_probe = (contract.get("probe_protocol") or {}).get("binary")
    if declared_probe not in {e.get("name") for e in binaries}:
        problems.append(
            f"probe_protocol.binary is {declared_probe!r}, which is not one of the "
            f"binaries state_b declares"
        )
    native = sorted(Path(e["path"]).name
                    for e in state_b.get("install_inventory") or []
                    if e.get("native"))
    if native != sorted(e.get("name") for e in binaries):
        problems.append(
            f"the install inventory's native entries are {native} and state_b "
            f"declares binaries {sorted(e.get('name') for e in binaries)}"
        )

    if problems:
        raise SystemExit(
            "the contract does not describe the tree it ships with:\n  "
            + "\n  ".join(problems)
        )
    log.write(
        f"contract self-consistent: {len(files)} baseline files, {got_bytes} "
        f"bytes, {js_files} JavaScript sources totalling {js_total} logic lines, "
        f"Rust floor {floor}, binaries "
        f"{', '.join(e['name'] for e in binaries)}"
    )
    return {"baseline_files": len(files), "baseline_bytes": got_bytes,
            "js_logic_lines": js_total, "js_source_files": js_files,
            "rust_line_floor": floor,
            "binaries": [e["name"] for e in binaries]}


def check_shape(cases: dict, fixtures: int, log: Log) -> None:
    """Run the grader's own catalog check over the corpus that was just frozen.

    `check_catalog` is the function driver.py calls at grading time.  Calling it
    here as well is what makes a coverage regression an image-build failure: a
    family the weight table does not know, a family the table knows and the corpus
    lost, a corpus or fixture count below its floor.  Each of those would
    otherwise be graded quietly at a fallback weight.
    """
    problems = catalogmod.check_catalog(dict(cases["families"]), fixtures)
    if problems:
        raise SystemExit(
            "the frozen corpus and this image's catalog disagree:\n  "
            + "\n  ".join(problems)
        )
    log.write(
        f"catalog agrees: {len(cases['families'])} families over "
        f"{cases['count']} cases, {fixtures} fixture cases, "
        f"{len(catalogmod.CLI_CASES)} CLI, {len(catalogmod.STRUCT_CASES)} "
        f"structural, {len(catalogmod.GUARD_CASES)} gates"
    )


def digest_all(assets: Path) -> dict[str, str]:
    """Digest every input driver.py will check before it grades anything.

    The list is catalog.FROZEN_INPUTS, and the key names in it are the ones
    `driver.Assets` looks up: a digest recorded under a name nothing reads is dead
    weight, and an input read without one is ungraded evidence.  One table, read
    from both sides, because a second copy is a way for the two to disagree -- and
    the guard that catches that reports "the manifest ships a digest nothing looks
    up", which names the symptom rather than the copy that drifted.

    Every path is relative to the asset root, including the corpus ones, which is
    the same directory by construction (`build_corpus` writes to
    `assets / "corpus"`).
    """
    files = {key: assets / rel for key, rel in catalogmod.FROZEN_INPUTS.items()}
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
    parser.add_argument("--archive", required=True, type=Path,
                        help="the tarball itself, digested against the contract")
    parser.add_argument("--buildtools", required=True, type=Path,
                        help="the installed rollup + buble tree, this stage only")
    parser.add_argument("--assets", required=True, type=Path,
                        help="where the frozen inputs are written")
    parser.add_argument("--node", type=Path, default=Path("/usr/local/bin/node"),
                        help="the interpreter that builds and answers the reference")
    parser.add_argument("--tests", type=Path,
                        default=Path(__file__).resolve().parent,
                        help="the suite's lib/ directory: the generator, the "
                             "reference responder and the interpreter shim")
    parser.add_argument("--data", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data",
                        help="the suite's data/ directory: the inputs shared with "
                             "the other stages, source-contract.json among them")
    parser.add_argument("--seed", type=int, default=20260730,
                        help="the corpus seed; lives in this image only")
    parser.add_argument("--generated", type=int, default=700,
                        help="how many synthetic sources the generator walks")
    parser.add_argument("--image", default="")
    args = parser.parse_args(argv)
    # Resolved for the same reason driver.py resolves its own: every path here is
    # handed to a subprocess that runs with a different cwd, and a relative one
    # would resolve somewhere else entirely.
    for name in ("baseline", "archive", "buildtools", "assets", "node", "tests",
                 "data"):
        setattr(args, name, getattr(args, name).resolve())
    if not args.node.is_file():
        raise SystemExit(f"no node at {args.node}")

    assets: Path = args.assets
    assets.mkdir(parents=True, exist_ok=True)
    log = Log(assets / "freeze.log")
    log.section("freeze verifier assets")

    # 1. The fixed inputs, including State A itself.  Everything below reads the
    #    staged copy rather than the argument, so the assets tree is what gets
    #    checked and what gets shipped.
    baseline_files = stage(args.baseline, args.tests, args.data, assets, log)
    contract = vlib.read_json(assets / "source-contract.json")

    # 2. The contract against the tree it describes, before anything is built from
    #    either.  A contract that contradicts its own baseline would otherwise
    #    withhold every score at grading time.
    contract_facts = check_contract(assets, contract, args.archive, log)

    # 3. The reference: State A's dist/, built with the pinned toolchain, then put
    #    through State A's own suite.  Every expectation below is this tree's
    #    opinion, so this is where a wrong one has to be caught.
    reference = build_reference(
        args.baseline, args.buildtools, args.node, assets, log
    )

    # 4. The corpus, from the generator and the seed.
    corpus, cases = build_corpus(
        args.tests, reference, args.node, assets, args.seed, args.generated, log
    )

    # 5. The answers, both sources, each in full.
    counts = freeze_answers(reference, corpus, assets, args.node, log)

    # 6. The cases the answers show have no portable answer.  After the freeze,
    #    because the rule is read off the expectations; before check_shape, so a
    #    family emptied by the exclusion is caught while the counts still say
    #    which one.
    unportable = check_unportable(reference, corpus, cases, args.node, log)

    # 7. The corpus against this image's weight table.  Last, because it needs
    #    every count above.
    check_shape(cases, counts["fixture_digests"], log)

    # What a submission is actually asked.  The excluded cases are subtracted
    # rather than described in a note: `graded_total` is quoted as the size of the
    # measured surface, and a total that counts cases nothing will ask is wrong by
    # exactly the amount it is convenient by.
    graded = (cases["count"] - unportable["count"] + counts["fixture_digests"]
              + len(catalogmod.CLI_CASES))
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "task": TASK,
        "image": args.image,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream_version": UPSTREAM_VERSION,
        "rust_toolchain": RUST_TOOLCHAIN,
        "digests": digest_all(assets),
        # Recorded because the corpus is a function of these two and of the
        # generator; a report that says "15,975 cases" is only reproducible if it
        # also says what produced them.
        "corpus": {"seed": args.seed, "generated": args.generated},
        "counts": {
            "corpus_cases": cases["count"],
            "corpus_families": len(cases["families"]),
            "families": dict(cases["families"]),
            "unportable_cases": unportable["count"],
            "fixture_cases": counts["fixture_digests"],
            "fixture_bytes": counts["fixture_bytes"],
            "cli_cases": len(catalogmod.CLI_CASES),
            "struct_cases": len(catalogmod.STRUCT_CASES),
            "gate_cases": len(catalogmod.GUARD_CASES),
            "graded_total": graded,
            "upstream_tests": UPSTREAM_TESTS,
            "baseline_files": baseline_files,
        },
        "contract": contract_facts,
    }
    vlib.write_json(assets / "verifier-manifest.json", manifest)

    if graded < MIN_GRADED_CASES:
        raise SystemExit(
            f"the frozen suite grades {graded} cases and MIN_GRADED_CASES is "
            f"{MIN_GRADED_CASES}; the corpus regressed"
        )
    log.write(
        f"frozen: {graded} graded cases "
        f"({cases['count']} corpus less {unportable['count']} with no portable "
        f"answer, {counts['fixture_digests']} fixture, "
        f"{len(catalogmod.CLI_CASES)} CLI), "
        f"{len(catalogmod.STRUCT_CASES)} structural, "
        f"{len(catalogmod.GUARD_CASES)} gates"
    )
    print(json.dumps(manifest["counts"], indent=1, sort_keys=True))
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

