#!/usr/bin/env python3
"""Behavioural: the public header set, including the generated version.h.

Contract §1.3 and §2. All 67 headers must be installed; the 66 that are ordinary
files must be installed verbatim from the source tree, and `sodium/version.h`
must be produced from `version.h.in` with the same substitutions `configure`
performs today.
"""
import hashlib
import json
import os
import re

import pytest

pytestmark = pytest.mark.behaviour

# data/ and lib/ live at the suite root, one level above modules/.  The
# runner exports SRB_SUITE_DIR; the fallback keeps a module runnable by hand.
SUITE = os.environ.get("SRB_SUITE_DIR") or os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
with open(os.path.join(SUITE, "data", "install.json")) as _fh:
    HEADERS = json.load(_fh)["headers"]
with open(os.path.join(SUITE, "data", "baseline.json")) as _fh:
    BASE = json.load(_fh)
with open(os.path.join(SUITE, "data", "sources.json")) as _fh:
    CHECKSUMS = json.load(_fh)["checksums"]

GENERATED = {"sodium/version.h"}
VERBATIM = [h for h in HEADERS if h not in GENERATED]
VERSION_MACROS = BASE["version_macros"]


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


@pytest.fixture(scope="session")
def incdir(b_default):
    if not b_default.installed:
        pytest.fail("default build did not install:\n%s"
                    % b_default.failure_summary())
    return os.path.join(b_default.prefix, "include")


@pytest.mark.parametrize("header", HEADERS)
def test_header_installed(incdir, header):
    p = os.path.join(incdir, header)
    assert os.path.isfile(p), (
        "public header %s was not installed; State A installs all %d of them "
        "(§1.3)" % (header, len(HEADERS)))


@pytest.mark.parametrize("header", VERBATIM)
def test_header_content_verbatim(incdir, header):
    """§3: an installed header must be the source header, byte for byte."""
    p = os.path.join(incdir, header)
    if not os.path.isfile(p):
        pytest.fail("%s not installed" % header)
    src_rel = "src/libsodium/include/" + header
    want = CHECKSUMS.get(src_rel)
    if want is None:
        pytest.skip("%s has no frozen checksum" % src_rel)
    got = sha256_file(p)
    assert got == want, (
        "installed %s differs from the source header (sha256 %s vs %s); headers "
        "must be installed unmodified (§3)" % (header, got[:16], want[:16]))


def test_version_h_installed(incdir):
    p = os.path.join(incdir, "sodium", "version.h")
    assert os.path.isfile(p), (
        "sodium/version.h was not installed; §2 requires it to be generated from "
        "version.h.in and installed")


@pytest.mark.parametrize("macro", sorted(VERSION_MACROS))
def test_version_h_macro(incdir, macro):
    p = os.path.join(incdir, "sodium", "version.h")
    if not os.path.isfile(p):
        pytest.fail("sodium/version.h not installed")
    text = open(p, errors="replace").read()
    m = re.search(r"^#\s*define\s+%s\s+(.+?)\s*$" % re.escape(macro), text, re.M)
    assert m, "sodium/version.h does not define %s" % macro
    got = m.group(1).strip()
    want = VERSION_MACROS[macro]
    assert got == want, (
        "%s is %s in the generated version.h; configure substitutes %s (§2)"
        % (macro, got, want))


def test_version_h_has_no_unsubstituted_placeholders(incdir):
    p = os.path.join(incdir, "sodium", "version.h")
    if not os.path.isfile(p):
        pytest.fail("sodium/version.h not installed")
    text = open(p, errors="replace").read()
    left = re.findall(r"@[A-Za-z_][A-Za-z0-9_]*@", text)
    assert left == [], (
        "sodium/version.h still contains unsubstituted placeholders %s — "
        "configure_file did not substitute them (§2)" % sorted(set(left)))


def test_version_h_matches_state_a(incdir):
    """The whole generated header, modulo trailing whitespace."""
    p = os.path.join(incdir, "sodium", "version.h")
    if not os.path.isfile(p):
        pytest.fail("sodium/version.h not installed")
    got = "\n".join(l.rstrip() for l in
                    open(p, errors="replace").read().strip().splitlines())
    want = "\n".join(l.rstrip() for l in BASE["version_h"].strip().splitlines())
    assert got == want, (
        "the generated sodium/version.h differs from State A's.\n--- expected "
        "---\n%s\n--- got ---\n%s" % (want, got))


def test_no_private_headers_installed(incdir):
    """State A installs no `private/` headers; they are internal."""
    bad = []
    for dirpath, _d, files in os.walk(incdir):
        for f in files:
            rel = os.path.relpath(os.path.join(dirpath, f), incdir)
            if "private/" in rel.replace(os.sep, "/"):
                bad.append(rel)
    assert bad == [], (
        "internal headers were installed: %s — State A installs only the public "
        "set (§1.3)" % bad[:10])


def test_no_version_h_in_template_installed(incdir):
    bad = []
    for dirpath, _d, files in os.walk(incdir):
        for f in files:
            if f.endswith(".in"):
                bad.append(os.path.relpath(os.path.join(dirpath, f), incdir))
    assert bad == [], "template file(s) installed: %s" % bad


def test_installed_header_count(incdir):
    got = []
    for dirpath, _d, files in os.walk(incdir):
        for f in files:
            if f.endswith(".h"):
                got.append(os.path.relpath(os.path.join(dirpath, f), incdir)
                           .replace(os.sep, "/"))
    assert sorted(got) == sorted(HEADERS), (
        "installed header set differs from State A's: missing %s, extra %s"
        % (sorted(set(HEADERS) - set(got))[:8], sorted(set(got) - set(HEADERS))[:8]))


def test_sodium_h_is_the_umbrella_header(incdir):
    p = os.path.join(incdir, "sodium.h")
    if not os.path.isfile(p):
        pytest.fail("sodium.h not installed")
    text = open(p, errors="replace").read()
    assert "version.h" in text, "sodium.h does not include version.h"


@pytest.mark.parametrize("cfg", ["minimal", "static-only", "shared-only"])
def test_headers_installed_in_every_configuration(request, cfg):
    fixture = {"minimal": "b_minimal", "static-only": "b_static_only",
               "shared-only": "b_shared_only"}[cfg]
    b = request.getfixturevalue(fixture)
    if not b.installed:
        pytest.fail("%s did not install:\n%s" % (cfg, b.failure_summary()))
    inc = os.path.join(b.prefix, "include")
    missing = [h for h in HEADERS if not os.path.isfile(os.path.join(inc, h))]
    assert missing == [], (
        "%s is missing %d public headers: %s" % (cfg, len(missing), missing[:8]))
