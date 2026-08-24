"""Response headers: the set that was sent, and the value of each one.

Headers are where a framework swap leaks most quietly. Gin sets ``Content-Type``
from its own render helpers, writes ``Content-Length`` through its own writer, and
ChartMuseum layers ``X-Request-Id`` on top via middleware; chi does none of that
for you. A port that returns the right JSON with a ``text/plain`` content type is
wrong in a way no body comparison would catch, and a port that stops sending
``Content-Length`` breaks every client that streams a chart.

Two levels are asserted separately. The header *set* catches a header that
vanished or appeared; the header *value* catches one that changed meaning. Both
are needed: a missing ``WWW-Authenticate`` and an empty one are different bugs,
and only the set test distinguishes them.

Values compared here are the normalised forms from :mod:`harness.normalize` --
shaped headers such as ``X-Request-Id`` are reduced to a placeholder if and only
if they match their expected shape, so a well-formed uuid passes and a literal
copy of the oracle's uuid also passes, but a missing or malformed one does not.
That is intentional: the contract is 'a fresh request id', not 'this request id'.

The three tests that can skip carry ``srb_skip_ok``. Each skips on
``header_is_volatile``, which is the two capture runs of State A disagreeing about
that header --- a property of the recording, not something a submission can reach.
See ``test_body.py``'s module docstring for the full argument; without the marker
``pytest_module`` scores these as misses against the oracle they were recorded
from.
"""

from __future__ import annotations

import pytest

import reference as ref


@pytest.mark.parametrize("key", ref.ALL_KEYS)
def test_header_set_matches(pair, key):
    """Exactly the same header names were sent, no more and no fewer."""
    want = set(pair.expected["header_names"])
    got = set(pair.actual["headers"])
    # A name whose value proved unstable in State A is not a contract in either
    # direction: it may legitimately be absent.
    volatile = {n for n in want | got if pair.header_is_volatile(n)}
    want -= volatile
    got -= volatile
    missing = sorted(want - got)
    extra = sorted(got - want)
    assert not missing and not extra, (
        f"{pair.describe()}: header set differs -- "
        f"missing {missing}, unexpected {extra}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key,name", ref.HEADER_PAIRS,
                         ids=[f"{k}::{n}" for k, n in ref.HEADER_PAIRS])
def test_header_value_matches(pair, key, name):
    """This header carries the same normalised value it did in State A."""
    if pair.header_is_volatile(name):
        pytest.skip(f"{name} was not stable across two runs of State A")
    want = pair.expected["headers"].get(name)
    got = pair.actual["headers"].get(name)
    assert got is not None, (
        f"{pair.describe()}: header {name!r} was not sent at all "
        f"(State A sent {want!r})")
    assert got == want, (
        f"{pair.describe()}: header {name!r} was {got!r}, expected {want!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", sorted(
    k for k, n in ref.HEADER_PAIRS if n == "content-type"))
def test_content_type_matches(pair, key):
    """``Content-Type`` specifically, named as its own test.

    Singled out because it is both the most likely header to drift in a router
    migration and the one whose drift is most invisible: the bytes are right, the
    status is right, and every client still misreads the response.
    """
    if pair.header_is_volatile("content-type"):
        pytest.skip("content-type was not stable across two runs of State A")
    want = pair.expected["headers"]["content-type"]
    got = pair.actual["headers"].get("content-type")
    assert got == want, (
        f"{pair.describe()}: Content-Type was {got!r}, expected {want!r}")


@pytest.mark.srb_skip_ok
@pytest.mark.parametrize("key", sorted(
    k for k, n in ref.HEADER_PAIRS if n == "www-authenticate"))
def test_www_authenticate_preserved(pair, key):
    """The 401 challenge is unchanged.

    A client that cannot read the realm cannot retry, so this is a behavioural
    contract and not a cosmetic one.
    """
    if pair.header_is_volatile("www-authenticate"):
        pytest.skip("www-authenticate was not stable in State A")
    want = pair.expected["headers"]["www-authenticate"]
    got = pair.actual["headers"].get("www-authenticate")
    assert got == want, (
        f"{pair.describe()}: WWW-Authenticate was {got!r}, expected {want!r}")


@pytest.mark.parametrize("key", ref.ALL_KEYS)
def test_content_length_describes_the_body(pair, key):
    """``Content-Length`` agreed with the bytes delivered, as it did in State A.

    The one header assertion that survives masking. Two recorded bodies embed a
    temporary directory name, so their ``Content-Length`` is frozen as a
    placeholder and ``test_header_value_matches`` cannot see its value -- which
    leaves room for a port that declares a length it does not send. This compares
    the declaration against the delivery inside each response, so it is immune to
    that masking and to the rundir entirely.

    ``None`` means the question did not apply (no header sent, or a HEAD, where the
    header describes the body a GET would have returned). Compared rather than
    skipped: a port that stopped sending the header where State A sent it, or
    started sending it where State A did not, differs here too.
    """
    want = pair.expected.get("content_length_consistent")
    got = pair.actual.get("content_length_consistent")
    if want is None and got is None:
        return
    assert got == want, (
        f"{pair.describe()}: Content-Length/body agreement is {got!r}, expected "
        f"{want!r} -- the port declared a length that does not describe the "
        f"{pair.actual['body_len']} bytes it sent, or stopped declaring one")


@pytest.mark.parametrize("key", ref.ALL_KEYS)
def test_no_framework_fingerprint_header(pair, key):
    """No header appeared that State A never sent.

    Overlaps ``test_header_set_matches`` deliberately, with a different failure
    message: a port that leaks ``X-Powered-By``, a CORS header on a route that had
    none, or a stray ``Vary`` is a behaviour change even though it looks additive.
    Clients and caches key on exactly these.
    """
    want = set(pair.expected["header_names"])
    extra = sorted(n for n in set(pair.actual["headers"]) - want
                   if not pair.header_is_volatile(n))
    assert not extra, (
        f"{pair.describe()}: the port sent header(s) State A did not: {extra}")
