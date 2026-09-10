"""The assertions, written once and imported by the modules that own them.

Every comparison module in this suite grades the same *kinds* of fact -- a status,
a header set, a body, a rendered field -- about a different slice of the corpus.
Writing those assertions once and routing the slice to them keeps the modules
honest about what they are: thirteen views of one recording, not thirteen
opinions about how to compare it.  A module's test file therefore reads as a list
of what its surface is graded on::

    from battery import (            # noqa: F401
        test_status, test_headers, test_header_names, test_body, ...
    )

pytest collects imported functions, and ``srbfixtures.pytest_generate_tests``
supplies each one with this module's share of the corpus.  So the same function
object grades 232 cases under ``listing`` and 15 under ``config``.

**Only parity assertions live here.**  The checks that hold of any correct server
without consulting State A -- a declared ``Content-Length`` matching the bytes
sent, no template syntax on the page -- are in the ``consistency`` module
instead.  Mixing them in would inflate every surface's pass rate with checks that
pass for free, and a surface's pass rate is meant to answer "how much of *this
part* of the migration is missing", which only parity can answer.

A module imports a battery only when its axis is non-empty; ``self_check`` below
asserts the correspondence in both directions at image build time, because the
failure otherwise is silent -- a surface that stops grading archives still
reports every check it does run as passing.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

import replay
import routing
from compare import (MISSING, _trim, canonical, compare_body, compare_field,
                     compare_headers, require, resolve, volatile)

# --------------------------------------------------------------------------- #
# Axes
# --------------------------------------------------------------------------- #
# Each axis is a predicate over what State A recorded, not over what the case
# declares.  The difference is load-bearing: 429 cases declare ``html`` or
# ``shape`` but only 416 have a parsed html record, and parametrising the ~20
# field comparisons by the declaration would run 260 comparisons that read
# ``MISSING`` on both sides and pass without measuring anything.


def _carries(field: str):
    def axis(module_id: str, golden: dict):
        return replay.golden_carries(field, routing.cases_for(module_id),
                                     golden=golden)
    return axis


def _all_cases(module_id: str, golden: dict):
    return routing.cases_for(module_id)


def _nonce_cases(module_id: str, golden: dict):
    get = replay.lookup(golden, golden=True)
    out = []
    for session, case in routing.cases_for(module_id):
        entry = get(session.id, case.id) or {}
        if entry.get("nonce"):
            out.append((session, case))
    return out


def _decoded_cases(module_id: str, golden: dict):
    return [(s, c) for s, c in routing.cases_for(module_id) if c.decompress]


def _sized_cases(module_id: str, golden: dict):
    """Every case whose body has a length worth comparing.

    A compressed archive does not: its length is how well deflate happened to do
    on the order the archiver walked the tree in, which is the order the
    filesystem handed the entries over in.  What the stream contains is graded by
    the archive facts, which read it back out.
    """
    get = replay.lookup(golden, golden=True)
    out = []
    for session, case in routing.cases_for(module_id):
        archive = (get(session.id, case.id) or {}).get("archive") or {}
        if archive.get("kind") == "tar.gz":
            continue
        out.append((session, case))
    return out


#: ``fixture name in the battery signature -> the cases it is run over``.
AXES = {
    "case": _all_cases,
    "html_case": _carries("html"),
    "archive_case": _carries("archive"),
    "nonce_case": _nonce_cases,
    "decoded_case": _decoded_cases,
    "sized_case": _sized_cases,
}


# --------------------------------------------------------------------------- #
# Per-case parity: status, headers, body
# --------------------------------------------------------------------------- #


def test_status(session_id, case, actual, expected):
    """The status code.

    The cheapest and least forgiving check there is, and the one where a port's
    framework assumptions surface first.  actix-files answers a directory request
    without a trailing slash with 301 and a Location, refuses an unsatisfiable
    Range with 416, honours If-None-Match with 304 and If-Match with 412, and
    renders a bad query parameter as 400.  A port on tower-http's ``ServeDir``
    gets several of those wrong by default; a hand-rolled one gets a different
    several wrong.
    """
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    if volatile(want, "status"):
        pytest.fail(f"{session_id}::{case.id}: the State A capture found the "
                    f"status itself unstable, which is a harness bug rather "
                    f"than a submission one")
    assert got["status"] == want["status"], (
        f"{session_id}::{case.id}: expected HTTP {want['status']}, got "
        f"{got['status']}"
        + (f"\n  ({case.note})" if case.note else ""))


def test_headers(session_id, case, actual, expected):
    """The always-compared headers, plus whatever the case names.

    Headers are where the two frameworks differ most and most quietly.
    actix-files sets ``Content-Type`` from its own mime guesser, adds
    ``Accept-Ranges: bytes`` to every file response, emits an ETag derived from
    the file's metadata, and disposes responses ``inline`` or ``attachment`` by
    guessed type.  tower-http's ``ServeDir`` emits no ETag at all, guesses types
    from a different table, and disposes of nothing.  None of that shows up in a
    status code and all of it is visible to a client.
    """
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    compare_headers(got, want, case.headers_extra, case.headers_skip,
                    session_id, case.id)


def test_header_names(session_id, case, actual, expected):
    """The *set* of headers, independent of their values.

    Separate from the value comparison because it catches the opposite mistake: a
    port that gets every value it emits right, but emits a header the baseline
    does not or omits one it does.  ``ETag`` is the case that matters -- a naive
    ``ServeDir`` port passes a content-type comparison and fails this.
    """
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    if volatile(want, "header_names"):
        return
    missing = [h for h in want["header_names"] if h not in got["header_names"]]
    extra = [h for h in got["header_names"] if h not in want["header_names"]]
    assert not missing and not extra, (
        f"{session_id}::{case.id}: header set differs\n"
        f"  missing: {missing}\n  unexpected: {extra}")


def test_body(session_id, case, actual, expected):
    """The body, by whichever means the case's mode calls for.

    miniserve's output is a rendered page, not a data structure: the row order,
    the timestamp column, the humanised span beside it, the escaping of a name
    containing ``" ' & < >``, the breadcrumbs, the sort links, the theme picker,
    the QR path, the wget footer, the version string.  All of it is bytes a user
    sees, so all of it is compared -- after the five classes of legitimate
    per-boot variation are normalised away, and after the capture's own
    volatility survey removes anything that moved between two runs of State A.

    What this is *not*: a check that the port produced valid HTML, used the same
    templating library, or structured its source like the original.  A port that
    emits these bytes by any means passes.
    """
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    compare_body(got, want, session_id, case.id)


def test_body_length(session_id, sized_case, actual, expected):
    """The response length, separately from the content.

    Cheap, and it isolates a class of failure that a body diff reports badly: a
    truncated range, an off-by-one on a suffix range, a listing that lost a row.
    Those read as a number here instead of as a wall of markup.

    Compares the *scrubbed* length for a text body and the raw length for a
    binary one -- miniserve's 500 pages name the filesystem path they failed to
    write, so a raw length there would compare the harness's own workdir name.
    """
    case = sized_case
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    field = "text_len" if want.get("is_text") else "body_len"
    compare_field(got, want, field, session_id, case.id)


def test_decoded(session_id, decoded_case, actual, expected):
    """A negotiated content-encoding actually decodes.

    Split from the body comparison because a body that fails to decompress makes
    that comparison fail for the wrong stated reason -- it would be comparing raw
    compressed bytes.  The finding here is exact: the port claimed an encoding it
    did not apply.
    """
    case = decoded_case
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    assert got.get("decompressed") == want.get("decompressed"), (
        f"{session_id}::{case.id}: expected the body to "
        + ("decode" if want.get("decompressed") else "fail to decode")
        + f" as {case.decompress}, but it "
        + ("decoded" if got.get("decompressed") else "did not"))


# --------------------------------------------------------------------------- #
# The rendered page, field by field
# --------------------------------------------------------------------------- #
# ``test_body`` already compares these pages, so nothing here can pass while that
# fails.  The point is the diagnosis.  A byte comparison on a 9 KiB page reports
# "the page differs" and hands over a wall of markup; these report "the rows are
# in the wrong order", "the size column reads 1.4 KiB where it should read
# 1.40 KiB", "the QR code has 441 modules instead of 625", "the theme picker lost
# an entry".  Each is a different bug with a different fix.
#
# They are also the fields most likely to survive a careless port and be wrong
# unnoticed: a port that renders a listing at all gets the file names right, and
# quietly gets the humanised sizes, the sort-link query strings and the timestamp
# format wrong.

FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    # What the page *is*: which rows, in which order, of which kind.
    "structure": ("title", "entry_names", "entry_classes", "breadcrumbs"),
    # The columns beside each row -- formatting the baseline does by hand.
    "columns": ("timestamps", "size_cells"),
    # Chrome whose absence means a whole feature is missing.
    "features": ("has_upload_form", "has_mkdir_form", "has_readme",
                 "readme_filename", "has_wget_footer", "has_qr"),
    # The QR is a generated SVG path.  Getting the module count right means the
    # encoded text, the error-correction level and the version all match.
    "qr": ("qr_modules", "qr_viewbox"),
    # Everything else visible and easy to drop.
    "shell": ("script_count", "theme_options", "version_footer",
              "error_message"),
}

_FIELDS = [(group, field) for group, fields in FIELD_GROUPS.items()
           for field in fields]


@pytest.mark.parametrize("group,field", _FIELDS,
                         ids=[f"{g}-{f}" for g, f in _FIELDS])
def test_html_field(session_id, html_case, group, field, actual, expected):
    got, want = actual(session_id, html_case.id), expected(session_id, html_case.id)
    require(got, want, session_id, html_case.id)
    compare_field(got, want, f"html.{field}", session_id, html_case.id)


def test_entry_hrefs_resolve(session_id, html_case, actual, expected):
    """Links point at the same *targets*, ignoring the per-boot route prefix.

    ``entry_hrefs`` is left out of the field comparison above because under
    ``--random-route`` every href carries a prefix that changes on every boot.
    What does not change is the rest of the href, so it is compared with the
    prefix this boot actually used stripped from both sides.  A port that forgets
    to prefix its links, or prefixes them twice, fails here.
    """
    got, want = actual(session_id, html_case.id), expected(session_id, html_case.id)
    require(got, want, session_id, html_case.id)
    compare_field(got, want, "html.entry_hrefs", session_id, html_case.id)


def test_query_links(session_id, html_case, actual, expected):
    """Sort links, archive links and form targets.

    Same normalisation as the hrefs and the same reason for being separate.  The
    sort links are the ones worth having: six per listing (name, size, date, each
    way), the query-string spelling is miniserve's own, and a port that invents
    ``?sort=name&order=asc`` where the baseline writes ``?sort=name&order=desc``
    has broken every column header while still rendering a plausible page.
    """
    got, want = actual(session_id, html_case.id), expected(session_id, html_case.id)
    require(got, want, session_id, html_case.id)
    for field in ("sort_links", "archive_links", "form_actions"):
        compare_field(got, want, f"html.{field}", session_id, html_case.id)


#: The ten-hex tail ``nanoid!(10, hex)`` produces, and whatever route prefix the
#: session put in front of it.  The prefix is not hard-coded: it is taken from the
#: baseline's own href, so an explicit ``--route-prefix``, the ``/RANDOM-ROUTE``
#: placeholder standing in for ``--random-route``, and no prefix at all are all
#: handled by one rule -- and a port serving the stylesheet under the wrong prefix
#: fails it.
NONCE_TAIL = re.compile(r"^(.*)/([0-9a-f]{10})$")


def test_nonce_route_shape(session_id, nonce_case, actual, expected):
    """The favicon and stylesheet live at fresh per-boot routes.

    Their values cannot be compared -- they are regenerated on every start, which
    is why the recorder replaces them with placeholders and the capture marks them
    volatile.  Their *shape* is a contract nothing else covers: a port that serves
    ``/static/style.css`` at a fixed path renders a page identical to the
    baseline's after normalisation and would pass every other assertion here.
    """
    case = nonce_case
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    want_nonce, got_nonce = want.get("nonce") or {}, got.get("nonce") or {}
    for key in ("icon_href", "css_href"):
        reference = NONCE_TAIL.match((want_nonce.get(key) or ""))
        if not reference:
            continue                      # not a nonce route in the baseline
        prefix = reference.group(1)
        href = got_nonce.get(key)
        match = NONCE_TAIL.match(href) if isinstance(href, str) else None
        assert match, (
            f"{session_id}::{case.id}: {key} is {href!r}. The baseline generates "
            f"this route per boot as {prefix}/<ten hex>; a fixed path is "
            f"observably different even though its value is not graded.")
        assert match.group(1) == prefix, (
            f"{session_id}::{case.id}: {key} is served under "
            f"{match.group(1)!r}, but the baseline puts it under {prefix!r}")
    if "icon_href" in want_nonce and "css_href" in want_nonce:
        assert got_nonce.get("icon_href") != got_nonce.get("css_href"), (
            f"{session_id}::{case.id}: the favicon and stylesheet share the "
            f"route {got_nonce.get('icon_href')!r}; the baseline gives each its "
            f"own")


# --------------------------------------------------------------------------- #
# Directory downloads
# --------------------------------------------------------------------------- #
# Not compared byte for byte, and this is the one place the suite deliberately
# grades less than it could.  A tar's block padding, a zip's central-directory
# extra fields, the compression level, the order the walker happened to visit --
# all per-implementation detail two correct archivers disagree on.  What a user
# receives, and what is exactly reproducible, is the member list and the member
# contents.  So that is the contract: which paths, of which sizes, of which
# kinds, with which bytes inside.
#
# The failures this catches are structural.  An archive rooted at the wrong
# prefix (``dira/file`` where the baseline writes ``dira/dira/file``, or an
# absolute path).  A symlink followed and inlined where the baseline skipped it.
# Hidden files included when ``--hidden`` was off.  A truncated stream -- which a
# length check calls "shorter" and this calls "unpackable".

ARCHIVE_FACTS = ("archive.ok", "archive.error", "archive.kind", "archive.count",
                 "archive.names", "archive.members", "archive.digest")


@pytest.mark.parametrize("fact", ARCHIVE_FACTS,
                         ids=[f.split(".")[1] for f in ARCHIVE_FACTS])
def test_archive_fact(session_id, archive_case, fact, actual, expected):
    got, want = actual(session_id, archive_case.id), expected(session_id, archive_case.id)
    require(got, want, session_id, archive_case.id)
    compare_field(got, want, fact, session_id, archive_case.id)


def test_archive_unpacks(session_id, archive_case, actual, expected):
    """The stream is a readable archive at all.

    Redundant with ``archive.ok`` above and kept anyway: it is the first thing
    anyone reading a failing report wants to know, and it can say what went wrong
    -- the exception the unpacker raised -- rather than reporting ``False !=
    True``.  Where the baseline itself produced no readable archive (an error page
    under ``--disable-archives``, say) the claim inverts rather than being waived,
    so a port that happily serves a tar there still fails.
    """
    case = archive_case
    got, want = actual(session_id, case.id), expected(session_id, case.id)
    require(got, want, session_id, case.id)
    want_ok = resolve(want, "archive.ok") is True
    got_ok = resolve(got, "archive.ok") is True
    if want_ok:
        assert got_ok, (
            f"{session_id}::{case.id}: the response did not unpack as "
            f"{resolve(want, 'archive.kind')}: {resolve(got, 'archive.error')}")
    else:
        assert not got_ok, (
            f"{session_id}::{case.id}: the response unpacked as a "
            f"{resolve(got, 'archive.kind')} archive, but the baseline served no "
            f"archive here ({resolve(want, 'archive.error')})")


# --------------------------------------------------------------------------- #
# Per-session facts
# --------------------------------------------------------------------------- #
# One check per configuration rather than per request.  These are attributed to
# the surface whose flags the session exists to exercise, so "the port cannot be
# started with --tls-cert" is a finding against ``tls`` and not a fifteenth of a
# point spread across the suite.


def test_session_starts(session, run):
    """The configuration boots at all.

    A session that cannot start is a missing or renamed flag, and worth naming as
    one: every case in it is unanswerable, and reporting that as 25 body
    mismatches hides the single cause.
    """
    failures = replay.boot_failures(run)
    if session.id not in failures:
        return
    log = (failures[session.id] or "").strip()
    tail = log.splitlines()[-1] if log else "(no output)"
    pytest.fail(f"{session.id}: the submission could not be started with "
                f"{' '.join(session.argv)}\n  {tail}", pytrace=False)


def test_session_survives(session, actual, expected):
    """The final read of the session, which proves the process was still up.

    Recorded as a synthetic ``__alive__`` case at the end of every session.  A
    port that panics on the twentieth request answers the first nineteen
    correctly, and without this the report would say so and never mention that it
    died.
    """
    want = expected(session.id, "__alive__")
    if want is None:
        pytest.fail(f"{session.id}: State A recorded no liveness probe, which is "
                    f"a harness bug", pytrace=False)
    got = actual(session.id, "__alive__")
    assert got is not None and got.get("status") == want.get("status"), (
        f"{session.id}: expected a final {want.get('status')} on "
        f"{session.alive_path}, got "
        + (f"{got.get('status')}" if got else "nothing -- the process was gone"))


def test_session_tree(session, actual, expected):
    """Capture and graded run served the same tree.

    Two different claims depending on the session.

    For a read-only session this checks the *harness*: the tree is materialised
    from a pinned spec, nothing wrote to it, and if the digests disagree then
    every other difference in that session is meaningless.  Saying so once is
    worth more than a thousand body diffs.

    For a session that uploads it checks the *submission*, and it is the only
    assertion in the suite that looks at the filesystem rather than at responses:
    a port can return the baseline's exact 303 and write the file to the wrong
    place, under the wrong name, or with the wrong bytes.  Compared without
    mtimes, because creating a file sets its own and its parent's to the wall
    clock, so a timed digest would only ever compare two clocks.
    """
    got, want = actual(session.id, "__tree__"), expected(session.id, "__tree__")
    if not want:
        pytest.fail(f"{session.id}: State A recorded no tree digest, which is a "
                    f"harness bug", pytrace=False)
    if not got:
        pytest.fail(f"{session.id}: the run recorded no tree digest -- the "
                    f"session did not reach the end", pytrace=False)
    field = "untimed" if session.mutating else "digest"
    assert got.get(field) == want.get(field), (
        f"{session.id}: "
        + ("the uploads left a different set of files, or the same files with "
           "different contents"
           if session.mutating else
           "the served tree itself differs, which is a harness problem rather "
           "than a submission one"))


# --------------------------------------------------------------------------- #
# Self-check
# --------------------------------------------------------------------------- #


def axis_of(func) -> str | None:
    """Which axis a battery is run over, read from its signature."""
    import inspect
    params = set(inspect.signature(func).parameters)
    for name in AXES:
        if name in params:
            return name
    return "session" if "session" in params else None


def _imported_batteries(module_dir: Path) -> set[str]:
    """The battery names a module's test file imports, read without importing."""
    import ast
    names: set[str] = set()
    for path in sorted(module_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "battery":
                names.update(alias.name for alias in node.names)
    return names


def self_check(modules_dir: Path, golden: dict) -> list[str]:
    """Assert every module grades exactly the axes it has cases on.

    Both directions matter and both fail silently otherwise.

    A module with cases on an axis it does not import stops grading a whole
    surface while still reporting every check it does run as passing -- add an
    archive case to the ``tls`` session and, without this, nobody finds out that
    nothing ever unpacked it.

    A module that imports a battery whose axis is empty collects a test with an
    empty parameter set, which pytest reports as a skip and the runner scores as
    a miss.  So the module would lose points for a surface it has no cases on.
    """
    batteries = {name: obj for name, obj in globals().items()
                 if name.startswith("test_") and callable(obj)}
    by_axis: dict[str, list[str]] = {}
    for name, func in batteries.items():
        axis = axis_of(func)
        if axis is None:
            return [f"battery {name} declares no axis; it must take one of "
                    f"{sorted(AXES)} or ``session``"]
        by_axis.setdefault(axis, []).append(name)

    problems = []
    for module_id in sorted(routing.SURFACE_BY_ID):
        directory = modules_dir / module_id
        if not directory.is_dir():
            problems.append(f"{module_id}: no module directory at {directory}")
            continue
        imported = _imported_batteries(directory)
        unknown = imported - set(batteries)
        if unknown:
            problems.append(f"{module_id}: imports {sorted(unknown)} from "
                            f"battery, which does not define them")
        for axis, names in sorted(by_axis.items()):
            if axis == "session":
                population = len(routing.sessions_for(module_id))
            else:
                population = len(AXES[axis](module_id, golden))
            wanted = set(names) & imported
            if population and not wanted:
                problems.append(
                    f"{module_id}: has {population} case(s) on the {axis!r} axis "
                    f"but imports none of {sorted(names)}, so that surface is "
                    f"ungraded")
            if not population and wanted:
                problems.append(
                    f"{module_id}: imports {sorted(wanted)} but has no cases on "
                    f"the {axis!r} axis, so those checks collect empty and score "
                    f"as misses")
    return problems
