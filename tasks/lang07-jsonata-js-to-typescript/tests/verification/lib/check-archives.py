"""Fail if any archive in this image holds JavaScript or TypeScript.

Run from the final stage of `tests/behavioural/Dockerfile` and of
`tests/verification/Dockerfile`, after the RUN that removes each suite's `data/`.

Why it exists
-------------
The sweeps beside it look at loose files: by name (`*.js`, `*.ts`, and a `jsonata`
directory) and by content (grep those same extensions for jsonata's error-code
table).  State A shipped past every one of them, in both images, as
`data/original.tar.gz` -- the tarball arrives from the build context through
`COPY . /tests/…`, which is a path none of the "the reference is absent" comments
considered, and a gzipped tarball has neither a matching name nor a greppable byte
inside it.  Six assertions that no reference was present, and a complete one sitting
in the image.

In the behavioural image that mattered because a submission is built and run there: a
`dist/probe.js` that unpacked the tarball and forwarded to the JavaScript would
answer all 13,940 frozen cases and score full marks as no port at all.  In the
verification image it mattered more, because `COPY .` lands the file at mode 664 and
`srbprobe` -- the user the submission's own build runs as -- could read it, which
walks around the two 0700 seals that stage is built around.

Containers are detected by magic bytes, not by extension.  Two reasons, and the
first is measured: the image holds around 1,550 files whose name ends in `.gz`, and
nearly all are man pages, so a name sweep is either noise or a list of path
exclusions that goes stale.  The second is the objection that produced the
error-code sweep -- renaming a file defeats a sweep that reads names, and the whole
point of this task is that the reference cannot be kept out by withholding a
toolchain, so it has to be kept out by describing the image.

There are no exempt paths.  `/opt/original` and `/opt/reference` legitimately hold
the engine in the verification image, but they hold it as loose files behind 0700
directories; an *archive* in either is not something that stage has a use for, and
an exemption nothing needs today is a hole for a later edit to fall into.
"""
from __future__ import annotations

import bz2
import gzip
import io
import lzma
import sys
import tarfile
import zipfile
from pathlib import Path

#: Kernel interfaces.  `Path.rglob` would otherwise walk /proc, where reading a file
#: can block, and the interesting question is about the image's own layers.
#:
#: `/run` is deliberately *not* here.  This task stages State A at `/run/state-a` in
#: `environment/Dockerfile` -- so `/run` is the one path outside /opt that a
#: reference tarball demonstrably reaches in this repository.  Both graded images
#: have an empty /run today, which means skipping it would have been safe by luck,
#: and the failure it exists to catch is precisely a sweep that looks past the place
#: a file actually is.
SKIP_ROOTS = ("/proc", "/sys", "/dev")

#: What an implementation would be written in -- the same extensions the two loose
#: sweeps use.  `.d.ts` is matched by `.ts` and that is deliberate: a declaration
#: file is not an implementation, but an archive of them is not something either
#: image carries either, so it is reported rather than carved out.
CODE_SUFFIXES = (".js", ".cjs", ".mjs", ".ts", ".mts", ".cts")

#: gzip, xz, bzip2, zip (also jar and whl), zstd.  Plain tar is recognised by the
#: `ustar` marker at offset 257 instead, which is why the read below is 262 bytes.
MAGIC = (
    (b"\x1f\x8b", "gzip"),
    (b"\xfd7zXZ\x00", "xz"),
    (b"BZh", "bzip2"),
    (b"PK\x03\x04", "zip"),
    (b"\x28\xb5\x2f\xfd", "zstd"),
)

#: Decompression ceiling.  Every archive either image legitimately carries is a few
#: hundred KB; the cap is what stops a crafted file from being a memory exhaustion.
READ_LIMIT = 64 << 20

#: The walk has to have happened.  A sweep rooted somewhere empty reports success,
#: which is the failure this whole file was written about -- so the count of files it
#: examined is asserted rather than trusted.  Measured in both finished images: 12,551
#: regular files in the behavioural one and 14,429 in the verification one, of which
#: 1,545 and 1,555 carry container magic (almost entirely compressed man pages, which
#: are gzip and are not tarballs).  The floor is far below either and far above zero;
#: it is not a tuned number and nothing should ever approach it.
#:
#: Those four figures move by a handful whenever a file is added to or removed from
#: `lib/` or `modules/`, because they count the finished image and the suite is in it.
#: They are here to document the distance to the floor, not as a fifth assertion --
#: nothing reads them, and a build must never fail because one drifted by six.
#:
#: The count of *archives* is deliberately not asserted.  The behavioural image
#: legitimately reaches zero once its `data/` is removed and npm's cache is cleared --
#: node arrives as a tarball and is deleted after it is unpacked, and nothing there
#: ships a wheel -- so a floor of one would fail that build for being correct.  It
#: takes only the one image to make the floor wrong; the verification image is not at
#: zero and never was, because it opens three: the pinned `pip` under `/opt/pins` and
#: the `pip`/`setuptools` pair Debian bundles in `/usr/share/python-wheels`.  All
#: three are zip, hold no `.js`/`.ts`, and are why the open-and-list path is exercised
#: somewhere rather than being dead code the pair never reaches.
EXAMINED_FLOOR = 5_000


def container_kind(path: Path) -> str | None:
    """The kind of container `path` is by its first bytes, or None."""
    try:
        with path.open("rb") as handle:
            head = handle.read(262)
    except OSError:
        return None
    for magic, kind in MAGIC:
        if head.startswith(magic):
            return kind
    return "tar" if head[257:262] == b"ustar" else None


def member_names(path: Path, kind: str) -> list[str] | None:
    """Every member of `path`, or None when it is not really an archive.

    A compressed man page is gzip and is not a tarball.  Decompressing one and
    failing to parse it as tar is how it gets skipped, so a parse failure here is
    not a finding -- it is the common case.
    """
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                return archive.namelist()
        if kind == "zstd":
            # Nothing in either image writes zstd and the stdlib cannot read it.
            # Reported through the caller rather than skipped silently, because a
            # zstd archive appearing here is a change this file should not absorb.
            return None
        if kind == "tar":
            with tarfile.open(path, "r:") as archive:
                return archive.getnames()
        opener = {"gzip": gzip.open, "xz": lzma.open, "bzip2": bz2.open}[kind]
        with opener(path, "rb") as stream:  # type: ignore[operator]
            blob = stream.read(READ_LIMIT)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            return archive.getnames()
    except Exception:
        return None


def main() -> int:
    findings: list[str] = []
    unreadable: list[str] = []
    opened = 0
    examined = 0
    for path in Path("/").rglob("*"):
        if str(path).startswith(SKIP_ROOTS):
            continue
        try:
            if path.is_symlink() or not path.is_file():
                continue
        except OSError:
            continue
        examined += 1
        kind = container_kind(path)
        if kind is None:
            continue
        if kind == "zstd":
            unreadable.append(str(path))
            continue
        names = member_names(path, kind)
        if names is None:
            continue
        opened += 1
        code = sorted(name for name in names if name.endswith(CODE_SUFFIXES))
        if code:
            shown = ", ".join(code[:6]) + (" ..." if len(code) > 6 else "")
            findings.append(f"{path} ({kind}, {len(names)} members) holds "
                            f"{len(code)} of them: {shown}")

    if unreadable:
        print("a zstd archive is present and this sweep cannot read it, so it "
              "cannot say whether it holds an implementation:", file=sys.stderr)
        for path_text in unreadable:
            print(f"  {path_text}", file=sys.stderr)
        return 1
    if findings:
        print("an archive in this image holds JavaScript or TypeScript, which is "
              "how a copy of State A once shipped past every other sweep here:",
              file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print("the loose-file sweeps cannot see inside an archive, so this is the "
              "one that has to fail", file=sys.stderr)
        return 1
    if examined < EXAMINED_FLOOR:
        print(f"this sweep examined {examined} file(s), below the floor of "
              f"{EXAMINED_FLOOR}: it walked a tree that is not this image, and a "
              f"sweep that finds nothing because it looked nowhere reports the same "
              f"success as one that found nothing because there is nothing",
              file=sys.stderr)
        return 1
    print(f"no archive holds .js/.ts ({examined} files examined, "
          f"{opened} archive(s) opened and listed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
