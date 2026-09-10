#!/usr/bin/env python3
"""Build-time self-check for the verification probe.

Run from this stage's Dockerfile, and it fails the image rather than the run.  The
arithmetic is why: this stage pays ten points per round that finds nothing, so
every way it can break pays the submission MORE than a working stage would.

  A probe.toml with eleven adversaries        pays 110 and looks like a full stage.
  A prompt that never reached the image       six rounds with no instructions,
                                              which find nothing, which pays 60.
  A configuration named in the prompt but
  not implemented in lib/srbgson.py          every candidate that asks for it
                                              raises against both trees, finds
                                              nothing, and pays 60.
  A scope with an empty deny list             every off-limits candidate is upheld,
                                              which pays 0 -- the one failure in
                                              the other direction.

None of those fails visibly at run time.  They all produce a plausible-looking
result file, which is why they are checked here, where a mistake is a red build.
"""
from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Six rounds at ten points each is the stage.  Hard-coded rather than derived
#: from the file it is checking, which is the only way a check on a count works.
EXPECTED_ADVERSARIES = 6

#: Every configuration lib/srbgson.py implements.  The prompt must name each one --
#: an adversary that asks for a configuration nobody told it about gets a KeyError,
#: and one that never learns about `with_sources` leaves the sharpest instrument in
#: the stage unused.
EXPECTED_CONFIGURATIONS = ("default", "full", "publish", "relocated")

#: The placeholders the harness fills.  A prompt that forgot {{original}} sends
#: six rounds looking for a tree by a path that is not in their instructions.
REQUIRED_PLACEHOLDERS = ("{{task}}", "{{original}}", "{{submission}}")

#: Named in the prompt's toolbox section, and each must exist in srbgson's __all__
#: or be an attribute of Tree.  A documented helper that does not exist costs every
#: round a candidate to discover.
DOCUMENTED_API = (
    "tree", "jars", "jar", "jar_name", "entries", "read", "sha256", "manifest",
    "majors", "tests", "published", "published_pom", "java", "with_sources",
    "build_again", "class_bytes_major", "class_strings", "class_module",
    "jar_of", "zip_entries", "zip_read", "BuildFailed",
)

problems: list[str] = []


def bad(msg: str) -> None:
    problems.append(msg)


def main() -> int:
    probe_path = HERE / "probe.toml"
    if not probe_path.is_file():
        print("check-probe: no probe.toml beside this script", file=sys.stderr)
        return 1
    with open(probe_path, "rb") as fh:
        probe = tomllib.load(fh)

    # ---- the shape the harness requires ------------------------------------
    if probe.get("schema") != "swerefactor.verification-probe/1":
        bad("schema is %r, not swerefactor.verification-probe/1"
            % probe.get("schema"))
    if not probe.get("task"):
        bad("no task id")
    command = probe.get("candidate_command") or []
    if not command:
        bad("candidate_command is empty, so no candidate can be run at all")
    else:
        script = next((Path(c) for c in command if str(c).endswith(".sh")), None)
        if script is None:
            bad("candidate_command names no .sh script: %r" % (command,))
        else:
            local = HERE / script.name
            if not local.is_file():
                bad("candidate_command runs %s, which is not in this directory"
                    % script.name)

    # ---- the scope ---------------------------------------------------------
    scope = probe.get("scope") or {}
    allow, deny = scope.get("allow") or [], scope.get("deny") or []
    if not allow:
        bad("the scope allows nothing, so no finding can be in scope")
    if not deny:
        # The one failure mode that costs the submission rather than paying it.
        bad("the scope denies nothing. Every candidate that discriminates for a "
            "reason unrelated to the migration would then be upheld, and a "
            "submission can be zeroed by a candidate that reads a build file")
    for label, entries in (("allow", allow), ("deny", deny)):
        for i, entry in enumerate(entries):
            if not isinstance(entry, str) or len(entry.strip()) < 40:
                bad("%s[%d] is too short to be a scope rule: %r"
                    % (label, i, entry))

    # The blinding rule has to be in the deny list in so many words. Without it a
    # candidate that looks for pom.xml discriminates perfectly, every time, on
    # every submission, and the whole stage measures nothing.
    deny_text = " ".join(deny).lower()
    if not ("identity of the tree" in deny_text
            or "which tree" in deny_text
            or "names the role" in deny_text):
        bad("the deny list does not rule out learning which tree is under test. "
            "A candidate that finds a pom.xml in one tree and not the other "
            "discriminates perfectly and establishes nothing")
    if "does not build" not in deny_text:
        bad("the deny list does not say that a submission which fails to build "
            "is another stage's verdict. Without it a broken build is filed as "
            "twelve findings and punished six times")

    if int(scope.get("reruns") or 0) < 2:
        bad("reruns is %r; a build system is the most nondeterministic thing "
            "this benchmark tests and one run cannot establish a difference"
            % scope.get("reruns"))
    if scope.get("network_allowed"):
        bad("network_allowed is true, but the image has no network and the "
            "dependency closure is vendored")

    # ---- the six rounds -------------------------------------------------
    adversaries = probe.get("adversary") or []
    if len(adversaries) != EXPECTED_ADVERSARIES:
        bad("probe.toml declares %d adversaries, expected %d. The stage pays "
            "ten points per round that finds nothing, so a missing round is "
            "ten points a submission did not earn"
            % (len(adversaries), EXPECTED_ADVERSARIES))
    seen_ids: set[str] = set()
    for i, adv in enumerate(adversaries):
        who = adv.get("id") or "#%d" % i
        if not adv.get("id"):
            bad("adversary %s has no id" % who)
        elif adv["id"] in seen_ids:
            bad("duplicate adversary id %r" % adv["id"])
        else:
            seen_ids.add(adv["id"])
        if not adv.get("model"):
            bad("adversary %s names no model" % who)
        if float(adv.get("budget_sec") or 0) <= 0:
            bad("adversary %s has budget_sec %r" % (who, adv.get("budget_sec")))
        focus = (adv.get("metadata") or {}).get("focus") or ""
        if len(focus.strip()) < 80:
            bad("adversary %s has no usable focus. Six rounds without one "
                "converge on whatever is easiest to look at, and eleven of them "
                "spend their first candidates rediscovering it" % who)

    # ---- the prompt --------------------------------------------------------
    name = probe.get("prompt") or "prompt.txt"
    prompt_path = HERE / name
    if not prompt_path.is_file():
        bad("the prompt file %s is not in the image. Six rounds would run "
            "with no instructions, find nothing, and pay 60 points" % name)
        return report()
    prompt = prompt_path.read_text(encoding="utf-8")

    for ph in REQUIRED_PLACEHOLDERS:
        if ph not in prompt:
            bad("the prompt never uses %s" % ph)
    for cfg in EXPECTED_CONFIGURATIONS:
        if not re.search(r'"%s"' % re.escape(cfg), prompt):
            bad('the prompt never names the "%s" configuration, so no round '
                "learns it exists" % cfg)
    # The reverse direction: a configuration promised but not implemented.
    quoted = set(re.findall(r'"([a-z_]{3,12})"', prompt))
    for word in quoted & {"assemble", "package", "install", "deploy", "test",
                          "check", "javadoc", "sources", "version", "rebuild"}:
        bad('the prompt quotes "%s" where it lists configurations, but '
            "lib/srbgson.py does not implement it. A candidate that asks for it "
            "raises against both trees, which reads as a survival" % word)

    for helper in DOCUMENTED_API:
        if helper not in prompt:
            bad("the prompt does not document %s, which lib/srbgson.py exports"
                % helper)

    # The prompt must say, in words a model will act on, that a broken submission
    # is not this stage's business and that finding nothing is a normal outcome.
    low = prompt.lower()
    if "not a finding" not in low:
        bad("the prompt has no section saying what is not a finding")
    if "found=false" not in low.replace(" ", ""):
        bad("the prompt never tells a round how to report finding nothing, which "
            "is the outcome most rounds should reach")

    # ---- the library the prompt promises -----------------------------------
    sys.path.insert(0, str(HERE / "lib"))
    try:
        import srbgson
    except Exception as exc:                          # pragma: no cover
        bad("lib/srbgson.py does not import: %s: %s" % (type(exc).__name__, exc))
        return report()

    if tuple(srbgson.CONFIGURATIONS) != EXPECTED_CONFIGURATIONS:
        bad("srbgson.CONFIGURATIONS is %r, expected %r"
            % (tuple(srbgson.CONFIGURATIONS), EXPECTED_CONFIGURATIONS))
    for helper in DOCUMENTED_API:
        if not (hasattr(srbgson, helper) or hasattr(srbgson.Tree, helper)):
            bad("the prompt documents %s but srbgson has no such name" % helper)

    # Nothing srbgson exports may name either tree or the dialect. A candidate
    # that can read one of those can discriminate for free.
    for name_ in dir(srbgson):
        if name_.startswith("_"):
            continue
        upper = name_.upper()
        if any(tok in upper for tok in ("TARGET", "ROLE", "ORIGINAL",
                                       "SUBMISSION", "DIALECT", "MAVEN",
                                       "GRADLE")):
            bad("srbgson exposes %r, which names the tree or its build system. "
                "A candidate that reads it discriminates perfectly and "
                "establishes nothing" % name_)
    for attr in dir(srbgson.Tree):
        if attr.startswith("_"):
            continue
        if attr in ("root", "source", "path", "dialect", "tree_path"):
            bad("Tree exposes %r, which hands a candidate the repository. The "
                "only admissible evidence in this stage is what a build "
                "produced" % attr)

    return report()


def report() -> int:
    if problems:
        print("check-probe: %d problem(s)" % len(problems), file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
        return 1
    print("check-probe: ok, %d adversaries, %d configurations, prompt documents "
          "%d helpers" % (EXPECTED_ADVERSARIES, len(EXPECTED_CONFIGURATIONS),
                          len(DOCUMENTED_API)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
