#!/usr/bin/env python3
"""Audit: the per-file ISA partition survived the migration.

Contract §1.5. Two independent views of the same rule, because either one alone
is fakeable:

* **flags** — read from `compile_commands.json`: each of the 15 specialised
  translation units must receive a flag that grants its own ISA, and none of the
  104 ordinary units may receive an elevating flag.
* **instructions** — read by disassembling every object file: each specialised
  unit must actually contain its ISA, and no ordinary unit may contain anything
  above the x86-64 baseline. This is what catches a global `-march=native` or a
  blanket `-mavx2`, which would make the library die with SIGILL on older CPUs.
"""
import json
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.migration

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import isa_scan  # noqa: E402

with open(os.path.join(SUITE, "data", "isa.json")) as _fh:
    ISA = json.load(_fh)
with open(os.path.join(SUITE, "data", "flags.json")) as _fh:
    FLAGS = json.load(_fh)

ELEVATED_ISA = ISA["elevated"]                 # 14 sources, by disassembly
BASELINE_ISA_SOURCES = ISA["baseline_sources"]  # 105 sources
BASELINE_ALLOWED = set(ISA["baseline_allowed"])  # {"sse2"}

ELEVATED_FLAGS = FLAGS["elevated"]             # 15 sources, by compile flags
BASELINE_FLAG_SOURCES = FLAGS["baseline"]      # 104 sources
FORBIDDEN_FLAGS = FLAGS["forbidden_for_baseline"]


# --------------------------------------------------------------------- flags --
def _flags_by_source(build):
    """{source_stem: [flags]} from compile_commands.json."""
    out = {}
    for rel, flags in build.flags_per_source().items():
        stem = os.path.basename(rel)
        for suf in (".c", ".S"):
            if stem.endswith(suf):
                stem = stem[: -len(suf)]
                break
        out.setdefault(stem, [])
        out[stem].extend(flags)
    return {k: sorted(set(v)) for k, v in out.items()}


@pytest.fixture(scope="session")
def flagmap(b_default):
    if not b_default.built:
        pytest.fail("default build failed:\n%s" % b_default.failure_summary())
    m = _flags_by_source(b_default)
    if not m:
        pytest.fail(
            "no compile database: cmake was invoked with "
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON but no compile_commands.json "
            "was produced, so per-file flags cannot be verified")
    return m


@pytest.mark.parametrize("source", sorted(ELEVATED_FLAGS))
def test_elevated_source_receives_its_isa_flag(flagmap, source):
    spec = ELEVATED_FLAGS[source]
    got = flagmap.get(source)
    assert got is not None, (
        "translation unit %s was never compiled (expected it in the %s ISA group)"
        % (source, spec["family"]))
    accepted = set(spec["required_any"])
    assert accepted & set(got), (
        "%s belongs to the %s group and must be compiled with one of %s; it got "
        "%s (§1.5)" % (source, spec["family"], sorted(accepted), got or "no -m flags"))


@pytest.mark.parametrize("source", sorted(BASELINE_FLAG_SOURCES))
def test_baseline_source_receives_no_isa_flag(flagmap, source):
    got = flagmap.get(source)
    assert got is not None, "translation unit %s was never compiled" % source
    bad = sorted(set(got) & set(FORBIDDEN_FLAGS))
    assert bad == [], (
        "%s is an ordinary translation unit but was compiled with %s; §1.5 keeps "
        "ISA flags per-file — raising the baseline for the whole library makes it "
        "SIGILL on CPUs without the extension" % (source, bad))


def test_no_global_march_native(b_default):
    if not b_default.built:
        pytest.fail(b_default.failure_summary())
    hits = [rel for rel, flags in b_default.flags_per_source().items()
            if any(f.startswith("-march=") for f in flags)]
    assert hits == [], "-march=... applied to %d translation units: %s" % (
        len(hits), hits[:8])


def test_translation_unit_count(flagmap):
    """119 units in State A; a migration that drops files is not equivalent."""
    expected = FLAGS["n_translation_units"]
    assert len(flagmap) >= expected, (
        "only %d library translation units were compiled, State A has %d"
        % (len(flagmap), expected))


# -------------------------------------------------------------- disassembly --
@pytest.fixture(scope="session")
def isamap(b_default):
    """{source_stem: [isa names]} by disassembling every object in the build."""
    if not b_default.built:
        pytest.fail("default build failed:\n%s" % b_default.failure_summary())
    dest = os.path.join(b_default.root, "isa.json")
    rc = subprocess.run([sys.executable, os.path.join(SUITE, "lib", "isa_scan.py"),
                         b_default.bld, dest],
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        timeout=3600)
    if rc.returncode != 0 or not os.path.isfile(dest):
        pytest.fail("ISA scan failed: %s" % rc.stdout.decode("utf-8", "replace")[-500:])
    with open(dest) as fh:
        return json.load(fh)


@pytest.mark.parametrize("source", sorted(ELEVATED_ISA))
def test_elevated_object_contains_its_isa(isamap, source):
    spec = ELEVATED_ISA[source]
    entry = isamap.get(source)
    assert entry is not None, (
        "no object file was produced for %s; State A compiles it into the %s "
        "implementation" % (source, "/".join(spec["required"])))
    found = set(entry["isa"])
    required = set(spec["required"])
    missing = sorted(required - found)
    assert not missing, (
        "%s must contain %s instructions but its object only shows %s — the ISA "
        "flags did not reach it (§1.5)"
        % (source, missing, sorted(found) or "baseline code only"))


@pytest.mark.parametrize("source", sorted(BASELINE_ISA_SOURCES))
def test_baseline_object_has_no_elevated_isa(isamap, source):
    entry = isamap.get(source)
    assert entry is not None, "no object file was produced for %s" % source
    found = set(entry["isa"])
    illegal = sorted(found - BASELINE_ALLOWED)
    assert illegal == [], (
        "%s contains %s instructions but State A compiles it at the x86-64 "
        "baseline; a library built this way crashes with SIGILL on CPUs without "
        "those extensions (§1.5). Objects: %s"
        % (source, illegal, entry["objects"][:3]))


def test_isa_scan_covered_the_whole_library(isamap):
    expected = set(ELEVATED_ISA) | set(BASELINE_ISA_SOURCES)
    missing = sorted(expected - set(isamap))
    assert missing == [], (
        "%d of State A's translation units produced no object file: %s"
        % (len(missing), missing[:12]))
