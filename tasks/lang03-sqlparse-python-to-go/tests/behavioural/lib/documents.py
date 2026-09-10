#!/usr/bin/env python3
"""The SQL document set every behavioural case is measured over.

Three sources, in descending order of how much thought went into each document:

  curated   -- written here, one construct or one calibrated quirk per document.
               This is the part that discriminates. Each entry was checked
               against the reference to confirm it produces the behavior it is
               named for, so a submission cannot pass the family by accident.
  samples  -- the .sql files State A ships under tests/files/. Real-world SQL,
               including the two that are not UTF-8, which is what makes the
               encoding path gradeable at all.
  generated -- generated from a seeded grammar. Not clever, but wide: it covers
               combinations nobody would think to write down, and it is what
               keeps a submission from special-casing the curated list.

The document set is frozen into the verifier image, so the agent never sees which
documents were chosen -- but note that the sample inputs are visible in State A.
That is fine and deliberate: the inputs were never the secret. The expected
outputs are, and those are computed from the reference after the document set is fixed.

Documents are stored as raw bytes, not text. Two samples are cp1251 and gbk,
and a document set that could only hold str would have to drop them.
"""

from __future__ import annotations

import hashlib
import json
import random
import shutil
from pathlib import Path

# --------------------------------------------------------------------------
# 1. Curated documents.  (id, sql) with ids that say what is being probed.
# --------------------------------------------------------------------------

BASIC = [
    ("sel-star", "select * from t"),
    ("sel-cols", "select a, b, c from t"),
    ("sel-distinct", "select distinct a from t"),
    ("sel-all", "select all a from t"),
    ("sel-top", "select top 10 a from t"),
    ("sel-alias-as", "select a as x from t"),
    ("sel-alias-bare", "select a x from t"),
    ("sel-qualified", "select t.a from t"),
    ("sel-schema-qualified", "select s.t.a from s.t"),
    ("sel-star-qualified", "select t.* from t"),
    ("sel-expr", "select a + b * c from t"),
    ("sel-paren-expr", "select (a + b) * c from t"),
    ("sel-literal-only", "select 1"),
    ("sel-string-only", "select 'x'"),
    ("sel-no-from", "select 1 + 1"),
    ("sel-into", "select a into b from t"),
    ("sel-limit", "select a from t limit 10"),
    ("sel-limit-offset", "select a from t limit 10 offset 20"),
    ("sel-fetch", "select a from t fetch first 10 rows only"),
    ("sel-for-update", "select a from t for update"),
    ("sel-multiline", "select a,\n  b\nfrom t"),
    ("sel-tabs", "select\ta\tfrom\tt"),
    ("sel-trailing-ws", "select a from t   "),
    ("sel-leading-ws", "   select a from t"),
    ("sel-crlf", "select a\r\nfrom t"),
    ("sel-cr-only", "select a\rfrom t"),
    ("sel-upper", "SELECT A FROM T"),
    ("sel-mixed", "SeLeCt A fRoM t"),
]

WHERE = [
    ("wh-eq", "select a from t where x = 1"),
    ("wh-ne-bang", "select a from t where x != 1"),
    ("wh-ne-angle", "select a from t where x <> 1"),
    ("wh-lt", "select a from t where x < 1"),
    ("wh-le", "select a from t where x <= 1"),
    ("wh-gt", "select a from t where x > 1"),
    ("wh-ge", "select a from t where x >= 1"),
    ("wh-spaceship", "select a from t where x <=> 1"),
    ("wh-nullsafe-pg", "select a from t where x is distinct from 1"),
    ("wh-and", "select a from t where x = 1 and y = 2"),
    ("wh-or", "select a from t where x = 1 or y = 2"),
    ("wh-and-or", "select a from t where x = 1 and y = 2 or z = 3"),
    ("wh-not", "select a from t where not x"),
    ("wh-paren", "select a from t where (x = 1 or y = 2) and z = 3"),
    ("wh-in-list", "select a from t where x in (1, 2, 3)"),
    ("wh-in-sub", "select a from t where x in (select y from u)"),
    ("wh-not-in", "select a from t where x not in (1, 2)"),
    ("wh-between", "select a from t where x between 1 and 10"),
    ("wh-not-between", "select a from t where x not between 1 and 10"),
    ("wh-like", "select a from t where x like '%a%'"),
    ("wh-not-like", "select a from t where x not like '%a%'"),
    ("wh-ilike", "select a from t where x ilike '%a%'"),
    ("wh-is-null", "select a from t where x is null"),
    ("wh-is-not-null", "select a from t where x is not null"),
    ("wh-exists", "select a from t where exists (select 1 from u)"),
    ("wh-not-exists", "select a from t where not exists (select 1 from u)"),
    ("wh-any", "select a from t where x = any (select y from u)"),
    ("wh-all", "select a from t where x > all (select y from u)"),
    ("wh-regexp", "select a from t where x regexp '^a'"),
    ("wh-similar", "select a from t where x similar to 'a%'"),
    ("wh-concat", "select a from t where x || y = 'ab'"),
    ("wh-mod", "select a from t where x % 2 = 0"),
    ("wh-nested-deep", "select a from t where ((((x = 1))))"),
]

JOINS = [
    ("jn-comma", "select a from t, u"),
    ("jn-inner", "select a from t inner join u on t.id = u.id"),
    ("jn-plain", "select a from t join u on t.id = u.id"),
    ("jn-left", "select a from t left join u on t.id = u.id"),
    ("jn-left-outer", "select a from t left outer join u on t.id = u.id"),
    ("jn-right", "select a from t right join u on t.id = u.id"),
    ("jn-right-outer", "select a from t right outer join u on t.id = u.id"),
    ("jn-full", "select a from t full join u on t.id = u.id"),
    ("jn-full-outer", "select a from t full outer join u on t.id = u.id"),
    ("jn-cross", "select a from t cross join u"),
    ("jn-natural", "select a from t natural join u"),
    ("jn-using", "select a from t join u using (id)"),
    ("jn-straight", "select a from t straight_join u on t.id = u.id"),
    ("jn-three", "select a from t join u on t.id = u.id join v on u.id = v.id"),
    ("jn-alias", "select a from t x join u y on x.id = y.id"),
    ("jn-alias-as", "select a from t as x join u as y on x.id = y.id"),
    ("jn-subquery", "select a from (select b from u) x"),
    ("jn-lateral", "select a from t, lateral (select b from u) x"),
    ("jn-on-and", "select a from t join u on t.id = u.id and t.k = u.k"),
]

GROUPING = [
    ("gb-one", "select a, count(*) from t group by a"),
    ("gb-two", "select a, b, count(*) from t group by a, b"),
    ("gb-ordinal", "select a, count(*) from t group by 1"),
    ("gb-having", "select a from t group by a having count(*) > 1"),
    ("gb-having-and", "select a from t group by a having count(*) > 1 and sum(b) < 5"),
    ("gb-rollup", "select a from t group by rollup(a, b)"),
    ("gb-cube", "select a from t group by cube(a, b)"),
    ("gb-grouping-sets", "select a from t group by grouping sets ((a), (b))"),
    ("ob-one", "select a from t order by a"),
    ("ob-asc", "select a from t order by a asc"),
    ("ob-desc", "select a from t order by a desc"),
    ("ob-two", "select a from t order by a asc, b desc"),
    ("ob-ordinal", "select a from t order by 1"),
    ("ob-nulls-first", "select a from t order by a nulls first"),
    ("ob-nulls-last", "select a from t order by a desc nulls last"),
    ("ob-expr", "select a from t order by upper(a)"),
    ("ob-spaced-kw", "select a from t Order   By a"),
]

SUBQUERY_CTE = [
    ("sq-scalar", "select (select max(b) from u) from t"),
    ("sq-from", "select a from (select a from t) x"),
    ("sq-correlated", "select a from t where exists (select 1 from u where u.id = t.id)"),
    ("sq-nested-two", "select a from (select a from (select a from t) x) y"),
    ("cte-one", "with x as (select 1) select * from x"),
    ("cte-two", "with x as (select 1), y as (select 2) select * from x, y"),
    ("cte-recursive", "with recursive x as (select 1 union all select 2) select * from x"),
    ("cte-cols", "with x (a, b) as (select 1, 2) select * from x"),
    ("cte-insert", "with x as (select 1) insert into t select * from x"),
    ("cte-materialized", "with x as materialized (select 1) select * from x"),
    ("set-union", "select a from t union select a from u"),
    ("set-union-all", "select a from t union all select a from u"),
    ("set-intersect", "select a from t intersect select a from u"),
    ("set-except", "select a from t except select a from u"),
    ("set-minus", "select a from t minus select a from u"),
    ("set-three", "select 1 union select 2 union select 3"),
]

DML = [
    ("ins-values", "insert into t values (1, 2)"),
    ("ins-cols", "insert into t (a, b) values (1, 2)"),
    ("ins-multi-values", "insert into t values (1, 2), (3, 4)"),
    ("ins-select", "insert into t select a, b from u"),
    ("ins-default", "insert into t default values"),
    ("ins-returning", "insert into t values (1) returning id"),
    ("ins-conflict", "insert into t values (1) on conflict do nothing"),
    ("ins-duplicate", "insert into t values (1) on duplicate key update a = 2"),
    ("upd-one", "update t set a = 1"),
    ("upd-where", "update t set a = 1 where b = 2"),
    ("upd-two", "update t set a = 1, b = 2 where c = 3"),
    ("upd-expr", "update t set a = a + 1"),
    ("upd-from", "update t set a = u.b from u where t.id = u.id"),
    ("upd-subquery", "update t set a = (select max(b) from u)"),
    ("del-all", "delete from t"),
    ("del-where", "delete from t where a = 1"),
    ("del-using", "delete from t using u where t.id = u.id"),
    ("del-no-from", "delete t where a = 1"),
    ("merge", "merge into t using u on t.id = u.id when matched then update set a = 1"),
    ("truncate", "truncate table t"),
    ("replace", "replace into t values (1)"),
    ("upsert-pg", "insert into t (a) values (1) on conflict (a) do update set b = 2"),
]

DDL = [
    ("cr-table", "create table t (a int, b varchar(10))"),
    ("cr-table-pk", "create table t (a int primary key, b text not null)"),
    ("cr-table-fk", "create table t (a int references u(id))"),
    ("cr-table-default", "create table t (a int default 0)"),
    ("cr-table-check", "create table t (a int check (a > 0))"),
    ("cr-table-unique", "create table t (a int unique)"),
    ("cr-table-as", "create table t as select a from u"),
    ("cr-table-ine", "create table if not exists t (a int)"),
    ("cr-temp-table", "create temporary table t (a int)"),
    ("cr-view", "create view v as select a from t"),
    ("cr-or-replace-view", "create or replace view v as select a from t"),
    ("cr-index", "create index i on t (a)"),
    ("cr-unique-index", "create unique index i on t (a, b)"),
    ("cr-schema", "create schema s"),
    ("cr-sequence", "create sequence s start with 1 increment by 1"),
    ("cr-trigger", "create trigger tr before insert on t for each row begin end"),
    ("cr-type", "create type ty as enum ('a', 'b')"),
    ("cr-database", "create database d"),
    ("al-add-col", "alter table t add column c int"),
    ("al-drop-col", "alter table t drop column c"),
    ("al-rename", "alter table t rename to u"),
    ("al-alter-col", "alter table t alter column a type bigint"),
    ("al-add-constraint", "alter table t add constraint c primary key (a)"),
    ("dr-table", "drop table t"),
    ("dr-table-ie", "drop table if exists t"),
    ("dr-table-cascade", "drop table t cascade"),
    ("dr-view", "drop view v"),
    ("dr-index", "drop index i"),
    ("grant", "grant select on t to u"),
    ("revoke", "revoke select on t from u"),
    ("comment-on", "comment on table t is 'a table'"),
    ("cr-func-sql", "create function f() returns int as 'select 1' language sql"),
]

FUNCTIONS = [
    ("fn-count-star", "select count(*) from t"),
    ("fn-count-col", "select count(a) from t"),
    ("fn-count-distinct", "select count(distinct a) from t"),
    ("fn-sum", "select sum(a) from t"),
    ("fn-avg-round", "select round(avg(a), 2) from t"),
    ("fn-min-max", "select min(a), max(a) from t"),
    ("fn-coalesce", "select coalesce(a, b, 0) from t"),
    ("fn-nullif", "select nullif(a, 0) from t"),
    ("fn-substring", "select substring(a from 1 for 2) from t"),
    ("fn-substr", "select substr(a, 1, 2) from t"),
    ("fn-trim", "select trim(both ' ' from a) from t"),
    ("fn-extract", "select extract(year from d) from t"),
    ("fn-cast", "select cast(a as int) from t"),
    ("fn-cast-nested", "select cast(cast(a as text) as int) from t"),
    ("fn-pg-cast", "select a::int from t"),
    ("fn-pg-cast-chain", "select a::text::int from t"),
    ("fn-pg-cast-paren", "select (a + b)::int from t"),
    ("fn-nested", "select f(g(h(a))) from t"),
    ("fn-noargs", "select now()"),
    ("fn-multiarg", "select f(a, b, c) from t"),
    ("fn-named-arg", "select f(a => 1) from t"),
    ("fn-qualified", "select s.f(a) from t"),
    ("fn-window-over", "select row_number() over () from t"),
    ("fn-window-partition", "select row_number() over (partition by a) from t"),
    ("fn-window-order", "select rank() over (order by a desc) from t"),
    ("fn-window-both", "select sum(a) over (partition by b order by c) from t"),
    ("fn-window-frame", "select sum(a) over (rows between unbounded preceding and current row) from t"),
    ("fn-window-named", "select sum(a) over w from t window w as (partition by b)"),
    ("fn-filter", "select count(*) filter (where a > 0) from t"),
    ("fn-within-group", "select percentile_cont(0.5) within group (order by a) from t"),
    ("fn-agg-order", "select string_agg(a, ',' order by a) from t"),
    ("fn-array", "select array[1, 2, 3]"),
    ("fn-array-index", "select a[1] from t"),
    ("fn-array-slice", "select a[1:2] from t"),
    ("fn-json-arrow", "select a -> 'k' from t"),
    ("fn-json-arrow2", "select a ->> 'k' from t"),
    ("fn-interval", "select interval '1 day'"),
    ("fn-date-literal", "select date '2020-01-01'"),
    ("fn-timestamp-literal", "select timestamp '2020-01-01 00:00:00'"),
    ("fn-time-literal", "select time '12:00:00'"),
]

CASE_EXPR = [
    ("case-simple", "select case when a then 1 else 2 end from t"),
    ("case-no-else", "select case when a then 1 end from t"),
    ("case-two-when", "select case when a then 1 when b then 2 else 3 end from t"),
    ("case-value", "select case a when 1 then 'x' when 2 then 'y' end from t"),
    ("case-nested", "select case when a then case when b then 1 end end from t"),
    ("case-in-where", "select a from t where case when a then 1 else 2 end = 1"),
    ("case-alias", "select case when a then 1 end as x from t"),
    ("case-upper", "SELECT CASE WHEN A THEN 1 ELSE 2 END FROM T"),
    ("case-multiline", "select case\n  when a then 1\n  else 2\nend from t"),
]

COMMENTS = [
    ("cm-dash", "select a from t -- a comment"),
    ("cm-dash-only", "-- just a comment"),
    ("cm-dash-newline", "select a -- comment\nfrom t"),
    ("cm-hash", "select a from t # a comment"),
    ("cm-block", "select a /* comment */ from t"),
    ("cm-block-multiline", "select a /* line1\nline2 */ from t"),
    ("cm-block-only", "/* just a comment */"),
    ("cm-block-nested-looking", "select a /* /* not nested */ from t"),
    ("cm-block-unterminated", "select a /* unterminated"),
    ("cm-leading-block", "/* c */ select a from t"),
    ("cm-between-cols", "select a, /* c */ b from t"),
    ("cm-in-where", "select a from t where /* c */ x = 1"),
    ("cm-dash-in-string", "select '-- not a comment' from t"),
    ("cm-block-in-string", "select '/* not a comment */' from t"),
    ("cm-two-dashes-no-space", "select a from t --comment"),
    ("cm-dash-single", "select a -b from t"),
]

STRINGS = [
    ("st-single", "select 'abc' from t"),
    ("st-empty", "select '' from t"),
    ("st-escaped-quote", "select 'it''s' from t"),
    ("st-backslash", "select 'a\\'b' from t"),
    ("st-double", 'select "abc" from t'),
    ("st-double-ident", 'select "col name" from t'),
    ("st-backtick", "select `abc` from t"),
    ("st-bracket-ident", "select [abc] from t"),
    ("st-newline-in", "select 'a\nb' from t"),
    ("st-unterminated-single", "select 'abc from t"),
    ("st-unterminated-double", 'select "abc from t'),
    ("st-unterminated-backtick", "select `abc from t"),
    ("st-lone-quote", "select a from t '"),
    ("st-dollar-quoted", "select $$abc$$ from t"),
    ("st-dollar-tagged", "select $tag$abc$tag$ from t"),
    ("st-dollar-nested", "select $a$ $b$ $a$ from t"),
    ("st-dollar-after-word", "select x$abc$ from t"),
    ("st-dollar-unmatched-tag", "select $a$x$b$ from t"),
    ("st-unicode-prefix", "select u'abc' from t"),
    ("st-national-prefix", "select n'abc' from t"),
    ("st-binary-prefix", "select b'0101' from t"),
    ("st-hex-prefix", "select x'ff' from t"),
    ("st-escape-prefix", "select e'a\\nb' from t"),
    ("st-raw-prefix", "select r'abc' from t"),
    ("st-concat-pipes", "select 'a' || 'b' from t"),
    ("st-concat-adjacent", "select 'a' 'b' from t"),
    ("st-quote-in-ident", 'select "a""b" from t'),
    ("st-long", "select '" + "x" * 500 + "' from t"),
]

NUMBERS = [
    ("nu-int", "select 1 from t"),
    ("nu-neg-int", "select -1 from t"),
    ("nu-plus-int", "select +1 from t"),
    ("nu-sub-two", "select 1-2 from t"),
    ("nu-sub-spaced", "select 1 - 2 from t"),
    ("nu-name-minus-int", "select a-1 from t"),
    ("nu-name-minus-name", "select a-b from t"),
    ("nu-float", "select 1.5 from t"),
    ("nu-float-trailing-dot", "select 1. from t"),
    ("nu-float-leading-dot", "select .5 from t"),
    ("nu-float-double-dot", "select 1.5.6 from t"),
    ("nu-exp", "select 1e3 from t"),
    ("nu-exp-neg", "select 1.5e-3 from t"),
    ("nu-exp-plus", "select 1.5E+3 from t"),
    ("nu-hex", "select 0xFF from t"),
    ("nu-hex-lower", "select 0xff from t"),
    ("nu-hex-neg", "select -0xff from t"),
    ("nu-underscore-name", "select _1 from t"),
    ("nu-int-alias", "select 1 x from t"),
    ("nu-zero", "select 0 from t"),
    ("nu-big", "select 12345678901234567890 from t"),
    ("nu-in-ident", "select a1 from t1"),
    ("nu-leading-digit-ident", "select 1a from t"),
]

IDENTIFIERS = [
    ("id-plain", "select abc from t"),
    ("id-underscore", "select _abc from t"),
    ("id-trailing-underscore", "select abc_ from t"),
    ("id-dollar", "select a$b from t"),
    ("id-hash", "select #temp from t"),
    ("id-at", "select @var from t"),
    ("id-at-at", "select @@version"),
    ("id-colon-param", "select a from t where x = :param"),
    ("id-percent-param", "select a from t where x = %s"),
    ("id-named-percent", "select a from t where x = %(name)s"),
    ("id-question-param", "select a from t where x = ?"),
    ("id-dollar-param", "select a from t where x = $1"),
    ("id-unicode-latin", "select été from t"),
    ("id-unicode-umlaut", "select über from t"),
    ("id-unicode-cjk", "select 中文 from t"),
    ("id-unicode-cyrillic", "select имя from t"),
    ("id-unicode-greek", "select αβ from t"),
    ("id-multiply-sign", "select × from t"),
    ("id-sharp-s", "select ß from t"),
    ("id-division-sign", "select ÷ from t"),
    ("id-thorn", "select þ from t"),
    ("id-y-diaeresis", "select ÿ from t"),
    ("id-keyword-as-name", "select select from t"),
    ("id-quoted-keyword", 'select "select" from t'),
    ("id-dotted-three", "select a.b.c from t"),
    ("id-dotted-four", "select a.b.c.d from t"),
    ("id-dot-star", "select a.* from t"),
    ("id-dot-no-space", "select a .b from t"),
    ("id-space-dot", "select a. b from t"),
    ("id-only-dot", "select . from t"),
    # An empty quoted identifier is the only input that separates "a name that
    # is the empty string" from "no name at all".  get_real_name, get_name,
    # get_parent_name and get_alias all return None when there is nothing to
    # report and a genuine "" for these, so a port that collapses absence to ""
    # -- the obvious way to give a Go method a plain string return -- answers
    # identically on every other document in this document set and differs only here.
    # Without these four the contract's presence flag would be unenforceable.
    ("id-empty-quoted", 'select "" from t'),
    ("id-empty-quoted-parent", 'select "".c from t'),
    ("id-empty-quoted-alias", 'select a as "" from t'),
    ("id-empty-backtick", "select `` from t"),
]

OPERATORS = [
    ("op-plus", "select a + b from t"),
    ("op-minus", "select a - b from t"),
    ("op-star", "select a * b from t"),
    ("op-slash", "select a / b from t"),
    ("op-percent", "select a % b from t"),
    ("op-caret", "select a ^ b from t"),
    ("op-double-pipe", "select a || b from t"),
    ("op-ampersand", "select a & b from t"),
    ("op-pipe", "select a | b from t"),
    ("op-tilde", "select ~a from t"),
    ("op-shift-left", "select a << 1 from t"),
    ("op-shift-right", "select a >> 1 from t"),
    ("op-at-at", "select a @@ b from t"),
    ("op-hash-hash", "select a ## b from t"),
    ("op-no-space", "select a+b from t"),
    ("op-many-no-space", "select a+b*c-d/e from t"),
    ("op-assign-colon-eq", "update t set a := 1"),
    ("op-not-eq-not", "select a from t where not a = 1"),
    ("op-unary-neg-paren", "select -(a) from t"),
    ("op-double-neg", "select --a from t"),
]

MULTI = [
    ("ms-two", "select 1; select 2"),
    ("ms-two-trailing", "select 1; select 2;"),
    ("ms-empty-between", "select 1;;select 2"),
    ("ms-only-semi", ";"),
    ("ms-semi-ws", "  ;  "),
    ("ms-three", "select 1; select 2; select 3; select 4"),
    ("ms-newline-sep", "select 1;\nselect 2"),
    ("ms-mixed-types", "insert into t values (1); select * from t; delete from t"),
    ("ms-semi-in-string", "select 'a;b'; select 2"),
    ("ms-semi-in-comment", "select 1 -- ;\n; select 2"),
    ("ms-empty", ""),
    ("ms-ws-only", "   "),
    ("ms-newline-only", "\n"),
    ("ms-comment-only-semi", "-- c\n;"),
]

PLSQL = [
    ("pl-begin-end", "begin select 1; end;"),
    ("pl-begin-nested", "begin begin select 1; end; end;"),
    ("pl-declare", "declare x int; begin select x; end;"),
    ("pl-if", "if a then select 1; end if;"),
    ("pl-if-else", "if a then select 1; else select 2; end if;"),
    ("pl-if-elsif", "if a then select 1; elsif b then select 2; end if;"),
    ("pl-for", "for i in 1..10 loop select i; end loop;"),
    ("pl-while", "while a loop select 1; end loop;"),
    ("pl-loop", "loop select 1; end loop;"),
    ("pl-case-stmt", "case a when 1 then select 1; end case;"),
    ("pl-exception", "begin select 1; exception when others then null; end;"),
    ("pl-func-dollar", "create function f() returns int as $$ begin return 1; end; $$ language plpgsql"),
    ("pl-func-dollar-semi", "create or replace function f() as $$ select 1; select 2; $$ language sql;"),
    ("pl-do-block", "do $$ begin perform 1; end $$"),
    ("pl-cursor", "declare c cursor for select 1"),
    ("pl-fetch", "fetch next from c"),
    ("pl-raise", "raise notice 'x'"),
    ("pl-perform", "perform f()"),
    ("pl-return-query", "return query select 1"),
    ("pl-assign", "x := 1"),
]

WHITESPACE = [
    ("ws-double-space", "select  a  from  t"),
    ("ws-four-space", "select    a from t"),
    ("ws-mixed-tab-space", "select \t a from t"),
    ("ws-newlines-many", "select\n\n\na\n\n\nfrom t"),
    ("ws-trailing-newline", "select a from t\n"),
    ("ws-only-newlines", "\n\n\n"),
    ("ws-form-feed", "select a\x0cfrom t"),
    ("ws-vertical-tab", "select a\x0bfrom t"),
    ("ws-nbsp", "select a from t"),
    ("ws-before-semi", "select 1 ;"),
    ("ws-inside-parens", "select ( a ) from t"),
    ("ws-around-comma", "select a , b from t"),
    ("ws-no-space-after-comma", "select a,b from t"),
]

ERRORS = [
    ("er-lone-single-quote", "select ' from t"),
    ("er-lone-double-quote", 'select " from t'),
    ("er-lone-backtick", "select ` from t"),
    ("er-unclosed-paren", "select (a from t"),
    ("er-extra-paren", "select a) from t"),
    ("er-unclosed-bracket", "select [a from t"),
    ("er-just-keyword", "distinct"),
    ("er-just-from", "from"),
    ("er-just-where", "where"),
    ("er-double-comma", "select a,, b from t"),
    ("er-trailing-comma", "select a, from t"),
    ("er-leading-comma", "select , a from t"),
    ("er-garbage", "!!!"),
    ("er-only-operator", "+"),
    ("er-backslash", "\\"),
    ("er-null-byte", "select a\x00b from t"),
    ("er-control-chars", "select \x01\x02 from t"),
]

DIALECT = [
    ("dl-mysql-limit-comma", "select a from t limit 1, 2"),
    ("dl-mysql-hint", "select /*+ index(t) */ a from t"),
    ("dl-mysql-handler", "handler t open"),
    ("dl-mysql-show", "show tables"),
    ("dl-mysql-describe", "describe t"),
    ("dl-mysql-explain", "explain select a from t"),
    ("dl-mysql-analyze", "analyze table t"),
    ("dl-mysql-use", "use d"),
    ("dl-mysql-set", "set @x = 1"),
    ("dl-pg-set", "set search_path to public"),
    ("dl-pg-copy", "copy t from stdin"),
    ("dl-pg-vacuum", "vacuum analyze t"),
    ("dl-pg-listen", "listen ch"),
    ("dl-pg-notify", "notify ch"),
    ("dl-pg-window-excl", "select sum(a) over (rows unbounded preceding exclude current row) from t"),
    ("dl-oracle-connect-by", "select a from t connect by prior id = pid"),
    ("dl-oracle-rownum", "select a from t where rownum < 10"),
    ("dl-oracle-dual", "select 1 from dual"),
    ("dl-oracle-plus-join", "select a from t, u where t.id = u.id(+)"),
    ("dl-oracle-nvl", "select nvl(a, 0) from t"),
    ("dl-oracle-decode", "select decode(a, 1, 'x', 'y') from t"),
    ("dl-oracle-partition-by-list", "create table t (a int) partition by list (a)"),
    ("dl-hql-map", "select map(a, b) from t"),
    ("dl-hql-struct", "select struct(a, b) from t"),
    ("dl-hql-array-type", "create table t (a array<int>)"),
    ("dl-hql-lateral-view", "select a from t lateral view explode(b) x"),
    ("dl-hql-cluster-by", "select a from t cluster by a"),
    ("dl-hql-distribute-by", "select a from t distribute by a"),
    ("dl-hql-sort-by", "select a from t sort by a"),
    ("dl-snowflake-qualify", "select a from t qualify row_number() over (order by a) = 1"),
    ("dl-snowflake-ilike-any", "select a from t where b ilike any ('%x%')"),
    ("dl-bigquery-unnest", "select a from unnest([1, 2]) a"),
    ("dl-bigquery-struct", "select struct(1 as a)"),
    ("dl-mssql-bracket", "select [a] from [dbo].[t]"),
    ("dl-mssql-nolock", "select a from t with (nolock)"),
    ("dl-mssql-declare", "declare @x int = 1"),
    ("dl-mssql-print", "print 'x'"),
    ("dl-mssql-go", "select 1\ngo"),
    ("dl-msaccess-first", "select first(a) from t"),
    ("dl-typed-literal-hql", "select date '2020-01-01' from t"),
]

KEYWORD_SHAPES = [
    ("kw-not-null", "select a from t where a is not null"),
    ("kw-not-null-bare", "not null"),
    ("kw-not-null-spaced", "NOT   NULL"),
    ("kw-order-by-bare", "Order By"),
    ("kw-order-by-spaced", "order  by"),
    ("kw-group-by-bare", "group by"),
    ("kw-union-all-bare", "union all"),
    ("kw-primary-key", "primary key"),
    ("kw-foreign-key", "foreign key"),
    ("kw-create-table-bare", "create table"),
    ("kw-inner-join-bare", "inner join"),
    ("kw-left-outer-join-bare", "left outer join"),
    ("kw-is-distinct-from", "is distinct from"),
    ("kw-at-time-zone", "select a at time zone 'utc' from t"),
    ("kw-current-timestamp", "select current_timestamp"),
    ("kw-current-date", "select current_date"),
    ("kw-session-user", "select session_user"),
    ("kw-true-false-null", "select true, false, null"),
    ("kw-and-or-not-bare", "and or not"),
    ("kw-case-when-bare", "case when"),
    ("kw-cte-with-bare", "with"),
    ("kw-dcl-grant-bare", "grant"),
    ("kw-ddl-create-bare", "create"),
    ("kw-dml-select-bare", "select"),
]

PATHOLOGICAL = [
    ("pa-nest-paren-50", "select " + "(" * 50 + "1" + ")" * 50),
    ("pa-nest-paren-200", "select " + "(" * 200 + "1" + ")" * 200),
    ("pa-nest-paren-400", "select " + "(" * 400 + "1" + ")" * 400),
    ("pa-nest-func-100", "select " + "f(" * 100 + "1" + ")" * 100),
    ("pa-nest-case-100", "select " + "case when 1 then " * 100 + "1" + " end" * 100),
    ("pa-nest-bracket-100", "select " + "[" * 100 + "a" + "]" * 100),
    ("pa-wide-cols-500", "select " + ",".join(f"c{i}" for i in range(500)) + " from t"),
    ("pa-wide-values-200", "insert into t values (" + ",".join(str(i) for i in range(200)) + ")"),
    ("pa-wide-in-500", "select a from t where x in (" + ",".join(str(i) for i in range(500)) + ")"),
    ("pa-many-stmts-200", "select 1;" * 200),
    ("pa-many-joins-50", "select a from t" + "".join(f" join u{i} on t.id = u{i}.id" for i in range(50))),
    ("pa-many-unions-100", " union ".join("select 1" for _ in range(100))),
    ("pa-long-string-10k", "select '" + "x" * 10000 + "'"),
    ("pa-long-ident-1k", "select " + "a" * 1000 + " from t"),
    ("pa-long-comment-10k", "select a /* " + "x" * 10000 + " */ from t"),
    ("pa-many-ws-1k", "select" + " " * 1000 + "a from t"),
    ("pa-many-newlines-500", "select a" + "\n" * 500 + "from t"),
    ("pa-quotes-alternating", "select " + " || ".join("'x'" for _ in range(200))),
    ("pa-nest-sub-30", "select a from " + "(select a from " * 30 + "t" + ")" * 30),
    ("pa-many-comments-200", "select a " + " ".join("/* c */" for _ in range(200)) + " from t"),
    ("pa-mixed-deep", "select " + "".join(f"case when x{i} then " for i in range(60))
        + "1" + " end" * 60 + " from t"),
]

FORMAT_TARGETS = [
    # Documents chosen because the reindent / aligned-indent / wrap paths do
    # something structural to them.  The format family runs its whole option
    # matrix over these, so each one is amplified ~24x and they are picked to
    # exercise different branches of the reindent filter.
    ("ft-simple", "select a, b from t where c = 1"),
    ("ft-many-cols", "select a, b, c, d, e, f, g, h from t"),
    ("ft-join-where", "select a from t join u on t.id = u.id where t.a = 1 and u.b = 2"),
    ("ft-subquery", "select a from (select b from u where c = 1) x where a > 2"),
    ("ft-cte", "with x as (select a from t where b = 1) select * from x join y on x.a = y.a"),
    ("ft-case", "select case when a = 1 then 'one' when a = 2 then 'two' else 'many' end from t"),
    ("ft-union", "select a from t where b = 1 union all select a from u where b = 2"),
    ("ft-insert", "insert into t (a, b, c) values (1, 2, 3)"),
    ("ft-insert-select", "insert into t (a, b) select c, d from u where e = 1"),
    ("ft-update", "update t set a = 1, b = 2, c = 3 where d = 4"),
    ("ft-delete", "delete from t where a in (select b from u where c = 1)"),
    ("ft-create", "create table t (a int primary key, b varchar(10) not null, c text)"),
    ("ft-group-having", "select a, count(*) from t group by a having count(*) > 1 order by a desc"),
    ("ft-comment-inline", "select a, -- first\n b from t"),
    ("ft-comment-block", "select a /* the a column */, b from t"),
    ("ft-long-line", "select " + ", ".join(f"column_number_{i}" for i in range(12)) + " from a_table_with_a_long_name"),
    ("ft-nested-func", "select coalesce(max(a), min(b), 0) from t where c between 1 and 10"),
    ("ft-window", "select a, row_number() over (partition by b order by c desc) from t"),
    ("ft-multi-stmt", "select 1; select 2; select 3"),
    ("ft-mixed-case", "SeLeCt A, b FrOm T wHeRe C = 1"),
    ("ft-already-indented", "select a,\n       b\nfrom t\nwhere c = 1"),
    ("ft-tabs", "select\ta,\tb\nfrom\tt"),
    ("ft-no-space-ops", "select a+b,c*d from t where e=1and f=2"),
    ("ft-in-list-long", "select a from t where b in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)"),
    ("ft-values-multi", "insert into t values (1, 2), (3, 4), (5, 6)"),
    ("ft-plsql", "create or replace function f() returns int as $$ begin if a then return 1; else return 2; end if; end; $$ language plpgsql"),
    ("ft-begin-block", "begin insert into t values (1); update t set a = 2; end;"),
    ("ft-order-limit", "select a from t where b = 1 order by c desc limit 10 offset 20"),
    ("ft-star-join", "select t.*, u.* from t, u where t.id = u.id"),
    ("ft-deep-paren", "select ((a + b) * (c - d)) / ((e + f) * (g - h)) from t"),
    ("ft-string-with-newline", "select 'line1\nline2' from t"),
    ("ft-typed-cast", "select a::int, b::text::varchar(10) from t"),
    ("ft-empty-parens", "select f() from t"),
    ("ft-semicolon-trailing", "select a from t;"),
    ("ft-unicode", "select été, 中文 from t where x = 'über'"),
    ("ft-wide-single-line", "select " + ",".join(f"c{i}" for i in range(40)) + " from t"),
    # The two below are here because a mutation went undetected, not because the
    # reindent filter does anything structural to them.  Both exercise
    # utils.split_unquoted_newlines through the postprocess serializer, which every
    # format() call installs -- and the *only* preset that lets the result through
    # unaltered is `identity`, because reindent and reindent_aligned both normalize
    # line endings before the serializer runs.  The format matrix is the one family
    # that runs identity, and it runs it only over this list, so a document not in
    # this list cannot observe either rule.
    #
    # ft-quote-newline-ws: a newline inside a single-quoted string, with trailing
    # whitespace before it.  The splitter must not treat that newline as a line
    # break, and the difference is only visible when whitespace precedes it: the
    # serializer rstrips each line it splits out, so with no trailing whitespace a
    # wrong split rejoins to the same bytes.  ft-string-with-newline above has the
    # newline but not the whitespace, which is why it never caught this.
    #   pristine "select 'a   \nb' from t"   mutated "select 'a\nb' from t"
    ("ft-quote-newline-ws", "select 'a   \nb' from t"),
    # ft-cr-line-break: a bare CR, which the reference treats as a line break and
    # normalizes to LF.  sel-cr-only carries the same bytes and is used by seven
    # cases, none of which could see it: the two format-wide cases reindent, and
    # the rest ask about tokens and trees, where the CR is preserved either way.
    #   pristine "select a\nfrom t"          mutated "select a\rfrom t"
    ("ft-cr-line-break", "select a\rfrom t where b = 1"),
]

CURATED_GROUPS = [
    ("basic", BASIC), ("where", WHERE), ("joins", JOINS), ("grouping", GROUPING),
    ("subquery", SUBQUERY_CTE), ("dml", DML), ("ddl", DDL),
    ("functions", FUNCTIONS), ("case", CASE_EXPR), ("comments", COMMENTS),
    ("strings", STRINGS), ("numbers", NUMBERS), ("identifiers", IDENTIFIERS),
    ("operators", OPERATORS), ("multi", MULTI), ("plsql", PLSQL),
    ("whitespace", WHITESPACE), ("errors", ERRORS), ("dialect", DIALECT),
    ("keywords", KEYWORD_SHAPES), ("pathological", PATHOLOGICAL),
    ("format", FORMAT_TARGETS),
]

# Documents whose bytes are deliberately not valid UTF-8, or are valid UTF-8 that
# also decodes as something else.  These are the encoding path.
RAW_DOCS = [
    ("by-cp1251", b"select '\xe1\xe2\xe3' from t", "cp1251"),
    ("by-latin1-high", b"select '\xe1\xe2\xe3' from t", ""),

    # The fallback codec is unicode-escape, not Latin-1.  The two agree on every
    # byte above 0x7f and disagree on every backslash sequence, so a port that
    # reached for Latin-1 needs these documents to tell it so.  Each is bytes
    # that fail UTF-8 -- the trailing 0xe1 forces the fallback -- and also
    # carries an escape the two codecs read differently.
    ("by-fb-hex", b"select '\\x41' from t \xe1", ""),
    ("by-fb-newline", b"select 'a\\nb' from t \xe1", ""),
    ("by-fb-tab", b"select 'a\\tb' from t \xe1", ""),
    ("by-fb-backslash", b"select 'a\\\\b' from t \xe1", ""),
    ("by-fb-octal", b"select '\\101' from t \xe1", ""),
    ("by-fb-u16", b"select '\\u00e9' from t \xe1", ""),
    ("by-fb-u32", b"select '\\U000000e9' from t \xe1", ""),
    ("by-fb-quote-esc", b"select '\\'' from t \xe1", ""),
    ("by-fb-unknown-esc", b"select 'a\\qb' from t \xe1", ""),
    ("by-fb-mixed", b"select '\\x41\\n\\\\' from t \xe1", ""),
    # A lone trailing backslash: the reference raises UnicodeDecodeError from
    # inside the fallback rather than yielding a backslash.
    ("by-fb-trailing-backslash", b"select 'a' from t \xe1\\", ""),

    # gbk is outside the declared supported set, so both sides must error.  The
    # bytes stay in the document set because they are also a fallback-path document
    # when no encoding is declared.
    ("by-gbk", b"select '\xd6\xd0\xce\xc4' from t", "gbk"),
    ("by-gbk-nodecl", b"select '\xd6\xd0\xce\xc4' from t", ""),
    ("by-utf8-explicit", "select 'été' from t".encode("utf-8"), "utf-8"),
    ("by-utf8-implicit", "select 'été' from t".encode("utf-8"), ""),
    ("by-utf8-bom", b"\xef\xbb\xbfselect 1", ""),
    ("by-utf16", "select 1".encode("utf-16"), "utf-16"),
    ("by-invalid-utf8", b"select '\xff\xfe' from t", ""),
    ("by-mixed-valid", b"select 'a\xc3\xa9b' from t", "utf-8"),
    ("by-truncated-utf8", b"select '\xc3' from t", ""),
    ("by-nul", b"select 'a\x00b' from t", ""),
    ("by-ascii-escape-lookalike", rb"select '\x41' x", ""),
    ("by-cp1251-file", None, "cp1251"),   # filled from the sample
    ("by-gbk-file", None, "gbk"),
]

# --------------------------------------------------------------------------
# 2. Synthetic generation
# --------------------------------------------------------------------------

_TABLES = ["t", "u", "orders", "line_items", "s.customers", '"odd name"']
_COLS = ["a", "b", "id", "total_amount", "s.x", '"q c"', "count(*)", "a + b"]
_PREDS = [
    "a = 1", "b > 2", "c is null", "d in (1, 2)", "e like '%x%'",
    "f between 1 and 2", "g <> 'z'", "h is not null", "not i",
]
_AGGS = ["count(*)", "sum(a)", "avg(b)", "min(c)", "max(d)"]
_JOINS = ["join", "left join", "right join", "inner join", "cross join",
          "full outer join"]
_ORDER = ["a", "b desc", "1", "upper(a)", "a asc, b desc"]


def _synth_select(rng: random.Random) -> str:
    cols = ", ".join(rng.sample(_COLS, rng.randint(1, 4)))
    parts = [f"select {'distinct ' if rng.random() < 0.15 else ''}{cols}"]
    parts.append(f"from {rng.choice(_TABLES)}")
    for _ in range(rng.randint(0, 2)):
        parts.append(f"{rng.choice(_JOINS)} {rng.choice(_TABLES)} on a = b")
    if rng.random() < 0.7:
        n = rng.randint(1, 3)
        joiner = rng.choice([" and ", " or "])
        parts.append("where " + joiner.join(rng.sample(_PREDS, n)))
    if rng.random() < 0.3:
        parts.append(f"group by {rng.choice(['a', 'a, b', '1'])}")
        if rng.random() < 0.5:
            parts.append(f"having {rng.choice(_AGGS)} > 0")
    if rng.random() < 0.4:
        parts.append(f"order by {rng.choice(_ORDER)}")
    if rng.random() < 0.2:
        parts.append(f"limit {rng.randint(1, 100)}")
    sep = rng.choice([" ", "\n", "  ", "\n  ", "\t"])
    return sep.join(parts)


def _synth_dml(rng: random.Random) -> str:
    kind = rng.choice(["insert", "update", "delete"])
    table = rng.choice(_TABLES)
    if kind == "insert":
        n = rng.randint(1, 4)
        literals = ["1", "'x'", "null", "now()", "-2", "1.5"]
        cols = ", ".join(f"c{i}" for i in range(n))
        vals = ", ".join(rng.choice(literals) for _ in range(n))
        if rng.random() < 0.4:
            return f"insert into {table} ({cols}) values ({vals})"
        return f"insert into {table} values ({vals})"
    if kind == "update":
        rhs = ["1", "'x'", "null", "c1 + 1"]
        sets = ", ".join(
            "c{} = {}".format(i, rng.choice(rhs))
            for i in range(rng.randint(1, 3))
        )
        tail = f" where {rng.choice(_PREDS)}" if rng.random() < 0.8 else ""
        return f"update {table} set {sets}{tail}"
    tail = f" where {rng.choice(_PREDS)}" if rng.random() < 0.8 else ""
    return f"delete from {table}{tail}"


def _synth_ddl(rng: random.Random) -> str:
    what = rng.choice(["table", "view", "index"])
    types = ["int", "varchar(10)", "text", "numeric(9,2)", "timestamp"]
    constraints = ["", " not null", " primary key", " default 0"]
    if what == "table":
        cols = ", ".join(
            "c{} {}{}".format(i, rng.choice(types), rng.choice(constraints))
            for i in range(rng.randint(1, 4))
        )
        ine = rng.choice(["", "if not exists "])
        return f"create table {ine}t ({cols})"
    if what == "view":
        orr = rng.choice(["", "or replace "])
        return f"create {orr}view v as {_synth_select(rng)}"
    uniq = rng.choice(["", "unique "])
    extra = rng.choice(["", ", b"])
    return f"create {uniq}index i on t (a{extra})"


def _synth_expr(rng: random.Random, depth: int = 0) -> str:
    if depth > 2 or rng.random() < 0.3:
        return rng.choice(["a", "1", "'x'", "null", "b.c", "-1", "0xff", "1.5e3"])
    op = rng.choice(["+", "-", "*", "/", "||", "%"])
    left = _synth_expr(rng, depth + 1)
    right = _synth_expr(rng, depth + 1)
    form = rng.random()
    if form < 0.3:
        return f"({left} {op} {right})"
    if form < 0.5:
        return f"{left}{op}{right}"
    if form < 0.65:
        return f"{rng.choice(['f', 'coalesce', 'upper', 'cast'])}({left})"
    if form < 0.75:
        return f"case when {left} then {right} else null end"
    return f"{left} {op} {right}"


def _synth_mixed(rng: random.Random) -> str:
    n = rng.randint(2, 4)
    stmts = []
    for _ in range(n):
        stmts.append(rng.choice([_synth_select, _synth_dml, _synth_ddl])(rng))
    sep = rng.choice(["; ", ";\n", ";", ";\n\n"])
    return sep.join(stmts) + rng.choice(["", ";", ";\n"])


def _synth_commented(rng: random.Random) -> str:
    body = _synth_select(rng)
    style = rng.random()
    if style < 0.3:
        return f"-- leading\n{body}"
    if style < 0.5:
        return f"{body} -- trailing"
    if style < 0.7:
        return f"/* block */ {body}"
    words = body.split(" ")
    if len(words) > 3:
        cut = rng.randint(1, len(words) - 1)
        words.insert(cut, "/* mid */")
    return " ".join(words)


def synthesize(
    count: int, seed: int = 0x5150A75E, exclude: set[str] | None = None
) -> list[tuple[str, str]]:
    """Deterministic pseudo-random documents.

    Fixed seed on purpose: the document set has to be the same in every build of the
    image, or the frozen expectations would not correspond to it.

    `exclude` holds the documents already in the document set.  The grammar is small
    enough that it does sometimes rediscover a curated document -- "create
    unique index i on t (a, b)" is a thing both a person and the generator would
    write -- and a duplicate would be a second identical expectation rather than
    additional coverage.  Skipping and drawing again keeps the count exact.
    """
    rng = random.Random(seed)
    makers = [
        ("sel", _synth_select, 0.34),
        ("dml", _synth_dml, 0.16),
        ("ddl", _synth_ddl, 0.12),
        ("expr", lambda r: f"select {_synth_expr(r)} from t", 0.14),
        ("multi", _synth_mixed, 0.12),
        ("comment", _synth_commented, 0.12),
    ]
    weights = [w for _, _, w in makers]
    out: list[tuple[str, str]] = []
    counters: dict[str, int] = {}
    seen: set[str] = set(exclude or ())
    guard = 0
    while len(out) < count and guard < count * 50:
        guard += 1
        name, maker, _ = rng.choices(makers, weights=weights)[0]
        sql = maker(rng)
        if sql in seen or not sql.strip():
            continue
        seen.add(sql)
        counters[name] = counters.get(name, 0) + 1
        out.append((f"gen-{name}-{counters[name]:03d}", sql))
    return out


# --------------------------------------------------------------------------
# 3. Assembly
# --------------------------------------------------------------------------

GENERATED_COUNT = 120


def build(baseline: Path, out_dir: Path) -> dict:
    """Write the document set and return its metadata, including a digest.

    The digest covers every document's id and bytes.  The driver compares it
    against the value recorded in the image manifest, so a document set that changed
    after freezing is reported as a broken verifier rather than as a submission
    that fails everything.
    """
    docs_dir = out_dir / "docs"
    if docs_dir.exists():
        shutil.rmtree(docs_dir)
    docs_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict] = []
    seen_ids: set[str] = set()
    seen_payloads: dict[tuple[bytes, str], str] = {}

    def add(doc_id: str, data: bytes, group: str, encoding: str = "") -> None:
        if doc_id in seen_ids:
            raise RuntimeError(f"duplicate document id: {doc_id}")
        # Two ids for the same bytes *and* the same declared encoding would
        # produce two identical expectations and inflate the case count without
        # adding coverage.  The same bytes under a different encoding is a
        # different input and is allowed on purpose: b"\xe1\xe2\xe3" read as
        # cp1251 and read with no encoding are the two halves of the fallback
        # behavior this suite grades.
        key = (data, encoding)
        prior = seen_payloads.get(key)
        if prior is not None:
            raise RuntimeError(
                f"documents {prior!r} and {doc_id!r} have identical bytes "
                f"under the same encoding {encoding!r}"
            )
        seen_ids.add(doc_id)
        seen_payloads[key] = doc_id
        (docs_dir / doc_id).write_bytes(data)
        entries.append({
            "id": doc_id,
            "group": group,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "encoding": encoding,
            "utf8": _is_utf8(data),
        })

    for group, items in CURATED_GROUPS:
        for doc_id, sql in items:
            add(doc_id, sql.encode("utf-8"), f"written/{group}")

    # Fixtures from State A.  Read as bytes: two of them are not UTF-8.
    samples = sorted((baseline / "tests" / "files").glob("*.sql"))
    if not samples:
        raise RuntimeError(f"no samples found under {baseline / 'tests' / 'files'}")
    for path in samples:
        data = path.read_bytes()
        enc = ""
        if "cp1251" in path.name:
            enc = "cp1251"
        elif "gbk" in path.name:
            enc = "gbk"
        add(f"sample-{path.stem}", data, "sample", enc)
        # Also register the first statement of each sample on its own: the whole
        # file exercises the splitter, one statement exercises the tree.
        text = data.decode("utf-8", "replace")
        head = text.split(";")[0].strip()
        if 8 <= len(head) <= 4000 and (head.encode("utf-8"), enc) not in seen_payloads:
            add(f"sample1-{path.stem}", head.encode("utf-8"), "sample-head", enc)

    for doc_id, data, enc in RAW_DOCS:
        if data is None:
            src = {"by-cp1251-file": "test_cp1251.sql",
                   "by-gbk-file": "encoding_gbk.sql"}[doc_id]
            path = baseline / "tests" / "files" / src
            if not path.is_file():
                raise RuntimeError(f"missing sample for {doc_id}: {path}")
            data = path.read_bytes()
            if (data, enc) in seen_payloads:
                # The sample is already in the document set under its sample- id; the
                # encoding family reaches it through that id instead.
                continue
        add(doc_id, data, "bytes", enc)

    already = {
        data.decode("utf-8")
        for data, enc in seen_payloads
        if enc == "" and _is_utf8(data)
    }
    for doc_id, sql in synthesize(GENERATED_COUNT, exclude=already):
        add(doc_id, sql.encode("utf-8"), "generated")
    if len([e for e in entries if e["group"] == "generated"]) != GENERATED_COUNT:
        raise RuntimeError(
            f"the generator produced fewer than {GENERATED_COUNT} distinct "
            f"generated documents"
        )

    entries.sort(key=lambda e: e["id"])
    digest = hashlib.sha256(
        json.dumps(
            [[e["id"], e["sha256"], e["encoding"]] for e in entries],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    by_group: dict[str, int] = {}
    for e in entries:
        by_group[e["group"]] = by_group.get(e["group"], 0) + 1

    meta = {
        "schema": "swerefactor-documents-v1",
        "count": len(entries),
        "digest": digest,
        "documents": entries,
        "by_group": dict(sorted(by_group.items())),
        "generated_count": GENERATED_COUNT,
        "non_utf8": sorted(e["id"] for e in entries if not e["utf8"]),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "documents.json").write_text(
        json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return meta


def _is_utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def ids_in_group(meta: dict, prefix: str) -> list[str]:
    return [e["id"] for e in meta["documents"] if e["group"].startswith(prefix)]


def utf8_ids(meta: dict) -> list[str]:
    """Documents that are valid UTF-8, i.e. usable as plain text on the wire."""
    return [e["id"] for e in meta["documents"] if e["utf8"]]


if __name__ == "__main__":
    import sys

    base = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/assets/assets/baseline")
    dest = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/documents")
    info = build(base, dest)
    print(json.dumps(
        {k: v for k, v in info.items() if k != "documents"}, indent=1, sort_keys=True
    ))
