#!/usr/bin/env python3
"""Publish a submission's two programs into an install prefix.

run-candidate.sh calls this for the C# side of a comparison; `make` handles the
C++ side.  Both end at the same interface: `<prefix>/bin/jsonnet` and
`<prefix>/bin/jsonnetfmt`, executable, taking the same argv.

This is a second implementation of a rule stage 2 also implements -- in
`tests/behavioural/lib/build.py`, whose `build` module runs first and whose output
every graded case needs, so any tree that reaches stage 3 has already published
there (reaching stage 3 needs every scored behavioural check, and a tree that
does not publish passes none).  The duplication is deliberate and the direction
of authority matters: neither copy is the rule.  The
rule is in `environment/` as part of the published contract --

    Each command-line program must be produced by an executable project
    (OutputType Exe or WinExe) whose assembly name is exactly `jsonnet` or
    `jsonnetfmt` -- <AssemblyName> when set, otherwise the .csproj filename.

-- and both copies implement it.  Sharing one file across two stage images would
mean a stage-3 image that cannot be built until stage 2's exists, which the schema
does not have and lang01's stage 3 does not need.  What guards the two against
drifting is that this file has a self-check the image build runs (`--self-check`),
and that a candidate upheld here has to be reproducible in stage 2's suite
afterwards -- a divergence in how the programs are found would show up as an
upheld candidate nobody can reproduce.

Five properties are forced off on the command line, overriding whatever the
project sets: PublishSingleFile, PublishAot, PublishTrimmed, PublishReadyToRun,
SelfContained.  Stage 2 forces them because a native-AOT image has no CLR metadata
for its assembly checks to read.  Here the reason is narrower and still real: the
two halves of a comparison must differ in the port, not in the packaging, and
`--multi` writing beside a single-file bundle's extraction directory is a
difference in packaging.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

CLI_NAMES = ("jsonnet", "jsonnetfmt")

#: Same closed feed stage 2 publishes against, for the same reason: this stage
#: must build the submission the way grading did, or a divergence it finds could
#: be an artefact of a different dependency set rather than of the port.  See the
#: note in tests/behavioural/lib/build.py -- the rule lives in `environment/`, and
#: neither copy of it here is the authority.
NUGET_CONFIG = Path("/opt/nuget.config")

#: Removed from the tree before anything runs.  The tree is already a copy -- the
#: harness copies both trees before the first candidate -- so the cost of being
#: wrong about one of these is a rebuild, not a lost file.  A submission that
#: ships a prebuilt assembly and no source that produces it must fail, and it
#: cannot be allowed to satisfy the publish step with an artefact this container
#: never compiled.
DISCARD_DIRS = ("bin", "obj", "publish", ".vs", "artifacts", ".git",
                "__pycache__", ".idea", "TestResults")

FORCED_OFF = ("PublishSingleFile", "PublishAot", "PublishTrimmed",
              "PublishReadyToRun", "SelfContained")

PUBLISH_TIMEOUT = 1800.0


@dataclass
class Project:
    assembly: str
    rel_path: str
    is_exe: bool


def produced_name(repo: Path, rel: str) -> tuple[str, bool]:
    """(lowercased assembly name, produces an executable) for one .csproj.

    A .NET project produces <AssemblyName> when it sets one and its own filename
    stem otherwise.  Deliberately a regex rather than an XML parse: a project file
    that does not parse as strict XML still builds if MSBuild accepts it, and this
    must not be stricter than the tool it is describing.
    """
    stem = os.path.splitext(os.path.basename(rel))[0]
    name, is_exe = stem, False
    try:
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return name.lower(), is_exe
    # The last <AssemblyName> wins, which is MSBuild's own rule for a property set
    # more than once in one file.
    last = None
    for last in re.finditer(r"<AssemblyName\s*>([^<]*)</AssemblyName\s*>", text,
                            re.IGNORECASE):
        pass
    if last and last.group(1).strip():
        name = last.group(1).strip()
    if re.search(r"<OutputType\s*>\s*(Exe|WinExe)\s*</OutputType\s*>", text,
                 re.IGNORECASE):
        is_exe = True
    return name.lower(), is_exe


def _is_build_output(rel: str) -> bool:
    parts = Path(rel).parts
    return any(p in DISCARD_DIRS for p in parts[:-1])


def find_cli_projects(repo: Path) -> dict[str, Project]:
    """The projects producing `jsonnet` and `jsonnetfmt`, keyed by assembly name.

    An executable-producing project beats a library of the same name, and an
    earlier path beats a later one, so the answer does not depend on walk order.
    """
    exes: dict[str, Project] = {}
    libs: dict[str, Project] = {}
    for path in sorted(repo.rglob("*.csproj")):
        rel = str(path.relative_to(repo))
        if _is_build_output(rel):
            continue
        name, is_exe = produced_name(repo, rel)
        if name not in CLI_NAMES:
            continue
        (exes if is_exe else libs).setdefault(
            name, Project(assembly=name, rel_path=rel, is_exe=is_exe))
    out = dict(libs)
    out.update(exes)
    return out


def _run(argv: list[str], cwd: Path, label: str) -> None:
    print(f"--- {label}: {' '.join(argv)}", flush=True)
    env = dict(os.environ)
    env.update({
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_NOLOGO": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_CLI_UI_LANGUAGE": "en",
        "LC_ALL": "C",
        "TZ": "UTC",
    })
    proc = subprocess.run(argv, cwd=str(cwd), env=env, timeout=PUBLISH_TIMEOUT,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    sys.stdout.write(proc.stdout.decode("utf-8", "replace"))
    sys.stdout.flush()
    if proc.returncode != 0:
        raise SystemExit(f"{label} failed with exit {proc.returncode}")


def _discard_build_output(repo: Path) -> None:
    for name in DISCARD_DIRS:
        for stale in list(repo.rglob(name)):
            if stale.is_dir():
                shutil.rmtree(stale, ignore_errors=True)


def _install(publish_dir: Path, assembly: str, prefix: Path) -> None:
    """Put something executable at <prefix>/bin/<assembly>.

    The apphost is preferred, because that is what a user runs and because it
    makes both halves of a comparison a native executable invoked by path.  When a
    project sets UseAppHost=false there is no apphost -- a legitimate shape, and
    refusing it would fail a submission over a property no case can observe -- so
    a two-line shim runs the dll instead.

    The shim is visible to a candidate that goes looking, and so is the publish
    directory beside it.  That is not what stops a candidate from identifying the
    tree it is on: `probe.toml`'s scope denies identity "however it is learned",
    and the adjudicator rejects such a candidate on sight.  Hiding the artefacts
    would be a weaker guarantee dressed up as a stronger one.
    """
    bindir = prefix / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / assembly

    apphost = publish_dir / assembly
    if apphost.is_file():
        apphost.chmod(0o755)
        target.symlink_to(apphost)
        return
    dll = publish_dir / f"{assembly}.dll"
    if not dll.is_file():
        listing = sorted(p.name for p in publish_dir.iterdir())[:20]
        raise SystemExit(
            f"publish of {assembly} produced neither {assembly} nor "
            f"{assembly}.dll in {publish_dir}; it holds {listing}")
    target.write_text(
        "#!/bin/sh\n"
        f'exec dotnet "{dll}" "$@"\n')
    target.chmod(0o755)


def _restore(repo: Path, projects: dict[str, Project]) -> None:
    """Restore offline, without assuming a solution file exists.

    `dotnet restore` with no target restores "the project or solution in the
    current directory", and fails outright when the directory holds neither.  A
    tree whose projects live under `src/<Name>/<Name>.csproj` with no `.sln` at
    the root is an ordinary .NET layout, and nothing asks for a solution file:
    the contract constrains the assembly each CLI project produces and leaves
    layout alone, and instruction.md says outright that none is required.
    Restoring the root would have failed every such submission at the first step
    of the build.

    So restore the two discovered projects, and only those.  A project restore
    walks its own ProjectReference graph, which is exactly the closure that
    publishing it needs.  Restoring a root solution instead would be wider than
    the job: it pulls in every project the solution lists, including the test
    project the instructions explicitly invite, and one of those failing to
    restore would fail a build whose two programs were fine.

    Iterates the argument rather than CLI_NAMES so the function is total over
    what it is handed.  Today the caller has already refused a tree missing
    either program, so the two are the same set -- but that is call ordering, not
    construction, and a KeyError here would surface as an unexplained crash in a
    stage whose whole job is to attribute failures precisely.
    """
    if not NUGET_CONFIG.is_file():
        raise SystemExit(
            f"{NUGET_CONFIG} is missing: it is what keeps the offline feed a "
            f"closed set.  The image is built wrong.")
    for name in sorted(projects):
        _run(["dotnet", "restore", projects[name].rel_path, "--nologo",
              "--configfile", str(NUGET_CONFIG)], repo, f"restore {name}")


def publish_repo(repo: Path, prefix: Path, state: Path) -> None:
    if not repo.is_dir():
        raise SystemExit(f"no tree at {repo}")
    _discard_build_output(repo)

    projects = find_cli_projects(repo)
    missing = [n for n in CLI_NAMES if n not in projects]
    if missing:
        found = sorted({produced_name(repo, str(p.relative_to(repo)))[0]
                        for p in repo.rglob("*.csproj")
                        if not _is_build_output(str(p.relative_to(repo)))})
        raise SystemExit(
            f"no executable project produces {missing}; the assembly names this "
            f"tree's project files produce are {found}")
    not_exe = [p.rel_path for p in projects.values() if not p.is_exe]
    if not_exe:
        raise SystemExit(
            f"{not_exe} produce the right assembly names but are not executable "
            f"projects: the contract asks for OutputType Exe or WinExe")

    _restore(repo, projects)
    for name in CLI_NAMES:
        project = projects[name]
        out_dir = state / "publish" / name
        argv = ["dotnet", "publish", project.rel_path, "-c", "Release",
                "-o", str(out_dir), "--nologo", "--no-restore"]
        argv += [f"-p:{prop}=false" for prop in FORCED_OFF]
        _run(argv, repo, f"publish {name}")
        _install(out_dir, name, prefix)
    print(f"installed {list(CLI_NAMES)} into {prefix}/bin", flush=True)


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #

_PROG = {
    "jsonnet": 'System.Console.Out.Write("A");',
    "jsonnetfmt": 'System.Console.Out.Write("B");',
}

_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>{assembly}</AssemblyName>
    <RootNamespace>Whatever.{assembly}</RootNamespace>
    <Nullable>enable</Nullable>
    <InvariantGlobalization>true</InvariantGlobalization>
  </PropertyGroup>
</Project>
"""

_LIB_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>jsonnet</AssemblyName>
  </PropertyGroup>
</Project>
"""


def self_check() -> int:
    """Prove discovery and publish work in this image, against a tree that is
    shaped like an idiomatic port rather than like the C++ original.

    The synthetic tree is verification in the two ways a real one will be: the
    project files are named nothing like the programs they produce
    (`Sharpnet.Tool.csproj` -> `jsonnet`), and there is a *library* also producing
    the name `jsonnet`, which must lose to the executable.  A discovery function
    that keyed on filenames would pass a check built from a tree that looks like
    the reference and fail every real submission -- so the check is built from a
    tree that does not.
    """
    root = Path(tempfile.mkdtemp(prefix="publish-selfcheck-"))
    try:
        tree = root / "tree"
        for assembly, body in _PROG.items():
            # Directory and file names chosen to share no substring with the
            # assembly name they produce.
            d = tree / "src" / f"Sharpnet.{assembly.title().replace('jsonnet','X')}"
            d.mkdir(parents=True)
            (d / f"Sharpnet.{assembly[-3:].title()}Tool.csproj").write_text(
                _CSPROJ.format(assembly=assembly))
            (d / "Program.cs").write_text(body + "\n")
        lib = tree / "src" / "Sharpnet.Core"
        lib.mkdir(parents=True)
        (lib / "Sharpnet.Core.csproj").write_text(_LIB_CSPROJ)
        (lib / "Placeholder.cs").write_text("namespace Sharpnet.Core;\n"
                                            "public static class P { }\n")
        # Build output that must be ignored by discovery and removed before the
        # publish: a stale project file under obj/ producing the same names.
        stale = tree / "src" / "Sharpnet.Stale" / "obj"
        stale.mkdir(parents=True)
        (stale / "jsonnet.csproj").write_text(_CSPROJ.format(assembly="jsonnet"))

        found = find_cli_projects(tree)
        problems = []
        for name in CLI_NAMES:
            if name not in found:
                problems.append(f"discovery missed {name}")
            elif not found[name].is_exe:
                problems.append(f"discovery chose a library for {name}: "
                                f"{found[name].rel_path}")
            elif "obj" in Path(found[name].rel_path).parts:
                problems.append(f"discovery chose build output for {name}: "
                                f"{found[name].rel_path}")
        if problems:
            print("discovery self-check FAILED:", file=sys.stderr)
            for p in problems:
                print(f"  {p}", file=sys.stderr)
            print(f"  found: { {k: v.rel_path for k, v in found.items()} }",
                  file=sys.stderr)
            return 1
        print(f"discovery ok: "
              f"{ {k: v.rel_path for k, v in found.items()} }", flush=True)

        prefix = root / "install"
        publish_repo(tree, prefix, root / "state")

        for name, expect in (("jsonnet", b"A"), ("jsonnetfmt", b"B")):
            got = subprocess.run([str(prefix / "bin" / name)],
                                 capture_output=True, timeout=120)
            if got.returncode != 0 or got.stdout != expect:
                print(f"self-check FAILED: {name} -> exit {got.returncode}, "
                      f"stdout {got.stdout!r}, stderr {got.stderr[:400]!r}",
                      file=sys.stderr)
                return 1
        print("publish self-check: both programs published, installed and ran",
              flush=True)
        return 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path)
    ap.add_argument("--prefix", type=Path)
    ap.add_argument("--state", type=Path)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args(argv)

    if args.self_check:
        return self_check()
    if not (args.repo and args.prefix and args.state):
        ap.error("--repo, --prefix and --state are required")
    publish_repo(args.repo, args.prefix, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
