"""The profile model.

fw02's oracle was one long-lived server, because json-server's behaviour is
fixed once it boots. ChartMuseum's is not. `--depth`, `--depth-dynamic`,
`--context-path`, `--disable-api`, `--disable-delete`, `--basic-auth-*`,
`--cors-alloworigin`, `--enable-metrics`, `--max-upload-size`, `--per-chart-limit`,
`--max-storage-objects`, `--allow-overwrite`, `--web-template-path`,
`--artifact-hub-repo-id`, `--chart-url` and `--enforce-semver2` each change which
routes exist, how a URL is split into (repo, params), which status a request
gets, or what the response body contains. A single launch can only witness one
point in that space.

A **profile** is therefore one server launch plus an ordered request sequence:

    Profile(id, flags, seeds, cases)

*   ``flags`` -- the command line, minus --port/--storage/--storage-local-rootdir,
    which the launcher owns. Passed byte-identically to the oracle and to the
    submission, which is what makes the comparison a fair one.
*   ``seeds`` -- ``dest=chart[,chart...]`` specs. Charts are COPIED from the
    frozen bytes under ``$CM_ORACLE_CHARTS``, never repackaged: `helm package`
    embeds mtimes and gzip framing, and ``index.yaml`` carries the digest of
    those exact bytes, so repackaging would make every digest disagree for a
    reason that has nothing to do with the migration.
*   ``cases`` -- the request sequence, replayed in list order.

Every profile gets its own freshly seeded storage directory -- sharing one
between profiles would make a write in the first change what the second sees,
and the flags differ anyway, so there is nothing to gain. ``mutating`` records
that a profile's own requests could change state, which is what makes its case
order part of the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Body comparison modes. Chosen per case, never inferred.
#:
#: ``exact``     -- byte for byte after scrubbing.
#: ``index``     -- an index.yaml. Compared as normalised YAML with the
#:                  ``generated`` timestamp masked; ``digest``, ``created``,
#:                  ``urls`` and entry order are all still compared, because they
#:                  are what a Helm client keys its cache on.
#: ``json``      -- parsed and compared as data, with volatile fields masked.
#: ``metrics``   -- Prometheus exposition. Metric names, label sets and help/type
#:                  lines are compared; sample values are not, since they depend
#:                  on how many requests the capture happened to make.
#: ``prefix``    -- the first N bytes only, for bodies that embed a Go error
#:                  string whose wording a port is not asked to reproduce.
#: ``len``       -- length and content type only, for binary payloads.
#: ``ignore``    -- status and headers are graded, body is not.
BODY_MODES = ("exact", "index", "json", "metrics", "prefix", "len", "ignore")


@dataclass(frozen=True)
class Case:
    """One request, and how its response is compared."""

    id: str
    method: str = "GET"
    path: str = "/"
    #: Raw request body, sent verbatim.
    data: bytes | None = None
    #: ``(field, chart)`` pairs assembled into a multipart/form-data body, where
    #: ``chart`` is a path under the frozen chart root.
    multipart: tuple[tuple[str, str], ...] = ()
    #: A frozen chart path, sent as the raw body (the classic push route).
    upload: str | None = None
    #: Extra request headers.
    headers: tuple[tuple[str, str], ...] = ()
    #: Basic auth credentials, as ``user:pass``.
    auth: str | None = None
    body_mode: str = "exact"
    #: For ``body_mode="prefix"``: how many bytes to compare.
    prefix_len: int = 0
    #: Response headers compared by value beyond the always-compared set.
    headers_extra: tuple[str, ...] = ()
    #: Response headers this case must not compare (inherently per-run).
    headers_skip: tuple[str, ...] = ()
    #: Free-form note carried into the golden file, for diagnosis.
    note: str = ""

    def __post_init__(self) -> None:
        if self.body_mode not in BODY_MODES:
            raise ValueError(f"{self.id}: unknown body_mode {self.body_mode!r}")
        if self.body_mode == "prefix" and self.prefix_len <= 0:
            raise ValueError(f"{self.id}: body_mode='prefix' needs prefix_len")
        given = [x for x in (self.data, self.multipart or None, self.upload) if x]
        if len(given) > 1:
            raise ValueError(f"{self.id}: data, multipart and upload are exclusive")


@dataclass(frozen=True)
class Profile:
    """One server launch and the request sequence it answers."""

    id: str
    cases: tuple[Case, ...]
    #: Command line, minus the flags the launcher owns.
    flags: tuple[str, ...] = ()
    #: ``dest=chart[,chart...]``; dest "" means the storage root (depth 0).
    seeds: tuple[str, ...] = ()
    #: This profile's requests *could* change server state, so replay order is
    #: part of the contract. Set from the requests alone -- a chart body, or a
    #: DELETE of a chart -- never from the expected response, because a profile
    #: whose writes are all expected refusals still needs private storage: if a
    #: submission fails to refuse one, the write lands, and every later case
    #: would then be graded against corrupted storage.
    mutating: bool = False
    #: Speak https and skip certificate verification. The launcher owns
    #: ``--tls-cert``/``--tls-key`` for these, pointing both sides at the same
    #: frozen self-signed pair, so ``flags`` never names a certificate path.
    tls: bool = False
    #: Why this profile exists. Ends up in the golden file and in failure output.
    note: str = ""


def case_key(profile_id: str, case_id: str) -> str:
    return f"{profile_id}::{case_id}"
