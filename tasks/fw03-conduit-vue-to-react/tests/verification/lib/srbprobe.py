"""The candidate's view of the application under test.

A stage-3 candidate is a pytest file. It is run twice -- once against a build of
the original, once against a build of the submission -- and it does not know
which run is which. It passes when the application behaves as the original does
and fails when it does not, so a candidate that passes one run and fails the
other has found a place where the two applications differ.

The only thing a candidate may do to the application is drive a browser at it:

    import srbprobe

    def test_tag_pill_survives_a_second_click():
        obs = srbprobe.observe([
            {"goto": "#/"},
            {"click": ".sidebar .tag-pill"},
            {"click": ".navbar-brand"},
        ], probes=[".article-preview", ".feed-toggle .nav-link.active"])
        assert obs.url.endswith("#/")
        assert obs.probe(".article-preview").count > 0

`observe` returns the same normalised observation stage 2 compares: the DOM under
``#app`` reduced to positions, tags, classes and text; a set of named selectors;
live form state; the API requests the page made; and where the router ended up.
The reduction is stage 2's, imported rather than reimplemented, which is what
makes a divergence found here one stage 2 would also have seen had its corpus
happened to visit the same screen in the same order.

What a candidate cannot do is ask which build it is talking to. There is no base
URL, no path to the bundle and no way to read a file out of it: the browser is
driven in a subprocess that is handed the location and does not report it back.
This is not merely inconvenient to work around -- it is the contract. A candidate
whose result depends on recognising the tree rather than on the application's
behaviour is rejected in adjudication however it learned it, because it would
"discriminate" between any two builds at all and so says nothing about this
migration. See ``probe.toml``.

Steps are the vocabulary stage 2's corpus uses, and are the whole vocabulary --
these five forms exactly, and any other key raises:

    {"goto": "#/tag/dragons"}          a full page load at a hash route: a cold
                                       boot, not an in-app transition
    {"click": "<selector>"}            a real click on the first match; in-app
                                       navigation goes through this
    {"fill": "<selector>", "value": "text"}   type into a field, driving the
                                       events both frameworks listen for
    {"press": "<selector>", "key": "Enter"}   a key on that element
    {"back": true}                     history.back()

Note that ``fill`` and ``press`` take a sibling key, not a nested mapping.

There is no wait step and none is needed: after *every* step the driver waits for
in-flight requests to reach zero and for the DOM to stop changing, then confirms
both once more, before the next step runs. What is observed is the settled page.
A finding that depends on catching an intermediate frame is a race, and a race is
not a divergence -- ``require_deterministic`` in probe.toml will reject it.

Two limits, enforced by the driver: 60 steps per scenario, and 40 selectors of
your own beyond the standard 35. Both are far above what a real finding needs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["observe", "Observation", "Probe", "ProbeError", "STANDARD_PROBES"]

#: How long one scenario may take, wall clock. A settled page is a couple of
#: seconds; this is generous enough that only a hung driver hits it.
_TIMEOUT_SEC = int(os.environ.get("SRB_PROBE_TIMEOUT", "300"))

_HERE = Path(__file__).resolve().parent
_DRIVER = _HERE / "probe-one.mjs"


class ProbeError(RuntimeError):
    """The browser could not be driven at all.

    This is a defect in the scenario or in the harness, never a finding: a
    candidate that raises it fails on both trees and discriminates nothing.
    """


# --------------------------------------------------------------------------- #
# the standard selectors, so a candidate can see what stage 2 already asks
# --------------------------------------------------------------------------- #
def _standard_probes() -> tuple[str, ...]:
    """Read the 35 selectors stage 2 observes out of the harness itself.

    Duplicating the list here would let it drift, and a candidate that "found" a
    divergence in a selector stage 2 already checks has found something stage 2
    would have scored -- worth knowing before spending a round on it.
    """
    src = (_HERE / "harness" / "normalize.mjs").read_text(encoding="utf-8")
    # The array ends at a `];` on its own line, not at the first `]` -- several
    # selectors contain one (`a[href]`, `input[placeholder="Email"]`), and
    # stopping at the first would truncate that entry and drop everything after
    # it while still returning a plausible-looking list.
    m = re.search(
        r"export const PROBE_SELECTORS\s*=\s*\[(.*?)^\];",
        src,
        re.DOTALL | re.MULTILINE,
    )
    if not m:  # pragma: no cover - the harness is checksummed
        return ()
    return tuple(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


STANDARD_PROBES: tuple[str, ...] = _standard_probes()


# --------------------------------------------------------------------------- #
# what comes back
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Probe:
    """What one selector matched on the settled page."""

    selector: str
    count: int
    texts: tuple[str, ...]
    hrefs: tuple[str, ...]

    @property
    def text(self) -> str:
        """The first match's collapsed text, or "" if nothing matched."""
        return self.texts[0] if self.texts else ""

    def __bool__(self) -> bool:
        return self.count > 0

    def __len__(self) -> int:
        return self.count

    def __repr__(self) -> str:
        return (
            f"Probe({self.selector!r}, count={self.count}, "
            f"texts={list(self.texts)!r}, hrefs={list(self.hrefs)!r})"
        )


@dataclass(frozen=True)
class Node:
    """One node under ``#app``, at one document position."""

    path: str
    kind: str
    tag: str
    text: str
    classes: tuple[str, ...]
    attrs: Mapping[str, str]

    @property
    def is_text(self) -> bool:
        return self.kind == "text"

    def __repr__(self) -> str:
        if self.is_text:
            return f"Node({self.path!r}, text={self.text!r})"
        return (
            f"Node({self.path!r}, <{self.tag}>, classes={list(self.classes)!r}, "
            f"attrs={dict(self.attrs)!r})"
        )


@dataclass(frozen=True)
class Request:
    """One request the page made to the API while the scenario ran."""

    method: str
    path: str
    search: str
    query: Mapping[str, Any]
    body: Any
    auth: str | None
    status: int

    @property
    def authenticated(self) -> bool:
        return bool(self.auth)

    def __repr__(self) -> str:
        return f"Request({self.method} {self.path}{self.search} -> {self.status})"


@dataclass(frozen=True)
class Control:
    """One form control's live state -- the property, not the attribute."""

    path: str
    tag: str
    type: str
    value: str
    checked: bool | None
    disabled: bool
    placeholder: str

    def __repr__(self) -> str:
        return (
            f"Control({self.path!r}, <{self.tag} type={self.type!r}>, "
            f"value={self.value!r}, checked={self.checked!r}, "
            f"disabled={self.disabled!r})"
        )


class Observation:
    """One settled page, reduced the way stage 2 reduces it.

    Compare whole observations when you mean "these two pages are the same", and
    reach for the accessors when you mean something narrower. Equality here is
    the same comparison stage 2 makes, so ``assert a == b`` between two
    observations of the same scenario is exactly stage 2's question.
    """

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._raw = dict(raw)
        self._observed = dict(raw.get("observed") or {})

    # -- the page ----------------------------------------------------------- #
    @property
    def rendered(self) -> bool:
        """Whether anything was observed at all.

        False when the page threw before settling. A candidate asserting
        ``obs.rendered`` is asserting the screen works, which is a legitimate
        finding -- but check ``step_errors`` first, because a selector that never
        appears because you named it wrongly looks identical from here.
        """
        return bool(self._raw.get("observed"))

    @property
    def url(self) -> str:
        """Where the router ended up: ``pathname + search + hash``, no origin.

        State A routes in hash mode, so this reads ``/#/`` at the feed and
        ``/#/@alice/favorites`` on a profile tab. The origin is omitted because
        the port is assigned per run and would differ between the two trees.
        """
        return str(self._observed.get("url", ""))

    @property
    def title(self) -> str:
        return str(self._observed.get("title", ""))

    @property
    def node_count(self) -> int:
        return int(self._observed.get("nodeCount", 0) or 0)

    @property
    def has_app(self) -> bool:
        return bool(self._observed.get("hasApp"))

    @property
    def body_classes(self) -> tuple[str, ...]:
        return tuple(self._observed.get("bodyClasses") or ())

    # -- the DOM ------------------------------------------------------------- #
    @property
    def nodes(self) -> tuple[Node, ...]:
        """Every node under ``#app``, in document order."""
        return tuple(
            Node(
                path=str(n.get("path", "")),
                kind=str(n.get("kind", "")),
                tag=str(n.get("tag", "")),
                text=str(n.get("text", "")),
                classes=tuple(n.get("classes") or ()),
                attrs=dict(n.get("attrs") or {}),
            )
            for n in self._observed.get("nodes") or ()
        )

    @property
    def text(self) -> str:
        """All text under ``#app``, joined with single spaces.

        Coarse on purpose: useful for "does the word 'dragons' appear anywhere",
        not for structure. Use ``probe`` or ``nodes`` for anything finer.
        """
        return " ".join(
            str(n.get("text", "")).strip()
            for n in self._observed.get("nodes") or ()
            if n.get("kind") == "text" and str(n.get("text", "")).strip()
        )

    def probe(self, selector: str) -> Probe:
        """What ``selector`` matched. Absent selectors read as count 0.

        The 35 in ``STANDARD_PROBES`` are always present; anything you passed in
        ``probes=`` is present too.
        """
        entry = (self._observed.get("probes") or {}).get(selector)
        if entry is None:
            return Probe(selector=selector, count=0, texts=(), hrefs=())
        return Probe(
            selector=selector,
            count=int(entry.get("count", 0) or 0),
            texts=tuple(entry.get("texts") or ()),
            hrefs=tuple(entry.get("hrefs") or ()),
        )

    @property
    def probes(self) -> Mapping[str, Probe]:
        return {sel: self.probe(sel) for sel in self._observed.get("probes") or {}}

    # -- forms --------------------------------------------------------------- #
    @property
    def controls(self) -> tuple[Control, ...]:
        return tuple(
            # `value` and `placeholder` are null in the harness when the element
            # has none; "" reads the same to a candidate and keeps `in` working.
            Control(
                path=str(c.get("path") or ""),
                tag=str(c.get("tag") or ""),
                type=str(c.get("type") or ""),
                value=str(c.get("value") or ""),
                checked=c.get("checked"),
                disabled=bool(c.get("disabled")),
                placeholder=str(c.get("placeholder") or ""),
            )
            for c in self._observed.get("controls") or ()
        )

    def control(self, needle: str) -> Control | None:
        """The first control whose placeholder, type or path contains ``needle``.

        Placeholders are how this app's forms are told apart -- "Article Title",
        "Email", "URL of profile picture" -- and they are part of the rendered
        product rather than an implementation detail.
        """
        for c in self.controls:
            if needle in c.placeholder or needle == c.type or needle in c.path:
                return c
        return None

    # -- traffic and storage -------------------------------------------------- #
    @property
    def requests(self) -> tuple[Request, ...]:
        """The API calls the page made, in the order the server saw them."""
        return tuple(
            Request(
                method=str(r.get("method", "")),
                path=str(r.get("path", "")),
                search=str(r.get("search", "")),
                query=dict(r.get("query") or {}),
                body=r.get("body"),
                auth=r.get("auth"),
                status=int(r.get("status", 0) or 0),
            )
            for r in self._raw.get("requests") or ()
        )

    def calls(self, method: str | None = None, path: str | None = None) -> tuple[Request, ...]:
        """Requests filtered by method and by ``path`` as a substring."""
        out = self.requests
        if method:
            out = tuple(r for r in out if r.method == method.upper())
        if path:
            out = tuple(r for r in out if path in r.path)
        return out

    @property
    def storage(self) -> Mapping[str, str]:
        """``localStorage`` as the page left it."""
        return dict(self._observed.get("storage") or {})

    @property
    def storage_keys(self) -> tuple[str, ...]:
        return tuple(self._observed.get("storageKeys") or ())

    # -- what went wrong ------------------------------------------------------ #
    @property
    def step_errors(self) -> tuple[str, ...]:
        """Steps that could not be carried out -- a selector that never appeared,
        a click that hit nothing. Usually your scenario, not the application."""
        return tuple(self._raw.get("stepErrors") or ())

    @property
    def page_errors(self) -> tuple[str, ...]:
        """Uncaught errors the page itself raised.

        State A raises some of these on purpose (an unhandled 401 on the
        anonymous feed, a 404 on a missing article), so "no page errors" is the
        wrong assertion. "The same ones" is the right one.
        """
        return tuple(self._raw.get("pageErrors") or ())

    # -- comparison ----------------------------------------------------------- #
    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Observation):
            return NotImplemented
        return self.digest() == other.digest()

    def __hash__(self) -> int:
        return hash(self.digest())

    def digest(self) -> str:
        """A stable JSON rendering of everything compared, for equality and diffs."""
        return json.dumps(
            {
                "observed": self._observed,
                "requests": self._raw.get("requests") or [],
                "pageErrors": self._raw.get("pageErrors") or [],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def as_dict(self) -> dict[str, Any]:
        """The raw observation, if you want to compare something not exposed here."""
        return json.loads(json.dumps(self._raw))

    def __repr__(self) -> str:
        return (
            f"Observation(url={self.url!r}, title={self.title!r}, "
            f"nodes={self.node_count}, controls={len(self.controls)}, "
            f"requests={len(self.requests)}, "
            f"stepErrors={len(self.step_errors)}, pageErrors={len(self.page_errors)})"
        )


# --------------------------------------------------------------------------- #
# driving
# --------------------------------------------------------------------------- #
def observe(
    steps: Sequence[Mapping[str, Any]],
    *,
    token: str | None = None,
    probes: Iterable[str] | None = None,
    name: str | None = None,
) -> Observation:
    """Drive one scenario against the application and return what it rendered.

    :param steps:  the scenario, in the vocabulary this module's docstring lists.
    :param token:  start as this signed-in user, by seeding the stored credential
                   the way a returning visitor would have it. ``"alice"``,
                   ``"bob"`` and ``"carol"`` exist in the fixtures. ``None`` (the
                   default) is an anonymous visitor, and so is any other name --
                   the seeding step only knows those three. To look at how an
                   invalid credential renders, log in as one of them and then
                   drive the app into the state you want; the fixtures have a
                   screen for it.
    :param probes: selectors of your own, observed *in addition* to the 35
                   standard ones. At most 40.
    :param name:   a label for this scenario, to make failure output readable.

    Called at most once per test in practice: each call launches a browser and a
    fresh API server with fresh fixture state, so two calls in one test are two
    independent visits rather than two steps of one.
    """
    if not isinstance(steps, Sequence) or isinstance(steps, (str, bytes)):
        raise ProbeError("steps must be a list of step mappings")
    if not steps:
        raise ProbeError("a scenario needs at least one step")

    dist = os.environ.get("SRB_PROBE_DIST", "").strip()
    if not dist:
        raise ProbeError(
            "SRB_PROBE_DIST is not set: this module only works inside the "
            "verification stage, where run-candidate.sh builds the application "
            "and points this at the result."
        )

    # Scratch is per-tree and private. SRB_WORK is deliberately not consulted as a
    # fallback: run-candidate.sh strips it, and it is the root both trees' state
    # sits under, so writing there would put this candidate's scenarios next to
    # the other tree's.
    work = Path(os.environ.get("SRB_PROBE_WORK") or "/tmp")
    work.mkdir(parents=True, exist_ok=True)
    tag = f"{name or 'candidate'}-{uuid.uuid4().hex[:8]}"
    scenario_file = work / f"scenario-{tag}.json"
    out_file = work / f"observation-{tag}.json"

    scenario: dict[str, Any] = {
        "id": str(name or "candidate"),
        "token": token,
        "steps": [dict(s) for s in steps],
    }
    if probes is not None:
        scenario["probes"] = [str(p) for p in probes]
    scenario_file.write_text(json.dumps(scenario), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(_DRIVER), dist, str(scenario_file), str(out_file)],
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SEC,
        cwd=str(_HERE),
    )
    if proc.returncode != 0 or not out_file.exists():
        # stderr, not stdout: the driver reports scenario-shape problems there,
        # and those are worth reading. It never prints the location it was given.
        raise ProbeError(
            f"the browser could not be driven (exit {proc.returncode}): "
            f"{proc.stderr.strip()[-800:]}"
        )

    raw = json.loads(out_file.read_text(encoding="utf-8"))
    return Observation(raw)
