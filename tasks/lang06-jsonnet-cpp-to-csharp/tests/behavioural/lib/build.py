#!/usr/bin/env python3
"""Publishing the submission's two command-line programs.

This is the only place the verifier runs the submission's build.  Everything
after it consumes two executables and knows nothing about how they were
produced -- which is the point: the graded question is what the programs do, not
how the repository is arranged.

Three decisions here are load-bearing.

**Projects are found by what they produce, not by what they are called.**  The
contract asks for an executable project whose *assembly name* is `jsonnet` or
`jsonnetfmt`, and says in as many words that project file names and directory
layout are unconstrained.  So discovery reads <AssemblyName> and <OutputType>
out of every .csproj and keys on the result.  A verifier that looked for
`jsonnet/jsonnet.csproj` would fail every port that used ordinary .NET
conventions -- one solution, projects named `Jsonnet.Cli` and `Jsonnet.Core` --
and it would fail it at the build step, which zeroes the whole stage.

**The publish is forced into one shape.**  PublishSingleFile, PublishAot,
PublishTrimmed, PublishReadyToRun and SelfContained are all passed as `false` on
the command line, which overrides whatever the project sets.

Two reasons, and they hold independently.  A native-AOT image has no CLR
metadata, so nothing downstream can read what the assemblies declare.  And these
flags decide what a publish directory contains and what the restore has to
resolve, while this image has no network: the feed seeded at /opt/nuget-offline
holds packages, not runtime packs, so a self-contained or single-file publish has
nothing to restore against.  Every publish the image performs for itself -- the
inspector, the metadata probe -- passes the same flags for the same reason.
Framework-dependent is the shape that works here, so it is the shape asked for,
and none of these flags changes anything a case can observe.

**A build directory the submission left behind is discarded, not reused.**
`bin/`, `obj/` and a checked-in `publish/` are removed from the copy before
anything runs.  A submission that ships a prebuilt assembly and no source that
produces it must fail, and it cannot be allowed to satisfy the publish step with
an artifact this container never compiled.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

# `--self-check` runs this file as a script under `python3 -I -B`, and -I implies -P:
# the script's own directory is not on sys.path, so a bare `import vlib` fails.
# Put it back the way driver.py does.  Harmless when imported as a module, since
# the directory is already there.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import vlib  # noqa: E402  (after the sys.path fix above)
from vlib import Log  # noqa: E402

#: The two programs the contract names, and the order they are reported in.
CLI_NAMES = ("jsonnet", "jsonnetfmt")

#: The NuGet configuration every restore here is made with.  It clears the source
#: list and adds nothing back, which is what turns "the implementation must
#: compile against the base class library alone, no external packages"
#: (instruction.md) from prose into a rule the image enforces.
#:
#: Clearing sources does not empty the feed: a package already in the global
#: packages folder is resolved from there without consulting any source, so the
#: pre-seeded test frameworks the instruction invites still restore and still
#: run.  A package that is *not* there fails as `NU1100: Unable to resolve`,
#: which names the problem, instead of as a network timeout, which describes a
#: symptom.
#:
#: Passed with --configfile rather than left to discovery on purpose.  NuGet walks
#: up from the project directory collecting every nuget.config it finds, so a
#: submission could otherwise add its own and put a source back; --configfile
#: replaces that hierarchy outright.  And it does not rely on the network being
#: down, which is not something this file can see and not uniform across the
#: ladder -- stages 1 and 3 need the model API.
NUGET_CONFIG = Path("/opt/nuget.config")

#: Directories that are build output wherever they appear.  A submission's own
#: `bin/` of source would be unusual, but these are removed from a *copy*, so the
#: cost of being wrong is that a module rebuilds something.
DISCARD_DIRS = ("bin", "obj", "publish", ".vs", "artifacts", ".git",
                "__pycache__", ".idea", "TestResults")

#: Properties forced off at publish time, with the value each is forced to.
#: Listed rather than inlined so the `about` text in suite.toml and the
#: instruction can quote the same set, and so a future addition is one line.
FORCED_OFF = (
    "PublishSingleFile",
    "PublishAot",
    "PublishTrimmed",
    "PublishReadyToRun",
    "SelfContained",
)

PUBLISH_TIMEOUT = 1500.0


@dataclass
class Project:
    """One .csproj that produces one of the two CLI assemblies."""
    assembly: str
    rel_path: str
    is_exe: bool


@dataclass
class PublishOutcome:
    """What happened when one project was published."""
    assembly: str
    project: str
    ok: bool = False
    publish_dir: str = ""
    launcher: list[str] = field(default_factory=list)
    detail: str = ""


def produced_name(repo: Path, rel: str) -> tuple[str, bool]:
    """(lowercased assembly name, produces an executable) for one .csproj.

    A .NET project produces <AssemblyName> when it sets one and its own filename
    stem otherwise.  Deliberately a regex rather than an XML parse: a project
    file that does not parse as strict XML still builds if MSBuild accepts it,
    and this must not be stricter than the tool it is describing.
    """
    stem = os.path.splitext(os.path.basename(rel))[0]
    name, is_exe = stem, False
    try:
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return name.lower(), is_exe
    # The last <AssemblyName> wins, which is MSBuild's own rule for a property
    # set more than once in one file.
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
    A library named `jsonnet` is still recorded: publishing it will fail to
    produce a runnable program, and "your jsonnet project is a library" is a more
    useful report than "no project found".
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


def snapshot(submitted: Path, dest: Path, log: Log) -> dict:
    """Copy the submission, then work only on the copy.

    Counted before the discards so the record describes the tree as submitted:
    a `bin/` full of assemblies is evidence, and deleting it silently would
    remove that evidence from the report.
    """
    if not submitted.is_dir():
        raise SystemExit(f"no submission at {submitted}")
    if dest.exists():
        shutil.rmtree(dest)
    files = vlib.copy_tree(submitted, dest)

    discarded: dict[str, int] = {}
    for name in DISCARD_DIRS:
        for stale in list(dest.rglob(name)):
            if stale.is_dir():
                n = sum(1 for _ in stale.rglob("*") if _.is_file())
                shutil.rmtree(stale, ignore_errors=True)
                if n:
                    discarded[name] = discarded.get(name, 0) + n
    log.write(f"snapshot: {files} file(s) copied"
              + (f", discarded {sum(discarded.values())} build-output file(s) "
                 f"in {sorted(discarded)}" if discarded else ""))
    return {"files": files, "discarded": discarded}


def restore(repo: Path, projects: dict[str, Project], log: Log) -> vlib.Result:
    """Restore offline, before any publish.

    Separate from publish so that "your project references a package that is not
    in the offline feed" reads as a restore failure rather than as a compile
    failure fifty lines into an MSBuild log.

    What gets restored is not the current directory.  `dotnet restore` with no
    target means "the project or solution here", and fails outright when the
    directory holds neither -- so a tree whose projects sit under
    `src/<Name>/<Name>.csproj` with no root `.sln` would have failed the first
    step of the build, and that is an ordinary .NET layout the contract asks
    nobody to avoid.  So restore the discovered projects, and only those: a
    project restore walks its own ProjectReference graph, which is exactly the
    closure publishing it needs.  Restoring a root solution instead would be
    wider than the job -- it pulls in every project the solution lists,
    including the test project the instructions explicitly invite, and one of
    those failing to restore offline would fail a build whose two programs were
    fine.
    """
    # The caller only restores once discovery found both programs, so there is
    # always at least one project here.  An empty dict would be a bug in the
    # driver rather than anything a submission did, and returning a fabricated
    # success for it would hide that.
    assert projects, "restore() called with no discovered projects"
    if not NUGET_CONFIG.is_file():
        raise SystemExit(
            f"{NUGET_CONFIG} is missing: it is what makes the offline feed a "
            f"closed set, and without it a restore falls back to whatever "
            f"sources the SDK has configured.  The image is built wrong.")
    # Restore and publish run submission-authored MSBuild, so they run dropped.
    # The repo is granted first because both write into it -- obj/ from restore,
    # obj/ and bin/ from publish.
    vlib.grant_unprivileged(repo)
    result = None
    for name in sorted(projects):
        result = vlib.run(
            ["dotnet", "restore", projects[name].rel_path, "--nologo",
             "--configfile", str(NUGET_CONFIG)],
            cwd=repo, timeout=PUBLISH_TIMEOUT, log=log,
            label=f"dotnet restore {name}", env=vlib.unprivileged_env(),
            unprivileged=True)
        if not result.ok:
            return result
    return result


def publish(repo: Path, project: Project, out_dir: Path,
            log: Log) -> PublishOutcome:
    """Publish one project into its own directory and locate what came out."""
    outcome = PublishOutcome(assembly=project.assembly, project=project.rel_path)
    if not project.is_exe:
        outcome.detail = (
            f"{project.rel_path} produces an assembly named "
            f"{project.assembly!r} but is not an executable project: the "
            f"contract asks for OutputType Exe or WinExe")
        return outcome

    argv = ["dotnet", "publish", project.rel_path,
            "-c", "Release", "-o", str(out_dir), "--nologo",
            "--no-restore"]
    argv += [f"-p:{prop}=false" for prop in FORCED_OFF]
    # out_dir is created here rather than left to MSBuild, because the dropped
    # child has to be able to write into it and only root can hand it over.
    out_dir.mkdir(parents=True, exist_ok=True)
    vlib.grant_unprivileged(out_dir, repo)
    result = vlib.run(argv, cwd=repo, timeout=PUBLISH_TIMEOUT, log=log,
                      label=f"publish {project.assembly}",
                      env=vlib.unprivileged_env(), unprivileged=True)
    if not result.ok:
        outcome.detail = result.tail(lines=40, limit=2400)
        return outcome

    outcome.publish_dir = str(out_dir)
    launcher = _launcher(out_dir, project.assembly)
    if launcher is None:
        listing = sorted(p.name for p in out_dir.iterdir())[:20] \
            if out_dir.is_dir() else []
        outcome.detail = (
            f"publish succeeded but produced neither {project.assembly} nor "
            f"{project.assembly}.dll in {out_dir}; it holds {listing}")
        return outcome
    outcome.launcher = launcher
    outcome.ok = True
    return outcome


def _launcher(out_dir: Path, assembly: str) -> list[str] | None:
    """How to invoke the published program.

    The apphost is preferred when it exists and is executable, because that is
    what a user runs.  `dotnet <dll>` is the fallback: a publish with
    UseAppHost=false is a legitimate shape, and refusing it would fail a
    submission for a property that changes nothing a case can observe.
    """
    apphost = out_dir / assembly
    if apphost.is_file() and os.access(apphost, os.X_OK):
        return [str(apphost)]
    dll = out_dir / f"{assembly}.dll"
    if dll.is_file():
        return ["dotnet", str(dll)]
    return None


# --------------------------------------------------------------------------- #
# Build-time self-check
# --------------------------------------------------------------------------- #
#
# Everything above runs before any case does, and every case needs what it
# publishes.  So a mistake in it does not cost the `build` module's own weight --
# that weight is 0.00 -- it costs all 40 points of this stage, because a family
# whose programs never published records every one of its cases as failed.  Which
# is why it is checked at image build time against a tree shaped like the ports it
# has to accept, rather than only against whatever a submission happens to look
# like.
#
# The tree below is deliberately the awkward legal shape: projects under
# `src/<Name>/`, **no solution file at the root**, project file names sharing no
# substring with the assemblies they produce, and a library that also produces
# `jsonnet` to make sure discovery prefers the executable.  This is ordinary .NET
# and it is what `restore` has to survive: `dotnet restore` with no target fails
# outright in a directory holding neither a project nor a solution, which would
# fail the build at its first step.

_SELF_CHECK_PROG = {
    "jsonnet": 'System.Console.Out.Write("A");',
    "jsonnetfmt": 'System.Console.Out.Write("B");',
}

_SELF_CHECK_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>{output_type}</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>{assembly}</AssemblyName>
    <Nullable>enable</Nullable>
    <InvariantGlobalization>true</InvariantGlobalization>
  </PropertyGroup>
</Project>
"""


def _self_check_tree(root: Path) -> None:
    """Write the synthetic submission described above."""
    # The two real programs, in project files named nothing like their output.
    for assembly, project_name in (("jsonnet", "Sharpnet.NetTool"),
                                   ("jsonnetfmt", "Sharpnet.FmtTool")):
        d = root / "src" / f"Sharpnet.{assembly.capitalize()}"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{project_name}.csproj").write_text(_SELF_CHECK_CSPROJ.format(
            output_type="Exe", assembly=assembly), encoding="utf-8")
        (d / "Program.cs").write_text(_SELF_CHECK_PROG[assembly],
                                      encoding="utf-8")

    # A *library* that also produces `jsonnet`.  Discovery must not pick it.
    lib = root / "src" / "Sharpnet.Core"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "Sharpnet.Core.csproj").write_text(_SELF_CHECK_CSPROJ.format(
        output_type="Library", assembly="jsonnet"), encoding="utf-8")
    (lib / "Thing.cs").write_text("namespace Sharpnet.Core; public class Thing {}",
                                  encoding="utf-8")

    # Build output naming a third `jsonnet` project, which must be ignored
    # because it is under obj/ -- and discarded from the copy entirely.
    stale = root / "obj" / "Debug"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "jsonnet.csproj").write_text(_SELF_CHECK_CSPROJ.format(
        output_type="Exe", assembly="jsonnet"), encoding="utf-8")


_PKG_CSPROJ = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <AssemblyName>jsonnet</AssemblyName>
  </PropertyGroup>
  <ItemGroup>
    <PackageReference Include="Newtonsoft.Json" Version="13.0.3" />
  </ItemGroup>
</Project>
"""


def _check_feed_is_closed(work: Path, log: Log) -> int:
    """A package outside the offline feed must fail restore.

    The positive case proves an ordinary port restores: a framework-dependent
    net8.0 console program needs nothing from NuGet, only the SDK's targeting
    pack, so the two CLI projects restore against a feed holding none of what
    they use.  Everything in the feed -- xunit, NUnit, FluentAssertions -- is
    there for the test project the instruction invites, which grading never
    builds.

    This proves the other half: that the feed is a *closed set*, so "port it with
    the standard library" is a rule the image enforces rather than a convention.
    The failure text is logged either way, because what a submitter sees decides
    whether the rule is legible -- naming the local source is a rule, a DNS
    timeout is a symptom.
    """
    root = work / "pkg" / "src" / "Sharpnet.Pkg"
    root.mkdir(parents=True, exist_ok=True)
    (root / "Sharpnet.Pkg.csproj").write_text(_PKG_CSPROJ, encoding="utf-8")
    (root / "Program.cs").write_text('System.Console.Out.Write("C");',
                                     encoding="utf-8")

    repo = work / "pkg"
    projects = find_cli_projects(repo)
    if "jsonnet" not in projects:
        print("the package-reference tree did not discover its own project",
              file=sys.stderr)
        return 1
    result = restore(repo, {"jsonnet": projects["jsonnet"]}, log)
    if result.ok:
        print("a PackageReference outside the offline feed restored anyway: the "
              "feed is not closed, so nothing stops a submission depending on a "
              "package -- and a port that leans on one is not the port this task "
              "grades", file=sys.stderr)
        return 1
    log.write("offline feed is closed; an outside package was refused with:\n"
              + result.tail(lines=12, limit=800))
    return 0


def self_check(work: Path, log: Log) -> int:
    """Run discovery, restore, publish and launch against the synthetic tree.

    `subprocess` is imported here rather than at the top of the module on
    purpose: the grading path spawns processes only through `vlib.run`, and this
    is the one place that does not, because it wants the two bytes a program
    printed rather than a verifier Result.  Keeping the import inside the
    self-check keeps that true of the module a case actually imports.
    """
    import subprocess

    submitted = work / "submitted"
    submitted.mkdir(parents=True, exist_ok=True)
    _self_check_tree(submitted)

    repo = work / "repo"
    snapshot(submitted, repo, log)

    projects = find_cli_projects(repo)
    got = {name: p.rel_path for name, p in sorted(projects.items())}
    log.write(f"discovery: {got}")
    if sorted(projects) != sorted(CLI_NAMES):
        print(f"discovery found {sorted(projects)}, expected {sorted(CLI_NAMES)}",
              file=sys.stderr)
        return 1
    for name in CLI_NAMES:
        if not projects[name].is_exe:
            print(f"discovery picked a non-executable project for {name}: "
                  f"{projects[name].rel_path}", file=sys.stderr)
            return 1
    if "obj" in projects["jsonnet"].rel_path.split("/"):
        print(f"discovery picked build output for jsonnet: "
              f"{projects['jsonnet'].rel_path}", file=sys.stderr)
        return 1
    if (repo / "obj").exists():
        print("snapshot kept obj/ in the copy", file=sys.stderr)
        return 1

    if not (restored := restore(repo, projects, log)).ok:
        print("restore failed on a tree with no root solution file:\n"
              + restored.tail(lines=40, limit=2400), file=sys.stderr)
        return 1

    for name, expected in (("jsonnet", b"A"), ("jsonnetfmt", b"B")):
        outcome = publish(repo, projects[name], work / "publish" / name, log)
        if not outcome.ok:
            print(f"publish of {name} failed: {outcome.detail}", file=sys.stderr)
            return 1
        proc = subprocess.run(outcome.launcher, capture_output=True,
                              timeout=120, env=vlib.base_env())
        if proc.returncode != 0 or proc.stdout != expected:
            print(f"{name} published but ran wrong: exit {proc.returncode}, "
                  f"stdout {proc.stdout!r}, expected {expected!r}",
                  file=sys.stderr)
            return 1

    # A tree that *does* carry a root solution must restore too.  An earlier fix
    # preferred `dotnet restore <root>.sln` when one existed, which is wider than
    # the job -- it drags in every project the solution lists, including the test
    # project, so one of those failing offline would fail a build whose two
    # programs were fine.  Restoring per project made that branch unnecessary;
    # this case is what stops it coming back unnoticed.
    (repo / "Sharpnet.sln").write_text(
        "Microsoft Visual Studio Solution File, Format Version 12.00\n",
        encoding="utf-8")
    if not (again := restore(repo, projects, log)).ok:
        print("restore failed once a root solution file existed:\n"
              + again.tail(lines=40, limit=2400), file=sys.stderr)
        return 1

    if (rc := _check_feed_is_closed(work, log)) != 0:
        return rc

    print("build self-check: discovery, restore, publish and launch agree on a "
          "src/*/ tree with no root solution, restore ignores a root solution "
          "that does exist, and the offline feed refuses an outside package",
          flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(
        description="self-check the build rule against a synthetic submission")
    parser.add_argument("--self-check", action="store_true", required=True,
                        help="the only mode; named so the call site reads")
    parser.parse_args(argv if argv is not None else sys.argv[1:])

    with tempfile.TemporaryDirectory(prefix="build-self-check-") as tmp:
        return self_check(Path(tmp), Log())


if __name__ == "__main__":
    raise SystemExit(main())
