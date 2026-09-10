"""The command line, which is an interface with users of its own.

Everything here runs the built binary and looks at what came back.  No server is
involved, which makes these the cheapest checks in the suite and the ones most
likely to fail first on a port that compiles: rewiring ``Cargo.toml`` to drop a
TLS feature is one edit away from a binary that no longer has ``--tls-cert``, and
that is a broken interface long before it is a broken response.

The flag inventory is derived from the baseline's own ``--help`` rather than
declared here, so it cannot drift.  It is also asserted per flag, which is the
whole value: "``--show-wget-footer`` is missing" is a finding someone can act on
in a minute, and it would otherwise arrive as one line of a 264-line diff.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

import replay
from compare import _trim, volatile
from harness import cli

#: A long flag as it appears in clap's help, at the start of an option block.
_LONG_FLAG = re.compile(r"^\s+(?:-(\w), )?(--[a-z0-9][a-z0-9-]*)", re.M)
#: The ``[env: MINISERVE_X=]`` annotation clap prints under a flag it aliases.
_ENV_ALIAS = re.compile(r"\[env: (MINISERVE_[A-Z0-9_]+)=")


def _flags(help_text: str) -> dict[str, str | None]:
    """Long flag -> short form, as help declares them."""
    return {long: short or None for short, long in _LONG_FLAG.findall(help_text)}


def _block(help_text: str, flag: str) -> str:
    """The help text belonging to one flag, up to the next flag."""
    matches = list(_LONG_FLAG.finditer(help_text))
    for index, match in enumerate(matches):
        if match.group(2) != flag:
            continue
        end = (matches[index + 1].start() if index + 1 < len(matches)
               else len(help_text))
        return help_text[match.start():end]
    return ""


def _golden_help() -> str:
    """The baseline's ``--help``, read at collection time.

    Read from the recording rather than hard-coded because the alternative is a
    second copy of a 41-flag interface that could disagree with the one being
    graded.
    """
    try:
        data = replay.load_golden()
    except Exception:                                            # noqa: BLE001
        return ""
    return ((data.get("cli") or {}).get("help") or {}).get("stdout") or ""


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def submission_binary() -> str:
    """The executable the build module compiled.

    Taken from the recording rather than rebuilt: this module is the one place
    outside ``build`` that runs the submission, and running a *second* build here
    would both cost minutes and risk grading a different binary than every other
    module did.
    """
    recording = replay.load_actual()
    binary = recording.get("binary")
    if not binary or not Path(binary).is_file():
        pytest.fail(f"the build module recorded no usable binary "
                    f"({binary!r}); every check here needs one", pytrace=False)
    return binary


@pytest.fixture(scope="session")
def cli_actual(submission_binary) -> dict:
    """Run every invocation against the submission, once."""
    workdir = Path(os.environ.get("SRB_WORK", "/tmp/srb-cli")) / "cli"
    workdir.mkdir(parents=True, exist_ok=True)
    return {inv.id: cli.run(submission_binary, inv, workdir)
            for inv in cli.INVOCATIONS}


@pytest.fixture(scope="session")
def cli_expected(golden) -> dict:
    return golden.get("cli") or {}


@pytest.fixture(scope="session")
def baseline_help(cli_expected) -> str:
    text = (cli_expected.get("help") or {}).get("stdout") or ""
    if not text:
        pytest.fail("the State A capture has no --help output, which is a "
                    "harness bug", pytrace=False)
    return text


@pytest.fixture(scope="session")
def submission_help(cli_actual) -> str:
    got = cli_actual.get("help") or {}
    return (got.get("stdout") or "") + (got.get("stderr") or "")


def pytest_generate_tests(metafunc):
    """One check per invocation, and one per flag the baseline declares."""
    if "inv_id" in metafunc.fixturenames:
        ids = [inv.id for inv in cli.INVOCATIONS]
        metafunc.parametrize("inv_id", ids, ids=ids)
    if "flag_spec" in metafunc.fixturenames:
        specs = sorted(_flags(_golden_help()).items())
        if not specs:
            raise pytest.UsageError(
                "the State A capture declares no flags; without them this "
                "module would silently grade nothing")
        metafunc.parametrize("flag_spec", specs, ids=[s[0] for s in specs])


# --------------------------------------------------------------------------- #
# Per-invocation
# --------------------------------------------------------------------------- #


def test_exit_code(inv_id, cli_actual, cli_expected):
    """The exit status, which scripts branch on.

    clap exits 2 for a usage error and miniserve exits 1 for a runtime failure.
    Both are load-bearing and a port that collapses them fails here.
    """
    got, want = cli_actual[inv_id], cli_expected.get(inv_id)
    assert want is not None, f"cli::{inv_id}: no State A record"
    assert not got["timed_out"], f"cli::{inv_id}: the process did not exit"
    assert got["exit"] == want["exit"], (
        f"cli::{inv_id} ({' '.join(want['argv'])}): exited {got['exit']}, "
        f"expected {want['exit']}")


def test_output_substrings(inv_id, cli_actual, cli_expected):
    """The parts of the message that name a flag, a value or a variable.

    ``missing`` is computed by the recorder against live output on both sides, so
    it is available even for invocations whose text is not stored.  The capture
    refuses to write a recording where the baseline's own ``missing`` is
    non-empty, so anything here is the submission's.
    """
    got, want = cli_actual[inv_id], cli_expected.get(inv_id)
    assert want is not None, f"cli::{inv_id}: no State A record"
    assert not got["missing"], (
        f"cli::{inv_id} ({' '.join(want['argv'])}): the output no longer "
        f"mentions {got['missing']}")


def test_output_bytes(inv_id, cli_actual, cli_expected):
    """Byte-exact stdout and stderr, for the invocations that are reproducible.

    ``--help``, ``--version``, the manpage and all five completion scripts are
    generated from one clap declaration, so they are byte-reproducible and are
    compared as such.  The strictest assertion in the suite, deliberately: the
    argument parser is an interface the task says to preserve, and "preserve" is a
    claim about bytes.

    The invocations whose output names a path or a chosen port are compared by the
    other four checks instead of being waived -- their exit code, their
    substrings, and which stream they wrote to are all still graded.
    """
    got, want = cli_actual[inv_id], cli_expected.get(inv_id)
    assert want is not None, f"cli::{inv_id}: no State A record"
    if not want.get("exact"):
        return
    vol = volatile(want)
    for stream in ("stdout", "stderr"):
        if stream in vol:
            continue
        a, b = got.get(stream) or "", want.get(stream) or ""
        assert a == b, (
            f"cli::{inv_id} ({' '.join(want['argv'])}): {stream} differs\n"
            f"  expected ({len(b)} chars):\n{_trim(b)}\n"
            f"  actual ({len(a)} chars):\n{_trim(a)}")


def test_stream_choice(inv_id, cli_actual, cli_expected):
    """Which stream the output went to.

    ``miniserve --print-completions bash > _miniserve`` has to write a usable
    file, and ``--help`` on a usage error has to be readable when stdout is piped
    to a pager.  Graded as "empty or not" per stream, which is the part a shell
    can observe without reading the content.
    """
    got, want = cli_actual[inv_id], cli_expected.get(inv_id)
    assert want is not None, f"cli::{inv_id}: no State A record"
    for stream in ("stdout", "stderr"):
        key = f"{stream}_len"
        if key in volatile(want):
            continue
        assert bool(got[key]) == bool(want[key]), (
            f"cli::{inv_id} ({' '.join(want['argv'])}): {stream} is "
            + ("empty" if not got[key] else f"{got[key]} bytes")
            + ", expected "
            + ("empty" if not want[key] else f"{want[key]} bytes")
            + ".  Which stream a message goes to is part of the interface.")


# --------------------------------------------------------------------------- #
# The flag inventory
# --------------------------------------------------------------------------- #


def test_flag_present(flag_spec, submission_help, baseline_help):
    """One flag, its short form, and its environment alias.

    41 separate findings instead of one diff.  The env alias is checked in the
    same place because it is declared on the same field; losing that half is
    silent -- the flag still works, and every deployment configuring miniserve
    through the environment stops being configured.
    """
    long, short = flag_spec
    got_flags = _flags(submission_help)
    assert long in got_flags, f"--help no longer offers {long}"
    assert got_flags[long] == short, (
        f"{long}: short form is "
        + (f"-{got_flags[long]}" if got_flags[long] else "absent")
        + ", expected " + (f"-{short}" if short else "absent"))
    want_env = _ENV_ALIAS.findall(_block(baseline_help, long))
    got_env = _ENV_ALIAS.findall(_block(submission_help, long))
    assert got_env == want_env, (
        f"{long}: environment aliases are {got_env}, expected {want_env}")


def test_help_flag_set(baseline_help, submission_help):
    """No flag added and none removed, as a set."""
    want, got = _flags(baseline_help), _flags(submission_help)
    missing = sorted(set(want) - set(got))
    added = sorted(set(got) - set(want))
    assert not missing, f"--help no longer offers {missing}"
    assert not added, (
        f"--help offers flags the baseline does not: {added}.  Extra options are "
        f"an interface change too -- a wrapper script that passes one to the real "
        f"miniserve breaks.")


def test_help_flag_order(baseline_help, submission_help):
    """The order flags are listed in, which is declaration order in the struct.

    Cosmetic in isolation, and kept because it is free: it holds as long as the
    port moves the argument declaration rather than rewriting it, and it fails
    loudly if someone reorders it while porting -- which is the moment an
    attribute is most likely to have been dropped along the way.
    """
    want, got = list(_flags(baseline_help)), list(_flags(submission_help))
    assert got == want, (
        f"--help lists flags in a different order\n  expected: {want}\n"
        f"  actual:   {got}")


def test_version_reports_a_version(cli_actual):
    """``--version`` prints something version-shaped.

    Not a parity check -- the exact string is compared byte for byte above -- and
    kept because it is the one CLI failure that makes every other report
    confusing: a binary that prints nothing here is usually a binary whose clap
    declaration was replaced wholesale, and saying that plainly beats eight
    simultaneous byte diffs.
    """
    got = cli_actual.get("version") or {}
    text = ((got.get("stdout") or "") + (got.get("stderr") or "")).strip()
    assert re.search(r"\d+\.\d+\.\d+", text), (
        f"`miniserve --version` printed {json.dumps(text[:200])}, which carries "
        f"no version number")
