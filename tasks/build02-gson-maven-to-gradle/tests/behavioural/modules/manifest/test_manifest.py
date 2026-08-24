#!/usr/bin/env python3
"""The OSGi bundle manifest.

State A generates it with bnd-maven-plugin 6.4.0 from ``gson/bnd.bnd``, and it is
the single most easily lost artifact in this migration: a Gradle build that simply
jars the classes produces a manifest with none of these headers, and nothing about
the jar looks wrong until an OSGi container refuses to resolve it.  Two of gson's
own tests read this manifest at runtime, so losing it is also a test failure --
but only if the test can find the manifest, which is a separate piece of build
wiring, which is why it is measured here as well as there.

Headers fall into three groups, decided by measurement, not by taste:

  required    13 headers that describe the bundle itself.  Byte-exact.
  provenance   4 headers bnd-maven-plugin synthesises from the POM's
               ``<developers>``, ``<url>``, ``<licenses>`` and ``<scm>``.  A Gradle
               build has no POM to read them from, so they are graded separately.
  exempt       Bnd-LastModified, Build-Jdk, Build-Jdk-Spec, Built-By, Created-By,
               Tool.  These record when and with what the build ran.  Requiring
               them would be requiring a timestamp to match.
"""
import json
import os

import pytest

import jarinspect

DATA = os.path.join(os.environ.get("SRB_SUITE_DIR", "/tests/behavioural"), "data")
with open(os.path.join(DATA, "manifest.json")) as _fh:
    MF = json.load(_fh)

REQUIRED = sorted(MF["required"].items())
PROVENANCE = sorted(MF["provenance"].items())
EXEMPT = MF["exempt_header_names"]


def _prov_norm(value):
    """Normalise a provenance header for comparison.

    State A's values come from bnd reading gson's *effective* POM, and Maven
    appends the module directory when a child inherits ``<url>`` and ``<scm>`` from
    a parent: the parent declares ``https://github.com/google/gson``, so gson's
    effective POM says ``.../gson/gson``.  That suffix is an artifact of Maven's
    inheritance rules, not project metadata anyone would choose, and State B has no
    parent POM to inherit from.  Both spellings are accepted; what is graded is
    that the values are the project's own.
    """
    if not value:
        return value
    out = value
    for suffix, repl in (("/gson.git/gson", "/gson.git"),
                         ("gson.git/gson", "gson.git"),
                         ("/gson/gson/", "/gson/"),
                         ("/gson/gson", "/gson")):
        out = out.replace(suffix, repl)
    return out.rstrip("/")


def _clauses(value):
    return jarinspect.parse_osgi_header(value)


def _manifest(jar):
    mf = jar.manifest
    assert mf, (
        "the gson jar has no manifest headers at all.\n"
        "State A's jar is an OSGi bundle: the migration must keep generating "
        "the manifest from gson/bnd.bnd (or equivalent instructions).")
    return mf


# ------------------------------------------------------------ the bundle exists
@pytest.mark.audit
def test_manifest_present(gson_jar):
    assert gson_jar.has("META-INF/MANIFEST.MF")


@pytest.mark.audit
def test_is_an_osgi_bundle(gson_jar):
    """Bundle-SymbolicName is what makes a jar a bundle.

    Audit rather than behavioural: a submission with no OSGi manifest has not
    reproduced State A's published artifact, whatever else it got right.
    """
    mf = _manifest(gson_jar)
    assert mf.get("Bundle-SymbolicName") == "com.google.gson", (
        "Bundle-SymbolicName is %r, want 'com.google.gson'. Without it the jar "
        "is not an OSGi bundle." % mf.get("Bundle-SymbolicName"))


@pytest.mark.audit
def test_manifest_was_generated_not_handwritten(gson_jar, data):
    """The manifest carries bnd's own analysis, which cannot be faked usefully.

    Import-Package lists what the *bytecode* references.  A hand-written manifest
    that happens to match today silently stops matching the moment the code
    changes, so the check is that the header exists and agrees with State A's
    computed value -- the same computation, not the same literal.
    """
    mf = _manifest(gson_jar)
    imp = mf.get("Import-Package")
    assert imp, "no Import-Package header: bnd never analysed the bundle"
    want = data["manifest"]["required"]["Import-Package"]
    assert imp == want, (
        "Import-Package is\n  %r\nState A computes\n  %r" % (imp, want))


# --------------------------------------------------------------- exact headers
@pytest.mark.behavioural
@pytest.mark.parametrize("header,want", REQUIRED, ids=[h for h, _ in REQUIRED])
def test_required_header_exact(gson_jar, header, want):
    """This header matches State A byte for byte."""
    mf = _manifest(gson_jar)
    got = mf.get(header)
    assert got is not None, "manifest has no %s header" % header
    assert got == want, (
        "%s differs.\n  State A : %s\n  produced: %s" % (header, want, got))


@pytest.mark.behavioural
@pytest.mark.parametrize("header,want", PROVENANCE,
                         ids=[h for h, _ in PROVENANCE])
def test_provenance_header(gson_jar, header, want):
    """bnd-maven-plugin derives this from the POM; State B should too.

    Reproducible, but only by supplying the same project metadata to bnd
    explicitly -- there is no POM for it to read.
    """
    mf = _manifest(gson_jar)
    got = mf.get(header)
    assert got is not None, (
        "manifest has no %s header. bnd-maven-plugin derives it from the POM's "
        "metadata; a Gradle build must supply the same values to bnd." % header)
    assert _prov_norm(got) == _prov_norm(want), (
        "%s:\n  State A : %s\n  produced: %s" % (header, want, got))


# ------------------------------------------------------ structured header detail
EXPORT_CASES = sorted(_clauses(MF["required"]["Export-Package"]).items())
IMPORT_CASES = sorted(_clauses(MF["required"]["Import-Package"]).items())

# Every (package, attribute) pair of the four export clauses, one check each: a
# missing `uses:=` on one package is a different defect from a wrong version, and
# both are different from the package not being exported at all.
EXPORT_ATTR_CASES = [(pkg, attr, want)
                     for pkg, attrs in EXPORT_CASES
                     for attr, want in sorted(attrs.items())]


@pytest.mark.behavioural
@pytest.mark.parametrize("pkg,attrs", EXPORT_CASES, ids=[p for p, _ in EXPORT_CASES])
def test_exported_package(gson_jar, pkg, attrs):
    """This package is exported at all."""
    mf = _manifest(gson_jar)
    got = _clauses(mf.get("Export-Package", ""))
    assert pkg in got, (
        "%s is not exported. State A exports exactly four packages; "
        "-exportcontents in bnd.bnd decides which." % pkg)


@pytest.mark.behavioural
@pytest.mark.parametrize("pkg,attr,want", EXPORT_ATTR_CASES,
                         ids=["%s/%s" % (p, a) for p, a, _ in EXPORT_ATTR_CASES])
def test_exported_package_attribute(gson_jar, pkg, attr, want):
    """This export clause carries State A's attribute value.

    ``version`` is the bundle version bnd stamps on every export; ``uses:=`` is
    computed from the package's own references, and getting it wrong makes an
    OSGi resolver accept wirings it should refuse.
    """
    mf = _manifest(gson_jar)
    got = _clauses(mf.get("Export-Package", ""))
    assert pkg in got, "%s is not exported" % pkg
    assert got[pkg].get(attr) == want, (
        "Export-Package %s: attribute %s is %r, want %r"
        % (pkg, attr, got[pkg].get(attr), want))


@pytest.mark.behavioural
@pytest.mark.parametrize("pkg,attrs", IMPORT_CASES, ids=[p for p, _ in IMPORT_CASES])
def test_imported_package(gson_jar, pkg, attrs):
    """This package is imported, with State A's resolution directive."""
    mf = _manifest(gson_jar)
    got = _clauses(mf.get("Import-Package", ""))
    assert pkg in got, (
        "%s is not imported. bnd computes Import-Package from the bytecode; a "
        "missing entry means the analysis did not run over these classes." % pkg)
    for attr, want in sorted(attrs.items()):
        assert got[pkg].get(attr) == want, (
            "Import-Package %s: %s is %r, want %r"
            % (pkg, attr, got[pkg].get(attr), want))


@pytest.mark.behavioural
def test_exports_exactly_four_packages(gson_jar):
    mf = _manifest(gson_jar)
    got = sorted(_clauses(mf.get("Export-Package", "")))
    want = sorted(_clauses(MF["required"]["Export-Package"]))
    assert got == want, "exported packages:\n  produced: %s\n  State A : %s" % (
        got, want)


@pytest.mark.audit
def test_internal_package_not_exported(gson_jar):
    """com.google.gson.internal must stay internal.

    ``-exportcontents`` in bnd.bnd names four packages explicitly.  A build that
    computes its exports from "every package in the jar" produces a manifest that
    looks richer and makes five internal packages into public API.
    """
    mf = _manifest(gson_jar)
    got = _clauses(mf.get("Export-Package", ""))
    leaked = [p for p in got if ".internal" in p]
    assert not leaked, (
        "internal packages are exported: %s\n"
        "State A exports only the four public packages; exporting internals "
        "makes them API." % leaked)


@pytest.mark.behavioural
def test_private_package_header_removed(gson_jar):
    """bnd.bnd sets ``-removeheaders: Private-Package``; that must hold."""
    mf = _manifest(gson_jar)
    assert "Private-Package" not in mf, \
        "Private-Package survives; State A removes it"


@pytest.mark.behavioural
def test_sun_misc_import_is_optional(gson_jar):
    """``sun.misc;resolution:=optional`` -- gson works on JVMs without it.

    One of gson's own tests asserts this exact clause, so it is checked here too:
    a failure in both places points at the manifest, not at the test.
    """
    mf = _manifest(gson_jar)
    got = _clauses(mf.get("Import-Package", ""))
    assert "sun.misc" in got, "sun.misc is not imported at all"
    assert got["sun.misc"].get("resolution") == "optional", (
        "sun.misc must be imported with resolution:=optional, got %r"
        % got["sun.misc"])


@pytest.mark.behavioural
def test_multi_release_header(gson_jar):
    """``Multi-Release: true`` -- without it the JVM ignores versions/9/."""
    mf = _manifest(gson_jar)
    assert mf.get("Multi-Release") == "true", (
        "Multi-Release is %r. The jar carries META-INF/versions/9/"
        "module-info.class, which the JVM only honours when this header is "
        "true." % mf.get("Multi-Release"))


@pytest.mark.behavioural
def test_execution_environment(gson_jar):
    mf = _manifest(gson_jar)
    assert mf.get("Bundle-RequiredExecutionEnvironment") == \
        MF["required"]["Bundle-RequiredExecutionEnvironment"]


@pytest.mark.behavioural
def test_require_capability_ee(gson_jar):
    mf = _manifest(gson_jar)
    assert mf.get("Require-Capability") == MF["required"]["Require-Capability"]


# ------------------------------------------------------------------- no residue
@pytest.mark.behavioural
@pytest.mark.parametrize("header", EXEMPT)
def test_exempt_header_is_not_required_but_may_appear(gson_jar, header):
    """These record how the build ran; any value, or none, is fine.

    Kept as explicit checks so the exemption is visible in the report rather
    than implied by silence.
    """
    mf = gson_jar.manifest
    if header in mf:
        assert mf[header].strip() != "", "%s is present but empty" % header


@pytest.mark.behavioural
def test_no_unexpected_headers(gson_jar, data):
    """A migration should not invent bundle headers State A does not have."""
    mf = _manifest(gson_jar)
    known = (set(data["manifest"]["required"])
             | set(data["manifest"]["provenance"])
             | set(data["manifest"]["exempt_header_names"])
             | {"Automatic-Module-Name", "Implementation-Title",
                "Implementation-Version", "Specification-Title",
                "Specification-Version", "Bundle-Copyright"})
    extra = sorted(set(mf) - known)
    assert not extra, (
        "manifest has headers State A does not: %s" % extra)


@pytest.mark.behavioural
def test_automatic_module_name_absent_or_correct(gson_jar):
    """gson ships a real module descriptor, so this header is redundant.

    Permitted, but if present it must name the same module -- a mismatch between
    Automatic-Module-Name and module-info is a genuine defect.
    """
    mf = _manifest(gson_jar)
    if "Automatic-Module-Name" not in mf:
        return
    assert mf["Automatic-Module-Name"] == "com.google.gson", (
        "Automatic-Module-Name is %r but the module descriptor declares "
        "com.google.gson" % mf["Automatic-Module-Name"])


@pytest.mark.behavioural
def test_manifest_version_and_bundle_manifest_version(gson_jar):
    mf = _manifest(gson_jar)
    assert mf.get("Manifest-Version") == "1.0"
    assert mf.get("Bundle-ManifestVersion") == "2"


@pytest.mark.behavioural
def test_manifest_is_first_entry(gson_jar):
    """META-INF/MANIFEST.MF must come first or some readers will not see it."""
    entries = gson_jar.entries
    assert entries, "empty jar"
    idx = entries.index("META-INF/MANIFEST.MF")
    assert idx <= 1, "MANIFEST.MF is at position %d in the archive" % idx


@pytest.mark.behavioural
def test_manifest_lines_are_wrapped_at_72_bytes(gson_jar):
    """The jar manifest format caps a line at 72 bytes; State A's needs it.

    Export-Package alone is 232 characters, so State A's manifest folds it across
    four physical lines.  Both writers a real build could use -- bnd and
    ``java.util.jar.Manifest`` -- fold at 72; a file assembled by string
    concatenation does not, and the OSGi tooling that reads it is entitled to
    reject the result.
    """
    raw = gson_jar.manifest_raw()
    assert raw, "no manifest bytes"
    over = [ln for ln in raw.split(b"\r\n") if len(ln) > 72]
    assert not over, (
        "%d manifest line(s) exceed the 72-byte limit that the jar file "
        "specification sets: %r" % (len(over), [ln[:60] for ln in over[:3]]))


@pytest.mark.behavioural
def test_manifest_uses_crlf_line_endings(gson_jar):
    """The manifest format specifies CRLF, and the folding depends on it.

    A manifest written with bare newlines still parses in most readers, but the
    continuation lines land in a different place, which is how a 232-character
    Export-Package turns into four headers named ``e.gson.stream"`` and friends.
    """
    raw = gson_jar.manifest_raw()
    assert raw, "no manifest bytes"
    bare = raw.replace(b"\r\n", b"").count(b"\n")
    assert bare == 0, (
        "the manifest has %d line ending(s) that are not CRLF" % bare)


# --------------------------------------------------------- the other three jars
NON_BUNDLE = ["gson-extras", "gson-metrics", "gson-proto"]


@pytest.mark.behavioural
@pytest.mark.parametrize("key", NON_BUNDLE)
def test_non_bundle_modules_have_no_osgi_headers(jars, key):
    """Only gson is a bundle in State A; the others must not become ones."""
    jar = jars.require(key)
    mf = jar.manifest
    assert "Bundle-SymbolicName" not in mf, (
        "%s became an OSGi bundle; State A applies bnd only to gson"
        % os.path.basename(jar.path))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", NON_BUNDLE)
def test_non_bundle_modules_have_a_manifest(jars, key):
    """Every jar has a manifest, even a minimal one."""
    jar = jars.require(key)
    assert jar.has("META-INF/MANIFEST.MF"), (
        "%s has no manifest at all" % os.path.basename(jar.path))
    assert jar.manifest.get("Manifest-Version") == "1.0", (
        "%s manifest has no Manifest-Version: 1.0"
        % os.path.basename(jar.path))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", NON_BUNDLE)
def test_non_bundle_modules_are_not_multi_release(jars, key):
    """Only gson carries a versions/ tree, so only gson claims the header."""
    jar = jars.require(key)
    assert jar.manifest.get("Multi-Release") != "true", (
        "%s claims Multi-Release; it has no META-INF/versions/ content"
        % os.path.basename(jar.path))


@pytest.mark.behavioural
@pytest.mark.parametrize("key", NON_BUNDLE)
def test_non_bundle_modules_declare_no_main_class(jars, key):
    """None of the four artifacts is an executable jar in State A.

    ``metrics`` is the one a build might plausibly get wrong: it has benchmark
    classes with ``main`` methods, and an application plugin applied to it would
    produce a Main-Class header and a start script State A does not publish.
    """
    jar = jars.require(key)
    assert "Main-Class" not in jar.manifest, (
        "%s declares Main-Class: %s"
        % (os.path.basename(jar.path), jar.manifest.get("Main-Class")))


@pytest.mark.behavioural
def test_gson_jar_declares_no_class_path(gson_jar):
    """A Class-Path header would make the thin jar depend on a local layout."""
    assert "Class-Path" not in gson_jar.manifest, (
        "the gson jar declares Class-Path: %s"
        % gson_jar.manifest.get("Class-Path"))
