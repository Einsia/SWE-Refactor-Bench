"""What do the published entry points start, and is there more than one server?

Advisory.  Stage 2 already measures that the binary boots and answers; which code
it reaches to do that, and whether a second implementation is sitting beside it,
is read here.

The gate this feeds asks whether the new implementation is what actually runs.
That question has a specific shape on this subject, because miniserve is a single
binary with a single ``fn main`` and four published ways to invoke it:

    Makefile                  cargo build --release, cargo run --release
    Containerfile             COPY miniserve /app/, ENTRYPOINT ["/app/miniserve"]
    Containerfile.alpine      the same, from a musl build
    packaging/miniserve@.service   ExecStart=/usr/bin/miniserve -- %I

None of those names a module, so none of them can be checked for naming the old
one.  What can be checked is narrower and still worth having: that there is one
binary target rather than two, that ``main`` is not choosing between servers on a
feature or an environment variable, and that the ``#[actix_web::main]`` attribute
State A's ``run`` carries is gone rather than replaced by something that starts an
actix system under another name.

The dead-code direction matters more here than in fw01.  A Rust binary with a
second server in it does not merely carry the old code -- it has to compile it,
which means the retired crates would have to resolve, which they cannot.  So on
this task a surviving second server is almost always a *reachable* one, and the
checks below look for the switch rather than for the file.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: The four published ways to start this program in State A.
ENTRY_POINTS = ["Makefile", "Containerfile", "Containerfile.alpine",
                "packaging/miniserve@.service"]

#: Attribute macros that start a runtime.  ``#[actix_web::main]`` is State A's;
#: the rest are what a submission would reach for, and ``#[tokio::main]`` is the
#: expected answer rather than a finding.
RUNTIME_ATTRIBUTES = {
    "actix_web::main": "starts an actix system",
    "actix_rt::main": "starts an actix runtime directly",
    "actix_web::rt": "reaches into actix's runtime re-export",
}

#: Names that suggest a second server exists to be chosen between.
SWITCH_NAMES = [
    "use_axum", "use_actix", "legacy_server", "old_server", "new_server",
    "USE_AXUM", "USE_ACTIX", "LEGACY", "FALLBACK_SERVER", "MINISERVE_LEGACY",
    "MINISERVE_BACKEND", "server_impl", "backend_impl",
]


# ---------------------------------------------------------------------------
# The binary and its entry
# ---------------------------------------------------------------------------

def test_there_is_exactly_one_binary_target(repo):
    """Two binaries is the cheapest way to keep both servers and ship both.

    State A declares no ``[[bin]]`` at all: ``src/main.rs`` is the default target
    and the binary is named after the package.  A submission that added a second
    one has to be asked why, and the behavioural stage builds
    ``cargo build --release`` and looks for ``target/release/miniserve`` -- so a
    second binary is also a way to have the graded name produced by something
    other than the graded code.
    """
    data = srbscan.manifest(repo)
    bins = data.get("bin") or []
    if not bins:
        assert (repo / "src" / "main.rs").is_file(), (
            "no [[bin]] target is declared and there is no src/main.rs"
        )
        return
    names = [b.get("name") for b in bins if isinstance(b, dict)]
    assert len(bins) == 1, (
        f"the manifest declares {len(bins)} binary targets: {names}. State A "
        "declares none, using src/main.rs as the default target"
    )
    assert names == ["miniserve"], (
        f"the single binary target is named {names}, not ['miniserve']"
    )


def test_no_second_source_file_defines_a_main(repo):
    """``src/bin/*.rs`` is a binary target without a manifest entry.

    Cargo picks these up automatically, so a second server can ship as a second
    binary with nothing declared anywhere.
    """
    offenders = []
    for path, rel in srbscan.crate_sources(repo):
        if rel == "src/main.rs":
            continue
        code = srbscan.shipped_code_of(srbscan.read(path))
        if re.search(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+main\s*\(", code,
                     re.MULTILINE):
            offenders.append(srbscan.cite(path, rel, "fn main", haystack=code))
    assert not offenders, (
        f"these files define a `main` besides src/main.rs: {offenders}"
    )


@pytest.mark.parametrize("attribute", sorted(RUNTIME_ATTRIBUTES))
def test_no_actix_runtime_attribute_survives(attribute, repo):
    """State A's ``run`` is ``#[actix_web::main]``; none of these may remain."""
    offenders: dict[str, str] = {}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.shipped_code_of(srbscan.read(path))
        if attribute in code:
            offenders[rel] = srbscan.cite(path, rel, attribute, haystack=code)
    assert not offenders, (
        f"`{attribute}` ({RUNTIME_ATTRIBUTES[attribute]}) is still in the code: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_main_starts_a_runtime_of_some_kind(repo):
    """The other direction: something has to run the server.

    Not a finding about which runtime -- ``#[tokio::main]``, a hand-built
    ``Runtime::new()``, ``block_on`` and a ``main`` that hands off to a
    ``#[tokio::main] async fn run`` are all legitimate answers, and State A itself
    uses the last shape. A finding here means none of them is present, which is
    worth a look before the reviewer starts reading.
    """
    joined = ""
    for path, rel in srbscan.crate_sources(repo):
        joined += srbscan.shipped_code_of(srbscan.read(path))
    markers = ("tokio::main", "Runtime::new", "block_on", "runtime::Builder",
               "Handle::current", "tokio::runtime")
    found = [m for m in markers if m in joined]
    assert found, (
        f"none of {list(markers)} appears anywhere in the shipped code, so it is "
        "not clear what starts the async runtime the server needs"
    )
    print(f"runtime entry markers present: {found}")


# ---------------------------------------------------------------------------
# A switch between two servers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", SWITCH_NAMES)
def test_no_switch_between_implementations(name, repo):
    """A name that only makes sense if there are two servers to pick from.

    This is a lead, not a verdict, and a weak one taken alone: ``server_impl``
    could be an honest module name in a tree with one server. What makes it worth
    reporting is where it appears -- next to a ``cfg`` or an ``env::var``, which
    the next two checks look for directly.
    """
    offenders: dict[str, str] = {}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.shipped_code_of(srbscan.read(path))
        if name in code:
            offenders[rel] = srbscan.cite(path, rel, name, haystack=code)
    assert not offenders, (
        f"{name!r} appears in the code, which is a name that implies a choice of "
        f"server: {json.dumps(offenders, indent=2)}"
    )


def test_no_cfg_feature_gate_selects_a_server(repo):
    """``#[cfg(feature = ...)]`` around a request path is the documented cheat.

    A feature-gated fallback compiles both servers and ships whichever the
    default feature set selects, which means a submission can pass here and serve
    the old implementation to anyone who builds it with a flag.  The gate that
    reads this asks about "behind a feature, a flag or an environment variable";
    this check finds the ``cfg`` attributes and reports what they gate.
    """
    interesting = re.compile(
        r"#\s*\[\s*cfg(?:_attr)?\s*\(\s*(?:not\s*\(\s*)?feature\s*=\s*\"([^\"]+)\"")
    findings: dict[str, list[str]] = {}
    allowed = {"tls"}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.shipped_code_of(srbscan.read(path))
        for match in interesting.finditer(code):
            feature = match.group(1)
            if feature in allowed:
                continue
            findings.setdefault(rel, []).append(
                f"feature {feature!r} at {srbscan.cite(path, rel, match.group(0), haystack=code)}")
    assert not findings, (
        "code is conditionally compiled on a feature other than 'tls', which is "
        "the only one State A declares: "
        f"{json.dumps(findings, indent=2)}"
    )


def test_no_environment_variable_selects_a_server(repo):
    """``env::var`` on the request path, reported with what it reads.

    miniserve does read the environment legitimately -- clap's ``env`` feature is
    enabled and the CLI declares ``MINISERVE_*`` variables for its options -- so
    the check reports the *names* and lets the reviewer decide. What it is looking
    for is a variable that is not one of the documented options.
    """
    pattern = re.compile(r"env::var(?:_os)?\s*\(\s*\"([^\"]+)\"")
    findings: dict[str, list[str]] = {}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.shipped_code_of(srbscan.read(path))
        for match in pattern.finditer(code):
            findings.setdefault(rel, []).append(match.group(1))
    assert not findings, (
        "the shipped code reads these environment variables directly rather than "
        "through clap's `env` attribute, which is how State A declares its "
        f"MINISERVE_* options: {json.dumps(findings, indent=2)}"
    )


# ---------------------------------------------------------------------------
# The published entry points
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ENTRY_POINTS)
def test_entry_point_still_exists(rel, repo):
    """A capability that quietly disappeared is a finding of its own.

    None of these can be checked for naming the right server -- they all name a
    binary -- so what is checkable is that they are still there and still name
    the binary the build produces.
    """
    path = repo / rel
    assert path.is_file(), (
        f"{rel} is in State A and is not in the submission; it is one of the four "
        "published ways to start this program"
    )
    text = srbscan.read(path)
    assert "miniserve" in text, (
        f"{rel} no longer mentions miniserve: {srbscan.cite(path, rel, 'miniserve')}"
    )


def test_the_makefile_still_builds_with_cargo(repo):
    """``make local`` is ``cargo build --release``, and the grader does the same.

    A Makefile that started fetching, downloading or copying a binary instead of
    compiling one would be doing the thing the behavioural stage is built to
    prevent, and it is a two-line edit.
    """
    path = repo / "Makefile"
    if not path.is_file():
        pytest.skip("the submission ships no Makefile")
    text = srbscan.read(path)
    assert "cargo build" in text, (
        f"the Makefile no longer runs `cargo build`: {srbscan.cite(path, 'Makefile', 'cargo')}"
    )
    for suspicious in ("curl", "wget", "docker pull", "/opt/oracle", "scp "):
        assert suspicious not in text, (
            f"the Makefile fetches rather than builds: "
            f"{srbscan.cite(path, 'Makefile', suspicious)}"
        )


@pytest.mark.parametrize("rel", ["Containerfile", "Containerfile.alpine"])
def test_container_file_copies_a_locally_built_binary(rel, repo):
    """State A's Containerfile copies ``miniserve`` from the build context.

    A ``FROM`` that pulls a published miniserve image, or a ``RUN`` that fetches a
    release tarball, replaces the submission's own build output with somebody
    else's -- and would answer every behavioural case correctly.
    """
    path = repo / rel
    if not path.is_file():
        pytest.skip(f"{rel} is not in the submission")
    text = srbscan.read(path)
    for suspicious in ("svenstaro/miniserve", "curl", "wget",
                       "github.com/svenstaro/miniserve/releases", "cargo install"):
        assert suspicious not in text, (
            f"{rel} obtains miniserve from outside this tree: "
            f"{srbscan.cite(path, rel, suspicious)}"
        )
    assert "COPY" in text or "ADD" in text, (
        f"{rel} no longer copies a binary into the image: "
        f"{srbscan.cite(path, rel, 'FROM')}"
    )


def test_the_systemd_unit_still_execs_the_binary(repo):
    """``ExecStart=/usr/bin/miniserve -- %I`` and nothing in front of it."""
    rel = "packaging/miniserve@.service"
    path = repo / rel
    if not path.is_file():
        pytest.skip(f"{rel} is not in the submission")
    text = srbscan.read(path)
    execs = [line for line in text.splitlines()
             if line.strip().startswith("ExecStart")]
    assert execs, f"{rel} declares no ExecStart"
    joined = " ".join(execs)
    assert "miniserve" in joined, (
        f"{rel} starts something that is not miniserve: {execs}"
    )
    for wrapper in ("sh -c", "bash -c", "/opt/oracle", "proxy"):
        assert wrapper not in joined, (
            f"{rel} wraps the binary rather than executing it: {execs}"
        )


def test_the_release_workflow_still_builds_from_source(repo):
    """CI is not graded, and is read anyway.

    A release workflow that stopped compiling is a strong statement about what
    the submitter believes produces the binary, and it costs one file read to
    know.
    """
    rel = ".github/workflows/build-release.yml"
    path = repo / rel
    if not path.is_file():
        pytest.skip(f"{rel} is not in the submission")
    text = srbscan.read(path)
    assert "cargo" in text, (
        f"the release workflow no longer mentions cargo: {srbscan.cite(path, rel, 'run')}"
    )


def test_no_startup_path_shells_out(repo):
    """``Command::new`` anywhere in ``src/`` is worth the reviewer's attention.

    miniserve has one legitimate reason to spawn a process and it is not on the
    request path: nothing in State A's ``src/`` does it at all. This overlaps with
    the provenance module by design -- that one asks whether an *answer* was
    borrowed, this one asks whether *starting* was delegated -- and a hit in both
    is a stronger lead than a hit in either.
    """
    offenders: dict[str, str] = {}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.shipped_code_of(srbscan.read(path))
        # `spawn(` is deliberately not in this list. State A's
        # `src/listing.rs:369` calls `std::thread::spawn` to build an archive on
        # another thread while the response streams, and a correct migration keeps
        # that shape -- a check that reported it would report the archive
        # implementation as process delegation on every submission that has one.
        # What is being looked for is a *process*, and in Rust that is `Command`.
        for marker in ("Command::new", "process::Command", "std::process::",
                       "libc::fork", "libc::exec", "CommandExt"):
            if marker in code:
                offenders.setdefault(rel, srbscan.cite(path, rel, marker,
                                                       haystack=code))
    assert not offenders, (
        f"the shipped code starts other processes: {json.dumps(offenders, indent=2)}"
    )
