"""The command-line surface: argument vector, dot commands, output modes.

The shell is not a thin wrapper around the library.  It is its own translation
unit that reaches for things WASI does not have -- ``popen`` for ``.import
'|cmd'``, ``system`` for ``.shell``, ``isatty`` to decide whether to print a
banner, ``fchmod``/``fchown`` when it creates an output file -- so a port can
build the engine perfectly and still ship a shell that behaves differently.
That surface is what a user actually touches, so it is scored directly rather
than assumed to follow from the engine being correct.

Everything here is host-independent by construction: the shell formats values it
already holds in memory.  The commands that genuinely are platform-dependent are
deliberately absent -- ``.vfslist`` and ``.vfsname`` report a VFS name and a
``szOsFile`` struct size, and ``-stats`` reports byte counts of live allocations,
which a 32-bit target legitimately answers differently.  The first two carry
literal expectations in ``cases_platform``; ``-stats`` is asserted here only as far
as it is portable, namely that the flag is accepted and stays off stderr.
"""

from __future__ import annotations

from case import Case

FAMILY = "cli"
DB = "/data/d.db"


def _cli(key: str, operation: str, *, argv: tuple[str, ...] = (":memory:",),
         stdin: str = "", **kw) -> Case:
    kw.setdefault("checks", ("stdout", "stderr", "exit"))
    # ``longdouble`` for the same reason as in cases_engine: it only bites on the
    # "%!.20e" fallback path, whose extra digits are a property of the host's long
    # double.  ``progname`` because the shell prints argv[0] in its usage banner
    # and option errors, and argv[0] is set by the launcher -- an absolute path
    # for the reference binary, a module name under wasmtime.  Both are no-ops on
    # text that does not contain the shape they match.
    kw["normalisers"] = tuple(kw.get("normalisers", ())) + ("longdouble", "progname")
    if stdin and not stdin.endswith("\n"):
        stdin += "\n"
    return Case(key=f"cli.{key}", family=FAMILY, operation=operation,
                kind=kw.pop("kind", "cli"),
                argv=argv, stdin=stdin, **kw)


# The ten output modes of 3.31.1.  ``insert`` needs a table name, the rest do not.
_MODES = ("ascii", "csv", "column", "html", "insert", "line", "list", "quote",
          "tabs", "tcl")

# Several datasets, because the modes differ from each other precisely on the
# awkward values: a NULL is empty in list mode, ``NULL`` in quote mode and an
# empty cell in html; a comma matters only to csv; a blob only to quote and
# insert; a newline inside a value is what separates a correct csv writer from a
# plausible one.
_DATASETS = (
    ("ints", "create table t(a,b,c);insert into t values(1,-2,300000),"
             "(0,9223372036854775807,-9223372036854775808);"),
    ("nulls", "create table t(a,b,c);insert into t values(null,1,'x'),"
              "(2,null,null),(null,null,null);"),
    ("quotes", "create table t(a,b,c);insert into t values("
               "'has,comma','has''quote','has\"dquote'),"
               "('has|pipe','has" + chr(9) + "tab','line1" + chr(10) + "line2');"),
    ("unicode", "create table t(a,b,c);insert into t values("
                "'caf" + chr(233) + "','" + chr(20013) + chr(25991) + "',"
                "'" + chr(128169) + "');"),
    ("blobs", "create table t(a,b,c);insert into t values("
              "x'00ff10',x'',x'deadbeef'),(x'0a0d09',zeroblob(4),x'7f80');"),
    ("reals", "create table t(a,b,c);insert into t values("
              "1.5,-0.0,3.0),(1e300,1e-300,0.1);"),
    ("empties", "create table t(a,b,c);insert into t values('','  ',x''),"
                "('   trailing   ','" + chr(9) + "','');"),
    ("wide", "create table t(a,b,c,d,e);insert into t values("
             "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',2,'c',4,'eeeee'),"
             "('b',22222222222,'cc',4444,'e');"),
)


def _modes() -> list[Case]:
    """Every mode against every dataset, with and without a header row."""
    cases: list[Case] = []
    for mode in _MODES:
        arg = " t" if mode == "insert" else ""
        for name, setup in _DATASETS:
            cases.append(_cli(
                f"mode.{mode}.{name}", f"cli.mode.{mode}",
                stdin=f"{setup}\n.headers on\n.mode {mode}{arg}\nselect * from t;",
            ))
        for name, setup in _DATASETS[:3]:
            cases.append(_cli(
                f"mode.{mode}.bare.{name}", f"cli.mode.{mode}.noheader",
                stdin=f"{setup}\n.headers off\n.mode {mode}{arg}\nselect * from t;",
            ))
    return cases


_T = "create table t(a,b,c);insert into t values(1,null,'x,y'),(2,'q','z');"

# (key, argv-after-the-flags, stdin).  The database argument is appended by
# _flags(), so each entry only names the flags under test.
_FLAG_CASES = (
    # `version` is built separately in _flags(), which explains why.
    ("help", ("-help",), ""),
    ("csv", ("-csv",), _T + "select * from t;"),
    ("csv.sep", ("-csv", "-separator", ";"), _T + "select * from t;"),
    ("list", ("-list",), _T + "select * from t;"),
    ("line", ("-line",), _T + "select * from t;"),
    ("column", ("-column",), _T + "select * from t;"),
    ("html", ("-html",), _T + "select * from t;"),
    ("quote", ("-quote",), _T + "select * from t;"),
    ("ascii", ("-ascii",), _T + "select * from t;"),
    ("nullvalue", ("-nullvalue", "<NIL>"), _T + "select * from t;"),
    ("nullvalue.empty", ("-nullvalue", ""), _T + "select * from t;"),
    ("separator", ("-separator", "::"), _T + "select * from t;"),
    ("newline", ("-newline", "<EOL>"), _T + "select * from t;"),
    ("echo", ("-echo",), _T + "select count(*) from t;"),
    ("batch", ("-batch",), _T + "select 1;"),
    ("bail.ok", ("-bail",), "select 1;select 2;"),
    ("bail.stops", ("-bail",), "select 1;select nosuch;select 3;"),
    ("nobail.continues", (), "select 1;select nosuch;select 3;"),
    ("cmd.one", ("-cmd", ".mode csv"), _T + "select * from t;"),
    ("cmd.two", ("-cmd", ".headers on", "-cmd", ".mode csv"),
     _T + "select * from t;"),
    ("cmd.sql", ("-cmd", "create table z(a)"), "insert into z values(7);"
     "select * from z;"),
    ("cmd.bad", ("-cmd", ".nosuchcommand"), "select 1;"),
    ("lookaside", ("-lookaside", "64", "32"), "select 1;"),
    ("pagecache", ("-pagecache", "4096", "16"), _T + "select count(*) from t;"),
    ("mmap", ("-mmap", "0"), "select 1;"),
    ("maxsize", ("-maxsize", "1048576"), "select 1;"),
    ("vfs.default", ("-vfs", "memdb"), "create table q(a);select count(*) from q;"),
    # `unix-nolock` is registered only under SQLITE_ENABLE_LOCKING_STYLE, which
    # the release build does not set, so the *reference* rejects it too -- this is
    # a recorded case rather than a platform one.  The other four unix VFS names
    # do exist natively and are gone under SQLITE_OS_OTHER; those are in
    # cases_platform.  Note the message prints "(null)": shell.c:18907 reads argv[i]
    # after the option loop has walked past the end, an upstream bug in 3.31.1 that
    # a faithful port reproduces.
    ("vfs.unregistered", ("-vfs", "unix-nolock"), "select 1;"),
    ("stats", ("-stats",), "select 1;"),
    ("interactive", ("-interactive",), "select 1;"),
)


def _flags() -> list[Case]:
    cases: list[Case] = []
    # `sqlite3 -version` prints "<version> <source id>", and the source id is the
    # one value in the whole suite that is a function of the *tree* rather than of
    # the code's behaviour: `tool/mksourceid` verifies every file `manifest` names
    # and appends `alt1` to the id when any of them is missing or altered, which a
    # platform rewrite guarantees.
    #
    # That invites writing the expected string down as a literal, on the reasoning
    # that the reference cannot produce it.  It can: the State A payload itself
    # drops three files that `manifest` still names, so the reference binary --
    # built from the very bytes the agent was handed -- already reports `alt1`.
    # The two targets agree, for two different reasons, on a string neither of
    # them was told.
    #
    # So this is an ordinary differential case, and it still fails the three
    # things worth failing: an id hard-coded into the port, a manifest regenerated
    # against the port's own tree, and a build that stopped deriving the id from
    # the manifest at all.  Each of those changes a value `sqlite3_sourceid()`
    # publishes as public API, and each shows up as a disagreement with the
    # reference rather than as a mismatch against a constant in this file.
    cases.append(_cli(
        "flag.version", "cli.flag.version", argv=("-version", ":memory:"),
        checks=("stdout", "stderr", "exit"),
    ))
    for key, flags, stdin in _FLAG_CASES:
        # -stats prints live allocation byte counts, which a 32-bit target
        # answers differently and correctly.  The portable half of the claim is
        # that the flag is accepted and reports nothing as a diagnostic, so that
        # is what this one case checks -- rather than dropping the flag and
        # leaving "does -stats still work" unasserted.
        checks = ("stderr", "exit") if key == "stats" else ("stdout", "stderr", "exit")
        cases.append(_cli(f"flag.{key}", f"cli.flag.{key}",
                          argv=(*flags, ":memory:"), stdin=stdin, checks=checks))

    # SQL as a trailing argument rather than on stdin: a separate entry point in
    # the shell's main(), and the only one that runs with stdin still at EOF.
    for i, sql in enumerate((
        "select 1+1;", "select 'a'||'b';", ".mode csv\nselect 1,2;",
        "select * from sqlite_master;", "select typeof(1.0);",
        "pragma user_version;", ".tables", ".schema",
    )):
        cases.append(_cli(f"argv.sql.{i}", "cli.argv.sql", argv=(":memory:", sql)))
    # Several statements as separate arguments -- each is executed in order.
    cases.append(_cli("argv.multi", "cli.argv.multi",
                      argv=(":memory:", "create table t(a);", "insert into t values(5);",
                            "select * from t;")))
    cases.append(_cli("argv.dash", "cli.argv.dash",
                      argv=("-csv", ":memory:", "select 1 as a, 2 as b;")))
    # No database argument at all: an anonymous temp database, which is a
    # different path through the VFS than ":memory:".
    cases.append(_cli("argv.nodb", "cli.argv.nodb", argv=(),
                      stdin="create table t(a);insert into t values(1);"
                            "select count(*) from t;"))
    cases.append(_cli("argv.nodb.temp", "cli.argv.nodb.temp", argv=(),
                      stdin="pragma database_list;"))
    cases.append(_cli("argv.dashdash", "cli.argv.dashdash",
                      argv=(":memory:", "select 9;")))
    cases.append(_cli("argv.unknown.flag", "cli.argv.unknown_flag",
                      argv=("-nosuchflag", ":memory:"), stdin="select 1;"))
    cases.append(_cli("argv.missing.value", "cli.argv.missing_value",
                      argv=("-separator",), stdin="select 1;"))
    return cases


# Dot commands that only read or set shell state.  Each entry is the script; the
# database is always in-memory so nothing here touches the VFS.
_CONFIG_CASES = (
    ("show.default", ".show"),
    ("show.after.mode", ".mode csv\n.headers on\n.nullvalue NIL\n.show"),
    ("show.after.sep", ".separator ; :\n.show"),
    ("show.width", ".width 4 8 12\n.show"),
    ("limit.all", ".limit"),
    ("limit.one", ".limit column"),
    ("limit.set", ".limit column 100\n.limit column"),
    ("limit.attached", ".limit attached"),
    ("limit.bad", ".limit nosuchlimit"),
    ("dbconfig.bad", ".dbconfig nosuchoption"),
    ("headers.on", f"{_T}\n.headers on\nselect * from t;"),
    ("headers.off", f"{_T}\n.headers on\n.headers off\nselect * from t;"),
    ("headers.midstream", f"{_T}\nselect a from t;\n.headers on\nselect a from t;"),
    ("headers.yes", f"{_T}\n.headers yes\nselect a from t;"),
    ("separator.col", f"{_T}\n.separator ::\nselect * from t;"),
    ("separator.both", f"{_T}\n.separator , ;\nselect * from t;"),
    ("separator.tab", f"{_T}\n.separator '\\t'\nselect * from t;"),
    ("separator.empty", f"{_T}\n.separator ''\nselect * from t;"),
    ("separator.multichar", f"{_T}\n.separator '<->'\nselect * from t;"),
    ("nullvalue.set", f"{_T}\n.nullvalue (null)\nselect * from t;"),
    ("nullvalue.reset", f"{_T}\n.nullvalue X\n.nullvalue ''\nselect * from t;"),
    ("nullvalue.csv", f"{_T}\n.mode csv\n.nullvalue NULL\nselect * from t;"),
    ("width.narrow", f"{_T}\n.mode column\n.width 1 1 1\nselect * from t;"),
    ("width.wide", f"{_T}\n.mode column\n.width 20 20 20\nselect * from t;"),
    ("width.negative", f"{_T}\n.mode column\n.width -8 -8 -8\nselect * from t;"),
    ("width.reset", f"{_T}\n.mode column\n.width 4\n.width\nselect * from t;"),
    ("width.partial", f"{_T}\n.mode column\n.width 6\nselect * from t;"),
    ("mode.insert.named", f"{_T}\n.mode insert other\nselect * from t;"),
    ("mode.insert.quoted", ".mode insert t\nselect x'ab', 'it''s', null, 2.5;"),
    ("mode.bad", ".mode nosuchmode\nselect 1;"),
    ("mode.missing", f"{_T}\n.mode\nselect 1;"),
    ("echo.on", f".echo on\n{_T}\nselect count(*) from t;"),
    ("echo.off", f".echo on\n.echo off\n{_T}\nselect count(*) from t;"),
    ("changes.on", f"{_T}\n.changes on\ninsert into t values(3,4,5);"
                   "update t set a=a;delete from t where a=3;"),
    ("changes.off", f"{_T}\n.changes on\n.changes off\ninsert into t values(9,9,9);"),
    ("print.plain", ".print hello world"),
    ("print.quoted", ".print 'a b' c"),
    ("print.empty", ".print"),
    ("print.escapes", ".print 'tab\\there'"),
    ("prompt.set", ".prompt 'A> ' 'B> '\nselect 1;"),
    ("binary.on", ".binary on\nselect x'414243';\n.binary off\nselect 1;"),
    ("timer.off", ".timer off\nselect 1;"),
    ("bail.dot.on", ".bail on\nselect 1;\nselect nosuch;\nselect 3;"),
    ("bail.dot.off", ".bail off\nselect nosuch;\nselect 3;"),
    ("eqp.off", f"{_T}\n.eqp off\nselect * from t;"),
    ("explain.off", f"{_T}\n.explain off\nselect count(*) from t;"),
    ("help.unknown", ".help nosuchtopic"),
    ("auth.on", ".auth on\nselect 1;\n.auth off"),
    ("trace.off", ".trace off\nselect 1;"),
    ("scanstats.off", ".scanstats off\nselect 1;"),
    ("progress.quiet", ".progress 1000 --quiet\nselect 1;"),
    ("stats.on", ".stats off\nselect 1;"),
)


# Every sqlite3_db_config switch the shell exposes, except ``load_extension``.
# That one is the forced consequence of a platform with no dlopen -- it reports
# ``off`` on any wasm build and ``on`` on the reference -- so it carries a literal
# expectation in cases_platform rather than being normalised away here.  Reading
# them one at a time rather than dumping ``.dbconfig`` wholesale is what isolates
# the forced row from the fifteen portable ones.
_DBCONFIG = (
    "defensive", "dqs_ddl", "dqs_dml", "enable_fkey", "enable_qpsg",
    "enable_trigger", "enable_view", "fts3_tokenizer", "legacy_alter_table",
    "legacy_file_format", "no_ckpt_on_close", "reset_database", "trigger_eqp",
    "trusted_schema", "writable_schema",
)

# The dot commands ``.help NAME`` resolves by name.  Three names are absent, all
# because their help entry is inside one of the porting macros' #ifndefs:
# ``load`` (shell.c:12104, SQLITE_OMIT_LOAD_EXTENSION) and ``shell`` and
# ``system`` (shell.c:12200, 12205, SQLITE_NOHAVE_SYSTEM).  With the entry gone
# the lookup falls through to a full-text search over the remaining help text,
# and what that search finds has nothing to do with the topic asked for.  Measured:
# ``load`` returns the whole of ``.open``'s help because of its ``--deserialize``
# line, ``system`` returns ``.once``'s because of "Invoke system text editor", and
# ``shell`` matches nothing at all and returns "Nothing matches 'shell'".  Each is
# deterministic, so the problem is not instability -- it is that a case keyed
# ``help.load`` asserting ``.open``'s help text is a claim about the wrong command,
# and it would keep passing if ``.load`` came back.  The three claims are made
# directly in cases_platform instead.  ``excel`` stays here: its help entry at shell.c:12081 is
# unconditional, so the topic resolves identically on both targets even though
# what the command *does* changes -- which is asserted in cases_platform too.
_HELP_TOPICS = (
    "archive", "auth", "backup", "bail", "binary", "cd", "changes", "check",
    "clone", "databases", "dbconfig", "dbinfo", "dump", "echo", "eqp", "excel",
    "exit", "expert", "explain", "filectrl", "fullschema", "headers", "help",
    "import", "imposter", "indexes", "limit", "lint", "log", "mode", "nullvalue",
    "once", "open", "output", "parameter", "print", "progress", "prompt", "quit",
    "read", "recover", "restore", "save", "scanstats", "schema", "selftest",
    "separator", "sha3sum", "show", "stats", "tables",
    "testcase", "timeout", "timer", "trace", "vfsinfo", "vfslist", "vfsname",
    "width",
)


def _config() -> list[Case]:
    cases = [_cli(f"config.{key}", f"cli.config.{key.split('.')[0]}", stdin=script)
             for key, script in _CONFIG_CASES]
    for name in _DBCONFIG:
        cases.append(_cli(f"config.dbconfig.read.{name}", "cli.config.dbconfig.read",
                          stdin=f".dbconfig {name}"))
        cases.append(_cli(f"config.dbconfig.toggle.{name}",
                          "cli.config.dbconfig.toggle",
                          stdin=f".dbconfig {name} on\n.dbconfig {name}\n"
                                f".dbconfig {name} off\n.dbconfig {name}"))
    for topic in _HELP_TOPICS:
        cases.append(_cli(f"config.help.topic.{topic}", "cli.config.help.topic",
                          stdin=f".help {topic}"))
    return cases


# A schema with one of everything the introspection commands have to render:
# a rowid table, a WITHOUT ROWID table, an AUTOINCREMENT (which creates
# sqlite_sequence), a view, a trigger, several index shapes, a virtual table and
# a temp table.  ``.schema`` and ``.dump`` are the two commands whose whole job is
# to reproduce this text, so the text has to be worth reproducing.
_SCHEMA = (
    "create table alpha(id integer primary key autoincrement, name text not null,"
    " score real default 0.0, tag);\n"
    "create table beta(k text primary key, v blob) without rowid;\n"
    "create table \"quoted name\"([odd col] integer, `back tick` text);\n"
    "create index i_alpha_name on alpha(name collate nocase);\n"
    "create index i_alpha_partial on alpha(score) where score > 10;\n"
    "create unique index i_beta_v on beta(v desc);\n"
    "create view gamma as select id, name from alpha where score > 1;\n"
    "create trigger tr_alpha after insert on alpha begin"
    " update alpha set tag='seen' where id=new.id; end;\n"
    "create virtual table delta using fts4(body);\n"
    "create temp table eps(a,b);\n"
    "insert into alpha(name,score,tag) values('one',1.5,null),('two',20.0,'t');\n"
    "insert into beta values('k1',x'0102'),('k2',null);\n"
    "insert into \"quoted name\" values(1,'x');\n"
    "insert into delta values('hello world');\n"
    "insert into eps values(9,'temp');\n"
)

_SCHEMA_CASES = (
    ("schema.all", ".schema"),
    ("schema.indent", ".schema --indent"),
    ("schema.one", ".schema alpha"),
    ("schema.pattern", ".schema %eta"),
    ("schema.view", ".schema gamma"),
    ("schema.trigger", ".schema tr_alpha"),
    ("schema.vtab", ".schema delta"),
    ("schema.quoted", ".schema 'quoted name'"),
    ("schema.missing", ".schema nosuchtable"),
    ("schema.temp", ".schema eps"),
    ("fullschema", ".fullschema"),
    ("fullschema.nosys", ".fullschema --indent"),
    ("tables.all", ".tables"),
    ("tables.pattern", ".tables a%"),
    ("tables.none", ".tables zzz%"),
    ("indexes.all", ".indexes"),
    ("indexes.table", ".indexes alpha"),
    ("indexes.none", ".indexes beta2"),
    ("databases", ".databases"),
    ("dump.all", ".dump"),
    ("dump.one", ".dump alpha"),
    ("dump.pattern", ".dump %eta"),
    ("dump.view", ".dump gamma"),
    ("dump.missing", ".dump nosuch"),
    ("dbinfo", ".dbinfo"),
    ("sqlite_master", "select type,name,tbl_name,sql from sqlite_master order by 1,2;"),
    ("sqlite_temp_master",
     "select type,name from sqlite_temp_master order by 1,2;"),
    ("table_info", "pragma table_info(alpha);pragma table_info(beta);"),
    ("index_list", "pragma index_list(alpha);pragma index_list(beta);"),
    ("index_info", "pragma index_info(i_alpha_name);"
                   "pragma index_xinfo(i_beta_v);"),
    ("foreign_key_list", "pragma foreign_key_list(alpha);"),
    ("database_list", "pragma database_list;"),
    ("table_xinfo", "pragma table_xinfo(alpha);"),
    ("stats.analyze", "analyze;select tbl,idx,stat from sqlite_stat1 order by 1,2;"),
    ("audit", "pragma audit_check;"),
    ("quick_check", "pragma quick_check;"),
    ("foreign_key_check", "pragma foreign_key_check;"),
    ("selftest.absent", ".selftest"),
    ("lint.fkey", ".lint fkey-indexes"),
    ("sha3.schema", ".sha3sum --schema"),
)


def _schema() -> list[Case]:
    cases: list[Case] = []
    for key, script in _SCHEMA_CASES:
        cases.append(_cli(f"introspect.{key}", f"cli.introspect.{key.split('.')[0]}",
                          stdin=_SCHEMA + script))
    # The same commands against a database file rather than :memory:, because
    # ``.dbinfo`` reads the header off disk and ``.databases`` prints the path.
    for key, script in (("dbinfo", ".dbinfo"), ("databases", ".databases"),
                        ("schema", ".schema"), ("dump", ".dump")):
        cases.append(_cli(f"introspect.file.{key}", f"cli.introspect.file.{key}",
                          argv=(DB,), stdin=_SCHEMA + script))
    return cases


# What ``.output`` wrote is checked two ways: the bytes are digested, and they are
# also read back through readfile() into stdout.  The digest alone would tell us
# a mismatch happened without saying what, and readfile() alone would go through
# the same shell that wrote the file -- together they pin the file and show it.
_SHOW = "select hex(readfile('/data/out.txt'));"


def _out(key: str, script: str, **kw) -> Case:
    return _cli(f"io.{key}", f"cli.io.{key.split('.')[0]}", argv=(DB,),
                stdin=script + "\n.output stdout\n" + _SHOW,
                checks=("stdout", "stderr", "exit", "files"),
                digest_files=("out.txt",), **kw)


def _io() -> list[Case]:
    cases: list[Case] = []
    body = _T + "\n"
    for key, pre in (
        ("output.list", ".output /data/out.txt\nselect * from t;"),
        ("output.csv", ".mode csv\n.headers on\n.output /data/out.txt\n"
                       "select * from t;"),
        ("output.html", ".mode html\n.output /data/out.txt\nselect * from t;"),
        ("output.insert", ".mode insert t\n.output /data/out.txt\nselect * from t;"),
        ("output.quote", ".mode quote\n.output /data/out.txt\nselect * from t;"),
        ("output.twice", ".output /data/out.txt\nselect 1;\nselect 2;"),
        ("output.reopen", ".output /data/out.txt\nselect 1;\n.output stdout\n"
                          ".output /data/out.txt\nselect 2;"),
        ("output.empty", ".output /data/out.txt\nselect * from t where 0;"),
        ("output.dump", ".output /data/out.txt\n.dump"),
        ("output.schema", ".output /data/out.txt\n.schema"),
        ("output.print", ".output /data/out.txt\n.print into-the-file"),
        ("output.binary", ".binary on\n.output /data/out.txt\nselect x'00ff41';"),
        ("output.blob", ".output /data/out.txt\nselect x'0a0d00';"),
        ("once.one", ".once /data/out.txt\nselect 1;\nselect 2;"),
        ("once.mode", ".mode csv\n.once /data/out.txt\nselect * from t;\nselect 9;"),
        ("once.dump", ".once /data/out.txt\n.dump"),
    ):
        cases.append(_out(key, body + pre))

    # ``.output`` at a path the sandbox does not reach.  The shell keeps running
    # and says so, which is a behaviour a port can easily lose.
    cases.append(_cli("io.output.badpath", "cli.io.output_badpath", argv=(DB,),
                      stdin=body + ".output /data/nodir/out.txt\nselect 1;\n"
                                   ".output stdout\nselect 2;"))
    cases.append(_cli("io.once.badpath", "cli.io.once_badpath", argv=(DB,),
                      stdin=body + ".once /data/nodir/out.txt\nselect 1;\nselect 2;"))

    # .read: the script file is a first-class input path, distinct from stdin.
    scripts = {
        "plain.sql": "select 1;\nselect 2;\n",
        "dots.sql": ".mode csv\n.headers on\nselect 1 as a, 2 as b;\n",
        "nested.sql": ".read /data/plain.sql\nselect 3;\n",
        "err.sql": "select nosuch;\nselect 4;\n",
        "empty.sql": "",
        "nonl.sql": "select 5;",
        "comment.sql": "-- a comment\n/* block */\nselect 6;\n",
        "multiline.sql": "select\n  1,\n  2;\n",
        "create.sql": "create table r(a);\ninsert into r values(1),(2);\n",
    }
    for name in scripts:
        cases.append(_cli(f"io.read.{name.split('.')[0]}", "cli.io.read",
                          argv=(DB,), stdin=f".read /data/{name}\nselect 'after';",
                          files=dict(scripts)))
    cases.append(_cli("io.read.missing", "cli.io.read_missing", argv=(DB,),
                      stdin=".read /data/nosuch.sql\nselect 'after';"))
    cases.append(_cli("io.read.dir", "cli.io.read_dir", argv=(DB,),
                      stdin=".read /data\nselect 'after';"))
    cases.append(_cli("io.init", "cli.io.init", argv=("-init", "/data/create.sql", DB),
                      stdin="select * from r;", files=dict(scripts)))
    cases.append(_cli("io.init.missing", "cli.io.init_missing",
                      argv=("-init", "/data/nosuch.sql", DB), stdin="select 1;"))
    return cases


# .import reads with its own CSV parser, which has to agree with the writer.
# Each fixture is a file plus the mode it should be read under.
_IMPORT_FILES = {
    "plain.csv": "1,alpha\n2,beta\n3,gamma\n",
    "quoted.csv": '1,"has,comma"\n2,"has""quote"\n3,"multi\nline"\n',
    "header.csv": "a,b\n1,x\n2,y\n",
    "ragged.csv": "1,a\n2\n3,c,extra\n",
    "empty.csv": "",
    "blank.csv": "\n\n1,a\n",
    "crlf.csv": "1,a\r\n2,b\r\n",
    "nonl.csv": "1,a\n2,b",
    "utf8.csv": "1,café\n2,中文\n",
    "tabs.tsv": "1\ta\n2\tb\n",
    "pipes.txt": "1|a\n2|b\n",
    "semi.txt": "1;a\n2;b\n",
    "onecol.csv": "alpha\nbeta\n",
    "spaces.csv": "1, leading\n2,trailing \n",
    "emptyfield.csv": "1,\n,2\n,\n",
}


def _import() -> list[Case]:
    cases: list[Case] = []
    shown = "select rowid,* from imp order by rowid;"
    for name, mode, sep in (
        ("plain.csv", "csv", None), ("quoted.csv", "csv", None),
        ("header.csv", "csv", None), ("ragged.csv", "csv", None),
        ("empty.csv", "csv", None), ("blank.csv", "csv", None),
        ("crlf.csv", "csv", None), ("nonl.csv", "csv", None),
        ("utf8.csv", "csv", None), ("spaces.csv", "csv", None),
        ("emptyfield.csv", "csv", None), ("onecol.csv", "csv", None),
        ("tabs.tsv", "tabs", None), ("pipes.txt", "list", None),
        ("semi.txt", "list", ";"), ("plain.csv", "ascii", None),
    ):
        sepcmd = f".separator '{sep}'\n" if sep else ""
        key = f"{name.split('.')[0]}.{mode}" + (".sep" if sep else "")
        cols = "(a)" if name == "onecol.csv" else "(a,b)"
        cases.append(_cli(
            f"import.{key}", "cli.import",
            argv=(DB,), files=dict(_IMPORT_FILES),
            stdin=f"create table imp{cols};\n.mode {mode}\n{sepcmd}"
                  f".import /data/{name} imp\n{shown}",
        ))
    # Into a table that does not exist yet: the shell invents one from the first
    # row, and whether that row is treated as a header is the interesting part.
    for name in ("plain.csv", "header.csv", "utf8.csv"):
        cases.append(_cli(f"import.auto.{name.split('.')[0]}", "cli.import.auto",
                          argv=(DB,), files=dict(_IMPORT_FILES),
                          stdin=f".mode csv\n.import /data/{name} fresh\n"
                                ".schema fresh\nselect * from fresh;"))
    cases.append(_cli("import.missing.file", "cli.import.missing_file", argv=(DB,),
                      stdin=".mode csv\n.import /data/nosuch.csv imp\nselect 1;"))
    cases.append(_cli("import.constraint", "cli.import.constraint", argv=(DB,),
                      files=dict(_IMPORT_FILES),
                      stdin="create table imp(a integer primary key, b);\n.mode csv\n"
                            ".import /data/plain.csv imp\n"
                            ".import /data/plain.csv imp\nselect count(*) from imp;"))
    cases.append(_cli("import.roundtrip", "cli.import.roundtrip", argv=(DB,),
                      stdin=_T + "\n.mode csv\n.output /data/rt.csv\nselect * from t;\n"
                            ".output stdout\ncreate table u(a,b,c);\n"
                            ".import /data/rt.csv u\nselect * from u;\n"
                            "select count(*) from t natural join u;"))
    return cases


# EXPLAIN output carries renumberable addresses, so these cases run through the
# ``eqp`` normaliser.  What is compared is the plan *text* -- which table is
# scanned, which index is used, whether a B-tree is built for the sort -- because
# that is the part the query planner promises and the part a port must not change.
_PLAN_SETUP = (
    "create table p(a integer primary key, b, c);\n"
    "create table q(x, y);\n"
    "create index i_p_b on p(b);\n"
    "insert into p values(1,'x',1),(2,'y',2),(3,'z',3);\n"
    "insert into q values('x',10),('y',20);\n"
    "analyze;\n"
)

_PLAN_QUERIES = (
    "select * from p;",
    "select * from p where a=2;",
    "select * from p where b='x';",
    "select * from p where c=1;",
    "select * from p order by b;",
    "select * from p order by c;",
    "select count(*) from p;",
    "select * from p, q where p.b=q.x;",
    "select * from p left join q on p.b=q.x;",
    "select * from p where a in (1,2);",
    "select * from p group by b;",
    "select distinct b from p;",
    "select * from p union select a,x,y from q;",
    "select * from (select a from p) where a>1;",
    "with r(n) as (select 1 union all select n+1 from r where n<3) select * from r;",
    "select b, count(*) over () from p;",
    "select * from p where b like 'x%';",
    "select max(b) from p;",
    "select * from p where a between 1 and 2;",
    "select * from q where exists(select 1 from p where p.b=q.x);",
)


def _plans() -> list[Case]:
    cases: list[Case] = []
    for i, query in enumerate(_PLAN_QUERIES):
        cases.append(_cli(f"plan.eqp.{i}", "cli.plan.eqp",
                          stdin=_PLAN_SETUP + ".eqp on\n" + query,
                          normalisers=("eqp",)))
        cases.append(_cli(f"plan.explain.{i}", "cli.plan.explain_query_plan",
                          stdin=_PLAN_SETUP + "explain query plan " + query,
                          normalisers=("eqp",)))
    for mode in ("full", "trigger"):
        cases.append(_cli(f"plan.eqp.{mode}", f"cli.plan.eqp_{mode}",
                          stdin=_PLAN_SETUP + f".eqp {mode}\nselect * from p where b='x';",
                          normalisers=("eqp",)))
    cases.append(_cli("plan.explain.auto", "cli.plan.explain_auto",
                      stdin=_PLAN_SETUP + ".explain auto\nexplain select 1;",
                      normalisers=("eqp",)))
    cases.append(_cli("plan.explain.on", "cli.plan.explain_on",
                      stdin=_PLAN_SETUP + ".explain on\nselect 1;",
                      normalisers=("eqp",)))
    return cases


# The diagnostic text and the exit status are both part of the surface.  A port
# that prints a different message, or exits 0 where the original exits 1, has
# broken every script that checks for either.
_ERROR_CASES = (
    ("dot.unknown", ".nosuchcommand"),
    ("dot.unknown.args", ".nosuchcommand a b c"),
    ("dot.abbrev.ambiguous", ".s"),
    ("dot.empty", "."),
    ("dot.only.dot", ".\nselect 1;"),
    ("dot.too.many.args", ".headers on off extra"),
    ("dot.headers.bad", ".headers maybe"),
    ("dot.width.bad", ".width abc"),
    ("dot.open.missing.dir", ".open /data/nodir/x.db\nselect 1;"),
    ("dot.open.notadb", ".open /data/notadb\nselect * from sqlite_master;"),
    ("dot.open.readonly", ".open --readonly /data/d2.db\ncreate table z(a);"),
    ("dot.backup.badpath", ".backup /data/nodir/b.db"),
    ("dot.restore.missing", ".restore /data/nosuch.db"),
    ("dot.save.badpath", ".save /data/nodir/s.db"),
    ("dot.clone.badpath", ".clone /data/nodir/c.db"),
    ("dot.log.badpath", ".log /data/nodir/l.txt\nselect 1;"),
    ("dot.log.stdout", ".log stdout\nselect 1;\n.log off"),
    ("dot.cd.missing", ".cd /data/nodir"),
    ("dot.mode.extra", ".mode csv extra other"),
    ("dot.separator.none", ".separator"),
    ("dot.nullvalue.extra", ".nullvalue a b"),
    ("dot.limit.badvalue", ".limit column notanumber"),
    ("dot.dbconfig.badvalue", ".dbconfig defensive maybe"),
    ("dot.timeout.bad", ".timeout notanumber"),
    ("dot.eqp.bad", ".eqp maybe"),
    ("dot.explain.bad", ".explain maybe"),
    ("dot.import.noargs", ".import"),
    ("dot.import.onearg", ".import /data/plain.csv"),
    ("dot.output.noargs", f"{_T}\n.output\nselect 1;"),
    ("dot.read.noargs", ".read"),
    ("dot.schema.extra", ".schema a b"),
    ("dot.tables.extra", ".tables a b"),
    ("dot.dump.after.error", "select nosuch;\n.dump"),
    ("sql.unterminated", "select 1"),
    ("sql.unterminated.string", "select 'abc"),
    ("sql.unterminated.comment", "select 1; /* open"),
    ("sql.semicolons", ";;;select 1;;;"),
    ("sql.only.whitespace", "   \n\t\n"),
    ("sql.after.dot.error", ".nosuch\nselect 42;"),
    ("exit.code.ok", "select 1;\n.exit"),
    ("exit.code.value", "select 1;\n.exit 3"),
    ("exit.quit", "select 1;\n.quit"),
    ("exit.after.error", "select nosuch;\n.exit"),
)


def _errors() -> list[Case]:
    cases: list[Case] = []
    for key, script in _ERROR_CASES:
        cases.append(_cli(
            f"error.{key}", f"cli.error.{key.rsplit('.', 1)[0]}",
            argv=(DB,), stdin=script,
            files={"notadb": "this is not a database at all\n",
                   "plain.csv": "1,a\n"},
        ))
    return cases


def build() -> list[Case]:
    cases: list[Case] = []
    for part in (_modes, _flags, _config, _schema, _io, _import, _plans, _errors):
        cases.extend(part())
    return cases
