"""What the app asked the API for, in order, across all 100 scenarios.

The screen modules say what the app showed. This says what it did, and they are
independent failures. A port can render the right article page by fetching the
article twice, or by fetching the whole article list and filtering client-side, or
by asking for page 1 when the pagination says page 3, and the DOM would not show
any of it. The server records every request it answers, so this is the log of the
app's side of the conversation.

Each request is compared on method, path, query string, body and the *identity* of
the bearer token -- not the token's value, which the driver symbolises, so the
contract is "it sent the current user's token" rather than "it sent this string".

Some of what this pins down is unflattering to State A and part of the contract
anyway, because it is observable. The clearest case: after signing out, Conduit
never clears the Authorization header it set on the axios default, so the next
three requests still carry the departed user's token. A tidier port would not do
that, and would fail here. instruction.md says so, and the scenario that measures
it is named for the quirk rather than hidden among the others.
"""

from __future__ import annotations

import pytest

import srbobserve as obs

SCENARIOS = obs.SCENARIOS


def _request_params():
    out = []
    for sid in SCENARIOS:
        for i in range(len(obs.reference(sid)["requests"])):
            out.append(pytest.param(sid, i, id=f"{sid}::req{i}"))
    return out


@pytest.mark.parametrize("sid", SCENARIOS)
def test_request_count(sid: str) -> None:
    """The app made the same number of API calls, no more and no fewer."""
    want = obs.reference(sid)["requests"]
    got = obs.observed(sid)["requests"]
    if len(got) == len(want):
        return
    verb = "more" if len(got) > len(want) else "fewer"
    detail = "\n".join(
        f"  {'expected' if i < len(want) else '        '} "
        f"{obs.request_line(want[i]) if i < len(want) else '-'}\n"
        f"  {'made    ' if i < len(got) else '        '} "
        f"{obs.request_line(got[i]) if i < len(got) else '-'}"
        for i in range(max(len(want), len(got)))
    )
    pytest.fail(
        f"the app made {len(got)} API call(s), {verb} than State A's "
        f"{len(want)}:\n{detail}"
    )


@pytest.mark.parametrize("sid,index", _request_params())
def test_request(sid: str, index: int) -> None:
    """This API call is the one State A made at this point in the scenario."""
    want = obs.reference(sid)["requests"][index]
    got_all = obs.observed(sid)["requests"]

    if index >= len(got_all):
        pytest.fail(
            f"the app made only {len(got_all)} call(s); State A's call {index} was "
            f"{obs.request_line(want)}"
        )

    got = got_all[index]
    assert obs.request_line(got) == obs.request_line(want), (
        f"API call {index} differs.\n"
        f"  expected  {obs.request_line(want)}\n"
        f"  made      {obs.request_line(got)}"
    )
