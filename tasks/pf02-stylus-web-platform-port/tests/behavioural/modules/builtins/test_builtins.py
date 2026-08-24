"""Every built-in Stylus ships, probed one at a time.

The corpus already exercises most of these, but it reports at the granularity of
a case: `bifs.styl` failing tells you something in the built-in library is wrong,
not which function.  These probes each isolate one function, so a submission that
ported 69 of the 70 JavaScript built-ins loses one check rather than a case -- and
the failure message names the function and shows both outputs.

Three groups, because they need different setup:

  * plain probes compile with nothing but a filename;
  * `io:` probes read the corpus fixtures (`image-size` parsing real PNG/GIF/JPEG
    bytes, `json()` reading real files) -- this is where the port has to deliver
    binary through `platform.readFile` with no `Buffer` to put it in;
  * `url:` probes install the `url()` embedding plugin, whose whole job upstream
    is `fs.readFileSync` plus `Buffer.prototype.toString('base64')`, and which the
    port has to rebuild out of the capability object and its own base64.

`p()` and `warn()` return null and exist only for what they print, so they are
compared on their console output instead of their CSS.
"""
from __future__ import annotations

import pytest

from harness import builtinfns as B
from harness import layout, runners
from srbstylus import permitted_skip, require_core

#: Where a probe pretends to live.  Same virtual filename for every probe, so
#: `@import` and lookup-path resolution behave the way the corpus expects.
_PROBE_FILENAME = f"{layout.VPROJ}/test/cases/probe.styl"
_LOOKUP_PATHS = [f"{layout.VPROJ}/test/cases"]
_INCLUDES = [f"{layout.VPROJ}/test/images", f"{layout.VPROJ}/test/cases/import.basic"]


def _ops() -> list[dict]:
    """One op per probe, described once for both runners."""
    ops = []
    for pid, src in B.probes():
        op = {
            "id": pid,
            "kind": "render",
            "source": src,
            "set": {"filename": _PROBE_FILENAME, "paths": _LOOKUP_PATHS},
            "include": list(_INCLUDES),
            "options": {},
        }
        cfg = B.url_config(pid)
        if cfg is not None:
            options, define_name = cfg
            op["defineUrl"] = True
            op["urlName"] = define_name
            # `paths` is where the plugin looks for the file, independently of the
            # stylesheet's own lookup paths.
            op["urlOptions"] = {"paths": [f"{layout.VPROJ}/test/images"], **options}
        ops.append(op)
    return ops


@pytest.fixture(scope="module")
def expected():
    return runners.oracle(_ops())


@pytest.fixture(scope="module")
def actual():
    require_core()
    return runners.sandbox(_ops())


# ------------------------------------------------------------------ the sweep


def test_core_loaded_for_builtin_probes(actual):
    """Nothing below can pass if the graph did not link."""
    assert actual.load_error is None, (
        "src/core failed to load in the web realm:\n"
        f"{actual.load_error.get('name')}: {actual.load_error.get('message')}"
    )


@pytest.mark.parametrize("pid", B.probe_ids())
def test_builtin_matches_state_a(pid, expected, actual):
    exp = expected.get(pid)
    if not exp.ok:
        # A probe the oracle cannot compile is a harness defect, not a
        # submission defect: there is no expectation to compare against.
        permitted_skip(f"oracle could not compile probe {pid}: {(exp.error or {}).get('message')}")

    got = actual.get(pid)
    assert got.ok, (
        f"{pid}: src/core raised where State A produced CSS\n"
        f"  source: {B.source_for()[pid]!r}\n"
        f"  {(got.error or {}).get('name')}: {(got.error or {}).get('message')}"
    )
    assert got.css == exp.css, (
        f"{pid}: output differs from State A\n"
        f"  source:   {B.source_for()[pid]!r}\n"
        f"  state-a:  {_trim(exp.css)}\n"
        f"  src/core: {_trim(got.css)}"
    )


def _trim(css: str | None, limit: int = 400) -> str:
    if css is None:
        return "<none>"
    one = " | ".join(line.strip() for line in css.strip().splitlines())
    return one if len(one) <= limit else f"{one[:limit]}... ({len(one)} chars)"


# ------------------------------------------------- the two console-only builtins


@pytest.mark.parametrize("pid", B.PRINTS_UNCOMPARABLE_IDS)
def test_tracing_builtin_still_prints(pid, expected, actual):
    """`trace()` dumps the frame stack, naming absolute filenames.

    The oracle's frames carry real paths and the realm's carry virtual ones, so
    the text cannot match and is not compared. That it prints at all is still
    worth a check: a port that stubbed `trace()` to a no-op would pass the CSS
    comparison, since it returns null either way.
    """
    exp, got = expected.get(pid), actual.get(pid)
    if not exp.ok or not exp.console:
        permitted_skip(f"State A printed nothing for {pid}; nothing to require")

    assert got.ok, f"{pid}: src/core raised: {(got.error or {}).get('message')}"
    assert got.console, (
        f"{pid}: State A printed {len(exp.console)} console line(s) and src/core printed none. "
        "The built-in returns null, so this is the only thing that distinguishes it from a stub."
    )


@pytest.mark.parametrize("pid", B.CONSOLE_ONLY_IDS)
def test_console_only_builtin_prints_what_state_a_prints(pid, expected, actual):
    """`p()` and `warn()` return null; their output is the console line.

    A port that dropped them would still pass the CSS comparison above, because
    `type(p(1 2))` is `'null'` either way.
    """
    exp, got = expected.get(pid), actual.get(pid)
    if not exp.ok:
        permitted_skip(f"oracle could not compile probe {pid}")
    if not exp.console:
        permitted_skip(f"State A printed nothing for {pid}; nothing to compare")

    assert got.ok, f"{pid}: src/core raised: {(got.error or {}).get('message')}"
    assert list(got.console) == list(exp.console), (
        f"{pid}: console output differs from State A\n"
        f"  state-a:  {list(exp.console)}\n"
        f"  src/core: {list(got.console)}"
    )


# ------------------------------------------------------- families as a whole
#
# The per-probe checks above localise a fault.  These say something the
# individual rows cannot: that a whole capability is present rather than a
# majority of it.


def test_javascript_builtins_all_present(expected, actual):
    """A port cannot skip a built-in and stay a drop-in replacement."""
    _assert_family("JavaScript built-ins", B.js_ids(), expected, actual)


def test_styl_builtin_library_all_present(expected, actual):
    """§1.4: the `.styl` half of the library, fetched through platform.readFile.

    These fail together if the library was not shipped or not loaded, which is a
    different fault from a single wrong function.
    """
    _assert_family("`.styl` built-in library", B.styl_ids(), expected, actual)


def test_binary_reading_builtins_all_present(expected, actual):
    """`image-size()` on real PNG, GIF, JPEG and SVG bytes.

    The port has no `Buffer`, so these are the checks that say the replacement
    byte handling actually parses the same headers.
    """
    _assert_family("binary-reading built-ins", B.io_ids(), expected, actual)


def test_url_embedding_all_present(expected, actual):
    """The `url()` plugin: base64, utf8, the size limit and every pass-through."""
    _assert_family("url() embedding", B.url_ids(), expected, actual)


def _assert_family(label: str, ids: tuple[str, ...], expected, actual) -> None:
    comparable = [pid for pid in ids if expected.get(pid).ok]
    if not comparable:
        permitted_skip(f"oracle compiled none of the {label} probes")

    wrong = []
    for pid in comparable:
        got = actual.get(pid)
        if not got.ok:
            wrong.append(f"{pid}: raised {(got.error or {}).get('message')!r}")
        elif got.css != expected.get(pid).css:
            wrong.append(f"{pid}: wrong output")
    assert not wrong, (
        f"{len(wrong)} of {len(comparable)} {label} probes disagree with State A:\n  "
        + "\n  ".join(wrong[:25])
    )


# ------------------------------------------------------- harness self-check


@pytest.mark.harness
def test_probe_catalog_covers_state_a_registry():
    """Every built-in State A registers is either probed or explicitly excluded.

    A harness self-check, not a judgement on the submission: it reads the registry
    out of the oracle at verify time so the catalog cannot quietly fall behind the
    pinned upstream. Two names are excluded by decision -- `error()`, which exists
    to throw and is measured in the errors dimension, and `use()`, which loads a
    JavaScript plugin from disk and has no web equivalent.
    """
    batch = runners.oracle([{"id": "registry", "kind": "builtinRegistry"}])
    reg = batch.get("registry")
    if not reg.ok:
        permitted_skip(f"could not read State A's registry: {(reg.error or {}).get('message')}")

    all_sources = "\n".join(B.source_for().values())
    excluded = set(B.UNPORTABLE_IDS) | {"error"}

    def probed(name: str) -> bool:
        import re
        esc = re.escape(name)
        return bool(
            re.search(rf"(^|[^\w-]){esc}\s*\(", all_sources)
            or re.search(rf"(^|[^\w-]){esc}($|[^\w-])", all_sources)
        )

    missing = sorted(
        n for n in (reg.value.get("js") or []) + (reg.value.get("styl") or [])
        if n not in excluded and not probed(n)
    )
    assert not missing, (
        f"{len(missing)} built-in(s) that State A registers have no probe, so the "
        f"suite cannot see whether the port implements them: {missing}"
    )


# ------------------------------------------------------------------ provenance


def test_builtin_library_came_through_the_capability_object(actual):
    """The `.styl` half has to be read, not inlined into a string literal.

    §1.4.  A submission that pasted `index.styl` into JavaScript would answer
    every probe above correctly and still be un-hostable, because a host that
    mounts its own runtime root would be ignored.
    """
    hits = [e for e in actual.access_log if layout.VRUNTIME in e]
    assert hits, (
        f"no read under platform.runtimeRoot ({layout.VRUNTIME}) across "
        f"{len(B.probe_ids())} built-in probes. The built-in .styl library was not "
        "loaded through the capability object."
    )


def test_image_probes_read_the_image_files(actual):
    """`image-size()` answering without a read means the answers were baked in."""
    reads = [e for e in actual.access_log if "/test/images/" in e]
    assert reads, (
        "image-size() and url() probes produced answers without reading anything "
        f"from {layout.VPROJ}/test/images. The dimensions did not come from the files."
    )
