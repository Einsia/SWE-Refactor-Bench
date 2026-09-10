"""The extension modules the release build ships: fts4, fts5, rtree, sqlar,
zipfile, the deserialize surface and the introspection virtual tables.

These are not optional decoration.  The repo's own Makefile decides which of them
the ``sqlite3`` binary carries -- ``OPT_FEATURE_FLAGS`` plus ``SHELL_OPT``,
compiled into *both* translation units by a single link line -- so "the same
binary on a new platform" means this exact set and no other.  A port that quietly
drops one has changed the artifact, and a port that adds one has too.

Three of them reach the platform rather than staying inside the engine, which is
why they are worth their own family:

``sqlar`` and ``zipfile``
    need zlib compiled for wasm32-wasi, so the port has to cross-compile a second
    library, and ``.archive`` reads a directory through ``opendir``/``readdir``.

``fsdir`` and ``writefile``
    stat and create real files.  WASI has no permission bits at all -- wasi-libc's
    ``chmod`` is a stub returning ENOTSUP, and ``filestat_get`` reports a file type
    with none of the mode bits -- so every observation of a ``mode`` here is
    masked to the type bits with ``& 61440``.  That mask is not a normaliser
    hiding a difference: the type bits are the portable part of the value, and the
    absent permission bits are asserted positively, as a literal, in
    ``cases_platform``.

``.open --deserialize``/``--hexdb``/``--append``
    run the engine against memory and against a database appended to another
    file, both through a different VFS than the port's own.
"""

from __future__ import annotations

import seeddb
from case import Case

FAMILY = "ext"

#: The preopened guest directory, spelled the same way runner.GUEST_DATA does.
#: Cases that need an *absolute* name use this; cases that exercise relative
#: resolution deliberately do not.
GUEST_DATA = "/data"
DB = f"{GUEST_DATA}/d.db"

# Only the type bits of a stat mode.  0xF000 == 61440; written in decimal because
# that is what SQLite's integer literals give back and what the ledger records.
TYPEBITS = "& 61440"


def _ext(key: str, operation: str, sql: str, *, argv: tuple[str, ...] = (":memory:",),
         **kw) -> Case:
    kw.setdefault("checks", ("stdout", "stderr", "exit"))
    kw["normalisers"] = tuple(kw.get("normalisers", ())) + ("longdouble", "progname")
    return Case(key=f"ext.{key}", family=FAMILY, operation=operation, kind="cli",
                argv=argv, stdin=sql if sql.endswith("\n") else sql + "\n", **kw)


# A small corpus with repeated terms, so ranking functions have something to
# rank and prefix queries have something to match.
_DOCS = (
    "the quick brown fox jumps over the lazy dog",
    "a quick brown dog outpaces a quick fox",
    "the lazy dog sleeps all day in the sun",
    "quick thinking beats quick talking every time",
    "brown bread and brown butter",
    "sphinx of black quartz judge my vow",
    "pack my box with five dozen liquor jugs",
    "how vexingly quick daft zebras jump",
)


def _rows(table: str, cols: str = "body") -> str:
    return "".join(
        f"insert into {table}({cols}) values('{d}');\n" for d in _DOCS
    )


_FTS4_QUERIES = (
    ("term", "body match 'quick'"),
    ("phrase", "body match '\"quick brown\"'"),
    ("prefix", "body match 'qui*'"),
    ("and", "body match 'quick AND dog'"),
    ("or", "body match 'sphinx OR zebras'"),
    ("not", "body match 'quick NOT dog'"),
    ("near", "body match 'quick NEAR dog'"),
    ("near.n", "body match 'quick NEAR/2 fox'"),
    ("nomatch", "body match 'nonexistentword'"),
    ("multi", "body match 'brown brown'"),
    ("case", "body match 'QUICK'"),
    ("paren", "body match '(quick OR lazy) AND dog'"),
)


def _fts4() -> list[Case]:
    """fts4, its auxiliary functions and the fts4aux shadow table."""
    setup = "create virtual table ft using fts4(body);\n" + _rows("ft")
    cases: list[Case] = []
    for name, where in _FTS4_QUERIES:
        cases.append(_ext(f"fts4.query.{name}", "ext.fts4.query",
                          setup + f"select docid,body from ft where {where}"
                                  " order by docid;"))
    for name, expr in (
        ("offsets", "offsets(ft)"),
        ("matchinfo", "matchinfo(ft)"),
        ("matchinfo.pcx", "hex(matchinfo(ft,'pcx'))"),
        ("matchinfo.nls", "hex(matchinfo(ft,'nls'))"),
        ("snippet", "snippet(ft)"),
        ("snippet.args", "snippet(ft,'[',']','...',-1,10)"),
    ):
        cases.append(_ext(f"fts4.aux.{name}", f"ext.fts4.{name.split('.')[0]}",
                          setup + f"select docid,{expr} from ft where body match"
                                  " 'quick' order by docid;"))
    for name, ddl in (
        ("simple", "fts4(body, tokenize=simple)"),
        ("porter", "fts4(body, tokenize=porter)"),
        ("unicode61", "fts4(body, tokenize=unicode61)"),
        ("unicode61.remove", "fts4(body, tokenize=unicode61 \"remove_diacritics=1\")"),
        ("notindexed", "fts4(body, extra, notindexed=extra)"),
        ("prefix", "fts4(body, prefix='2,3')"),
        ("compress", "fts4(body, order=DESC)"),
        ("matchinfo.fts3", "fts4(body, matchinfo=fts3)"),
        ("languageid", "fts4(body, languageid=lid)"),
        ("two.cols", "fts4(title, body)"),
    ):
        ins = ("insert into ft(title,body) values('t1','quick brown'),"
               "('t2','lazy dog');\n" if "title" in ddl else
               "insert into ft(body,extra) values('quick brown','x');\n"
               if "extra" in ddl else _rows("ft"))
        cases.append(_ext(f"fts4.ddl.{name}", "ext.fts4.tokenizer",
                          f"create virtual table ft using {ddl};\n{ins}"
                          "select count(*) from ft where "
                          f"{'title' if 'title' in ddl else 'body'} match 'quick';"))
    for name, stmt in (
        ("optimize", "insert into ft(ft) values('optimize');"),
        ("rebuild", "insert into ft(ft) values('rebuild');"),
        ("audit", "insert into ft(ft) values('audit-check');"),
        ("merge", "insert into ft(ft) values('merge=100,8');"),
        ("automerge", "insert into ft(ft) values('automerge=4');"),
    ):
        cases.append(_ext(f"fts4.cmd.{name}", "ext.fts4.command",
                          setup + stmt + "select count(*) from ft where body"
                                         " match 'quick';"))
    cases.append(_ext("fts4.delete", "ext.fts4.delete",
                      setup + "delete from ft where docid=1;"
                              "select count(*) from ft where body match 'quick';"))
    cases.append(_ext("fts4.update", "ext.fts4.update",
                      setup + "update ft set body='replaced text' where docid=2;"
                              "select docid,body from ft where body match"
                              " 'replaced';"))
    cases.append(_ext("fts4.aux.table", "ext.fts4.fts4aux",
                      setup + "create virtual table fa using fts4aux(ft);"
                              "select term,documents,occurrences from fa"
                              " where col='*' order by term limit 12;"))
    cases.append(_ext("fts4.shadow", "ext.fts4.shadow",
                      setup + "select name from sqlite_master where name like"
                              " 'ft_%' order by name;"))
    cases.append(_ext("fts4.content.external", "ext.fts4.content",
                      "create table src(id integer primary key, body);\n"
                      "insert into src values(1,'quick brown fox'),(2,'lazy dog');\n"
                      "create virtual table ft using fts4(content=src, body);\n"
                      "insert into ft(ft) values('rebuild');\n"
                      "select docid from ft where body match 'quick';"))
    return cases


_FTS5_QUERIES = (
    ("term", "'quick'"),
    ("phrase", "'\"quick brown\"'"),
    ("prefix", "'qui*'"),
    ("and", "'quick AND dog'"),
    ("or", "'sphinx OR zebras'"),
    ("not", "'quick NOT dog'"),
    ("near", "'NEAR(quick dog)'"),
    ("near.n", "'NEAR(quick fox, 2)'"),
    ("plus", "'quick + brown'"),
    ("nomatch", "'nonexistentword'"),
    ("initial", "'^the'"),
    ("paren", "'(quick OR lazy) AND dog'"),
)


def _fts5() -> list[Case]:
    """fts5: queries, ranking, auxiliary functions, the vocab tables, config."""
    setup = "create virtual table f5 using fts5(body);\n" + _rows("f5")
    cases: list[Case] = []
    for name, query in _FTS5_QUERIES:
        cases.append(_ext(f"fts5.query.{name}", "ext.fts5.query",
                          setup + f"select rowid,body from f5 where f5 match {query}"
                                  " order by rowid;"))
        cases.append(_ext(f"fts5.rank.{name}", "ext.fts5.rank",
                          setup + f"select rowid, printf('%.6f',rank) from f5"
                                  f" where f5 match {query} order by rank, rowid;"))
    for name, expr in (
        ("bm25", "printf('%.6f',bm25(f5))"),
        ("bm25.weighted", "printf('%.6f',bm25(f5,2.0))"),
        ("highlight", "highlight(f5,0,'<b>','</b>')"),
        ("snippet", "snippet(f5,0,'[',']','...',8)"),
        ("snippet.short", "snippet(f5,-1,'{','}','~',3)"),
    ):
        cases.append(_ext(f"fts5.aux.{name}", f"ext.fts5.{name.split('.')[0]}",
                          setup + f"select rowid,{expr} from f5 where f5 match"
                                  " 'quick' order by rowid;"))
    for name, ddl in (
        ("ascii", "fts5(body, tokenize='ascii')"),
        ("unicode61", "fts5(body, tokenize='unicode61')"),
        ("porter", "fts5(body, tokenize='porter ascii')"),
        ("unicode61.nodia", "fts5(body, tokenize=\"unicode61 remove_diacritics 0\")"),
        ("prefix", "fts5(body, prefix='2 3')"),
        ("columnsize", "fts5(body, columnsize=0)"),
        ("detail.none", "fts5(body, detail=none)"),
        ("detail.column", "fts5(body, detail=column)"),
        ("detail.full", "fts5(body, detail=full)"),
        ("two.cols", "fts5(title, body)"),
        ("unindexed", "fts5(body, extra UNINDEXED)"),
        ("contentless", "fts5(body, content='')"),
    ):
        if "title" in ddl:
            ins = ("insert into f5(title,body) values('t1','quick brown'),"
                   "('t2','lazy dog');\n")
            probe = "select count(*) from f5 where f5 match 'quick';"
        elif "extra" in ddl:
            ins = "insert into f5(body,extra) values('quick brown','ignored');\n"
            probe = "select count(*) from f5 where f5 match 'quick';"
        elif "content=''" in ddl:
            ins = _rows("f5")
            probe = "select rowid from f5 where f5 match 'quick' order by rowid;"
        else:
            ins = _rows("f5")
            probe = "select count(*) from f5 where f5 match 'quick';"
        cases.append(_ext(f"fts5.ddl.{name}", "ext.fts5.config",
                          f"create virtual table f5 using {ddl};\n{ins}{probe}"))
    for name, kind in (("row", "row"), ("col", "col"), ("instance", "instance")):
        limit = " limit 12" if kind == "instance" else " limit 10"
        cases.append(_ext(f"fts5.vocab.{name}", "ext.fts5.vocab",
                          setup + f"create virtual table v using fts5vocab(f5,{kind});"
                                  f"select * from v order by 1,2,3{limit};"))
    for name, stmt in (
        ("rebuild", "insert into f5(f5) values('rebuild');"),
        ("optimize", "insert into f5(f5) values('optimize');"),
        ("audit", "insert into f5(f5) values('audit-check');"),
        ("merge", "insert into f5(f5,rank) values('merge',100);"),
        ("pgsz", "insert into f5(f5,rank) values('pgsz',1024);"),
        ("automerge", "insert into f5(f5,rank) values('automerge',4);"),
        ("crisismerge", "insert into f5(f5,rank) values('crisismerge',8);"),
        ("usermerge", "insert into f5(f5,rank) values('usermerge',4);"),
        ("deletall", "insert into f5(f5) values('delete-all');"),
    ):
        cases.append(_ext(f"fts5.cmd.{name}", "ext.fts5.command",
                          setup + stmt + "select count(*) from f5 where f5 match"
                                         " 'quick';"))
    cases.append(_ext("fts5.rank.config", "ext.fts5.rank_config",
                      setup + "insert into f5(f5,rank) values('rank','bm25(3.0)');"
                              "select rowid from f5 where f5 match 'quick'"
                              " order by rank,rowid;"))
    cases.append(_ext("fts5.colfilter", "ext.fts5.colfilter",
                      "create virtual table f5 using fts5(title,body);"
                      "insert into f5 values('quick title','lazy body'),"
                      "('plain title','quick body');"
                      "select rowid from f5 where f5 match 'title:quick';"))
    cases.append(_ext("fts5.delete", "ext.fts5.delete",
                      setup + "delete from f5 where rowid=1;"
                              "select count(*) from f5 where f5 match 'quick';"))
    cases.append(_ext("fts5.update", "ext.fts5.update",
                      setup + "update f5 set body='replaced' where rowid=2;"
                              "select rowid from f5 where f5 match 'replaced';"))
    cases.append(_ext("fts5.shadow", "ext.fts5.shadow",
                      setup + "select name from sqlite_master where name like"
                              " 'f5_%' order by name;"))
    return cases


# A grid of boxes plus a few degenerate ones, so overlap, containment and
# empty-result queries all have something to find.
_RT_ROWS = "".join(
    f"insert into rt values({i},{i * 10},{i * 10 + 5},{i * 10},{i * 10 + 5});\n"
    for i in range(1, 9)
) + (
    "insert into rt values(100,0.0,100.0,0.0,100.0);\n"      # covers everything
    "insert into rt values(101,50.0,50.0,50.0,50.0);\n"      # zero area
    "insert into rt values(102,-10.0,-5.0,-10.0,-5.0);\n"    # negative
)

_RT_QUERIES = (
    ("overlap", "minx<=25 and maxx>=15"),
    ("contains", "minx>=0 and maxx<=20 and miny>=0 and maxy<=20"),
    ("point", "minx<=50 and maxx>=50 and miny<=50 and maxy>=50"),
    ("negative", "minx<0"),
    ("empty", "minx>1000"),
    ("byid", "id=3"),
    ("range", "id between 2 and 5"),
    ("all", "1"),
)


def _rtree() -> list[Case]:
    """rtree in several dimensions, plus rtreecheck and the integer variant."""
    setup = "create virtual table rt using rtree(id,minx,maxx,miny,maxy);\n" + _RT_ROWS
    cases: list[Case] = []
    for name, where in _RT_QUERIES:
        cases.append(_ext(f"rtree.query.{name}", "ext.rtree.query",
                          setup + f"select id from rt where {where} order by id;"))
    for dims in (1, 2, 3, 4, 5):
        cols = ",".join(f"c{i}min,c{i}max" for i in range(dims))
        vals = ",".join(f"{i},{i + 1}" for i in range(dims))
        cases.append(_ext(f"rtree.dims.{dims}", "ext.rtree.dimensions",
                          f"create virtual table r{dims} using rtree(id,{cols});"
                          f"insert into r{dims} values(1,{vals});"
                          f"select id,{cols} from r{dims};"))
    cases.append(_ext("rtree.i32", "ext.rtree.i32",
                      "create virtual table ri using rtree_i32(id,x1,x2,y1,y2);"
                      "insert into ri values(1,0,10,0,10),(2,-5,-1,-5,-1);"
                      "select id,x1,x2,y1,y2 from ri order by id;"))
    cases.append(_ext("rtree.aux", "ext.rtree.aux",
                      "create virtual table ra using rtree(id,x1,x2,+label);"
                      "insert into ra values(1,0,10,'first'),(2,20,30,'second');"
                      "select id,label from ra where x1<15 order by id;"))
    cases.append(_ext("rtree.check", "ext.rtree.rtreecheck",
                      setup + "select rtreecheck('rt');"))
    cases.append(_ext("rtree.delete", "ext.rtree.delete",
                      setup + "delete from rt where id=100;"
                              "select count(*) from rt;select rtreecheck('rt');"))
    cases.append(_ext("rtree.update", "ext.rtree.update",
                      setup + "update rt set minx=minx-1 where id=2;"
                              "select id,minx,maxx from rt where id=2;"
                              "select rtreecheck('rt');"))
    cases.append(_ext("rtree.shadow", "ext.rtree.shadow",
                      setup + "select name from sqlite_master where name like"
                              " 'rt_%' order by name;"))
    cases.append(_ext("rtree.many", "ext.rtree.many",
                      "create virtual table rt using rtree(id,minx,maxx);"
                      "with recursive c(i) as (select 1 union all select i+1 from c"
                      " where i<600) insert into rt select i,i*1.0,i*1.0+2 from c;"
                      "select count(*) from rt where minx between 100 and 200;"
                      "select rtreecheck('rt');"))
    cases.append(_ext("rtree.plan", "ext.rtree.plan",
                      setup + "explain query plan select id from rt where minx>5"
                              " and maxx<40;", normalisers=("eqp",)))
    return cases


# A directory tree to archive.  Contents are fixed text so the compressed blobs
# are byte-comparable, and the shapes vary: nested, empty file, long file, a name
# needing quoting, and one that compresses well versus one that does not.
_TREE = {
    "src/a.txt": "hello archive\n",
    "src/b.txt": "second file\n",
    "src/nested/deep/c.txt": "third file, further down\n",
    "src/empty.txt": "",
    "src/repeat.txt": "AAAAAAAAAAAAAAAA" * 64,
    "src/random.bin": "".join(chr(33 + (i * 37) % 90) for i in range(1024)),
    "src/has space.txt": "name with a space\n",
}


def _lines(path: str) -> str:
    """SQL that splits a file's text back into one row per line.

    Used to sort a dot command's output before asserting it.  ``.archive -cv``,
    ``-xv``, ``-uv`` and ``-iv`` print one name per file in the order ``fsdir``
    walked the directory, which is ``readdir`` order -- and that is a property of
    the *host filesystem*, not of the port.  Measured over eight names on ext4 and
    on tmpfs: the two enumerate them differently, and neither returns creation
    order or sorted order.  wasmtime hands the host order through unchanged, so a
    recorded expectation would fail in the verifier container for a reason that has
    nothing to do with wasm.  The same applies to any listing that scans the sqlar
    table instead of its index, because the rowids come from the same walk.

    Sorting in SQL asserts the *set* of names and their count, which is what
    upstream actually promises.  ``.archive -t`` needs none of this: it selects
    only ``name``, so it reads the primary-key index and comes out sorted.
    """
    return (
        "with recursive lines(rest, line) as ("
        f" select cast(readfile('{path}') as text), null"
        " union all"
        " select substr(rest, instr(rest, char(10))+1),"
        "        substr(rest, 1, instr(rest, char(10))-1)"
        " from lines where instr(rest, char(10))>0)"
    )


def _archive() -> list[Case]:
    """.archive, the sqlar functions and zipfile -- the parts that need zlib.

    Every ``mode`` is masked to its type bits.  WASI reports no permission bits,
    so the raw column is a platform property asserted once in ``cases_platform``;
    the type bits, the names, the sizes and the *contents* are all portable, and
    they are what says the archive is really an archive.
    """
    cases: list[Case] = []
    listing = (f"select name, mode {TYPEBITS} as t, sz from sqlar order by name;")
    content = ("select name, sqlar_uncompress(data,sz) from sqlar where sz>0"
               " order by name;")
    for key, script, tail in (
        ("create", ".archive -c src", listing),
        ("list", ".archive -c src\n.archive -t", ""),
        ("content", ".archive -c src", content),
        ("digest", ".archive -c src",
         "select name, length(data) from sqlar order by name;"),
        ("update", ".archive -c src/a.txt\n.archive -u src/b.txt", listing),
        ("insert", ".archive -c src/a.txt\n.archive -i src/b.txt", listing),
        ("extract", ".archive -c src\n.archive -x -C out",
         "select name, mode " + TYPEBITS + " from sqlar order by name;"),
        ("extract.roundtrip", ".archive -c src\n.archive -x -C out",
         "select readfile('out/src/a.txt'), readfile('out/src/repeat.txt')"
         "=readfile('src/repeat.txt');"),
        ("one.file", ".archive -c src/a.txt", listing),
        ("nested", ".archive -c src/nested", listing),
        ("empty.file", ".archive -c src/empty.txt", listing),
        ("spaces", ".archive -c 'src/has space.txt'", listing),
        ("append.twice", ".archive -c src/a.txt\n.archive -u src/a.txt", listing),
        ("sizes", ".archive -c src",
         "select sum(sz), count(*), sum(data is null) from sqlar;"),
        ("dirs", ".archive -c src",
         f"select name from sqlar where mode {TYPEBITS}=16384 order by name;"),
        ("compressed", ".archive -c src",
         "select name from sqlar where length(data)<sz order by name;"),
        ("stored", ".archive -c src",
         "select name from sqlar where length(data)=sz order by name;"),
    ):
        cases.append(_ext(f"archive.{key}", f"ext.archive.{key.split('.')[0]}",
                          f"{script}\n{tail}", argv=(DB,), files=dict(_TREE)))

    # The verbose forms print one name per archived file, in the order the
    # directory walk produced them -- see _lines().  Route the names through a
    # file and sort them, so the assertion is about which files were archived
    # rather than about the host's readdir order.
    for key, script in (
        ("create.verbose", ".archive -cv src"),
        ("extract.verbose", ".archive -c src\n.output x.txt\n.archive -xv -C out"),
        ("update.verbose", ".archive -c src\n.output x.txt\n.archive -uv src"),
        ("insert.verbose", ".archive -c src\n.output x.txt\n.archive -iv src"),
    ):
        # -cv is the one that must write its own names to the file; the others
        # need a plain -c first, whose output would otherwise land there too.
        body = (f".output x.txt\n{script}\n" if key == "create.verbose"
                else f"{script}\n")
        for suffix, tail in (
            ("", " select line from lines where line is not null order by 1;"),
            (".count", " select count(*), count(distinct line) from lines"
                       " where line is not null;"),
        ):
            cases.append(_ext(f"archive.{key}{suffix}",
                              f"ext.archive.{key.split('.')[0]}",
                              f"{body}.output stdout\n{_lines('x.txt')}{tail}",
                              argv=(DB,), files=dict(_TREE)))

    # ``.archive -tv`` prints a mode string and an mtime.  The mtime is the
    # clock at run time, so it is normalised; the permission bits are the one
    # forced platform difference and are asserted as a literal in cases_platform.
    # What survives here is the column layout, the sizes and the ordering, and a
    # ``d`` in the first column for a directory -- all of which a listing has to
    # get right on any platform.
    cases.append(_ext("archive.list.verbose", "ext.archive.list",
                      ".archive -c src\n.archive -tv\n", argv=(DB,),
                      files=dict(_TREE), checks=("stderr", "exit")))

    # The listing's *layout* is portable even though two of its columns are not.
    # Write it to a file, split it back into rows, and assert the type character,
    # the right-aligned size field and the name -- skipping the nine permission
    # characters and the 19-character timestamp by offset.  Asserting by offset
    # is what makes this a claim about alignment: a listing whose columns moved
    # would fail here even if every value in it were right.
    split = _lines("l.txt")
    # Column offsets follow arListCommand()'s "%s % 10d  %s  %s": a 10-character
    # mode, one space, a 10-wide right-aligned size at 12, two spaces, the
    # 19-character datetime at 24, two spaces, the name at 45.
    for key, tail in (
        ("shape", "select substr(line,1,1), substr(line,12,10), substr(line,45)"
                  " from lines where line is not null order by 3;"),
        ("count", "select count(*), sum(substr(line,1,1)='d')"
                  " from lines where line is not null;"),
        ("width", "select distinct length(line)-length(substr(line,45))"
                  " from lines where line is not null;"),
        ("sizes", "select substr(line,45), cast(substr(line,12,10) as integer)"
                  " from lines where line is not null order by 1;"),
        ("stamp", "select distinct length(substr(line,24,19)),"
                  " substr(line,24,19) glob"
                  " '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]"
                  " [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'"
                  " from lines where line is not null;"),
    ):
        cases.append(_ext(f"archive.list.verbose.{key}", "ext.archive.list",
                          ".archive -c src\n.output l.txt\n.archive -tv\n"
                          f".output stdout\n{split}{tail}",
                          argv=(DB,), files=dict(_TREE)))

    for key, sql in (
        ("compress.zeros", "select hex(sqlar_compress(zeroblob(400)));"),
        ("compress.text", "select hex(sqlar_compress(cast("
                          "'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' as blob)));"),
        ("compress.short", "select hex(sqlar_compress(x'0102'));"),
        ("compress.empty", "select hex(sqlar_compress(x''));"),
        ("compress.null", "select quote(sqlar_compress(null));"),
        ("roundtrip.zeros", "select sqlar_uncompress(sqlar_compress(zeroblob(400)),400)"
                            "=zeroblob(400);"),
        ("roundtrip.text", "select cast(sqlar_uncompress(sqlar_compress("
                           "cast(replace(hex(zeroblob(80)),'00','ab') as blob)),160)"
                           " as text)=replace(hex(zeroblob(80)),'00','ab');"),
        ("uncompress.short", "select hex(sqlar_uncompress(x'0102',2));"),
        ("uncompress.badsize", "select hex(sqlar_uncompress("
                              "sqlar_compress(zeroblob(400)),4));"),
    ):
        cases.append(_ext(f"sqlar.{key}", f"ext.sqlar.{key.split('.')[0]}", sql))

    # zipfile() is (name, mode, mtime, data[, method]), and an omitted mtime
    # defaults to time(0) -- see zipfileGetTime().  Every case that looks at the
    # produced *bytes* therefore pins the mtime; the default-mtime path is still
    # covered, by the one case that asserts only what does not depend on it.
    zf = "zipfile('a.txt',420,1580000000,'hello')"
    for key, sql in (
        ("build", f"select hex({zf});"),
        ("build.dir", "select hex(zipfile('d',16877,1580000000,null));"),
        ("build.store", f"select hex(zipfile('a.txt',420,1580000000,'hello',0));"),
        ("build.deflate", "select hex(zipfile('a.txt',420,1580000000,"
                          "cast(replace(hex(zeroblob(60)),'00','ab') as blob),8));"),
        ("build.auto", "select length(zipfile('a.txt',420,1580000000,"
                       "cast(replace(hex(zeroblob(60)),'00','ab') as blob)))"
                       "<length(zipfile('a.txt',420,1580000000,"
                       "cast(replace(hex(zeroblob(60)),'00','ab') as blob),0));"),
        # The default-mtime form: its length is fixed even though four of its
        # bytes are not, so this asserts the shape without asserting the clock.
        ("build.nomtime", "select length(zipfile('a.txt','hello'))"
                          f"=length({zf});"),
        ("build.badmethod", "select hex(zipfile('a.txt',420,1580000000,'x',3));"),
        ("build.badname", "select hex(zipfile(null,420,1580000000,'x'));"),
        ("build.badargs", "select hex(zipfile('a.txt',420,'x'));"),
        ("agg.two", f"select hex(zipfile(name,420,1580000000,body)) from"
                    " (select 'a.txt' as name,'one' as body union all"
                    " select 'b.txt','two') order by name;"),
        ("read.back", f"create table z as select {zf} as f;"
                      "select name, cast(data as text), sz, method"
                      " from zipfile((select f from z));"),
        ("read.mode", f"create table z as select {zf} as f;"
                      f"select name, mode {TYPEBITS} from zipfile((select f from z));"),
        ("read.mtime", f"create table z as select {zf} as f;"
                       "select name, mtime from zipfile((select f from z));"),
        ("read.raw", f"create table z as select {zf} as f;"
                     "select name, hex(rawdata) from zipfile((select f from z));"),
        ("read.dir", "create table z as select zipfile('d',16877,1580000000,null) as f;"
                     f"select name, mode {TYPEBITS}, sz, data is null"
                     " from zipfile((select f from z));"),
        ("read.cds", f"create table z as select {zf} as f;"
                     "select length(zipfile_cds(zipfile))"
                     " from zipfile((select f from z));"),
        ("read.empty", "select count(*) from zipfile(x'504b0506"
                       "00000000000000000000000000000000');"),
        ("read.notzip", "select count(*) from zipfile(x'0102030405');"),
        # Out to the filesystem and back in: writefile() puts the archive on
        # disk, the vtab reads it from there.  Two extensions and the VFS in one
        # statement, which is the shape a stubbed port fails.
        ("read.file", f"select writefile('a.zip',{zf});"
                      "select name, cast(data as text), sz from zipfile('a.zip');"),
        ("read.file.missing", "select name from zipfile('nosuch.zip');"),
    ):
        cases.append(_ext(f"zipfile.{key}", f"ext.zipfile.{key.split('.')[0]}", sql,
                          argv=(DB,)))

    for key, sql in (
        ("one", "select name, mode " + TYPEBITS + " from fsdir('src/a.txt');"),
        ("dir", "select name from fsdir('src') order by name;"),
        ("recursive", "select name from fsdir('src','.') order by name;"),
        ("missing", "select name from fsdir('src/nosuch');"),
        ("count", "select count(*) from fsdir('src');"),
        ("data.null", "select name, data is null from fsdir('src') order by name;"),
    ):
        cases.append(_ext(f"fsdir.{key}", "ext.fsdir", sql, argv=(DB,),
                          files=dict(_TREE)))

    for key, sql in (
        ("readfile", "select cast(readfile('src/a.txt') as text);"),
        ("readfile.empty", "select length(readfile('src/empty.txt'));"),
        ("readfile.binary", "select length(readfile('src/random.bin'));"),
        ("readfile.missing", "select quote(readfile('src/nosuch'));"),
        ("writefile", "select writefile('out.txt','written');"
                      "select cast(readfile('out.txt') as text);"),
        ("writefile.mode", "select writefile('out2.txt','written',420);"
                           "select cast(readfile('out2.txt') as text);"),
        ("writefile.mkdir", "select writefile('a/b/c.txt','deep');"
                            "select cast(readfile('a/b/c.txt') as text);"),
        ("writefile.dir", "select writefile('newdir',null,16877);"
                          "select name from fsdir('newdir');"),
        ("writefile.roundtrip", "select writefile('copy.bin',"
                                "readfile('src/random.bin'));"
                                "select readfile('copy.bin')"
                                "=readfile('src/random.bin');"),
    ):
        cases.append(_ext(f"file.{key}", f"ext.file.{key.split('.')[0]}", sql,
                          argv=(DB,), files=dict(_TREE)))
    return cases


# --------------------------------------------------------------------------
# Modification times.
#
# WASI has no permission bits, but it does have path_filestat_set_times, and
# wasi-libc's utimes() lowers onto it -- so the mtime argument writefile() takes
# is one of the few pieces of file metadata a port can and must honour.  That
# matters beyond writefile: ``.archive`` stores mtimes, ``-x`` restores them, and
# ``-u``'s decision to re-archive a file is exactly
# ``mem.mtime=disk.mtime AND mem.mode=disk.mode`` (shell.c:14658).  A port that
# drops mtimes silently turns ``-u`` into ``-i``.
#
# Every stamp below is pinned by the case itself, so these are ordinary portable
# cases rather than anything needing a clock normaliser.  Note the mode half of
# that predicate compares the *stored* value with the *disk* value on the same
# platform, so it holds on both even though the numbers differ.
# --------------------------------------------------------------------------
#: 2020-01-26 00:53:20 UTC, and 100 seconds later.  Chosen near the 3.31.1
#: release date so a stray "now" is obvious when a case fails.
_T0 = 1580000000
_T1 = 1580000100

#: Build the tree from inside the run so every stamp is pinned.  The directory's
#: own stamp is set last: creating a file inside it bumps the directory's mtime,
#: so pinning the directory first would be undone by the files.
_LAY = (
    f"select writefile('m/a.txt','hello',420,{_T0});"
    f"select writefile('m/b.txt','second',420,{_T0});"
    f"select writefile('m',null,16877,{_T0});\n"
)


def _mtime() -> list[Case]:
    cases: list[Case] = []
    for key, sql in (
        ("set", f"select writefile('f.txt','x',420,{_T0});"
                "select name, mtime from fsdir('f.txt');"),
        ("set.dir", f"select writefile('d',null,16877,{_T0});"
                    "select name, mtime from fsdir('d');"),
        ("set.twice", f"select writefile('f.txt','x',420,{_T0});"
                      f"select writefile('f.txt','y',420,{_T1});"
                      "select name, mtime from fsdir('f.txt');"),
        ("set.epoch", "select writefile('f.txt','x',420,0);"
                      "select name, mtime from fsdir('f.txt');"),
        ("set.future", "select writefile('f.txt','x',420,2000000000);"
                       "select name, mtime from fsdir('f.txt');"),
        ("set.nomode", f"select writefile('f.txt','x',0,{_T0});"
                       "select name, mtime from fsdir('f.txt');"),
    ):
        cases.append(_ext(f"file.mtime.{key}", "ext.file.mtime", sql, argv=(DB,)))

    for key, script, tail in (
        ("store", ".archive -c m",
         "select name, mtime from sqlar order by name;"),
        ("extract", ".archive -c m\n.archive -x -C out",
         "select name, mtime from fsdir('out/m') order by name;"),
        ("roundtrip", ".archive -c m\n.archive -x -C out",
         "select (select mtime from fsdir('out/m/a.txt'))"
         "=(select mtime from sqlar where name='m/a.txt');"),
        # -u leaves a file alone when neither its stamp nor its mode moved, even
        # though its *contents* changed.  Upstream's own documented rule; it is
        # asserted here because a port with a broken mtime re-archives instead.
        ("update.nochange", ".archive -c m\n.archive -uv m",
         "select name, cast(sqlar_uncompress(data,sz) as text)"
         " from sqlar where sz>0 order by name;"),
        ("update.newer",
         f".archive -c m\nselect writefile('m/a.txt','hello',420,{_T1});\n"
         ".archive -uv m",
         "select name, mtime from sqlar order by name;"),
        ("update.samestamp",
         f".archive -c m\nselect writefile('m/a.txt','CHANGED',420,{_T0});\n"
         ".archive -uv m",
         "select name, cast(sqlar_uncompress(data,sz) as text)"
         " from sqlar where sz>0 order by name;"),
        ("insert.always",
         f".archive -c m\nselect writefile('m/a.txt','CHANGED',420,{_T0});\n"
         ".archive -iv m",
         "select name, cast(sqlar_uncompress(data,sz) as text)"
         " from sqlar where sz>0 order by name;"),
        ("zip.store", ".archive -c -f a.zip m",
         "select name, mtime from zipfile('a.zip') order by name;"),
        ("zip.extract", ".archive -c -f a.zip m\n.archive -x -f a.zip -C out",
         "select name, mtime from fsdir('out/m') order by name;"),
    ):
        # The verbose forms print names in readdir order; only the ordered SQL
        # tail is asserted, so the printed sequence is sent to a file.
        redirect = ".output x.txt\n" if "-uv" in script or "-iv" in script else ""
        cases.append(_ext(f"archive.mtime.{key}", "ext.archive.mtime",
                          f"{_LAY}{redirect}{script}\n.output stdout\n{tail}",
                          argv=(DB,)))

    # The listing renders the stored stamp through datetime(mtime,'unixepoch'),
    # so a pinned stamp makes the date column exact.  Sorted by name, because the
    # verbose listing scans the table rather than its index.
    cases.append(_ext(
        "archive.mtime.listing", "ext.archive.mtime",
        f"{_LAY}.archive -c m\n.output l.txt\n.archive -tv\n.output stdout\n"
        f"{_lines('l.txt')} select substr(line,45), substr(line,24,19)"
        " from lines where line is not null order by 1;",
        argv=(DB,)))
    return cases


# A database with enough shape that the page-level views have something to say:
# two tables of different widths, an index, an overflowing row and a freelist.
_PAGES = (
    "pragma page_size=4096;pragma journal_mode=delete;"
    "create table t(a integer primary key, b text);"
    "with recursive c(i) as (select 1 union all select i+1 from c where i<400)"
    " insert into t select i, 'row-'||i from c;"
    "create index ti on t(b);"
    "create table big(x); insert into big values(hex(zeroblob(9000)));"
    "create table gone(y); insert into gone values(1); drop table gone;"
)


def _introspect() -> list[Case]:
    """dbstat, dbpage, sqlite_stmt and the pragma-backed views.

    These read SQLite's own storage and its own prepared-statement list, so they
    are the closest thing the extension surface has to a structural assertion:
    if the port changed page layout or statement bookkeeping, this is where it
    shows.  Byte counts and page numbers are included deliberately -- they were
    measured equal, and a port that padded a page would fail here first.
    """
    cases: list[Case] = []

    def _page(key: str, operation: str, sql: str) -> Case:
        return _ext(key, operation, _PAGES + sql, argv=(DB,))

    for key, sql in (
        ("names", "select name from dbstat order by name;"),
        ("pages", "select name, path, pageno from dbstat order by pageno limit 40;"),
        ("payload", "select name, sum(payload), sum(unused), count(*) from dbstat"
                    " group by name order by name;"),
        ("ncell", "select name, path, ncell, mx_payload from dbstat"
                  " order by pageno limit 30;"),
        ("pagetype", "select pagetype, count(*) from dbstat group by 1 order by 1;"),
        ("aggregate", "select sum(pgsize) from dbstat;"),
        ("agg.mode", "select name, pageno, pagetype, ncell, payload, unused, pgsize"
                     " from dbstat('main',1) order by name;"),
        ("one.table", "select path, pagetype, ncell from dbstat"
                      " where name='t' order by pageno limit 20;"),
        ("overflow", "select pagetype, count(*) from dbstat where name='big'"
                     " group by 1 order by 1;"),
        ("index", "select count(*) from dbstat where name='ti';"),
    ):
        cases.append(_page(f"dbstat.{key}", "ext.dbstat", sql))

    for key, sql in (
        ("header", "select hex(substr(data,1,32)) from dbpage where pgno=1;"),
        ("count", "select count(*) from dbpage;"),
        ("sizes", "select pgno, length(data) from dbpage order by pgno limit 20;"),
        ("schema", "select pgno from dbpage('main') order by pgno limit 5;"),
        ("digest", "select pgno, hex(substr(data,1,8)) from dbpage"
                   " order by pgno limit 20;"),
        ("write", "update dbpage set data=data where pgno=2;pragma audit_check;"),
    ):
        cases.append(_page(f"dbpage.{key}", "ext.dbpage", sql))

    for key, sql in (
        ("empty", "select count(*) from sqlite_stmt;"),
        ("self", "select sql from sqlite_stmt;"),
        ("counters", "select ncol, ro, busy, nscan, nsort, naidx from sqlite_stmt;"),
        ("prepared", "select ncol, ro from sqlite_stmt where sql like 'select%';"),
    ):
        cases.append(_page(f"stmt.{key}", "ext.stmt", sql))
    return cases


def _deserialize() -> list[Case]:
    """The in-memory database surface: --deserialize, --hexdb, --append, memdb.

    A serialised database is the file format in a malloc'd buffer, so this is
    where a port that got pointer width or buffer ownership wrong shows up while
    the on-disk path still looks fine.
    """
    cases: list[Case] = []
    fx = {"f.db": seeddb.FIXTURE_HEX}

    def _des(key: str, operation: str, sql: str,
             argv: tuple[str, ...] = (), **kw) -> Case:
        return _ext(key, operation, sql, argv=argv, binfiles=dict(fx), **kw)

    for key, argv, sql in (
        ("open", (), ".open --deserialize f.db\nselect * from t order by a;"),
        ("flag", ("--deserialize", "f.db"), "select * from t order by a;"),
        ("schema", (), ".open --deserialize f.db\n.schema"),
        ("types", (), ".open --deserialize f.db\n"
                      "select quote(x), quote(y) from u;"),
        ("write", (), ".open --deserialize f.db\ninsert into t values(4,'four');"
                      "select count(*) from t;"),
        ("nopersist", (), ".open --deserialize f.db\ninsert into t values(4,'four');"
                          ".open f.db\nselect count(*) from t;"),
        ("ddl", (), ".open --deserialize f.db\ncreate table v(z);"
                    "insert into v values(9);select * from v;"),
        ("delete", (), ".open --deserialize f.db\ndelete from t where a=2;"
                       "select a from t order by a;"),
        ("vacuum", (), ".open --deserialize f.db\nvacuum;"
                       "select count(*) from t;pragma page_count;"),
        ("audit", (), ".open --deserialize f.db\npragma audit_check;"),
        ("pagesize", (), ".open --deserialize f.db\npragma page_size;"),
        ("pagecount", (), ".open --deserialize f.db\npragma page_count;"),
        ("journal", (), ".open --deserialize f.db\npragma journal_mode;"),
        ("wal", (), ".open --deserialize f.db\npragma journal_mode=wal;"),
        ("dbstat", (), ".open --deserialize f.db\n"
                       "select name, pagetype from dbstat order by pageno;"),
        ("dbpage", (), ".open --deserialize f.db\n"
                       "select pgno, hex(substr(data,1,16)) from dbpage"
                       " order by pgno;"),
        ("dblist", (), ".open --deserialize f.db\npragma database_list;"),
        ("index.used", (), ".open --deserialize f.db\n"
                           "select a from t where b='two';"),
        ("maxsize", (), ".open --deserialize --maxsize 65536 f.db\n"
                        "select count(*) from t;"),
        # Growing past --maxsize is the one place the deserialised path can
        # fail on its own terms, and it is worth an assertion: the buffer is
        # reallocated by the port's own allocator, and a 32-bit size_t handling
        # bug would show up as either a wrong error or no error at all.
        ("maxsize.hit", (), ".open --deserialize --maxsize 8192 f.db\n"
                            "with recursive c(i) as (select 1 union all"
                            " select i+1 from c where i<2000)"
                            " insert into t select i+100, hex(zeroblob(60)) from c;"
                            "select count(*) from t;"),
        ("backup", (), ".open --deserialize f.db\n.backup copy.db\n"
                       ".open copy.db\nselect a from t order by a;"),
        ("save", (), ".open --deserialize f.db\ninsert into t values(4,'four');"
                     ".save saved.db\n.open saved.db\nselect count(*) from t;"),
        ("dump", (), ".open --deserialize f.db\n.dump t"),
        ("readonly", (), ".open --readonly f.db\nselect count(*) from t;"),
        ("readonly.write", (), ".open --readonly f.db\ninsert into t values(4,'x');"),
        ("nofollow", (), ".open --nofollow f.db\nselect count(*) from t;"),
    ):
        cases.append(_des(f"deser.{key}", f"ext.deser.{key.split('.')[0]}",
                          sql, argv=argv))

    # ``--hexdb`` parses the text form of a database image.  The text is derived
    # from the very bytes laid down as f.db, so a case that reads both and
    # compares them is asserting the two paths into sqlite3_deserialize agree.
    hexdb = seeddb.hexdb_text()
    for key, sql in (
        ("hexdb", ".open --hexdb h.txt\nselect * from t order by a;"),
        ("hexdb.schema", ".open --hexdb h.txt\n.schema"),
        ("hexdb.audit", ".open --hexdb h.txt\npragma audit_check;"),
        ("hexdb.pagecount", ".open --hexdb h.txt\npragma page_count;"),
        ("hexdb.write", ".open --hexdb h.txt\ninsert into t values(4,'four');"
                        "select count(*) from t;"),
        ("hexdb.truncated", ".open --hexdb short.txt\nselect * from t;"),
        ("hexdb.garbage", ".open --hexdb junk.txt\nselect * from t;"),
        ("hexdb.missing", ".open --hexdb nosuch.txt\nselect * from t;"),
    ):
        cases.append(_ext(f"deser.{key}", "ext.deser.hexdb", sql,
                          argv=(":memory:",),
                          files={
                              "h.txt": hexdb,
                              # First page only: a real prefix, so the failure is
                              # "this is not a whole database" rather than "this
                              # is not the format".
                              "short.txt": "".join(
                                  hexdb.splitlines(True)[:34]) + "| end x.db\n",
                              "junk.txt": "not a hexdb dump at all\n",
                          }))

    # The append VFS puts a database at the end of a file that already has other
    # content, and finds it again from a trailer.  Every offset the VFS computes
    # is relative to that start, so it is a direct test of the port's 64-bit
    # offset arithmetic on a 32-bit target.
    prefix = "#!/bin/sh\n# a host file with a database appended to it\nexit 0\n"
    for key, sql, files in (
        ("append.create", "create table t(a,b);insert into t values(1,'x');"
                          "select * from t;", {}),
        ("append.pagesize", "create table t(a);pragma page_size;", {}),
        ("append.reopen", "create table t(a);insert into t values(7);"
                          ".open --append ap.db\nselect * from t;", {}),
        ("append.onhost", "create table t(a);insert into t values(1);"
                          "select * from t;", {"ap.db": prefix}),
        ("append.prefix.kept", "create table t(a);"
                               "select cast(substr(readfile('ap.db'),1,9) as text);",
         {"ap.db": prefix}),
        ("append.audit", "create table t(a);insert into t values(1);"
                             "pragma audit_check;", {}),
        ("append.vacuum", "create table t(a);insert into t values(1);vacuum;"
                          "select * from t;", {}),
        ("append.grow", "create table t(a,b);"
                        "with recursive c(i) as (select 1 union all"
                        " select i+1 from c where i<800)"
                        " insert into t select i, hex(zeroblob(50)) from c;"
                        "select count(*), sum(a) from t;", {}),
        ("append.journal", "create table t(a);pragma journal_mode;", {}),
    ):
        cases.append(_ext(f"deser.{key}", "ext.deser.append", sql,
                          argv=("--append", "ap.db"), files=dict(files)))

    # ``pragma database_list`` prints what xFullPathname made of the name the
    # database was opened with, and on WASI a relative name stays relative:
    # there is no cwd in the ABI to resolve it against, only preopens.  That is a
    # platform property, asserted as a literal in cases_platform.  Opening by an
    # absolute path gives both sides the same answer, so the portable claim --
    # that the append VFS reports its own file under seq 0 and name "main" -- is
    # made here.
    cases.append(_ext("deser.append.dblist", "ext.deser.append",
                      "create table t(a);pragma database_list;",
                      argv=("--append", f"{GUEST_DATA}/ap.db")))
    cases.append(_ext("deser.append.dblist.attach", "ext.deser.append",
                      f"create table t(a);attach '{GUEST_DATA}/two.db' as two;"
                      "create table two.s(b);pragma database_list;",
                      argv=("--append", f"{GUEST_DATA}/ap.db")))

    for key, sql in (
        ("memdb.crud", "create table t(a);insert into t values(1),(2);"
                       "select sum(a) from t;"),
        ("memdb.dblist", "pragma database_list;"),
        ("memdb.journal", "pragma journal_mode;"),
        ("memdb.pagesize", "pragma page_size;"),
        ("memdb.temp", "create temp table x(a);insert into x values(1);"
                       "select count(*) from sqlite_temp_master;"),
        ("memdb.audit", "create table t(a);pragma audit_check;"),
        ("memdb.big", "create table t(a,b);"
                      "with recursive c(i) as (select 1 union all"
                      " select i+1 from c where i<1500)"
                      " insert into t select i, hex(zeroblob(40)) from c;"
                      "select count(*) from t;pragma page_count;"),
        ("memdb.vacuum", "create table t(a);insert into t values(1);vacuum;"
                         "select * from t;"),
    ):
        cases.append(_ext(f"deser.{key}", "ext.deser.memdb", sql,
                          argv=("-vfs", "memdb", "m.db")))
    return cases


# A JSON document with every type the parser distinguishes, some nesting, and
# text that needs escaping.
_JSON = (r'{"a":1,"b":2.5,"c":"str","d":null,"e":true,"f":false,'
         r'"g":[1,2,[3,4]],"h":{"i":{"j":"deep"}},'
         r'"k":"quote\" back\\ tab\t nl\n","l":[],"m":{},"n":1e400}')


def _json() -> list[Case]:
    """JSON1.  In the release feature set twice over: OPT_FEATURE_FLAGS and SHELL_OPT.

    Included because it is the largest pure-computation extension in the build,
    and because its number formatting goes through the same printf the port has
    to supply -- a float rendered one digit differently would show up here.
    """
    doc = _JSON.replace("'", "''")
    cases: list[Case] = []
    for key, expr in (
        ("json", f"select json('{doc}');"),
        ("valid", f"select json_valid('{doc}'), json_valid('{{bad');"),
        ("type", f"select json_type('{doc}'), json_type('{doc}','$.b'),"
                 f" json_type('{doc}','$.g'), json_type('{doc}','$.d');"),
        ("extract.scalar", f"select json_extract('{doc}','$.a'),"
                           f" json_extract('{doc}','$.c');"),
        ("extract.real", f"select json_extract('{doc}','$.b');"),
        ("extract.bool", f"select json_extract('{doc}','$.e'),"
                         f" json_extract('{doc}','$.f');"),
        ("extract.null", f"select quote(json_extract('{doc}','$.d'));"),
        ("extract.deep", f"select json_extract('{doc}','$.h.i.j');"),
        ("extract.array", f"select json_extract('{doc}','$.g[2][1]');"),
        ("extract.multi", f"select json_extract('{doc}','$.a','$.c');"),
        ("extract.missing", f"select quote(json_extract('{doc}','$.zz'));"),
        ("extract.escapes", f"select json_extract('{doc}','$.k');"),
        ("extract.bignum", f"select json_extract('{doc}','$.n');"),
        ("array.len", f"select json_array_length('{doc}','$.g'),"
                      f" json_array_length('{doc}','$.l');"),
        ("array", "select json_array(1,2.5,'x',null,json('[7]'));"),
        ("object", "select json_object('a',1,'b',json_array(2,3));"),
        ("insert", f"select json_insert('{doc}','$.new',5);"),
        ("replace", f"select json_replace('{doc}','$.a',99);"),
        ("set", f"select json_set('{doc}','$.a',99,'$.new','v');"),
        ("remove", f"select json_remove('{doc}','$.g','$.h');"),
        ("patch", f"select json_patch('{doc}','{{\"a\":9,\"d\":1}}');"),
        ("quote", "select json_quote('a\"b'), json_quote(2.5), json_quote(null);"),
        ("group.array", "select json_group_array(value) from json_each('[3,1,2]');"),
        ("group.object", "select json_group_object(key,value) from"
                         " json_each('{\"x\":1,\"y\":2}');"),
        ("each", f"select key, type, atom, value, id, parent, fullkey, path"
                 f" from json_each('{doc}');"),
        ("each.path", f"select key, type from json_each('{doc}','$.h') order by key;"),
        ("each.array", "select key, value from json_each('[10,20,30]');"),
        ("tree", f"select fullkey, type, atom from json_tree('{doc}')"
                 f" order by fullkey;"),
        ("tree.path", f"select fullkey, type from json_tree('{doc}','$.g')"
                      f" order by fullkey;"),
        ("tree.count", f"select count(*), sum(type='integer') from json_tree('{doc}');"),
        ("errors.badpath", f"select json_extract('{doc}','bad');"),
        ("errors.badjson", "select json_extract('{','$.a');"),
        ("errors.badargs", "select json_object('a');"),
        ("roundtrip", f"select json('{doc}')=json(json('{doc}'));"),
        ("minify", "select json(' { \"a\" : [ 1 , 2 ] } ');"),
        ("unicode", "select json_extract('{\"k\":\"caf\\u00e9\"}','$.k');"),
        ("in.table", "create table j(d);"
                     f"insert into j values('{doc}'),('[1,2,3]'),('7');"
                     "select json_type(d), json_valid(d) from j;"),
        ("join", "create table j(id integer primary key, d);"
                 "insert into j values(1,'[1,2]'),(2,'[3]');"
                 "select j.id, e.value from j, json_each(j.d) e order by 1,2;"),
        ("index", "create table j(d);"
                  "insert into j values('{\"n\":1}'),('{\"n\":2}');"
                  "create index ji on j(json_extract(d,'$.n'));"
                  "select json_extract(d,'$.n') from j"
                  " where json_extract(d,'$.n')=2;"),
    ):
        cases.append(_ext(f"json.{key}", f"ext.json.{key.split('.')[0]}", expr))
    return cases


def _misc() -> list[Case]:
    """The rest of SHELL_OPT: offset(), completion, expert, recover, unknown fns."""
    cases: list[Case] = []
    base = ("create table t(a integer primary key, b text, c);"
            "insert into t values(1,'x',null),(2,'y',3.5),(3,'z',x'01');")
    for key, sql in (
        ("offset.rowid", base + "select a, sqlite_offset(a) is not null from t;"),
        ("offset.col", base + "select b, sqlite_offset(b)=sqlite_offset(c) from t;"),
        ("offset.order", base + "select count(distinct sqlite_offset(b)) from t;"),
        ("offset.index", base + "create index ti on t(b);"
                                "select sqlite_offset(b) is null from t"
                                " where b='y';"),
        ("offset.expr", base + "select quote(sqlite_offset(a+1)) from t limit 1;"),
    ):
        cases.append(_ext(f"misc.{key}", "ext.offset", sql, argv=(DB,)))

    for key, prefix in (
        ("empty", ""),
        ("sel", "SEL"),
        ("pragma", "PRAGMA jour"),
        ("dot", "sqlite_"),
        ("func", "jso"),
        ("table", "t"),
        ("keyword", "CREA"),
        ("nomatch", "zzzzz"),
    ):
        cases.append(_ext(f"misc.completion.{key}", "ext.completion",
                          base + "select candidate from completion"
                                 f"('{prefix}') order by 1;", argv=(DB,)))

    for key, sql in (
        ("unknown.fn", "select nosuchfunction(1);"),
        ("unknown.fn.explain", "explain query plan select nosuchfunction(1);"),
        ("unknown.fn.eqp", ".eqp on\nselect * from sqlite_master"
                           " where nosuchfunction(name);"),
    ):
        cases.append(_ext(f"misc.{key}", "ext.unknownfn", sql,
                          normalisers=("eqp",)))

    # .expert reads the schema and proposes indexes; it links the whole query
    # planner and the ANALYZE machinery through a second entry point.
    for key, sql in (
        ("expert.simple", ".expert\nselect * from t where b='y';"),
        ("expert.two", ".expert\nselect * from t where b='y' and c>1;"),
        ("expert.join", "create table s(k,v);.expert\n"
                        "select * from t join s on t.a=s.k where s.v=3;"),
        ("expert.order", ".expert\nselect * from t order by b;"),
        ("expert.verbose", ".expert -verbose\nselect * from t where b='y';"),
        ("expert.sample", ".expert -sample 100\nselect * from t where b='y';"),
    ):
        cases.append(_ext(f"misc.{key}", "ext.expert", base + sql, argv=(DB,)))

    for key, sql in (
        ("recover", ".recover"),
        ("recover.freelist", ".recover --freelist-corrupt"),
        ("recover.rowids", ".recover --no-rowids"),
        ("recover.lostfound", ".recover --lost-and-found lf"),
    ):
        cases.append(_ext(f"misc.{key}", "ext.recover", base + sql, argv=(DB,)))

    # The registered virtual-table modules, as a set.  This is the feature list
    # of the build restated at run time: every module here comes from a flag in
    # OPT_FEATURE_FLAGS or SHELL_OPT, so a port that quietly dropped one -- the
    # cheap way to make a hard extension "pass" -- fails on this one row.
    # Measured identical on both platforms; nothing here needs an OS.
    cases.append(_ext("misc.module.list", "ext.module_list",
                      "select name from pragma_module_list order by 1;"))
    cases.append(_ext("misc.module.count", "ext.module_list",
                      "select count(*) from pragma_module_list;"))

    # There is deliberately no `readfile()`-on-a-directory case.  It looked like a
    # portable error path, and both targets do fail -- but probe_fs showed the
    # outcome is decided by the *host filesystem*, not by the port: readfile()
    # allocates st_size bytes and reads that many, and a directory's st_size is
    # 4096 on ext4 (the read fails, exit 1, no output) but ~60 on tmpfs (the read
    # returns an empty blob, exit 0).  Recording either answer would fail in the
    # verifier container for a reason that has nothing to do with wasm.  What
    # upstream actually promises -- an unreadable path yields NULL rather than an
    # error -- is covered by `file.readfile.missing`, with no filesystem in the way.
    return cases


def build() -> list[Case]:
    cases: list[Case] = []
    for part in (_fts4, _fts5, _rtree, _archive, _mtime, _introspect,
                 _deserialize, _json, _misc):
        cases.extend(part())
    return cases
