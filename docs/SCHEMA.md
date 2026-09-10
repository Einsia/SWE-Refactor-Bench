# SWERefactorBench task schema

A task is a directory. Everything a grader needs is inside it; everything an
agent may see is in exactly two of its subdirectories.

```
tasks/<task-id>/
├── task.toml                     metadata, public config, resource limits, State A/B
├── instruction.md                what to accomplish  (agent-visible)
├── score.py                      the three-stage ladder, per task
├── environment/                  the frozen development environment  (agent-visible)
│   ├── Dockerfile
│   ├── original.tar.gz           State A, frozen
│   └── original.sha256
└── tests/                        never exposed to the agent
    ├── evaluation.toml           orchestration: stages, weights, timeouts
    ├── audit/
    │   ├── .dockerignore         identical in all three stage directories
    │   ├── prompt.txt            this task's audit review, in prose
    │   └── Dockerfile
    ├── behavioural/
    │   ├── .dockerignore
    │   ├── Dockerfile
    │   ├── suite.toml            module manifest and scoring config
    │   ├── lib/                  shared helpers the modules import
    │   ├── data/                 ground truth, frozen at authoring time
    │   └── modules/<id>/         one directory per capability under test
    └── verification/
        ├── .dockerignore
        ├── prompt.txt            the brief handed to each attacking model
        ├── probe.toml            the six models, their budgets, the scope
        ├── run-candidate.sh      runs one candidate against both trees
        ├── check-probe.py        authoring-time smoke test of this stage
        ├── lib/                  the capability surface a candidate writes against
        ├── data/                 whatever a candidate needs that the image lacks
        └── Dockerfile
```

Each of the three stage directories is a Docker build context, and all three follow
the same rule: **`lib/` and `modules/` are code the stage runs, `data/` is input it
only reads, manifests sit at the top level.** Nothing else is a directory there.
`swerefactor validate` enforces it, which is what keeps "is this file executed, or
only read?" answerable from a path rather than by reading a Dockerfile.

`environment/` and `instruction.md` are what the agent gets. `tests/` is built
as a separate image (`environment_mode = "separate"` in `task.toml`) so the
suite is not merely unread but absent from the container the agent works in.

Alongside the tasks:

```
infra/
├── Dockerfile                    the donor image: swerefactor/infra:1
├── swerefactor/                  the shared ladder
└── tests/                        its unit tests
```

The three stage images do not vendor the grader; they copy it out of the donor
image, which is why one `docker build infra` precedes them:

```dockerfile
COPY --from=infra /opt/swerefactor /opt/swerefactor
```

Two directories by convention inside every stage image: `/opt/swerefactor` is the
shared grader, `/opt/assets` is whatever that task's stage ships with it. Keeping
them apart is what lets the donor image be rebuilt without touching a task, and a
task's assets be changed without rebuilding the grader.

Neither name appears in an agent image. What State A leaves behind for the agent
goes under `/opt/state-a` — lang01 puts its source contract and baseline commit
hash there. A grader-named directory in front of the agent is at best confusing and
at worst a hint about how it is being marked.

## The ladder

Three stages, in order, and each one can stop the run.

| stage | what it asks | worth | on failure |
|---|---|---|---|
| 1 audit | did the migration really happen | pass/fail | **score 0** and stage 3 skipped; stage 2 is still run, and reported as uncredited |
| 2 behavioural | is the rewrite complete | **40**, or none | one failed scored check pays **0** for the stage, and stage 3 is skipped |
| 3 verification | can 6 models find a test it fails | **60** | each model that finds nothing pays **+10** |

Maximum 100, and in full:

```
S_task = 1[stage 1 passed] x ( 40 x 1[stage 2 complete]
                             + 60 x 1[stage 2 complete] x survived / 6 )
```

so the reachable scores are **0, 40, 50, 60, 70, 80, 90 and 100** and nothing in
between. "Complete" means every scored check in every weighted module passed;
there is no partial credit anywhere in the ladder.

The numbers live in `[scoring]` in `evaluation.toml`; the defaults above are the
benchmark's published policy, and `ScoringPolicy.from_dict` refuses a table whose
arithmetic does not close
(`points_per_survived_model × verification_models == verification_points`, and
`behavioural_points + verification_points == max_score`).

Two properties hold at every layer, and most of the design follows from them:

**Status is not verdict.** "The stage did not run" and "the submission failed"
are different facts with different consequences. A verifier that ran out of
memory, an adversary whose API call failed, a module whose container died —
these produce `status: error` and a `valid: false` verdict that says re-run,
never a published zero. A submission is only zeroed by evidence about the
submission.

A module that exceeded its own `timeout_sec` is the one exception, and it is an
application of that last sentence rather than a relaxation of it. The budget is
the task author's declaration in `suite.toml`, the reference tree is required to
meet it on the same machine, and `behavioural.py` hands the module
`SRB_MODULE_TIMEOUT_SEC` and `SRB_MODULE_DEADLINE` so that overrunning is a thing
the module was told about. So `status: timeout` scores the module 0.0, keeps its
weight, and publishes: a tree that needs 3000s where the reference needs 0.1s has
been measured, and lang02's `streaming` stalling 8 of 183 cases on 64KB fed
byte-at-a-time through `deflate()` is a finding about a library, not an absence of
one. `scoring._MEASURED_STATUSES` is the whole carve-out — `{"ok", "timeout"}` —
and every other non-ok status still refers the run back. A module killed by a
signal is the case that keeps the two apart: SIGKILL is nearly always the OOM
killer, which is a fact about the machine.

Whether a *partial* timeout is scored on what it found is up to the module: the
runner's `wait` is still the limit, so a module that wants its recorded checks
published has to reserve a slice against `SRB_MODULE_DEADLINE` and write inside
it. One that ignores the deadline is killed exactly as before and reports nothing,
which is scored 0.0 for the same reason.

The **stage** clock is one level up and is enforced from outside, by `docker kill`,
so no module gets told about it. `SuiteRunner` therefore writes its result after
every module rather than once at the end, and seeds a row for every declared module
before the first one starts — so the file on disk describes the whole suite at every
instant, and a stage killed mid-run leaves the modules that finished carrying their
measurements and the rest carrying `status: unreached`. Before that, the single
write at the end was unreachable once the kill landed: lang04/max lost ten measured
modules (`build` 4/4, `structure` 14/17, `provenance` 4/6 and seven more) to record
nothing at all, and its report said `NOT RUN` over three hours of grading.

`unreached` is not a measurement and is not in `_MEASURED_STATUSES`. It is
*charged*: `_CHARGED_STATUSES` adds it, so the module scores 0.0 and keeps its
weight instead of vanishing from the denominator — otherwise a submission that
exhausts the stage clock on module 2 would have modules 3..N cost it nothing, which
makes hanging cheaper than failing. The charge has no exceptions left. An unreached
module costs its own share and nothing more — there is no module whose zero can void
another's result — and a stage where **every** module is unreached publishes 0.0
rather than being referred back. Nothing in the artifact separates a tree that ate the
clock from an image that hung on startup, but a referral is not a neutral act: it
reads as "we do not know" while costing exactly what a zero costs, and the tree does
not come back for a second grading. So the cause is written into the notes, where a
cause belongs, and it does not decide whether the number exists.

**Nothing scores by string matching.** No task carries a pattern list that decides
a score. The argument against such rules is in two parts: a rule forbidding a token
measures the absence of a string rather than the completeness of a migration, and
the rule list itself is a map of what to hide. Both halves are fixed by asking a model to read the two
trees and say what it found, with citations that the runner re-opens and
confirms.

## `task.toml`

Harbor's task descriptor (`schema_version = "1.4"`), plus the fields
SWERefactorBench reads:

```toml
[metadata]
category = "language-rewrite"   # language-rewrite | framework-rewrite
                                # platform-rewrite | build-toolchain
difficulty = "frontier"
state_a = "cmark 0.31.1, C99, CMake+autotools"
state_b = "the same library in Rust, no external crates"
```

What crosses the boundary from the agent container is declared as a table, not a
bare list, because the exclusions matter as much as the path:

```toml
[[artifacts]]
source = "/workspace/repo"
exclude = ["target", "build", "*.o", "__pycache__"]
```

Always the repository, never a build tree. The `exclude` list is what makes "the
old build system is gone" a fact about the source rather than a claim about what
was left in an output directory — every stage that needs an artifact builds one
from the submitted source. It has to be written per task and read twice: a
directory that is both build output and delivered content must be excluded file
by file rather than wholesale. fw04 is the worked example and the measured one —
`*.tgz` matched `testdata/charts/badcharts/mybadchart/mybadchart-1.0.0.tgz`,
which State A ships *because* helm refuses to build it, so every submission was
charged for a file the collector removed: complete when the tree was handed to
the stage directly, one check short when the same tree was collected through the
manifest, complete again once the pattern became `testdata/charts/*/*.tgz`.  Under
an all-or-nothing stage that one check is the difference between 40 points and
nothing, which is what makes an over-broad `exclude` pattern a scoring bug rather
than a rounding error. `swerefactor validate` and
`infra/tests/test_layout.py::test_no_artifact_exclusion_deletes_something_state_a_ships`
both check this against State A's own file list, under both matching rules.

Sometimes there is no narrower pattern and the exclusion has to be given up
instead. pf02 is that case: a bare name matches at any depth — as a path segment
under `validate`, as a path suffix under GNU `tar` — so its `node_modules` took
`test/cases/import.lookup/node_modules/` too, three committed files that are the
subject of a graded case. `node_modules/*` matches the fixture as well;
`./node_modules` anchors only when the collector stores members with a `./`
prefix, which is Harbor's argv and not in this repository; `--anchored` is a tar
flag, which a list of patterns has nowhere to put. Enumerating the install would
have meant naming State A's 89 top-level packages in a contract a *port* is
allowed to renegotiate. The pattern was dropped, on the measurement that no
stage depended on it — each one excludes the install where it can say "top
level" and the manifest cannot. Collecting 8.3 MB nothing grades is the cheaper
error, and the general rule is the one that decided it: a pattern has to be safe
under either reading, because the wrong guess deletes a submission rather than a
build.

The agent phase declares what it is given, and both fields are read — by
`swerefactor validate`, and by the driver that launches the run:

```toml
[agent]
harness = "codex-cli 0.146.0"    # one of the pinned clients; validate rejects others
timeout_sec = 32400.0            # the wall clock the driver must be launched with
network_mode = "no-network"
user = "agent"
```

A run measures a model *and* a client: what a tool call records, whether a round
has a token account, whether a stalled model gets nudged and what the nudge said,
and whether the transcript the model reads back is the one it wrote are all
properties of the driver. The first twenty-run corpus used five clients in seven
log formats and cannot answer those questions uniformly, which is a comparison
problem no amount of care in scoring fixes.

The suite pins one client per model family — `PINNED_HARNESSES` in `config.py`:

| family | pinned harness |
|---|---|
| gpt | `codex-cli 0.146.0` |
| claude, deepseek, glm, kimi, qwen | `claude-code 2.1.220` |

**`harness` here is a declaration about the file, not about the run.** Which client
drives a trial is a property of the trial — it follows from the model, which
`task.toml` does not name — so a static string in a task file cannot state it. What
this field is for is that a task is self-describing and a drifting one fails
validation: `validate` checks it against the set of pinned clients, and rejects an
unpinned name or a pinned client at an unpinned version. All twenty shipped tasks
declare the gpt pin, which is both the suite default and the client the archived
corpus was driven with.

What actually drove a run is settled at launch rather than by this field. Each
adapter reads the installed client's own `--version` and refuses to start when it
is not the pin for the family it drives — `codex harness is …, benchmark pins …` —
so a family driven by another family's client fails the phase instead of producing
a score. That check and this field answer different questions: this one says what
the task expects, that one says what ran.

This is not the `harness` that appears on a stage result. Two different things
carry that name in this schema, and both end up in JSON a reader compares across
runs: `[agent].harness` here is the **client that drove the submission**, a
string, and it answers "what produced this tree". `harness` on a
`StageResult`, described under the result envelope below, is the **grader that
scored it**, an object of `{fingerprint}`, and it answers "what produced
this number". A submission and its score can disagree about either one
independently — the same submission can be regraded by a newer grader, and one
grader can score runs from two clients — so neither field substitutes for the
other and nothing derives one from the other.

`timeout_sec` is the one figure a driver must honour, and the adapters derive the
loop's own deadline from it so that a run stops itself and records why rather than
being cut mid-round. A budget the driver is not actually given is worse than a
wrong one: a run stopped part-way through a port scores 0.0, which is
indistinguishable from a model that cannot do the task at all. Declare a budget
the driver will actually be given.

The rest of the resource limits and the environment's `env` table are Harbor's,
and Harbor's means read by nothing in this repository: `[environment]`,
`[verifier]` and their `build_timeout_sec`, `cpus`, `memory_mb` and `network_mode`
are declared here for the harness that runs the containers, and no `swerefactor`
code path parses them. Worth stating, because a field with no reader in the tree
looks like a field whose reader was lost. There are two exceptions above:
`[[artifacts]]`, which `validate` reads against State A's file list, and
`[agent]`, whose four keys are the four `AgentPhase` reads — so a fifth, or a
near-miss of one of the four, is rejected rather than defaulted.

The verifier's `timeout_sec` has to cover the whole ladder, so it is at least the sum
of the three stage timeouts in `evaluation.toml` — a ceiling rather than an
expectation, since most submissions stop at stage 1 or short of stage 2's full
marks. `swerefactor validate` checks that sum against it and reports the margin. Both
are worth doing, because the two numbers live in different files that no single
runner reads together, and the failure is quiet at either size: a ceiling under the
sum is a verifier Harbor kills mid-ladder, and a ceiling only just over it is legal
and still leaves the image builds nowhere to go.

The margin above the sum is for the three image builds and the ladder's own
overhead, and it is not a bound on `build_timeout_sec`: the convention across the
tasks is 5400s of margin, while a single verifier image is allowed 7200s. What
the margin covers is what a build normally costs, not what one is permitted to.

Nothing about scoring belongs here: a reader of `task.toml` should learn what the
task is, not how many points a module is worth. No task ships a `solution/` tree
or a `[solution.env]` table, and each `task.toml` says why where that table would
have been — a reference implementation is something that has to be kept correct
against every future change to the suite, and its only consumer would be a smoke
test that the suite can be passed at all. The gate already answers that: stage 3
is asked only of a submission that scored stage 2 in full. What six models add is
the other half — an hour each spent hunting for a behaviour nobody thought to
record.

## `instruction.md`

What to accomplish and what the public contract is. Not the scoring rules, and
not a list of what the grader checks — an instruction that enumerates checks is
an instruction to satisfy checks. It states the goal, the constraints that are
real (offline, no new dependencies, the ABI stays), and where the oracle is.

The oracle is deliberate: the agent may run State A and compare against it as
much as it likes. What it must not have is the grader's own gate list, which is
why `environment/` ships no self-check against it.

## `environment/`

`original.tar.gz` is State A, frozen, with its digest in `original.sha256`. One
artifact feeds three consumers: the agent's initial workspace, the oracle it can
diff against, and `/opt/original` inside the verifier. Freezing it once is what
makes "the original passed this and the submission does not" a statement about
the submission.

The `Dockerfile` builds the development environment: the toolchain, the offline
dependency set, the oracle. It must not contain a scoring rule. A rule in the
environment is a rule the agent can read.

There is one thing that looks like an exception and is not. Most environment
Dockerfiles run an environment self-check — `verify_environment.sh` in the build
and platform tasks, `verify_environment.py` in the `lang` ones — that builds State
A its own documented way and fails the image build if that does not work offline.
An environment that cannot build the thing it hands over is broken, and it should
break at authoring time rather than mid-trial. Eight tasks have none at all; there
is no requirement to have one, only a requirement that if one exists it is this
and not a rubric.

The distinction that matters:

* it runs at **image-build time**, as a `RUN` step, and is usually deleted
  afterwards — the six `.sh` ones with an `rm -f` in the same layer, and `lang06`
  by removing the whole staging directory and asserting it is gone. Five
  `lang` tasks (`lang01` through `lang05`) do not delete theirs, so it is still on
  disk in the agent's container, under `/run/state-a/`, `/opt/state-a/` or
  `/opt/swerefactor/`. Nothing tells the agent it is there, and nothing runs it
  again;
* it asserts facts about the **environment** — the toolchain resolves offline, the
  forbidden plugins do not, State A's own build produces its own artifacts — and
  never a fact about a submission, which does not exist yet. That is what makes
  the retained copies harmless rather than a self-check in disguise: they take
  State A's path and its contract and assert State A's own file census against it,
  so pointed at a submission they fail on the premise rather than reporting
  anything about the port;
* it reports **nothing** to the agent. No grade, no gate list, no score.

The asymmetry is history rather than design — the first `lang` tasks were authored
against a `RUN python3 …` line and nobody added the delete; `lang06` added it.
Either shape satisfies the rule above. A new task should delete it,
because "not in the image" needs no argument and "in the image but inert" needs the
paragraph you just read.

A self-check the agent can run to find out whether it would pass is the thing this
schema does not have, and removing it was the point of the restructuring. The
agent gets the oracle instead: run State A, diff against it, as much as it likes.
That is an input. A verdict is not.

## `tests/evaluation.toml`

Per-task orchestration. `schema = "swerefactor.evaluation/1"`.

```toml
schema = "swerefactor.evaluation/1"
task = "swerefactor/lang01-cmark-c-to-rust"
results_dir = "/logs/verifier"

[scoring]
max_score = 100.0
behavioural_points = 40.0          # paid whole, or not at all
verification_points = 60.0
verification_models = 6
points_per_survived_model = 10.0

[stages.audit]
runner = "audit"
result = "audit.json"
timeout_sec = 5400.0
prompt = "audit/prompt.txt"
model = "claude-opus-5"
samples = 3                        # majority over independent reviews
image = "swerefactor/lang01-audit:1"
context = "audit"       # build context, relative to this file

[[stages.audit.gate]]
id = "old_implementation_retired"
title = "The C implementation no longer runs"
question = """Does the library that gets built and installed contain the
original C sources, or link against them?  A repository that keeps them as
reference material is fine; one whose default build path still compiles them
has not migrated."""
required = true

[stages.behavioural]
runner = "suite"
result = "behavioural.json"
suite = "behavioural/suite.toml"
image = "swerefactor/lang01-behavioural:1"
context = "behavioural"

[stages.verification]
runner = "probe"
result = "verification.json"
probe = "verification/probe.toml"
enabled = true
image = "swerefactor/lang01-verification:1"
context = "verification"
```

Each stage declares its own `image` and `context`. They are per stage rather than
per task because the stages need different things to be true of them, and
`swerefactor validate` requires all three to exist and to be distinct — one image
serving two stages means one of them has a capability it was designed not to
have.

The capability asymmetry is the design, not an accident of packaging:

| stage | has | does not have | why |
|---|---|---|---|
| 1 audit | code-reading tools, read-only original | the build system, often any compiler | a reviewer that can build grades a build log; "assembled by rule or by transcribed list" is not a question a build log answers |
| 2 behavioural | the new toolchain | the old engine — asserted absent at image-build time | a repository that still needs the old one is unbuildable here by construction |
| 3 verification | **both** build systems | — | "the original passes this and yours does not" has to be executed against both |

A stage's `timeout_sec` is how a task states its share of the verifier phase, so
the ceiling above can be derived rather than guessed, and `validate` checks the sum
against it. `swerefactor ladder` also applies it, as the wall clock on that stage's
container. Harbor enforces `[verifier].timeout_sec` over the whole verifier and
never reads this file, so without a per-stage clock one wedged stage would spend
what the two after it needed and the trial would end with no `reward.json` at all —
a harness error where a score belonged.

It is a hang detector rather than a quota, and the distinction is load-bearing
because the budgets underneath it sum higher. A module's `timeout_sec` in
`suite.toml` and an adversary's `budget_sec` in `probe.toml` are per-unit ceilings,
and summed they exceed the stage figure in all twenty tasks — fw04's fifteen
modules are allowed 44400s inside a stage declaring 10800 — because a module that
finishes early returns early, exactly as stage 3's 54000 sits above six rounds of
3600. The consequence to know: a stage that really spends its declared total is
killed at it, so a submission whose modules were each inside budget can still lose
stage 2 to the clock. That outcome is reported as the stage's, and what it
indicts is the task's arithmetic rather than the submission.

Gates are declared here as well as argued in the prompt, for one reason: the
scorer has to know which are mandatory, and deriving that from the prompt's
prose would mean deciding whether a submission scores zero by parsing English.
A gate with `required = false` is reported and reasoned about but does not
block — the place for an observation worth knowing and not worth voiding a
submission over. A file where no gate is required is rejected at load time.

### Which stage a question belongs to

The suites this schema replaced put every audit question in one pass, and
they were not one kind of question. Sorting them was most of the restructuring
work, and the rule that fell out is worth stating, because it is the rule for
adding a check to a new task:

* **Observations belong to stage 2.** A PATH stub either recorded a call to `ant`
  or it did not; a source file's sha256 either matches State A's or it does not;
  an installed pom either declares a dependency or it does not. These cannot be
  argued with, and as stage-2 modules they are *reported* one by one — "three
  entries in the wrong jar" and "there is no build system here" score the same
  zero, and only a per-module report tells a reader which one happened. A stage 1
  gate would collapse them into a single `fail` and lose that.
* **Judgements belong to stage 1.** Whether a build "hard-codes the release
  label", whether nine jars are cut by rules over paths or by a transcribed list
  of 2,858 entry names, whether a wrapper delegates back to the old system in
  spirit — these need something that can read a build system and follow what it
  does.
* **Behaviour belongs to stage 2, and its gaps to stage 3.** The corpus a task
  ships measures what the author thought to freeze. Stage 3 is how the rest gets
  found.

A check that looks for a *string* is almost always in the wrong stage. The
clearest case in the original suite was a seven-element regex list hunting for
`SRB-` and `/tests/`, which any submission that renamed its variables walked
through untouched.

Unknown keys under `[stages.*]` land in `options` and are passed to the runner,
so a stage can take a setting the loader has never heard of. Every table also
accepts a `metadata` sub-table that the loader carries through untouched: tasks
differ, and the alternative to an escape hatch is a required key that means
nothing for three tasks out of four.

## `tests/audit/`

Stage 1. A fixed model gets two roots and a set of read-only tools:

```
/opt/original     State A, unpacked from original.tar.gz, read-only
/opt/workspace     the submission, as collected
```

`prompt.txt` is the task's whole audit review — the migration's goal, what
to look at, what counts as evidence, and the output format. It is the most
semantically dense file in the task, and it is prose rather than a rule list on
purpose. Placeholders are filled before the review starts:

| placeholder | becomes |
|---|---|
| `{{task}}` | the task id |
| `{{original}}` / `{{workspace}}` | the two absolute paths |
| `{{gates}}` | the declared gates, ids and questions, marked REQUIRED or advisory |
| `{{roots}}` | every root and its path |
| `{{commands}}` | the command allowlist, or a note that this review is by reading |

`{{submission}}` is an accepted alias for `{{workspace}}`; the full set is
`swerefactor.audit.PLACEHOLDERS`.

An unknown placeholder is left visible rather than erased, so a typo shows up in
the transcript instead of silently deleting an instruction.

The reviewer's tools are `list_dir`, `fetch_source`, `search`, `diff` (across the
two roots) and optionally `run` against a basename allowlist. Every path is
resolved and checked against its root's realpath, so a symlink out of the
workspace is refused rather than followed.

**Two verdicts, no abstention.** The schema takes `pass` or `fail` and nothing
else. `fail` is the verdict that has to be earned, so everything that is not an
earned fail resolves to `pass`: an unevidenced fail, a verdict the schema does
not recognise, a gate the review left out after being asked again, and a tie.
Each of those is recorded on the check — `metadata.defaulted_to_pass`, or
`metadata.tie_broken_to_pass` — so a pass reached by rule is never mistaken for
one a review argued for.

The asymmetry is deliberate, and so is the removal of the third option. An
undecided *required* gate is `error`, and `error` on a required check stops the
ladder as a `harness_error` — which discards stage 2's already-measured points.
Two runs lost their whole score that way with the behavioural stage already
measured, so "I could not tell" was costing more than either verdict.

**Evidence.** A `fail` verdict must cite `path`, `line` and `quote`. The runner
re-opens each citation and checks the quote is within three lines of where it
was claimed, whitespace-insensitively. A `fail` whose citations do not check out
is counted as a `pass`, because the cost of a false fail is a zero on work that
may be sound.

**Aggregation.** `samples` independent reviews vote per gate; majority carries.
With three samples and no abstention a tie needs a dead review, and it resolves
to `pass` on the same rule — a fail is carried by a majority or it is not
carried. `error` survives for one case, and it is not a verdict about the
submission: no review returned a usable answer on that gate at all. That is the
grader's outage, and it reports as a `harness_error` to re-run. The stage is
`status: error` when no sample is usable at all.

The `Dockerfile` provides the reviewer's runtime: the code-browsing tools, the
language toolchain if the review needs to build anything, and whatever is needed
to reach the model. It mounts the original read-only and takes its own copy of
the workspace.

## `tests/behavioural/`

Stage 2. Deterministic, rule-based, and all-or-nothing: it is the stage that
measures the most and the one whose measurement is not what it pays. It answers
"how much of the repository was actually rewritten" — build, install, run,
external compatibility, behavioural equivalence — publishes that answer as a rate,
and pays for it only when the answer is all of it.

`suite.toml` (`schema = "swerefactor.behavioural-suite/1"`) is the manifest:

```toml
schema = "swerefactor.behavioural-suite/1"
task = "swerefactor/lang01-cmark-c-to-rust"
lib_dir = "lib"
default_timeout_sec = 1800.0

[[module]]
id = "build"
title = "The submission builds from source"
weight = 0.08           # small, because it measures no behaviour itself
timeout_sec = 3600.0

[[module]]
id = "conformance"
title = "CommonMark conformance, byte for byte"
weight = 0.30
about = "the 652 spec examples plus the upstream regression corpus"
```

`dir` defaults to `modules/<id>` and `command` to that directory's `run.sh`
(then `run.py`), so a conventional module declares three fields. Weights are
normalised across the suite, so they read as relative importance and need not
sum to one.

Weights exist because the suites differ in size by three orders of magnitude —
one module contributes 9 checks and another 18,000. They read as relative
importance, they shape how the stage-2 table is summarised, and they decide which
modules are allowed to block the stage. They are not prices: nothing here is paid
per module.

#### How the module rates become the stage's verdict

    complete = every scored check in every weighted module passed
    points   = behavioural_points if complete else 0.0

There is no arithmetic between the rates and the points. Each module's rate is
computed, published, and multiplied by nothing; what the stage is paid comes off
the rows rather than off the combined rate, because `complete` is a property each
row carries and re-deriving it from a summary figure is how a printed number and a
scored number drift apart.

A rate is a fair summary of a suite only when its checks are interchangeable, and
these are not: a task's corpus is thousands of near-identical cases while its
packaging module is four checks, so any weighted average is mostly a report on the
largest suite and a single missing behaviour disappears into it. A submission that
produced no build at all but kept one large corpus passing would read as
half-finished rather than as absent. That is the reason the stage does not pay a
rate — not severity, and not a preference for strictness.

So weight has exactly two jobs, and neither is pricing:

* **It shapes the report.** The stage-2 table sorts and summarises by area, so a
  reader can see at a glance which part of the migration is weak.
  `verdict.behavioural_rate` is published for the same reason: it is the evidence
  that separates a port failing one edge case from one that never built, and a
  verdict of 0 cannot carry that distinction by itself.
* **It decides whether a module can block.** A module carrying weight has to be
  complete; a weight-0 module reports without judging and cannot stop the stage.
  Everything else follows from that one rule — a module that crashed, timed out or
  was never reached has no complete row, and so stops the stage with no separate
  rule needed to say so.

A module that did not run scores **0** and keeps its weight rather than dropping
out of the denominator: a module that vanished is a loss, not an abstention. Its
rate is still reported, and its incompleteness stops the stage exactly as one
failed check does. A suite that cannot build scores 0 on `build` and still runs the
modules downstream — what they can observe about a tree with no artefact is usually
near nothing, but it is measured rather than assumed, and it is what makes the
report diagnosable after the verdict is already decided.

Two consequences of an all-or-nothing stage are properties of the *rule* rather
than of any submission, and both belong in front of anyone authoring a suite:

* **The gate and the payment are the same event.** There is no threshold to place
  and no figure between 0 and `behavioural_points` for one to sit in, so there is
  nothing an author can retune that moves the entry condition for stage 3 without
  also moving what stage 2 pays. "Complete" is the whole rule.
* **Suite size does not change the price of a failed check, only what the report
  can say about it.** One failure costs the same 40 points whether the module that
  found it holds 10 checks or 18,000. Splitting a coarse check into twenty cannot
  make a defect cheaper — it can only make the report name the defect more
  precisely. That is the direction a suite should be written in: finer checks buy
  diagnosis, not partial credit.

Because the points are a verdict rather than a measurement, the measurement is
published beside them everywhere they appear: `behavioural_rate` on the verdict,
`rate` and `complete` on each module row, `behavioural_measured_rate` and
`behavioural_measured_points` in `metadata` for a stage that ran uncredited, and
both the verdict and the rate on the stage-2 line of the report. A report that
reads "0.00 points" tells a reader whether the submission passed four fifths of its
checks or none of them only because that second number is there.

### The build ledger

Every task here needs the same build several times over, and building a
581-file source tree once per module would spend the stage's budget on repetition.
So the `build` module is first, runs the whole configuration matrix once into
`$SRB_SUITE_WORK`, and publishes `builds.json` naming what it produced.
The modules after it restore from that ledger rather than building.

Two rules keep the ledger honest:

* A module that asks for a configuration the ledger does not have gets a
  **failure naming it** — never a fresh build. If the matrix did not produce that
  configuration, that is a finding about the submission, and quietly rebuilding it
  in a downstream module would convert the finding into a pass.
* `build` is **scored, not setup**. "The default configuration produces the nine
  release jars" is a measurement, and its failure belongs in the report with its
  build log rather than as a harness error.

Configurations are **named**, and modules ask for them by name — `default`,
`no-props`, `relocated`, `installed`, and so on. The names are the vocabulary the
whole suite shares, and they are what makes a downstream failure legible: "fails
under `relocated`" says where to look, which "fails under configuration 4" does
not.

### The module contract

One subprocess per module. The runner exports:

| variable | meaning |
|---|---|
| `SRB_REPO` | the submission |
| `SRB_ORIGINAL` | State A, read-only |
| `SRB_MODULE_DIR` | this module's own directory |
| `SRB_SUITE_DIR` | `tests/behavioural/` |
| `SRB_WORK` | a scratch directory, this module's alone |
| `SRB_SUITE_WORK` | scratch shared by the whole suite — where the ledger lives |
| `SRB_RESULT` | where to write the result JSON |
| `SRB_MODULE_ID` | this module's id |

Check ids are namespaced with the module id (`<module-id>/<check-id>`) by the
runner, so two modules may name a check the same thing.

The module writes `$SRB_RESULT` and exits. Its exit status is advisory: the JSON
is the answer, and a module that exits non-zero having written a complete result
is believed. A module that writes nothing is an `error`, which is how a crash is
distinguished from a suite of failing checks.

```json
{
  "checks": [
    {"id": "spec/0042", "verdict": "pass", "weight": 1.0,
     "summary": "example 42 renders byte-identically"},
    {"id": "install/pkgconfig", "verdict": "fail", "weight": 2.0,
     "summary": "cmark.pc names -lcmark_rs", "evidence": [{"path": "...", "line": 3}]}
  ],
  "metadata": {"runner": "pytest", "tests": 4231}
}
```

`verdict` is `pass`, `fail`, `skip` or `error`. Skips leave the denominator
untouched — a skip is the suite's decision, never the submission's. `weight`,
`detail`, `evidence` and `metadata` are optional per check. A `weight` of 0 is not an
exemption: the check is dropped at collection, so it never reaches the merged report or
the module's counts. There is no per-check `required`; a module's weight divides
equally among the checks it kept, and no check can zero a module.

Existing pytest suites become modules without being rewritten:
`pytest -p swerefactor.pytest_module` writes the contract from the report stream.
`srb_weight`, `srb_skip_ok` and `srb_group` markers annotate what the plugin cannot
infer. There is no `srb_required` marker: it is unregistered, so under
`--strict-markers` a task applying it fails at collection instead of carrying a flag
no scorer reads, and the plugin never writes a `required` field even from a stray
keyword. An unlicensed skip is recorded as a `fail`, because in a fixed offline
environment a test that declined to run is a test that could not.

The `Dockerfile` builds a clean environment and the suite rebuilds the
submission from source inside it. It does not trust anything the agent left
behind: no build artifacts, no cached results, no installed package. That is
what makes "it builds" a measurement rather than a claim.

### What a stage-2 score is measured under

Stated here rather than left to be inferred from each task's Dockerfile, because
it bounds what a stage-2 score means.

**The stage-2 container is the boundary.** It runs with no network at all and no
Docker socket, and the runner, the modules and the submission's own build are
subprocesses of each other inside it, under one uid — root in all but `pf02`,
which is ordinary for a container that has to install packages and compile. What
the boundary holds is everything outside that container; inside it, a build that
goes looking can read `/tests/behavioural` and `/opt/assets`, and a process still
running can write `$SRB_RESULT`. Read-only bits (`chmod -R a-w`, seven tasks on
`/opt/assets`, `pf02` on the suite itself) keep an ordinary build from reaching
them by accident.

**What holds up the score is therefore not containment.** Three things do, none of
which depend on it:

* *The submission is authored blind.* Hidden tests, fixtures and expected values
  exist only in the verifier images, which are built from `tests/` after the
  agent's container is already gone. At the time the code is written there is
  nothing to read and no network to read it over.
* *Inputs are pinned by content.* Most suites re-check a digest manifest while
  grading, not only at image-build time, so a fixture that changed between freeze
  and use is a loud failure rather than a quiet pass. The rest verify their inputs
  in the Dockerfile, which is the weaker of the two positions: a suite that
  generates its corpus during the build and checks the result there is comparing
  against a digest the same build wrote. Which suite is in which group is a
  property of that task's `data/` and its Dockerfile, and is not tallied here: a
  count nobody recomputes is worse than no count.
* *Looking is itself scored.* Every task gates stage 1 on grader-awareness —
  `no_grader_awareness`, `no-verifier-awareness`, or folded into
  `no-answer-lookup`. Code that branches on `/tests`, on a `SRB_*` name, or on
  anything else that only exists while being graded fails there, before the
  behavioural score is reached.

The exposure that remains is a submission adapting to the grader **at grade
time** — not exfiltration. Two consequences follow, and both are implemented in
`swerefactor/contract.py`:

* The eight contract names above are removed from the environment before any
  subprocess a submission controls is started (`submission_env`). A module keeps
  the full contract; what the module hands to a build does not. Note that this is
  hygiene, not containment: a build can still hardcode `/tests/behavioural`. The
  point is that it then has to, and hardcoding it is evidence.
* Each module runs in its own process group and the group is signalled after the
  module exits and **before** `$SRB_RESULT` is read (`reap_group`). A leftover
  process cannot still be writing the file being scored, or still holding a port
  the next module expects to bind.

A second, unprivileged sandbox around the submission's build would narrow the
remaining exposure further. It is a deliberate non-choice: it would touch all
twenty behavioural images and the runner's execution path, and for submissions that
come from a known authoring pipeline it buys less than it costs — the same model
other container-based harnesses run under. Accepting submissions from outside that
pipeline — a public leaderboard, say — is the point at which the trade changes,
and this section is where to start reading.

## `tests/verification/`

Stage 3. Six models, each with its own budget — an hour is typical — get the
original and the submission and try to write a behavioural test the original
passes and the submission fails. A model that fails to find one pays the
submission ten points.

```toml
schema = "swerefactor.verification-probe/1"
task = "swerefactor/lang01-cmark-c-to-rust"
prompt = "prompt.txt"
candidate_command = ["bash", "modules/run-candidate.sh"]

[scope]
allow = [
  "documented CommonMark rendering for all five output formats",
  "the public C ABI: symbol names, signatures, struct layout",
]
deny = [
  "iteration order where the specification does not fix it",
  "wording of internal diagnostics, timing, memory addresses",
]
reruns = 3

[[adversary]]
id = "a01"
model = "claude-opus-5"
driver = "anthropic"
budget_sec = 3600.0
```

A candidate counts only when five things hold:

0. **both trees could be tested at all** — see the fault codes below;
1. it **passes on the original** — otherwise it is a broken test, not a defect;
2. it **fails on the submission**;
3. it **reproduces** `reruns` times — a flake that costs ten points is our bug;
4. it is **in scope** — the assertion is on something the task promised.

The first four are mechanical and run first, because they are cheap and discard
most of what arrives. The fifth is a reading, so it is read: an adjudicator
model gets the task's own `allow` and `deny` statements and rules on the
assertion the code actually makes.

### Condition 0: a tree that could not be tested

"Fails on the submission" is two observations sharing one exit status: the
submission *behaved* differently, and the submission could not be *built*. The
second is stage 2's finding and is already scored there. Read as the first it
charges ten points, six rounds over — sixty, for a tree that does not compile.
Under an all-or-nothing stage 2 that tree cannot reach this stage at all, so the
confusion now costs nothing in production; the condition stays because `--force`
runs the rounds against exactly such a tree, and an adjudicator that cannot tell
the two readings apart is wrong about it whether or not the points are live.

So `run-candidate.sh` says which it was, in its exit code, and the adjudicator
reads it. The vocabulary is `sysexits.h`'s 64–78, which is what the twenty scripts
already use: 71 for "the tree does not build" in fifteen of them, 74 for "would
not start", 70 for the driver itself failing, 64 for a harness contract that
changed underneath it. A fault on **either** side is `invalid` — the candidate
established nothing about the submission, in either direction.

Below that range is the candidate's own answer, and it stays outside: `0` passed,
`1` its assertions failed, `2`–`5` are pytest's own troubles. A task whose
candidate command answers in another vocabulary says so:

```toml
[scope]
fault_exit_codes = [9, 40]   # absent from all twenty tasks; empty means 64-78
```

`0` and `1` are refused there by name. `0` as a fault code makes every passing
candidate undecidable, so no round can find anything and all six survive: the
full sixty points, to any submission, from one number in a file nobody re-reads.

A timeout is deliberately **not** a fault. A candidate that hangs against one tree
and not the other is a real asymmetry, and the adversary is entitled to it.

Only the adjudicator can decide this, and that is the whole reason it lives in the
harness rather than in the twenty scripts. A script sees one tree: it can refuse
to report a fault as a failure, but refusing on *both* sides inverts the error into
six survivals and pays the sixty points instead of charging them. Deciding it
needs both runs side by side. The adversary is told the same thing in its
transcript, so a round does not spend the rest of an hour rewriting a candidate
that was fine.

Scope is what keeps the round winnable. An adversary that may assert anything
will always find *something* — a set's iteration order, a timing difference, the
exact wording of a diagnostic the task never promised — and no submission would
survive. With no scope declared at all, nothing is out of scope, and
`swerefactor validate` says so. With no adjudicator model *configured*, a break is
**not** upheld: the mechanical half alone would let a timing assertion cost ten
points.

An adjudicator that was configured and could not be reached is a different
outcome, and gets a fourth decision: `undecided`. Rejecting there would make the
round a survival, and a survival is ten points — paid, in that case, for an
outage rather than for a submission that withstood attack. An undecided
adjudication makes the round an `error`, which pays nothing, is counted in
`errored`, and says the round should be run again. The distinction is on whether
the endpoint answered, not on whether the answer was useful: a model that
responds and declines to rule would decline again, so that stays a rejection.
Retries are already spent by the time this is reached — `Driver.complete` backs
off and retries the retryable statuses four times by default — so `undecided`
means the backoff ran and did not help.

Candidates run against copies of both trees. Stage 3 must not be able to change
what stage 2 measured.

### Two rules for stage-3 harnesses

Both come from the same asymmetry: condition 1 says a candidate only counts if the
**original** passes it, so anything that stops the original from working silently
converts every candidate into "fails on the original" and pays the submission all
sixty points. A broken stage 3 does not fail loudly. It awards full marks.

* **The harness must never write inside the tree it measures.** A helper that
  compiles a test driver into the repository it is about to inventory will find
  its own output and report it as the submission's leftovers. Scratch goes beside
  the tree, never in it.
* **`run-candidate.sh` scrubs by name, never by glob.** A scrub pattern wide
  enough to catch the harness's own scratch directory is wide enough to delete
  something delivered. Name the directories.

And one for the capability surface in `lib/`: when a candidate asks for something
that does not exist, **raise an error that blames the candidate**, with the valid
alternatives listed. The failure mode to avoid is a helper that passes an unknown
name through to a tool and lets the tool produce a plausible-looking negative
result — that is a false break, and a false break costs a sound submission five
points.

`check-probe.py` is the authoring-time smoke test for all of this: it runs
candidates through `run-candidate.sh` in the real stage-3 image and asserts the
original still builds afterwards.

A round that spends its whole budget without claiming is a survival, and the
report records how it ended, so an adversary that spent an hour thrashing is
visible rather than hidden inside a score. A round that could not run is an
`error` and pays nothing: a model we failed to call has not demonstrated
anything.

## The result contract

Every stage writes the same envelope, `swerefactor.stage-result/1`:

```json
{
  "schema": "swerefactor.stage-result/1",
  "stage": "behavioural",
  "task": "swerefactor/lang01-cmark-c-to-rust",
  "status": "ok",
  "started_at": "2026-07-30T09:12:44Z",
  "duration_sec": 1841.2,
  "harness": {"fingerprint": "8815a3a68b15"},
  "units":  [{"id": "conformance", "weight": 0.3, "status": "ok"}],
  "checks": [{"id": "conformance/spec/0042", "verdict": "pass", "unit": "conformance"}],
  "metadata": {},
  "notes": []
}
```

`status` is about the stage: `ok`, `error`, `timeout`, `skip`. A *unit's* status
takes one more value, `unreached`, which the suite seeds and no module writes about
itself; see "Status is not verdict" above for what it costs. `verdict` is
about the submission: `pass`, `fail`, `skip`, `error`. A `unit` is a module or a
round; a `check` is one measurement. Unknown verdicts read as `error` rather
than raising, so a future module's vocabulary degrades to "undecided" instead of
crashing the scorer.

`harness` names the grader, stamped when the result object is constructed. It is
not `[agent].harness`, which names the client that drove the submission; see the
note under the agent phase above.
`fingerprint` is `swerefactor.fingerprint()` — twelve hex characters of a SHA-256
over the `.py` files in `infra/swerefactor/`, each mixed in with its name so that
moving code between modules registers. A name alone cannot attribute a score: all
sixty stage images take the harness from a mutable tag
(`ARG INFRA_IMAGE=swerefactor/infra:1`), so rebuilding the donor image changes what
grades a submission with nothing else in the record moving. The fingerprint moves
whenever the source moves. `infra/Dockerfile` prints it in its self-check, so the
donor image's build log is what a recorded fingerprint is matched against.

It is a top-level key rather than something in `metadata` because `metadata` is
free-form and a task may write anything into it; provenance must not be a field a
task can overwrite. It is stamped once and copied verbatim by `from_dict`, never
recomputed on write — a result re-read and written back out would otherwise claim
to have been graded by whatever is running now, which is the one thing a
provenance record must never say. Absent, or `{}`, in results written before the
field existed; readers render nothing rather than `unknown`, and must not
substitute their own identity.

`score.py` reads the three files and writes the graded verdict:

```json
{
  "schema": "swerefactor.score/1",
  "reward": 0.9, "score": 90.0, "max_score": 100.0, "valid": true,
  "audit_gate": "pass",
  "behavioural": {"points": 40.0, "rate": 1.0, "modules": [...]},
  "verification": {"points": 50.0, "models_total": 6, "models_survived": 5},
  "metadata": {},
  "harnesses": [{"fingerprint": "8815a3a68b15", "stage": "audit"},
                {"fingerprint": "8815a3a68b15", "stage": "behavioural"}],
  "blocked_by": "", "notes": [], "uncounted": {}
}
```

`reward` is the normalised headline Harbor reads; `score` is the same number on
the published 100-point scale. Both are written so neither consumer has to
rescale and get it wrong. `valid: false` means the number reflects the harness
rather than the submission.

`harnesses` collects the `harness` block off each stage result that carried one,
tagged with the stage it came from. A list and not one value because the three
stages run in three separate images that each take the harness from that mutable
tag: a run whose gate came from one build and whose modules came from another is a
run whose disagreements cannot be attributed to either, and that has to be visible
rather than resolved by picking one. The report prints
`graded by swerefactor 8815a3a68b15` in its footer, and appends
`*** stages disagree on the harness ***` when more than one distinct
fingerprint appears. Empty means no stage recorded one — a verdict from
before the field existed — and the footer is then omitted entirely. Note the
different axis from `harness_error`: that field is about a run having failed, this
one about who ran it.

`behavioural.rate` is the **pass** rate — the weighted mean of the module rates,
measured and multiplied by nothing. `behavioural.points` is the verdict, and the two
do not determine each other: a stage that passed 1999 of 2000 checks publishes
`rate: 0.9995` beside `points: 0.0`, which is not an arithmetic error but the whole
design. Each module row carries `rate` next to `complete` for the same reason, and
`complete` is the field the payment is decided by. A consumer answering "how much of
the migration works" reads the rates; one answering "what did this score" reads
`points`. `metadata` carries the pair again, as `behavioural_measured_rate` and
`behavioural_measured_points`, only for a stage that ran without being credited —
see `uncounted` below.

`uncounted` is what a stage measured before a rung above it discarded the result:

```json
"uncounted": {
  "behavioural": {"ran": true, "measured": true, "status": "ok",
                 "points": 0.0, "rate": 0.9, "complete": false, "modules": 15,
                 "note": "1 behavioural module(s) did not pass every scored check",
                 "stage_blocked_by": "behavioural-incomplete"}
}
```

`points` here is the figure the stage would have been paid and follows the same
all-or-nothing rule as the counted one, so it is 0.0 for anything short of a
complete stage. That is exactly why `rate` and `complete` are beside it: this record
exists so that a failed rung above does not discard the evidence of what the
submission *does*, and "0.00 points" cannot carry that alone. The stage above passed
0.9 of its weighted checks and is one module short of complete — a near-finished port
and a tree that never compiled both publish `points: 0.0`, and only the rate tells
them apart. `complete` is carried explicitly rather than left to be inferred, since a
reader handed `points: 0.0` next to `rate: 0.9000` has no other way to resolve the
pair. In `reward.json` the same split is `stage2_uncounted_rate` against
`stage2_uncounted_points` and `stage2_uncounted_complete`.

The ladder short-circuits, so under Harbor a discarded stage is also a stage that
never launched, and `uncounted` stays empty. Run stage at a time by hand — to get
a gate verdict and a behavioural number out of one submission — the result file
exists, and a report calling it `NOT RUN` states something false about a
measurement the scorer read. So the figure is recorded here and rendered as
`RAN, NOT COUNTED`. It is never an addend: the counted figure for such a stage is
the `0.0` in `behavioural.points` beside it, and the same numbers reach
`reward.json` under `stage2_uncounted_*` / `stage3_uncounted_*` keys, whose
`_ran` flag is what separates a discarded zero from nothing discarded at all.
An absent `uncounted` — in a file written before the field existed — reads as
empty, which is what those runs recorded.

The same record appears twice more, as `unscored` and `uncredited`. Three task
branches built this field independently and named it three things, and all three
spellings are read by shipped consumers, so all three are written. `unscored` and
`uncredited` are one dict under two names; `uncounted` is a projection of it in the
shape above — `modules` as a count where the other two carry the module rows, and
`ran`/`measured` stated as flags where the others leave them implied by which keys
are present. Read whichever one you already read. Do not sum any of them: none is
an addend, which is why they sit outside `behavioural` and `verification` rather than
inside the objects a consumer walks to total the score.

## The telemetry contract

`telemetry.json` is what deciding one run cost, in one place, written by
`swerefactor telemetry <run-dir>` after that run is graded. The input is not produced
by the command: it reads the run directory's `score.json`, where stage 1 recorded
the `usage` of each review it drove and stage 3 the `usage` of each round, so a
corpus can be back-filled one run at a time.

```json
{
  "schema": "swerefactor.telemetry/1",
  "task": "build01-libsodium-autotools-to-cmake",
  "model": "kimi-k3",
  "run": "run1",
  "stage1": [{"index": 1, "driver": "openai", "model": "gpt-5.6-sol",
              "elapsed_sec": 72.0, "calls": 4, "input_tokens": 95081,
              "output_tokens": 2243, "credits": null}, ...],
  "stage3": [{"adversary": "a01-install-and-pkgconfig", "driver": "openai",
              "model": "gpt-5.6-sol", "outcome": "broken", "elapsed_sec": 787.4,
              "calls": 23, "input_tokens": 825515, "output_tokens": 18233,
              "credits": null}, ...]
}
```

The two stages are kept in separate lists because a single per-run total over both
would average a six-model stage-3 spend into a figure that belongs to none of the
models involved. A row's tokens are the single `usage` the stage recorded for that
review or round. `credits` is carried through where the endpoint reported its own
billing unit (Anthropic does; the OpenAI chat and command drivers do not) and is
`null` otherwise, not 0, so an absent figure is not read as a free round.

What this file reports is cost, never verdicts: the projection drops `votes`,
`reason` and every other field a reader of a decision would want, so a reader who
wants to know what was decided reads `score.json` beside it. `task` is taken from
the verdict, which names its own task, and `model`/`run` from the directory, so a
telemetry row and the score beside it cannot disagree about which run they
describe.

## Shared infrastructure

`infra/swerefactor/` holds everything that is identical across tasks:

| module | what it holds |
|---|---|
| `result.py` | the stage-result contract: `Check`, `Unit`, `StageResult` |
| `config.py` | readers for the three TOML files |
| `scoring.py` | the ladder — the one place the 40/60/6/10 policy is implemented |
| `behavioural.py` | the module runner and its environment contract |
| `pytest_module.py` | pytest plugin: an existing suite becomes a module |
| `audit.py` | stage 1: prompt, tools, votes, evidence grounding, aggregation |
| `verification.py` | stage 3: candidates, adjudication, rounds |
| `models.py` | vendor-neutral driver layer (anthropic, openai, command, scripted) |
| `tools.py` | the code-reading toolbox, and root containment |
| `agentloop.py` | the conversation cycle both model-driven stages share |
| `report.py` | the human-readable rendering |
| `harbor.py` | the Harbor boundary: writes `reward.json`, `score.json`, `summary.txt` |
| `tomlcompat.py` | `tomllib` on 3.11+, a reader for it below — the host is 3.10 |
| `telemetry.py` | what a run's grading models cost in time and tokens |
| `cli.py` | `swerefactor validate \| audit \| behavioural \| verification \| score \| report \| ladder \| telemetry` |
| `__main__.py` | so `python3 -m swerefactor` works on the host and in every image |

`harbor.py` writes three files rather than one because their audiences differ:
`reward.json` is numerics only, which is all Harbor reads; `score.json` is the
whole verdict, for a re-score; `summary.txt` is the rendered report, for a person
deciding whether to trust it. `reward.json` is written last, so a consumer that
sees it sees the other two as well, and the flattening is lossy in one direction
only — every number in the verdict appears in `reward.json` and nothing there is
derived from anything absent from the verdict, so the two can be compared and a
disagreement is a bug rather than a rounding convention.

Because that file is numbers only, the harness fingerprints do not appear in it;
`harnesses_distinct` does. Anything other than `1` means the row cannot be
attributed to one harness — `0` for a verdict recorded before stages stamped
themselves, more than `1` for a submission whose stages were graded by different
builds. A consumer comparing two runs of a task, or averaging across a fleet,
excludes those rows on this key and reads `score.json` for which builds they were.

A task's `score.py` is a thin wrapper: load `evaluation.toml`, read the three
stage results, call `swerefactor.scoring.grade`. The ladder is shared because it
is the same ladder; the weights and the policy are per task because they are
not.

`swerefactor validate --task-dir <task>` checks a task before it is ever run:

* the declared gates appear in the prompt, and `probe.prompt` exists;
* every module's directory and command exist, and the weights normalise;
* the number of adversaries matches `verification_models` — every missing one is
  ten points nobody can earn;
* `candidate_command` is non-empty and a scope is declared at all;
* each stage's `image` and `context` exist and are unique across stages;
* every declared model is paired with the driver its family speaks, and says so;
* `environment/{Dockerfile,original.tar.gz,original.sha256}` and
  `{task.toml,instruction.md,score.py}` are present;
* every stage directory has a `.dockerignore`, keeps its subdirectories to
  `lib/`, `modules/` and `data/`, and has no payload loose in its root;
* no shared input has drifted between the environment's copy and a stage's copy;
* every digest, size and file count recorded for a committed file is that file's;
* where `instruction.md` quotes one of the ladder's figures, it is the figure
  `[scoring]` declares.

The digest check reads what `environment/original.sha256` and the four Dockerfiles
write down about `original.tar.gz`, in the four spellings the corpus uses: the
`sha256sum -c` file, an `ARG` piped into `sha256sum -c`, a bare 64-character
literal with no `ARG` to declare it, and a digest hashed into a shell variable and
compared. `tools/build_repo_snapshot.py` prints a new digest and updates none of
the files holding it — there are 2 to 13 per task, 119 in the benchmark — so a
re-pin that updates some of them is the failure. Seventeen environments do verify
at build time, which turns an authoring mistake into a build error hours later
whose log says a tarball is corrupt when the tarball is fine; lang01, lang03 and
lang06 inline the digest with no `ARG`, and for those `original.sha256` is a file
no build reads. A guarded image path is resolved through the Dockerfile's own
`COPY` map rather than by basename, because lang04 guards
`/opt/swerefactor/repo.tar.gz`, which is `original.tar.gz` renamed on the way in.
Tool downloads skip themselves: no `COPY` names them, and their digests are
upstream's fact rather than ours.

The drift check exists because a Docker build context cannot reach outside itself,
so an input that both the agent environment and a stage image need exists twice on
disk — a `requirements*.txt`, a Maven warm-up cache, a State A tarball, a source
contract. Two copies is tolerable; two copies with different bytes is not. Stage
3's claim is "the original passes this test", which only means something if the
original it builds is the one the agent developed against, and stage 2's is
"installs from the same wheel set the agent had". Both are claims about a file, and
both go false the moment one copy is regenerated alone. Same basename means same
bytes — directory trees compared entry by entry, and checked by comparison rather
than against a manifest, so a new shared input is covered the day it is added.

It matches on name rather than on a list because it was a list once, spelled
`requirements*`, and went stale immediately: three other things were duplicated for
exactly the same reason and none of them was being checked.

The layout check is the other half of the same idea. Every stage Dockerfile ends in
`COPY . /tests/<stage>`, so a stage directory without a `.dockerignore` bakes
whatever a local test run left behind into a graded image, and which files are in
it depends on who built it. One list, identical in every one of a task's stage
directories: a stage that excluded something the others kept would produce an
image nobody could reproduce from the tree. Stated without a count on purpose —
earlier copies of this paragraph said "all twelve", which was true of four tasks
and stopped being true at the fifth. Holding subdirectories to `lib/`, `modules/`
and `data/` is what makes "is this file executed, or only read?" answerable from
its path — the question an anti-cheat review turns on. The first four tasks had
grown four answers to where a stage keeps its ground truth (`data/`,
`reference/`, a bare tarball in the stage root) before this was enforced.

The instruction check is the same idea pointed at prose. `instruction.md` is the
only graded artifact an agent reads and the only one no code consumed, so the
figures in it were held to `[scoring]` by nobody. What that costs is
straightforward and has been paid: a ladder retune moves five numbers in
`[scoring]` and leaves every sentence that multiplied them out standing, so an
instruction can go on promising a stage worth twice or half what it now pays.
Nothing in a test suite reads a paragraph, the sentences stay true-looking, and the
agent spends its budget against the stage it was promised rather than the one that
exists.

Prose cannot be checked in general, so four sentence shapes are, and only where
one is present: a stage's share of the whole (`60 of the 100 points`), the
full-marks entry condition (`all 40 of its 40`), the number of adversaries in
words or digits inside a paragraph about that stage (`Six independent
adversaries`), and a per-adversary figure multiplied out (`10 points, for 60`).
The figures come from each task's own `[scoring]`, so a task that deliberately
retuned its ladder is checked against what it declares rather than against the
published numbers. A task whose instruction states none of these shapes is
reported as making no claim, which is the honest outcome for a file that makes
none rather than a check that cannot fail.

The two directions this can be wrong are not symmetric, and it is built to be
wrong in the second. An instruction phrased in a shape none of the twenty uses
goes unchecked — the rule misses it. An instruction phrased in one of these
shapes about something that is not a ladder figure would be reported as drift
that is not there, which is worse: it accuses a correct file. So the shapes carry
guards derived from the corpus, each with a test naming what it protects —
`1200 of 1277` is a pass count, `13 of 35` an inventory, and lang05's table row
`| 3 verification | **60 points** |` names a stage rather than counting three
adversaries.

The model/driver check is there because `driver` has a default and the two
dialects are not interchangeable: they differ in URL, in the key a tool schema
goes under, and in the shape of a message. A task naming an OpenAI model and
omitting the line posts Anthropic-shaped requests to `/v1/messages` — per sample,
for the whole timeout, arriving as a harness fault rather than as a configuration
mistake. The reverse case is subtler: a `claude-*` model with no `driver` line *is*
served correctly, so nothing looks wrong, while the stage silently sits on whichever
dialect the default happens to name. Which dialect a stage speaks decides which
endpoint quirks it is exposed to, and across twenty tasks an unstated default is a
difference no file records. So the driver is stated even where the default is already
right, and a missing line is reported — being correct by accident and being legible
are not the same property, and only one of them survives an edit.

Each stage's model is checked, and so is `[stages.verification.adjudicator]`
separately, because it is built from its own table and so defaults independently
of the stage that contains it.

An unrecognised model name gets no opinion, only a note. A gateway may serve
anything under a name of its own choosing, and guessing would make this fire on
every task using a private deployment.

`--driver scripted` replays canned turns instead of calling a model, which is
how the stage runners are tested offline and how a new task's prompt can be
smoke-tested without spending a budget. It is exempt from the check above: it
replaces the endpoint, so no dialect is involved.

## Exit codes

Stage commands use the same convention:

| code | meaning |
|---|---|
| 0 | the stage ran; the verdict is in the JSON |
| 1 | the stage could not run — a harness fault |
| 2 | the task's own configuration is broken |

A verdict never travels in an exit code. It travels in the result file, which
is written on every path, including the ones where the stage failed.

With one deliberate exception, and it is not a failure path. `swerefactor
verification` exits **0 having written nothing** when stage 2 was not complete —
because that is not a stage that failed, it is a stage the submission did not
earn, and *absent* is how this schema already says the ladder
correctly stopped below a stage. Writing a `status: "skip"` file instead would be
read back as `status != "ok"` and graded as an verification harness error: a fault
charged to a submission whose only fault was stage 2. `--force` runs the rounds
anyway, for developing an adversary against a tree that is not meant to pass; the
scorer still will not pay for the result. An earlier run's `verification.json` is
removed on this path, so a previous submission's rounds cannot stand as this
one's.

Including the path where the config itself is what failed, which is the awkward
one: the result *path* is read out of `evaluation.toml`, so a file that will not
parse takes both the answer and the place to put it. The stage falls back to
`<results_dir>/<stage>.json` — which is what `result` and `results_dir` default
to when omitted, and what all twenty tasks declare explicitly — honouring
`--results` because that arrives on the command line rather than in the
unreadable file. A task that overrode `result` and then broke its own config
gets the file in the default place rather than its chosen one: a worse guess
than reading the file, and a better one than silence. The result carries `status: "error"`,
`metadata.config_error: true`, and the loader's message as a note. Its `task` is
the task directory's name, since `task` is a field in the file that just failed
to load; a note says so rather than presenting the name as read.

Both halves matter, and for one reason. The numeric coercions go through the same
`ConfigError` path as everything else, because `float()` on file data raises
`ValueError`, which escapes every handler and exits non-zero with a traceback —
telling a pipeline to retry an authoring bug forever, and leaving no file. The
missing file is the worse half: absent is how the ladder says stage 3 was correctly
never reached, so a task nobody can grade would read as a working one that stopped
early. `swerefactor.harbor`, which is what each task's `score.py` calls, publishes
`reward.json` with `valid: 0` for the same reason.
