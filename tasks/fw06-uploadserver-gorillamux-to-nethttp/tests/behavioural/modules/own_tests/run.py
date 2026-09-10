#!/usr/bin/env python3
"""The repository's own test suite, run as the repository runs it.

Weighted lowest of the nine, and worth having anyway.  State A ships eight test
functions across three files, and none of them imports the retired router: the unit
tests call the handler methods directly, and the integration test starts the
server through the package's own entry point.  So a correct port keeps them
passing, and the ones that break tell you something the recorded corpus cannot --
the corpus speaks HTTP, and these tests reach inside the package.

What is graded is that tests exist and pass, not which tests exist.  A submission is
free to restructure, rename, split or add to them; a rewrite is a legitimate part of
porting, and asserting on test function names would be asserting on how the port
was written.  What is not free is deleting them: a tree with no tests passes `go
test ./...` in every package and would otherwise score full marks on this module.
So the count of packages that actually ran tests is a check, with a floor derived
from State A rather than from a number typed here -- the original is mounted, and it
is read for its count and for nothing else.

`go vet` runs and never decides a weight.  It is genuinely useful reading on a fresh
port -- a `Printf` verb that no longer matches its argument, a lost `err` -- and it
is also a moving target across Go releases, so a submission losing marks to a vet
check that upstream itself would fail is not a defensible outcome.  It is recorded
as a note.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))) + "/lib")

import toolchain as tc  # noqa: E402

_TEST_FUNC = re.compile(r"^func\s+(Test|Fuzz|Example)\w*\s*\(", re.M)

#: `go test` prints one of these per package.  "ok" and "FAIL" mean tests ran;
#: "no test files" means the package has none, which is not a failure for a package
#: that never had any.
_RAN = re.compile(r"^(ok|FAIL|---)\s", re.M)


def _count_test_funcs(root) -> tuple[int, int]:
    """(test functions, files holding them) under a tree."""
    funcs = files = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "vendor", "node_modules")]
        for name in filenames:
            if not name.endswith("_test.go"):
                continue
            try:
                text = open(os.path.join(dirpath, name),
                            encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            found = len(_TEST_FUNC.findall(text))
            if found:
                funcs += found
                files += 1
    return funcs, files


def main() -> int:
    rep = tc.Report()
    env = tc.go_env()

    if not (tc.SOURCE / "go.mod").is_file():
        rep.record("source-prepared", False,
                   "the build module did not publish a source copy", required=True)
        return rep.finish({"stage": "no-source"})

    got_funcs, got_files = _count_test_funcs(tc.SOURCE)

    # The original is a runtime mount, not something this image contains, so its
    # absence is a harness condition rather than a submission defect.  In that case
    # the floor is not graded at all: a floor of 1 computed from a count of 0 would
    # be a check that passes for a tree with one trivial test, which is worse than
    # not asking.  The rest of the module -- do the tests pass, did any run -- is
    # unaffected and still grades.
    if (tc.ORIGINAL / "go.mod").is_file():
        original_funcs, original_files = _count_test_funcs(tc.ORIGINAL)
        rep.note("counts",
                 f"the original holds {original_funcs} test function(s) in "
                 f"{original_files} file(s); the submission holds {got_funcs} in "
                 f"{got_files}")
        # Two thirds, rounded down, and at least one.  A floor rather than
        # equality: merging two table-driven tests into one is a normal thing to do
        # while porting and should not cost anything, while deleting the suite
        # should cost this module.  Derived from the mounted original so that a
        # fixture or corpus change cannot silently move it.
        floor = max(1, (original_funcs * 2) // 3)
        rep.record("tests-still-exist", got_funcs >= floor,
                   f"the tree carries {got_funcs} test function(s); at least "
                   f"{floor} expected, from the original's {original_funcs}",
                   weight=1.0)
    else:
        original_funcs = 0
        rep.note("original-not-mounted",
                 f"no Go module at {tc.ORIGINAL}, so the test-count floor was not "
                 f"graded. The submission carries {got_funcs} test function(s) in "
                 f"{got_files} file(s); whether that is a regression cannot be "
                 f"decided without the tree it is a regression from")

    run = tc.run([tc.GO, "test", "./...", "-count=1"], cwd=tc.SOURCE, env=env,
                 timeout=2700, log="go-test.log")
    out = tc.output_of(run)
    rep.record("own-tests-pass", run.returncode == 0,
               "go test ./... passes on the submitted tree", out, weight=3.0)

    ran = len(_RAN.findall(out))
    rep.record("tests-actually-ran", ran > 0,
               f"go test reported {ran} package result line(s) -- a tree whose "
               f"tests were all deleted reports only 'no test files'",
               out if ran == 0 else "", weight=1.0)

    vet = tc.run([tc.GO, "vet", "./..."], cwd=tc.SOURCE, env=env, timeout=1800,
                 log="go-vet.log")
    if vet.returncode == 0:
        rep.note("vet-clean", "go vet ./... is clean")
    else:
        rep.note("vet-findings",
                 "go vet ./... reported findings. Not graded -- vet moves between "
                 "Go releases and a submission should not lose marks to a check "
                 "the original might also fail. Reading:\n"
                 + tc.output_of(vet)[-1500:])

    return rep.finish({
        "test_functions": got_funcs,
        "test_files": got_files,
        "original_test_functions": original_funcs,
        "packages_reporting": ran,
        "vet_exit": vet.returncode,
    })


if __name__ == "__main__":
    sys.exit(main())
