"""The ``SRB_`` environment contract, and the two things a stage does about it.

This module exists to be importable from a task's ``lib/``, which means it may
depend on nothing but the standard library.  ``swerefactor.behavioural`` reads
``suite.toml`` and so reaches ``tomllib``; no behavioural image asserts that import
at its own build time, so for every one of them it is an assumption rather than a
checked fact -- and a scrub helper that only works where a TOML reader happens to
be installed is a scrub helper that silently is not there.  Everything here is
stdlib and stays that way.

``swerefactor.behavioural`` re-exports all three names, so either import spelling
works.
"""

from __future__ import annotations

import os
import signal
import time

#: The environment the behavioural runner injects.  Every entry is a path into the
#: grading apparatus or a name for the role being played:
#:
#:   SRB_REPO        the submission
#:   SRB_ORIGINAL    State A, which for a rewrite task is the answer
#:   SRB_MODULE_DIR  this module's own directory
#:   SRB_MODULE_ID   the module's declared id
#:   SRB_SUITE_DIR   tests/behavioural -- the hidden tests and their data
#:   SRB_WORK        this module's scratch
#:   SRB_SUITE_WORK  scratch shared across modules
#:   SRB_RESULT      the file this module's score is read out of
#:
#: A module needs all eight.  A program the submission controls needs none of
#: them, and ``submission_env`` is what enforces the difference.
#:
#: Names a *task* declares are deliberately absent from this set.  SRB_SHIM,
#: SRB_GOPROXY_ROOT, SRB_STUB_DIR, SRB_GEM_REPO_SEED and their kind are inputs
#: the build is supposed to use: a shimmed compiler, an offline proxy, a seeded
#: repository.  Scrubbing by ``SRB_`` prefix would break exactly the tasks that
#: instrument their builds most carefully, which is why this is a set of eight
#: names rather than a prefix match.
CONTRACT_ENV = frozenset({
    "SRB_REPO",
    "SRB_ORIGINAL",
    "SRB_MODULE_DIR",
    "SRB_MODULE_ID",
    "SRB_SUITE_DIR",
    "SRB_WORK",
    "SRB_SUITE_WORK",
    "SRB_RESULT",
})

#: How long a process group gets between SIGTERM and SIGKILL.
REAP_GRACE_SEC = 5.0


def submission_env(base: dict[str, str] | None = None,
                   extra: dict[str, str] | None = None) -> dict[str, str]:
    """An environment for a process the submission controls.

    ``base`` defaults to the caller's own environment, from which the eight
    ``CONTRACT_ENV`` names are removed.  ``extra`` is applied afterwards and is
    not filtered: a caller that means to pass one of them through says so, and
    the grep for that is short.

    This is not a boundary.  A build that goes looking can still read
    ``/tests/behavioural`` -- it runs as root in the same container, and
    ``docs/SCHEMA.md`` says so plainly.  What it changes is that the build is no
    longer *handed* the path, which is the difference between a submission that
    had to go looking and one that only had to read a variable.  A build that
    hardcodes ``/tests/behavioural`` after this is evidence rather than ambiguity,
    and stage 1's grader-awareness gate is where that evidence lands.

    The scrub is by exact name over a copy, never by editing ``os.environ``: a
    module reads its own contract long after it has launched a build, and a
    process-wide edit would break the module rather than blind the build.
    """
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if k not in CONTRACT_ENV}
    if extra:
        env.update(extra)
    return env


def reap_group(pid: int, grace: float = REAP_GRACE_SEC, log=None) -> int:
    """Signal everything left in ``pid``'s process group.  Returns how many tries.

    A module that launched a server and exited without stopping it, a build that
    left a daemon behind, a test runner that leaked a worker: all of them are in
    this group, because the module was started with ``start_new_session=True``.

    A grandchild that called ``setsid`` itself has left the group and is not
    reached here.  The task harnesses that start long-lived servers stop their
    own children in a ``finally``; this is the sweep for what they missed.
    """
    tries = 0
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return tries
        tries += 1
        if log:
            log(f"    reaped leftover processes with {sig.name}")
        deadline = time.time() + grace
        while time.time() < deadline:
            try:
                os.killpg(pid, 0)
            except OSError:
                return tries
            time.sleep(0.1)
    return tries
