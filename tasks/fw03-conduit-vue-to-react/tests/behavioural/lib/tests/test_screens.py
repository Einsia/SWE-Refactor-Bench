"""What one group of screens renders, compared against what State A rendered.

Seven modules run this file, one per scenario group, each seeing only its own
scenarios through ``$SRB_GROUP``. The file is shared because the question is the
same for every group -- did this screen come out the way it went in -- and having
seven copies of it would mean seven places to fix a comparison bug.

Four kinds of check, from four different angles on the same settled page:

``node``     every rendered node under ``#app``, positionally. The exhaustive
             view: tag, class set, remaining attributes, and for text nodes the
             collapsed text. What was erased before comparison -- scoped-style
             attributes, class order, ``router-link-active``, framework-generated
             ids -- is in ``harness/normalize.mjs``. Those are things a different
             framework cannot be expected to reproduce, so they are not the
             contract. Everything else is.
``probe``    35 named selectors. The positional view is thorough but brittle: one
             extra wrapper shifts everything after it and the failures stop saying
             what broke. A probe asks about a piece of the UI by name -- how many
             ``.article-preview``, what do the ``.nav-link`` say, where do the
             ``.tag-list`` links point -- and is unaffected by structure
             elsewhere. A port with the right content in the wrong wrapper loses
             the positional checks and keeps these.
``shape``    where the router ended up, the document title, the presence of
             ``#app``, how much was rendered, the classes on ``<body>``, and
             whether any new uncaught error appeared.
``control``  live form state, which a DOM snapshot cannot carry: an input's
             current value lives in the property, not the attribute. This is
             where a framework port most often goes subtly wrong, because
             ``v-model`` and React's controlled inputs disagree about when state
             flows back into the field.

Nothing here reads the submitted source. Both sides are bundles that were built
and then driven; what is compared is what came out of a browser.
"""

from __future__ import annotations

import pytest

import srbobserve as obs

SCENARIOS = obs.SCENARIOS


def _node_params():
    out = []
    for sid in SCENARIOS:
        for i, node in enumerate(obs.reference(sid)["observed"]["nodes"]):
            out.append(pytest.param(sid, i, id=f"{sid}::{i}::{node['path']}"))
    return out


def _probe_params():
    out = []
    for sid in SCENARIOS:
        for selector in sorted(obs.reference(sid)["observed"]["probes"]):
            out.append(pytest.param(sid, selector, id=f"{sid}::{selector}"))
    return out


def _control_params():
    out = []
    for sid in SCENARIOS:
        for i, control in enumerate(obs.reference(sid)["observed"]["controls"]):
            out.append(pytest.param(sid, i, id=f"{sid}::{control.get('path', '?')}"))
    return out


# --------------------------------------------------------------------------- #
# shape
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sid", SCENARIOS)
def test_final_url(sid: str) -> None:
    """The scenario ended at the same app URL."""
    want = obs.reference(sid)["observed"]["url"]
    got = obs.observed(sid)["observed"]["url"]
    assert got == want, (
        f"the scenario ended at a different URL.\n"
        f"  expected  {want}\n"
        f"  reached   {got}\n"
        "State A routes in hash mode; the URL is part of the observable product."
    )


@pytest.mark.parametrize("sid", SCENARIOS)
def test_document_title(sid: str) -> None:
    want = obs.reference(sid)["observed"]["title"]
    got = obs.observed(sid)["observed"]["title"]
    assert got == want, f"document.title is {got!r}, expected {want!r}"


@pytest.mark.parametrize("sid", SCENARIOS)
def test_app_root_present(sid: str) -> None:
    """There is an element with id="app" to measure from."""
    assert obs.observed(sid)["observed"].get("hasApp"), (
        'no element with id="app" was found. The comparison anchor is the #app '
        "element State A's App component renders; a React root may mount into "
        "any container, but the app itself must still render #app."
    )


@pytest.mark.parametrize("sid", SCENARIOS)
def test_node_count(sid: str) -> None:
    """The same amount of DOM was rendered."""
    want = obs.reference(sid)["observed"]["nodeCount"]
    got = obs.observed(sid)["observed"]["nodeCount"]
    assert got == want, (
        f"rendered {got} nodes under #app, expected {want}. "
        + ("Something extra is being rendered." if got > want else "Something is missing.")
    )


@pytest.mark.parametrize("sid", SCENARIOS)
def test_body_classes(sid: str) -> None:
    """Classes on <body>, which some views set and must therefore unset."""
    want = obs.reference(sid)["observed"]["bodyClasses"]
    got = obs.observed(sid)["observed"]["bodyClasses"]
    assert got == want, f"<body> classes are {got}, expected {want}"


@pytest.mark.parametrize("sid", SCENARIOS)
def test_no_new_page_errors(sid: str) -> None:
    """The page raised the same uncaught errors State A did -- no new ones.

    State A genuinely throws in a few places (an unhandled 401 on the anonymous
    feed, a 404 on a missing article), so "no errors at all" would be the wrong
    contract and would fail a faithful port. What is required is that no *new*
    error appears: a port that throws where State A did not is broken even if the
    DOM happens to settle in the right place.
    """
    want = obs.reference(sid)["pageErrors"]
    got = obs.observed(sid)["pageErrors"]
    extra = [e for e in got if e not in want]
    assert not extra, f"{len(extra)} uncaught error(s) State A did not raise: {extra[:3]}"


# --------------------------------------------------------------------------- #
# node
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sid,index", _node_params())
def test_node(sid: str, index: int) -> None:
    """The node at this document position is the one State A rendered."""
    want_nodes = obs.reference(sid)["observed"]["nodes"]
    want = want_nodes[index]

    got_nodes = obs.observed(sid)["observed"]["nodes"]
    if index >= len(got_nodes):
        pytest.fail(
            f"the submission rendered only {len(got_nodes)} nodes here, but State A "
            f"rendered {len(want_nodes)}; nothing at position {index}, which should "
            f"have been {want['path']}  {obs.node_line(want)}"
        )

    got = got_nodes[index]
    if got["path"] != want["path"]:
        pytest.fail(
            f"node {index} is in the wrong place.\n"
            f"  expected  {want['path']}  {obs.node_line(want)}\n"
            f"  rendered  {got['path']}  {obs.node_line(got)}"
        )

    want_line, got_line = obs.node_line(want), obs.node_line(got)
    assert got_line == want_line, (
        f"node {index} at {want['path']} differs.\n"
        f"  expected  {want_line}\n"
        f"  rendered  {got_line}"
    )


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sid,selector", _probe_params())
def test_probe(sid: str, selector: str) -> None:
    """This selector selects the same elements, with the same text and links."""
    want = obs.reference(sid)["observed"]["probes"][selector]
    got_all = obs.observed(sid)["observed"]["probes"]

    if selector not in got_all:
        pytest.fail(
            f"the submission's observation has no entry for '{selector}'. "
            "State A saw: " + obs.probe_line(want)
        )
    got = got_all[selector]

    if got.get("count") != want.get("count"):
        pytest.fail(
            f"'{selector}' matched {got.get('count')} element(s); State A matched "
            f"{want.get('count')}.\n"
            f"  expected  {obs.probe_line(want)}\n"
            f"  rendered  {obs.probe_line(got)}"
        )

    assert got.get("texts") == want.get("texts"), (
        f"'{selector}' has the right number of elements but different text.\n"
        f"  expected  {want.get('texts')}\n"
        f"  rendered  {got.get('texts')}"
    )
    assert got.get("hrefs") == want.get("hrefs"), (
        f"'{selector}' links elsewhere than State A.\n"
        f"  expected  {want.get('hrefs')}\n"
        f"  rendered  {got.get('hrefs')}"
    )


# --------------------------------------------------------------------------- #
# control
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("sid", SCENARIOS)
def test_control_count(sid: str) -> None:
    """The same number of form controls exist."""
    want = obs.reference(sid)["observed"]["controls"]
    got = obs.observed(sid)["observed"]["controls"]
    assert len(got) == len(want), (
        f"found {len(got)} form control(s), expected {len(want)}.\n"
        f"  expected  {[c.get('path') for c in want]}\n"
        f"  rendered  {[c.get('path') for c in got]}"
    )


@pytest.mark.parametrize("sid,index", _control_params())
def test_control_live_state(sid: str, index: int) -> None:
    """This control holds the value State A's control held."""
    want = obs.reference(sid)["observed"]["controls"][index]
    got_all = obs.observed(sid)["observed"]["controls"]

    if index >= len(got_all):
        pytest.fail(
            f"no control at position {index}; State A had "
            f"{want.get('path')}  {obs.control_line(want)}"
        )
    got = got_all[index]

    if got.get("path") != want.get("path"):
        pytest.fail(
            f"control {index} is elsewhere in the document.\n"
            f"  expected  {want.get('path')}\n"
            f"  found     {got.get('path')}"
        )
    assert obs.control_line(got) == obs.control_line(want), (
        f"control at {want.get('path')} is in a different state.\n"
        f"  expected  {obs.control_line(want)}\n"
        f"  rendered  {obs.control_line(got)}"
    )
