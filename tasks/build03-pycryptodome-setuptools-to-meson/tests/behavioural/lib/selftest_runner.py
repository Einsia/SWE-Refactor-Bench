"""Run pycryptodome's own test suite against an installed tree.

This is the module that answers the only question a cryptography library really
has to answer after a build-system migration: does it still compute the right
answers. 42,784 test instances over 39,245 distinct ids, driven by the project's
own vectors -- NIST response files, Wycheproof, the lot.

Three things about how it runs:

  * The suite is loaded out of the *installed* tree, not the repository, in a
    subprocess whose sys.path starts at the install directory. What is being
    tested is what the wheel put on disk.

  * The vectors come from `pycryptodome_test_vectors`, which the image pins.
    They are deliberately not read from the submission's requirements-test.txt:
    that file is editable, and a submission that pinned a different vector
    release would be choosing its own ground truth.

  * `slow_tests` is on. With it off the suite loads 8,699 instances over 5,160
    ids instead of 42,784 over 39,245, and the difference is almost entirely
    Cipher: the known-answer vectors are the part that would notice a wrong
    compiler flag.

    Both numbers are measured. Do not confuse the `slow_tests` figure with the
    vectors figure: with the vectors package ABSENT the suite loads 3,603
    instances over 1,580 ids, whatever `slow_tests` says, because most of those
    42,784 instances exist only because a vector file was found to build them
    from. Two different ways to end up measuring almost nothing, and only one of
    them is a flag.

Ids repeat -- pycryptodome builds many instances of the same TestCase with
different vectors, so `Crypto.SelfTest.Cipher.common.CipherSelfTest.runTest`
appears hundreds of times. The recorded outcome for an id is therefore the worst
outcome any instance of it produced, which is why a single failing vector cannot
be averaged away by the hundreds that passed alongside it.

Run as a script:  python selftest_runner.py <install-dir> <out.json> [--fast]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

#: Worst wins, so one failing vector is visible under an id that mostly passes.
SEVERITY = {"pass": 0, "skip": 1, "fail": 2, "error": 3}

CHILD = Path(__file__).resolve()


def _child(install: str, out: str, fast: bool) -> int:
    """The part that runs inside the subprocess. Imports Crypto; do not call directly."""
    import unittest

    sys.path.insert(0, install)
    from Crypto import SelfTest  # noqa: E402 -- must follow the sys.path edit

    config = {"slow_tests": not fast, "wycheproof_warnings": False}
    suite = unittest.TestSuite(SelfTest.get_tests(config=config))

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                yield from walk(t)
            else:
                yield t

    instances = list(walk(suite))
    outcomes: dict[str, str] = {}

    def record(test, outcome):
        tid = test.id()
        if SEVERITY[outcome] >= SEVERITY.get(outcomes.get(tid, "pass"), 0):
            outcomes[tid] = outcome

    class Collector(unittest.TestResult):
        def addSuccess(self, test):
            super().addSuccess(test)
            record(test, "pass")

        def addFailure(self, test, err):
            super().addFailure(test, err)
            record(test, "fail")
            self._note(test, err)

        def addError(self, test, err):
            super().addError(test, err)
            record(test, "error")
            self._note(test, err)

        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            record(test, "skip")

        def addExpectedFailure(self, test, err):
            super().addExpectedFailure(test, err)
            record(test, "pass")

        def addUnexpectedSuccess(self, test):
            super().addUnexpectedSuccess(test)
            record(test, "fail")

        def _note(self, test, err):
            import traceback

            text = "".join(traceback.format_exception(*err))[-1200:]
            notes.setdefault(test.id(), text)

    notes: dict[str, str] = {}
    started = time.time()
    result = Collector()
    unittest.TestSuite(instances).run(result)

    counts = {"pass": 0, "fail": 0, "error": 0, "skip": 0}
    for outcome in outcomes.values():
        counts[outcome] += 1

    # Which integer backend the library actually chose, asked of the library
    # rather than inferred from what imports. `import Crypto.Math._IntegerGMP`
    # succeeds even when libgmp is absent -- the module falls back internally --
    # so an import probe would report "gmp" unconditionally and never fail.
    # `Numbers.Integer.__module__` is the class that won.
    try:
        from Crypto.Math import Numbers

        library = {
            "Crypto.Math._IntegerGMP": "gmp",
            "Crypto.Math._IntegerCustom": "custom",
            "Crypto.Math._IntegerNative": "native",
        }.get(Numbers.Integer.__module__, Numbers.Integer.__module__)
    except Exception as exc:  # noqa: BLE001 -- reported, not raised
        library = f"unavailable: {type(exc).__name__}"

    Path(out).write_text(json.dumps({
        "root": "Crypto",
        "collected": len(instances),
        "recorded": len(outcomes),
        "counts": counts,
        "tests": outcomes,
        "notes": notes,
        "seconds": round(time.time() - started, 1),
        "implementation": {"library": library, "api": "ctypes"},
        "install": install,
    }, sort_keys=True), encoding="utf-8")
    return 0


def run(install: Path, out: Path, *, fast: bool = False, timeout: int = 5400) -> dict:
    """Run the suite in a subprocess and return the payload it wrote.

    A crash of the child is not an exception here: it becomes a payload with
    `crashed` set and the tail of its output, so the module that called this can
    fail its checks with the reason attached rather than erroring out.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("SRB_")}
    env.update(PYTHONPATH=str(install), PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0",
               LC_ALL="C.UTF-8", TZ="UTC")
    argv = [sys.executable, str(CHILD), str(install), str(out)] + (["--fast"] if fast else [])
    try:
        p = subprocess.run(argv, env=env, capture_output=True, timeout=timeout)
        rc, tail = p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace")[-4000:]
    except subprocess.TimeoutExpired:
        rc, tail = 124, f"timed out after {timeout}s"
    except OSError as exc:
        rc, tail = 127, str(exc)
    if out.is_file():
        payload = json.loads(out.read_text(encoding="utf-8"))
        payload["child_rc"] = rc
        return payload
    return {"crashed": True, "child_rc": rc, "output": tail, "tests": {}, "counts": {},
            "collected": 0, "recorded": 0}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(_child(args[0], args[1], "--fast" in sys.argv))
