#!/bin/bash
# Build native sqlite3 3.31.1 from a State A tree.
#
#   build-native-oracle.sh <source-tree> <output-binary>
#
# Two callers, one recipe.
#
# In the agent image this is a convenience: a reference build of the same source, so a
# port can be compared against the behaviour it is supposed to preserve without having
# to guess what that behaviour was.
#
# In the verifier image it builds the reference the port is scored against.  The
# behavioural stage does not carry written-down expectations; it runs this binary and the
# submission's wasm module side by side on the same input and compares what came out.
# So this script is the definition of "the behaviour to preserve", and it is deliberately
# the same file in both places -- a reference built by a recipe that differs from the
# documented one is a reference nobody can reproduce, and an agent that cannot reproduce
# it cannot check its own work.
#
# It runs at image build time in both images, never against a submission.  A compiler
# that has stopped working therefore fails a `docker build`, where the message says so,
# instead of producing a stage where every comparison fails and the result looks exactly
# like a port that was never attempted.
#
# The tree must still have src/os_unix.c: this is the *pre-migration* build, and it is
# the only thing here that still needs the POSIX layer.
#
# On SQLITE_SOURCE_ID
# -------------------
# `tool/mksourceid` replaces the last four hex digits of the id with "alt1" when any
# file named in `manifest` is missing or altered.  It is one fixed suffix regardless of
# what moved or how much.
#
# The State A payload drops three files that `manifest` still names
# (.fossil-settings/empty-dirs, .fossil-settings/ignore-glob and ext/lsm1/Makefile: the
# first two are version-control configuration and cannot ship in a tree the agent reads,
# the third is a build file for an extension the amalgamation does not include), so a
# native build from it reports ...837balt1.
#
# A correct port reports the same string for a different reason: deleting src/os_unix.c
# also fails the inventory.  That coincidence is what lets the suite *measure* the two
# source-id expectations against this binary rather than write them down -- and writing
# them down is the version that goes wrong, because a constant cannot tell the difference
# between a port that regenerated the manifest and one that did not.
#
# The date and the leading 60 hex digits must match either way, and that is the check
# that actually establishes "this is 3.31.1".  A canonical tail is reported rather than
# rejected: it means the caller passed a tree that is not the shipped payload, which is
# fine for a local experiment and is something a grading image asserts for itself.
set -euo pipefail

SRC="${1:?usage: build-native-oracle.sh <source-tree> <output-binary>}"
OUT="${2:?usage: build-native-oracle.sh <source-tree> <output-binary>}"

[ -f "$SRC/src/os_unix.c" ] \
  || { echo "FATAL: $SRC has no src/os_unix.c -- this recipe builds the pre-migration" \
            "native binary and needs the POSIX layer it was written against" >&2; exit 1; }

# Resolved before the build moves to a scratch directory, so both arguments may be given
# relative to wherever the caller is standing.
SRC=$(cd "$SRC" && pwd)
OUT=$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
cd "$WORK"

"$SRC/configure" --disable-tcl --disable-readline >configure.log 2>&1 \
  || { echo "FATAL: configure failed" >&2; tail -20 configure.log >&2; exit 1; }
make sqlite3.c shell.c >generate.log 2>&1 \
  || { echo "FATAL: could not generate the amalgamation" >&2; tail -30 generate.log >&2; exit 1; }

# The feature set the pre-migration build shipped with.  Every flag here is one the port
# is expected to carry too, because this binary is the definition of the behaviour being
# preserved: a feature this build does not have is a feature the corpus cannot ask about.
#
# The list is in two halves, and the split is not cosmetic.
#
# The first eight are reported by `pragma compile_options` (src/ctime.c has an entry for
# each), so they are visible in a scored observation and both builds must agree on them.
#
# The last five are *not* reported by 3.31.1's ctime.c -- there is no entry for
# ENABLE_DESERIALIZE, ENABLE_DBPAGE_VTAB, ENABLE_OFFSET_SQL_FUNC, ENABLE_EXPLAIN_COMMENTS
# or HAVE_ZLIB -- so leaving them out would change nothing in the option list.  That is
# the reason they are easy to omit and the reason omitting them is wrong: what they
# change is behaviour, and the direction it fails in is the one that quietly empties
# the corpus.
#
# Four of them decide whether a feature exists at all.  Counted from the catalog, the
# cases that ask about one (`family`/`key` naming the feature):
#
#   HAVE_ZLIB              .archive, sqlar, zipfile          73 cases (71 differential)
#   ENABLE_DESERIALIZE     --deserialize, --hexdb, memdb      53 cases
#   ENABLE_DBPAGE_VTAB     the dbpage virtual table            7 cases
#   ENABLE_OFFSET_SQL_FUNC sqlite_offset()                     7 cases
#                                                            --- 139 distinct cases
#
# Without the flag this binary answers "Error: unknown command" or "no such table" to
# the 137 differential ones; a submission that also omitted it answers the same thing;
# and every one of those cases passes while asserting nothing.  A port that dropped
# `.archive` entirely would score full marks on 41 archive cases.  The remaining
# two are worse than uninformative: plat.mode.sqlar.extract and plat.mode.sqlar.store
# are recorded literals, so without zlib the required answer is unreachable rather
# than merely unchecked -- as is `memdb` in plat.vfs.list, which pins `.vfslist` to
# `wasi, apndvfs, memdb` and which only the deserialize flag registers.
#
# ENABLE_EXPLAIN_COMMENTS is the fifth and is different in kind: all 56 explain cases
# are differential, so omitting it on both sides fails nothing.  It empties them
# instead -- the comment column is where the opcode's operands are spelled out, so
# without it the two builds are compared on the bare opcode stream and a port that
# mangled the operands would agree anyway.
#
# THREADSAFE is left at its default of 1.  It is one of the two options that legitimately
# differ between the two builds -- WASI has no pthreads -- and the corpus knows that.
#
# Into a log rather than onto the console: 3.31.1 compiles with a handful of warnings
# under gcc 12 (a -Wreturn-local-addr in sqlite3SelectNew, a -Wstringop-overflow in
# fts5), all upstream's own and none of them new.  Twenty lines of known noise in front
# of the provenance line is how the provenance line stops being read.
if ! gcc -O2 -o "$OUT" shell.c sqlite3.c \
    -DSQLITE_ENABLE_DBSTAT_VTAB \
    -DSQLITE_ENABLE_FTS4 \
    -DSQLITE_ENABLE_FTS5 \
    -DSQLITE_ENABLE_JSON1 \
    -DSQLITE_ENABLE_RTREE \
    -DSQLITE_ENABLE_STMTVTAB \
    -DSQLITE_ENABLE_UNKNOWN_SQL_FUNCTION \
    -DHAVE_ISNAN \
    -DSQLITE_HAVE_ZLIB \
    -DSQLITE_ENABLE_DESERIALIZE \
    -DSQLITE_ENABLE_DBPAGE_VTAB \
    -DSQLITE_ENABLE_OFFSET_SQL_FUNC \
    -DSQLITE_ENABLE_EXPLAIN_COMMENTS \
    -lm -lpthread -ldl -lz >compile.log 2>&1; then
  echo "FATAL: the native build failed" >&2
  tail -30 compile.log >&2
  exit 1
fi

# The source id establishes what was built.  Both tails are accepted -- see the header --
# and both require the date and the leading 60 hex digits, which is what makes this
# 3.31.1 rather than some other checkout.  Anything else is a different SQLite and the
# build fails here rather than becoming a reference that silently disagrees.
CANONICAL="2020-01-27 19:55:54 3bfa9cc97da10598521b342961df8f5f68c7388fa117345eeb516eaa837bb4d6"
ALTERED="2020-01-27 19:55:54 3bfa9cc97da10598521b342961df8f5f68c7388fa117345eeb516eaa837balt1"
actual=$("$OUT" :memory: "select sqlite_source_id();")

if [ "$actual" = "$CANONICAL" ]; then
  provenance="canonical source id -- the tree passed is not the shipped State A payload"
elif [ "$actual" = "$ALTERED" ]; then
  provenance="altered inventory (alt1) -- expected when building from the State A payload"
else
  echo "FATAL: built binary is not 3.31.1 at all." >&2
  echo "  expected: $CANONICAL" >&2
  echo "        or: $ALTERED" >&2
  echo "  actual:   $actual" >&2
  exit 1
fi

# --- the five invisible flags actually took ------------------------------------------
# Everything above is a flag on a command line, and four of the five below cannot be seen
# in `pragma compile_options` -- which is the whole reason they went missing once.  A typo
# in a -D, a dropped -lz, a zlib1g-dev that fell out of the image's package list: each
# silently returns this binary to the state where 139 cases compare two failures, and
# nothing downstream notices, because a differential case whose two sides agree on
# "unknown command" *passes*.  So the recipe asks the binary, in the same run that built
# it, rather than trusting the command line it just used.
#
# One probe per flag, each phrased so a missing feature is an error rather than a
# different answer.  These are assertions about the reference, not expectations for a
# submission: the corpus never reads them.
probe() {
    what=$1 want=$2 flag=$3
    shift 3
    got=$("$OUT" "$@" 2>&1) || got="<exit $?> $got"
    [ "$got" = "$want" ] && return 0
    echo "FATAL: the reference build cannot $what." >&2
    echo "  expected: $want" >&2
    echo "  actual:   $got" >&2
    echo "  cause:    $flag did not reach this binary, or its dependency is missing" >&2
    echo "            from the image.  Fix the build; do not relax this check --" >&2
    echo "            without the feature the cases that ask about it compare two" >&2
    echo "            failures and pass while asserting nothing." >&2
    exit 1
}

"$OUT" probe.db 'create table t(x); insert into t values(1);' >/dev/null

probe "compress with zlib" 1 SQLITE_HAVE_ZLIB \
      probe.db 'select length(sqlar_compress(zeroblob(500))) > 0;'
probe "read its own pages"  2 SQLITE_ENABLE_DBPAGE_VTAB \
      probe.db 'select count(*) from sqlite_dbpage;'
probe "report row offsets"  1 SQLITE_ENABLE_OFFSET_SQL_FUNC \
      probe.db 'select sqlite_offset(x) is not null from t;'

# The last two print a block or a table rather than one value, so they match instead of
# comparing: .vfslist prints a stanza per VFS, and the explain header carries a `comment`
# column only under ENABLE_EXPLAIN_COMMENTS.
"$OUT" probe.db '.vfslist' | grep -q '"memdb"' || {
    echo "FATAL: the reference build has no memdb VFS, so SQLITE_ENABLE_DESERIALIZE did" >&2
    echo "  not reach it.  53 cases ask about --deserialize, --hexdb or memdb, and" >&2
    echo "  cases_platform pins .vfslist to 'wasi, apndvfs, memdb' for the port -- an" >&2
    echo "  expectation the reference itself would fail." >&2
    exit 1
}
# Not the header: shell.c prints `comment` as a column name whether or not the library
# fills it in, so matching the header is a probe that passes on both builds -- it was
# written that way once and asserted nothing, which is the same mistake one layer up.
# The content is what the flag decides, so the probe reads the Init opcode's comment.
"$OUT" probe.db 'explain select 1;' | grep -q 'Start at' || {
    echo "FATAL: explain output has an empty comment column, so" >&2
    echo "  SQLITE_ENABLE_EXPLAIN_COMMENTS did not reach this binary.  All 56 explain" >&2
    echo "  cases are differential, so this fails nothing -- it empties them: the two" >&2
    echo "  builds would be compared on the bare opcode stream, with the operands that" >&2
    echo "  a mangled port would get wrong left blank on both sides." >&2
    exit 1
}
rm -f probe.db

echo "native sqlite3 -> $OUT"
echo "  version    $("$OUT" --version)"
echo "  source id  $actual"
echo "  provenance $provenance"
echo "  options    $("$OUT" :memory: 'pragma compile_options;' | tr '\n' ' ')"
echo "  features   zlib dbpage offset memdb explain-comments (probed, not assumed)"
