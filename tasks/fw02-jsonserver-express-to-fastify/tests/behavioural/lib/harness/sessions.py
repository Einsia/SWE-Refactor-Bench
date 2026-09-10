"""The session model.

httpbin was stateless, so a single long-lived server could answer a whole
corpus in any order. json-server is not: every POST, PUT, PATCH and DELETE
changes what the next request sees. Replay order therefore has to be part of
the ground truth, and a mutating group cannot share a server with anything else.

A **session** is one server configuration plus an ordered request sequence:

    Session(id, argv, seed, cases)

*   ``argv``  -- extra command line passed to the production entry point, so a
    session can grade an operational variant (``--read-only``, ``--delay 200``,
    ``--id _id``) without a second launcher.
*   ``seed``  -- which seed database to boot against. ``None`` means the one the
    repository ships. The ``_id``/``_ref`` variants need field names to match, so
    they derive their own.
*   ``cases`` -- the request sequence, replayed in list order.

Read-only sessions are marked ``mutating=False`` and share one boot; every
mutating session gets its own freshly seeded server. A boot costs a few hundred
milliseconds, which bounds the whole suite at well under a minute of server
startup even with fifteen sessions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Case:
    """One request, and how to compare the response to it."""

    id: str
    method: str = "GET"
    path: str = "/"
    #: JSON body, sent as application/json.
    json: Any = None
    #: Raw body, sent verbatim. Mutually exclusive with ``json``.
    data: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()
    #: Response headers to compare by value, beyond the always-compared set.
    headers_extra: tuple[str, ...] = ()
    #: Response headers this case must not compare (inherently per-run).
    headers_skip: tuple[str, ...] = ()
    #: Compare the decompressed body under this scheme rather than the raw bytes.
    decompress: str | None = None
    #: How the body is compared.
    #:
    #: ``"exact"``  -- byte for byte, after address and path scrubbing.
    #: ``"stack"``  -- the body embeds a stack trace naming Express's and Node's
    #:                 internals, which a Fastify port cannot reproduce and is
    #:                 not asked to. The frame lines are cut from both sides and
    #:                 everything around them is compared byte for byte, so the
    #:                 error class, the message and Express's HTML error page are
    #:                 all still graded. Never inferred; always set here.
    body_mode: str = "exact"
    #: Free-form note carried into the golden file, for diagnosis.
    note: str = ""


@dataclass(frozen=True)
class Session:
    """One server configuration and the request sequence it answers."""

    id: str
    cases: tuple[Case, ...]
    argv: tuple[str, ...] = ()
    seed: str | None = None
    mutating: bool = False
    env: tuple[tuple[str, str], ...] = ()
    note: str = ""


# ---------------------------------------------------------------------------
# Seed variants
# ---------------------------------------------------------------------------
# Two CLI options rename the fields the data layer keys on, so a session using
# them needs a seed whose field names match. These transforms are applied to the
# shipped seed at capture and at grading time, identically.

def seed_with_id_field(db: dict, id_field: str) -> dict:
    """Rename every ``id`` to ``id_field`` (for ``--id _id``)."""
    out = {}
    for key, value in db.items():
        if isinstance(value, list):
            rows = []
            for row in value:
                row = dict(row)
                if "id" in row:
                    row[id_field] = row.pop("id")
                rows.append(row)
            out[key] = rows
        else:
            out[key] = value
    return out


def seed_with_fk_suffix(db: dict, suffix: str) -> dict:
    """Rename ``<x>Id`` foreign keys to ``<x><suffix>`` (for --foreignKeySuffix)."""
    out = {}
    for key, value in db.items():
        if isinstance(value, list):
            rows = []
            for row in value:
                new = {}
                for k, v in row.items():
                    if k.endswith("Id") and k != "id":
                        new[k[:-2] + suffix] = v
                    else:
                        new[k] = v
                rows.append(new)
            out[key] = rows
        else:
            out[key] = value
    return out


#: name -> callable(db) -> db. Referenced by ``Session.seed``.
SEED_VARIANTS = {
    "underscore-id": lambda db: seed_with_id_field(db, "_id"),
    "underscore-fk": lambda db: seed_with_fk_suffix(db, "_id"),
}
