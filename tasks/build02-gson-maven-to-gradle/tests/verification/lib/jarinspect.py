#!/usr/bin/env python3
"""Read jars, manifests and POMs without a JDK on PATH.

Everything this suite asserts about a produced artifact goes through here, so a
check describes the artifact rather than the tool that happened to inspect it.
Nothing shells out: `unzip`, `javap` and `jar` are all absent from the reasoning
even where they exist in the image, because a parser that can be read is a parser
whose failure modes are visible in the failure message.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import struct
import xml.etree.ElementTree as ET
import zipfile


# ------------------------------------------------------------------- manifest --
def parse_manifest(raw):
    """Parse a jar manifest, undoing the 72-byte continuation folding.

    The folding is not cosmetic here: bnd's Export-Package header for the gson
    jar is several hundred characters, so a reader that treats each physical line
    as one entry sees a truncated value followed by several junk keys.
    """
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\n ", "")
    out = {}
    for line in text.split("\n"):
        if not line.strip() or ":" not in line:
            continue
        k, v = line.split(":", 1)
        out[k.strip()] = v.strip()
    return out


def split_osgi_clauses(value):
    """Split an OSGi header on its top-level commas.

    Quotes and brackets nest, and both appear in real headers:
    `Require-Capability: osgi.ee;filter:="(&(osgi.ee=JavaSE)(version=1.7))"` has a
    comma inside neither, but `Bundle-SCM` and a multi-package Export-Package do,
    and splitting them naively invents clauses that were never there.
    """
    out, buf, depth, quoted = [], [], 0, False
    for ch in value:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif quoted:
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf).strip())
    return [c for c in out if c]


def parse_osgi_header(value):
    """`pkg;a=b,pkg2;c="d"` -> {pkg: {a: b}, pkg2: {c: d}}."""
    out = {}
    for clause in split_osgi_clauses(value):
        parts = clause.split(";")
        attrs = {}
        for p in parts[1:]:
            if "=" not in p:
                continue
            k, v = p.split("=", 1)
            attrs[k.strip().rstrip(":")] = v.strip().strip('"')
        out[parts[0].strip()] = attrs
    return out


def exported_packages(manifest):
    """Package -> attributes from Export-Package, or {} when absent."""
    return parse_osgi_header(manifest.get("Export-Package", ""))


def imported_packages(manifest):
    return parse_osgi_header(manifest.get("Import-Package", ""))


# ----------------------------------------------------------------------- jars --
class Jar:
    """A jar, read once into memory.

    The four jars together are under a megabyte, so reading them whole costs less
    than the bookkeeping of keeping a handle open across a session-scoped fixture
    -- and it means an entry's bytes are available after the file has been
    replaced, which the rebuild comparison needs.
    """

    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        with zipfile.ZipFile(path) as zf:
            self._info = {i.filename: i for i in zf.infolist()}
            self._names = [i.filename for i in zf.infolist() if not i.is_dir()]
            self._dirs = [i.filename for i in zf.infolist() if i.is_dir()]
            self._data = {n: zf.read(n) for n in self._names}
        self._mf = None

    @classmethod
    def from_bytes(cls, data, name="<memory>"):
        obj = cls.__new__(cls)
        obj.path = name
        obj.name = name
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            obj._info = {i.filename: i for i in zf.infolist()}
            obj._names = [i.filename for i in zf.infolist() if not i.is_dir()]
            obj._dirs = [i.filename for i in zf.infolist() if i.is_dir()]
            obj._data = {n: zf.read(n) for n in obj._names}
        obj._mf = None
        return obj

    def close(self):
        self._data = {}

    # -- entries
    @property
    def entries(self):
        return sorted(self._names)

    @property
    def dir_entries(self):
        return sorted(self._dirs)

    def has(self, name):
        return name in self._data

    def read(self, name):
        return self._data[name]

    def text(self, name, encoding="utf-8"):
        return self._data[name].decode(encoding, "replace")

    def sha256(self, name):
        return hashlib.sha256(self._data[name]).hexdigest()

    def size(self, name):
        return len(self._data[name])

    def compress_type(self, name):
        return self._info[name].compress_type

    def classes(self):
        return sorted(n for n in self._names if n.endswith(".class"))

    def matching(self, pattern):
        rx = re.compile(pattern)
        return sorted(n for n in self._names if rx.search(n))

    def digests(self):
        return {n: self.sha256(n) for n in self._names}

    # -- manifest
    @property
    def manifest(self):
        if self._mf is None:
            self._mf = parse_manifest(self._data.get("META-INF/MANIFEST.MF", b""))
        return self._mf

    def manifest_raw(self):
        return self._data.get("META-INF/MANIFEST.MF", b"")

    # -- bytecode
    def major(self, name):
        data = self._data[name]
        if data[:4] != b"\xca\xfe\xba\xbe":
            raise ValueError("%s is not a class file" % name)
        return struct.unpack(">H", data[6:8])[0]

    def majors(self):
        return {n: self.major(n) for n in self.classes()}

    def major_histogram(self):
        hist = {}
        for major in self.majors().values():
            hist[str(major)] = hist.get(str(major), 0) + 1
        return hist

    def nested(self, name):
        return Jar.from_bytes(self._data[name], name)


# ------------------------------------------------------------------------ POM --
POM_NS = "{http://maven.apache.org/POM/4.0.0}"


class Pom:
    """A published pom, read for what a consumer would read it for.

    A POM is not necessarily self-contained, and the ones a consumer resolves
    frequently are not.  Maven's own release of gson 2.10.1 publishes a file that
    declares an ``<artifactId>``, a ``<name>``, a licence and a ``<parent>``, and
    leaves its group, version, description, url, scm and developer list to be
    inherited from ``gson-parent`` -- which is published beside it, because a
    resolver has to fetch it before it can compute anything.  Gradle's
    ``maven-publish`` writes the flattened form instead: every field in one file
    and no parent.  Both resolve to the same model.

    So the reading methods come in two flavours.  ``text``/``findall`` read this
    file, which is what a check about *this document's* shape wants.
    ``inherited``/``inherited_findall`` walk the parent chain the way a resolver
    does, which is what a check about *what a consumer sees* wants -- and they
    need somewhere to find the parents, which is `repo_root`: the root of the
    repository layout this POM was read out of.  Without one (a POM read from
    bytes, or from outside a layout) the chain is this file alone, and the
    inherited readers degrade to the plain ones.
    """

    def __init__(self, path_or_bytes, repo_root=None):
        if isinstance(path_or_bytes, bytes):
            self.path = None
            self.root = ET.fromstring(path_or_bytes)
        else:
            self.path = path_or_bytes
            self.root = ET.parse(path_or_bytes).getroot()
        self.repo_root = repo_root
        self._chain_cache = None

    def text(self, path, default=None):
        """`scm/url` -> the text of <scm><url>, namespace-agnostic."""
        node = self.root
        for part in path.split("/"):
            found = node.find(POM_NS + part)
            if found is None:
                found = node.find(part)
            if found is None:
                return default
            node = found
        return node.text.strip() if node.text else default

    def findall(self, tag):
        out = list(self.root.iter(POM_NS + tag))
        return out or list(self.root.iter(tag))

    def child_text(self, node, tag, default=None):
        if node is None:
            return default
        found = node.find(POM_NS + tag)
        if found is None:
            found = node.find(tag)
        if found is None or found.text is None:
            return default
        return found.text.strip()

    # ------------------------------------------------------- the parent chain --
    def parent_coordinates(self):
        """``(group, artifact, version)`` of the declared parent, or None."""
        if not self.findall("parent"):
            return None
        g = self.text("parent/groupId")
        a = self.text("parent/artifactId")
        v = self.text("parent/version")
        return (g, a, v) if (g and a and v) else None

    def parent_path(self):
        """Where this POM's parent would be in `repo_root`, or None.

        The path a *resolver* would use, which is the repository layout and not
        ``<relativePath>``: that element is a build-time convenience for a reactor
        and means nothing to a consumer who downloaded one file.
        """
        coords = self.parent_coordinates()
        if coords is None or not self.repo_root:
            return None
        g, a, v = coords
        return os.path.join(self.repo_root, *(g.split(".") + [a, v,
                                                             "%s-%s.pom" % (a, v)]))

    def parent(self):
        """The parent POM as a `Pom`, if it is resolvable in `repo_root`."""
        p = self.parent_path()
        if p is None or not os.path.isfile(p):
            return None
        try:
            return Pom(p, repo_root=self.repo_root)
        except ET.ParseError:
            return None

    def chain(self):
        """This POM, then its parent, then its parent's parent.

        Stops at the first ancestor that is not resolvable, so the chain is always
        the prefix a consumer could actually have fetched.  Guards against a POM
        that declares itself as its own parent, which is unresolvable rather than
        infinite.
        """
        if self._chain_cache is not None:
            return self._chain_cache
        out, seen, node = [], set(), self
        while node is not None:
            key = node.coordinates()
            if key in seen:
                break
            seen.add(key)
            out.append(node)
            node = node.parent()
        self._chain_cache = out
        return out

    def inherited(self, path, default=None):
        """`text`, resolved through the parent chain the way a resolver does.

        The first POM in the chain that declares the field wins, which is Maven's
        rule for every scalar a consumer reads.  Not applied: the path Maven
        appends to an inherited ``url`` or ``scm`` (an inherited
        ``https://github.com/google/gson`` becomes ``.../gson`` in the effective
        model of the ``gson`` module).  That derivation is Maven-specific
        bookkeeping about where a module sits in a reactor -- Maven 4 lets a
        project switch it off -- and it carries no information the declared value
        does not.  What is compared here is what the model declares.
        """
        for pom in self.chain():
            got = pom.text(path)
            if got is not None:
                return got
        return default

    def inherited_findall(self, tag):
        """`findall`, resolved through the parent chain.

        The nearest POM that declares any is the one that counts.  Maven does not
        merge a child's ``<licenses>`` with its parent's -- a child that declares
        one licence declares *the* licence list -- so neither does this.
        """
        for pom in self.chain():
            got = pom.findall(tag)
            if got:
                return got
        return []

    def _own_dependency_nodes(self):
        """The ``<dependency>`` children of this file's project ``<dependencies>``."""
        node = self.root.find(POM_NS + "dependencies")
        if node is None:
            node = self.root.find("dependencies")
        if node is None:
            return []
        out = list(node.findall(POM_NS + "dependency"))
        return out or list(node.findall("dependency"))

    def _declared_dependency_nodes(self):
        """The same, across the parent chain, nearest first."""
        out = []
        for pom in self.chain():
            out.extend(pom._own_dependency_nodes())
        return out

    def dependencies(self, scopes=None):
        """Declared dependencies, optionally filtered by scope.

        `scope` defaults to `compile` when absent, which is Maven's rule and the
        one a consumer's resolver applies.  This matters for the one claim the
        publication module makes about the graph: State A's gson artifact has no
        runtime dependencies at all, and a Gradle port that publishes junit as
        `compile` instead of test-only has changed what every consumer downloads.

        What counts as declared is the project's own ``<dependencies>``, plus the
        same element in each POM up the parent chain, because a consumer inherits
        those too.  Deliberately excluded: ``<dependencyManagement>``, a plugin's
        ``<dependencies>`` and anything inside a ``<profile>``.  None of the three
        puts an artifact on a consumer's classpath -- management supplies a version
        to a dependency declared elsewhere, a plugin's are the producer's -- and
        counting them would report gson's POM as carrying a dependency because its
        parent manages junit's version.  A submission that hid a real dependency in
        a published parent's ``<dependencies>`` is still caught, which is why the
        chain is walked rather than just this file read.
        """
        out = []
        for dep in self._declared_dependency_nodes():
            scope = self.child_text(dep, "scope", "compile")
            if scopes and scope not in scopes:
                continue
            out.append({
                "groupId": self.child_text(dep, "groupId"),
                "artifactId": self.child_text(dep, "artifactId"),
                "version": self.child_text(dep, "version"),
                "scope": scope,
                "optional": self.child_text(dep, "optional", "false"),
            })
        return out

    def coordinates(self):
        return (self.text("groupId") or self.text("parent/groupId"),
                self.text("artifactId"),
                self.text("version") or self.text("parent/version"))


# ------------------------------------------------------------------ JUnit XML --
def parse_junit_dirs(dirs):
    """{`classname#name`: outcome} across every TEST-*.xml in these directories.

    A rerun can report the same case twice; a real result beats a skip and a
    failure beats both, so the outcome recorded is the worst one seen.  Gradle
    also writes a `binary/` subdirectory beside the XML, which is ignored: it is
    the same information in a form that needs Gradle to read it.
    """
    cases = {}
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not (fn.startswith("TEST-") and fn.endswith(".xml")):
                continue
            try:
                root = ET.parse(os.path.join(d, fn)).getroot()
            except ET.ParseError:
                continue
            for tc in root.iter("testcase"):
                key = "%s#%s" % (tc.get("classname"), tc.get("name"))
                if tc.find("failure") is not None or tc.find("error") is not None:
                    cases[key] = "failed"
                elif tc.find("skipped") is not None:
                    cases.setdefault(key, "skipped")
                elif cases.get(key) != "failed":
                    cases[key] = "passed"
    return cases
