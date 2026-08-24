"""The command-line surface, which no HTTP profile can reach.

ChartMuseum is a binary before it is a server, and three of its behaviours never
involve a socket:

*   ``--version``, ``--help`` and flag parsing. The flag table lives in
    ``pkg/config``, which imports the Gin-coupled logger -- so a rewrite touches
    it whether or not it means to, and dropping or renaming one of the 85 flags
    breaks every deployment that sets it.
*   ``--gen-index``: print the root index to stdout and exit 0, *without*
    listening. It is guarded by ``Depth == 0``, so with ``--depth`` it silently
    becomes a normal server launch -- a conditional that is easy to lose.
*   crash paths. A missing storage backend, an unreadable config file, a bad
    extension: each exits with a specific diagnostic, via ``log.Fatal`` or
    urfave/cli's own usage handler. A port that turns these into a panic, or into
    a server that boots anyway, is a regression a request-based test cannot see.

Every entry here is an ARGV and an environment, never an expected output. What
the binary prints is captured from the pinned State A build, the same way the
HTTP corpus is.

``needles`` is the one place a string appears, and it is not an expectation
either: diagnostics carry timestamps and log noise, so a needle says *which
substring of State A's output is the contract* rather than what the output will
be. Capture asserts every needle against State A's own run and fails if one does
not appear -- so a needle that is wrong breaks the capture, loudly, and never
reaches a grading run.

Needles are deliberately **not** tied to a stream by the case. Measuring showed
why: ``log.Fatal`` diagnostics go to stderr, but urfave/cli writes usage errors
(``flag provided but not defined``, ``invalid value ... for flag``) to *stdout*
and then exits **0**. Declaring the stream per case meant guessing it, and three
guesses were wrong. So a needle is searched for on both streams, and *which
stream carried it* is recorded at capture time as a measured expectation. That
makes the stream part of the graded contract -- a port that moves diagnostics
from stdout to stderr changes what ``2>/dev/null`` hides, which is a real
behaviour change -- without any case having to predict it.

``stdout_mode`` says how stdout is compared:

``exact``    -- byte for byte.
``index``    -- an index.yaml on stdout (``--gen-index``), compared as
                normalised YAML like the HTTP index bodies.
``flaglist`` -- ``--help`` output. Compared as the SET of flag names and their
                argument shapes plus the usage strings, not as a layout: urfave/cli
                aligns columns to the longest flag, so an unrelated change to one
                flag's name reflows every line. The names and usage strings are
                the contract; the column width is not.
``version``  -- the ``x.y.z (build rev)`` line, with the revision masked.
``ignore``   -- not compared (the case is about the exit code or stderr).

``stderr_mode`` is ``exact`` or ``ignore``. Logger output carries timestamps, so
``exact`` is only usable where nothing logs; everywhere else the diagnostic is
pinned by ``needles`` instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

STDOUT_MODES = ("exact", "index", "flaglist", "version", "ignore")
STDERR_MODES = ("exact", "ignore")


@dataclass(frozen=True)
class CliCase:
    """One invocation of the binary."""

    id: str
    #: Argv after the program name. ``{root}`` is replaced with a storage
    #: directory seeded from ``seeds``; ``{conf}`` with the written config file.
    argv: tuple[str, ...]
    #: ``dest=chart[,chart...]`` specs, seeded exactly as for a Profile.
    seeds: tuple[str, ...] = ()
    #: Extra environment. The launcher clears CHARTMUSEUM-relevant variables
    #: first, so a case's environment is exactly what it declares.
    env: tuple[tuple[str, str], ...] = ()
    #: Written to a file before the run and substituted for ``{conf}``.
    config_file: str | None = None
    #: Name for the written config file, when the extension is what is on test.
    config_name: str = "config.yaml"
    stdout_mode: str = "exact"
    stderr_mode: str = "ignore"
    #: Substrings that must appear somewhere in the run's output. The stream each
    #: one lands on is measured at capture, not declared here.
    needles: tuple[str, ...] = ()
    #: True when the process is expected NOT to exit on its own. The launcher
    #: appends ``--port=<free>``, waits for the boot grace period, kills it, and
    #: records only that it was still alive -- so these cases assert "this
    #: configuration boots", which is the one thing a killed process can say.
    serves: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.stdout_mode not in STDOUT_MODES:
            raise ValueError(f"{self.id}: bad stdout_mode {self.stdout_mode!r}")
        if self.stderr_mode not in STDERR_MODES:
            raise ValueError(f"{self.id}: bad stderr_mode {self.stderr_mode!r}")
        if self.stderr_mode == "exact" and self.needles:
            # Redundant, and worse than redundant: an exact stderr comparison
            # already covers every substring of it, so a needle here would look
            # like a second assertion while adding nothing.
            raise ValueError(f"{self.id}: stderr_mode='exact' with needles")


CLI_CASES: list[CliCase] = []


def cli_case(c: CliCase) -> CliCase:
    CLI_CASES.append(c)
    return c


# Reused seed sets, named the same way the HTTP corpus names them.
from .corpus import FULL, FULL_PRE, MY_010, OTHER_010  # noqa: E402

LOCAL = ("--storage=local", "--storage-local-rootdir={root}")


# ---------------------------------------------------------------------------
# Identity and help
# ---------------------------------------------------------------------------

cli_case(CliCase(
    id="version",
    argv=("--version",),
    stdout_mode="version",
    note="the ldflags-injected version and revision, printed by urfave/cli",
))

cli_case(CliCase(
    id="version-short",
    argv=("-v",),
    stdout_mode="version",
))

cli_case(CliCase(
    id="help",
    argv=("--help",),
    stdout_mode="flaglist",
    note="all 85 flags: the migration must not drop, rename or retype one",
))

cli_case(CliCase(
    id="help-short",
    argv=("-h",),
    stdout_mode="flaglist",
))

cli_case(CliCase(
    id="no-args",
    argv=(),
    stdout_mode="ignore",
    needles=("Missing required flags(s): --storage",),
    note="no storage backend: log.Fatal, exit 1, and the message names the flag",
))


# ---------------------------------------------------------------------------
# --gen-index
# ---------------------------------------------------------------------------

cli_case(CliCase(
    id="gen-index",
    argv=LOCAL + ("--gen-index",),
    seeds=(f"={FULL}",),
    stdout_mode="index",
    note="prints the root index and exits 0 without ever listening",
))

cli_case(CliCase(
    id="gen-index-empty",
    argv=LOCAL + ("--gen-index",),
    seeds=(),
    stdout_mode="index",
    note="an empty repository still yields a valid index document",
))

cli_case(CliCase(
    id="gen-index-prerelease",
    argv=LOCAL + ("--gen-index",),
    seeds=(f"={FULL_PRE}",),
    stdout_mode="index",
))

cli_case(CliCase(
    id="gen-index-chart-url",
    argv=LOCAL + ("--gen-index", "--chart-url=https://charts.example.com/base"),
    seeds=(f"={FULL}",),
    stdout_mode="index",
))

cli_case(CliCase(
    id="gen-index-context-path",
    argv=LOCAL + ("--gen-index", "--context-path=/cm"),
    seeds=(f"={FULL}",),
    stdout_mode="index",
    note="the context path reaches the index through ServerInfo",
))

cli_case(CliCase(
    id="gen-index-depth1",
    argv=LOCAL + ("--gen-index", "--depth=1"),
    seeds=(f"myrepo={FULL}",),
    stdout_mode="ignore",
    serves=True,
    note="the guard is `GenIndex && Depth == 0`, so at depth 1 --gen-index is "
         "ignored and this becomes an ordinary server that never exits",
))

cli_case(CliCase(
    id="gen-index-disable-statefiles",
    argv=LOCAL + ("--gen-index", "--disable-statefiles"),
    seeds=(f"={FULL}",),
    stdout_mode="index",
))

cli_case(CliCase(
    id="gen-index-log-json",
    argv=LOCAL + ("--gen-index", "--log-json"),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
    note="log output goes to stderr, so stdout stays a clean index -- a port "
         "that logs to stdout corrupts the document",
))

cli_case(CliCase(
    id="gen-index-debug",
    argv=LOCAL + ("--gen-index", "--debug"),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
))

cli_case(CliCase(
    id="gen-index-env",
    argv=("--gen-index",),
    env=(("STORAGE", "local"), ("STORAGE_LOCAL_ROOTDIR", "{root}")),
    seeds=(f"={FULL}",),
    stdout_mode="index",
    note="every flag has an env var; the whole table must keep working",
))


# ---------------------------------------------------------------------------
# Crash paths
# ---------------------------------------------------------------------------

cli_case(CliCase(
    id="unknown-storage",
    argv=("--storage=nosuchbackend",),
    stdout_mode="ignore",
    needles=("Unsupported storage backend: nosuchbackend",),
))

cli_case(CliCase(
    id="local-without-rootdir",
    argv=("--storage=local",),
    stdout_mode="ignore",
    needles=("Missing required flags(s): --storage-local-rootdir",),
))

cli_case(CliCase(
    id="amazon-without-bucket",
    argv=("--storage=amazon",),
    stdout_mode="ignore",
    needles=("Missing required flags(s): --storage-amazon-bucket",),
    note="the non-local backends are unreachable offline, but their flag "
         "validation runs before any network call and must survive the port",
))

cli_case(CliCase(
    id="google-without-bucket",
    argv=("--storage=google",),
    stdout_mode="ignore",
    needles=("--storage-google-bucket",),
))

cli_case(CliCase(
    id="storage-case-insensitive",
    argv=("--storage=LOCAL",),
    stdout_mode="ignore",
    needles=("Missing required flags(s): --storage-local-rootdir",),
    note="strings.ToLower on the backend name, so LOCAL reaches the local "
         "branch and fails on the rootdir instead of on the backend",
))

cli_case(CliCase(
    id="unknown-flag",
    argv=("--no-such-flag",),
    stdout_mode="ignore",
    needles=("flag provided but not defined: -no-such-flag",),
    note="urfave/cli's own parse error, printed before any of our code runs",
))

cli_case(CliCase(
    id="bad-int-flag",
    argv=LOCAL + ("--port=notanumber", "--gen-index"),
    seeds=(),
    stdout_mode="ignore",
    needles=("invalid value \"notanumber\" for flag -port",),
))

cli_case(CliCase(
    id="bad-duration-flag",
    argv=LOCAL + ("--cache-interval=notaduration", "--gen-index"),
    seeds=(),
    stdout_mode="ignore",
    needles=("-cache-interval",),
))

cli_case(CliCase(
    id="unknown-cache",
    argv=LOCAL + ("--cache=nosuchcache", "--gen-index"),
    seeds=(),
    stdout_mode="ignore",
    needles=("Unsupported cache store: nosuchcache",),
))

cli_case(CliCase(
    id="redis-without-addr",
    argv=LOCAL + ("--cache=redis", "--gen-index"),
    seeds=(),
    stdout_mode="ignore",
    needles=("--cache-redis-addr",),
))


# ---------------------------------------------------------------------------
# --config
# ---------------------------------------------------------------------------

cli_case(CliCase(
    id="config-file",
    argv=("--config={conf}", "--gen-index"),
    config_file="storage: local\nstorage-local-rootdir: {root}\n",
    seeds=(f"={FULL}",),
    stdout_mode="index",
    note="viper reads the file; the keys are the flag names",
))

cli_case(CliCase(
    id="config-file-chart-url",
    argv=("--config={conf}", "--gen-index"),
    config_file=("storage: local\nstorage-local-rootdir: {root}\n"
                 "chart-url: https://charts.example.com\n"),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
))

cli_case(CliCase(
    id="config-flag-overrides-file",
    argv=("--config={conf}", "--gen-index",
          "--chart-url=https://from-the-flag.example.com"),
    config_file=("storage: local\nstorage-local-rootdir: {root}\n"
                 "chart-url: https://from-the-file.example.com\n"),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
    note="precedence: an explicit flag beats the config file",
))

cli_case(CliCase(
    id="config-file-yml",
    argv=("--config={conf}", "--gen-index"),
    config_name="config.yml",
    config_file="storage: local\nstorage-local-rootdir: {root}\n",
    seeds=(f"={MY_010}",),
    stdout_mode="index",
    note=".yml is accepted alongside .yaml",
))

cli_case(CliCase(
    id="config-file-no-extension",
    argv=("--config={conf}", "--gen-index"),
    config_name="chartmuseum-config",
    config_file="storage: local\nstorage-local-rootdir: {root}\n",
    seeds=(f"={MY_010}",),
    stdout_mode="index",
    note="no extension is accepted too -- the check allows \"\"",
))

cli_case(CliCase(
    id="config-file-bad-extension",
    argv=("--config={conf}", "--gen-index"),
    config_name="config.json",
    config_file="{}\n",
    stdout_mode="ignore",
    needles=("config file must have .yaml/.yml extension",),
))

cli_case(CliCase(
    id="config-file-missing",
    argv=("--config=/opt/nonexistent/config.yaml", "--gen-index"),
    stdout_mode="ignore",
    needles=("does not exist",),
))

cli_case(CliCase(
    id="config-file-malformed",
    argv=("--config={conf}", "--gen-index"),
    config_file="storage: local\n  bad indent: [unclosed\n",
    stdout_mode="ignore",
    needles=("yaml",),
))


# ---------------------------------------------------------------------------
# Deprecation warnings
# ---------------------------------------------------------------------------

cli_case(CliCase(
    id="deprecated-enforce-semver2",
    argv=LOCAL + ("--gen-index", "--enforce-semver2"),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
    needles=("has been deprecated",),
    note="the warning is emitted by ShowDeprecationWarnings, on the logger",
))

cli_case(CliCase(
    id="deprecated-disable-metrics",
    argv=LOCAL + ("--gen-index", "--disable-metrics"),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
))

cli_case(CliCase(
    id="no-deprecation-warning",
    argv=LOCAL + ("--gen-index",),
    seeds=(f"={MY_010}",),
    stdout_mode="index",
    note="the negative half: no deprecated flag, so nothing about deprecation",
))


# ---------------------------------------------------------------------------
# Boot without exit
#
# These launch a real server and are killed. What they assert is that the
# process was still alive, i.e. that the configuration boots at all -- which is
# how a rewrite that panics on an unusual flag combination gets caught before
# the HTTP corpus even runs.
# ---------------------------------------------------------------------------

def _boots(case_id: str, extra: tuple[str, ...], *,
           seeds: tuple[str, ...] = (), note: str = "") -> None:
    cli_case(CliCase(
        id=f"boots-{case_id}",
        # No --port here: the launcher assigns one, exactly as it does for a
        # Profile, so two cases can never race for the same socket.
        argv=LOCAL + extra,
        seeds=seeds,
        stdout_mode="ignore",
        serves=True,
        note=note,
    ))


_boots("plain", (), seeds=(f"={FULL}",))
_boots("depth3", ("--depth=3",), seeds=(f"org/team/repo={FULL}",))
_boots("depth-dynamic", ("--depth-dynamic",), seeds=(f"a/b/c={MY_010}",))
_boots("listen-host", ("--listen-host=127.0.0.1",), seeds=(f"={MY_010}",))
_boots("timeouts", ("--read-timeout=30", "--write-timeout=30"),
       seeds=(f"={MY_010}",))
_boots("web-template-missing", ("--web-template-path=/opt/webtemplate/empty",),
       seeds=(f"={MY_010}",),
       note="a template path with no .html logs a warning and boots anyway")
_boots("web-template-nonexistent",
       ("--web-template-path=/opt/nonexistent-template-dir",),
       seeds=(f"={MY_010}",),
       note="an unopenable path must also boot, not crash")
_boots("cache-interval", ("--cache-interval=1h",), seeds=(f"={MY_010}",))
_boots("per-chart-limit", ("--per-chart-limit=3",), seeds=(f"={FULL}",))
_boots("max-storage-objects", ("--max-storage-objects=10",), seeds=(f"={FULL}",))
_boots("index-limit", ("--index-limit=2",), seeds=(f"={FULL_PRE}",))
_boots("everything",
       ("--depth=2", "--context-path=/cm", "--cors-alloworigin=*",
        "--enable-metrics", "--basic-auth-user=u", "--basic-auth-pass=p",
        "--allow-overwrite", "--log-json", "--debug",
        "--chart-url=https://charts.example.com"),
       seeds=(f"org1/repo1={FULL}", f"org2/repo2={OTHER_010}"),
       note="the combination most likely to expose an ordering assumption "
            "made during the port")


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def fingerprint() -> str:
    import hashlib

    h = hashlib.sha256()
    for c in CLI_CASES:
        h.update(f"X|{c.id}|{c.argv}|{c.seeds}|{c.env}|{c.config_file!r}|"
                 f"{c.config_name}|{c.stdout_mode}|{c.stderr_mode}|"
                 f"{c.needles}|{c.serves}\n".encode())
    return h.hexdigest()[:16]


def _selfcheck() -> None:
    ids = [c.id for c in CLI_CASES]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate cli case ids: {dupes}")
    for c in CLI_CASES:
        joined = " ".join(c.argv) + " " + " ".join(v for _, v in c.env)
        needs_root = "{root}" in joined or "{root}" in (c.config_file or "")
        if c.seeds and not needs_root:
            raise ValueError(f"{c.id}: has seeds but never uses {{root}}")
        if "{conf}" in " ".join(c.argv) and c.config_file is None:
            raise ValueError(f"{c.id}: uses {{conf}} with no config_file")
        if c.config_file is not None and "{conf}" not in " ".join(c.argv):
            raise ValueError(f"{c.id}: writes a config file it never passes")
        if c.serves and c.stdout_mode != "ignore":
            # A killed process's stdout is whatever it managed to flush, which
            # is a race. Cases that serve assert liveness, nothing else.
            raise ValueError(f"{c.id}: serves=True needs stdout_mode='ignore'")


_selfcheck()
