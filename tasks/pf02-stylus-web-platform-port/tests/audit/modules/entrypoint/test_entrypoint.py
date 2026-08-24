"""What does the package publish, and is there one implementation behind it?

Advisory.  Stage 2 already measures that the compiler works: it loads
`src/core/index.js` into the realm, executes `bin/stylus` as a program, and
compares 356 stylesheets against State A.  What it cannot see is *which* code
answered.  A submission that keeps `lib/` and points `exports['./web']` at a
thin wrapper over it produces byte-identical CSS through every one of those
checks and has ported nothing.

So this module reads the declarations -- the exports map, `bin`, the root
`index.js` -- and looks for a second copy of the implementation.  Its most useful
findings are the ones about things that are still *present*, which is the
opposite of `closure`: there, an empty tree passes everything; here, a tree with
two implementations of the same visitor is what fails.
"""

from __future__ import annotations

import json

import pytest
import srbscan

pytestmark = pytest.mark.scan


def _target(entry):
    """The path an `exports` entry resolves to, conditional or plain."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        for key in ("import", "default", "module", "node"):
            if isinstance(entry.get(key), str):
                return entry[key]
    return None


# ---------------------------------------------------------------------------
# The old tree
# ---------------------------------------------------------------------------

def test_the_retired_lib_tree_is_gone(repo):
    """§2.1: `lib/` must not exist.

    The most mechanical check in the suite and the one the review should not have
    to spend a turn on.  A hit is not automatically a failed migration -- the
    reviewer decides that -- but it is the first thing to look at, because every
    "is the new code the production path" question changes shape when the old code
    is still in the tree.
    """
    lib = repo / "lib"
    if not lib.is_dir():
        return
    js = srbscan.js_files(lib)
    pytest.fail(
        f"lib/ is still present: {len(js)} JavaScript file(s), "
        f"{sum(1 for _ in lib.rglob('*') if _.is_file())} file(s) total. §2.1 says "
        "the directory must not exist. Read whether anything outside test/ reaches "
        "it -- a directory nothing imports is dead weight, one the entry points "
        "reach is the old compiler still running.\n"
        f"lib/ present with {len(js)} .js file(s)"
    )


def test_nothing_outside_the_upstream_suite_imports_lib(repo):
    """§2.1's second half, over the whole tree rather than just the core.

    `closure` asks this of `src/core/` only.  Here it is asked of everything the
    submission ships, because an adapter or a CLI that imports `lib/` is the same
    finding: the retired tree is still the implementation.
    """
    hits = [
        h for h in srbscan.imports_of(repo, "lib/")
        if not h.where.startswith(("test/", "tests/", "node_modules/"))
    ]
    srbscan.fail_if(hits, (
        "files outside test/ still import from lib/:\n  "
        + "\n  ".join(str(h) for h in hits[:15])
        + "\n§2.1 forbids keeping lib/ as the implementation behind a new "
        "directory.\n"
        + srbscan.cite_line(hits)
    ))


def test_the_root_index_does_not_point_at_lib(repo):
    """`index.js`, if it is still there, must not re-export the old tree.

    Deleting it is allowed: §1.5 moves entry resolution into `exports`.  What is
    not allowed is leaving it as a bridge to `lib/`.
    """
    index = repo / "index.js"
    if not index.is_file():
        pytest.skip("no root index.js; §1.5 permits removing it")
    text = srbscan.strip_noise(srbscan.read(index), keep_specifiers=True)
    srbscan.fail_if("lib/" in text, (
        f"index.js still names lib/:\n  {text.strip()[:300]}\n"
        "index.js:1 -- the root entry point resolves into the retired tree"
    ))


# ---------------------------------------------------------------------------
# What the manifest declares
# ---------------------------------------------------------------------------

def test_exports_map_declares_both_faces(repo):
    """§1.5: `.` into `src/node/`, `./web` into `src/core/`.

    Recorded here rather than scored anywhere: stage 2 resolves the package by
    path, not through the exports map, so a wrong map does not fail a behavioural
    check.  It is what a consumer would use, which makes it the published shape of
    the port.
    """
    pkg = srbscan.package_json(repo)
    if not pkg:
        pytest.skip("package.json is missing or unparseable; reported by `frozen`")

    exports = pkg.get("exports")
    srbscan.fail_if(not isinstance(exports, dict), (
        f"package.json has no `exports` object (§1.5); got {type(exports).__name__}.\n"
        "package.json: no exports map, so neither face of the port is published"
    ))

    problems = []
    for key, want in ((".", "src/node"), ("./web", "src/core")):
        got = _target(exports.get(key))
        if not got:
            problems.append(f"exports[{key!r}] missing or not a path: {exports.get(key)!r}")
            continue
        if want not in got.replace("\\", "/"):
            problems.append(f"exports[{key!r}] = {got!r}, which does not resolve into {want}/")
            continue
        resolved = repo / got.lstrip("./")
        if not resolved.is_file():
            problems.append(f"exports[{key!r}] = {got!r}, which does not exist")

    srbscan.fail_if(problems, (
        "the exports map does not publish the two faces §1.5 asks for:\n  "
        + "\n  ".join(problems)
        + "\npackage.json: " + "; ".join(problems)[:200]
    ))


def test_the_cli_is_still_a_program(repo):
    """`bin/stylus` exists, has its shebang, and is executable.

    Stage 2 executes this file 376 times, so a missing one is caught there far more
    loudly than here.  What stage 2 cannot report is the mode bit: it runs the CLI
    through the interpreter named in the shebang when it has to, so a file that
    lost `+x` still passes there and would not work for a user who installed the
    package.
    """
    cli = repo / "bin" / "stylus"
    srbscan.fail_if(not cli.is_file(), (
        "bin/stylus is missing. §1.6 keeps the CLI.\nbin/stylus: absent"
    ))
    first = srbscan.read(cli).splitlines()[:1]
    shebang = first[0] if first else ""
    problems = []
    if not shebang.startswith("#!"):
        problems.append(f"no shebang; first line is {shebang[:60]!r}")
    elif "node" not in shebang:
        problems.append(f"shebang does not name node: {shebang[:60]!r}")
    if not cli.stat().st_mode & 0o111:
        problems.append(f"not executable (mode {cli.stat().st_mode & 0o777:o})")
    srbscan.fail_if(problems, (
        "bin/stylus is no longer a runnable program: " + "; ".join(problems)
        + "\nbin/stylus:1 -- " + "; ".join(problems)[:200]
    ))


def test_the_cli_goes_through_the_adapter(repo):
    """§1.6: the CLI is a client of `src/node/`, not of a private path.

    A CLI that reaches into `src/core/` directly has to build the platform object
    itself, which means there are two of them -- and then the one stage 2 exercises
    through the CLI is not the one a library consumer gets.
    """
    cli = repo / "bin" / "stylus"
    if not cli.is_file():
        pytest.skip("no bin/stylus; reported above")
    specs = srbscan.specifiers(srbscan.read(cli))
    reaches_core = [s for s in specs if "src/core" in s.replace("\\", "/")]
    srbscan.fail_if(reaches_core, (
        f"bin/stylus imports the core directly: {reaches_core}\n"
        "§1.6 makes the CLI a client of src/node/. Reaching past the adapter means "
        "the CLI assembles its own platform object, so there are two.\n"
        f"bin/stylus: imports {reaches_core[:3]}"
    ))


# ---------------------------------------------------------------------------
# Two implementations
# ---------------------------------------------------------------------------

def test_no_near_duplicate_files_under_src(repo):
    """§2.4: one implementation of each compiler stage.

    Overlap by shared significant lines, thresholds in `srbscan`
    (>= 40 shared lines and >= 60% of the smaller file).  Chosen to sit above the
    noise floor of two files that both import the same six things and below a
    copied visitor.  It will still pair two genuinely similar-but-distinct
    modules, which is why the finding names both paths and the counts: the reviewer
    opens them and says which.
    """
    src = repo / "src"
    if not src.is_dir():
        pytest.skip("no src/ directory; the shape question is the review's")

    pairs = srbscan.duplicate_files(src)
    lines = [
        f"{a.relative_to(repo)} <-> {b.relative_to(repo)}: "
        f"{shared} shared lines, {frac:.0%} of the smaller file"
        for a, b, shared, frac in pairs[:10]
    ]
    srbscan.fail_if(pairs, (
        f"{len(pairs)} near-duplicate file pair(s) under src/:\n  "
        + "\n  ".join(lines)
        + "\n§2.4 allows one implementation of each stage. A pair here is either a "
        "copied visitor or two modules that legitimately look alike -- opening both "
        "is the only way to tell.\n"
        f"{len(pairs)} pair(s): "
        + "; ".join(f"{a.relative_to(repo)}~{b.relative_to(repo)}"
                   for a, b, _, _ in pairs[:3])
    ))


def test_no_two_delivered_files_are_byte_identical(repo):
    """The threshold-free half of §2.4, and the half that catches small modules.

    Line-overlap needs a threshold, and any threshold quiet on unmodified Stylus
    (measured: 20 shared lines) is blind to a module below it -- which is most of
    them, since the median `lib/` module is 17 significant lines.  Equality has no
    such gap.  Sweeping pristine Stylus finds no byte-identical pair anywhere in the
    tree, so a group here was introduced by the submission.

    Leading and trailing whitespace is ignored and files under 60 bytes are not
    compared: a one-line re-export is legitimately repeated.
    """
    groups = [
        g for g in srbscan.identical_files(repo)
        if not all(
            p.relative_to(repo).as_posix().startswith(("test/", "tests/",
                                                       "node_modules/"))
            for p in g
        )
    ]
    lines = [
        " == ".join(str(p.relative_to(repo)) for p in g[:4])
        + (f" (+{len(g) - 4} more)" if len(g) > 4 else "")
        for g in groups[:10]
    ]
    srbscan.fail_if(groups, (
        f"{len(groups)} group(s) of byte-identical files:\n  " + "\n  ".join(lines)
        + "\n§2.4 allows one implementation of each stage. Two files with the same "
        "bytes are the same implementation twice, and the reviewer needs to know "
        "which one the entry points reach.\n"
        f"{len(groups)} group(s): " + "; ".join(lines[:3])[:200]
    ))


def test_the_core_and_the_retired_tree_are_not_the_same_files(repo):
    """The cross-tree version of the check above.

    If `lib/` is gone this collects nothing to compare and passes trivially.  When
    both exist it is the sharpest finding in the module: a file under `src/core/`
    that is a near-copy of one under `lib/` is the old implementation moved rather
    than ported, and it is exactly what a submission produces when it renames the
    directory and calls the job done.
    """
    lib, src = repo / "lib", repo / "src"
    if not (lib.is_dir() and src.is_dir()):
        pytest.skip("lib/ and src/ do not both exist; nothing to compare")

    inside_src = {p for p in srbscan.js_files(src)}
    pairs = [
        (a, b, shared, frac)
        for a, b, shared, frac in srbscan.duplicate_files(lib, src)
        if (a in inside_src) != (b in inside_src)
    ]
    lines = [
        f"{a.relative_to(repo)} <-> {b.relative_to(repo)}: "
        f"{shared} shared lines, {frac:.0%}"
        for a, b, shared, frac in pairs[:10]
    ]
    srbscan.fail_if(pairs, (
        f"{len(pairs)} file(s) under src/ are near-copies of a file under lib/:\n  "
        + "\n  ".join(lines)
        + "\nThis is the retired tree relocated rather than rewritten.\n"
        f"{len(pairs)} pair(s): "
        + "; ".join(f"{a.relative_to(repo)}~{b.relative_to(repo)}"
                   for a, b, _, _ in pairs[:3])
    ))
