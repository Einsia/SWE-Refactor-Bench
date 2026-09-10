#!/usr/bin/env python3
"""What the published assemblies are allowed to do, read from their metadata.

These are measurements of the build output, not of the source.  The distinction
matters: a grep for `DllImport` is defeated by a computed string, and asserting
that a particular attribute appears in a particular file is asserting about an
implementation.  The CLR's own tables cannot be talked around --

    ImplMap    every P/Invoke declaration.  A DllImport cannot be hidden from it.
    ModuleRef  the native modules those P/Invokes bind to.
    MemberRef  every external member called, which is where Process.Start shows.

-- because they are what the runtime reads to make the call.  A submission can
name its classes anything it likes and still be measured here.

Three things are being asked, and they are separate checks because they fail for
separate reasons.  A port that shells out to the original C++ binary is a
different defect from a port that publishes a native image with nothing to
inspect, and from a port that quietly links a native helper for its float
formatting.

The inspector is a small C# program (lib/asminspect) the verifier image builds
for itself.  If it cannot run, that is a verifier defect and the module errors:
handing a submission a zero because our own tool broke would look like a verdict.

No module in this stage calls these checks.  Reading the CLR tables is the
sharper instrument, and it needs a thing that has been built -- State A is C++
and publishes no assembly, so a scored module here could not be passed by the
reference the stage is frozen from.  The question is therefore asked once, by
stage 1's `no-native-interop` gate, which reads the submitted source, is required
there, and zeroes the whole submission rather than a share of this stage.

The image still builds the inspector and still proves it works (Dockerfile,
`manifest_check.py --asminspect-report`), so a caller that wants the measurement
back needs a weight and a module id and nothing else.  `build.py` forces
PublishSingleFile, PublishAot, PublishTrimmed, PublishReadyToRun and
SelfContained off, which keeps a publish inspectable and is also right for its
own reasons: those shapes change which files a publish directory contains, and
`build.py` finds the programs it launches by looking for them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import vlib
from vlib import Log

#: Types and members that reach outside the managed world.  Matched as
#: substrings of a MemberRef's rendered name, so `Process::Start` is caught
#: whether it was called directly or through a helper in the same assembly.
PROCESS_API_PATTERNS = (
    "System.Diagnostics.Process",
    "System.Diagnostics.ProcessStartInfo",
    "Mono.Unix.Native",
    "System.Runtime.InteropServices.NativeLibrary",
    "System.Runtime.InteropServices.Marshal",
    "System.Reflection.Assembly::LoadFile",
    "System.Reflection.Assembly::LoadFrom",
)

#: Suppressing the GC transition or reaching a member through the unsafe
#: accessor are the two ways to touch native code without leaving an ImplMap row.
UNSAFE_ATTRIBUTES = (
    "System.Runtime.InteropServices.SuppressGCTransitionAttribute",
    "System.Runtime.CompilerServices.UnsafeAccessorAttribute",
)

INSPECT_TIMEOUT = 300.0


class InspectorError(RuntimeError):
    """The inspector itself failed.  A verifier defect, not a submission failure."""


@dataclass
class Finding:
    """One reason a check did not hold, in the assembly it was found in."""
    assembly: str
    note: str

    def __str__(self) -> str:
        return f"{self.assembly}: {self.note}"


@dataclass
class MetaCheck:
    check_id: str
    summary: str
    passed: bool
    detail: str = ""
    findings: list[str] = field(default_factory=list)


def inspect(inspector: Path, publish_dir: Path, log: Log) -> dict:
    """Run the metadata inspector over one publish directory."""
    result = vlib.run(["dotnet", str(inspector), str(publish_dir)],
                      timeout=INSPECT_TIMEOUT, log=log,
                      label=f"asminspect {publish_dir.name}", full_capture=True)
    if not result.ok:
        raise InspectorError(
            f"the metadata inspector exited {result.returncode} on "
            f"{publish_dir}: {result.stderr.decode('utf-8', 'replace')[:400]}")
    try:
        return json.loads(result.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InspectorError(f"inspector output was not JSON: {exc}") from exc


def merge(reports: dict[str, dict]) -> dict:
    """One report over both publish directories, tagged by which CLI it came from.

    The two CLIs are published separately so that neither can satisfy a check by
    borrowing the other's files, but the questions are about the port as a whole:
    a P/Invoke in a library shared by both should be reported once per place it
    was published, and named so a reader can tell which is which.
    """
    out: dict = {"assemblies": [], "nativeFiles": [], "unreadable": [],
                 "publishDirs": {}}
    for cli, report in sorted(reports.items()):
        out["publishDirs"][cli] = report.get("publishDir", "")
        for asm in report.get("assemblies", []):
            tagged = dict(asm)
            tagged["cli"] = cli
            out["assemblies"].append(tagged)
        for rel in report.get("nativeFiles", []):
            out["nativeFiles"].append(f"{cli}/{rel}")
        for row in report.get("unreadable", []):
            tagged = dict(row)
            tagged["cli"] = cli
            out["unreadable"].append(tagged)
    return out


def _named(asm: dict) -> str:
    cli = asm.get("cli")
    name = asm.get("name", "?")
    return f"{cli}:{name}" if cli else name


def check_managed_only(report: dict) -> MetaCheck:
    """No P/Invoke, no native module references, no unmanaged entry point."""
    bad: list[str] = []
    for asm in report.get("assemblies", []):
        name = _named(asm)
        for row in asm.get("pinvokes", []):
            bad.append(f"{name}: P/Invoke {row.get('method')} -> "
                        f"{row.get('module')}!{row.get('entryPoint')}")
        for mod in asm.get("moduleRefs", []):
            bad.append(f"{name}: references native module {mod}")
        for attr in asm.get("attributes", []):
            if attr in UNSAFE_ATTRIBUTES:
                bad.append(f"{name}: carries {attr}")
        if asm.get("hasNativeResources"):
            names = asm.get("nativeResourceNames") or []
            bad.append(f"{name}: embeds a native resource"
                       + (f" ({', '.join(names[:4])})" if names else ""))
    return MetaCheck(
        "managed-only",
        "the published assemblies contain no native interop",
        not bad,
        "no ImplMap or ModuleRef rows in any assembly" if not bad
        else f"{len(bad)} native-interop row(s) in the published output",
        bad)


def check_no_process_spawn(report: dict) -> MetaCheck:
    """Nothing in the published closure can start a process or load a library.

    This is what closes the cheapest shortcut: keep the C++ program somewhere and
    shell out to it.  The reference binaries are not in this image, but "there is
    no binary to call" is a property of the image, and a submission graded in a
    later image should not become a pass because one appeared.  So the ability to
    call one is removed too.
    """
    bad: list[str] = []
    for asm in report.get("assemblies", []):
        name = _named(asm)
        for ref in asm.get("memberRefs", []):
            for pat in PROCESS_API_PATTERNS:
                if pat in ref:
                    bad.append(f"{name}: references {ref}")
                    break
    return MetaCheck(
        "no-process-spawn",
        "no process-spawning or library-loading API is referenced",
        not bad,
        "no references to Process, NativeLibrary or Marshal" if not bad
        else f"{len(bad)} reference(s) to process or interop APIs",
        bad)


def check_publish_is_managed(report: dict, expect: tuple[str, ...]) -> MetaCheck:
    """The publish output is inspectable managed IL, with both entry points.

    A native-AOT image has no metadata, so every check above it would pass by
    having nothing to read.  The absence of inspectable assemblies is therefore
    itself a failure -- and it is why this check is the module's `required` one:
    passing the other two over an empty report is not evidence of anything.

    The module is weight 0.00, so `required` here decides nothing about the score;
    it zeroes a rate that carries no weight.  It is kept because the sentence above
    stays true of the report a reader opens.  Why the module is zero is in
    suite.toml: this check asks for a managed assembly, State A is C++ and has none,
    and every expectation this stage grades was frozen from State A.
    """
    bad: list[str] = []
    asms = report.get("assemblies", [])
    if not asms:
        bad.append("no managed assemblies in the publish output; a native or "
                   "single-file-native publish cannot be inspected and is not "
                   "accepted")

    entry = {a.get("name", "").lower() for a in asms if a.get("isExecutable")}
    for want in expect:
        if want.lower() not in entry:
            bad.append(f"no managed executable assembly named {want!r} "
                       f"(executables found: {sorted(entry) or 'none'})")

    for rel in report.get("nativeFiles", []):
        bad.append(f"native file in the publish output: {rel}")

    for row in report.get("unreadable", []):
        bad.append(f"unreadable file {row.get('path')}: {row.get('error')}")

    return MetaCheck(
        "publish-is-managed",
        "the published output is inspectable managed IL with both programs",
        not bad,
        "both programs present as managed IL, no native files" if not bad
        else "; ".join(bad[:3]),
        bad)


def evaluate(report: dict, expect: tuple[str, ...]) -> list[MetaCheck]:
    """Every metadata check, in the order they are worth reading."""
    return [
        check_publish_is_managed(report, expect),
        check_managed_only(report),
        check_no_process_spawn(report),
    ]


def summarize(report: dict) -> dict:
    """Counts for the module's metadata block, so a report is readable alone."""
    asms = report.get("assemblies", [])
    return {
        "assemblies": len(asms),
        "executables": sum(1 for a in asms if a.get("isExecutable")),
        "il_only": sum(1 for a in asms if a.get("ilOnly")),
        "pinvokes": sum(len(a.get("pinvokes") or []) for a in asms),
        "module_refs": sum(len(a.get("moduleRefs") or []) for a in asms),
        "member_refs": sum(len(a.get("memberRefs") or []) for a in asms),
        "native_files": len(report.get("nativeFiles") or []),
        "unreadable": len(report.get("unreadable") or []),
        "names": sorted(_named(a) for a in asms)[:40],
    }
