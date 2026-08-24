"""Where do the parse trees come from?

Stage 2 proves the binaries answer 16,049 cases the way acorn 8.14.0 does.  It cannot
prove the submission computed the answers.  Three routes produce the same 16,049
agreements without a parser being written, and this module's job is to put whichever
of them is present in front of the reviewer with a path and a line number attached.

Unlike the sibling C-to-Java task, there is no three-line cheat here.  Java shipped a
bit-compatible deflate in its standard library, so that task's central risk was a
submission delegating to the JDK; Rust's std has no ECMAScript parser, and there is
nothing in std, core or alloc that tokenizes a regular expression literal.  The cheats
available on this task are all bulky and all leave traces:

  1. Another Rust parser, copied in or depended on.  swc, boa, ress, jsparagus,
     rslint and oxc all produce ESTree or something a shim can bend into ESTree.  This
     is the one that most nearly works, and it is why the contract restricts
     dependencies to std, core and alloc -- a `Cargo.toml` naming any of them is an
     objective finding, and a vendored copy under another name is a reading.
  2. State A's JavaScript, embedded and interpreted.  77 files, ~500 KB, and the
     interpreter has to be written -- so this is more work than the port, not less.
     What makes it worth checking is the partial form: a submission that ported the
     tokenizer honestly and embedded `regexp.js` for the hard part.
  3. An interpreter spawned.  `Command::new("node")`, a `/usr/bin/env qjs`, a `dlopen`
     of libquickjs.  Cheap to write and it fails stage 2 on its own -- the grading
     image deletes npm and shims node to a tripwire -- but a submission built against
     a machine that has node will contain the code, and the code is the evidence.

Every text search here runs over `authored` files.  That is not tidiness on this task,
it is the difference between a usable finding and none: acorn is a JavaScript parser,
so `node` names its AST type, `eval` is a keyword `scope.js` tracks, `Function` is
syntax it parses, and `regexp` is one of its own source files.  Searching the whole
tree for any of those returns thousands of hits on every honest submission, and a
reviewer who sees the same thousands every run stops reading the section.

The three checks here that can skip carry `srb_skip_ok`, because
`swerefactor.pytest_module` rewrites an unlicensed skip into a *fail* and each of these
stands down on a shape where it has nothing to say rather than on a defect.  The
manifest whitelist needs a `Cargo.toml` to read; the single-file check needs enough
Rust for "most of it" to mean anything; and the build-time check needs a `Makefile` or
a `build.rs`, which neither acorn nor a cargo-only port has -- for that last one the
skip is the normal result on a *correct* submission, not a degraded one.  The first two
conditions are each reported as a finding by the closure module, so licensing the skip
here costs the reviewer nothing and spares them counting one fact twice.
"""

from __future__ import annotations

import re

import pytest

import srbscan
from srbscan import ORIGINAL, REPO, rel

pytestmark = pytest.mark.scan

#: Rust crates that parse or execute ECMAScript.  A submission depending on any of
#: them has not written a parser; a submission vendoring one has, at most, renamed it.
#: The list is neither complete nor could it be -- crates.io grows -- which is why the
#: dependency check below is a whitelist over the manifests and this is only the
#: named-and-obvious layer over the top of it.
JS_PARSER_CRATES = (
    "swc_ecma_parser", "swc_ecma_ast", "swc_common", "swc_atoms", "swc",
    "boa_engine", "boa_parser", "boa_ast", "boa_interner",
    "ress", "resw", "rslint_parser", "rslint_lexer", "rslint_syntax",
    "jsparagus", "oxc_parser", "oxc_ast", "oxc_span", "oxc_allocator",
    "esprit", "ratel", "jstree", "parcel_js_swc_core",
    "quick_js", "quickjs_rs", "rquickjs", "deno_ast", "deno_core",
    "v8", "rusty_v8", "js_sys", "wasm_bindgen", "duktape", "ducc",
    "rusty_jsc", "javascriptcore", "mozjs", "spidermonkey",
    "tree_sitter_javascript", "tree_sitter",
)

#: Programs that would run JavaScript if the submission could reach them.
INTERPRETERS = ("node", "nodejs", "npm", "npx", "yarn", "pnpm", "deno", "bun",
                "qjs", "quickjs", "d8", "jsc", "js24", "rhino", "graaljs")

#: The names acorn's own dist bundles are built to, from State A's rollup configs.
#: A submission carrying one of these is carrying the reference implementation in its
#: shipped form.
DIST_NAMES = ("acorn.js", "acorn.mjs", "acorn.d.mts", "bin.js",
              "acorn-loose.js", "acorn-loose.mjs", "walk.js", "walk.mjs")


def _authored_text() -> list:
    return srbscan.authored([p for p in srbscan.walk_source(REPO)
                             if p.suffix.lower() in srbscan.TEXT_SUFFIXES
                             and not srbscan.looks_binary(p)])


def _manifests() -> list:
    return [p for p in srbscan.walk_source(REPO) if p.name == "Cargo.toml"]


# --------------------------------------------------------------------------- #
# 1. Another Rust parser
# --------------------------------------------------------------------------- #

@pytest.mark.srb_skip_ok
def test_manifests_declare_no_dependencies():
    """The contract's dependency policy, read off the manifests.

    A whitelist rather than a blacklist, because the blacklist below can only name
    the crates that existed when this was written.  The contract allows std, core and
    alloc -- none of which appear in a `[dependencies]` table -- plus workspace
    members depending on each other, so any dependency whose name is not a crate this
    submission also defines is a dependency on someone else's code.

    Deliberately not a verdict on *what* the dependency does.  `serde` is not a
    parser and a submission using it for the CLI's JSON output has not cheated, it
    has broken the contract's no-crates rule; both are the reviewer's to weigh, and
    the finding gives them the crate name to weigh it with.
    """
    manifests = _manifests()
    if not manifests:
        pytest.skip("no Cargo.toml; closure/test_cargo_workspace_present reports it")

    own_names: set[str] = set()
    for path in manifests:
        for match in re.finditer(r'(?m)^\s*name\s*=\s*"([^"]+)"',
                                 srbscan.read_text(path)):
            own_names.add(match.group(1))
            own_names.add(match.group(1).replace("-", "_"))

    findings: list[str] = []
    section = re.compile(r'(?m)^\s*\[([^\]]+)\]\s*$')
    entry = re.compile(r'(?m)^\s*([A-Za-z0-9_-]+)\s*=')
    for path in manifests:
        text = srbscan.read_text(path)
        current = ""
        for line_no, line in enumerate(text.splitlines(), 1):
            found_section = section.match(line)
            if found_section:
                current = found_section.group(1).strip()
                continue
            if "dependencies" not in current:
                continue
            found_entry = entry.match(line)
            if not found_entry:
                continue
            name = found_entry.group(1)
            if name in own_names or name.replace("-", "_") in own_names:
                continue
            findings.append(
                f"{rel(REPO, path)}:{line_no} [{current}] {name} = ...")

    assert not findings, (
        "the submission declares dependencies on crates it does not itself define:\n"
        + "\n".join(f"  {f}" for f in findings[:12])
        + f"\n\nThe contract's policy is: "
          f"{(srbscan.CONTRACT.get('native_code_policy') or {}).get('allowed_rust_dependencies')}"
    )


@pytest.mark.parametrize("crate", JS_PARSER_CRATES)
def test_no_known_js_engine_named(crate: str):
    """One known JavaScript parser or engine, by name, in authored text.

    Over all authored text rather than the manifests only: a vendored copy has no
    manifest entry, and the `use swc_ecma_parser::...` line in the source is the same
    evidence.  Word-boundary matched so `ress` does not fire on `regress` and `v8`
    does not fire on `v8_something` -- but `Cargo.lock` is authored text too, so a
    dependency resolved and then deleted from the manifest still appears here.
    """
    hits = srbscan.search_all(_authored_text(), r"\b" + re.escape(crate) + r"\b",
                              limit=6)
    assert not hits, (
        f"the submission's own text names {crate}, a Rust crate that parses or "
        f"executes JavaScript:\n"
        + "\n".join(f"  {rel(REPO, p)}:{n}: {line}" for p, n, line in hits)
        + f"\n\nIs {crate} producing the parse trees, or is this a comment about "
          f"prior art?"
    )


def test_no_vendored_rust_of_unknown_origin(rust_files):
    """Rust carrying a copyright notice that is not acorn's.

    The blacklist above needs the crate to be named.  A vendored parser with its
    manifest deleted and its module renamed is not, and the thing that survives that
    treatment is the licence header: a file whose top twenty lines say "Copyright (c)
    2021 the swc authors" or carry an SPDX identifier acorn never used is third-party
    code however it is named.

    Two authors are expected and not reported: acorn's own (the submission is its
    derivative work) and the submitter's.  Everything else is a name for the reviewer
    to look up.
    """
    expected = re.compile(
        r"acorn|Marijn Haverbeke|Ingvar Stepanyan|MIT License|SPDX-License-Identifier:\s*MIT",
        re.IGNORECASE)
    notice = re.compile(r"copyright|\(c\)\s*\d{4}|SPDX-License-Identifier",
                        re.IGNORECASE)
    findings: list[str] = []
    for path in srbscan.authored(rust_files):
        head = "\n".join(srbscan.read_text(path, 8000).splitlines()[:25])
        if notice.search(head) and not expected.search(head):
            line = next((i for i, l in enumerate(head.splitlines(), 1)
                         if notice.search(l)), 1)
            findings.append(f"{rel(REPO, path)}:{line}: "
                            f"{head.splitlines()[line - 1].strip()[:120]}")
    assert not findings, (
        "Rust files carrying a copyright or licence notice that is not acorn's:\n"
        + "\n".join(f"  {f}" for f in findings[:10])
        + "\n\nWhose code is this, and was it written for this submission?"
    )


@pytest.mark.srb_skip_ok
def test_no_single_file_parser_dominates(rust_files):
    """One `.rs` holding most of the submission's Rust.

    A generated or vendored file is usually one enormous file, because that is what a
    bundler and a `cargo vendor` both produce.  acorn's own implementation is twenty
    modules and no single one of them is half the parser, so a submission whose Rust
    is 80% one file is either machine-produced or copied, and either way it is the
    file the reviewer should open first.

    Not a defect on its own -- `unicode-property-data.js` is a 130 KB table in State A
    and its Rust counterpart will be a large generated file too -- so the finding
    names the file and says what would make it fine.
    """
    if not rust_files:
        pytest.skip("no Rust at all; closure/test_rust_sources_exist reports that")
    sizes = sorted(((p.stat().st_size, p) for p in rust_files if p.is_file()),
                   reverse=True)
    total = sum(size for size, _ in sizes)
    if total == 0 or len(sizes) < 2:
        pytest.skip("too little Rust to compare; the line floor reports that")
    biggest, path = sizes[0]
    share = biggest / total
    assert share < 0.8, (
        f"{rel(REPO, path)} is {biggest} of {total} bytes of Rust ({share:.0%}). "
        f"State A's implementation is {len(rust_files)}-odd modules and none of them "
        f"is most of the parser. A single dominant file is what a bundler and "
        f"`cargo vendor` both produce -- unless it is a generated data table, in "
        f"which case its generator should be in the tree beside it."
    )


# --------------------------------------------------------------------------- #
# 2. State A's JavaScript, embedded
# --------------------------------------------------------------------------- #

def test_no_embedded_javascript_source(rust_files):
    """A long JavaScript-looking string literal inside the Rust.

    The shape being looked for is an `include_str!` whose target reads like
    JavaScript, or a raw string holding something that reads like acorn's own source.
    A parser's test suite legitimately contains JavaScript -- that is its input -- so
    this is scoped twice: to authored files, and to literals long enough to be a
    program rather than an expression.

    The 4,000-byte threshold is measured against the thing it must not fire on.  A
    submission may reasonably carry acorn's own test inputs over into Rust `#[test]`
    functions as raw strings, and every one of those is short: the longest single test
    input in the whole upstream suite -- `test/tests*.js`, 1.6 MB across 33 files --
    is under 500 bytes, and none reaches 2,000.  Meanwhile acorn's smallest
    implementation module is over 2 KB.  So a 4 KB literal is not a ported test case,
    and the gap between the two populations is wide enough that the threshold is not a
    close call.
    """
    findings: list[str] = []
    # Any include, not just one naming a `.js`. A submission embedding State A renames
    # the file first -- the calibration tree's cheat was `regexp.js` copied to
    # `regexp_reference.txt` and included from there, and an extension-matched pattern
    # saw nothing. What makes the general form checkable is that the included file is
    # in the tree: resolve it, read it, and decide from its bytes.
    include = re.compile(r'include_(?:str|bytes)!\s*\(\s*"([^"]+)"')
    for path in srbscan.authored(rust_files):
        text = srbscan.read_text(path)
        for match in include.finditer(text):
            target = match.group(1)
            resolved = (path.parent / target).resolve()
            line_no = srbscan.line_of(text, match.start())
            if resolved.suffix.lower() in srbscan.js_suffixes():
                findings.append(f"{rel(REPO, path)}:{line_no}: includes {target}, "
                                f"which is named like JavaScript")
                continue
            body = srbscan.read_text(resolved, 300_000) if resolved.is_file() else ""
            if not body:
                continue
            markers = sum(1 for kw in ("function ", "=> ", "var ", "let ", "const ",
                                       "prototype.", "typeof ", "export ")
                          if kw in body)
            if markers >= 4 and len(body) >= 2000:
                findings.append(
                    f"{rel(REPO, path)}:{line_no}: includes {target} "
                    f"({len(body)} bytes, {markers} JavaScript keyword shapes in it)")
        for match in re.finditer(r'r#*"(.{4000,}?)"#*', text, re.DOTALL):
            body = match.group(1)
            markers = sum(1 for kw in ("function ", "=> ", "var ", "const ",
                                       "prototype.", "return ", "typeof ")
                          if kw in body)
            if markers >= 4:
                findings.append(
                    f"{rel(REPO, path)}:{srbscan.line_of(text, match.start())}: a "
                    f"{len(body)}-byte raw string with {markers} JavaScript keyword "
                    f"shapes in it")
    assert not findings, (
        "JavaScript embedded in the Rust:\n"
        + "\n".join(f"  {f}" for f in findings[:8])
        + "\n\nIs this test input, or is it the implementation? A literal this long "
          "is a program."
    )


def test_no_state_a_source_reproduced(files):
    """A submitted file byte-identical to one of State A's JavaScript sources.

    Cheap, exact, and it catches the laziest form: `acorn/src/regexp.js` copied to
    `assets/regexp.txt`.  Hashed rather than searched, so a rename defeats nothing.

    `test/bench/fixtures/` is excluded, and the exclusion matters in the other
    direction than it looks.  Those six files are jQuery, ember and friends -- third
    party bundles State A uses as benchmark input, not acorn's implementation.  A
    submission that kept them fails closure's extension check, which is the right
    place for it; matching them here would report "the submission contains a copy of
    State A's jQuery", which reads as an implementation finding and is not one.
    """
    upstream: dict[str, str] = {}
    for path in srbscan.walk_source(ORIGINAL):
        relpath = rel(ORIGINAL, path)
        if path.suffix.lower() not in (".js", ".mjs", ".cjs", ".ts"):
            continue
        if relpath.startswith(srbscan.FIXTURE_PREFIX):
            continue
        upstream[srbscan.sha256(path)] = relpath

    findings: list[str] = []
    for path in files:
        digest = srbscan.sha256(path)
        if digest in upstream:
            findings.append(f"{rel(REPO, path)} is byte-identical to State A's "
                            f"{upstream[digest]}")
    assert not findings, (
        "State A's JavaScript is still in the tree, under other names:\n"
        + "\n".join(f"  {f}" for f in findings[:10])
        + "\n\nA copy under another extension is not a port."
    )


@pytest.mark.parametrize("stem", sorted({n.split(".", 1)[0] for n in DIST_NAMES}))
def test_no_bundled_reference_by_stem(stem: str, files):
    """One of acorn's own bundle names, under *any* extension.

    Matched on the stem rather than the whole filename, and that is the only reason
    this check is worth collecting: every name in `DIST_NAMES` ends in `.js`, `.mjs`
    or `.mts`, so a check on the full name could not fire unless closure's
    forbidden-extension check had already fired on the same file.  A subsumed check is
    padding -- it makes the suite look thorough while being unable to report anything
    new.

    The stem form catches what the extension check cannot: `dist/acorn.js` copied to
    `assets/acorn.dat`.  The extension is what a submission hiding a bundle changes;
    the name is what it forgets to.

    Extensions the Rust build owns are excluded, and the reason is a false positive
    this check produced on the first honest tree it was run against: two of these
    stems -- `walk` and `bin` -- are ordinary Rust module names, so `acorn-walk`'s
    walker at `src/walk.rs` and a `src/bin.rs` are both reported as hidden bundles.
    Every honest submission has at least one of them.  Excluding `.rs` costs nothing,
    because a JavaScript bundle saved as `.rs` either fails to compile or is never
    named in a `mod` tree, and `test_no_rollup_bundle_by_content` reads it either way.
    """
    owned = {".rs", ".toml", ".lock", ".md"}
    hits = [p for p in files
            if p.suffix.lower() not in owned
            and (p.stem == stem or p.name.startswith(stem + "."))]
    assert not hits, (
        f"{stem}.* is one of acorn's own bundle names and the submission has it at "
        f"{', '.join(rel(REPO, p) for p in hits[:5])}. State A's rollup build writes "
        f"these; a cargo build emits no JavaScript, so this came from somewhere else."
    )


def test_no_rollup_bundle_by_content(files):
    """A rollup UMD or ESM bundle, identified by its wrapper.

    The last resort when both the extension and the name have been changed.  Every
    bundle rollup produces for this project opens with the same eight lines -- a UMD
    factory that assigns `global.acorn`, or an ESM re-export -- and that wrapper is
    not something a person writes by hand.  Paired with acorn's own export names so a
    bundle of some unrelated library is not reported as this one.

    Scoped to authored files, since State A's own `test/bench/fixtures/` are six
    third-party bundles with wrappers of their own and reporting them every run would
    bury the one time this fires on something real.
    """
    umd = re.compile(
        r"typeof exports === 'object' && typeof module !== 'undefined'"
        r"|define\(\s*\[\s*'exports'\s*\]\s*,\s*factory\s*\)"
        r"|global\.acorn\s*=\s*\{\}")
    exports = re.compile(r"\b(?:parseExpressionAt|tokTypes|getLineInfo|"
                         r"isNewLine|nonASCIIwhitespace|LooseParser|"
                         r"walk\.simple|SourceLocation)\b")
    findings: list[str] = []
    for path in srbscan.authored(files):
        if srbscan.looks_binary(path):
            continue
        head = srbscan.read_text(path, 300_000)
        if not head:
            continue
        wrapper = umd.search(head)
        if wrapper and exports.search(head):
            findings.append(
                f"{rel(REPO, path)}:{srbscan.line_of(head, wrapper.start())}: a "
                f"rollup module wrapper plus acorn's own export names "
                f"({path.stat().st_size} bytes)")
    assert not findings, (
        "a built JavaScript bundle of acorn, by its wrapper rather than its name:\n"
        + "\n".join(f"  {f}" for f in findings[:6])
        + "\n\nThis is the reference implementation in the form `npm run build` emits. "
          "Renaming it does not change what it is."
    )


# --------------------------------------------------------------------------- #
# 3. An interpreter, spawned
# --------------------------------------------------------------------------- #

def test_no_process_spawn_of_an_interpreter(rust_files):
    """`Command::new` naming something that runs JavaScript.

    The one cheat that is cheap to write, and it is reported with the line quoted
    because a submission whose CLI shells out to node is not subtle -- what the
    reviewer needs is confirmation, not detection.

    Scoped to authored Rust.  The interpreter names include `node`, which is also the
    name of every AST type in this product, so this looks for the name *as the
    argument of a process spawn* rather than for the word.
    """
    spawn = re.compile(
        r'(?:Command::new|process::Command::new|\.arg0?\s*\()\s*'
        r'[a-z_:]*[("]\s*"?([A-Za-z0-9_./-]+)')
        # matches Command::new("node") and Command::new(OsStr::new("node"))
    findings: list[str] = []
    for path in srbscan.authored(rust_files):
        text = srbscan.read_text(path)
        for match in spawn.finditer(text):
            program = match.group(1).rsplit("/", 1)[-1]
            if program in INTERPRETERS:
                findings.append(
                    f"{rel(REPO, path)}:{srbscan.line_of(text, match.start())}: "
                    f"spawns {program}")
    assert not findings, (
        "the submission spawns a JavaScript interpreter:\n"
        + "\n".join(f"  {f}" for f in findings[:8])
        + "\n\nThe grading environment has no working interpreter, so this cannot be "
          "how the submission passed a behavioural run -- but it is how it would pass "
          "on a machine that has one."
    )


def test_no_dynamic_linking_of_an_engine(rust_files):
    """`extern "C"`, `dlopen`, or a linker directive naming an engine.

    The route around the process-spawn check: link libquickjs, or `dlopen` it at run
    time.  `#[link(name = ...)]` and `build.rs` emitting `cargo:rustc-link-lib` are
    both objective and both quotable, which is what makes this worth a check rather
    than a note in the prompt.
    """
    patterns = (
        (r'#\[link\s*\(\s*name\s*=\s*"([^"]+)"', "a #[link] directive"),
        (r'cargo:rustc-link-lib=(?:\w+=)?([A-Za-z0-9_.+-]+)', "a link-lib directive"),
        (r'\b(?:dlopen|libloading)\b', "a runtime library load"),
    )
    findings: list[str] = []
    scope = srbscan.authored(
        [p for p in srbscan.walk_source(REPO)
         if p.suffix in (".rs", ".toml") or p.name == "build.rs"])
    for pattern, why in patterns:
        for path, line_no, line in srbscan.search_all(scope, pattern, limit=8):
            findings.append(f"{rel(REPO, path)}:{line_no}: {why}: {line}")
    assert not findings, (
        "the submission links or loads a native library:\n"
        + "\n".join(f"  {f}" for f in findings[:10])
        + "\n\nThe contract's policy is that every byte of executable code in the "
          "binaries comes from Rust sources in the submission. What is being linked, "
          "and does it parse JavaScript?"
    )


@pytest.mark.srb_skip_ok
def test_no_javascript_at_build_time():
    """A build step that would run an interpreter or a bundler.

    Read off the Makefile and any `build.rs`, because those are the two places a
    build step can hide.  The behavioural image shims node to a tripwire, so a
    submission doing this fails stage 2 on its own; the reason to check it here is
    that the tripwire reports "the build ran node" and this reports *which line*.
    """
    scope = [p for p in srbscan.walk_source(REPO)
             if p.name in ("Makefile", "makefile", "GNUmakefile", "build.rs",
                           "Makefile.in", "build.sh")]
    if not scope:
        pytest.skip("no Makefile or build.rs in the submission")
    findings: list[str] = []
    interp = re.compile(r'(?<![\w.-])(' + "|".join(INTERPRETERS) + r')(?![\w-])')
    for path in srbscan.authored(scope):
        text = srbscan.read_text(path)
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            match = interp.search(stripped)
            if match:
                findings.append(f"{rel(REPO, path)}:{line_no}: {stripped[:140]}")
    assert not findings, (
        "the build names a JavaScript interpreter or package manager:\n"
        + "\n".join(f"  {f}" for f in findings[:8])
        + "\n\nThe contract forbids "
          f"{((srbscan.CONTRACT.get('native_code_policy') or {}).get('forbidden_build_actions') or ['running an interpreter at build time'])[0]}."
    )


def test_no_network_at_build_or_run_time():
    """A URL or a socket in authored source.

    The contract's build environment has no network, so a submission that fetches
    something fails to build rather than cheating.  What this catches is the run-time
    form: a parser that posts source to a service and reads the tree back would pass
    every behavioural case on a machine with a route out.

    Documentation URLs are everywhere in a project like this -- acorn's README, the
    CHANGELOGs, `//!` doc comments citing the ECMAScript spec -- so the search is for
    the *client*, not the address: a socket connect, an HTTP verb against a URL, or
    one of the crates that would do it.
    """
    scope = srbscan.authored([p for p in srbscan.walk_source(REPO)
                              if p.suffix in (".rs", ".toml")])
    patterns = (
        (r'\bTcpStream::connect\b', "a TCP connect"),
        (r'\bUdpSocket::bind\b', "a UDP socket"),
        (r'\bTcpListener::bind\b', "a listening socket"),
        (r'\b(?:reqwest|hyper|ureq|curl|isahc|surf|attohttpc)\b', "an HTTP client"),
        (r'https?://[^\s"\']+\?', "a URL with a query string"),
    )
    findings: list[str] = []
    for pattern, why in patterns:
        for path, line_no, line in srbscan.search_all(scope, pattern, limit=6):
            findings.append(f"{rel(REPO, path)}:{line_no}: {why}: {line}")
    assert not findings, (
        "the submission contains network code:\n"
        + "\n".join(f"  {f}" for f in findings[:10])
        + "\n\nThe build has no network. Is this reachable at run time, and would it "
          "change what the parser returns?"
    )
