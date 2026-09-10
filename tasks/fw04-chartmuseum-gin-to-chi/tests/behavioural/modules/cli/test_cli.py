"""The command-line surface: exit codes, which stream spoke, and the flag table.

ChartMuseum is shipped as a binary, so its CLI *is* part of its public interface.
Two things about it are easy to break while porting the HTTP layer and invisible
if you only test HTTP:

``urfave/cli`` writes usage errors to **stdout** and exits **0**, while
``log.Fatal`` writes to stderr and exits 1. Which of those a given bad invocation
gets is not a detail -- it is what every wrapper script and container healthcheck
keys on. A port that routes an unknown flag through a different error path changes
the exit code of ``chartmuseum --nope`` from 0 to 1 and breaks callers that never
made an HTTP request.

The flag table is the other half. Flags are what select the behaviour the HTTP
corpus exercises, so a flag that silently stopped being accepted would turn a
whole class of HTTP tests into 'server would not start' -- and a flag that quietly
changed its default changes behaviour for everyone who did not set it.

Needles are asserted against the streams that carried them in State A, and that
mapping was *measured* at capture time, never declared: capture refuses to write a
golden file in which State A itself fails to print one of its own needles.
"""

from __future__ import annotations

import pytest

import reference as ref


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("case_id", ref.CLI_IDS)
def test_cli_exit_code(cli_pair, case_id):
    """The process exited with the same code.

    The single most consequential CLI fact, and the one a framework swap is most
    likely to move: see the module docstring on urfave/cli's exit 0 for usage
    errors.
    """
    if cli_pair.field_is_volatile("exit"):
        pytest.skip("exit code was not stable across two runs of State A")
    want = cli_pair.expected.get("exit")
    got = cli_pair.actual.get("exit")
    assert got == want, (
        f"{cli_pair.describe()}: exited {got}, expected {want}\n"
        f"  argv: {' '.join(cli_pair.expected.get('argv', []))}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("case_id", ref.CLI_IDS)
def test_cli_needles_on_expected_streams(cli_pair, case_id):
    """Each needle still appears, and on the same stream(s) as before.

    Stream identity matters because operators redirect the two differently: a
    message that moved from stdout to stderr disappears from anything capturing
    only stdout, even though the text is unchanged.
    """
    if cli_pair.field_is_volatile("needles_on"):
        pytest.skip("needle placement was not stable in State A")
    want = cli_pair.expected.get("needles_on", {})
    got = cli_pair.actual.get("needles_on", {})
    problems = []
    for needle, streams in sorted(want.items()):
        mine = got.get(needle)
        if not mine:
            problems.append(f"{needle!r} appeared on neither stream")
        elif sorted(mine) != sorted(streams):
            problems.append(
                f"{needle!r} was on {sorted(mine)}, expected {sorted(streams)}")
    assert not problems, (
        f"{cli_pair.describe()}: {len(problems)} needle problem(s):\n  "
        + "\n  ".join(problems)
        + f"\n  argv: {' '.join(cli_pair.expected.get('argv', []))}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("case_id", ref.CLI_IDS)
def test_cli_served_matches(cli_pair, case_id):
    """An invocation that started serving still serves, and one that did not, does not.

    This is the CLI-side statement of the same property the HTTP corpus tests from
    the outside: ``--help`` must not bind a port, and a valid flag set must.
    """
    if cli_pair.field_is_volatile("served"):
        pytest.skip("serving behaviour was not stable in State A")
    want = cli_pair.expected.get("served")
    got = cli_pair.actual.get("served")
    assert got == want, (
        f"{cli_pair.describe()}: served={got}, expected {want}\n"
        f"  argv: {' '.join(cli_pair.expected.get('argv', []))}")


# ``reduce_stdout``'s dict is merged into the record at the top level, so which
# fields a case carries is decided by its stdout_mode. Parametrising on presence
# rather than on all 48 ids keeps each test to the cases that actually measured
# the thing it asserts. Measured distribution: 27 ignore, 17 index, 2 flaglist,
# 2 version.
_VERSION_CASES = sorted(c for c, r in ref.CLI.items() if "version_line" in r)
_FLAG_CASES = sorted(c for c, r in ref.CLI.items() if "flags" in r)
_INDEX_CASES = sorted(c for c, r in ref.CLI.items() if "index_text" in r)
_STDERR_CASES = sorted(c for c, r in ref.CLI.items() if "stderr" in r)


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("case_id", _VERSION_CASES)
def test_cli_version_line(cli_pair, case_id):
    """``--version`` prints the same line, with the build revision normalised.

    The version string is a release artifact: the Makefile stamps it, the image
    tags follow it, and G5 checks the same value from the build side. Here it is
    checked from the outside, which is the only place a *user* sees it.
    """
    if cli_pair.field_is_volatile("version_line"):
        pytest.skip("version line was not stable in State A")
    want = cli_pair.expected["version_line"]
    got = cli_pair.actual.get("version_line")
    assert got == want, (
        f"{cli_pair.describe()}: version line is {got!r}, expected {want!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("case_id", _FLAG_CASES)
def test_cli_flag_table(cli_pair, case_id):
    """Every documented flag is still offered, with the same argument shape.

    A flag that vanished breaks every deployment that sets it. The usage text is
    compared too, because several ChartMuseum flags document their environment
    variable there -- ``--storage-amazon-bucket`` is set by ``AWS_BUCKET`` in
    practice far more often than on a command line, and losing that line loses the
    only place the mapping is written down.

    Flags are keyed by their full spelling tuple, so a flag that kept its long
    name but lost its short alias is a difference rather than a match.
    """
    if cli_pair.field_is_volatile("flags"):
        pytest.skip("the flag table was not stable in State A")
    want = {tuple(f["names"]): f for f in cli_pair.expected["flags"]}
    got = {tuple(f["names"]): f for f in (cli_pair.actual.get("flags") or [])}
    missing = sorted(n for n in set(want) - set(got))
    extra = sorted(n for n in set(got) - set(want))
    changed = sorted(
        f"{'/'.join(n)}: arg {got[n]['arg']!r} != {want[n]['arg']!r}"
        for n in set(want) & set(got) if got[n]["arg"] != want[n]["arg"])
    reworded = sorted(
        f"{'/'.join(n)}: usage {got[n]['usage']!r} != {want[n]['usage']!r}"
        for n in set(want) & set(got) if got[n]["usage"] != want[n]["usage"])
    assert not (missing or extra or changed or reworded), (
        f"{cli_pair.describe()}: flag table differs ({len(want)} flags expected)\n"
        f"  missing: {missing}\n"
        f"  unexpected: {extra}\n"
        f"  argument shape changed: {changed}\n"
        f"  usage text changed: {reworded[:5]}")


@pytest.mark.parametrize("case_id", _INDEX_CASES)
def test_cli_index_output(cli_pair, case_id):
    """A subcommand that prints an index prints the same one.

    Both forms are asserted for the reason ``reduce_stdout`` records both: the
    masked text catches a serialisation change, the parsed structure catches a
    semantic one and names the field that moved.
    """
    if not cli_pair.field_is_volatile("index_text"):
        assert cli_pair.actual.get("index_text") == cli_pair.expected["index_text"], (
            f"{cli_pair.describe()}: printed index text differs\n"
            f"  expected {cli_pair.expected['index_text'][:300]!r}\n"
            f"  got      {str(cli_pair.actual.get('index_text'))[:300]!r}")
    if not cli_pair.field_is_volatile("index"):
        assert cli_pair.actual.get("index") == cli_pair.expected["index"], (
            f"{cli_pair.describe()}: printed index structure differs\n"
            f"  expected {cli_pair.expected['index']!r}\n"
            f"  got      {cli_pair.actual.get('index')!r}")


# Defined only if the corpus has a case for it, which today it does not: every
# binary that writes to stderr writes through the logger, and a timestamped line is
# not comparable byte for byte. Those diagnostics are pinned by ``needles`` instead,
# and their stream is pinned by ``test_cli_needles_on_expected_streams``, so nothing
# is unmeasured here.
#
# A guard rather than a deletion: the day the corpus declares such a case, this
# grades it with no further edit. Defining it unconditionally over a sentinel case
# would be the same thing at a cost -- every skip is charged, so the sentinel's id
# would sit in the module's denominator forever, unanswerable by anyone, costing
# every submission one check on a question the corpus does not ask. An empty
# ``parametrize`` list is not the alternative either: pytest collects one skipped
# test for it, which is the same check under a different name.
if _STDERR_CASES:
    @pytest.mark.parametrize("case_id", _STDERR_CASES)
    def test_cli_stderr_exact(cli_pair, case_id):
        """Cases whose stderr is a contract still emit it byte for byte."""
        if cli_pair.field_is_volatile("stderr"):
            pytest.skip("stderr was not stable in State A")
        want = cli_pair.expected["stderr"]
        got = cli_pair.actual.get("stderr")
        assert got == want, (
            f"{cli_pair.describe()}: stderr differs\n"
            f"  expected {want[:300]!r}\n  got      {str(got)[:300]!r}")
