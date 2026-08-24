#!/usr/bin/env python3
"""Behavioural: pkg-config parity, end to end.

Contract §1.7. Not just "a .pc file exists" — a downstream program is compiled
and linked with the flags pkg-config reports, then run, and its output is
compared with the same program built against State A.
"""
import json
import os
import re
import shlex

import pytest

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import consumers  # noqa: E402
import elfutil  # noqa: E402

with open(os.path.join(SUITE, "data", "baseline.json")) as _fh:
    BASE = json.load(_fh)

PC_FIELDS = BASE["pc_fields"]
CONSUMER_OUTPUT = BASE["consumer_output"]   # "1.0.20|26|2|0"
WORK = os.environ.get("SRB_WORK", "/tmp/srb-work")


@pytest.fixture(scope="session")
def pc_text(b_default):
    if not b_default.installed:
        pytest.fail("default build did not install:\n%s"
                    % b_default.failure_summary())
    p = b_default.pc_file()
    if not p:
        pytest.fail("lib/pkgconfig/libsodium.pc was not installed (§1.7)")
    return open(p, errors="replace").read()


def pc_field(text, name):
    m = re.search(r"^%s:\s*(.*)$" % re.escape(name), text, re.M)
    return m.group(1).strip() if m else None


@pytest.mark.parametrize("field", ["Name", "Version", "Description"])
def test_pc_field_exact(pc_text, field):
    got = pc_field(pc_text, field)
    want = PC_FIELDS[field]
    assert got == want, (
        "libsodium.pc %s is %r; State A's is %r (§1.7)" % (field, got, want))


def test_pc_libs_semantics(pc_text):
    got = pc_field(pc_text, "Libs")
    assert got, "libsodium.pc has no Libs field"
    assert "-lsodium" in got, "Libs does not link the library: %r" % got
    assert "${libdir}" in got or "${exec_prefix}" in got, (
        "Libs must reach the library through ${libdir}: %r (§1.7 relocatable)" % got)


def test_pc_cflags_semantics(pc_text):
    got = pc_field(pc_text, "Cflags") or pc_field(pc_text, "CFlags")
    assert got, "libsodium.pc has no Cflags field"
    assert "${includedir}" in got, (
        "Cflags must reach the headers through ${includedir}: %r (§1.7)" % got)


def test_pc_declares_variables(pc_text):
    for var in ("prefix", "exec_prefix", "libdir", "includedir"):
        assert re.search(r"^%s=" % var, pc_text, re.M), (
            "libsodium.pc does not define %s=; §1.7 requires the standard "
            "relocatable variables" % var)


def test_pc_is_relocatable(pc_text, b_default):
    """No absolute path may appear outside the prefix= line."""
    prefix = b_default.prefix
    offenders = []
    for line in pc_text.splitlines():
        if line.startswith(("prefix=", "#")):
            continue
        if prefix in line:
            offenders.append(line.strip())
    assert offenders == [], (
        "libsodium.pc bakes absolute paths into its body: %s — §1.7 requires "
        "${prefix}/${libdir}/${includedir}" % offenders[:5])


def test_pc_exec_prefix_derives_from_prefix(pc_text):
    m = re.search(r"^exec_prefix=(.*)$", pc_text, re.M)
    assert m, "no exec_prefix= in libsodium.pc"
    assert "${prefix}" in m.group(1), (
        "exec_prefix is %r; §1.7 requires it to derive from ${prefix}" % m.group(1))


def test_pkg_config_reports_version(b_default):
    rc, out = consumers.pkgconfig_query(b_default.libdir(), "--modversion")
    assert rc == 0, "pkg-config --modversion libsodium failed: %s" % out
    assert out == PC_FIELDS["Version"], (
        "pkg-config reports version %r, State A reports %r" % (out, PC_FIELDS["Version"]))


def _points_at(flag_output, prefix_char, wanted):
    """Whether any `-I`/`-L` in `flag_output` names `wanted` once resolved.

    The comparison is between resolved paths and not between strings, because a
    `.pc` file that derives its directories from `${prefix}` legitimately emits
    `-I${prefix}/lib/pkgconfig/../../include` -- pkg-config substitutes the
    variable and leaves the `..` segments in place. That path is the installed
    include directory; the compiler opens it and finds the headers. Requiring the
    collapsed spelling measures how the `.pc` file was written rather than where it
    points, and this module already has a check that compiles and links a real
    program with these flags, which is the behavioural question.
    """
    target = os.path.normpath(wanted)
    for tok in shlex.split(flag_output):
        if tok.startswith(prefix_char) and len(tok) > 2:
            if os.path.normpath(tok[2:]) == target:
                return True
    return False


def test_pkg_config_cflags_resolve(b_default):
    rc, out = consumers.pkgconfig_query(b_default.libdir(), "--cflags")
    assert rc == 0, "pkg-config --cflags failed: %s" % out
    inc = os.path.join(b_default.prefix, "include")
    assert _points_at(out, "-I", inc), (
        "pkg-config --cflags is %r, in which no -I resolves to the installed "
        "headers (%s)" % (out, inc))


def test_pkg_config_libs_resolve(b_default):
    rc, out = consumers.pkgconfig_query(b_default.libdir(), "--libs")
    assert rc == 0, "pkg-config --libs failed: %s" % out
    assert "-lsodium" in out, "pkg-config --libs is %r" % out
    assert _points_at(out, "-L", b_default.libdir()), (
        "pkg-config --libs is %r, in which no -L resolves to the installed "
        "library directory (%s)" % (out, b_default.libdir()))


@pytest.fixture(scope="session")
def pc_consumer(b_default):
    """Compile and link a real program with pkg-config's flags."""
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    d = os.path.join(WORK, "_consumer_pc")
    rc, out, binary = consumers.pkgconfig_consumer(d, b_default.prefix,
                                                   b_default.libdir())
    return {"rc": rc, "out": out, "binary": binary, "build": b_default}


def test_pkgconfig_consumer_links(pc_consumer):
    assert pc_consumer["rc"] == 0, (
        "a downstream program could not be built with pkg-config's flags "
        "(§1.7):\n%s" % pc_consumer["out"][-1500:])
    assert os.path.isfile(pc_consumer["binary"]), "no consumer binary produced"


def test_pkgconfig_consumer_runs_and_matches_state_a(pc_consumer):
    if pc_consumer["rc"] != 0:
        pytest.fail("consumer did not build:\n%s" % pc_consumer["out"][-800:])
    b = pc_consumer["build"]
    env = {"LD_LIBRARY_PATH": b.libdir()}
    rc, out = elfutil.run_binary(pc_consumer["binary"], env=env)
    assert rc == 0, "the consumer program exited %d:\n%s" % (rc, out[-800:])
    first = out.strip().splitlines()[0] if out.strip() else ""
    assert first.startswith(CONSUMER_OUTPUT), (
        "the consumer printed %r; built against State A it prints %r… — the "
        "library reports a different version (§1.7, §3)" % (first, CONSUMER_OUTPUT))


def test_pkgconfig_static_consumer_links(b_default):
    """--static must yield flags that link the archive."""
    if not b_default.installed:
        pytest.fail(b_default.failure_summary())
    rc, out = consumers.pkgconfig_query(b_default.libdir(), "--static", "--libs")
    assert rc == 0, "pkg-config --static --libs failed: %s" % out
    assert "-lsodium" in out, "static Libs is %r" % out


def test_pc_installed_in_every_library_configuration(b_static_only, b_shared_only):
    for b, label in ((b_static_only, "static-only"), (b_shared_only, "shared-only")):
        if not b.installed:
            pytest.fail("%s did not install:\n%s" % (label, b.failure_summary()))
        assert b.pc_file(), "%s installed no libsodium.pc (§1.7)" % label


def test_no_uninstalled_pc_installed(b_default):
    """"uninstalled .pc" is an Autotools-only concept (§1.7)."""
    bad = [p for _k, p in b_default.install_entries()
           if "uninstalled" in os.path.basename(p)]
    assert bad == [], "an -uninstalled.pc file was installed: %s" % bad


def test_pc_has_no_autotools_placeholders(pc_text):
    left = re.findall(r"@[A-Za-z_][A-Za-z0-9_]*@", pc_text)
    assert left == [], (
        "libsodium.pc still contains Autotools placeholders %s — it was copied "
        "from libsodium.pc.in without substitution (§1.7)" % sorted(set(left)))
