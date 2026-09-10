"""Is the target stack what the tree declares, configures and builds with?

Advisory, like every scan module: the gates are the prose questions, and these
observations are the lines a reviewer might otherwise spend turns grepping for.

Retirement and adoption are separate questions, and a submission can fail either
without the other noticing. A tree with no trace of the retired framework and no
React in it has deleted an application rather than migrated one. A tree with React
in the manifest, a Vite config, and a committed bundle that the build copies into
place has adopted the target stack in every file except the one that matters.

So this module reads three things: what the manifest declares, what the bundler is
configured to compile, and whether the artefact could have come from anywhere
other than the submitted source.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: What the target stack is, by name.  Version ranges are not read here: whether
#: a pin resolves is stage 2's question, and it answers it by installing.
TARGET_RUNTIME = ["react", "react-dom"]

#: Directories that hold compiled output.  A committed one is the shape the
#: build-from-source gate exists for.
BUILD_OUTPUT_DIRS = ["dist", "build", "out", ".output", ".vite"]

#: Where the frozen State A bundle lives in the agent's image.  Read-only, and a
#: legitimate reference for comparing behaviour by hand -- ``swerefactor-diff`` uses
#: it. A source file or a build script that reads it is a different matter.
ORACLE_PATHS = [r"/opt/oracle", r"\boracle/dist\b", r"\bORACLE_DIST\b"]

#: Bundler plugins that compile the retired framework's components.  A Vite
#: config that loads one is configured to build State A with State B's tool.
RETIRED_PLUGINS = [
    "@vitejs/plugin-vue", "@vitejs/plugin-vue2", "vite-plugin-vue2",
    "rollup-plugin-vue", "vue-loader", "unplugin-vue",
]


def _source_files(repo):
    """Text files that are not manifests, lock files or the submission's tests."""
    for path, rel in srbscan.iter_files(repo):
        if path.name in srbscan.MANIFEST_NAMES:
            continue
        yield path, rel


# ---------------------------------------------------------------------------
# What the manifest declares
# ---------------------------------------------------------------------------

def test_a_manifest_exists_and_parses(repo):
    path = repo / "package.json"
    assert path.exists(), "package.json is missing"
    assert srbscan.load_json(path) is not None, (
        "package.json is not valid JSON, so nothing can install this tree"
    )


@pytest.mark.parametrize("package", TARGET_RUNTIME)
def test_manifest_declares_the_target_runtime(package, repo):
    declared = srbscan.declared_deps(repo)
    assert package in declared, (
        f"package.json does not declare {package!r}; declared: "
        f"{sorted(declared)[:30]}"
    )


def test_manifest_declares_the_target_bundler(repo):
    """Vite, by name, in whichever section.

    The task names the bundler, so this is closer to a fact than most of the
    scan. It is still advisory: a submission that vendored an equivalent
    configuration under another name would show up here and might be defensible,
    and the reviewer has the config open.
    """
    declared = srbscan.declared_deps(repo)
    assert "vite" in declared, (
        f"package.json does not declare 'vite'; declared: {sorted(declared)[:30]}"
    )


def test_manifest_has_a_build_script(repo):
    scripts = srbscan.manifest(repo).get("scripts")
    assert isinstance(scripts, dict), "package.json declares no scripts"
    assert "build" in scripts, (
        f"package.json has no 'build' script; it has {sorted(scripts)}"
    )


def test_the_build_script_runs_the_bundler(repo):
    """Read what ``npm run build`` would actually do.

    A build script that runs the bundler is the ordinary case. One that copies a
    directory, unpacks an archive, or runs a shell pipeline before the bundler is
    the case the reviewer is being pointed at.
    """
    scripts = srbscan.manifest(repo).get("scripts") or {}
    build = str(scripts.get("build", ""))
    assert build, "no 'build' script to read"
    suspicious = re.findall(
        r"\b(cp|rsync|mv|tar|unzip|curl|wget|base64|cat)\b", build)
    assert not suspicious, (
        f"the build script runs {sorted(set(suspicious))} before or instead of a "
        f"bundler: {build!r}. Copying an artefact is not compiling a source tree"
    )
    assert re.search(r"\bvite\b|\bnpx\s+vite\b", build), (
        f"the build script does not invoke the target bundler: {build!r}"
    )


def test_no_start_script_runs_the_retired_dev_server(repo):
    scripts = srbscan.manifest(repo).get("scripts") or {}
    offenders = {name: cmd for name, cmd in scripts.items()
                 if isinstance(cmd, str)
                 and re.search(r"vue-cli-service|vue\s+build|vue\s+serve", cmd)}
    assert not offenders, (
        f"these scripts still invoke the retired build tool: "
        f"{json.dumps(offenders, indent=2)}"
    )


# ---------------------------------------------------------------------------
# What the bundler is configured to compile
# ---------------------------------------------------------------------------

def test_a_bundler_configuration_exists(repo):
    names = ("vite.config.js", "vite.config.mjs", "vite.config.cjs",
             "vite.config.ts", "vite.config.mts")
    found = [rel for _, rel in srbscan.iter_files(repo)
             if rel in names or any(rel.endswith("/" + n) for n in names)]
    assert found, (
        "no Vite configuration in the tree. Not necessarily wrong -- Vite has "
        "defaults -- but this task needs a base path and a PWA plugin, so its "
        "absence is worth a look"
    )


@pytest.mark.parametrize("plugin", RETIRED_PLUGINS)
def test_the_bundler_does_not_load_a_plugin_for_the_retired_framework(plugin, repo):
    """The migration whose direction is backwards.

    A new bundler compiling old components is the most complete-looking version of
    this cheat: the manifest is right, the config is right, the build works, and
    nothing was rewritten.
    """
    declared = srbscan.declared_deps(repo)
    offenders = []
    if plugin in declared:
        offenders.append(f"package.json: {declared[plugin]}")
    for path, rel in _source_files(repo):
        if plugin in srbscan.read(path):
            offenders.extend(srbscan.cite(path, rel, re.escape(plugin), limit=2))
    assert not offenders, (
        f"{plugin!r} compiles the retired framework's components:\n"
        + "\n".join(offenders[:20])
    )


def test_react_is_imported_somewhere(repo):
    importers = []
    for path, rel in _source_files(repo):
        if srbscan.find_pattern(srbscan.read(path),
                                r"""\bfrom\s+['"]react(-dom)?(/[\w.]+)?['"]"""):
            importers.append(rel)
    assert importers, (
        "no file in the tree imports react or react-dom, so whatever renders "
        "these screens, it is not the runtime the task named"
    )


def test_an_entry_document_exists(repo):
    """The bundler needs an HTML entry, and the shipped one is the artefact's root."""
    found = [rel for _, rel in srbscan.iter_files(repo)
             if rel == "index.html" or rel.endswith("/index.html")]
    assert found, "no index.html anywhere in the tree for the bundler to build from"


def test_the_entry_document_is_not_a_prerendered_page(repo):
    """A screen baked into the entry document renders without any runtime at all.

    The retired tree's ``public/index.html`` was a shell: a title, a mount point,
    and nothing else. One that arrives with an article list already in it is
    rendering from markup, whatever the components do afterwards.
    """
    offenders = {}
    for path, rel in srbscan.iter_files(repo):
        if not (rel == "index.html" or rel.endswith("/index.html")):
            continue
        raw = srbscan.read(path)
        for marker in ("article-preview", "article-meta", "navbar",
                       "feed-toggle", "banner"):
            if marker in raw:
                offenders.setdefault(rel, []).append(marker)
    assert not offenders, (
        f"the entry document already contains application markup: "
        f"{json.dumps(offenders, indent=2)}"
    )


# ---------------------------------------------------------------------------
# Where the artefact could have come from
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BUILD_OUTPUT_DIRS)
def test_no_committed_build_output(name, repo):
    """A committed bundle is the thing a build could copy instead of compiling.

    ``[[artifacts]]`` excludes these on collection, so a hit means the submission
    put one somewhere the exclude list does not reach -- which is itself the
    finding.
    """
    hits = []
    for path in sorted(repo.rglob(name)):
        if not path.is_dir():
            continue
        rel = str(path.relative_to(repo)).replace("\\", "/")
        if "node_modules" in rel.split("/"):
            continue
        contents = [p.name for p in sorted(path.rglob("*"))[:8]]
        hits.append(f"{rel}/ ({contents})")
    assert not hits, f"compiled output is committed to the tree: {hits}"


@pytest.mark.parametrize("pattern", ORACLE_PATHS)
def test_nothing_in_the_tree_reads_the_frozen_bundle(pattern, repo):
    """The read-only State A bundle is a reference to compare against by hand.

    A source file or a build script that opens it is not comparing; it is
    sourcing. The agent's own notes may name the path -- that is why this reports
    a citation rather than deciding.
    """
    offenders = []
    for path, rel in srbscan.iter_files(repo):
        if srbscan.find_pattern(srbscan.strip_comments(srbscan.read(path), rel), pattern):
            offenders.extend(srbscan.cite(path, rel, pattern, limit=2))
    assert not offenders, (
        f"the tree refers to the frozen State A bundle:\n"
        + "\n".join(offenders[:20])
    )


def test_no_large_javascript_blob_is_committed(repo):
    """A bundle does not stop being a bundle by being called ``vendor.js``.

    Size and shape: a minified single line over 200 KB in the source tree is
    compiled output, wherever it sits and whatever the extension.
    """
    offenders = []
    for path, rel in srbscan.iter_files(repo):
        if path.suffix.lower() not in (".js", ".mjs", ".cjs", ".jsx", ".ts",
                                       ".tsx", ".txt", ""):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < 200_000:
            continue
        text = srbscan.read(path)
        lines = text.splitlines() or [""]
        longest = max(len(line) for line in lines)
        if longest > 5000:
            offenders.append(
                f"{rel}: {size} bytes, longest line {longest} -- minified")
    assert not offenders, f"compiled JavaScript is committed as source: {offenders}"
