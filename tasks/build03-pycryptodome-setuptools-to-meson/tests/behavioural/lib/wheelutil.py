"""Read a wheel the way an installer reads it.

A wheel is the deliverable, so the questions here are an installer's questions:
what is the filename claiming, what will land on disk, does RECORD describe what
is actually in the archive, and what does the metadata say.

One asymmetry is deliberate and is documented on `PACKAGING_ONLY`. State A's
wheel was written by bdist_wheel, which adds `top_level.txt` and copies the
license files into `.dist-info/`. A different backend is not required to produce
those, and a submission that does not is not shipping less library -- it is
shipping the same library through a tool with different conventions. So the
member comparison downstream is over the *payload*, and the three packaging-only
names are excluded by name, listed here, once.
"""

from __future__ import annotations

import base64
import csv
import email
import hashlib
import io
import re
import zipfile
from pathlib import Path

#: `{distribution}-{version}-{python}-{abi}-{platform}.whl`, PEP 427.
WHEEL_NAME = re.compile(
    r"^(?P<distribution>[^-]+)-(?P<version>[^-]+)"
    r"(?:-(?P<build>\d[^-]*))?"
    r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$"
)

#: `.dist-info` members that are the packaging tool's convention rather than the
#: project's content. Excluded from set comparisons by name, never by pattern:
#: a glob over `.dist-info/*` would also excuse a missing METADATA.
PACKAGING_ONLY = frozenset({"top_level.txt", "AUTHORS.rst", "LICENSE.rst"})


class Wheel:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path)
        self.names = sorted(self.zip.namelist())

    # -- what the filename claims ------------------------------------------- #

    @property
    def tags(self) -> dict:
        m = WHEEL_NAME.match(self.path.name)
        if not m:
            raise AssertionError(f"{self.path.name} is not a PEP 427 wheel filename")
        return m.groupdict()

    @property
    def dist_info(self) -> str:
        roots = {n.split("/")[0] for n in self.names if n.split("/")[0].endswith(".dist-info")}
        if len(roots) != 1:
            raise AssertionError(f"expected exactly one .dist-info, found {sorted(roots)}")
        return roots.pop()

    # -- what will land on disk --------------------------------------------- #

    @property
    def payload(self) -> list[str]:
        """Every member that is the project, not the packaging."""
        di = self.dist_info
        return sorted(n for n in self.names if not n.startswith(di + "/") and not n.endswith("/"))

    @property
    def dist_info_payload(self) -> list[str]:
        di = self.dist_info
        return sorted(
            n for n in self.names
            if n.startswith(di + "/") and not n.endswith("/")
            and n.split("/")[-1] not in PACKAGING_ONLY
        )

    def by_suffix(self, suffix: str) -> list[str]:
        return sorted(n for n in self.payload if n.endswith(suffix))

    # -- metadata ----------------------------------------------------------- #

    def text(self, member: str) -> str:
        return self.zip.read(member).decode("utf-8", "replace")

    @property
    def metadata(self):
        return email.message_from_string(self.text(f"{self.dist_info}/METADATA"))

    @property
    def wheel_metadata(self):
        return email.message_from_string(self.text(f"{self.dist_info}/WHEEL"))

    def fields(self) -> dict:
        """METADATA as a lowercase-keyed dict; repeated keys become lists."""
        out: dict = {}
        for key, value in self.metadata.items():
            k = key.lower()
            if k in out:
                cur = out[k]
                out[k] = cur + [value] if isinstance(cur, list) else [cur, value]
            else:
                out[k] = value
        return out

    # -- RECORD ------------------------------------------------------------- #

    def record(self) -> dict:
        """RECORD as {path: (algorithm, digest, size)}; RECORD's own row has none."""
        rows = {}
        raw = self.text(f"{self.dist_info}/RECORD")
        for row in csv.reader(io.StringIO(raw)):
            if not row:
                continue
            path = row[0]
            digest = row[1] if len(row) > 1 else ""
            size = row[2] if len(row) > 2 else ""
            algo, _, value = digest.partition("=")
            rows[path] = (algo, value, size)
        return rows

    def verify_record(self) -> list[str]:
        """Every disagreement between RECORD and the archive, as sentences.

        This is what makes the payload comparison mean anything: a member list
        says which names exist, and RECORD says the bytes behind them are the
        bytes the installer will check.
        """
        problems = []
        rows = self.record()
        me = f"{self.dist_info}/RECORD"
        listed = {n for n in self.names if not n.endswith("/")}
        for missing in sorted(listed - set(rows)):
            problems.append(f"{missing} is in the archive but not in RECORD")
        for extra in sorted(set(rows) - listed):
            problems.append(f"RECORD names {extra}, which is not in the archive")
        for path, (algo, value, size) in sorted(rows.items()):
            if path == me:
                continue
            if path not in listed:
                continue
            blob = self.zip.read(path)
            if not algo:
                problems.append(f"RECORD gives no digest for {path}")
                continue
            if algo != "sha256":
                problems.append(f"RECORD hashes {path} with {algo}, not sha256")
                continue
            want = base64.urlsafe_b64encode(hashlib.sha256(blob).digest()).rstrip(b"=").decode()
            if want != value:
                problems.append(f"RECORD digest for {path} does not match its bytes")
            if size and str(len(blob)) != size:
                problems.append(f"RECORD size for {path} is {size}, archive holds {len(blob)}")
        return problems

    def close(self) -> None:
        self.zip.close()
