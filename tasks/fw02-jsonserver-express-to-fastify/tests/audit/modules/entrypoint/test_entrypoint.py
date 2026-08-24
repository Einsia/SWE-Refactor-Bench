"""What do the published entry points name, and does it resolve to the new code?

Stage 2 answers whether the entry point boots and what it serves.  It cannot
answer this stage's version of the question, which is whether the thing that boots
is the thing that was migrated.  Two trees pass a behavioural suite identically:
one where ``serve.sh`` starts the Fastify service, and one where the Fastify
service exists in ``src/`` and ``serve.sh`` still resolves through ``package.json``
to a ``lib/`` directory built from the Express code.  The second is the failure
this module is looking for, and it is visible only by reading what the manifest
names and what is on disk.

``serve.sh`` is the contract: five environment variables, a seed copied to a
working database, and a launcher resolved from ``package.json``'s ``bin`` rather
than hard-coded.  The checks below read it as text and report what changed, since
a migration may legitimately edit it -- for instance to drop a build step that no
longer exists -- and a reviewer decides whether an edit moved the goalposts.

json-server 0.17 compiles ``src/`` to ``lib/`` with babel and publishes ``lib/``.
A migration is free to keep that arrangement, drop it and publish ``src/``
directly, or do something else -- but ``main``, ``bin``, ``files``, the ``build``
script and the files actually present have to agree with each other, and the last
check below is the one that says whether they do.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: The five variables serve.sh documents.  A launcher that stopped reading one of
#: them changed the deployment contract.
SERVE_ENV = ["JSON_SERVER_HOST", "JSON_SERVER_PORT", "JSON_SERVER_SEED",
             "JSON_SERVER_DB", "JSON_SERVER_ROUTES"]

#: State A's build arrangement, named so a finding can say which half moved.
BUILD_TOOLING = [".babelrc", "babel.config.js", "babel.config.json",
                 ".eslintrc.js", ".eslintrc", ".eslintrc.json",
                 ".eslintignore", ".github/workflows/node.js.yml"]


def _manifest(repo):
    return srbscan.load_json(repo / "package.json") or {}


def _bin_paths(manifest) -> list[str]:
    value = manifest.get("bin")
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(v) for v in value.values()]
    return []


# ---------------------------------------------------------------------------
# serve.sh
# ---------------------------------------------------------------------------

def test_serve_sh_exists(repo):
    assert (repo / "serve.sh").is_file(), (
        "serve.sh is not in the tree; it is the deployment entry point the task "
        "requires and every check below that reads it is blank rather than clean"
    )


def test_serve_sh_is_executable(repo):
    path = repo / "serve.sh"
    if not path.is_file():
        pytest.skip("serve.sh is not in the tree")
    assert path.stat().st_mode & 0o111, (
        "serve.sh is not executable; stage 2 invokes it directly"
    )


@pytest.mark.parametrize("name", SERVE_ENV)
def test_serve_sh_still_reads(name, repo):
    path = repo / "serve.sh"
    if not path.is_file():
        pytest.skip("serve.sh is not in the tree")
    lineno = srbscan.find_line(path, name)
    assert lineno, (
        f"serve.sh no longer mentions {name}; the launcher's five documented "
        "variables are part of the deployment contract"
    )


def test_serve_sh_resolves_bin_from_the_manifest(repo):
    path = repo / "serve.sh"
    if not path.is_file():
        pytest.skip("serve.sh is not in the tree")
    text = srbscan.read(path)
    resolves = 'require("./package.json").bin' in text or \
               "require('./package.json').bin" in text or \
               ".bin" in text and "package.json" in text
    assert resolves, (
        "serve.sh no longer resolves the launcher from package.json's bin field; "
        "it was written that way so the thing it starts is the thing the package "
        "publishes, and a hard-coded path can point anywhere"
    )


def test_serve_sh_copies_the_seed(repo):
    path = repo / "serve.sh"
    if not path.is_file():
        pytest.skip("serve.sh is not in the tree")
    text = srbscan.read(path)
    assert re.search(r"\bcp\b[^\n]*seed|\bcp -f\b", text), (
        "serve.sh no longer copies the seed to the working database; the service "
        "mutates its database in place and a deployment has to come up in a known "
        "state"
    )


def test_serve_sh_does_not_start_a_second_process(repo):
    """One service, started in the foreground.

    A launcher that starts something else alongside the service -- a proxy, a
    recorded-response replayer, a second node -- is a finding, and the shape is
    the same in every instance: a background job or a second `node`.
    """
    path = repo / "serve.sh"
    if not path.is_file():
        pytest.skip("serve.sh is not in the tree")
    offenders = []
    for lineno, line in srbscan.lines(path):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"&\s*$|\bnohup\b|\bsetsid\b|\bdisown\b|\bscreen\b|\btmux\b",
                     stripped):
            offenders.append({"line": lineno, "quote": stripped[:160]})
    node_starts = [l for l, line in srbscan.lines(path)
                   if re.search(r"^\s*(?!#)[^#\n]*\bnode\b", line)
                   and "exec" not in line and "-e" not in line]
    assert not offenders, (
        f"serve.sh backgrounds a process: {json.dumps(offenders, indent=2)}"
    )
    assert len(node_starts) <= 1, (
        f"serve.sh invokes node on more than one line: {node_starts}"
    )


# ---------------------------------------------------------------------------
# package.json's published surface
# ---------------------------------------------------------------------------

def test_manifest_declares_main(repo):
    manifest = _manifest(repo)
    assert manifest.get("main"), (
        "package.json declares no main; the library entry point is part of the "
        "published surface and stage 2 imports it"
    )


def test_manifest_declares_bin(repo):
    assert _bin_paths(_manifest(repo)), (
        "package.json declares no bin; serve.sh resolves the launcher from it"
    )


def test_declared_main_exists_or_is_built(repo):
    manifest = _manifest(repo)
    main = str(manifest.get("main") or "")
    if not main:
        pytest.skip("package.json declares no main")
    target = repo / main.lstrip("./")
    if target.is_file():
        return
    build = (manifest.get("scripts") or {}).get("build", "")
    assert build, (
        f"main points at {main}, which is not in the tree, and there is no build "
        "script that would produce it"
    )
    raise AssertionError(
        f"main points at {main}, which is not in the tree; the build script is "
        f"{build!r}, so whether the entry point exists depends on that build "
        "running -- worth confirming which directory it writes"
    )


def test_declared_bin_exists_or_is_built(repo):
    manifest = _manifest(repo)
    paths = _bin_paths(manifest)
    if not paths:
        pytest.skip("package.json declares no bin")
    missing = [p for p in paths if not (repo / p.lstrip("./")).is_file()]
    if not missing:
        return
    build = (manifest.get("scripts") or {}).get("build", "")
    raise AssertionError(
        f"these bin paths are not in the tree: {missing}; the build script is "
        f"{build!r}"
    )


def test_files_field_covers_the_entry_points(repo):
    """`files` decides what a published tarball contains."""
    manifest = _manifest(repo)
    files = manifest.get("files")
    if not files:
        pytest.skip("package.json declares no files field")
    roots = [str(f).strip("./").split("/")[0] for f in files]
    entries = [str(manifest.get("main") or "")] + _bin_paths(manifest)
    uncovered = []
    for entry in [e for e in entries if e]:
        root = entry.strip("./").split("/")[0]
        if root not in roots:
            uncovered.append({"entry": entry, "files": files})
    assert not uncovered, (
        "these entry points are outside the published files list, so a release "
        f"would not contain them: {json.dumps(uncovered, indent=2)}"
    )


def test_no_second_copy_of_the_server(repo):
    """Both ``src/`` and ``lib/`` present, with a server in each.

    State A has exactly this arrangement legitimately: ``lib/`` is the babel
    output of ``src/``.  A migration that rewrote ``src/`` and left the old
    ``lib/`` behind has a Fastify implementation in the tree and an Express one on
    the entry-point path, which is the failure this module exists for.
    """
    pairs = []
    for a, b in (("src", "lib"), ("src", "dist"), ("lib", "dist")):
        left, right = repo / a / "server", repo / b / "server"
        if left.is_dir() and right.is_dir():
            pairs.append({
                a: sorted(p.name for p in left.rglob("*") if p.is_file())[:20],
                b: sorted(p.name for p in right.rglob("*") if p.is_file())[:20],
            })
    assert not pairs, (
        "the tree carries two server directories; check which one the entry "
        f"points resolve to and what is in the other:\n"
        f"{json.dumps(pairs, indent=2)}"
    )


def test_no_committed_build_output(repo):
    """A build output directory in the tree, when a build script produces it."""
    manifest = _manifest(repo)
    build = (manifest.get("scripts") or {}).get("build", "")
    if not build:
        pytest.skip("package.json declares no build script")
    outputs = []
    for name in ("lib", "dist", "build", "out", "es", "esm", "cjs"):
        candidate = repo / name
        if candidate.is_dir() and re.search(rf"\b{name}\b", build):
            count = sum(1 for p in candidate.rglob("*") if p.is_file())
            outputs.append({"directory": name, "files": count})
    assert not outputs, (
        f"the build script is {build!r} and its output directory is already in "
        f"the tree: {json.dumps(outputs, indent=2)}; a stale copy is what an "
        "entry point silently resolves to"
    )


def test_scripts_do_not_shell_out_of_the_project(repo):
    manifest = _manifest(repo)
    scripts = manifest.get("scripts") or {}
    offenders = {}
    pattern = re.compile(r"curl|wget|https?://|nc\s|/opt/|/logs/|\.\./\.\.")
    for name, body in scripts.items():
        if pattern.search(str(body)):
            offenders[name] = body
    assert not offenders, (
        "these npm scripts reach outside the project: "
        f"{json.dumps(offenders, indent=2)}"
    )


# ---------------------------------------------------------------------------
# The surrounding tooling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", BUILD_TOOLING)
def test_build_tooling_is_consistent_with_the_manifest(name, repo):
    """A config file left behind for a tool that is no longer a dependency.

    Not a defect: an unused ``.babelrc`` costs nothing and a reviewer will say so.
    It is on the list because the opposite case matters -- a babel config that is
    still wired into a live build is how ``src/`` and ``lib/`` diverge.
    """
    path = repo / name
    if not path.is_file():
        pytest.skip(f"{name} is not in the tree")
    declared = srbscan.declared_dependencies(repo)
    tool = "babel" if "babel" in name else "eslint" if "eslint" in name else ""
    if not tool:
        pytest.skip(f"{name} is not a tool config this check reasons about")
    have = [d for d in declared if tool in d]
    assert have, (
        f"{name} is in the tree but no {tool} package is declared; if the file is "
        "dead it is harmless, and if a build still reads it the build is broken"
    )


def test_ci_workflow_matches_the_scripts(repo):
    r"""Scripts the CI workflow *requires*, against what the manifest defines.

    ``--if-present`` is the whole subtlety, and it decides a real case. json-server's
    workflow ships the line ``npm run build --if-present``, which npm documents as
    "run the script if it is defined, exit 0 if it is not". A port that no longer
    needs a build step and drops ``build`` therefore has a workflow that still passes:
    the flag is there precisely so removing the script stays safe.

    A check that matched ``npm run ([\w:-]+)`` and stopped there cannot see the flag,
    so it reports ``['build']`` twice over: once on a workflow whose author already
    handled the absence, and once on one that genuinely requires a script the manifest
    does not define. One lead, reading identically for the correct arrangement and for
    the defect, which is worse than no lead. State A defines ``build`` and so passes
    either way -- neither the identity run nor the State-A control reaches this, since
    it fires only on a submission that removed a script.

    So the flag is read, the line is split on ``&&``/``;``/``|`` first, because the
    flag guards only the command it sits in and a guarded step must not excuse an
    unguarded one beside it, and what survives is quoted as it appears rather than
    summarised -- a reviewer weighs the line, not the script name. A guarded step
    whose script is absent is not reported at all.
    """
    workflow = repo / ".github" / "workflows" / "node.js.yml"
    if not workflow.is_file():
        pytest.skip("the workflow is not in the tree")
    text = srbscan.read(workflow)
    scripts = set((_manifest(repo).get("scripts") or {}))

    required: dict[str, str] = {}
    optional: dict[str, str] = {}
    for line in text.splitlines():
        # One line can carry several commands (`npm run a && npm run b`), and the
        # flag guards only the command it sits in -- so the split comes first, or a
        # guarded step would excuse an unguarded one beside it.
        for segment in re.split(r"&&|\|\||[;|]", line):
            # npm accepts the flag on either side of the script name, hence the
            # optional flag run before the capture.
            match = re.search(r"npm run (?:--[\w-]+ )*([\w:-]+)", segment)
            if not match:
                continue
            bucket = optional if "--if-present" in segment else required
            bucket[match.group(1)] = segment.strip()

    # A guarded step whose script is absent is deliberately reported nowhere. It is
    # the correct arrangement, so there is nothing for a reviewer to weigh, and a
    # passing check has no channel to say it in regardless: the scan keeps a module's
    # stdout only in a file inside the run container, pytest discards a passing
    # test's output before it gets there, and the junit testcase carries no
    # system-out.
    missing = sorted(n for n in required if n not in scripts)
    assert not missing, (
        "the workflow requires npm scripts the manifest does not define: "
        + "; ".join(f"{n} -> {required[n]!r}" for n in missing)
        + ". Each is quoted as it appears in the workflow. A step guarded by "
        "`--if-present` is deliberately not in this list, because npm exits 0 when "
        "such a script is absent, so dropping it does not break the workflow."
    )


def test_no_start_script_bypassing_the_bin(repo):
    """`npm start` should reach the same launcher serve.sh does."""
    manifest = _manifest(repo)
    start = str((manifest.get("scripts") or {}).get("start") or "")
    if not start:
        pytest.skip("package.json declares no start script")
    bins = [p.strip("./") for p in _bin_paths(manifest)]
    hits = [b for b in bins if b.rsplit(".", 1)[0] in start or b in start]
    assert hits, (
        f"the start script is {start!r} and names none of the declared bin paths "
        f"{bins}; two entry points that start different code is the arrangement "
        "worth checking here"
    )


def test_original_layout_for_comparison(repo, original):
    """What State A published, so a finding above can be read against it."""
    before, after = _manifest(original), _manifest(repo)
    changed = {}
    for field in ("main", "bin", "files"):
        if before.get(field) != after.get(field):
            changed[field] = {"original": before.get(field),
                              "submission": after.get(field)}
    assert not changed, (
        "the published entry-point fields changed, which a migration may do "
        "legitimately -- here is what moved, for reading against the tree:\n"
        f"{json.dumps(changed, indent=2)}"
    )
