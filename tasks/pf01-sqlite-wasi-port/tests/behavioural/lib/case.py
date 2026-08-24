"""What a differential case *is*.

A case is a recipe, not a transcript: the files to lay down, the argument vector,
the bytes to feed stdin, and what to compare afterwards.  Keeping it declarative
is what lets one executor drive two very different implementations of the same
CLI -- native ``sqlite3`` built from State A, and the submission's wasm module
under ``wasmtime`` -- from a single description, with no second code path that
could disagree with the first.

Nothing here records an expected answer, and that is the point.  The seals worked
and the design was still wrong: what was being compared was the submission
against a *file*, so every question about whether the file was still right had to
be answered by more machinery.  Now both sides run in the same container, in the
same second, and the comparison is between two processes.

The one exception is a ``platform`` case.  Native has pipes, ``dlopen`` and an
unrestricted filesystem, so for the handful of behaviours that WASI *removes*
there is no native answer to compare against -- ``load_extension()`` has to be
absent, not merely different.  Those cases carry a written-down expectation and
say so in their ``kind``; ``__post_init__`` refuses a platform case without one,
because a case with no expectation asserts nothing while looking like a test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping


class CaseError(RuntimeError):
    """Raised when a case is malformed.  Never caught while scoring."""


# How a case is executed.  The distinction matters because it decides where the
# case's expectation comes from.
#
# ``sql``       in-memory database, SQL on stdin.  Pure engine semantics.
# ``file``      a real database file in the sandbox.  Exercises the VFS.
# ``cli``       argument-vector and dot-command surface.
# ``platform``  a WASI-specific contract with a *literal* expectation.  Native
#               cannot produce it -- it has pipes, dlopen and an unrestricted
#               filesystem -- so the required answer is written down from the
#               platform's own rules.  cases_platform.py argues each one and
#               records what native answered beside it.
KINDS = ("sql", "file", "cli", "platform")

# What is compared after the run.
#   stdout        exact stdout bytes, after the case's normalisers
#   stderr        exact stderr bytes, after normalisers
#   exit          process exit status
#   files         digests of named files the run produced
#   native_read   a database the run wrote is handed to native sqlite3, and the
#                 SQL in ``crosscheck`` must produce the same answer it produces
#                 for a database native itself wrote
CHECKS = ("stdout", "stderr", "exit", "files", "native_read")

# How a produced file is compared.  ``digest_files`` uses ``sha256`` -- raw bytes,
# the strongest form, and the default because the database file itself supports
# it: two builds of 3.31.1 write byte-identical databases.  The weaker forms exist
# for the one file that provably cannot support a digest: a journal left behind by
# ``journal_mode=persist`` retains stale page images and the random checksum nonce
# from sqlite3.c's journal header, so it differs even native-to-native.  Measured
# rather than assumed -- two native runs of the same script produce different
# journal bytes, which is why ``journal.persist`` asserts ``zero_prefix:28`` (the
# header a clean shutdown zeroes) and ``journal.persist.size`` asserts the length.
# Everything else in this suite gets ``sha256``.
#   sha256        exact bytes
#   size          byte length only
#   exists        presence only
#   absent        the file must not be there
#   zero_prefix   the first N bytes are all zero (N given as "zero_prefix:28")
PROBES = ("sha256", "size", "exists", "absent", "zero_prefix")


@dataclass(frozen=True, slots=True)
class Case:
    key: str
    family: str
    operation: str
    kind: str = "sql"
    argv: tuple[str, ...] = ()
    stdin: str = ""
    files: Mapping[str, str] = field(default_factory=dict)
    binfiles: Mapping[str, str] = field(default_factory=dict)
    checks: tuple[str, ...] = ("stdout", "exit")
    digest_files: tuple[str, ...] = ()
    # filename -> probe spec, for files a raw digest cannot describe.
    probes: Mapping[str, str] = field(default_factory=dict)
    crosscheck: str = ""
    normalisers: tuple[str, ...] = ()
    literal: Mapping[str, Any] | None = None
    timeout: float = 60.0
    mounts: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise CaseError(f"{self.key}: unknown kind {self.kind!r}")
        for check in self.checks:
            if check not in CHECKS:
                raise CaseError(f"{self.key}: unknown check {check!r}")
        if self.kind == "platform" and self.literal is None:
            raise CaseError(
                f"{self.key}: a platform case carries a literal expectation, "
                f"because the native reference cannot produce one.  A platform "
                f"case without one passes against any output at all."
            )
        if self.kind != "platform" and self.literal is not None:
            raise CaseError(
                f"{self.key}: kind {self.kind!r} is answered by running native "
                f"sqlite3, so a written-down expectation would be a second, "
                f"unreachable answer.  Use kind='platform' or drop the literal."
            )
        if "native_read" in self.checks and not self.crosscheck:
            raise CaseError(f"{self.key}: native_read needs crosscheck SQL")
        if "files" in self.checks and not (self.digest_files or self.probes):
            raise CaseError(
                f"{self.key}: files check needs digest_files or probes"
            )
        for name, spec in self.probes.items():
            kind = spec.split(":", 1)[0]
            if kind not in PROBES:
                raise CaseError(f"{self.key}: unknown probe {spec!r} for {name!r}")
            if kind == "zero_prefix":
                _, _, count = spec.partition(":")
                if not count.isdigit():
                    raise CaseError(
                        f"{self.key}: zero_prefix needs a byte count, got {spec!r}"
                    )
            if name in self.digest_files:
                raise CaseError(
                    f"{self.key}: {name!r} is both digested and probed; pick the "
                    f"strongest form that the file actually supports"
                )

    @property
    def case_id(self) -> str:
        """A short stable id, used to name this case's sandbox directory."""
        return hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:16]

    @property
    def differential(self) -> bool:
        """Whether native supplies this case's expectation by being run."""
        return self.kind != "platform"
