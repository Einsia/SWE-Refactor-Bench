"""Reads, under default flags: 204 exchanges, compared on every dimension.

This is most of what the service is. A client of json-server spends its time
filtering, paginating, sorting, slicing, searching and embedding, and every one of
those is a query-string feature the rewrite has to reimplement rather than
forward. Also here: the static mount, conditional requests, ranges, HEAD, OPTIONS,
and the methods json-server does not handle.

Each dimension is a separate test function over the same exchanges, so a failure
names both the request and what about it broke -- the difference between "case 214
differs" and "case 214 returns the right body with the wrong Content-Type". They
all read one shared replay, so all of them describe the same response.

Nothing here opens a file in the submission. The subject is a running server and
what it answers.
"""

from __future__ import annotations

import pytest
import srbcompare as compare
from srbfixtures import parametrize

pytestmark = pytest.mark.behaviour

SESSION = "read"


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

@parametrize(SESSION)
def test_status(pair, key):
    compare.status(pair)


@parametrize(SESSION)
def test_status_class(pair, key):
    compare.status_class(pair)


@parametrize(SESSION)
def test_no_new_server_error(pair, key):
    compare.no_new_server_error(pair)


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #

@parametrize(SESSION)
def test_content_type(pair, key):
    compare.content_type(pair)


@parametrize(SESSION)
def test_header_values(pair, key):
    compare.header_values(pair)


@parametrize(SESSION)
def test_case_headers(pair, key):
    compare.case_headers(pair)


@parametrize(SESSION)
def test_no_extra_headers(pair, key):
    compare.no_extra_headers(pair)


@parametrize(SESSION)
def test_no_missing_headers(pair, key):
    compare.no_missing_headers(pair)


# --------------------------------------------------------------------------- #
# Bodies
# --------------------------------------------------------------------------- #

@parametrize(SESSION)
def test_body(pair, key):
    compare.body(pair)


@parametrize(SESSION)
def test_body_length(pair, key):
    compare.body_length(pair)


@parametrize(SESSION)
def test_json_shape(pair, key):
    compare.json_shape(pair)


@parametrize(SESSION)
def test_json_values(pair, key):
    compare.json_values(pair)


@parametrize(SESSION)
def test_json_style(pair, key):
    compare.json_style(pair)


@parametrize(SESSION)
def test_stack_body(pair, key):
    compare.stack_body(pair)


@parametrize(SESSION)
def test_stack_present(pair, key):
    compare.stack_present(pair)


@parametrize(SESSION)
def test_stack_validator_shape(pair, key):
    compare.stack_validator_shape(pair)


@parametrize(SESSION)
def test_body_mode_known(pair, key):
    compare.body_mode_known(pair)


@parametrize(SESSION)
def test_compressed_body_decodes(pair, key):
    compare.decompresses(pair)
