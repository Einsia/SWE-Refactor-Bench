"""Has the retired framework left the tree, and left what the tree declares?

Advisory.  Nothing in this file can fail the audit gate: the gate is the five
prose questions in evaluation.toml, and this module's output reaches the reviewer
as findings to check rather than as verdicts to inherit.

That distinction matters more here than anywhere else in the suite, because this
framework's entire surface is text.  ``v-if`` is a live directive in one file and
a sentence in a migration note in another; ``<template>`` is a single-file
component in one file and a native HTML element that React also uses in another;
``vue`` is a dependency in one line of a lock file and a substring of
``vuex-persist`` in the next.  A rule that fails on the token punishes all of
those equally.  Reporting the token, to a reader who can open the file at the line
named, costs the innocent cases nothing.

The scan that scored on these tokens failed a correct port for the word "golden"
in a comment and measured it at 0.0.  It also passed three submissions that had
migrated nothing, because every rule it had was satisfiable by renaming.  Both
halves of that are the argument for this file being advisory and for the gate
being a question.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: Packages whose presence anywhere in the dependency graph means the retired
#: stack is still reachable.  A stale lock file counts: an install would bring the
#: framework straight back even if the manifest no longer names it.
RETIRED_PACKAGES = [
    "vue", "vue-router", "vuex", "vue-template-compiler", "vue-axios",
    "vue-loader", "vue-style-loader", "vue-hot-reload-api", "vue-jest",
    "vue-eslint-parser", "eslint-plugin-vue", "@vue/cli-service",
    "@vue/cli-plugin-babel", "@vue/cli-plugin-eslint", "@vue/cli-plugin-pwa",
    "@vue/cli-plugin-unit-jest", "@vue/test-utils", "@vue/eslint-config-prettier",
    "@vue/babel-preset-app", "@vue/component-compiler-utils", "@vue/runtime-core",
    "@vue/runtime-dom", "@vue/compiler-sfc", "@vue/reactivity", "@vue/shared",
]

#: Compatibility shims and alternative renderers.  Each would let the retired
#: components, or a runtime shaped like them, survive behind an adapter -- which
#: is a migration in the manifest and not in the code.
BRIDGE_PACKAGES = [
    "@vue/compat", "vuera", "veaury", "vue-next", "petite-vue", "alpinejs",
    "preact", "preact-compat", "@preact/compat", "vue2-teleport", "vue-demi",
    "nervjs", "anujs", "rax", "inferno", "inferno-compat",
]

#: Unambiguous markers of the retired runtime in source.  Directives are matched
#: with a trailing delimiter so ordinary prose cannot trip them, and every one of
#: these is matched against comment-stripped text.
SOURCE_PATTERNS = [
    (r"""\bfrom\s+['"]vue['"]""", "imports the retired runtime"),
    (r"""\brequire\(\s*['"]vue['"]\s*\)""", "requires the retired runtime"),
    (r"""\bfrom\s+['"]vue-(router|axios)['"]""", "imports a companion package"),
    (r"""\bfrom\s+['"]vuex['"]""", "imports the retired state library"),
    (r"""\bfrom\s+['"]@vue/""", "imports a @vue/* package"),
    (r"\bnew\s+Vue\s*\(", "instantiates a root of the retired runtime"),
    (r"\bVue\s*\.\s*(use|component|filter|directive|mixin|config|nextTick)\b",
     "calls a global API of the retired runtime"),
    (r"\bcreateApp\s*\(", "instantiates a Vue 3 application"),
    (r"\bdefineComponent\s*\(", "declares a component of the retired runtime"),
    (r"\bmap(Getters|State|Actions|Mutations)\s*\(",
     "uses a binding helper from the retired state library"),
    (r"\bthis\s*\.\s*\$(store|router|route|emit|refs|nextTick|set|delete)\b",
     "uses an instance property of the retired runtime"),
    (r"\bv-(if|else-if|else|for|model|show|html|text|once|cloak|pre)\b[=\s>]",
     "carries a template directive"),
    (r"\bv-bind:|\bv-on:", "carries a binding shorthand"),
    (r"<(router-link|router-view|keep-alive|transition-group)\b",
     "renders a built-in element of the retired runtime"),
    (r"""['"]vue-template-compiler['"]""", "references the retired SFC compiler"),
]

#: Configuration files that only the retired build tool reads.
RETIRED_CONFIG_NAMES = [
    "vue.config.js", "vue.config.mjs", "vue.config.cjs", "vue.config.ts",
]

#: Hand-written reimplementation.  Retiring a framework by rewriting it is not
#: retiring it, and it is the one shape in this file that no dependency check can
#: see.  All three markers have honest uses, which is why they are reported.
REIMPLEMENTATION_PATTERNS = [
    (r"\bObject\s*\.\s*defineProperty\s*\([^)]{0,120}\bget\s*[:(]",
     "defines a reactive property with a getter, as a reactivity system does"),
    (r"\b(compileToFunctions|parseTemplate|compileTemplate|genElement|"
     r"resolveDirective|patchVnode|createPatchFunction)\b",
     "names a template-compiler or virtual-DOM internal"),
    (r"\b(mergeOptions|initState|initLifecycle|callHook|normalizeChildren)\s*\(",
     "names an options-object component runtime internal"),
]


# ---------------------------------------------------------------------------
# What the tree declares
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("package", RETIRED_PACKAGES)
def test_manifest_does_not_declare_the_retired_stack(package, repo):
    declared = srbscan.declared_deps(repo)
    where = declared.get(package)
    assert where is None, (
        f"package.json still declares the retired package {package!r} -- {where}"
    )


@pytest.mark.parametrize("package", BRIDGE_PACKAGES)
def test_manifest_does_not_declare_a_bridge(package, repo):
    """A shim or an alternative renderer, declared.

    Not automatically a finding: ``preact`` in a submission that uses it as its
    only renderer is a different migration from the one asked for, and
    ``@vue/compat`` is the retired framework wearing a hat. The reviewer decides
    which one this is.
    """
    declared = srbscan.declared_deps(repo)
    where = declared.get(package)
    assert where is None, (
        f"package.json declares {package!r} -- {where}. A compatibility shim "
        f"keeps the retired components running; a second renderer replaces the "
        f"one the task named. Read which"
    )


def test_no_lockfile_resolves_the_retired_stack(repo):
    """The lock file is the dependency graph an install would actually build."""
    offenders: dict[str, list[str]] = {}
    for path, rel in srbscan.lockfiles(repo):
        text = srbscan.read(path)
        for package in RETIRED_PACKAGES + BRIDGE_PACKAGES:
            hit = srbscan.resolves_in_lockfile(text, package)
            if hit:
                offenders.setdefault(rel, []).append(f"{package}: {hit}")
    assert not offenders, (
        f"a lock file still resolves the retired stack, so an install would "
        f"bring it back: {json.dumps(offenders, indent=2)[:3000]}"
    )


def test_only_one_lockfile_kind_survived(repo):
    """The retired build used yarn; the target uses npm.

    Two lock files is not a framework finding, but it is how a tree ends up
    installable two ways with two different dependency graphs, and only one of
    them is the one the reviewer read.
    """
    found = sorted(rel for _, rel in srbscan.lockfiles(repo))
    assert len(found) <= 1, f"the tree carries several lock files: {found}"


@pytest.mark.parametrize("name", RETIRED_CONFIG_NAMES)
def test_no_configuration_for_the_retired_build_tool(name, repo):
    hits = [rel for _, rel in srbscan.iter_files(repo)
            if rel == name or rel.endswith("/" + name)]
    assert not hits, (
        f"{hits} is configuration only the retired build tool reads; a build "
        f"that still consults it has not moved"
    )


# ---------------------------------------------------------------------------
# What the source holds
# ---------------------------------------------------------------------------

def test_no_single_file_components_by_extension(repo):
    hits = [rel for _, rel in srbscan.iter_files(repo)
            if rel.lower().endswith(".vue")]
    assert not hits, f"single-file components are still in the tree: {sorted(hits)}"


def test_no_single_file_components_by_shape(repo):
    """The same question asked of the contents rather than the name.

    This is the check that catches the rename. An adversary moved 26 components
    to another extension, shipped a loader that compiled them at build time, and
    passed every extension-based rule in the suite.
    """
    offenders: dict[str, str] = {}
    for path, rel in srbscan.iter_files(repo):
        if rel.lower().endswith(".vue"):
            continue  # counted by the check above
        if path.name in srbscan.MANIFEST_NAMES:
            continue
        shape = srbscan.sfc_shape(srbscan.read(path))
        if shape:
            offenders[rel] = shape
    assert not offenders, (
        f"these files have the shape of a single-file component whatever they "
        f"are called: {json.dumps(offenders, indent=2)[:3000]}"
    )


@pytest.mark.parametrize("pattern,why", SOURCE_PATTERNS,
                         ids=[p[:38] for p, _ in SOURCE_PATTERNS])
def test_source_does_not_use_the_retired_runtime(pattern, why, repo):
    offenders: list[str] = []
    for path, rel in srbscan.iter_files(repo):
        if path.name in srbscan.MANIFEST_NAMES:
            continue  # a dependency name is the manifest checks' business
        snippet = srbscan.find_pattern(srbscan.strip_comments(srbscan.read(path), rel),
                                       pattern)
        if snippet:
            offenders.extend(srbscan.cite(path, rel, pattern, limit=2)
                             or [f"{rel}: {snippet!r}"])
    assert not offenders, (
        f"source that {why}:\n" + "\n".join(sorted(offenders)[:40])
    )


@pytest.mark.parametrize("pattern,why", REIMPLEMENTATION_PATTERNS,
                         ids=["reactivity", "compiler", "options-runtime"])
def test_no_hand_written_reimplementation(pattern, why, repo):
    """A framework rewritten by hand is still a second framework.

    Weak on purpose, and reported rather than scored: ``Object.defineProperty``
    with a getter is also how you write a lazily-computed field, and a function
    called ``patchVnode`` in a submission that wrote its own renderer is the
    finding while the same name in a comment is not.
    """
    offenders: list[str] = []
    for path, rel in srbscan.iter_files(repo):
        if path.name in srbscan.MANIFEST_NAMES:
            continue
        if srbscan.find_pattern(srbscan.strip_comments(srbscan.read(path), rel), pattern):
            offenders.extend(srbscan.cite(path, rel, pattern, limit=2))
    assert not offenders, (
        f"source that {why}:\n" + "\n".join(sorted(offenders)[:40]) +
        "\n(each of these has an honest use; the question is what the "
        "surrounding code does with it)"
    )


def test_no_path_is_named_after_the_retired_stack(repo):
    """A lead about vendoring, and sometimes just about naming.

    ``src/components/vue-ish-button/`` in a submission that rewrote the component
    and kept the folder name shows up here and is fine. ``vendor/vue/dist/`` does
    not.
    """
    offenders = []
    for path in sorted(repo.rglob("*")):
        rel = str(path.relative_to(repo)).replace("\\", "/")
        if any(part in srbscan.SKIP_DIRS for part in rel.split("/")):
            continue
        if re.search(r"(^|[^a-z])vue([^a-z]|$)|vuex", path.name, re.IGNORECASE):
            offenders.append(rel)
    assert not offenders, (
        f"these paths are named after the retired stack: {sorted(offenders)[:40]}. "
        f"A vendored copy and a kept directory name both look like this"
    )


def _suite_size(tree):
    """``(files, lines)`` for whatever looks like a test suite in ``tree``."""
    rels = [rel for _, rel in srbscan.iter_files(tree)
            if srbscan.TEST_PATH_RE.search(rel)]
    lines = sum(len(srbscan.read(tree / rel).splitlines()) for rel in rels)
    return rels, lines


def test_the_projects_own_test_suite_survived(repo, original):
    """The retired tree had a unit suite; a migration ports it.

    Measured against the original rather than against a number written here, so
    the check says what it means -- "a quarter of what was there" -- instead of
    encoding the original's size in a constant that goes stale the moment the
    fixture tree changes.

    Advisory in both directions. A submission that rewrote the tests onto the
    target's runner passes; one that deleted them to make the port look finished
    shows up here; and a submission that rewrote them more concisely shows up here
    too and is fine. The reviewer is being told whether the tests moved with the
    code.
    """
    ported, ported_lines = _suite_size(repo)
    was, was_lines = _suite_size(original)
    assert ported, (
        f"no test files survived the migration; the retired tree had "
        f"{len(was)} across {was_lines} lines: {sorted(was)[:10]}"
    )
    floor = was_lines // 4
    assert ported_lines >= floor, (
        f"the surviving suite is {ported_lines} lines across {len(ported)} files, "
        f"against {was_lines} across {len(was)} before the migration. Below a "
        f"quarter of the original ({floor} lines) it looks emptied rather than "
        f"ported: {sorted(ported)[:20]}"
    )
