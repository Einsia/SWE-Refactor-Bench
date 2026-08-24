#!/usr/bin/env python3
"""Freezes the grading inputs into the verifier image, at image build time.

Everything a submission is measured against is produced here, once, from the
pinned upstream tarball -- the same bytes the agent's workspace was unpacked
from.  By the time any submission exists, the corpus, the catalog and the
expected outputs are already files in a read-only layer.

That ordering is the whole design.  If the corpus were generated at grading time
from the submitted tree, a submission could edit `test/spec.txt` and shrink the
conformance suite to nothing; if the expectations were computed at grading time,
the submission would be in the container while the answers were being decided.
Neither is possible when both are frozen in the image.

The manifest this writes records the digest of each frozen input.  Every module
checks those digests before grading anything, so a mismatch is reported as a
broken suite rather than as a failing submission.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

# Isolated mode (-I) drops the script's directory from sys.path, and every entry
# point in this suite is run that way, so each one puts it back itself.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build as buildmod  # noqa: E402
import catalog as catalogmod  # noqa: E402
import corpus as corpusmod  # noqa: E402
import oracle  # noqa: E402
import vlib  # noqa: E402
from vlib import Log  # noqa: E402

MANIFEST_SCHEMA = "swerefactor-verifier-manifest-v1"


def stage(baseline: Path, tests: Path, data: Path, assets: Path, log: Log) -> None:
    """Copy the verifier's fixed inputs into the assets tree.

    Everything grading needs lives under one directory afterwards, so a module
    takes a single --assets path and the image can make that whole directory
    read-only in one step.  The baseline copy is what tells a rewritten file from
    an untouched one, and it is copied from the verifier's own authenticated
    tarball rather than from anything the submission could reach.

    Two source directories rather than one: `tests` is code this suite executes
    (the probe crate, the compiler shim), `data` is input it only reads (the
    source contract).  Every stage in the benchmark splits them the same way, so
    "is this file run or read?" is answerable from its path.
    """
    log.write("staging fixed inputs")
    dest_baseline = assets / "baseline"
    if dest_baseline.exists():
        shutil.rmtree(dest_baseline)
    files = vlib.copy_tree(baseline, dest_baseline)
    for name in ("probe", "shim"):
        src = tests / name
        if not src.is_dir():
            raise SystemExit(f"missing verifier input directory: {src}")
        dest = assets / name
        if dest.exists():
            shutil.rmtree(dest)
        vlib.copy_tree(src, dest)
    contract = data / "source-contract.json"
    if not contract.is_file():
        raise SystemExit(f"missing source contract: {contract}")
    (assets / "source-contract.json").write_bytes(contract.read_bytes())
    log.write(f"staged baseline ({files} files), probe, shim, source contract")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=Path,
                        help="the pristine State A tree, unpacked from the tarball")
    parser.add_argument("--assets", required=True, type=Path,
                        help="where the frozen inputs are written")
    parser.add_argument("--tests", type=Path, default=Path(__file__).resolve().parent,
                        help="the verifier source tree, staged into the assets")
    parser.add_argument("--data", type=Path,
                        default=Path(__file__).resolve().parent.parent / "data",
                        help="the suite's read-only inputs (the source contract)")
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/freeze"))
    parser.add_argument("--image", default="")
    args = parser.parse_args(argv)
    for name in ("baseline", "assets", "tests", "data", "workspace"):
        setattr(args, name, getattr(args, name).resolve())

    assets: Path = args.assets
    assets.mkdir(parents=True, exist_ok=True)
    log = Log(assets / "freeze.log")
    log.section("freeze verifier assets")

    stage(args.baseline, args.tests, args.data, assets, log)

    # 1. The reference, in both link configurations.  Built from the baseline
    #    with the real compiler: this is State A, and it is the standard.
    reference_root = assets / "reference"
    reference_root.mkdir(parents=True, exist_ok=True)
    refbuilder = buildmod.ReferenceBuilder(args.baseline, reference_root, log)
    prefixes = {config: refbuilder.build(config) for config in ("shared", "static")}
    for config, prefix in sorted(prefixes.items()):
        cli = prefix / "bin" / "cmark"
        if not cli.is_file():
            raise SystemExit(f"reference {config} produced no cmark")
        check = vlib.run(
            [str(cli), "--version"],
            env=vlib.base_env(LD_LIBRARY_PATH=str(prefix / "lib")),
            timeout=60.0,
            check=True,
        )
        if b"0.31.1" not in check.stdout + check.stderr:
            raise SystemExit(f"reference {config} reports the wrong version")
        log.write(f"reference[{config}]: {prefix}")

    # 2. The corpus, from the baseline's own conformance data.
    corpus_dir = assets / "corpus"
    corpus_meta = corpusmod.build(args.baseline, corpus_dir)
    corpus_meta["reference_version"] = "0.31.1"
    log.write(f"corpus: {corpus_meta['count']} documents, {corpus_meta['digest']}")

    # 3. The catalog, generated over that corpus.  The floors inside
    #    build_catalog abort the image build if coverage regressed.
    catalog = catalogmod.build_catalog(corpus_meta)
    vlib.write_json(assets / "catalog.json", catalog)
    counts = catalog["counts"]
    log.write(
        f"catalog: {counts['total']} cases ({counts['behavioural']} behavioural, "
        f"{counts['assertions']} assertions), {catalog['digest']}"
    )

    # 4. The probe, compiled against the reference install.  It is the verifier's
    #    own C program; the same source is later compiled against the submission.
    probe_src = assets / "probe" / "probe.c"
    if not probe_src.is_file():
        raise SystemExit(f"probe source missing at {probe_src}")
    args.workspace.mkdir(parents=True, exist_ok=True)
    probe_bin = args.workspace / "probe-reference"
    result = buildmod.compile_probe(
        probe_src, prefixes["shared"], probe_bin, log, label="freeze-probe"
    )
    if not result.ok:
        raise SystemExit(f"the probe did not build against the reference: {result.tail()}")

    # 5. The expectations.  Any crash here is a defective case, not a defective
    #    submission, and freeze() refuses to write a store that contains one.
    oracle_dir = assets / "oracle"
    oracle_dir.mkdir(parents=True, exist_ok=True)
    digest = oracle.freeze(
        catalog,
        corpus_meta,
        probe_bin,
        prefixes["shared"] / "bin" / "cmark",
        corpus_dir / "docs",
        oracle_dir,
        log,
    )

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "task": "lang01-cmark-c-to-rust",
        "image": args.image,
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "upstream_version": "0.31.1",
        "digests": {
            "catalog": catalog["digest"],
            "corpus": corpus_meta["digest"],
            "oracle": digest,
        },
        "counts": {
            "cases": counts["total"],
            "behavioural": counts["behavioural"],
            "assertions": counts["assertions"],
            "by_kind": counts["by_kind"],
            "documents": corpus_meta["count"],
            "expectations": len(json.loads(
                (oracle_dir / "expectations.json").read_text(encoding="utf-8")
            )["entries"]),
        },
        "reference_prefixes": {c: str(p) for c, p in sorted(prefixes.items())},
    }
    vlib.write_json(assets / "verifier-manifest.json", manifest)
    log.write(
        f"frozen: {counts['total']} cases, {corpus_meta['count']} documents, "
        f"oracle {digest}"
    )
    print(json.dumps(manifest["counts"], indent=1, sort_keys=True))
    log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
