"""Engine semantics: cases that touch no file and no host facility.

These are the cases a port passes as soon as it *compiles and runs* -- and fails
in droves if it does not.  They are cheap to run and they are the difference
between "the module loads" and "the module is SQLite": integer overflow
promotion, float formatting to the last digit, collation order, the exact text of
an error message.  wasm32 is a 32-bit target with a different compiler, so none
of that is free; every one of these was confirmed byte-identical between the two
builds before being written down.

Deliberately excluded, because they are not functions of the port:
``random()``/``randomblob()`` values, ``'now'`` in any date function, anything
formatting a pointer, and ``.timer``/``.stats`` output.
"""

from __future__ import annotations

from case import Case

FAMILY = "sql_semantics"


def _sql(key: str, operation: str, sql: str, **kw) -> Case:
    # stderr is part of the contract, not incidental: for the error cases below
    # the diagnostic text *is* the observable, and for the rest an unexpected
    # warning on stderr should fail the case rather than pass unnoticed.
    kw.setdefault("checks", ("stdout", "stderr", "exit"))
    # `longdouble` is carried by every case in this family.  It is a no-op unless
    # the output contains a mantissa longer than a double can hold, which only
    # happens on sqlite3.c's "%!.20e" fallback path and is a property of the
    # host's `long double`, not of the value -- see normalise.longdouble.  The
    # property that path is *for* is asserted separately by the quote-roundtrip
    # cases below, so nothing is lost by rounding the noise digits off.
    kw["normalisers"] = tuple(kw.get("normalisers", ())) + ("longdouble",)
    # `kind` is overridable for the single case in this family whose answer is a
    # platform literal rather than a recorded one -- see `misc.source-id-value`.
    # It stays in this family because it is a SQL-surface case and belongs beside
    # the other identity functions; only where its expectation comes from differs.
    return Case(
        key=f"sql.{key}",
        family=FAMILY,
        operation=operation,
        kind=kw.pop("kind", "sql"),
        argv=(":memory:",),
        stdin=sql if sql.endswith("\n") else sql + "\n",
        **kw,
    )


# --------------------------------------------------------------------------
# Integer and real arithmetic.  A 32-bit host has to get 64-bit integer maths
# and the overflow-to-real promotion right, and those are different code paths
# in the VDBE than on a 64-bit host.
# --------------------------------------------------------------------------
_INT_EDGES = [
    "0", "1", "-1", "2147483647", "2147483648", "-2147483648", "-2147483649",
    "4294967295", "4294967296", "9223372036854775807", "-9223372036854775808",
    "9223372036854775806", "-9223372036854775807",
]

_ARITH_OPS = ["+", "-", "*", "/", "%"]


def _arithmetic() -> list[Case]:
    cases: list[Case] = []
    for i, left in enumerate(_INT_EDGES):
        for op in _ARITH_OPS:
            # Pair each edge with a small operand and with another edge, so both
            # "does it promote" and "does it stay exact" get exercised.
            right = "3" if op in "%/" else "1"
            cases.append(
                _sql(
                    f"arith.int.{i}.{_opname(op)}.small",
                    f"vdbe.arith.{_opname(op)}",
                    f"select ({left}) {op} ({right}), typeof(({left}) {op} ({right}));",
                )
            )
        cases.append(
            _sql(
                f"arith.int.{i}.unary",
                "vdbe.arith.unary",
                f"select -({left}), +({left}), typeof(-({left}));",
            )
        )
        cases.append(
            _sql(
                f"arith.int.{i}.bitwise",
                "vdbe.arith.bitwise",
                f"select ({left}) & 255, ({left}) | 1, ({left}) << 1, ({left}) >> 1, ~({left});",
            )
        )
        cases.append(
            _sql(
                f"arith.int.{i}.abs",
                "func.abs",
                f"select abs({left}), typeof(abs({left}));",
            )
        )
    for i, a in enumerate(_INT_EDGES):
        for j, b in enumerate(_INT_EDGES):
            if (i + j) % 5:
                continue
            cases.append(
                _sql(
                    f"arith.pair.{i}.{j}",
                    "vdbe.arith.pair",
                    f"select ({a})+({b}), ({a})-({b}), typeof(({a})*({b}));",
                )
            )
    return cases


def _opname(op: str) -> str:
    return {"+": "add", "-": "sub", "*": "mul", "/": "div", "%": "mod"}[op]


_REALS = [
    "0.0", "1.0", "-1.0", "0.1", "0.5", "1e-300", "1e300", "1.5e-10",
    "3.141592653589793", "2.718281828459045", "1e17", "1e16", "9007199254740993.0",
    "0.30000000000000004", "1.7976931348623157e308", "5e-324",
]


def _reals() -> list[Case]:
    cases: list[Case] = []
    for i, value in enumerate(_REALS):
        cases.append(
            _sql(f"real.repr.{i}", "vdbe.real.repr",
                 f"select {value}, cast({value} as text), typeof({value});")
        )
        cases.append(
            _sql(f"real.round.{i}", "func.round",
                 f"select round({value}), round({value},3), round({value},15);")
        )
        cases.append(
            _sql(f"real.cast.{i}", "vdbe.cast.real",
                 f"select cast({value} as integer), cast({value} as numeric);")
        )
        for op in ["+", "-", "*", "/"]:
            cases.append(
                _sql(f"real.arith.{i}.{_opname(op)}", f"vdbe.real.{_opname(op)}",
                     f"select ({value}) {op} 3.0;")
            )
    cases.append(_sql("real.div-zero", "vdbe.real.divzero",
                      "select 1.0/0.0, -1.0/0.0, 0.0/0.0, 1/0, typeof(1/0);"))
    cases.append(_sql("real.inf-compare", "vdbe.real.inf",
                      "select 1e400, -1e400, 1e400=1e400, cast(1e400 as integer);"))
    return cases


# --------------------------------------------------------------------------
# printf: the single densest formatting surface in the product, and the one most
# likely to move under a different libc.  SQLite implements it itself, which is
# exactly why it must not move -- if it does, the port reached for the host's.
# --------------------------------------------------------------------------
_PRINTF = [
    ("%d", "255"), ("%d", "-255"), ("%i", "42"), ("%u", "4294967295"),
    ("%x", "255"), ("%X", "255"), ("%o", "255"), ("%c", "65"),
    ("%5d", "42"), ("%-5d|", "42"), ("%05d", "42"), ("%+d", "42"),
    ("%d", "9223372036854775807"), ("%d", "-9223372036854775808"),
    ("%f", "3.14159"), ("%.0f", "3.5"), ("%.0f", "2.5"), ("%.10f", "1.0/3"),
    ("%.20f", "0.1"), ("%e", "12345.6789"), ("%E", "12345.6789"),
    ("%g", "0.00001"), ("%G", "100000000.0"), ("%g", "123456789.0"),
    ("%s", "'txt'"), ("%10s|", "'txt'"), ("%-10s|", "'txt'"), ("%.2s", "'txt'"),
    ("%q", "'it''s'"), ("%Q", "'it''s'"), ("%Q", "null"), ("%w", "'a\"b'"),
    ("%z", "'zz'"), ("%%", "1"), ("%s%d%s", "'a',1,'b'"),
    ("%!.15g", "0.1"), ("%.3e", "0.0"), ("%08.3f", "-1.5"),
]


def _printf() -> list[Case]:
    cases = []
    for i, (fmt, args) in enumerate(_PRINTF):
        cases.append(
            _sql(f"printf.{i}", "func.printf",
                 f"select printf('{fmt}', {args});")
        )
    cases.append(_sql("printf.nul-arg", "func.printf.missing",
                      "select printf('%d %d', 1);"))
    cases.append(_sql("printf.extra-arg", "func.printf.extra",
                      "select printf('%d', 1, 2);"))
    cases.append(_sql("printf.no-arg", "func.printf.noarg", "select printf('plain');"))
    cases.append(_sql("printf.width-star", "func.printf.star",
                      "select printf('%*d', 6, 42);"))
    return cases


# --------------------------------------------------------------------------
# Strings, and the UTF-8 handling underneath them.
# --------------------------------------------------------------------------
_STRINGS = [
    "''", "'a'", "'abc'", "'ABC'", "'héllo'", "'日本語'", "'a b'", "'  pad  '",
    "'tab\ttab'", "'quote''s'", "'ünïcödé'", "'ÀÉÎÕÜ'", "'straße'",
    "'🙂emoji'", "'mixed AbC 123'", "'x'||char(0)||'y'",
]

_STR_FUNCS = [
    "upper({s})", "lower({s})", "length({s})", "hex({s})", "quote({s})",
    "trim({s})", "ltrim({s})", "rtrim({s})", "substr({s},2)", "substr({s},2,3)",
    "substr({s},-2)", "instr({s},'a')", "replace({s},'a','Z')",
    "char(length({s})+64)", "unicode({s})", "typeof({s})",
    "printf('%s|',{s})", "cast({s} as blob)", "ltrim({s},'aA ')",
    "rtrim({s},'cC ')", "trim({s},'aAcC ')", "substr({s},1,0)",
]


def _strings() -> list[Case]:
    cases = []
    for i, s in enumerate(_STRINGS):
        for j, fn in enumerate(_STR_FUNCS):
            cases.append(
                _sql(f"str.{i}.{j}", f"func.str.{j}",
                     f"select {fn.format(s=s)};")
            )
    cases.append(_sql("str.concat-null", "vdbe.concat.null",
                      "select 'a'||null, null||'a', ''||'';"))
    cases.append(_sql("str.char-multi", "func.char",
                      "select char(72,101,0x6c,108,111), char(), char(0);"))
    cases.append(_sql("str.hex-blob", "func.hex.blob",
                      "select hex(x''), hex(x'00ff10'), hex(cast('é' as blob));"))
    return cases


# --------------------------------------------------------------------------
# Comparison, affinity, NULL.  The three-valued logic and the affinity table are
# where a subtly wrong port produces *plausible* wrong answers.
# --------------------------------------------------------------------------
_CMP_VALUES = ["1", "1.0", "'1'", "'1.0'", "x'31'", "null", "0", "''", "'a'", "-0.0"]


def _comparisons() -> list[Case]:
    cases = []
    for i, a in enumerate(_CMP_VALUES):
        for j, b in enumerate(_CMP_VALUES):
            if (i * len(_CMP_VALUES) + j) % 3:
                continue
            cases.append(
                _sql(f"cmp.{i}.{j}", "vdbe.compare",
                     f"select {a}={b}, {a}<{b}, {a}>{b}, {a} is {b}, "
                     f"{a} is not {b};")
            )
        cases.append(
            _sql(f"cmp.in.{i}", "vdbe.in",
                 f"select {a} in (1,'1',null), {a} not in (0,'a');")
        )
        cases.append(
            _sql(f"cmp.between.{i}", "vdbe.between",
                 f"select {a} between 0 and 2, {a} not between 0 and 2;")
        )
        cases.append(
            _sql(f"cmp.coalesce.{i}", "func.coalesce",
                 f"select coalesce({a},'d'), ifnull({a},'d'), nullif({a},1);")
        )
    cases.append(_sql("cmp.null-logic", "vdbe.null.logic",
                      "select null and 0, null and 1, null or 0, null or 1, not null;"))
    cases.append(_sql("cmp.affinity-table", "vdbe.affinity",
                      "create table t(i integer, r real, t text, b blob, n numeric);"
                      "insert into t values('1','1','1','1','1');"
                      "select typeof(i),typeof(r),typeof(t),typeof(b),typeof(n) from t;"))
    return cases


# --------------------------------------------------------------------------
# CAST, across every affinity pair that has a defined answer.
# --------------------------------------------------------------------------
_CAST_IN = [
    "'12abc'", "'abc'", "''", "'  42  '", "'0x1f'", "'1e3'", "'-.5'", "'+7'",
    "'9223372036854775808'", "'1.7976931348623157e309'", "42", "42.9", "-42.9",
    "x'41'", "x'00'", "null", "'inf'", "'nan'", "'1,000'",
]
_CAST_TO = ["integer", "real", "text", "blob", "numeric"]


def _casts() -> list[Case]:
    cases = []
    for i, value in enumerate(_CAST_IN):
        for to in _CAST_TO:
            cases.append(
                _sql(f"cast.{i}.{to}", f"vdbe.cast.{to}",
                     f"select cast({value} as {to}), typeof(cast({value} as {to}));")
            )
    return cases


# --------------------------------------------------------------------------
# Date and time, with fixed inputs only.  'now' would make the case a clock
# reading rather than a measurement of the port.
# --------------------------------------------------------------------------
_DATES = [
    "'2020-01-27'", "'2020-01-27 13:45:00'", "'2020-01-27T13:45:00'",
    "'2020-01-27 13:45:00.123'", "'2020-02-29'", "'1999-12-31 23:59:59'",
    "'1970-01-01'", "'2038-01-19 03:14:07'", "'0001-01-01'", "'9999-12-31'",
    "2451545.0", "'13:45:00'", "'2020-06-15 12:00:00-05:00'",
]
_DATE_FUNCS = ["date({d})", "time({d})", "datetime({d})", "julianday({d})",
               "strftime('%Y-%m-%d %H:%M:%S',{d})", "strftime('%s',{d})",
               "strftime('%j %W %w %U',{d})", "strftime('%f',{d})"]
_MODIFIERS = ["'+1 day'", "'-1 month'", "'+1 year'", "'start of month'",
              "'start of year'", "'start of day'", "'weekday 0'", "'weekday 3'",
              "'+36 hours'", "'-90 minutes'", "'+1.5 seconds'"]


def _dates() -> list[Case]:
    cases = []
    for i, d in enumerate(_DATES):
        for j, fn in enumerate(_DATE_FUNCS):
            cases.append(
                _sql(f"date.{i}.{j}", f"func.date.{j}",
                     f"select {fn.format(d=d)};")
            )
    for i, d in enumerate(_DATES[:8]):
        for j, mod in enumerate(_MODIFIERS):
            cases.append(
                _sql(f"date.mod.{i}.{j}", "func.date.modifier",
                     f"select datetime({d}, {mod});")
            )
    cases.append(_sql("date.unixepoch", "func.date.unixepoch",
                      "select datetime(0,'unixepoch'), datetime(1580131500,'unixepoch'),"
                      "datetime(-1,'unixepoch');"))
    cases.append(_sql("date.invalid", "func.date.invalid",
                      "select date('not-a-date'), datetime('2020-13-45'), julianday('x');"))
    cases.append(_sql("date.localtime-utc", "func.date.localtime",
                      "select datetime('2020-06-15 12:00:00','utc'),"
                      "datetime('2020-06-15 12:00:00','localtime');"))
    return cases


# --------------------------------------------------------------------------
# Collation and ordering.  BINARY/NOCASE/RTRIM are implemented in the product,
# so a port that quietly picked up the host's ``strcoll`` would sort differently
# here and nowhere else.
# --------------------------------------------------------------------------
_SORT_SETS = [
    "('b'),('A'),('a'),('B')",
    "('a '),('a'),('a  ')",
    "('10'),('9'),('1'),('100')",
    "(1),(10),(9),(2)",
    "('é'),('e'),('z'),('E')",
    "(null),(1),('1'),(x'31'),(1.0)",
    "('Z'),('a'),('_'),('0'),(' ')",
]
_COLLATIONS = ["", "collate binary", "collate nocase", "collate rtrim"]


def _collation() -> list[Case]:
    cases = []
    for i, values in enumerate(_SORT_SETS):
        for j, collate in enumerate(_COLLATIONS):
            for direction in ("asc", "desc"):
                cases.append(
                    _sql(
                        f"sort.{i}.{j}.{direction}",
                        f"vdbe.sort.{j}",
                        f"with t(x) as (values{values}) "
                        f"select group_concat(quote(x),'~') from "
                        f"(select x from t order by x {collate} {direction});",
                    )
                )
        cases.append(
            _sql(f"sort.distinct.{i}", "vdbe.distinct",
                 f"with t(x) as (values{values}) "
                 f"select count(*), count(distinct x) from t;")
        )
        cases.append(
            _sql(f"sort.nocase-eq.{i}", "vdbe.collate.eq",
                 f"with t(x) as (values{values}) "
                 f"select sum(x='a' collate nocase), sum(x='a' collate rtrim) from t;")
        )
    return cases


# --------------------------------------------------------------------------
# Aggregates and window functions.  3.31 has the full window surface, and the
# frame arithmetic is a dense, easily-broken corner.
# --------------------------------------------------------------------------
_AGGS = ["count(*)", "count(x)", "sum(x)", "total(x)", "avg(x)", "min(x)",
         "max(x)", "group_concat(x)", "group_concat(x,'-')",
         "count(distinct x)", "sum(distinct x)", "avg(distinct x)"]
_AGG_SETS = [
    "(1),(2),(3),(4),(5)",
    "(1),(1),(2),(null),(3)",
    "(null),(null)",
    "(-1),(0),(1)",
    "(9223372036854775807),(1)",
    "(0.1),(0.2),(0.3)",
    "('a'),('b'),(null)",
    "(1),(2.5),('3'),(null)",
]


def _aggregates() -> list[Case]:
    cases = []
    for i, values in enumerate(_AGG_SETS):
        for j, agg in enumerate(_AGGS):
            cases.append(
                _sql(f"agg.{i}.{j}", f"func.agg.{j}",
                     f"with t(x) as (values{values}) select {agg}, "
                     f"typeof({agg}) from t;")
            )
        cases.append(
            _sql(f"agg.having.{i}", "vdbe.having",
                 f"with t(x) as (values{values}) "
                 f"select x, count(*) from t group by x having count(*)>=1 order by x;")
        )
    return cases


_WINDOWS = [
    "row_number() over (order by x)",
    "rank() over (order by x)",
    "dense_rank() over (order by x)",
    "percent_rank() over (order by x)",
    "cume_dist() over (order by x)",
    "ntile(2) over (order by x)",
    "lag(x) over (order by x)",
    "lag(x,2,-1) over (order by x)",
    "lead(x) over (order by x)",
    "first_value(x) over (order by x)",
    "last_value(x) over (order by x)",
    "nth_value(x,2) over (order by x)",
    "sum(x) over (order by x)",
    "sum(x) over (order by x rows between 1 preceding and 1 following)",
    "sum(x) over (order by x range between unbounded preceding and current row)",
    "avg(x) over (partition by x%2 order by x)",
    "count(*) over ()",
    "group_concat(x) over (order by x rows between unbounded preceding and unbounded following)",
    "max(x) over (order by x rows between current row and unbounded following)",
    "min(x) over (order by x groups between 1 preceding and 1 following)",
]


def _windows() -> list[Case]:
    cases = []
    for i, values in enumerate(_AGG_SETS[:6]):
        for j, win in enumerate(_WINDOWS):
            cases.append(
                _sql(f"win.{i}.{j}", f"func.window.{j}",
                     f"with t(x) as (values{values}) "
                     f"select group_concat(quote(w),'~') from "
                     f"(select {win} w from t order by x);")
            )
    cases.append(_sql("win.named", "vdbe.window.named",
                      "with t(x) as (values(1),(2),(3)) "
                      "select group_concat(s) from (select sum(x) over w s from t "
                      "window w as (order by x));"))
    cases.append(_sql("win.filter", "vdbe.window.filter",
                      "with t(x) as (values(1),(2),(3),(4)) "
                      "select group_concat(s) from (select count(*) filter (where x%2=0) "
                      "over (order by x) s from t);"))
    return cases


# --------------------------------------------------------------------------
# Query planning surface: joins, subqueries, recursive CTEs, set operations.
# These run the byte-code generator hard, and a mis-sized pointer or integer in
# the port shows up here as a wrong *answer* rather than a crash.
# --------------------------------------------------------------------------
_SCHEMA = (
    "create table a(id integer primary key, v text, n integer);"
    "create table b(id integer primary key, aid integer references a(id), w real);"
    "insert into a values(1,'one',10),(2,'two',20),(3,'three',30),(4,null,null);"
    "insert into b values(1,1,1.5),(2,1,2.5),(3,2,3.5),(4,99,4.5);"
    "create index bi on b(aid);"
)

_QUERIES = [
    "select a.id,a.v,b.w from a join b on b.aid=a.id order by a.id,b.id",
    "select a.id,b.w from a left join b on b.aid=a.id order by a.id,b.id",
    "select a.id,b.w from b left join a on b.aid=a.id order by b.id",
    "select a.id,b.id from a,b order by a.id,b.id limit 5",
    "select a.id from a where exists(select 1 from b where b.aid=a.id) order by a.id",
    "select a.id from a where not exists(select 1 from b where b.aid=a.id) order by a.id",
    "select a.id from a where a.id in (select aid from b) order by a.id",
    "select a.id from a where a.id not in (select aid from b) order by a.id",
    "select (select count(*) from b where b.aid=a.id) c, a.id from a order by a.id",
    "select a.v, sum(b.w) from a join b on b.aid=a.id group by a.v order by a.v",
    "select a.v from a union select v from a order by 1",
    "select a.v from a union all select v from a order by 1",
    "select v from a intersect select v from a where n>15 order by 1",
    "select v from a except select v from a where n>15 order by 1",
    "select id from a order by n desc nulls last",
    "select id from a order by n asc nulls first",
    "select count(*) from a cross join b",
    "select a.id from a where a.n between 10 and 25 order by a.id",
    "select group_concat(id) from (select id from a order by id desc)",
    "select id, v, n, coalesce(n,-1) from a order by id",
    "select * from a where v like 't%' order by id",
    "select * from a where v glob '*e' order by id",
    "select id from a where n is null",
    "select max(n), min(n), count(n), count(*) from a",
    "select b.aid, count(*), avg(w) from b group by b.aid order by b.aid",
    "select a.id from a join b using(id) order by a.id",
    "select a.id from a natural join b order by a.id",
    "select id from a where id > (select avg(id) from a) order by id",
    "select v from a where n = (select max(n) from a)",
    "select id, row_number() over (order by n) from a order by id",
]


def _queries() -> list[Case]:
    cases = []
    for i, query in enumerate(_QUERIES):
        cases.append(
            _sql(f"query.{i}", f"planner.query.{i}", _SCHEMA + query + ";")
        )
        cases.append(
            _sql(f"query.explain.{i}", "planner.explain",
                 _SCHEMA + "explain query plan " + query + ";",
                 normalisers=("eqp",))
        )
    return cases


_RECURSIVE = [
    ("count-to-20",
     "with recursive c(x) as (select 1 union all select x+1 from c where x<20) "
     "select group_concat(x) from c"),
    ("fib",
     "with recursive f(a,b) as (select 0,1 union all select b,a+b from f where b<1000) "
     "select group_concat(a) from f"),
    ("tree",
     "with recursive t(id,parent) as (values(1,null),(2,1),(3,1),(4,2)), "
     "d(id,depth) as (select id,0 from t where parent is null union all "
     "select t.id,d.depth+1 from t join d on t.parent=d.id) "
     "select group_concat(id||':'||depth) from (select id,depth from d order by id)"),
    ("mandel-lite",
     "with recursive c(i,x) as (select 1,0.5 union all select i+1,x*x+0.1 from c where i<10) "
     "select group_concat(printf('%.6f',x)) from c"),
    ("string-build",
     "with recursive s(n,t) as (select 1,'a' union all select n+1,t||'b' from s where n<12) "
     "select t from s order by n desc limit 1"),
    ("cycle-guard",
     "with recursive c(x) as (select 1 union select (x+1)%5 from c) "
     "select group_concat(x) from (select x from c order by x)"),
    ("depth-limited",
     "with recursive c(x) as (select 1 union all select x+1 from c limit 5) "
     "select group_concat(x) from c"),
    ("two-anchor",
     "with recursive c(x) as (select 1 union all select 100 union all "
     "select x+1 from c where x<3) select group_concat(x) from c"),
]


def _recursive() -> list[Case]:
    return [
        _sql(f"recursive.{name}", f"planner.recursive.{name}", sql + ";")
        for name, sql in _RECURSIVE
    ]


# --------------------------------------------------------------------------
# DDL, DML, constraints, triggers, views, generated columns, UPSERT.  All of
# 3.31's authoring surface, driven to a printed result so a wrong answer is
# visible rather than merely stored.
# --------------------------------------------------------------------------
_DDL_CASES = [
    ("pk-autoincrement",
     "create table t(id integer primary key autoincrement, v);"
     "insert into t(v) values('a'),('b');delete from t where id=2;"
     "insert into t(v) values('c');select id,v from t order by id;"
     "select seq from sqlite_sequence where name='t';"),
    ("without-rowid",
     "create table t(k text primary key, v) without rowid;"
     "insert into t values('b',2),('a',1);select k,v from t;"),
    ("check-constraint",
     "create table t(a integer check(a>0));insert into t values(1);"
     "insert into t values(-1);select * from t;"),
    ("not-null",
     "create table t(a not null);insert into t values(null);"
     "insert into t values(1);select * from t;"),
    ("unique",
     "create table t(a unique);insert into t values(1);insert into t values(1);"
     "select count(*) from t;"),
    ("default-values",
     "create table t(a default 42, b default 'x', c default current_date);"
     "insert into t default values;select a,b,length(c) from t;"),
    ("foreign-key-on",
     "pragma foreign_keys=on;create table p(id integer primary key);"
     "create table c(pid references p(id));insert into c values(1);"
     "insert into p values(1);insert into c values(1);select count(*) from c;"),
    ("foreign-key-cascade",
     "pragma foreign_keys=on;create table p(id integer primary key);"
     "create table c(pid references p(id) on delete cascade);"
     "insert into p values(1);insert into c values(1);delete from p;"
     "select count(*) from c;"),
    ("generated-virtual",
     "create table t(a,b generated always as (a*2));insert into t(a) values(3);"
     "select a,b from t;"),
    ("generated-stored",
     "create table t(a,b generated always as (a||'!') stored);"
     "insert into t(a) values('x');select a,b from t;"),
    ("upsert-do-update",
     "create table t(k primary key, n);insert into t values('a',1);"
     "insert into t values('a',2) on conflict(k) do update set n=n+10;"
     "select k,n from t;"),
    ("upsert-do-nothing",
     "create table t(k primary key, n);insert into t values('a',1);"
     "insert into t values('a',2) on conflict do nothing;select k,n from t;"),
    ("insert-or-replace",
     "create table t(k primary key, n);insert into t values('a',1);"
     "insert or replace into t values('a',2);select k,n from t;"),
    ("insert-or-ignore",
     "create table t(k primary key, n);insert into t values('a',1);"
     "insert or ignore into t values('a',2);select k,n from t;"),
    ("trigger-insert",
     "create table t(a);create table log(m);"
     "create trigger tr after insert on t begin insert into log values('ins '||new.a); end;"
     "insert into t values(1),(2);select m from log order by m;"),
    ("trigger-update-old-new",
     "create table t(a);create table log(m);insert into t values(1);"
     "create trigger tr after update on t begin insert into log values(old.a||'->'||new.a); end;"
     "update t set a=2;select m from log;"),
    ("trigger-delete",
     "create table t(a);create table log(m);insert into t values(7);"
     "create trigger tr before delete on t begin insert into log values('del '||old.a); end;"
     "delete from t;select m from log;"),
    ("trigger-instead-of",
     "create table t(a);insert into t values(1);create view v as select a from t;"
     "create trigger tr instead of insert on v begin insert into t values(new.a*10); end;"
     "insert into v values(5);select a from t order by a;"),
    ("trigger-raise-abort",
     "create table t(a);create trigger tr before insert on t begin "
     "select raise(abort,'nope') where new.a<0; end;"
     "insert into t values(1);insert into t values(-1);select count(*) from t;"),
    ("view-simple",
     "create table t(a,b);insert into t values(1,2),(3,4);"
     "create view v as select a+b s from t;select s from v order by s;"),
    ("view-nested",
     "create table t(a);insert into t values(1),(2);"
     "create view v1 as select a*2 x from t;create view v2 as select sum(x) y from v1;"
     "select y from v2;"),
    ("alter-add-column",
     "create table t(a);insert into t values(1);alter table t add column b default 9;"
     "select a,b from t;"),
    ("alter-rename-table",
     "create table t(a);insert into t values(1);alter table t rename to u;"
     "select a from u;select name from sqlite_master where type='table';"),
    ("alter-rename-column",
     "create table t(a);insert into t values(1);alter table t rename column a to z;"
     "select z from t;select sql from sqlite_master where name='t';"),
    ("index-partial",
     "create table t(a,b);insert into t values(1,1),(2,null);"
     "create index i on t(a) where b is not null;"
     "select count(*) from t where a=1 and b is not null;"),
    ("index-expression",
     "create table t(a);insert into t values(-3),(2);create index i on t(abs(a));"
     "select a from t where abs(a)=3;"),
    ("index-desc-collate",
     "create table t(a);insert into t values('B'),('a');"
     "create index i on t(a collate nocase desc);select a from t order by a collate nocase desc;"),
    ("temp-table",
     "create temp table t(a);insert into t values(1);select a from t;"
     "select count(*) from temp.sqlite_master where name='t';"),
    ("conflict-rollback",
     "create table t(a unique);insert into t values(1);"
     "begin;insert into t values(2);insert or rollback into t values(1);"
     "select count(*) from t;"),
    ("returning-absent",
     "create table t(a);insert into t values(1) returning a;"),
    ("multi-row-update",
     "create table t(a,b);insert into t values(1,1),(2,2),(3,3);"
     "update t set b=b*10 where a>1;select a,b from t order by a;"),
    ("delete-limit-absent",
     "create table t(a);insert into t values(1),(2);delete from t limit 1;"
     "select count(*) from t;"),
    ("replace-into-trigger",
     "create table t(k primary key,n);create table log(m);"
     "create trigger tr after delete on t begin insert into log values('d'||old.k); end;"
     "insert into t values('a',1);replace into t values('a',2);"
     "select (select count(*) from log), n from t;"),
]


def _ddl() -> list[Case]:
    return [
        _sql(f"ddl.{name}", f"ddl.{name}", sql)
        for name, sql in _DDL_CASES
    ]


# --------------------------------------------------------------------------
# Error text.  A port that returns the right error *code* but the wrong message
# is not a drop-in replacement, and error strings are exactly what tends to
# drift when a translation unit is rebuilt for a new target.  Each of these
# prints a diagnostic on stderr and, under the default shell, keeps going.
# --------------------------------------------------------------------------
_ERROR_CASES = [
    ("syntax-near", "select from;"),
    ("syntax-unterminated-string", "select 'abc;"),
    ("no-such-table", "select * from nope;"),
    ("no-such-column", "create table t(a);select b from t;"),
    ("no-such-function", "select nosuchfunc(1);"),
    ("wrong-arg-count", "select abs(1,2);"),
    ("ambiguous-column",
     "create table t(a);create table u(a);select a from t,u;"),
    ("datatype-mismatch",
     "create table t(a integer primary key);insert into t values('x');"),
    ("integer-overflow-literal", "select 99999999999999999999999999;"),
    ("divide-by-zero-int", "select 1/0;"),
    ("group-by-misuse", "select a from (select 1 a) group by nosuch;"),
    ("order-by-out-of-range", "select 1 order by 5;"),
    ("too-many-terms-union", "select 1 union select 1,2;"),
    ("recursive-no-anchor",
     "with recursive c(x) as (select x+1 from c) select * from c;"),
    ("misuse-aggregate-in-where",
     "create table t(a);select a from t where count(*)>0;"),
    ("subquery-column-count",
     "create table t(a);select * from t where a in (select 1,2);"),
    ("no-such-collation", "select 1 collate nosuchcollation;"),
    ("no-such-pragma-fn", "select * from pragma_nosuchpragma;"),
    ("insert-column-count",
     "create table t(a,b);insert into t values(1);"),
    ("unknown-table-alter", "alter table nope rename to x;"),
    ("drop-missing-table", "drop table nope;"),
    ("drop-missing-index", "drop index nope;"),
    ("drop-missing-view", "drop view nope;"),
    ("drop-missing-trigger", "drop trigger nope;"),
    ("view-cannot-insert",
     "create table t(a);create view v as select a from t;insert into v values(1);"),
    ("readonly-sqlite-master", "insert into sqlite_master values(1,2,3,4,5);"),
    ("generated-column-write",
     "create table t(a,b generated always as (a*2));insert into t values(1,2);"),
    ("check-failed-message",
     "create table t(a check(a>0));insert into t values(0);"),
    ("not-null-message", "create table t(a not null);insert into t values(null);"),
    ("unique-message", "create table t(a unique);insert into t values(1),(1);"),
    ("fk-violation-message",
     "pragma foreign_keys=on;create table p(id integer primary key);"
     "create table c(pid references p(id));insert into c values(9);"),
    ("primary-key-message",
     "create table t(a primary key);insert into t values(1),(1);"),
    ("nested-transaction", "begin;begin;"),
    ("commit-no-transaction", "commit;"),
    ("rollback-no-transaction", "rollback;"),
    ("release-missing-savepoint", "release nosuchsavepoint;"),
    ("cast-to-unknown-type", "select cast(1 as nosuchtype), typeof(cast(1 as nosuchtype));"),
    ("window-without-over",
     "create table t(a);select row_number() from t;"),
    ("bad-window-frame",
     "create table t(a);select sum(a) over (rows between 1 following and 1 preceding) from t;"),
    ("too-many-args-printf",
     "select printf();"),
    ("parameter-unbound", "select ?1;"),
    ("attach-missing-file-strict",
     "attach 'file:/nonexistent/dir/x.db?mode=rw' as z;"),
]


def _errors() -> list[Case]:
    return [
        _sql(f"error.{name}", f"error.{name}", sql)
        for name, sql in _ERROR_CASES
    ]


# --------------------------------------------------------------------------
# Introspection pragmas that do not touch a file.  These read out the
# compiled-in configuration and the parsed schema, and they are how a caller
# discovers what the library can do -- so they are interface, not trivia.
# --------------------------------------------------------------------------
_SCHEMA_PRAGMAS = [
    "table_info", "table_xinfo", "index_list", "foreign_key_list",
    "collation_list", "function_list", "module_list", "pragma_list",
]

# ``cache_spill`` is deliberately absent from this list.  Its default is a page
# count derived from the 2MB default cache divided by the per-page overhead, and
# that overhead contains pointers -- so a 32-bit target legitimately reports a
# different number (measured: 483 native, 489 wasm32).  Reading it after pinning
# cache_size to a page count removes the pointer width from the answer without
# removing the pragma from the suite; see _pragmas() below.
_VALUE_PRAGMAS = [
    "auto_vacuum", "automatic_index", "busy_timeout", "cache_size",
    "cell_size_check", "checkpoint_fullfsync", "defer_foreign_keys",
    "encoding", "foreign_keys", "fullfsync", "ignore_check_constraints",
    "journal_mode", "journal_size_limit", "legacy_alter_table", "locking_mode",
    "max_page_count", "page_size", "query_only", "read_uncommitted",
    "recursive_triggers", "reverse_unordered_selects", "secure_delete",
    "synchronous", "temp_store", "threads", "trusted_schema", "wal_autocheckpoint",
    "writable_schema", "data_version", "freelist_count", "page_count",
]

_PRAGMA_SETS = [
    ("cache_size", "-3000"), ("cache_size", "500"),
    ("synchronous", "0"), ("synchronous", "full"),
    ("temp_store", "1"), ("temp_store", "memory"),
    ("locking_mode", "exclusive"), ("locking_mode", "normal"),
    ("secure_delete", "1"), ("secure_delete", "fast"),
    ("max_page_count", "1000"),
    ("journal_size_limit", "4096"),
    ("busy_timeout", "1234"),
    ("recursive_triggers", "1"),
    ("foreign_keys", "1"),
    ("query_only", "1"),
    ("automatic_index", "0"),
    ("reverse_unordered_selects", "1"),
    ("threads", "0"),
    ("wal_autocheckpoint", "100"),
]


def _pragmas() -> list[Case]:
    cases: list[Case] = []
    for name in _SCHEMA_PRAGMAS:
        arg = "(t)" if name in ("table_info", "table_xinfo", "index_list",
                                "foreign_key_list") else ""
        # These are hash-table walks, so an explicit order is required before
        # the output is a function of the input at all.  function_list also
        # excludes the two functions that need a process the platform does not
        # have: `load_extension` (no dlopen) and `edit`, which the shell registers
        # only when it can spawn an editor (shell.c:11455, inside the
        # SQLITE_NOHAVE_SYSTEM guard).  Both absences are asserted positively in
        # cases_platform rather than tolerated here, and every other row of the
        # table -- 134 of them, arity and flags included -- still has to match the
        # reference exactly.
        order = " order by 1,2,3,4,5,6" if name in (
            "collation_list", "function_list", "module_list", "pragma_list",
        ) else ""
        where = (" where name not in ('load_extension','edit')"
                 if name == "function_list" else "")
        cases.append(_sql(
            f"pragma.schema.{name}",
            f"pragma.schema.{name}",
            "create table t(a integer primary key, b text not null default 'x',"
            " c real, d blob, e generated always as (a+1));"
            "create index ti on t(b);"
            "create table u(x references t(a));"
            f"select * from pragma_{name}{arg}{where}{order};",
        ))
    # cache_spill read from a pinned cache size: the pragma stays covered, the
    # pointer width drops out of the answer.
    cases.append(_sql(
        "pragma.read.cache_spill.pinned", "pragma.read.cache_spill",
        "pragma cache_size=500;pragma cache_spill;pragma cache_spill=0;"
        "pragma cache_spill;",
    ))
    for name in _VALUE_PRAGMAS:
        cases.append(_sql(
            f"pragma.read.{name}", f"pragma.read.{name}",
            f"pragma {name};",
        ))
    for i, (name, value) in enumerate(_PRAGMA_SETS):
        cases.append(_sql(
            f"pragma.set.{i}.{name}", f"pragma.set.{name}",
            f"pragma {name}={value};pragma {name};",
        ))
    # compile_options is the port's declaration of what it is.  Three entries
    # cannot match native and each is handled by name rather than by a blanket
    # normaliser: COMPILER= is underdetermined (any C compiler is allowed, so it
    # is dropped), while THREADSAFE=0 and OMIT_LOAD_EXTENSION are *required* --
    # asserted as literals in the platform family, because a port that reports
    # THREADSAFE=1 without pthreads is lying about its own build.  Everything
    # else must agree exactly, which is what this case pins.
    cases.append(_sql(
        "pragma.compile_options", "pragma.compile_options",
        "select * from pragma_compile_options where compile_options not like"
        " 'COMPILER=%' and compile_options not in"
        " ('THREADSAFE=0','THREADSAFE=1','OMIT_LOAD_EXTENSION')"
        " order by 1;",
    ))
    cases.append(_sql(
        "pragma.compile_options.compiler-present", "pragma.compile_options",
        "select count(*) from pragma_compile_options where compile_options"
        " like 'COMPILER=%';",
    ))
    cases.append(_sql(
        "pragma.audit_check.memory", "pragma.audit_check",
        "create table t(a);insert into t values(1);pragma audit_check;",
    ))
    cases.append(_sql(
        "pragma.quick_check.memory", "pragma.quick_check",
        "create table t(a);insert into t values(1);pragma quick_check;",
    ))
    cases.append(_sql(
        "pragma.optimize", "pragma.optimize",
        "create table t(a);insert into t values(1);analyze;pragma optimize;"
        "select count(*) from sqlite_stat1;",
    ))
    return cases


# --------------------------------------------------------------------------
# Everything else with a documented answer: version and identity functions,
# json1, blob handling, quoting, likelihood hints, sqlite_source_id.
# --------------------------------------------------------------------------
_MISC = [
    ("version", "select sqlite_version();"),
    # The *length* is recorded, because 84 is what both forms of a source id
    # measure -- see `_source_id_value` for why there are two forms -- and a port
    # that changed the shape of the string would be caught here without this
    # family needing to know which form it got.
    ("source-id-length", "select length(sqlite_source_id());"),
    # THREADSAFE and OMIT_LOAD_EXTENSION are asserted with literal expectations
    # in cases_platform, not recorded from native.  What is portable is that the
    # option is *reported* at all, in the documented shape.
    ("compile-threadsafe-shape",
     "select count(*), max(compile_options like 'THREADSAFE=_')"
     " from pragma_compile_options where compile_options like 'THREADSAFE%';"),
    ("changes-total", "create table t(a);insert into t values(1),(2);"
                      "select changes(), total_changes();"),
    ("last-insert-rowid", "create table t(a);insert into t values(1);"
                          "select last_insert_rowid();"),
    ("rowid-alias", "create table t(a);insert into t values(9);"
                    "select rowid, oid, _rowid_ from t;"),
    ("quote-forms", "select quote(1), quote(1.5), quote('a''b'), quote(null),"
                    " quote(x'0102');"),
    ("blob-roundtrip", "select hex(x'00ff10'), length(x'00ff10'),"
                       " typeof(x'00ff10'), quote(x'');"),
    ("blob-embedded-nul", "select length(cast(x'610062' as text)),"
                          " hex(cast(x'610062' as text));"),
    ("zeroblob", "select length(zeroblob(10)), hex(zeroblob(4));"),
    ("likelihood-hints", "select likelihood(1,0.5), likely(1), unlikely(0);"),
    ("iif-nullif", "select iif(1,'y','n'), iif(0,'y','n'), nullif(1,1), nullif(1,2);"),
    ("json-valid", "select json_valid('{\"a\":1}'), json_valid('{');"),
    ("json-extract", "select json_extract('{\"a\":[1,2,{\"b\":3}]}','$.a[2].b'),"
                     " json_extract('[1,2,3]','$[1]');"),
    ("json-type", "select json_type('{\"a\":1}'), json_type('[1]','$[0]'),"
                  " json_type('null');"),
    ("json-array-object", "select json_array(1,'a',null), json_object('k',1,'j','x');"),
    ("json-set-insert-replace",
     "select json_set('{\"a\":1}','$.b',2), json_insert('{\"a\":1}','$.a',9),"
     " json_replace('{\"a\":1}','$.a',9), json_remove('{\"a\":1,\"b\":2}','$.a');"),
    ("json-quote-patch", "select json_quote(3.5), json_patch('{\"a\":1}','{\"b\":2}');"),
    ("json-group", "create table t(a);insert into t values(1),(2);"
                   "select json_group_array(a), json_group_object('k'||a,a) from t;"),
    ("json-array-length", "select json_array_length('[1,2,3]'),"
                          " json_array_length('{\"a\":[1,2]}','$.a');"),
    ("json-error", "select json_extract('{bad}','$.a');"),
    ("unicode-char", "select unicode('A'), unicode('é'), char(65,233,0x4e2d);"),
    ("utf8-length", "select length('é中'), length(cast('é中' as blob));"),
    ("upper-lower-ascii-only", "select upper('é'), lower('É'), upper('abcé');"),
    ("like-escape", "select 'a_b' like 'a\\_b' escape '\\', 'axb' like 'a\\_b' escape '\\';"),
    ("glob-classes", "select 'abc' glob '[a-c]*', 'Abc' glob '[a-c]*', '?' glob '[?]';"),
    ("instr-substr-negative",
     "select instr('hello','l'), substr('hello',-3), substr('hello',2,2),"
     " substr('hello',-3,2);"),
    ("replace-empty", "select replace('aaa','a',''), replace('abc','','x');"),
    ("trim-charset", "select trim('xxhixx','x'), ltrim('  hi'), rtrim('hi..','.');"),
    ("round-half", "select round(0.5), round(1.5), round(2.5), round(-0.5),"
                   " round(2.675,2);"),
    ("real-to-text-edges",
     "select 1e300*1e300, -1e300*1e300, 0.0/0.0 is null, 1.0/3.0, 1e-300;"),
    ("min-max-scalar-vs-agg",
     "create table t(a);insert into t values(3),(1);"
     "select min(2,1), max(2,1), (select min(a) from t), (select max(a) from t);"),
    ("typeof-all", "select typeof(1), typeof(1.0), typeof('a'), typeof(x'01'),"
                   " typeof(null);"),
    ("total-order-sort",
     "create table t(a);insert into t values(1),('a'),(x'01'),(null),(2.5);"
     "select typeof(a) from t order by a;"),
    ("limit-offset-negative",
     "create table t(a);insert into t values(1),(2),(3);"
     "select a from t limit -1 offset 1;"),
    ("values-clause", "values(1,'a'),(2,'b');"),
    ("with-clause-scalar", "with x(v) as (values(1),(2)) select sum(v) from x;"),
    ("sqlite-master-shape", "create table t(a);create index i on t(a);"
                            "select type,name,tbl_name,sql from sqlite_master order by name;"),
    ("pragma-function-table-form",
     "create table t(a);select name,type from pragma_table_info('t');"),
    ("nested-json-each-scalar",
     "select key,value,type,atom,fullkey,path from json_each('[7,\"x\"]');"),
    ("nested-json-tree-scalar",
     "select fullkey,type from json_tree('{\"a\":{\"b\":[1]}}') order by fullkey;"),
]

# quote() on a real promises exactly one thing: the text it returns parses back
# to the identical double (sqlite3.c:116678-116682 tries 15 digits and falls back
# to 20 only when it does not round-trip).  That promise is host-independent, so
# it is asserted directly -- these cases are what allows the `longdouble`
# normaliser to round off the digits that are not.
_ROUNDTRIP_REALS = [
    "0.1", "0.2", "0.3", "0.1+0.2", "1.0/3.0", "2.0/3.0", "1.0/7.0",
    "0.1+0.2+0.3", "1e-300", "1e300", "3.141592653589793",
    "2.718281828459045", "1.7976931348623157e308", "5e-324", "4.9e-324",
    "9007199254740993.0", "0.5", "1.5", "2.5", "-0.1", "-1.0/3.0",
    "123456789.123456789", "1e16+1", "1e17+1", "0.1*3", "1.1*1.1",
    "9223372036854775807+0.0", "(-9223372036854775807)-1.0",
    "1e-5", "1e-4", "1234567890123456.7", "0.007", "1/3.0*3",
]


def _roundtrip() -> list[Case]:
    cases: list[Case] = []
    for i, expr in enumerate(_ROUNDTRIP_REALS):
        cases.append(_sql(
            f"real.roundtrip.{i}", "func.quote.roundtrip",
            f"select cast(quote({expr}) as real) = ({expr}),"
            f" typeof({expr}),"
            f" cast(quote({expr}) as real) - ({expr}) = 0.0;",
        ))
        # The same property through the text path the CLI itself prints with.
        cases.append(_sql(
            f"real.printf17.{i}", "func.printf.roundtrip",
            f"select cast(printf('%.17g',{expr}) as real) = ({expr});",
        ))
    return cases


def _misc() -> list[Case]:
    cases = [_sql(f"misc.{name}", f"misc.{name}", sql) for name, sql in _MISC]
    # The same value through the SQL surface rather than the argument vector; see
    # cases_shell._flags on why this is measured against the reference and not
    # written down.  Both targets report the `alt1` inventory suffix, the reference
    # because the State A payload is missing three files `manifest` names and the
    # port because it removed the platform layer.
    cases.append(_sql(
        "misc.source-id-value", "misc.source-id-value",
        "select sqlite_source_id();",
    ))
    return cases


def build() -> list[Case]:
    """All engine-semantics cases, in a stable order."""
    cases: list[Case] = []
    for builder in (
        _arithmetic, _reals, _printf, _strings, _comparisons, _casts, _dates,
        _collation, _aggregates, _windows, _queries, _recursive, _ddl,
        _errors, _pragmas, _misc, _roundtrip,
    ):
        cases.extend(builder())
    return cases
