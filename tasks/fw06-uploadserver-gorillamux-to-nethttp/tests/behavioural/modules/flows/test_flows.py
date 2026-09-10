"""Sequences, where a later step can see what an earlier one did.

Every other module grades one request against one recording.  These cases are
ordered chains -- a write, then a read of what it wrote, then a write to the same
path -- replayed without re-seeding between steps, and what they measure is state
rather than a response shape.  A port can answer every single-request case
correctly and still store the bytes under a different name, or fail to store them
at all, and only a chain sees it.

The three-step truncation chain is the one worth reading twice.  The upload limit
is enforced by `MaxBytesReader`, which fires part-way through the copy, after the
destination file has already been created -- so a 413 leaves the first 16 bytes on
disk.  Step 2 reads them back with a 200, and step 3 gets a 409 writing to that
path, because the partial file is really there.  A port that checks
`Content-Length` before opening anything is cleaner than what it replaced, returns
the same 413, and fails both of the later steps.  That is the intended outcome:
this task is a port, and a port that improves the behaviour has changed it.

The `flow-put-then-get` chain pins the other easy-to-miss rule: the stored path
comes from the request target, not from the multipart part's filename.  The two
deliberately disagree, so step 3 asks for the part's filename and expects a 404.
"""
from __future__ import annotations

import pytest
import srbcheck
import srbfixtures
from harness import corpus

CASES = srbfixtures.cases_in("flows")


@pytest.mark.parametrize("case_id", CASES)
class TestRecordedFlows:
    def test_status(self, pair, case_id):
        srbcheck.status(pair)

    def test_header_names_and_order(self, pair, case_id):
        srbcheck.header_names(pair)

    def test_header_values(self, pair, case_id):
        srbcheck.header_values(pair)

    def test_body_bytes(self, pair, case_id):
        srbcheck.body(pair)

    def test_content_length_matches_body(self, pair, case_id):
        srbcheck.length_is_honest(pair)


# --------------------------------------------------------------------------- #
# The chains, as chains
# --------------------------------------------------------------------------- #
#
# Each of the three below asserts a relation *between* steps rather than any step's
# response.  They overlap the per-case checks and cannot fail where those pass --
# what they add is a report that says which property of the sequence broke, since
# "step 3 returned 200 where 409 was recorded" does not by itself say that a
# refused upload left a file behind.

def _steps(pairs, *ids):
    """The Pairs for a chain, or a skip naming the step that is not measurable."""
    out = []
    for cid in ids:
        p = pairs.get(cid)
        if p is None or p.actual is None:
            pytest.skip(f"{cid} was not measured; the per-case tests report why")
        out.append(p)
    return out


def test_a_put_stores_under_the_target_not_the_part_filename(pairs):
    read, other = _steps(pairs, "flow-put-then-get.get",
                         "flow-put-then-get.notfilename")
    assert read.actual["status"] == 200, (
        f"the bytes written by the preceding PUT are not readable at the target "
        f"they were written to\n{read.render()}")
    assert read.actual_body == read.expected_body, (
        f"readable at the right path, with different bytes\n{read.render()}")
    assert other.actual["status"] == 404, (
        f"the multipart part's filename was also created; the target decides the "
        f"stored path and nothing else should exist\n{other.render()}")


def test_overwrite_is_refused_and_then_allowed(pairs):
    again, forced = _steps(pairs, "flow-put-then-get.again",
                           "flow-put-then-get.overwrite")
    assert again.actual["status"] == 409, (
        f"a second write to an existing path was not refused\n{again.render()}")
    assert forced.actual["status"] == 201, (
        f"the same write with ?overwrite=true was not allowed\n{forced.render()}")


def test_a_refused_upload_still_left_its_partial_file(pairs):
    refused, readback, reput = _steps(pairs, "flow-truncation",
                                      "flow-truncation.retry",
                                      "flow-truncation.reput")
    assert refused.actual["status"] == 413, (
        f"the over-limit upload was not refused with 413\n{refused.render()}")
    assert readback.actual["status"] == 200, (
        f"the refused upload left nothing on disk. Upstream's limit is enforced "
        f"by MaxBytesReader, which fires after the destination file is open, so "
        f"the first bytes survive -- refusing before opening anything is a "
        f"different behaviour\n{readback.render()}")
    assert readback.actual_body == readback.expected_body, (
        f"a partial file is there and holds different bytes than the recorded "
        f"prefix\n{readback.render()}")
    assert reput.actual["status"] == 409, (
        f"writing to the partial file's path was not refused, so nothing was "
        f"left there\n{reput.render()}")


def test_the_corpus_still_orders_these_chains(pairs):
    """Every continuation's predecessor is present and precedes it.

    A guard on the corpus.  A continuation id is one containing a dot, and the
    replay runs it immediately after the case its prefix names, without re-seeding.
    If a corpus edit removed a first step, its continuations would be replayed
    against a freshly seeded document root and would fail for a reason that has
    nothing to do with the submission -- so it fails here instead, naming the step
    that went missing.
    """
    order = {c["id"]: i for i, c in enumerate(corpus.all_cases())}
    broken = []
    for cid in CASES:
        if not corpus.is_continuation(cid):
            continue
        head = cid.split(".", 1)[0]
        if head not in order:
            broken.append(f"{cid}: its first step {head!r} is not in the corpus")
        elif order[head] > order[cid]:
            broken.append(f"{cid}: runs before its first step {head!r}")
    assert not broken, "\n".join(broken)
