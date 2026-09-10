"""Is the capability object really the only door the core has?

Every other module in this suite asks whether the submission produces the right
CSS.  This one asks whether it produces it the way the port requires, and it does
so without reading a line of the submission's source: each check takes a
capability away, or moves one, and then looks at what the compiler did.

That is the distinction worth keeping.  "Does `src/core/` contain the string
`readFileSync`" is a question about the text, it belongs to the review in
`tests/audit/`, and it is answerable by spelling the call differently.
"Withdraw the mount the built-in library lives at and see whether `abs(-5px)`
still compiles" is a question about behaviour, and there is no way to spell your
way past it: either the core went through `platform.readFile` to get that
library, in which case removing it has an effect, or it did not.

The four probes:

  * withdraw `runtimeRoot` -- a core that inlined the `.styl` built-ins keeps
    working and never attempts a read, which means a web host could not supply
    its own copy;
  * mount the same logical import at two different virtual paths -- a core
    reading through `platform` follows both, one reading the real disk behind the
    VFS follows neither;
  * compile a selector generated microseconds ago -- a table of recorded answers
    has no entry for it;
  * call `renderSync()` on a platform that declares no synchronous read -- it has
    to refuse, because a port that manages it anyway is doing blocking IO through
    a door that is not the capability object.

All four run in the realm, which has no Node in it at all: the realm rejects a
bare specifier, so a core that needs one cannot even link.  What the checks add
is the cases where linking succeeds and the dependency was smuggled in some other
way.

**Two of the four have no subject before the port.**  They are two of the three
such checks in the stage, not all of it: the third is `paths` / the port's own path
module being reachable as an export, and `suite.toml` lists all three together with
the gate each defers to.  It is worth being exact about which two are here and why,
because the rest of the stage measures behaviour that a platform port must not
change and is therefore asked of whichever face the tree presents
(`harness.layout.face`).

  * The middle two are behavioural either way.  Reading through the mount rather
    than the disk is a property State A has -- it resolves an `@import` by reading
    the file at the path it was given, and mounting the same import at two
    unrelated prefixes shows it following both.  Computing the output from the
    input likewise.  Both stay scored on both faces.
  * Withdrawing `runtimeRoot` and refusing an async platform are not.  Before the
    port there is no capability object, so there is nothing to withdraw: the mount
    is how a path is *named*, not how the bytes are reached, and taking it away
    would change what the access log calls a file rather than whether the file was
    read.  Nor is there a platform to declare `sync: false` -- upstream reads the
    disk synchronously and always did, which is the behaviour §1.3 exists to keep.
    A check with no subject cannot pass or fail honestly, so on that face these two
    keep a real verdict at weight 0.0 and name the stage-1 gate that owns the
    question instead.  On a ported tree both are scored in full.
"""
from __future__ import annotations

import os

import pytest

from harness import layout, runners, vfs
from srbstylus import require_core

#: Needs `abs()`, which State A defines in lib/functions/index.styl -- so it can
#: only be answered by a core that actually loaded the built-in library.
NEEDS_BUILTIN_STYL = "a\n  width abs(-5px)\n"

#: Needs only the JS built-ins, which live in the compiler itself.
NEEDS_NOTHING = "a\n  color red\n"


def _project_only_vfs() -> dict[str, str]:
    """The inputs, with nothing mounted at runtimeRoot."""
    return {p: d for p, d in vfs.full().items() if not p.startswith(layout.VRUNTIME)}


@pytest.mark.srb_weight(3)
def test_builtin_styl_library_is_loaded_through_the_platform():
    """Withdraw runtimeRoot and the `.styl` built-ins must stop working.

    A port that pasted `index.styl` into a JavaScript string literal passes every
    other module in this suite and is still un-hostable on the web, because a host
    that wanted to supply its own copy of the built-in library could not.  The
    check is not "is there a string literal in the source" -- it is "does removing
    the file change the answer".

    Asked of the pre-migration face too, and charged there.  Upstream finds its
    library through `__dirname`, so there is no capability object to withdraw it
    from and the mount names the path rather than supplying the bytes -- which is
    the absence §1.4 asks the port to close, not a question this row declines to
    put.  See `paths/test_paths.py::test_core_exports_path_helpers` for why a row
    like this is charged on that face rather than weighted to 0.0.
    """
    require_core()
    empty_runtime = runners.sandbox(
        [{"id": "b", "kind": "render", "source": NEEDS_BUILTIN_STYL, "options": {}},
         {"id": "n", "kind": "render", "source": NEEDS_NOTHING, "options": {}}],
        files=_project_only_vfs(),
        sync=False,
    )
    with_runtime = runners.sandbox(
        [{"id": "b", "kind": "render", "source": NEEDS_BUILTIN_STYL, "options": {}}],
        files=vfs.full(),
        sync=False,
    )

    # An assertion, not a `permitted_skip`, for the reason `test_options.py`
    # states in full: a skip is neutral in the denominator as well as the
    # numerator, so excusing this row when the submission cannot compile at all
    # would *remove* weight 3 from a port for being more broken -- a core that
    # fails to import would take this branch and leave `platform` scored out of 9
    # instead of 12.  `permitted_skip` is for the oracle failing to state an
    # expectation, never for the submission failing to meet one.
    #
    # It does mean a core that cannot use built-ins loses this row and the
    # `builtins` module both.  That is the right way round: the row asks whether
    # withdrawing the file changes the answer, and a port that cannot compile
    # `abs(-5px)` with the library mounted has not shown that it loads the library
    # -- it has shown the opposite.
    assert with_runtime.results["b"].ok, (
        f"the core cannot compile {NEEDS_BUILTIN_STYL!r} even with the built-in "
        "library mounted at platform.runtimeRoot, so it has not demonstrated that "
        "it loads the library through the platform at all:\n"
        f"  {(with_runtime.results['b'].error or {}).get('name')}: "
        f"{((with_runtime.results['b'].error or {}).get('message') or '')[:300]}"
    )

    attempted = [e for e in empty_runtime.access_log if layout.VRUNTIME in e]
    r = empty_runtime.results["b"]
    assert (not r.ok) or attempted, (
        "with nothing mounted at platform.runtimeRoot the core still compiled "
        f"{NEEDS_BUILTIN_STYL!r} to {r.css!r}, without ever attempting a read "
        "under runtimeRoot. The built-in library has to be loaded through "
        "platform.readFile; this copy is inlined, so a web host cannot supply "
        f"its own.\nAccess log: {list(empty_runtime.access_log)[:15]}"
    )


@pytest.mark.srb_weight(3)
def test_reads_follow_the_mount_rather_than_the_disk():
    """The core must read what it is given, not the real tree behind it.

    The same logical import is mounted at two different virtual paths.  A core
    reading through `platform` sees both move; one quietly using the host
    filesystem, or a cached copy of the inputs, sees neither.
    """
    require_core()
    body = "a\n  color red\n"
    first = runners.sandbox(
        [{"id": "r", "kind": "render", "source": "@import 'p'\n",
          "options": {"filename": "/proj/one/main.styl", "paths": ["/proj/one"]}}],
        files=vfs.text({"/proj/one/main.styl": "@import 'p'\n", "/proj/one/p.styl": body}),
        sync=False,
    )
    second = runners.sandbox(
        [{"id": "r", "kind": "render", "source": "@import 'p'\n",
          "options": {"filename": "/elsewhere/deep/main.styl", "paths": ["/elsewhere/deep"]}}],
        files=vfs.text({"/elsewhere/deep/main.styl": "@import 'p'\n",
                        "/elsewhere/deep/p.styl": body}),
        sync=False,
    )
    for label, batch, marker in (("first", first, "/proj/one"),
                                 ("second", second, "/elsewhere/deep")):
        assert batch.results["r"].ok, (
            f"the {label} compile failed: {batch.results['r'].error}"
        )
        touched = [e for e in batch.access_log if marker in e]
        assert touched, (
            f"the {label} compile produced CSS without reading anything under "
            f"{marker}, so it did not resolve the @import through the platform "
            "object it was handed.\nAccess log:\n  "
            + "\n  ".join(batch.access_log[:15])
        )
    assert first.results["r"].css == second.results["r"].css, (
        "the same stylesheet compiled differently at two mount points:\n"
        f"  /proj/one:       {first.results['r'].css!r}\n"
        f"  /elsewhere/deep: {second.results['r'].css!r}"
    )


@pytest.mark.srb_weight(3)
def test_output_is_computed_from_the_input():
    """Output must be computed, not recognised.

    A table keyed by input hash answers the whole recorded corpus perfectly and
    fails here, because this selector is generated at verify time and is in no
    table anyone could have shipped.
    """
    require_core()
    nonce = os.urandom(8).hex()
    src = f".c-{nonce}\n  color red\n  padding 1px 2px\n"
    batch = runners.sandbox(
        [{"id": "r", "kind": "render", "source": src, "options": {}}],
        files=vfs.full(),
        sync=False,
    )
    r = batch.results["r"]
    assert r.ok, f"compiling a freshly generated selector failed: {r.error}"
    assert nonce in (r.css or ""), (
        f"the nonce {nonce!r} does not appear in the output {r.css!r}; whatever "
        "produced that CSS, it was not the input this call was given."
    )


@pytest.mark.srb_weight(3)
def test_render_sync_refuses_a_platform_without_sync():
    """`renderSync()` needs a synchronous read, and must refuse without one.

    A port that returns CSS anyway found blocking IO somewhere other than the
    capability object it was handed, which is the whole thing the injection is
    for.  The second half checks the refusal is legible: an Error whose message
    does not say why leaves a host guessing.

    Asked of the pre-migration face too, and charged there.  Upstream reads the disk
    synchronously in every case and has no platform to declare `sync: false` on, so
    it returns CSS here; §1.3 asks the port to keep `renderSync()` working *and* to
    take its reads from the injected platform, and a tree that has only the first
    half has answered half the question.  See
    `paths/test_paths.py::test_core_exports_path_helpers` for why a row like this is
    charged on that face rather than weighted to 0.0.
    """
    require_core()
    async_batch = runners.sandbox(
        [{"id": "s", "kind": "render", "source": NEEDS_NOTHING, "options": {},
          "sync": True}],
        files=vfs.full(),
        sync=False,
    )
    r = async_batch.results["s"]
    assert not r.ok, (
        f"renderSync() returned {r.css!r} on a platform whose `sync` flag is "
        "false. With no synchronous read available it has to throw; returning "
        "CSS means it did blocking IO some other way."
    )
    msg = ((r.error or {}).get("message") or "").lower()
    assert any(w in msg for w in ("sync", "async", "platform")), (
        "renderSync() on an async platform threw, but the message does not say "
        f"why: {(r.error or {}).get('message')!r}. A host that hands in an async "
        "platform and gets an unexplained Error cannot tell what to fix."
    )
