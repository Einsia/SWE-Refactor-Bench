"""Normalisers, one per named and justified reason.

A normaliser deletes information from a comparison, so each one is a hole in the
contract and has to earn its place.  The rule inherited from an earlier port task: a difference
gets normalised only when the source *leaves it to the port*, and gets asserted
as a literal when the source *pins* it.  Anything that is merely inconvenient
stays in the comparison and the port has to match it.

Every entry below cites the line in SQLite 3.31.1 that produces the difference.
"""

from __future__ import annotations

import math
import re
from decimal import Context, Decimal, ROUND_HALF_EVEN

# --------------------------------------------------------------------------
# eqp: `explain query plan` prints a tree whose node ids are counters over the
# parse, and 3.31 prints them as `--SCAN TABLE t` under a `QUERY PLAN` root.
# The *shape* is the contract; the leading id columns are an implementation
# detail that a rebuild can renumber.  We keep the text and drop the ids.
# --------------------------------------------------------------------------
_EQP_ID = re.compile(r"^\s*\d+\|\d+\|\d+\|", re.M)


def eqp(text: str) -> str:
    return _EQP_ID.sub("", text)


# --------------------------------------------------------------------------
# longdouble: two sites render a double with more digits than a double has.
#
#   sqlite3.c:116678-116682  quote() tries "%!.15g" and falls back to "%!.20e"
#                            when the 15-digit form does not parse back equal.
#   shell.c:10717            `.mode quote` uses "%!.20g" unconditionally.
#
# Both go through LONGDOUBLE_TYPE (sqlite3.c:14213 -> `long double`), whose
# significand is 64 bits on x86-64 and 113 bits on wasm32.  The "!" flag asks for
# 26 significant digits and then strips trailing zeros (sqlite3.c:28548, 28577),
# so the hosts disagree past digit 17 *and* disagree on how many characters
# survive the strip.  Measured, both shapes, over 44 divergent expressions:
#
#   0.1+0.2               nat 0.20000000000000001110  wsm 0.2000000000000000111
#   9007199254740993.0    nat 9007199254740991.9999   wsm 9007199254740992.0
#   9.9999999999999694e-311  nat ...4515e-311  wsm ...4493e-311
#
# Quantising the rendered text to 17 digits is the obvious repair and is wrong
# twice over: it cannot reconcile two strings of different length, and quantising
# an already-diverged digit string is unstable at the boundary -- the third pair
# above rounds to ...695 and ...694.  Take the digits from the *value* instead:
# every one of the 44 divergent pairs satisfies float(nat) == float(wsm), because
# both are correctly-rounded renderings of the same double and 20 digits is far
# more than the 17 needed to name one uniquely.
#
# So: any real token carrying more than 17 significant digits is re-rendered from
# the double it parses to, at 17 digits, keeping the shape it arrived in
# (exponent or not) so that the two output modes stay distinguishable.  A
# rendering of 17 or fewer digits is left exactly as it is, which is every
# ordinary `%!.15g` result and therefore the overwhelming majority of the corpus.
# The round-trip property this discards is asserted positively and directly by
# the `real.roundtrip.*` cases in cases_engine.
# --------------------------------------------------------------------------
_REAL = re.compile(r"(?<![\w.])(-?\d+\.\d+)(?:e([+-]\d+))?(?![\w.])", re.I)

#: 17 significant digits is the most a binary64 needs to be named uniquely.
_D17 = Context(prec=17, rounding=ROUND_HALF_EVEN)


def _significant(digits: str) -> int:
    return len(digits.replace(".", "").lstrip("0"))


def _canonical(m: re.Match[str]) -> str:
    mantissa, exp = m.group(1), m.group(2)
    if _significant(mantissa) <= 17:
        return m.group(0)
    try:
        value = float(m.group(0))
    except (ValueError, OverflowError):  # pragma: no cover - regex excludes it
        return m.group(0)
    if not math.isfinite(value):  # pragma: no cover - needs a 1e999 literal
        return m.group(0)
    d = _D17.create_decimal(repr(value))
    if exp is None:
        # Plain shape: `%!.20g` chose no exponent, so keep it that way.  The "!"
        # flag guarantees at least one digit after the point (sqlite3.c:28581), so
        # a value whose 17-digit form is integral still ends in ".0" -- both when
        # the strip eats the whole fraction and when 17 digits do not reach the
        # point at all (9223372036854775807.8 -> 9223372036854776000.0).
        out = format(d, "f")
        if "." not in out:
            return out + ".0"
        out = out.rstrip("0")
        return out + "0" if out.endswith(".") else out
    # Exponent shape: 16 digits after the point is 17 significant.
    text = f"{value:.16e}"
    head, _, tail = text.partition("e")
    head = head.rstrip("0")
    if head.endswith("."):
        head += "0"
    return f"{head}e{int(tail):+03d}"


def longdouble(text: str) -> str:
    return _REAL.sub(_canonical, text)


# There is deliberately no `mtime` normaliser.  Blanking the
# `YYYY-MM-DD HH:MM:SS` column in `.archive -tv` would hide the question the
# column answers -- whether the port stores and restores mtimes at all -- and a
# port that dropped them entirely would pass.  The cases pin every stamp with
# `writefile(name,data,mode,mtime)` before archiving, so the expected date is a
# constant and is asserted exactly.  See cases_extensions._mtime.  What makes the
# stamp assertable at all is that `path_filestat_set_times` is one of the WASI
# calls wasmtime does implement, so a port that stores an mtime can restore it;
# the cases would have to be dropped rather than normalised if it were not.


# shell.c prints argv[0] at exactly four sites, in two shapes: the usage banner
# and the "%s: Error: ..." prefix for option errors (shell.c:18584, 18917, 19089,
# 18649).  argv[0] is chosen by whoever launches the process -- an absolute path
# for the reference binary, a module name under wasmtime -- so it says nothing
# about the port.  Only those two shapes are rewritten; a bare "Error: ..." from
# the engine has no prefix token and is left exactly as it is.
_USAGE = re.compile(r"^Usage: \S+ \[OPTIONS\]", re.M)
_ARGV0_ERR = re.compile(r"^(\S+): Error: ", re.M)


def progname(text: str) -> str:
    text = _USAGE.sub("Usage: sqlite3 [OPTIONS]", text)
    return _ARGV0_ERR.sub("sqlite3: Error: ", text)


# src/shell.c.in:5188 names the file behind `.excel` and `.once -e|-x` by rendering
# a random 64-bit value: `sqlite3_mprintf("temp%llx.%s", r, zSuffix)`, with r from
# sqlite3_randomness on the line before.  (The other citations in this file are to
# the *generated* shell.c, which is three times longer; this one is to the shipped
# source, because that is the file a reader of State A has.)  The name is therefore
# different on every run of the same binary, and it reaches a scored stream: under
# NOHAVE_SYSTEM the port never launches a viewer and never prints it, but the
# pre-migration build does --
# `sh: 1: xdg-open: not found\nFailed: [xdg-open temp54bd092d.csv]`.
#
# That makes the token non-deterministic rather than platform-dependent, which is
# the one thing a differential comparison cannot survive: two runs of one binary
# disagree.  The four `plat.system.excel` cases already record the native answer
# with the digits struck out by hand (`tempNNNN.csv` in their DIFFER: comments),
# so this normaliser writes down what those comments were already saying.
#
# It removes nothing a port is responsible for.  The suffix is kept, because
# which of .csv/.txt the shell chooses *is* the contract the cases assert; only
# the random digits go.
_TEMPNAME = re.compile(r"\btemp[0-9a-f]{1,16}\.(csv|txt)\b")


def tempname(text: str) -> str:
    return _TEMPNAME.sub(r"tempNNNN.\1", text)


NORMALISERS = {
    "eqp": eqp,
    "longdouble": longdouble,
    "progname": progname,
    "tempname": tempname,
}


def apply(text: str, names: tuple[str, ...]) -> str:
    for name in names:
        try:
            fn = NORMALISERS[name]
        except KeyError:  # pragma: no cover - guarded by case
            raise KeyError(f"unknown normaliser {name!r}") from None
        text = fn(text)
    return text
