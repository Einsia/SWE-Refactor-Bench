#!/usr/bin/env python3
"""Which build system the submission delivered, and what that licenses.

Stage 2 measures *preserved capability*: everything State A's build system can
do, the submission's build system must still do.  It does not measure the
migration itself -- `autotools_retired` and `cmake_is_the_build` in stage 1 ask
that, over both trees, with a reviewer.  The separation matters because State A
is the oracle every expectation in ``data/`` was recorded from, and a stage whose
own oracle scores zero is measuring the wrong thing.

Two consequences, and this module is both of them:

``FLAVOUR``
    "cmake" or "autotools", read from the ledger the `build` module wrote (or
    from the delivered tree, when a module runs before it).  A check that has to
    spell a capability differently per build system asks here.

``only(...)``
    A licensed skip for a check that can only exist under one build system --
    ``find_package`` has no Autotools spelling, ``SODIUM_BUILD_TESTS`` has no
    ``configure`` switch.  Licensed, not weighted to zero: the check stays scored
    for the build system it applies to, and leaves the pool entirely for the one
    it does not.  That is what "not applicable" means, and it is why the pass
    rate of a submission is not diluted by the other flavour's surface.

A check that fails for State A because State A *has* something State B must not
(a ``.la`` archive, a ``config.status`` in the build tree) is not gated here --
it is a removal requirement, it belongs to stage 1's gates, and it carries
``srb_weight(0.0)`` where it is still worth reading in the report.
"""

from __future__ import annotations

import functools
import os

import pytest

import builder

FLAVOUR = builder.delivered_flavour()

CMAKE = "cmake"
AUTOTOOLS = "autotools"


def is_cmake() -> bool:
    return FLAVOUR == CMAKE


def only(*flavours: str, reason: str = ""):
    """Skip -- licensed -- unless the delivered build system is one of these."""
    why = reason or (
        "the delivered build system is %s; this check measures an interface only "
        "%s has" % (FLAVOUR, "/".join(flavours)))

    def deco(fn):
        @pytest.mark.srb_skip_ok
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if FLAVOUR not in flavours:
                pytest.skip(why)
            return fn(*a, **kw)
        return wrapper
    return deco


def skip_unless(flavour: str, reason: str = ""):
    """Marker form of :func:`only`, for parametrised checks."""
    return pytest.mark.skipif(FLAVOUR != flavour,
                              reason=reason or "not applicable to " + FLAVOUR)


#: What the four library-shape switches are called in each build system.  A
#: value of None means the build system has no switch for that capability, which
#: is a fact about the build system rather than a defect in the submission.
SWITCHES = {
    CMAKE: {
        "minimal": "SODIUM_MINIMAL",
        "shared": "SODIUM_BUILD_SHARED",
        "static": "SODIUM_BUILD_STATIC",
        "tests": "SODIUM_BUILD_TESTS",
    },
    AUTOTOOLS: {
        "minimal": "--enable-minimal",
        "shared": "--enable-shared",
        "static": "--enable-static",
        # `make` does not build the test programs at all; `make check` does.  The
        # capability "install the library without building tests" exists, but not
        # as a configure switch, so there is nothing to read a default off.
        "tests": None,
    },
}

#: Capability -> default, as the delivered build system ships it.
DEFAULTS = {"minimal": "OFF", "shared": "ON", "static": "ON", "tests": "ON"}


def switch(capability: str):
    return SWITCHES[FLAVOUR].get(capability)


def switchable():
    """Capabilities the delivered build system exposes as a real option."""
    return [c for c in ("minimal", "shared", "static", "tests")
            if SWITCHES[FLAVOUR].get(c)]


if os.environ.get("SRB_FLAVOUR_DEBUG"):
    print("flavour: %s, switchable=%s" % (FLAVOUR, switchable()))
