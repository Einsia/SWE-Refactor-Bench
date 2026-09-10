#!/usr/bin/env python3
"""Where byte order is consulted now, and where it was consulted before.

A location report. Every check in this file reports coordinates and asserts nothing
about what it finds, and that is not modesty -- it is the only correct behaviour
available, for a reason specific to this task.

Why nothing here can be a requirement
-------------------------------------
The subject is a compile-time conditional, and there is no spelling a correct port
must use. All of these are right:

    #if defined(__BYTE_ORDER__) && __BYTE_ORDER__ == __ORDER_BIG_ENDIAN__
    #include <endian.h>   /* then compare BYTE_ORDER with BIG_ENDIAN */
    #include <sys/param.h>
    #ifdef __s390x__      /* as one arm of a list, with a fallback */
    -DWORDS_BIGENDIAN=1   /* added by the build for the big-endian target */

The last one is arguably the best engineering of the five and it leaves *no source
line at all* for a scan to find. So a check requiring any token would fail correct
ports, and the strongest one -- `grep -q 'define WORDS_BIGENDIAN'` -- would fail the
best of them.

The failure in the other direction is worse, because it is silent. Every one of
these satisfies a token requirement and leaves the interpreter broken:

    /* #define WORDS_BIGENDIAN 1 */          in a comment
    #if 0
    #define WORDS_BIGENDIAN 1
    #endif
    #define WORDS_BIGENDIAN 1                in a header nothing includes
    #define WORDS_BIGENDIAN __BYTE_ORDER__   defined, and always truthy

A check that rewards the token pays for all four. That is the shape of the mistake
this suite exists to not make: the string is evidence of where to look and is never
evidence of what is true.

So the outcome is measured elsewhere. Stage 2 compiles the submission for x86-64,
s390x and armhf and compares what each computes against the reference's x86-64
answers; a detection that concludes wrongly is a wrong value there, whatever it was
written with. What this module contributes is that the reviewer answering
`byte_order_is_detected` and `byte_order_handling_is_complete` starts with every
relevant line in both trees already in front of it, including State A's eight sites,
rather than spending its turns finding them.

The two sites worth naming
--------------------------
State A has six `#ifdef WORDS_BIGENDIAN` sites and two `#ifndef` ones. The `#ifndef`
pair is the trap: they are DataView's get and set paths, where the sense of a swap
flag is flipped *because* the host is little-endian. A submission that searched for
`#ifdef` found six of eight. A submission that fixed the detection and also inverted
those two by hand has flipped them twice and is wrong again. Both mistakes are
invisible to a token count and both are visible to a reader, so this module reports
the polarity of every site it finds and leaves the judgement where it belongs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import srbscan

pytestmark = pytest.mark.scan

#: `#if`-family lines mentioning a byte-order token, with the polarity captured.
COND = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif)\b(.*)$")


def _conditional_sites(root: Path) -> list[tuple[str, int, str, str]]:
    """``(rel, lineno, directive, text)`` for every conditional on byte order."""
    out = []
    for path, rel in srbscan.text_files(root):
        if path.suffix not in (".c", ".h", ".S"):
            continue
        for n, line in enumerate(srbscan.read(path).splitlines(), 1):
            m = COND.match(line)
            if not m:
                continue
            directive, rest = m.group(1), m.group(2)
            if any(tok in rest for tok in srbscan.ORDER_TOKENS):
                out.append((rel, n, directive, line.strip()[:160]))
    return out


def test_state_a_conditional_sites(original):
    """State A's own sites, so the review has the before-picture without grepping.

    Always fails, by construction: it is a report and State A always has sites. The
    scan is advisory, so this costs nothing and guarantees the eight lines the port
    is about are in the prompt verbatim.
    """
    sites = _conditional_sites(original)
    if not sites:
        pytest.fail("no byte-order conditional found in State A at all, which "
                    "means this scan's token list has gone stale relative to the "
                    "archive -- treat every other finding in this module as "
                    "unreliable")
    lines = [f"{rel}:{n}: [#{d}] {text}" for rel, n, d, text in sites]
    ifdef = sum(1 for _, _, d, _ in sites if d == "ifdef")
    ifndef = sum(1 for _, _, d, _ in sites if d == "ifndef")
    pytest.fail(
        f"State A consults byte order at {len(sites)} conditional site(s) "
        f"(#ifdef: {ifdef}, #ifndef: {ifndef}). The #ifndef ones are the trap: "
        f"they invert a swap flag *because* the host is little-endian, so a "
        f"submission that searched only for #ifdef missed them, and one that "
        f"inverted them by hand as well as fixing the detection flipped them "
        f"twice.\n  " + "\n  ".join(lines))


def test_submission_conditional_sites(repo):
    """The submission's sites, in the same format, for a line-by-line comparison."""
    sites = _conditional_sites(repo)
    if not sites:
        pytest.fail("the submission contains no conditional on any byte-order "
                    "token. That is not necessarily wrong -- a build that defines "
                    "the macro per target on the compile line leaves no source "
                    "line here -- but it means the detection, if there is one, is "
                    "in the build system and should be read there")
    lines = [f"{rel}:{n}: [#{d}] {text}" for rel, n, d, text in sites]
    ifdef = sum(1 for _, _, d, _ in sites if d == "ifdef")
    ifndef = sum(1 for _, _, d, _ in sites if d == "ifndef")
    pytest.fail(
        f"the submission consults byte order at {len(sites)} conditional site(s) "
        f"(#ifdef: {ifdef}, #ifndef: {ifndef}):\n  " + "\n  ".join(lines))


def test_site_delta(repo, original):
    """Which conditional sites appeared, disappeared, or changed polarity.

    The most useful single finding in this module, and still only a lead. A site
    that vanished may have been correctly replaced by a build-system definition or
    may have been deleted; a site whose `#ifndef` became `#ifdef` may be a correct
    simplification or a double inversion. Both need the surrounding code read.
    """
    def key(sites):
        return {(rel, text): (n, d) for rel, n, d, text in sites}

    before = key(_conditional_sites(original))
    after = key(_conditional_sites(repo))
    gone = sorted(set(before) - set(after))
    new = sorted(set(after) - set(before))

    if not gone and not new:
        return  # identical conditional structure: nothing for the review here

    lines = []
    if gone:
        lines.append(f"sites present in State A and absent from the submission "
                     f"({len(gone)}):")
        lines += [f"  {rel}:{before[(rel, text)][0]}: "
                  f"[#{before[(rel, text)][1]}] {text}" for rel, text in gone[:30]]
    if new:
        lines.append(f"sites present in the submission and absent from State A "
                     f"({len(new)}):")
        lines += [f"  {rel}:{after[(rel, text)][0]}: "
                  f"[#{after[(rel, text)][1]}] {text}" for rel, text in new[:30]]
    pytest.fail("\n".join(lines))


def test_token_locations(repo):
    """Every byte-order spelling in the submission, with citations.

    Wider than the conditional scan on purpose: a detection can be a `const` or an
    inline function rather than a preprocessor test, and a swap can be a call to a
    helper. Requires nothing to be present.
    """
    found = srbscan.order_sites(repo)
    if not found:
        pytest.fail("no byte-order token of any kind appears in the submission's "
                    "text. Read the build system: either the detection is there, "
                    "or there is no detection")
    lines = []
    for token in sorted(found):
        cites = found[token]
        lines.append(f"{token} ({len(cites)}):")
        lines += [f"  {c}" for c in cites[:6]]
        if len(cites) > 6:
            lines.append(f"  ... +{len(cites) - 6} more")
    pytest.fail(f"{len(found)} byte-order token(s) appear in the submission. "
                f"None of this is a requirement -- see this module's docstring -- "
                f"it is where to read:\n" + "\n".join(lines))


def test_build_file_byteorder_mentions(repo):
    """The same report, restricted to files that describe the build.

    Separated out because it answers a different question. A byte-order token on a
    compile line is how the most defensible version of this port looks, and it is
    also where the cross-endian bootstrap fix has to live: the rule that generates
    the two bytecode blobs is in the `Makefile`, and whether it accounts for a
    host/target mismatch is `bootstrap_handles_endianness`. Reading the build is not
    optional for that gate, so its lines are hoisted here rather than buried in the
    previous check's output.
    """
    lines = []
    for path, rel in srbscan.text_files(repo):
        name = path.name
        if not (name == "Makefile" or name.startswith("Makefile")
                or path.suffix in (".mk", ".m4", ".ac", ".am", ".cmake", ".sh")
                or name == "CMakeLists.txt"):
            continue
        text = srbscan.read(path)
        if not text:
            continue
        for token in srbscan.ORDER_TOKENS + ("qjsc", "QJSC", "-x", "CROSS_PREFIX",
                                             "HOST_CC", "repl.c", "qjscalc.c"):
            if token in text:
                lines.extend(srbscan.cite(path, rel, token, limit=5))
    if lines:
        # De-duplicate: one line can match several tokens.
        seen, unique = set(), []
        for line in lines:
            if line not in seen:
                seen.add(line)
                unique.append(line)
        pytest.fail(
            f"build files mention byte order, the bytecode compiler, or the "
            f"cross-compilation bootstrap at {len(unique)} line(s). The rules "
            f"generating repl.c and qjscalc.c are the bootstrap; whether they "
            f"account for the host and the target disagreeing is a gate:\n  "
            + "\n  ".join(unique[:60]))
