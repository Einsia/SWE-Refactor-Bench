"""What the service does when the request is wrong.

Second-heaviest module on the stage, above its share of the corpus, and the reason
is worth stating plainly: error responses are the part of a web framework a
migration INHERITS rather than writes.  Dropwizard's Jersey stack, with the three
exception mappers this repository registers on top of it, turns an unparseable
query parameter into a 400 with a particular JSON envelope, an unknown path into a
404 with a different one, and an unacceptable Accept into a 406 with no body at
all.  None of that is in the repository's own code; all of it is in its observable
contract.

A submission that wired up the happy paths and let its new framework's defaults
answer everything else diverges on all 22 of these at once, while looking complete
from the outside.  That is precisely the failure mode this module exists to price.

What is compared: status, media type, and the STRUCTURE of the body -- which keys
an error envelope carries, and their types.  What is not: the human-readable
message inside it.  Wording is the framework's to phrase, and a task that graded
it would be asking for a Dropwizard reimplementation rather than a migration.  The
harness enforces that split; these tests do not restate it per case.

Also excluded, and excluded in stage 3's scope for the same reason: anything the
container answers before application code runs.  A malformed request line or a
truncated header block is Jetty's to reject, and what it says is not this
repository's contract.
"""
from __future__ import annotations


# --- parameters that are missing or unusable ---------------------------------

def test_route_without_any_point(pair):
    """The required parameter absent entirely."""
    pair("err-no-point").check()


def test_route_with_one_point(pair):
    """Present, but not enough of it: a routing request needs two."""
    pair("err-one-point").check()


def test_route_with_unparseable_point(pair):
    """A coordinate that is not a coordinate.  The failure happens in the
    parameter binder, before any application code sees the request."""
    pair("err-malformed-point").check()


def test_route_with_point_out_of_range(pair):
    """Syntactically a coordinate, geographically impossible.  This one parses and
    then fails validation -- a different layer from the case above, and often a
    different status."""
    pair("err-point-out-of-range").check()


def test_route_with_unknown_profile(pair):
    """A profile name that was never configured.  The resolver's own error."""
    pair("err-unknown-profile").check()


def test_route_without_a_profile(pair):
    """No profile at all: whether that is a 400 or a fallback to a default is
    exactly the kind of thing a rewrite changes without meaning to."""
    pair("err-no-profile").check()


def test_route_between_unroutable_points(pair):
    """Both points valid, no path between them -- one is off the graph entirely.
    The request is well-formed and the ANSWER is a failure, which is a third
    distinct category from the two above."""
    pair("err-unroutable").check()


def test_integer_parameter_given_a_word(pair):
    """time_limit=abc.  Type coercion failing inside the framework."""
    pair("err-bad-integer").check()


def test_boolean_parameter_given_a_number(pair):
    """Boolean binding is famously permissive in some stacks and strict in others.
    Which one this service is counts as behaviour."""
    pair("err-bad-boolean").check()


def test_unknown_path_detail_requested(pair):
    """A details key the encoding manager does not have.  Application-level
    validation with a specific message shape."""
    pair("err-unknown-details").check()


def test_custom_model_refused_on_ch(pair):
    """CH cannot honour a custom model, and refusing it is a deliberate error
    rather than an oversight.  A submission that silently accepted it would be
    returning a wrong route instead of an error."""
    pair("err-ch-custom-model").check()


# --- request bodies that cannot be used --------------------------------------

def test_post_with_malformed_json(pair):
    """Unparseable JSON.  The failure is in the body reader."""
    pair("err-post-malformed-json").check()


def test_post_with_empty_body(pair):
    """No body where one is required -- distinct from a body that is present and
    broken, and frequently a different status."""
    pair("err-post-empty-body").check()


def test_post_with_wrong_types(pair):
    """Valid JSON, wrong types inside: a string where the model wants a number."""
    pair("err-post-wrong-type").check()


def test_post_with_wrong_shape(pair):
    """Valid JSON, valid types, wrong structure -- an object where an array
    belongs.  Deserialisation gets further before failing, and says something
    different when it does."""
    pair("err-post-json-wrong-shape").check()


# --- paths and methods -------------------------------------------------------

def test_unknown_path(pair):
    """A path nothing is registered under."""
    pair("err-404-unknown").check()


def test_near_miss_path(pair):
    """`/routes` against a service that serves `/route`.  Whether a near miss is a
    404 or a redirect is dispatch behaviour, and both frameworks have opinions."""
    pair("err-404-near-miss").check()


def test_wrong_method_on_a_real_path(pair):
    """DELETE on /route.  A 405 carries an Allow header, and `headers` grades that
    header while this module grades the status and envelope."""
    pair("err-405-wrong-method").check()


def test_post_to_a_get_only_resource(pair):
    """POST /info.  The same 405 from a resource that declares only one method,
    rather than from one that declares two."""
    pair("err-405-on-info").check()


def test_trailing_slash_on_a_real_path(pair):
    """`/route/` where `/route` is registered.  A one-character difference that
    dispatch layers treat differently, and one an operator's clients will hit."""
    pair("err-trailing-slash").check()


def test_encoded_slash_in_a_path_segment(pair):
    """`%2F` inside what should be a single path segment.  Whether it is decoded
    before matching decides which resource sees it, or whether anything does."""
    pair("err-encoded-slash").check()


def test_double_slash_mid_path(pair):
    """`/i18n//de`: an empty path segment.  Some dispatchers collapse it, some
    match an empty template variable, some reject the request."""
    pair("err-double-slash-mid").check()


def test_every_error_case_is_graded(cases_in):
    graded = sum(1 for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)) - 1
    expected = len(cases_in("errors"))
    assert graded == expected, (
        f"the corpus has {expected} error case(s) and this module grades "
        f"{graded}."
    )
