"""Response headers, as a map from lowercased name to its ordered values.

A map and not a sequence, and that choice is the substance of this module.  HTTP
does not make the relative order of two DIFFERENT header names meaningful, so
grading it would fail a submission for a difference no client can observe -- and
the order a framework emits headers in is an implementation detail of which
component wrote each one.  Repeated values of the SAME name do keep their order,
because that order is semantic.

Dropped as container bookkeeping: Date, Server, Connection, Keep-Alive,
Transfer-Encoding, Content-Length.  Everything else is compared -- Content-Type,
Content-Encoding, Vary, Allow, Location, Cache-Control, ETag, and every
Access-Control-* -- because a client that depended on one of them would notice it
disappear.  Charset parameters are normalised away: RFC 8259 makes JSON UTF-8, so
whether the header restates it is formatting.

Content-Encoding is compared as a header and then UNDONE before the body is, so a
submission may compress at a different level or with a different zlib and still
pass, and one that stops compressing when asked fails on the header.  That is
worth stating because it was not true until it was measured: with no decode, the
gzipped case's body parsed as nothing, landed in the "not comparable" branch of
harness.diff, and was the one case in the corpus whose body was never graded.

`headers` grades the header map on these eleven cases.  Every OTHER module also
compares header maps on its own cases -- the axis is not exclusive to this file.
What is here are the cases chosen BECAUSE of their headers, where the body is
incidental and the header is the answer.
"""
from __future__ import annotations


def test_cors_on_a_successful_request(pair):
    """The CORS headers a normal answer carries."""
    pair("hdr-cors-get").check()


def test_cors_preflight(pair):
    """OPTIONS with the preflight headers set.  A method no resource in this
    repository declares, answered by the framework's own CORS filter -- so a
    submission has to configure an equivalent filter rather than write a
    handler."""
    pair("hdr-cors-preflight").check()


def test_cors_survives_an_error(pair):
    """A 400 still needs its CORS headers, or a browser client cannot read the
    error it was given.  A filter wired into the success path only breaks exactly
    this case and nothing in `errors`."""
    pair("hdr-cors-on-error").check()


def test_cors_survives_a_404(pair):
    """The same question one layer further out: a path that matched no resource
    never reached application code, and whether the filter still ran decides
    whether the browser sees a 404 or an opaque network failure."""
    pair("hdr-cors-on-404").check()


def test_gzip_when_accepted(pair):
    """Accept-Encoding: gzip.  Whether the response is compressed, and whether it
    says so, are both part of the contract -- and the harness compares the DECODED
    body, so a submission may compress differently but must not answer
    differently."""
    pair("hdr-gzip-accepted").check()


def test_no_gzip_when_not_accepted(pair):
    """The same request without the header: identity, and no Content-Encoding.
    The pair is what matters -- a service that always compresses passes the case
    above and fails this one."""
    pair("hdr-gzip-refused").check()


def test_accept_json_explicitly(pair):
    """/route declares three producible types.  Asking for JSON by name should
    reach the same representation as asking for nothing."""
    pair("hdr-accept-json").check()


def test_accept_xml_still_gets_json(pair):
    """`Accept: application/xml` names a type the method's @Produces declares, and
    upstream answers `application/json` regardless: the method builds its own
    Response with the type set, and only `type=gpx` changes it.  A submission whose
    framework does content negotiation properly answers XML or 406 here and fails,
    which is the point -- what has to be preserved is upstream's behaviour, not the
    behaviour the annotations suggest."""
    pair("hdr-accept-xml").check()


def test_unacceptable_accept(pair):
    """A type the resource cannot produce.  Jersey answers 406 with no body; a
    framework that ignores Accept answers 200 with JSON, which is a silent
    difference that only shows up here."""
    pair("hdr-accept-unacceptable").check()


def test_head_on_a_routing_request(pair):
    """HEAD is not declared anywhere in this repository -- the framework derives it
    from GET, runs the whole handler, and discards the body.  The headers have to
    match the GET's, and a hand-rolled dispatcher usually answers 405."""
    pair("hdr-head-route").check()


def test_head_on_info(pair):
    """The same derivation on a cheap endpoint, to separate 'HEAD is not wired up'
    from 'HEAD on an expensive handler timed out'."""
    pair("hdr-head-info").check()


def test_every_header_case_is_graded(cases_in):
    graded = sum(1 for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)) - 1
    expected = len(cases_in("headers"))
    assert graded == expected, (
        f"the corpus has {expected} header case(s) and this module grades "
        f"{graded}."
    )
