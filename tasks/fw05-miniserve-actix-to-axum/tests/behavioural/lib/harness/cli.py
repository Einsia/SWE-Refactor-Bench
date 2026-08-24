"""One-shot command-line invocations, recorded like responses.

miniserve's argument parser is part of its public interface, and more of it is
observable than the flags themselves. ``--help`` is 264 lines of clap output
naming every flag, its short form, its value placeholder, its default and its
``MINISERVE_*`` environment alias. ``--print-completions`` emits a shell script
per shell, ``--print-manpage`` emits roff, and both are generated from the same
declaration -- so all three move together, and all three are byte-reproducible.

Grading them matters here specifically because the migration *forces* the agent
into this file. The baseline's ``tls`` feature is declared as
``["rustls", "rustls-pemfile", "actix-web/rustls"]``; that third entry cannot
survive, so ``Cargo.toml`` has to be rewired, and the ``#[cfg(feature = "tls")]``
blocks around ``--tls-cert`` and ``--tls-key`` are the natural place for a port to
drift. An agent that quietly drops the feature, or renames a flag, or reflows a
help string, has changed the interface its users script against.

Exit codes are recorded too, and they are not uniform: clap usage errors exit 2
while runtime failures exit 1. A port that collapses both to 1, or to 0, has
broken every script that checks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .sessions import digest

from swerefactor.contract import submission_env


@dataclass(frozen=True)
class Invocation:
    """A single run of the binary with no server involved."""

    id: str
    argv: tuple[str, ...]
    #: Compared byte for byte when true; otherwise only the exit code and a set
    #: of required substrings are graded. Used for the outputs that name a path
    #: or an address the harness chose.
    exact: bool = True
    #: Substrings that must appear in the combined output. The point of grading
    #: these *as well as* the bytes is the failure report: "the completion script
    #: no longer mentions --tls-cert" is a finding, "byte 4137 differs" is not.
    contains: tuple[str, ...] = ()
    #: Environment overrides for the run.
    env: tuple[tuple[str, str], ...] = ()
    note: str = ""
    stdin: bytes = b""


#: Every shell clap_complete can emit. Each is a different generator with its own
#: quoting rules, and each one names all 41 flags.
SHELLS = ("bash", "zsh", "fish", "powershell", "elvish")

INVOCATIONS: tuple[Invocation, ...] = (
    Invocation(id="version", argv=("--version",),
               contains=("miniserve", "0.27.1")),
    Invocation(id="version-short", argv=("-V",), contains=("0.27.1",)),
    Invocation(id="help", argv=("--help",),
               contains=("Usage: miniserve [OPTIONS] [PATH]",
                         "MINISERVE_PATH", "--tls-cert", "--tls-key",
                         "--random-route", "--print-completions")),
    Invocation(id="help-short", argv=("-h",),
               contains=("Usage: miniserve",)),
    Invocation(id="manpage", argv=("--print-manpage",),
               contains=(".TH miniserve 1", "miniserve 0.27.1",
                         "\\fB\\-\\-tls\\-cert\\fR")),
    *(Invocation(id=f"completions-{shell}",
                 argv=("--print-completions", shell),
                 contains=("miniserve",))
      for shell in SHELLS),

    # Usage errors. Every one of these is an exit code plus a message, and the
    # message is clap's, not miniserve's -- which is the point: a port that keeps
    # clap keeps them for free, and a port that hand-rolls parsing does not.
    Invocation(id="err-unknown-flag", argv=("--nope",),
               contains=("unexpected argument", "--nope")),
    Invocation(id="err-unknown-short", argv=("-Z",),
               contains=("unexpected argument",)),
    Invocation(id="err-missing-value", argv=("--port",),
               contains=("a value is required",)),
    Invocation(id="err-bad-port", argv=("--port", "notanumber"),
               contains=("invalid value", "notanumber")),
    Invocation(id="err-port-range", argv=("--port", "99999"),
               contains=("invalid value",)),
    Invocation(id="err-bad-shell", argv=("--print-completions", "nonsense"),
               contains=("invalid value", "nonsense")),
    Invocation(id="err-bad-sorting", argv=("--default-sorting-method", "nope"),
               contains=("invalid value",)),
    Invocation(id="err-bad-order", argv=("--default-sorting-order", "nope"),
               contains=("invalid value",)),
    Invocation(id="err-bad-color", argv=("--color-scheme", "nope"),
               contains=("invalid value",)),
    Invocation(id="err-bad-interface", argv=("--interfaces", "not-an-ip"),
               contains=("invalid value",)),
    Invocation(id="err-bad-auth", argv=("--auth", "missing-colon"),
               contains=("invalid value",)),
    Invocation(id="err-bad-auth-algo",
               argv=("--auth", "joe:sha999:deadbeef"),
               contains=("invalid value",)),
    Invocation(id="err-bad-header", argv=("--header", "no-colon-here"),
               contains=("invalid value",)),
    Invocation(id="err-two-positionals", argv=("dir-one", "dir-two"),
               contains=("unexpected argument",)),
    Invocation(id="err-auth-and-file",
               argv=("--auth", "joe:secret", "--auth-file", "/nonexistent"),
               exact=False,
               note="two credential sources; the message names a path"),

    # Runtime failures, which exit 1 rather than 2. Their messages name a path,
    # so they are graded on the exit code and the substrings only.
    Invocation(id="err-missing-path", argv=("/nonexistent-srb-path",),
               exact=False, contains=("Error",)),
    Invocation(id="err-missing-tls-cert",
               argv=("--tls-cert", "/nonexistent.pem",
                     "--tls-key", "/nonexistent.key"),
               exact=False, contains=("Error",)),

    # Environment aliases, checked through the parser rather than through a
    # server. Each MINISERVE_* variable is declared on its own flag with
    # ``#[arg(env = ...)]``, and losing that half of the declaration is silent:
    # the flag keeps working and every deployment that configures miniserve
    # through the environment stops being configured.
    #
    # The probe is ``--print-completions bash`` rather than ``--help``, because
    # clap resolves ``--help`` before it validates anything else and exits 0 with
    # a bad MINISERVE_PORT still in the environment. Completions are generated
    # after parsing, so a bad value surfaces -- and the resulting message names
    # the flag the variable is attached to, which is the proof that it is wired.
    *(Invocation(id=f"env-bad-{name.lower().removeprefix('miniserve_')}",
                 argv=("--print-completions", "bash"),
                 env=((name, value),), exact=False,
                 contains=("invalid value", flag))
      for name, value, flag in (
          ("MINISERVE_PORT", "notanumber", "--port <PORT>"),
          ("MINISERVE_COLOR_SCHEME", "nope", "--color-scheme <COLOR_SCHEME>"),
          ("MINISERVE_COLOR_SCHEME_DARK", "nope",
           "--color-scheme-dark <COLOR_SCHEME_DARK>"),
          ("MINISERVE_AUTH", "missing-colon", "--auth <AUTH>"),
          ("MINISERVE_HEADER", "no-colon-here", "--header <HEADER>"),
          ("MINISERVE_INTERFACE", "not-an-ip", "--interfaces <INTERFACES>"),
          ("MINISERVE_DEFAULT_SORTING_METHOD", "nope",
           "--default-sorting-method <DEFAULT_SORTING_METHOD>"),
          ("MINISERVE_DEFAULT_SORTING_ORDER", "nope",
           "--default-sorting-order <DEFAULT_SORTING_ORDER>"),
      )),

    # A boolean alias cannot be given an invalid value in the same way -- clap's
    # boolish parser accepts true/false/1/0/yes/no and rejects the rest -- so
    # these prove the alias exists by feeding it something no parser accepts.
    #
    # Two of these names are *not* what you would guess, and that is why they are
    # here by name rather than derived from the flag: ``--mkdir`` is aliased
    # ``MINISERVE_MKDIR_ENABLED``, and ``--overwrite-files`` is aliased bare
    # ``OVERWRITE_FILES`` with no prefix at all. Both are upstream inconsistencies
    # that real deployments already depend on, and both are exactly the sort of
    # thing a port tidies up on the way past. ``--upload-files`` is absent from
    # the list for a different reason: it takes an optional path, so every string
    # is a valid value and there is nothing to reject.
    *(Invocation(id=f"env-nonboolish-{name.lower().removeprefix('miniserve_')}",
                 argv=("--print-completions", "bash"),
                 env=((name, "maybe"),), exact=False,
                 contains=("invalid value",))
      for name in ("MINISERVE_HIDDEN", "MINISERVE_QRCODE",
                   "MINISERVE_MKDIR_ENABLED", "OVERWRITE_FILES",
                   "MINISERVE_ENABLE_TAR", "MINISERVE_ENABLE_ZIP",
                   "MINISERVE_COMPRESS_RESPONSE", "MINISERVE_DIRS_FIRST",
                   "MINISERVE_HIDE_VERSION_FOOTER", "MINISERVE_SPA",
                   "MINISERVE_PRETTY_URLS", "MINISERVE_NO_SYMLINKS",
                   "MINISERVE_SHOW_SYMLINK_INFO", "MINISERVE_SHOW_WGET_FOOTER",
                   "MINISERVE_RANDOM_ROUTE", "MINISERVE_DISABLE_INDEXING",
                   "MINISERVE_VERBOSE")),

    # The optional-value flag, which behaves differently from every boolean above:
    # the variable is accepted, and what it does is scope uploads to one
    # subdirectory. Graded through a server elsewhere; here only that it parses.
    Invocation(id="env-upload-files-path",
               argv=("--print-completions", "bash"),
               env=(("MINISERVE_ALLOWED_UPLOAD_DIR", "/uploads"),),
               contains=("miniserve",),
               note="an optional-value alias, so any string parses"),
)


def run(binary: str, inv: Invocation, cwd: Path, timeout: float = 60.0) -> dict:
    """Run one invocation and record what a caller would observe.

    stdout and stderr are kept apart. Which stream a message lands on is a real
    part of the contract -- ``miniserve --print-completions bash > _completions``
    has to produce a usable file, and a port that writes the script to stderr or
    the error to stdout breaks the pipelines its users already have.
    """
    import os

    env = submission_env()
    env.update({"TZ": "UTC", "NO_COLOR": "1", "TERM": "dumb",
                "CLICOLOR": "0", "COLUMNS": "100"})
    # Same reasoning as the server runner: a MINISERVE_* variable inherited from
    # the container is an argument nobody passed. Removed, then re-added only if
    # the invocation asks for one.
    for key in list(env):
        if key.startswith("MINISERVE_"):
            del env[key]
    env.update(dict(inv.env))

    try:
        proc = subprocess.run([binary, *inv.argv], cwd=str(cwd), env=env,
                              input=inv.stdin, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return {"id": inv.id, "exit": None, "timed_out": True,
                "stdout": "", "stderr": "", "stdout_len": 0, "stderr_len": 0,
                "stdout_sha256": None, "stderr_sha256": None, "exact": inv.exact}

    def text(raw: bytes) -> str:
        return raw.decode("utf-8", "replace")

    record = {
        "id": inv.id,
        "exit": code,
        "timed_out": False,
        "exact": inv.exact,
        "stdout_len": len(out),
        "stderr_len": len(err),
        "stdout_sha256": digest(out),
        "stderr_sha256": digest(err),
        # A backtrace is not part of the contract: RUST_BACKTRACE is unset here,
        # but a panic message would still carry a thread name and an address.
        "stdout": text(out) if inv.exact else "",
        "stderr": text(err) if inv.exact else "",
        # Recorded for every invocation, exact or not, because "the message no
        # longer mentions --tls-cert" is a finding on its own.
        "found": [needle for needle in inv.contains
                  if needle in text(out) or needle in text(err)],
        "missing": [needle for needle in inv.contains
                    if needle not in text(out) and needle not in text(err)],
    }
    return record


def fingerprint() -> str:
    """Digest of the invocation list, folded into the corpus fingerprint."""
    parts: list[str] = []
    for inv in INVOCATIONS:
        parts.append("|".join([
            inv.id, " ".join(inv.argv), str(inv.exact),
            ",".join(inv.contains),
            ",".join(f"{k}={v}" for k, v in inv.env),
        ]))
    return digest("\n".join(parts).encode())
