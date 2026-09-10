"""A key this reader does not read, and a bool that is not one.

Every loader in ``config.py`` reaches for its keys with ``raw.get(key, default)``,
which cannot tell a key that was omitted from a key that was misspelled.  Both
return the default, and the default is a working run of a policy the author did
not write: ``behavioural_gate_mn = 65`` graded against 70, ``wieght = 0.26``
normalised as an ordinary module of weight 1.0, ``requried = true`` left a gate
advisory.  No stage can find any of that afterwards -- there is no failure, only a
score computed under different rules than the file states.

The second half is narrower and worse.  ``bool(raw.get(key, default))`` reads as a
coercion and behaves as a truth test on the object, so every non-empty string is
``True``.  TOML's bools are bare words, so quoting one is a plausible slip, and it
is the one wrong value that *inverts* a field rather than breaking it:
``network_allowed = "false"`` ran with the network open on a task that had written
it shut, and ``required = "false"`` made a gate mandatory.  A quoted ``"true"``
would at least have been right by accident; a quoted ``"false"`` is silently the
opposite of the file.

These tests are written against the loaders rather than through the CLI: the
property is which values the readers accept, and a stage in between would only add
ways for a case to pass for the wrong reason.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swerefactor import config


# --------------------------------------------------------------------------- #
# fixtures: the smallest file of each kind that loads
# --------------------------------------------------------------------------- #

EVALUATION = """\
schema = "swerefactor.evaluation/1"
task = "t"
results_dir = "/logs/verifier"

[scoring]
max_score = 100.0
behavioural_points = 40.0
verification_points = 60.0
verification_models = 6
points_per_survived_model = 10.0

[stages.audit]
runner = "swerefactor.audit"
result = "audit.json"
image = "example:1"

[[stages.audit.gate]]
id = "g"
title = "a gate"
question = "is it ported?"
required = true

[stages.behavioural]
runner = "swerefactor.behavioural"
result = "behavioural.json"
suite = "behavioural/suite.toml"
"""

SUITE = """\
schema = "swerefactor.behavioural-suite/1"
task = "t"
lib_dir = "lib"
default_timeout_sec = 900

[[module]]
id = "m"
title = "a module"
weight = 1.0
about = "what it measures"
"""

PROBE = """\
schema = "swerefactor.verification-probe/1"
task = "t"
prompt = "prompt.txt"
candidate_command = ["run"]

[[adversary]]
id = "a"
model = "claude-opus-5"
driver = "anthropic"
budget_sec = 3600

[scope]
allow = ["the public API"]
deny = ["timing"]
require_deterministic = true
reruns = 3
network_allowed = false
"""

# Which loader reads which fixture, and how to call it.
KINDS = {
    "evaluation.toml": (EVALUATION, lambda p: config.Evaluation.load(p)),
    "suite.toml": (SUITE, lambda p: config.Suite.load(p)),
    "probe.toml": (PROBE, lambda p: config.Probe.load(p)),
}


def _load(tmp_path: Path, kind: str, *, edit: tuple[str, str] | None = None):
    """Write one fixture, optionally with a single anchored substitution."""
    text, loader = KINDS[kind]
    if edit is not None:
        old, new = edit
        assert text.count(old) == 1, f"{kind}: {old!r} is not a unique anchor"
        text = text.replace(old, new)
    path = tmp_path / kind
    path.write_text(text, encoding="utf-8")
    return loader(path)


def test_the_fixtures_load_unedited(tmp_path):
    """The control.  Every case below is one substitution away from this."""
    for kind in KINDS:
        _load(tmp_path, kind)


# --------------------------------------------------------------------------- #
# an unknown key is an authoring bug, in each closed table
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("kind,old,new,names", [
    # The top level of each of the three files.
    ("evaluation.toml", 'results_dir = "/logs/verifier"',
     'reslts_dir = "/logs/verifier"', "results_dir"),
    ("suite.toml", 'lib_dir = "lib"', 'libdir = "lib"', "lib_dir"),
    ("probe.toml", 'prompt = "prompt.txt"', 'prmopt = "prompt.txt"', "prompt"),
    # [scoring].  Every figure here is cross-checked against another -- stage 2 plus
    # stage 3 is the maximum, and six survivors at their per-round rate is stage 3 --
    # so a misspelling drops a declared value back to the default and the arithmetic
    # closes on the default instead of failing.  The task then runs on a policy its
    # own file does not state.
    ("evaluation.toml", "verification_points = 60.0", "verification_ponts = 60.0",
     "verification_points"),
    ("evaluation.toml", "points_per_survived_model = 10.0",
     "points_per_survivd_model = 10.0", "points_per_survived_model"),
    # [[gate]] -- a misspelled `required` leaves the hard gate with nothing in it.
    ("evaluation.toml", "required = true", "requried = true", "required"),
    # [[module]] -- two letters that rewrite a suite's arithmetic.
    ("suite.toml", "weight = 1.0", "wieght = 1.0", "weight"),
    # [scope] -- the mechanical half of what an adversary may do.
    ("probe.toml", "reruns = 3", "rerun = 3", "reruns"),
    ("probe.toml", "network_allowed = false", "network_alowed = false",
     "network_allowed"),
])
def test_a_misspelled_key_is_named_and_not_defaulted(tmp_path, kind, old, new,
                                                     names):
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, kind, edit=(old, new))
    msg = str(exc.value)
    assert "unknown key" in msg, msg
    # The suggestion is the point: the author is looking at a file that reads
    # correctly to them, so the message has to name the key they meant.
    assert names in msg, f"no suggestion of {names!r} in: {msg}"


def test_an_unknown_key_with_no_near_match_is_still_rejected(tmp_path):
    """Not only typos.  A key from another benchmark's schema.
    """
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, "evaluation.toml",
              edit=("[scoring]", "[scoring]\nabsent_module_scores_zero = false"))
    msg = str(exc.value)
    assert "unknown key" in msg and "absent_module_scores_zero" in msg, msg
    # Nothing reads that key, and a module that recorded nothing scores 0.0 whatever
    # it is set to.  A task still declaring it is told so, rather than quietly graded
    # under the behaviour it means to switch off.


# --------------------------------------------------------------------------- #
# the two tables that keep their extra keys, and still catch a near miss
# --------------------------------------------------------------------------- #

# `[stages.*]` hands `image`, `samples`, `scan`, `model` and the rest to a runner
# this reader knows nothing about, and `[[adversary]]` hands over
# `driver_options`.  Rejecting unknown keys there would mean editing config.py to
# add a stage option, which is the coupling the bag exists to avoid.

@pytest.mark.parametrize("line", [
    'image = "example:2"',
    "samples = 3",
    'scan = "audit/scan.toml"',
    'model = "claude-opus-5"',
    'prompt = "verifier/prompt.txt"',
    'context = "verifier/context"',
    'driver = "anthropic"',
    "driver_options = { effort = \"high\" }",
    "allowed_commands = [\"ls\"]",
    "max_turns = 40",
])
def test_a_stage_option_is_carried_not_rejected(tmp_path, line):
    """Every option key the twenty shipped tasks actually use."""
    ev = _load(tmp_path, "evaluation.toml",
               edit=("[stages.behavioural]", f"[stages.behavioural]\n{line}"))
    key = line.split("=")[0].strip()
    assert key in ev.stage("behavioural").options, (
        f"{key} was dropped instead of carried to the runner")


@pytest.mark.parametrize("kind,anchor,bad,meant", [
    # A bag still cannot absorb a near miss of a key the *reader* reads: this
    # lands in `options`, where nothing looks for it, and the stage runs on the
    # default hour with no sign that a timeout was ever declared.
    ("evaluation.toml", "[stages.behavioural]", "timeout_secs = 60", "timeout_sec"),
    ("evaluation.toml", "[stages.behavioural]", 'reslt = "f.json"', "result"),
    ("evaluation.toml", "[stages.behavioural]", "enable = false", "enabled"),
    ("probe.toml", "[[adversary]]", "budget_secs = 60", "budget_sec"),
])
def test_a_near_miss_in_a_bag_is_rejected_with_its_suggestion(tmp_path, kind,
                                                              anchor, bad, meant):
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, kind, edit=(anchor, f"{anchor}\n{bad}"))
    msg = str(exc.value)
    assert "unknown key" in msg and meant in msg, msg


def test_the_real_option_keys_are_not_near_misses_of_anything(tmp_path):
    """The bag check is only safe if it misfires on none of them.

    Measured against the loaders' own key lists rather than a copy of them, so a
    key added to ``KNOWN`` later cannot silently start rejecting a stage option
    that has been carried for twenty tasks.
    """
    import difflib
    shipped_stage_options = (
        "allowed_commands", "context", "driver", "driver_options", "image",
        "max_turns", "model", "prompt", "samples", "scan", "suite", "probe",
        "adjudicator",
    )
    for key in shipped_stage_options:
        assert not difflib.get_close_matches(
            key, config.StageConfig.KNOWN, n=1, cutoff=0.8), (
            f"{key!r} is within reach of {config.StageConfig.KNOWN}, so a real "
            f"stage option would be rejected as a typo")
    assert not difflib.get_close_matches(
        "driver_options", config.Adversary.KNOWN, n=1, cutoff=0.8)


# --------------------------------------------------------------------------- #
# a bool has to be a bool
# --------------------------------------------------------------------------- #

# `suite.toml` declares no bool field, so the rows below cover `evaluation.toml` and
# `probe.toml` only.  A module's `required` is refused rather than read -- see
# `test_required_on_a_module_is_refused_not_ignored`.  The guard itself is one
# helper, `config._bool`, so the property is about that helper and not about any one
# file.
@pytest.mark.parametrize("kind,old,new", [
    # The quoted form, which was True and therefore the opposite of the file.
    ("evaluation.toml", "required = true", 'required = "false"'),
    ("probe.toml", "require_deterministic = true",
     'require_deterministic = "false"'),
    ("probe.toml", "network_allowed = false", 'network_allowed = "false"'),
    ("evaluation.toml", "[stages.behavioural]",
     '[stages.behavioural]\nenabled = "false"'),
    # A number, which has the same problem in the other direction: 0 is False and
    # 1 is True, so a field could be set by arithmetic.
    ("probe.toml", "network_allowed = false", "network_allowed = 1"),
    ("evaluation.toml", "required = true", "required = 1"),
    # And a list, which is neither but is truthy when non-empty.
    ("probe.toml", "network_allowed = false", 'network_allowed = ["yes"]'),
])
def test_a_bool_field_rejects_what_is_not_a_bool(tmp_path, kind, old, new):
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, kind, edit=(old, new))
    assert "must be true or false" in str(exc.value), str(exc.value)


def test_required_on_a_module_is_refused_not_ignored(tmp_path):
    """`required` on a module is an error, and says why rather than "unknown key".

    Dropping it from ``KNOWN`` alone would report it as a misspelling, and the
    author's next move on "unknown key" is to hunt for the correct spelling of a
    key that does not exist at this level.  Worse, ignoring it would leave a suite
    whose file singles out a mandatory module being graded by a stage that treats
    every weighted module as mandatory -- the same class of silent divergence the
    rest of this file is about, with the file and the arithmetic disagreeing and
    nothing raised.
    """
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, "suite.toml",
              edit=('about = "what it measures"',
                    'about = "what it measures"\nrequired = true'))
    msg = str(exc.value)
    assert "not a module key" in msg, msg
    # And says what does express importance, so the author has somewhere to go.
    assert "weight" in msg, msg
    # Named, so the author can find it in a suite of fifteen modules.
    assert "[m]" in msg, msg
    # And not reported as a typo, which would send the reader looking for a
    # spelling rather than for what the key was reaching for.
    assert "unknown key" not in msg.lower(), msg


def test_a_quoted_false_used_to_open_the_network(tmp_path):
    """The inversion, stated as the thing it did rather than as a type error.

    ``network_allowed`` is the field where the slip opened something instead of
    closing it.  Nineteen of the twenty tasks declare it ``false``; the twentieth
    leaves it to a default of ``false``.  A quoted ``"false"`` in any of them ran
    stage 3 with the network reachable, on a task whose file says it is not.
    """
    with pytest.raises(config.ConfigError):
        _load(tmp_path, "probe.toml",
              edit=("network_allowed = false", 'network_allowed = "false"'))
    # And the honest value still loads as itself, so the guard is about the type
    # and not about the field being unsettable.
    probe = _load(tmp_path, "probe.toml",
                  edit=("network_allowed = false", "network_allowed = true"))
    assert probe.scope.network_allowed is True


def test_a_bool_that_is_a_bool_still_reads_as_itself(tmp_path):
    """The other direction, on every field the guard now covers.

    A type check that rejected the honest values too would pass every test above
    and break all twenty tasks, so each field is read back rather than merely
    loaded.
    """
    probe = _load(tmp_path, "probe.toml",
                  edit=("require_deterministic = true",
                        "require_deterministic = false"))
    assert probe.scope.require_deterministic is False

    ev = _load(tmp_path, "evaluation.toml",
               edit=("[stages.behavioural]", "[stages.behavioural]\nenabled = false"))
    assert ev.stage("behavioural").enabled is False
    assert ev.stage("audit").gates[0].required is True

    # The one field whose honest `false` is rejected for a different reason: this
    # file declares one gate, so clearing `required` leaves the hard gate with
    # nothing that can fail it.  That rule predates this change and the message
    # has to stay distinguishable from a type error, or an author reading it goes
    # looking for a quoting mistake.
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, "evaluation.toml",
              edit=("required = true", "required = false"))
    msg = str(exc.value)
    assert "no gate is required" in msg, msg
    assert "must be true or false" not in msg, msg


# --------------------------------------------------------------------------- #
# an exit code has to be one, and two of them are not faults
# --------------------------------------------------------------------------- #
#
# ``[scope].fault_exit_codes`` says which statuses from the candidate command mean
# the tree could not be tested.  It is a list of numbers with no natural range, and
# a wrong number here is not a broken run: the stage completes, every round reaches
# a verdict, and the verdicts are about something else.


def _fault_codes(tmp_path: Path, value: str):
    return _load(tmp_path, "probe.toml",
                 edit=("network_allowed = false",
                       f"network_allowed = false\nfault_exit_codes = {value}"))


def test_a_list_of_exit_codes_loads_as_itself(tmp_path):
    """The control, and the shape a task overriding the default would write."""
    probe = _fault_codes(tmp_path, "[71, 99]")
    assert probe.scope.fault_exit_codes == [71, 99]


def test_the_field_is_absent_from_every_task_and_defaults_to_empty(tmp_path):
    """Which is what makes the empty case the one that actually runs."""
    probe = _load(tmp_path, "probe.toml")
    assert probe.scope.fault_exit_codes == []


def test_zero_as_a_fault_code_would_pay_every_submission_thirty(tmp_path):
    """The expensive slip, refused by name rather than by range.

    ``0`` is a candidate that passed. Called a fault, every candidate becomes
    undecidable, no round can find anything, and all six survive: the full sixty
    points, to any submission, from one number in a file nobody re-reads. Nothing
    downstream would look wrong -- the stage completes and writes six passes.
    """
    with pytest.raises(config.ConfigError) as exc:
        _fault_codes(tmp_path, "[0, 71]")
    msg = str(exc.value)
    assert "candidate's own verdict" in msg, msg
    assert "a pass" in msg and "survival" in msg, msg


def test_one_as_a_fault_code_would_decide_nothing(tmp_path):
    """The mirror, which costs nothing and is equally not a fault: ``1`` is a
    candidate whose assertions failed, which is the ordinary way to find a defect."""
    with pytest.raises(config.ConfigError) as exc:
        _fault_codes(tmp_path, "[1]")
    msg = str(exc.value)
    assert "candidate's own verdict" in msg, msg
    assert "a failure" in msg, msg


@pytest.mark.parametrize("value,expect", [
    # Not a list.  A single number is the plausible slip, and `71 in 71` raises
    # where `71 in [71]` would not, so this fails late rather than at load.
    ("71", "must be a list"),
    ('"71"', "must be a list"),
    # A string inside the list, which compares unequal to every exit code forever:
    # no error, no fault ever recognised.
    ('[71, "72"]', "not an exit code"),
    # A bool, which Python is happy to compare against an int -- `True == 1` -- so
    # `[true]` would silently mean `[1]`, the case refused above.
    ("[true]", "not an exit code"),
    ("[71.0]", "not an exit code"),
    # Outside what a process can return.  256 is the one that looks right: a shell
    # truncates it to 0, so the file would be naming the code that pays sixty.
    ("[256]", "cannot exit with it"),
    ("[-1]", "cannot exit with it"),
])
def test_a_fault_code_that_is_not_one_is_named(tmp_path, value, expect):
    with pytest.raises(config.ConfigError) as exc:
        _fault_codes(tmp_path, value)
    assert expect in str(exc.value), str(exc.value)


def test_a_misspelled_fault_code_key_is_not_silently_no_faults(tmp_path):
    """The default is a working policy, so this key has the same failure mode as
    every other one in this file: the run is fine and the file is not honoured."""
    with pytest.raises(config.ConfigError) as exc:
        _load(tmp_path, "probe.toml",
              edit=("network_allowed = false",
                    "network_allowed = false\nfault_exit_code = [71]"))
    msg = str(exc.value)
    assert "unknown key" in msg and "fault_exit_codes" in msg, msg


def test_the_key_list_does_not_travel_inside_the_objects(tmp_path):
    """``KNOWN`` is the loader's, not the stage's.

    Declared without ``ClassVar`` it was a dataclass *field* on ``StageConfig``
    and ``Adversary``: in ``__init__``, in ``__eq__``, and printed by every
    ``repr`` of a stage or an adversary.  The other seven tables gained a
    ``KNOWN`` in the same change, so getting the annotation wrong here would have
    put nine copies of the loader's key lists into the objects it returns.
    """
    tables = (config.ScoringPolicy, config.Gate, config.StageConfig,
              config.Evaluation, config.ModuleConfig, config.Suite,
              config.Adversary, config.ScopeRules, config.Probe)
    for cls in tables:
        names = [f.name for f in dataclasses.fields(cls)]
        assert "KNOWN" not in names, (
            f"{cls.__name__}.KNOWN is a dataclass field; annotate it ClassVar")
        assert isinstance(cls.KNOWN, tuple) and cls.KNOWN, (
            f"{cls.__name__}.KNOWN is missing or empty")
    assert "KNOWN" not in repr(config.Adversary(id="a", model="m"))
