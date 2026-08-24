#!/usr/bin/env python3
"""Build-time check that State A installs offline and answers through the driver.

The most important assertion in this stage, and the reason it is worth a minute of
build time.

Every break here is "passes on the original, fails on the submission".  So
anything that makes the ORIGINAL fail makes every candidate fail on it, and a
candidate that fails on the original is discarded as broken -- six rounds of
discarded candidates read as "six rounds found nothing", which pays the
submission the whole 60 points.  The failure is silent and it is scored in the
submission's favour, which is the worst combination available.

Three things could cause it and all three are knowable at build time: the offline
cache does not satisfy State A's lockfile, the consumer directory does not resolve
a CommonJS package, or the driver does not run under this Node.  So State A is
driven here through the real `srbstylus` -- the real `npm ci --offline`, the real
`node_modules/stylus` symlink, the real `import('stylus')`, the real `bin/stylus`
-- and the build fails if any of it does not answer.

Run with SRB_TARGET pointing at an unpacked State A.  Not shipped as a runtime
capability: nothing imports this, and the tree it checks is deleted after it runs.
"""
from __future__ import annotations

import sys

import srbstylus


def main() -> int:
    t = srbstylus.tree()  # npm ci --offline, then the consumer directory

    r = t.version()
    assert r.ok and r.value["version"] == srbstylus.VERSION, f"version: {r}"

    # The synchronous face, which is what §1.5 asks a submission to preserve.
    r = t.render("a\n  color red\n")
    assert r.ok, f"render failed on State A: {r}"
    assert r.css == "a {\n  color: #f00;\n}\n", f"render: {r.css!r}"
    assert r.was_promise is False, "State A's synchronous face returned a Promise"

    r = t.render_callback("a\n  width 10px + 5px\n")
    assert r.ok and r.sync is True, f"callback was not synchronous on State A: {r}"
    assert r.css == "a {\n  width: 15px;\n}\n", f"callback css: {r.css!r}"

    # The built-in library, reached with no explicit import.  §1.4 lets a
    # submission relocate it; an install that unpacked but lost `functions/`
    # renders the call as a literal rather than failing, so this is checked by
    # what comes out.
    r = t.render("a\n  color rgba(255,0,0,0.5)\n")
    assert r.ok and "rgba" in r.css, f"built-ins unreachable on State A: {r}"

    # The dialect filter.  Measured: `arguments` and `caller` are own properties
    # of a sloppy-mode function, so they are on the CJS original and impossible on
    # any correct ESM submission.  One of them reaching the surface would hand
    # every round a free break against a submission that did what §1.5 required.
    surface = t.api_surface()
    assert surface.ok, f"api_surface: {surface}"
    for banned in ("arguments", "caller", "length", "name", "prototype"):
        assert banned not in surface.keys, (
            f"{banned!r} reached the published surface; the dialect filter in "
            "candidate-driver.mjs is not doing its job"
        )

    out = t.cli("--version")
    assert out.ok, f"cli --version: {out}"
    assert out.text().strip() == srbstylus.VERSION, f"cli --version: {out}"

    # A compile error must arrive as a diagnosis and nothing else: the staged path
    # in it is `SRB_TARGET_TOKEN`, which differs between the two trees by
    # construction, and the frames name internals §1.5 requires to change.
    p = t.project({"bad.styl": "a\n  b {{{\n"})
    out = t.cli("bad.styl", cwd=p)
    assert out.returncode == 1, f"cli should exit 1 on a compile error: {out}"
    err = out.err_text()
    assert str(t.root) not in err, f"the staged tree path reached CLI stderr: {err!r}"
    assert "Node.js v" not in err, f"the Node version footer reached stderr: {err!r}"
    assert "throw err" not in err, f"the throw site reached stderr: {err!r}"
    assert out.diagnostic(), f"no diagnosis survived the stripping: {err!r}"

    print("State A OK through srbstylus: install, both faces, built-ins, CLI, errors")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FATAL: State A does not answer: {exc}", file=sys.stderr)
        print(
            "Every candidate in this stage would fail on the original, which "
            "reads as 'nobody found anything' and pays the submission 60 points.",
            file=sys.stderr,
        )
        sys.exit(1)
