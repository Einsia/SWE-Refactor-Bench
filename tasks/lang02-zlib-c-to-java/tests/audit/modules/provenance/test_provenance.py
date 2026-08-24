"""Where do the compressed bytes come from?

Stage 2 can prove the bytes are right.  It cannot prove they are the
submission's, because the JDK ships zlib: `java.util.zip.Deflater` is a binding
to the same algorithm at the same default settings, so a class that forwards
every call to it matches the C reference byte for byte at every level, every
strategy and every window size.  Every behavioural case in the suite passes.  It
is three lines of work and it is the whole task not done.

That is why this module exists and why it is a *reading* rather than a
measurement.  Stage 2 does measure it -- constant-pool type references, string
constants that could reach `Class.forName`, a differential run under a class
loader that refuses `java.util.zip` -- and all three are defeated by a name
assembled from two halves at run time, which a reader sees immediately.  The two
instruments do not subsume each other, so both run, and stage 1's verdict costs a
submission nothing extra: stage-1 gates are pass or fail, not deductions.

The grep shapes below are deliberately generous, and every one of them has an
innocent reading that the finding states.  A comment explaining why the port does
*not* use `java.util.zip` contains the string; so does a test that asserts the
absence; so does a doc-comment quoting the forbidden list from instruction.md.
None of those is a defect and all three land here.  A scan that only fired on
guilt would be a scan that missed the guilty.
"""

from __future__ import annotations

import re

import pytest

import srbscan
from srbscan import REPO, rel

pytestmark = pytest.mark.scan

MISSING = "<state-a-unreadable>"


def _policy(key: str, default=None):
    return srbscan.CONTRACT.get("jvm_code_policy", {}).get(key, default)


#: Type-reference prefixes the jar may not carry, from the class-file contract.
#: The same list stage 2 checks the constant pool against, used here as a text
#: search over source -- which is the weaker instrument for a name written out in
#: full and the stronger one for a name split across a concatenation.
FORBIDDEN_PREFIXES = srbscan.CONTRACT.get("classfile_contract", {}).get(
    "forbidden_prefixes") or [MISSING]

#: Ways of naming the JDK compression API that no port has an innocent use for.
#: A package name is not a class name: nothing in `org.zlib` needs to write
#: `java.util.zip` except to reach it, or to say in a comment that it does not --
#: and the second is rare enough to be worth one line of a reviewer's time.
ZIP_API_PACKAGE_PATTERNS = (
    r"\bjava\.util\.zip\b",
    r"\bjava\.util\.jar\b",
)

#: Bare class names from that API.  Unlike the package patterns these are
#: *ambiguous by construction*, because instruction.md's own surface table
#: requires classes called `Deflater` and `Inflater`, and `adler32.c`/`crc32.c`
#: are two of the eighteen translation units -- so a correct port must contain
#: these words.  Resolved against the submission's own declarations before being
#: reported; see `test_no_jdk_compression_api`.
ZIP_API_CLASS_PATTERNS = (
    r"\bDeflater\b",
    r"\bInflater\b",
    r"\bDeflaterOutputStream\b",
    r"\bInflaterInputStream\b",
    r"\bGZIPOutputStream\b",
    r"\bGZIPInputStream\b",
    r"\bCRC32\b",
    r"\bAdler32\b",
    r"\bDeflaterInputStream\b",
    r"\bInflaterOutputStream\b",
    r"\bZipEntry\b",
    r"\bZipFile\b",
    r"\bZipOutputStream\b",
    r"\bZipInputStream\b",
    r"\bChecksum\b",
)

#: Kept as the union, for the callers that want the old meaning: a text search
#: over non-Java files, where there is no declaration to resolve against.
ZIP_API_PATTERNS = ZIP_API_PACKAGE_PATTERNS + ZIP_API_CLASS_PATTERNS

#: A type declared by the submission itself.  `sealed`/`non-sealed` and the
#: modifier salad before `class` are why this does not anchor to line start.
_DECLARATION = re.compile(
    r"\b(?:class|interface|enum|record|@interface)\s+([A-Z]\w*)")

#: Ways out of the JVM: a native method, a library load, a process, a downcall.
ESCAPE_PATTERNS = (
    (r"\bnative\s+\w[\w<>\[\]\s,.]*\s+\w+\s*\(", "a native method declaration"),
    (r"\bSystem\s*\.\s*load(Library)?\s*\(", "System.load / System.loadLibrary"),
    (r"\bRuntime\s*\.\s*getRuntime\s*\(\s*\)\s*\.\s*exec\b", "Runtime.exec"),
    (r"\bnew\s+ProcessBuilder\b", "ProcessBuilder"),
    (r"\bjava\.lang\.foreign\b", "the FFM API"),
    (r"\bLinker\s*\.\s*nativeLinker\s*\(", "Linker.nativeLinker"),
    (r"\bSymbolLookup\b", "SymbolLookup"),
    (r"\bsun\.misc\.Unsafe\b", "sun.misc.Unsafe"),
    (r"\bjdk\.internal\b", "a jdk.internal package"),
    (r"--add-exports\b", "--add-exports, which opens a JDK internal"),
    (r"--enable-native-access\b", "--enable-native-access"),
)

#: Names of compression implementations that are not this repository.  A port of
#: jzlib is a port; it is not a port of *this* repository, and telling the two
#: apart is a reading of the code rather than a property of the artifact.
FOREIGN_IMPLEMENTATIONS = (
    "jzlib", "com.jcraft", "miniz", "zlib-ng", "zlibng", "libdeflate",
    "zopfli", "puff", "tinflate", "fflate", "pako", "inflate64",
    "commons-compress", "org.apache.commons.compress", "xerial", "snappy",
    "lz4", "zstd", "brotli",
)

#: Things a build does that this task forbids: fetching, or compiling C.
BUILD_PATTERNS = (
    (r"\bFetchContent\b", "CMake FetchContent, which downloads at configure time"),
    (r"\bExternalProject_Add\b", "ExternalProject_Add, which downloads and builds"),
    (r"\bfile\s*\(\s*DOWNLOAD\b", "file(DOWNLOAD)"),
    (r"\bcurl\b", "curl"),
    (r"\bwget\b", "wget"),
    (r"\bgit\s+clone\b", "git clone"),
    (r"\bmaven-?central\b", "a Maven Central reference"),
    (r"\brepo1\.maven\.org\b", "repo1.maven.org"),
    (r"\bhttps?://[^\s\"'<>)]+\.(jar|tar\.gz|tgz|zip)\b", "a URL to an archive"),
    (r"\benable_language\s*\(\s*(C|CXX|ASM)", "enable_language(C), which needs a C compiler"),
    (r"\badd_library\s*\([^)]*\.c\b", "add_library over a .c file"),
    (r"\badd_executable\s*\([^)]*\.c\b", "add_executable over a .c file"),
    (r"\bproject\s*\([^)]*\bLANGUAGES\s+[^)]*\b(C|CXX|ASM)\b", "project(LANGUAGES C)"),
    (r"\btry_compile\b", "try_compile, which compiles C"),
    (r"\bcheck_(include_file|function_exists|type_size|symbol_exists)\b",
     "a CMake C feature probe"),
)


def _text_hits(patterns, paths, *, flags=0):
    """Every (path, line, quote, why) a pattern matched.

    One pass over each file, all patterns, so a 4 MB ChangeLog is read once
    rather than fifteen times.
    """
    out = []
    for path in paths:
        text = srbscan.read_text(path)
        if not text:
            continue
        for pattern, why in patterns:
            match = srbscan.first_match(text, pattern, flags)
            if match is None:
                continue
            line = srbscan.line_of(text, match.start())
            quote = text.splitlines()[line - 1].strip()[:200]
            out.append((rel(REPO, path), line, quote, why))
    return out


def _render(hits, limit: int = 12) -> str:
    body = "\n".join(f"  {p}:{n}  ({why})\n      {quote}"
                     for p, n, quote, why in hits[:limit])
    if len(hits) > limit:
        body += f"\n  ... and {len(hits) - limit} more"
    return body


# --------------------------------------------------------------------------- #
# no-jdk-deflate: the central question
# --------------------------------------------------------------------------- #

def _declared_types(paths) -> dict[str, str]:
    """Every type the submission declares, name -> the file declaring it.

    Read from the same `.java` files the hits come from, so the resolution and
    the finding cannot disagree about what the tree contains.
    """
    out: dict[str, str] = {}
    for path in paths:
        text = srbscan.read_text(path)
        if not text:
            continue
        for match in _DECLARATION.finditer(text):
            out.setdefault(match.group(1), rel(REPO, path))
    return out


def test_no_jdk_compression_api(java_files):
    """Does control reach `java.util.zip`, rather than: does the string appear.

    Two groups of pattern, because they carry different amounts of information.

    `java.util.zip` and `java.util.jar` are *package* names. Nothing in a port
    writes one except to reach the API or to say in a comment that it does not,
    so a match is reported as it stands and the reviewer reads the one line.

    The bare class names are ambiguous *by construction*, and this is the part
    that was wrong. instruction.md's own surface table requires classes called
    `Deflater` and `Inflater`; `adler32.c` and `crc32.c` are two of the eighteen
    translation units. So every correct submission contains these words --
    including the reference -- and reporting them unresolved made this check fire
    on every correct port, with a headline that read "12 mention(s) of the JDK
    compression API" when the tree mentioned it nowhere. Twelve false leads, on
    the one prohibition whose breach is fatal, in front of a reviewer who is told
    that each flagged line is a place to look.

    So a bare name is resolved against the submission's own declarations first,
    which is what a reader does and what `javac` does:

      - declared in this tree -> the name is this port's own. Not reported.
      - not declared anywhere in this tree -> an unqualified `Deflater` that the
        submission never declares can only have come from an import, and the
        only `Deflater` there is to import is the JDK's. **That** is the finding,
        and it is now unambiguous rather than one entry in a list of twelve.

    The partition does not weaken the check, and the wrapper case is why. A class
    `org.zlib.Deflater` that forwards to `java.util.zip.Deflater` resolves here as
    the port's own -- but its file has to name `java.util.zip` to forward to it,
    which is a package match and reported regardless of any declaration. There is
    no way to reach the API without writing its package name or importing a name
    this tree does not declare, and both are still caught.

    The module docstring's rule still holds: the shapes are generous and a scan
    that only fired on guilt would miss the guilty. Resolution is not narrowing
    the shape -- every pattern still runs over every Java file. It is declining to
    report a match whose innocence this check can establish mechanically, instead
    of passing the work to a reviewer who is given no more evidence than it had.
    """
    package_hits = _text_hits(
        [(p, f"matches {p}") for p in ZIP_API_PACKAGE_PATTERNS], java_files)

    declared = _declared_types(java_files)
    class_hits = []
    resolved = []
    # One pattern at a time, so the class name each hit belongs to is carried
    # rather than parsed back out of the rendered message -- which would couple
    # the resolution to the wording of the finding.
    for pattern in ZIP_API_CLASS_PATTERNS:
        name = pattern.replace(r"\b", "")
        where = declared.get(name)
        found = _text_hits([(pattern, f"matches {pattern}")], java_files)
        if not found:
            continue
        if where:
            resolved.append(f"{name} (declared in {where})")
            continue
        for path, line, quote, why in found:
            class_hits.append((path, line, quote,
                               f"{why}, and this tree declares no {name}"))

    hits = package_hits + class_hits
    note = ""
    if resolved:
        note = ("\n\nResolved to this port's own types and not reported: "
                + ", ".join(sorted(set(resolved)))
                + ". instruction.md's surface table requires these names.")
    assert not hits, (
        f"{len(hits)} reference(s) that reach the JDK compression API, or name a "
        f"class this tree does not declare.\n"
        f"{_render(hits)}\n\n"
        f"For each: is this an import that reaches java.util.zip, or a comment "
        f"about not using it? Names that resolve to this port's own declarations "
        f"have already been excluded, so what is left is either the defect or a "
        f"comment. It is the one defect that makes every behavioural case pass "
        f"while the task goes undone."
        + note
    )


def test_no_reflective_class_loading(java_files):
    """`Class.forName`, `MethodHandles`, and a name built at run time.

    The evasion the constant-pool scan in stage 2 cannot see. `Class.forName` with
    a literal is visible to both instruments; `Class.forName("java.util." + "zip"
    + ".Deflater")` is visible only to a reader, and that asymmetry is the reason
    this gate is asked in both stages rather than one.

    Reflection is not forbidden. A port with a pluggable checksum might use it
    honestly. What the reviewer has to establish is what the resolved name can be.
    """
    patterns = (
        (r"\bClass\s*\.\s*forName\b", "Class.forName"),
        (r"\bMethodHandles\s*\.\s*lookup\b", "MethodHandles.lookup"),
        (r"\bgetDeclaredMethod\b", "getDeclaredMethod"),
        (r"\bServiceLoader\s*\.\s*load\b", "ServiceLoader.load"),
        (r"\bClassLoader\b", "a ClassLoader reference"),
        (r"\bgetSystemClassLoader\b", "getSystemClassLoader"),
    )
    hits = _text_hits(patterns, java_files)
    assert not hits, (
        f"{len(hits)} reflective entry point(s) in Java sources.\n{_render(hits)}\n\n"
        f"Reflection is legal. What is the resolved name, and can it be a JDK "
        f"compression class assembled from parts? A literal is checkable in stage "
        f"2's constant pool; a concatenation is only checkable here."
    )


@pytest.mark.parametrize("prefix", sorted(set(FORBIDDEN_PREFIXES)))
def test_forbidden_type_prefix_absent(prefix: str, text_files):
    """One forbidden type prefix, from the class-file contract, over all text.

    Not only Java: a `--add-modules java.util.zip` in a CMake file or a wrapper
    script reaches the same place, and the class-file contract's prefix list is
    the same list stage 2 applies to the constant pool. Applying it to text
    catches the build's half of the same evasion.
    """
    if prefix == MISSING:
        pytest.fail("the contract states no classfile_contract.forbidden_prefixes")
    needle = prefix.replace("/", ".").rstrip(".")
    hits = []
    for path in text_files:
        # find_text rather than a containment test plus locate(): the third slot of
        # a hit is the quoted line -- see _render.  Both are shapes a correct
        # submission produces, and the gate is no-jdk-deflate.
        found = srbscan.find_text(path, needle)
        if found:
            line, quote = found
            hits.append((rel(REPO, path), line, quote, f"contains {needle}"))
    assert not hits, (
        f"the forbidden type prefix {needle} appears in {len(hits)} file(s).\n"
        f"{_render(hits)}"
    )


# --------------------------------------------------------------------------- #
# no-vendored-zlib, no-embedded-reference
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", FOREIGN_IMPLEMENTATIONS)
def test_no_foreign_implementation_named(name: str, text_files):
    """One third-party compression implementation, by name, anywhere in text.

    `no-vendored-zlib` is the sharpest gate on this task and the one no artifact
    can answer. jzlib is a real, complete, pure-Java port of zlib: dropping its
    sources in, renaming the package to `org.zlib` and adjusting the public
    signatures produces a jar that passes every behavioural case, declares no
    native method, requires nothing outside java.base, and contains several
    thousand lines of Java. Every mechanical instrument this benchmark has says
    yes.

    What says no is a reader comparing the Java against the C it claims to be a
    port of. Resemblance is not the finding -- a faithful port of `deflate.c`
    looks like `deflate.c`, and it is supposed to. The finding is Java that
    matches a *different* implementation's structure: variable names, table
    layouts and function decomposition that came from somewhere other than the
    eighteen files in /opt/original.

    A mention in a comment ("this differs from jzlib in ...") is innocent and
    lands here. So does a NOTICE file listing what the port is not.

    The name must appear as a name: see srbscan.find_name, which is where the
    word-boundary rule and its reason live. The hit quotes the line, because on a
    judgement gate the reviewer's next question is always "quoted where?" and a
    finding it cannot check cheaply is one it has to either trust or ignore.
    """
    hits = []
    for path in text_files:
        found = srbscan.find_name(path, name)
        if found:
            line, quote = found
            hits.append((rel(REPO, path), line, quote, f"mentions {name}"))
    assert not hits, (
        f"{name} is named in {len(hits)} file(s).\n{_render(hits)}\n\n"
        f"Is this a mention, or is it the implementation? Compare the Java "
        f"against /opt/original's C, not against your memory of what zlib looks "
        f"like."
    )


def test_no_second_language_implementation(files, java_files):
    """A complete implementation in some third language.

    `no-c-fallback` in the form it takes when the fallback is not C. A tree
    holding a Rust, Go or C# implementation of deflate beside the Java has not
    ported to Java; it has ported somewhere else and wrapped the result. The
    build cannot use it -- the install inventory is one jar -- but the reviewer
    should know it is there.
    """
    implementation_suffixes = {
        ".rs": "Rust", ".go": "Go", ".cs": "C#", ".kt": "Kotlin",
        ".scala": "Scala", ".zig": "Zig", ".swift": "Swift", ".d": "D",
        ".ml": "OCaml", ".hs": "Haskell", ".jl": "Julia", ".nim": "Nim",
    }
    found = {}
    for path in files:
        language = implementation_suffixes.get(path.suffix)
        if language:
            found.setdefault(language, []).append(rel(REPO, path))
    assert not found, (
        "the submission contains source in another implementation language: "
        + "; ".join(f"{lang}: {', '.join(paths[:4])}" for lang, paths in found.items())
        + f" (against {len(java_files)} .java file(s)). Kotlin and Scala compile "
        f"to the same bytecode a jar carries, so an implementation in either "
        f"would pass every artifact check while not being the Java port the task "
        f"asks for."
    )


# --------------------------------------------------------------------------- #
# no-native-methods, no-library-load, no-exec-helpers, no-unsafe-ffm
# --------------------------------------------------------------------------- #

def test_no_escape_from_the_jvm(text_files):
    """A native method, a library load, a subprocess, or an FFM downcall.

    All four are the same finding wearing different clothes: the compressed bytes
    were produced by something that is not this repository's Java. Stage 2
    measures the artifact half of this under `no-native-methods`, `no-library-load`
    and `no-exec-helpers`; what it cannot see is a build script that sets
    `-Djava.library.path`, or a wrapper that execs a helper before the JVM starts,
    and both of those are text.

    The innocent readings: `native` appears in `alternatives --set java` prose, in
    a comment about what the C did, and as part of `nativeLinker` in a doc-comment
    explaining why the port avoids it.
    """
    hits = _text_hits(ESCAPE_PATTERNS, text_files)
    assert not hits, (
        f"{len(hits)} way(s) out of the JVM in the submission.\n{_render(hits)}\n\n"
        f"Does control actually leave the JVM here, or is this a comment, a "
        f"string, or a name that merely contains the word? The compression paths "
        f"are the ones that matter."
    )


def test_no_system_compressor_invocation(text_files):
    """An external compressor, spawned rather than implemented.

    `gzip`, `zcat`, `pigz`, `zlib-flate`: any of them run as a process is the
    task not done, and none of them shows up in the constant pool if the exec
    happens in the CMake-generated wrapper script rather than in Java.

    Heavily innocent by default: `gzip` appears in `ChangeLog`, in the three RFCs,
    in `README`, and in every doc-comment about the gzip wrapper format. The
    preserved documentation alone will produce most of what lands here.
    """
    patterns = (
        (r"\bexec(vp?|lp?)?\s*\(\s*[\"']?(gzip|gunzip|zcat|pigz|zlib-flate)\b",
         "an exec of a system compressor"),
        (r"\|\s*(gzip|gunzip|zcat|pigz)\b", "a pipe into a system compressor"),
        (r"\b(gzip|gunzip|zcat|pigz)\s+-[a-z0-9]+\s", "a system compressor with flags"),
        (r"\bfind_(program|package)\s*\(\s*\w*\s*(ZLIB|GZIP|PIGZ)\b",
         "a CMake search for a system compressor"),
        (r"\bpkg_check_modules\s*\([^)]*\bzlib\b", "pkg-config for a system zlib"),
        (r"\btarget_link_libraries\s*\([^)]*\b(z|zlib)\b",
         "linking against a system zlib"),
        (r"-l\s*z\b", "-lz on a command line"),
    )
    hits = _text_hits(patterns, text_files, flags=re.IGNORECASE)
    assert not hits, (
        f"{len(hits)} reference(s) to an external compressor.\n{_render(hits)}\n\n"
        f"Most of what lands here is documentation: ChangeLog, README and the "
        f"three preserved RFCs all discuss gzip at length, and none of that is a "
        f"finding. What matters is whether the *build* or the *library* runs one."
    )


# --------------------------------------------------------------------------- #
# no-c-in-build, no-network
# --------------------------------------------------------------------------- #

def test_build_files_compile_no_c_and_fetch_nothing(files):
    """The build's own text, for a C language and for a download.

    Two gates in one check because they are one question -- what does the build do
    that it should not -- and because the file set is the same: CMakeLists.txt,
    every `*.cmake`, and any Maven or Gradle file a submission added.

    `no-c-in-build` has a subtlety the reviewer owns. `project()` in State A's
    CMakeLists.txt declares C, and a submission that leaves the declaration while
    compiling nothing has a build that *requires a C compiler to configure* and
    compiles no C. That is a defensible reading of "keep the build interface" and
    a bad one: the graded image has no C compiler, so the configure fails and
    stage 2 reports it as a build failure. Read it as sloppiness, not as cheating.
    """
    build_files = [p for p in files if p.name in (
        "CMakeLists.txt", "pom.xml", "build.gradle", "build.gradle.kts",
        "settings.gradle", "Makefile", "GNUmakefile", "build.xml",
    ) or p.suffix in (".cmake", ".cmakein")]
    if not build_files:
        pytest.fail(
            "the submission has no CMakeLists.txt and no other build file. "
            "CMakeLists.txt is a preserved path and CMake stays the build driver."
        )
    hits = _text_hits(BUILD_PATTERNS, build_files, flags=re.IGNORECASE)
    assert not hits, (
        f"{len(hits)} finding(s) in {len(build_files)} build file(s).\n"
        f"{_render(hits)}\n\n"
        f"Does the build compile a translation unit, or reach the network? Note "
        f"that the graded build runs with no network at all, so a fetch here is "
        f"a build that cannot work rather than one that cheats -- unless what it "
        f"fetches is already vendored beside it."
    )


def test_no_committed_dependency_archives(files):
    """A jar, wheel or tarball checked in beside the sources.

    The way `no-network` is satisfied dishonestly: vendor the dependency instead
    of downloading it, and the build needs no network because the artifact is
    already there. `allowed_dependencies` is java.base only, so any archive in
    the tree is either a dependency that should not exist or build output that
    should not be committed.
    """
    archive_suffixes = (".jar", ".war", ".zip", ".tar", ".gz", ".tgz", ".bz2",
                        ".xz", ".whl", ".jmod", ".aar")
    # doc/crc-doc.1.0.pdf is a preserved-adjacent document, not an archive; the
    # .gz check would otherwise fire on any compressed test fixture State A ships.
    upstream = {srbscan.rel(srbscan.ORIGINAL, p)
                for p in srbscan.walk_source(srbscan.ORIGINAL)}
    hits = [rel(REPO, p) for p in files
            if p.suffix.lower() in archive_suffixes
            and rel(REPO, p) not in upstream]
    assert not hits, (
        f"{len(hits)} archive(s) in the source tree that State A did not have: "
        f"{', '.join(hits[:10])}. The allowed dependency set is java.base only, "
        f"so what is in them?"
    )
