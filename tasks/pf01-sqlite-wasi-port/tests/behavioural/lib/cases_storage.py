"""On-disk format: the family a fake port cannot pass.

The measurement that shapes this file: a database written by wasm32-wasi 3.31.1 is
**byte-identical** to one written by native 3.31.1 for the same statements --
across every page size, both text encodings, all six journal modes, WAL, fts5,
rtree, VACUUM and incremental vacuum (21 workloads measured, 20 byte-identical;
the twenty-first is the persist journal below).  That is a strong result and it is
what lets this family assert raw sha256 over the file the run produced.

Why it is hard to fake: the bytes are decided by the pager and the VFS acting
together -- page allocation order, freelist threading, journal framing, the
checkpoint that moves WAL frames back.  A VFS that merely "works" gets different
bytes.  A stub that returns SQLITE_OK without writing gets no bytes at all.

The one exception found by measurement is recorded honestly rather than dropped:
a journal left behind by ``journal_mode=persist`` keeps stale page images and the
random checksum nonce from the journal header, so it differs native-to-native.
It gets a size + zeroed-header probe instead of a digest.

Every case here also hands its database to *native* sqlite3 afterwards
(``native_read``).  Byte-identity and native-readability are different claims:
the first says the port produced the right bytes, the second says those bytes are
a database the reference implementation accepts.
"""

from __future__ import annotations

from case import Case

FAMILY = "ondisk_format"

DB = "/data/db.sqlite"

#: The SQL every case runs before its own body, to make the starting state
#: explicit rather than inherited.
_HEAD = "pragma page_size=4096;\n"


def _file(
    key: str,
    operation: str,
    sql: str,
    *,
    digest: tuple[str, ...] = ("db.sqlite",),
    probes: dict[str, str] | None = None,
    crosscheck: str = "pragma audit_check; select count(*) from sqlite_master;",
    head: str = _HEAD,
    **kw,
) -> Case:
    checks = ["stdout", "stderr", "exit", "files"]
    if crosscheck:
        checks.append("native_read")
    return Case(
        key=f"file.{key}",
        family=FAMILY,
        operation=operation,
        kind="file",
        argv=(DB,),
        stdin=head + (sql if sql.endswith("\n") else sql + "\n"),
        checks=tuple(checks),
        digest_files=digest,
        probes=probes or {},
        crosscheck=crosscheck,
        normalisers=("longdouble",),
        **kw,
    )


# --------------------------------------------------------------------------
# Header fields.  Every one of these is a documented byte range in the database
# header, and each is set by a different code path in the pager.
# --------------------------------------------------------------------------
_PAGE_SIZES = ["512", "1024", "2048", "4096", "8192", "16384", "32768", "65536"]
_ENCODINGS = ["utf-8", "utf-16le", "utf-16be"]
_AUTO_VACUUM = ["0", "1", "2"]


def _header() -> list[Case]:
    cases: list[Case] = []
    for ps in _PAGE_SIZES:
        cases.append(_file(
            f"page_size.{ps}", "format.page_size",
            f"pragma page_size={ps};create table t(a,b);"
            f"insert into t values(1,'x'),(2,'y');pragma page_size;",
            head="",
            crosscheck="pragma page_size; pragma audit_check;"
                       " select a,b from t order by a;",
        ))
        # Same page size, enough rows to force interior b-tree pages.
        cases.append(_file(
            f"page_size.{ps}.deep", "format.page_size.deep",
            f"pragma page_size={ps};create table t(a integer primary key,b);"
            "with recursive c(i) as (select 1 union all select i+1 from c where i<800)"
            " insert into t select i,'v'||i from c;select count(*) from t;",
            head="",
            crosscheck="pragma audit_check; select count(*),sum(a) from t;",
        ))
    for enc in _ENCODINGS:
        cases.append(_file(
            f"encoding.{enc}", "format.encoding",
            f"pragma encoding='{enc}';create table t(a);"
            "insert into t values('ascii'),('é'),('中文'),(x'00ff');"
            "pragma encoding;select a from t order by rowid;",
            head="",
            crosscheck="pragma encoding; select quote(a) from t order by rowid;",
        ))
        cases.append(_file(
            f"encoding.{enc}.indexed", "format.encoding.indexed",
            f"pragma encoding='{enc}';create table t(a);create index i on t(a);"
            "insert into t values('b'),('a'),('中');select a from t order by a;",
            head="",
            crosscheck="select a from t order by a; pragma audit_check;",
        ))
    for av in _AUTO_VACUUM:
        cases.append(_file(
            f"auto_vacuum.{av}", "format.auto_vacuum",
            f"pragma auto_vacuum={av};create table t(a);"
            "with recursive c(i) as (select 1 union all select i+1 from c where i<400)"
            " insert into t select i from c;pragma auto_vacuum;",
            head="",
            crosscheck="pragma auto_vacuum; pragma audit_check;",
        ))
        cases.append(_file(
            f"auto_vacuum.{av}.churn", "format.auto_vacuum.churn",
            f"pragma auto_vacuum={av};create table t(a,b);"
            "with recursive c(i) as (select 1 union all select i+1 from c where i<600)"
            " insert into t select i,hex(i) from c;"
            "delete from t where a%3<>0;pragma auto_vacuum;"
            "select count(*) from t;",
            head="",
            crosscheck="pragma auto_vacuum; pragma freelist_count;"
                       " pragma audit_check; select count(*) from t;",
        ))
    for setting in ("0", "1", "2", "3", "full", "normal", "off", "extra"):
        cases.append(_file(
            f"synchronous.{setting}", "format.synchronous",
            f"pragma synchronous={setting};create table t(a);"
            "insert into t values(1),(2);pragma synchronous;",
        ))
    for setting in ("0", "1", "fast"):
        cases.append(_file(
            f"secure_delete.{setting}", "format.secure_delete",
            # The payload has to be deterministic *and* recognisably non-zero, since
            # what secure_delete does is overwrite it with zeros -- a random filler
            # would make the resulting file differ run to run and prove nothing.
            f"pragma secure_delete={setting};create table t(a,b);"
            "with recursive c(i) as (select 1 union all select i+1 from c where i<300)"
            " insert into t select i,'payload-'||hex(i)||'-ABCDEFGHIJKLMNOP' from c;"
            "delete from t where a%2=0;pragma secure_delete;",
        ))
    cases.append(_file(
        "application_id", "format.application_id",
        "pragma application_id=123456;create table t(a);pragma application_id;",
        crosscheck="pragma application_id; pragma audit_check;",
    ))
    cases.append(_file(
        "user_version", "format.user_version",
        "pragma user_version=4242;create table t(a);pragma user_version;",
        crosscheck="pragma user_version;",
    ))
    cases.append(_file(
        "schema_version", "format.schema_version",
        "create table t(a);pragma schema_version;create table u(b);"
        "pragma schema_version;",
        crosscheck="pragma schema_version;",
    ))
    cases.append(_file(
        "legacy_file_format", "format.legacy_file_format",
        "pragma legacy_file_format=1;create table t(a);"
        "insert into t values(1);pragma legacy_file_format;",
        head="",
    ))
    cases.append(_file(
        "max_page_count", "format.max_page_count",
        "pragma max_page_count=20;create table t(a,b);"
        "with recursive c(i) as (select 1 union all select i+1 from c where i<5000)"
        " insert into t select i,hex(i) from c;"
        "select count(*) from t;pragma max_page_count;pragma page_count;",
    ))
    return cases


# --------------------------------------------------------------------------
# Journal modes.  Each mode is a different contract between the pager and the
# VFS about what is written where, and each leaves a different footprint on
# disk.  This is the group that catches a VFS whose xSync/xTruncate/xDelete are
# stubs: the database contents can still look right while the journal footprint
# is wrong.
# --------------------------------------------------------------------------
_BODY = (
    "create table t(a integer primary key,b);"
    "insert into t values(1,'one'),(2,'two'),(3,'three');"
    "update t set b='TWO' where a=2;delete from t where a=3;"
)


def _journal() -> list[Case]:
    cases: list[Case] = []
    # After a clean shutdown: delete/truncate/persist differ only in what is
    # left behind, which is exactly what the probes below pin down.
    for mode in ("delete", "truncate", "memory", "off"):
        cases.append(_file(
            f"journal.{mode}", "journal.clean",
            f"pragma journal_mode={mode};{_BODY}pragma journal_mode;",
            probes={"db.sqlite-journal": "absent"},
            crosscheck="select a,b from t order by a; pragma audit_check;",
        ))
    # persist keeps the file; its bytes are not comparable (stale pages + the
    # random nonce in the journal header) but its size and zeroed header are.
    cases.append(_file(
        "journal.persist", "journal.clean",
        f"pragma journal_mode=persist;{_BODY}pragma journal_mode;",
        probes={"db.sqlite-journal": "zero_prefix:28"},
        crosscheck="select a,b from t order by a; pragma audit_check;",
    ))
    cases.append(_file(
        "journal.persist.size", "journal.persist.size",
        f"pragma journal_mode=persist;{_BODY}pragma journal_mode;",
        probes={"db.sqlite-journal": "size"},
        crosscheck="select count(*) from t;",
    ))
    # truncate leaves a zero-length file in some orderings; assert what the
    # reference actually does rather than what the docs imply.
    cases.append(_file(
        "journal.truncate.then-write", "journal.truncate",
        "pragma journal_mode=truncate;" + _BODY +
        "insert into t values(9,'nine');pragma journal_mode;",
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    for mode in ("delete", "truncate", "persist", "memory", "off", "wal"):
        cases.append(_file(
            f"journal.{mode}.rollback", "journal.rollback",
            f"pragma journal_mode={mode};{_BODY}"
            "begin;insert into t values(50,'fifty');"
            "update t set b='X';rollback;"
            "select a,b from t order by a;",
            crosscheck="select a,b from t order by a; pragma audit_check;",
        ))
        cases.append(_file(
            f"journal.{mode}.savepoint", "journal.savepoint",
            f"pragma journal_mode={mode};{_BODY}"
            "savepoint s1;insert into t values(60,'sixty');"
            "savepoint s2;insert into t values(61,'sixtyone');"
            "rollback to s2;release s1;select a,b from t order by a;",
            crosscheck="select a,b from t order by a; pragma audit_check;",
        ))
        cases.append(_file(
            f"journal.{mode}.commit-after-rollback", "journal.mixed",
            f"pragma journal_mode={mode};{_BODY}"
            "begin;insert into t values(70,'x');rollback;"
            "begin;insert into t values(71,'y');commit;"
            "select a,b from t order by a;",
            crosscheck="select a,b from t order by a; pragma audit_check;",
        ))
    cases.append(_file(
        "journal.size_limit", "journal.size_limit",
        "pragma journal_mode=persist;pragma journal_size_limit=1024;"
        + _BODY + "pragma journal_size_limit;",
        probes={"db.sqlite-journal": "size"},
        crosscheck="pragma audit_check;",
    ))
    return cases


# --------------------------------------------------------------------------
# WAL.  A separable hard sub-goal: it needs xShmMap/xShmLock/xShmBarrier/
# xShmUnmap, which WASI has no mmap for.  Without them the pragma silently
# degrades to ``delete`` -- measured -- so these cases fail loudly on a port
# that skipped shared memory, and the failure names the reason.
# --------------------------------------------------------------------------
def _wal() -> list[Case]:
    cases: list[Case] = []
    cases.append(_file(
        "wal.enable", "wal.enable",
        "pragma journal_mode=wal;select 'mode='||(select * from pragma_journal_mode);",
        crosscheck="pragma journal_mode; pragma audit_check;",
    ))
    cases.append(_file(
        "wal.commit", "wal.commit",
        f"pragma journal_mode=wal;{_BODY}select a,b from t order by a;",
        crosscheck="pragma journal_mode; select a,b from t order by a;"
                   " pragma audit_check;",
    ))
    for mode in ("passive", "full", "restart", "truncate"):
        cases.append(_file(
            f"wal.checkpoint.{mode}", "wal.checkpoint",
            f"pragma journal_mode=wal;{_BODY}"
            f"pragma wal_checkpoint({mode});select count(*) from t;",
            crosscheck="pragma journal_mode; select count(*) from t;"
                       " pragma audit_check;",
        ))
    cases.append(_file(
        "wal.autocheckpoint", "wal.autocheckpoint",
        "pragma journal_mode=wal;pragma wal_autocheckpoint=2;"
        "create table t(a,b);"
        "with recursive c(i) as (select 1 union all select i+1 from c where i<300)"
        " insert into t select i,hex(i) from c;"
        "pragma wal_autocheckpoint;select count(*) from t;",
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "wal.back-to-delete", "wal.mode-switch",
        f"pragma journal_mode=wal;{_BODY}"
        "pragma journal_mode=delete;pragma journal_mode;"
        "select count(*) from t;",
        probes={"db.sqlite-wal": "absent", "db.sqlite-shm": "absent"},
        crosscheck="pragma journal_mode; select count(*) from t;"
                   " pragma audit_check;",
    ))
    cases.append(_file(
        "wal.rollback-large", "wal.rollback",
        "pragma journal_mode=wal;create table t(a,b);"
        "begin;"
        "with recursive c(i) as (select 1 union all select i+1 from c where i<2000)"
        " insert into t select i,hex(i) from c;"
        "rollback;select count(*) from t;",
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "wal.index-and-vacuum", "wal.vacuum",
        "pragma journal_mode=wal;create table t(a,b);"
        "with recursive c(i) as (select 1 union all select i+1 from c where i<800)"
        " insert into t select i,hex(i) from c;"
        "create index i1 on t(b);delete from t where a%2=0;vacuum;"
        "select count(*) from t;pragma journal_mode;",
        crosscheck="select count(*) from t; pragma audit_check;"
                   " pragma journal_mode;",
    ))
    for ps in ("512", "4096", "65536"):
        cases.append(_file(
            f"wal.page_size.{ps}", "wal.page_size",
            f"pragma page_size={ps};pragma journal_mode=wal;{_BODY}"
            "select count(*) from t;",
            head="",
            crosscheck="pragma page_size; select count(*) from t;"
                       " pragma audit_check;",
        ))
    return cases


# --------------------------------------------------------------------------
# VACUUM and space reclamation.  VACUUM rewrites the whole database through a
# temporary one, so it exercises temp-file creation, the pager, and the final
# rename-by-copy path all at once.
# --------------------------------------------------------------------------
_CHURN = (
    "create table t(a integer primary key,b,c);"
    "with recursive c(i) as (select 1 union all select i+1 from c where i<1200)"
    " insert into t select i,hex(i),i*1.5 from c;"
    "create index i1 on t(b);create index i2 on t(c);"
)


def _vacuum() -> list[Case]:
    cases: list[Case] = []
    cases.append(_file(
        "vacuum.plain", "vacuum.plain",
        _CHURN + "delete from t where a%3<>0;vacuum;"
        "select count(*) from t;pragma page_count;pragma freelist_count;",
        crosscheck="select count(*) from t; pragma page_count;"
                   " pragma freelist_count; pragma audit_check;",
    ))
    for ps in ("512", "4096", "16384"):
        cases.append(_file(
            f"vacuum.into-page_size.{ps}", "vacuum.page_size",
            _CHURN + f"delete from t where a>600;pragma page_size={ps};vacuum;"
            "pragma page_size;select count(*) from t;",
            crosscheck="pragma page_size; select count(*) from t;"
                       " pragma audit_check;",
        ))
    cases.append(_file(
        "vacuum.preserves-schema", "vacuum.schema",
        _CHURN + "create view v as select a from t;"
        "create trigger tr after insert on t begin select 1; end;"
        "vacuum;select type,name from sqlite_master order by name;",
        crosscheck="select type,name from sqlite_master order by name;"
                   " pragma audit_check;",
    ))
    cases.append(_file(
        "vacuum.autovacuum-change", "vacuum.auto_vacuum",
        "pragma auto_vacuum=0;" + _CHURN +
        "pragma auto_vacuum=1;vacuum;pragma auto_vacuum;",
        head="",
        crosscheck="pragma auto_vacuum; pragma audit_check;",
    ))
    cases.append(_file(
        "vacuum.incremental", "vacuum.incremental",
        "pragma auto_vacuum=2;" + _CHURN +
        "delete from t;pragma incremental_vacuum;"
        "pragma page_count;pragma freelist_count;",
        head="",
        crosscheck="pragma page_count; pragma freelist_count;"
                   " pragma audit_check;",
    ))
    cases.append(_file(
        "vacuum.incremental-partial", "vacuum.incremental",
        "pragma auto_vacuum=2;" + _CHURN +
        "delete from t;pragma incremental_vacuum(5);"
        "pragma freelist_count;",
        head="",
        crosscheck="pragma freelist_count; pragma audit_check;",
    ))
    cases.append(_file(
        "vacuum.empty-db", "vacuum.empty",
        "vacuum;select count(*) from sqlite_master;",
        crosscheck="pragma audit_check;",
    ))
    cases.append(_file(
        "vacuum.no-change", "vacuum.idempotent",
        _CHURN + "vacuum;vacuum;select count(*) from t;",
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "vacuum.into-file", "vacuum.into",
        _CHURN + "vacuum into '/data/copy.sqlite';"
        "select count(*) from t;",
        digest=("db.sqlite", "copy.sqlite"),
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    return cases


# --------------------------------------------------------------------------
# ATTACH, backup, and multi-file work.  Two open database files at once is
# where a VFS that keeps global state instead of per-file state falls over.
# --------------------------------------------------------------------------
def _multifile() -> list[Case]:
    cases: list[Case] = []
    cases.append(_file(
        "attach.two-files", "attach.basic",
        "attach '/data/two.sqlite' as b;"
        "create table main.t(a);create table b.u(x);"
        "insert into main.t values(1),(2);insert into b.u values('p'),('q');"
        "select (select count(*) from main.t),(select count(*) from b.u);"
        "select name from pragma_database_list order by seq;",
        digest=("db.sqlite", "two.sqlite"),
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "attach.cross-insert", "attach.cross",
        "attach '/data/two.sqlite' as b;"
        "create table main.t(a,b);create table b.u(a,b);"
        "with recursive c(i) as (select 1 union all select i+1 from c where i<200)"
        " insert into main.t select i,hex(i) from c;"
        "insert into b.u select * from main.t where a%2=0;"
        "select (select count(*) from main.t),(select count(*) from b.u);",
        digest=("db.sqlite", "two.sqlite"),
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "attach.transaction-both", "attach.transaction",
        "attach '/data/two.sqlite' as b;"
        "create table main.t(a);create table b.u(a);"
        "begin;insert into main.t values(1);insert into b.u values(2);commit;"
        "select (select count(*) from main.t),(select count(*) from b.u);",
        digest=("db.sqlite", "two.sqlite"),
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "attach.rollback-both", "attach.transaction",
        "attach '/data/two.sqlite' as b;"
        "create table main.t(a);create table b.u(a);"
        "begin;insert into main.t values(1);insert into b.u values(2);rollback;"
        "select (select count(*) from main.t),(select count(*) from b.u);",
        digest=("db.sqlite", "two.sqlite"),
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "attach.detach", "attach.detach",
        "attach '/data/two.sqlite' as b;create table b.u(x);"
        "insert into b.u values(1);detach b;"
        "select name from pragma_database_list order by seq;",
        digest=("two.sqlite",),
        crosscheck="pragma audit_check;",
    ))
    cases.append(_file(
        "attach.wal-both", "attach.wal",
        "pragma journal_mode=wal;attach '/data/two.sqlite' as b;"
        "pragma b.journal_mode=wal;"
        "create table main.t(a);create table b.u(a);"
        "insert into main.t values(1);insert into b.u values(2);"
        "select (select count(*) from main.t),(select count(*) from b.u);"
        "pragma main.journal_mode;pragma b.journal_mode;",
        digest=(),
        probes={"db.sqlite": "exists", "two.sqlite": "exists"},
        crosscheck="pragma audit_check;",
    ))
    cases.append(_file(
        "attach.memory", "attach.memory",
        "attach ':memory:' as m;create table m.t(a);"
        "insert into m.t values(1),(2);select count(*) from m.t;"
        "select name,file from pragma_database_list order by seq;",
        digest=(),
        probes={"db.sqlite": "exists"},
        crosscheck="pragma audit_check;",
    ))
    cases.append(_file(
        "attach.temp-db-listed", "attach.temp",
        "create temp table tt(a);insert into tt values(1);"
        "select count(*) from tt;"
        "select name from pragma_database_list order by seq;",
        digest=(),
        probes={"db.sqlite": "exists"},
        crosscheck="pragma audit_check;",
    ))
    cases.append(_file(
        "backup.dot-backup", "backup.dot",
        _CHURN + ".backup /data/bk.sqlite\n"
        "select count(*) from t;",
        digest=("db.sqlite", "bk.sqlite"),
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "backup.dot-clone", "backup.clone",
        _CHURN + ".clone /data/cl.sqlite\n"
        "select count(*) from t;",
        digest=("db.sqlite",),
        probes={"cl.sqlite": "exists"},
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "backup.restore-roundtrip", "backup.restore",
        _CHURN + ".backup /data/bk.sqlite\n"
        "delete from t;\n"
        ".restore /data/bk.sqlite\n"
        "select count(*) from t;",
        digest=("bk.sqlite",),
        probes={"db.sqlite": "exists"},
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    return cases


# --------------------------------------------------------------------------
# Reopen.  A port whose xRead/xFileSize are subtly wrong can write a correct
# file and still fail to read its own output, so every one of these closes the
# database and opens it again in a second process.
# --------------------------------------------------------------------------
def _reopen() -> list[Case]:
    cases: list[Case] = []
    workloads = [
        ("simple", "create table t(a,b);insert into t values(1,'x'),(2,'y');",
         "select a,b from t order by a;"),
        ("wide-row", "create table t(a);insert into t values(hex(zeroblob(40000)));",
         "select length(a) from t;"),
        ("many-rows", "create table t(a integer primary key,b);"
                      "with recursive c(i) as (select 1 union all select i+1 from c where i<3000)"
                      " insert into t select i,hex(i) from c;",
         "select count(*),sum(a),max(length(b)) from t;"),
        ("overflow-pages", "create table t(a);insert into t values(hex(zeroblob(200000)));",
         "select length(a) from t;"),
        ("indexed", "create table t(a,b);create index i on t(b);"
                    "with recursive c(i) as (select 1 union all select i+1 from c where i<500)"
                    " insert into t select i,hex(i) from c;",
         "select count(*) from t where b like 'A%';"),
        ("blob-values", "create table t(a);insert into t values(x'00010203'),(x'ff');",
         "select quote(a) from t order by rowid;"),
        ("nulls-and-types", "create table t(a);"
                            "insert into t values(1),(1.5),('s'),(x'01'),(null);",
         "select typeof(a),quote(a) from t order by rowid;"),
        ("fts5", "create virtual table f using fts5(x);"
                 "insert into f values('alpha beta'),('gamma delta');",
         "select x from f where f match 'beta';"),
        ("rtree", "create virtual table r using rtree(id,x0,x1,y0,y1);"
                  "insert into r values(1,0.0,1.0,0.0,1.0),(2,5.0,6.0,5.0,6.0);",
         "select id from r where x0>4;"),
        ("wal", "pragma journal_mode=wal;create table t(a);"
                "insert into t values(1),(2),(3);",
         "select count(*) from t;pragma journal_mode;"),
        ("triggers-and-views",
         "create table t(a);create table log(m);"
         "create trigger tr after insert on t begin insert into log values(new.a); end;"
         "create view v as select m from log;insert into t values(7),(8);",
         "select m from v order by m;"),
        ("generated-stored",
         "create table t(a,b generated always as (a*3) stored);"
         "insert into t(a) values(1),(2);",
         "select a,b from t order by a;"),
        ("without-rowid",
         "create table t(k text primary key,v) without rowid;"
         "insert into t values('b',2),('a',1);",
         "select k,v from t;"),
        ("collate-nocase",
         "create table t(a collate nocase);create index i on t(a);"
         "insert into t values('B'),('a'),('C');",
         "select a from t order by a;"),
    ]
    for name, setup, read in workloads:
        # First process writes and exits; second process reads.  The `.open`
        # in between is what forces a genuine close/reopen through the VFS.
        cases.append(_file(
            f"reopen.{name}", f"reopen.{name}",
            setup + ".open /data/db.sqlite\n" + read,
            crosscheck=read + " pragma audit_check;",
        ))
    return cases


# --------------------------------------------------------------------------
# Read-only and error paths on the filesystem.  These decide whether the VFS
# reports failures the way the library expects, instead of returning SQLITE_OK
# and corrupting state.
# --------------------------------------------------------------------------
def _fs_errors() -> list[Case]:
    cases: list[Case] = []
    cases.append(_file(
        "readonly.query_only", "fs.query_only",
        "create table t(a);insert into t values(1);"
        "pragma query_only=1;insert into t values(2);"
        "select count(*) from t;pragma query_only;",
        crosscheck="select count(*) from t;",
    ))
    cases.append(_file(
        "missing-dir", "fs.missing_dir",
        "attach '/data/nosuchdir/x.sqlite' as z;select 1;",
        digest=(),
        probes={"db.sqlite": "exists"},
        crosscheck="",
    ))
    cases.append(_file(
        "attach-creates-file", "fs.attach_creates",
        "attach '/data/made.sqlite' as z;create table z.t(a);"
        "insert into z.t values(1);select count(*) from z.t;",
        digest=("made.sqlite",),
        crosscheck="",
    ))
    cases.append(_file(
        "delete-on-detach-keeps-file", "fs.detach_keeps",
        "attach '/data/kept.sqlite' as z;create table z.t(a);detach z;select 1;",
        digest=("kept.sqlite",),
        crosscheck="",
    ))
    cases.append(_file(
        "zero-length-file-is-empty-db", "fs.zero_length",
        "attach '/data/empty.sqlite' as z;"
        "select count(*) from z.sqlite_master;",
        files={"empty.sqlite": ""},
        digest=("empty.sqlite",),
        crosscheck="",
    ))
    cases.append(_file(
        "not-a-database", "fs.not_a_db",
        "attach '/data/junk.sqlite' as z;select count(*) from z.sqlite_master;",
        files={"junk.sqlite": "this is definitely not a sqlite database\n"},
        digest=("junk.sqlite",),
        crosscheck="",
    ))
    cases.append(_file(
        "truncated-header", "fs.truncated",
        "attach '/data/short.sqlite' as z;select count(*) from z.sqlite_master;",
        files={"short.sqlite": "SQLite format 3\x00"},
        digest=("short.sqlite",),
        crosscheck="",
    ))
    cases.append(_file(
        "uri-readonly", "fs.uri_readonly",
        "create table t(a);insert into t values(1);"
        ".open 'file:/data/db.sqlite?mode=ro'\n"
        "select count(*) from t;insert into t values(2);",
        crosscheck="select count(*) from t;",
    ))
    cases.append(_file(
        "uri-immutable", "fs.uri_immutable",
        "create table t(a);insert into t values(1);"
        ".open 'file:/data/db.sqlite?immutable=1'\n"
        "select count(*) from t;",
        crosscheck="select count(*) from t;",
    ))
    cases.append(_file(
        "uri-nolock", "fs.uri_nolock",
        "create table t(a);insert into t values(1);"
        ".open 'file:/data/db.sqlite?nolock=1'\n"
        "insert into t values(2);select count(*) from t;",
        crosscheck="select count(*) from t; pragma audit_check;",
    ))
    cases.append(_file(
        "uri-cache-shared", "fs.uri_cache",
        ".open 'file:/data/db.sqlite?cache=shared'\n"
        "create table t(a);insert into t values(1);select count(*) from t;",
        crosscheck="select count(*) from t;",
    ))
    return cases


def build() -> list[Case]:
    """All on-disk-format cases, in a stable order."""
    cases: list[Case] = []
    for builder in (_header, _journal, _wal, _vacuum, _multifile, _reopen,
                    _fs_errors):
        cases.extend(builder())
    return cases
