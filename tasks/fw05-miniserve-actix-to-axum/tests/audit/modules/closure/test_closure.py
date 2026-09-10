"""Has the retired stack left the tree, and left what the tree declares?

Advisory.  Nothing in this file can fail the audit gate: the gate is the six
prose questions in evaluation.toml, and this module's output reaches the reviewer
as findings to check rather than as verdicts to inherit.

On this subject that distinction is not academic, because the retired dependency
is named legitimately in the original tree in four places.  ``src/listing.rs``
carries a doc comment citing the actix-web 0.7 source that ``directory_listing``
was adapted from; the CHANGELOG records the 2.0, 3.0 and 4.0 upgrades; the README
links to actix.rs; and every one of the 28 ``.rs`` files may say in a comment what
it no longer does.  Keeping all of that is honest, and a scan that scored the
string would punish honesty and reward a submission that vendored
``actix_files::NamedFile`` under a new name and deleted the citation.

So the checks below are split by *where* the name is, not by whether it appears.
``crate_sources`` with comments blanked is where a live dependency lives; a
comment is where a note about a dead one lives; and the two are reported
separately so the reviewer can see that the honest case was seen and dismissed
rather than missed.

The strongest thing in this module is not a string check at all.  It is the pair
that reads the manifest and the lock file, because on this task those two answer
the question almost completely on their own: the delivery registry has no index
entry for any retired crate, so a manifest that still names one cannot resolve and
a lock file that still pins one cannot be satisfied.  A finding there means the
submission could not have built, which the reviewer can state without opening a
single ``.rs`` file.
"""

from __future__ import annotations

import json
import re

import pytest
import srbscan

pytestmark = pytest.mark.scan

#: The four crates miniserve names in its own manifest.  Parametrised separately
#: from the full retired list so that a finding says whether the submission kept
#: a *declared* dependency or reached for something deeper in the family.
DECLARED_IN_STATE_A = ["actix-web", "actix-files", "actix-multipart",
                       "actix-web-httpauth"]

#: Text that only appears in a copied actix source tree.  Each is a type, trait or
#: function actix defines and nothing else does -- a submission that contains one
#: of these has actix's implementation, whatever the directory is called.
#:
#: ``impl FromRequest for`` is deliberately absent and ``trait FromRequest`` is
#: deliberately present.  axum has its own ``FromRequest`` and
#: ``FromRequestParts``, so a correct submission writing a custom extractor
#: implements that trait by that name -- reporting the ``impl`` would fire on every
#: submission that did the idiomatic thing.  *Declaring* the trait is the opposite
#: signal, and it is the one the axum_native gate names: "a locally defined
#: extractor trait mirroring FromRequest that the handlers implement instead of
#: using axum's".
VENDORED_SOURCE_MARKERS = [
    "trait FromRequest",
    "trait Transform<",
    "impl Transform<",
    "struct ServiceRequest",
    "struct ServiceResponse",
    "enum AnyBody",
    "struct HttpServer",
    "trait Responder",
    "struct NamedFile",
    "struct HttpRequest",
    "struct HttpResponseBuilder",
    "mod actix",
]

#: actix's public vocabulary.  Distinct from the markers above: seeing one of
#: these names *used* is a lead about a shim, seeing one *defined* is a lead about
#: a vendored copy, and the two checks below say which they found.
#: ``FromRequest`` is not in this list, for the reason given above: it is axum's
#: name as much as actix's, so using it is what a correct extractor looks like.
ACTIX_VOCABULARY = [
    "ServiceRequest", "ServiceResponse", "HttpAuthentication", "NamedFile",
    "fn_service", "HttpMessage", "BodyStream", "Compat::new",
    "web::Data", "web::Query", "web::Payload", "HttpServer::new",
]

#: Every file in State A that can name a dependency, and their submission
#: counterparts.  A retired crate surviving in any of these is a different
#: sentence in the review than one surviving in ``src/``.
MANIFEST_FILES = [
    "Cargo.toml", "Cargo.lock", ".cargo/config.toml", "Containerfile",
    "Containerfile.alpine", "Makefile", "release.toml", "rustfmt.toml",
    ".github/workflows/ci.yml", ".github/workflows/build-release.yml",
    ".github/dependabot.yml", "packaging/miniserve@.service",
]


# ---------------------------------------------------------------------------
# The manifest and the lock file
# ---------------------------------------------------------------------------

def test_manifest_parses(repo):
    """Said once, so twelve checks that read it do not each say it.

    ``srbscan.manifest`` returns ``{}`` on a manifest it cannot read, which makes
    every dependency check below pass vacuously.  This is the check that turns
    that silence into a finding.
    """
    path = repo / "Cargo.toml"
    assert path.is_file(), "there is no Cargo.toml at the root of the submission"
    assert srbscan.manifest(repo), (
        f"{path.name} exists but does not parse as TOML, so every check in this "
        "module that reads a declared dependency found nothing for the wrong "
        "reason"
    )


def test_the_package_is_still_miniserve(repo):
    """A rename would make every other citation in the review ambiguous."""
    package = srbscan.manifest(repo).get("package") or {}
    assert package.get("name") == "miniserve", (
        f"the manifest declares package.name = {package.get('name')!r}; State A "
        "declares 'miniserve', and the binary the behavioural suite looks for is "
        "named after it"
    )


@pytest.mark.parametrize("crate", DECLARED_IN_STATE_A)
def test_manifest_no_longer_declares(crate, repo):
    """The four crates State A declared, one check each.

    A finding here is close to decisive on its own, and worth saying so about:
    the delivery registry has no index entry for these names, so a manifest that
    declares one cannot resolve at all.  Either the submission does not build, or
    it is being built against a registry that is not this one.
    """
    declared = srbscan.declared_deps(repo)
    assert crate not in declared, (
        f"Cargo.toml still declares {crate} = {declared[crate]}. The grading "
        f"registry has no index entry for {crate}, so this cannot resolve here"
    )


def test_no_retired_crate_is_declared_anywhere(repo, retired):
    """The rest of the family, including the four above, across all four tables.

    ``actix-web`` alone would leave ``actix-http`` + ``actix-server`` +
    ``actix-service`` reachable, which is enough to assemble a working stack by
    hand.  ``dev-dependencies`` counts: a retired crate there is still a retired
    crate in the tree, and the review should say which table it was in.
    """
    declared = srbscan.declared_deps(repo)
    offenders = {name: declared[name] for name in retired if name in declared}
    assert not offenders, (
        "the manifest still declares retired crates: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_lock_file_pins_no_retired_crate(repo, retired):
    """``Cargo.lock`` is generated, so a name in it is a name cargo resolved."""
    locked = srbscan.locked_packages(repo)
    if not locked:
        pytest.skip("the submission ships no Cargo.lock")
    offenders = {name: locked[name] for name in retired if name in locked}
    assert not offenders, (
        f"Cargo.lock still pins retired crates: {json.dumps(offenders, indent=2)}"
        " -- a lock file naming a crate the registry does not index cannot be "
        "satisfied offline"
    )


def test_lock_file_is_present_and_covers_the_manifest(repo):
    """A missing or stale lock file is a build risk, not a migration finding.

    Reported because the behavioural stage builds ``--offline`` and a manifest
    whose lock file does not cover it makes cargo re-resolve, which is where a
    submission that works on a machine with a network fails on the grader.
    """
    locked = srbscan.locked_packages(repo)
    assert locked, (
        "the submission ships no Cargo.lock; the behavioural stage builds offline "
        "against a fixed local registry, where an unlocked manifest has to "
        "re-resolve"
    )
    declared = set(srbscan.declared_deps(repo))
    missing = sorted(name for name in declared if name not in locked)
    assert not missing, (
        f"these declared dependencies are absent from Cargo.lock: {missing}"
    )


def test_no_crate_is_declared_under_two_names(repo):
    """The trap ``manifest-target.toml`` documents, which nothing else can see.

    The target manifest lists several crates twice under different keys -- the
    four ``-full`` aliases and the ``-new`` majors -- so that one resolve vendored
    a superset closure.  It says to take one declaration per crate, and says what
    happens otherwise: a manifest carrying both keys of a pair "resolves and locks
    without complaint and then fails at build with `depends on crate ... multiple
    times with different names`".

    Because it resolves and locks, no manifest check and no lock-file check can
    reach it; it arrives as a stage-2 ``build.compiles`` failure, which is one
    scored check short of complete, so the whole behavioural stage scores zero and
    the report names a cargo error rather than the pasted duplicate behind it.
    This check is here to make the reviewer's copy of that finding arrive in
    stage 1 instead.
    """
    dupes = srbscan.declared_aliases(repo)
    assert not dupes, (
        "these crates are declared under more than one manifest key: "
        f"{json.dumps(dupes, indent=2)}. Each pair resolves and locks cleanly and "
        "then fails `cargo build` with \"depends on crate ... multiple times with "
        "different names\" -- manifest-target.toml lists them twice to vendor a "
        "superset closure and says to take one declaration per crate"
    )


def test_no_patch_or_replace_section_reintroduces_a_retired_crate(repo, retired):
    """``[patch]``, ``[replace]`` and a path dependency are three ways back in.

    A vendored copy of actix does not have to be called actix.  It can sit in
    ``vendor/`` and be spliced in under the name of something else entirely, and
    the manifest is where that splice has to be declared.
    """
    data = srbscan.manifest(repo)
    findings: dict[str, str] = {}
    for section in ("patch", "replace"):
        table = data.get(section)
        if isinstance(table, dict) and table:
            findings[f"[{section}]"] = json.dumps(table)[:400]
    for name, spec in (data.get("dependencies") or {}).items():
        if isinstance(spec, dict) and (spec.get("path") or spec.get("git")):
            source = spec.get("path") or spec.get("git")
            findings[name] = f"local or remote source: {source}"
    assert not findings, (
        "the manifest sources dependencies from outside the registry, which is "
        f"how a vendored copy is spliced in: {json.dumps(findings, indent=2)}"
    )


def test_cargo_config_adds_no_source_replacement(repo):
    """State A's ``.cargo/config.toml`` sets Windows rustflags and nothing else.

    A ``[source]`` table added to it can point cargo at a directory in the tree,
    which is a way to supply a retired crate that leaves the manifest looking
    clean.
    """
    path = repo / ".cargo" / "config.toml"
    if not path.is_file():
        pytest.skip("the submission ships no .cargo/config.toml")
    text = srbscan.read(path)
    for table in ("[source", "[registries", "replace-with", "local-registry",
                  "directory ="):
        assert table not in text, (
            f"{srbscan.cite(path, '.cargo/config.toml', table)} -- this "
            "redirects where cargo gets its crates from"
        )


def test_features_no_longer_reference_the_retired_stack(repo, retired):
    """State A's ``tls`` feature enables ``actix-web/rustls``.

    It cannot survive the migration, so what the submission's ``tls`` feature now
    enables is one of the six gates.  This check only reports whether a retired
    crate is still named in the table; whether HTTPS still happens is a question
    for a reader, and for stage 2's ``tls`` module.
    """
    table = srbscan.features(repo)
    offenders = {
        name: enables for name, enables in table.items()
        if any(entry.split("/")[0].split("?")[0] in retired for entry in enables)
    }
    assert not offenders, (
        f"the [features] table still enables retired crates: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_a_tls_feature_still_exists(repo):
    """Not a finding about actix -- a finding about a capability going missing.

    State A ships ``default = ["tls"]``.  A submission that deleted the feature
    to make the migration compile has removed a documented capability, and the
    gate that asks about it wants to know before it opens the file.
    """
    table = srbscan.features(repo)
    if not table:
        pytest.skip("the submission declares no [features] table at all")
    assert "tls" in table, (
        f"the [features] table declares {sorted(table)} and no 'tls'; State A "
        "ships default = [\"tls\"] and documents --tls-cert and --tls-key"
    )


# ---------------------------------------------------------------------------
# What the code refers to
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("crate", DECLARED_IN_STATE_A)
def test_no_crate_source_refers_to(crate, repo):
    """Comments blanked first, so the ``listing.rs`` citation does not count.

    ``srbscan.uses`` counts both spellings of a reference: the ``use`` at the top
    of the file and the inline ``actix_web::body::BodyStream`` path that needs no
    ``use`` at all.  State A's ``src/listing.rs`` has both -- a ``use`` on line 6
    and an inline path on 363 -- and a check that only read the ``use`` block
    would miss a submission that deleted the imports and qualified everything.
    """
    offenders: dict[str, list[str]] = {}
    for path, rel in srbscan.crate_sources(repo):
        if crate in srbscan.uses(path):
            code = srbscan.code_of(srbscan.read(path))
            offenders[rel] = [srbscan.cite(path, rel, crate.replace("-", "_"),
                                           haystack=code)]
    assert not offenders, (
        f"these shipped sources refer to {crate} in code, not in a comment: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_no_shipped_source_refers_to_any_retired_crate(repo, retired):
    """The whole family, over ``src/`` and ``build.rs``."""
    offenders: dict[str, list[str]] = {}
    for path, rel in srbscan.crate_sources(repo):
        hit = sorted(srbscan.uses(path) & set(retired))
        if hit:
            offenders[rel] = hit
    assert not offenders, (
        "these shipped sources refer to retired crates in code: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_the_ported_test_suite_refers_to_no_retired_crate(repo, retired):
    """``tests/`` separately, because it is a weaker finding than ``src/``.

    A retired crate here does not reach the binary, but ``cargo test`` still has
    to resolve it, and it cannot: the registry does not index it. Reported as its
    own check so the reviewer is not left deciding which half of the tree a
    combined finding was about.
    """
    offenders: dict[str, list[str]] = {}
    for path, rel in srbscan.rust_files(repo):
        if not rel.startswith("tests/"):
            continue
        hit = sorted(srbscan.uses(path) & set(retired))
        if hit:
            offenders[rel] = hit
    assert not offenders, (
        "the ported test suite refers to retired crates: "
        f"{json.dumps(offenders, indent=2)}"
    )


@pytest.mark.parametrize("marker", VENDORED_SOURCE_MARKERS)
def test_no_vendored_actix_type_is_defined(marker, repo):
    """A definition, not a use.

    ``struct ServiceRequest`` in the submission means the submission *is* the
    thing actix provided.  Using the name would be a shim; defining it is a
    vendored copy, and this is the check that can tell them apart -- it looks for
    the definition keyword, so ``use axum::...ServiceRequest`` does not match.
    """
    offenders: dict[str, str] = {}
    for path, rel in srbscan.rust_files(repo):
        code = srbscan.code_of(srbscan.read(path))
        for keyword in ("pub ", ""):
            if f"{keyword}{marker}" in code:
                offenders[rel] = srbscan.cite(path, rel, marker, haystack=code)
                break
    assert not offenders, (
        f"{marker!r} is defined in the submission, which is actix's own "
        f"vocabulary rather than a use of it: {json.dumps(offenders, indent=2)}"
    )


@pytest.mark.parametrize("name", ACTIX_VOCABULARY)
def test_actix_vocabulary_is_not_used_in_code(name, repo):
    """A weaker lead than the one above, and labelled as one.

    Some of these names are not actix's alone.  ``NamedFile`` is a reasonable
    thing to call a struct; ``web::Query`` is a plausible module path in anyone's
    code.  A hit here means *this name is in the code*, and whether it is actix's
    type or the submission's own is a question for a reader, which is why the
    citation is a line number rather than a verdict.
    """
    offenders: dict[str, str] = {}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.code_of(srbscan.read(path))
        if name in code:
            offenders[rel] = srbscan.cite(path, rel, name, haystack=code)
    assert not offenders, (
        f"{name!r} appears in shipped code. It is actix's vocabulary; it may also "
        f"be the submission's own name for something: {json.dumps(offenders, indent=2)}"
    )


def test_no_actix_string_survives_in_shipped_code(repo):
    """The blunt one, over code only, kept because it catches the spellings above.

    ``actix`` in ``crate_sources`` with comments blanked has one honest
    explanation -- a string literal in a user-facing message -- and several
    dishonest ones.  The comment-only case is handled by the next check and does
    not reach here.
    """
    offenders: dict[str, str] = {}
    for path, rel in srbscan.crate_sources(repo):
        code = srbscan.code_of(srbscan.read(path))
        if "actix" in code.lower():
            offenders[rel] = srbscan.cite(path, rel, "actix", haystack=code,
                                          ignore_case=True)
    assert not offenders, (
        f"'actix' appears in the code of shipped sources: "
        f"{json.dumps(offenders, indent=2)}"
    )


def test_actix_is_named_in_comments_which_is_not_a_defect(repo):
    """An inventory, not a defect, and it has to report as one to be read at all.

    State A's ``src/listing.rs:155`` cites the actix-web 0.7 source that
    ``directory_listing`` was adapted from, and ``src/main.rs:306`` says
    "Configures the Actix application".  A correct migration rewrites both and may
    well keep the citation, and the review should see the mention, see that it is
    in a comment, and not have to work out for itself that the previous check
    already excluded it.

    The search is case-folded, so the citation is too -- ``Actix`` with a capital
    is why :func:`srbscan.cite` takes ``ignore_case``.

    The reason this asserts rather than prints: ``swerefactor.pytest_module`` records
    a check's ``detail`` only when the verdict is fail or error, and ``summary`` is
    empty on a pass.  A passing check contributes a number to "111 found nothing"
    and not one word.  So an observation that needs to reach the reviewer has to
    arrive as a finding -- which costs nothing, because no scan check is scored and
    the digest tells the reviewer in its own header that these are places to look.
    The check's name is the disclaimer, and it is in the assertion message too.
    """
    mentions: dict[str, str] = {}
    for path, rel in srbscan.rust_files(repo):
        comments = srbscan.comments_of(srbscan.read(path))
        if "actix" in comments.lower():
            mentions[rel] = srbscan.cite(path, rel, "actix", haystack=comments,
                                         ignore_case=True)
    assert not mentions, (
        "NOT A DEFECT -- an inventory. actix is named in comments here, and a "
        "comment naming a retired framework is honest documentation, not a live "
        "dependency. State A cites the actix-web 0.7 source in a doc comment above "
        f"`directory_listing`: {json.dumps(mentions, indent=2)}"
    )


# ---------------------------------------------------------------------------
# Where a name can hide that is not a .rs file
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rel", MANIFEST_FILES)
def test_published_file_names_no_retired_crate(rel, repo, retired):
    """The build automation, the container files, the unit file, the CI.

    ``Cargo.lock`` is in this list as well as in its own check above: this one
    reports the line, which is what a reviewer wants when the answer is "line
    1841 still pins actix-http".
    """
    path = repo / rel
    if not path.is_file():
        pytest.skip(f"{rel} is not in the submission")
    text = srbscan.read(path)
    hits = sorted({name for name in retired
                   if name in text or name.replace("-", "_") in text})
    assert not hits, (
        f"{rel} names retired crates {hits}: "
        f"{srbscan.cite(path, rel, hits[0])}"
    )


def test_no_directory_is_named_after_the_retired_stack(repo):
    """A vendored copy has to live somewhere, and it is usually named honestly."""
    offenders = []
    for path in sorted(repo.rglob("*")):
        if not path.is_dir():
            continue
        rel = str(path.relative_to(repo))
        if srbscan.is_exempt(rel):
            continue
        if "actix" in path.name.lower() or path.name in ("vendor", "third_party"):
            offenders.append(rel)
    assert not offenders, (
        f"these directories are named after the retired stack or are a vendor "
        f"drop: {offenders}"
    )


def test_no_crate_archive_or_prebuilt_binary_was_added(repo):
    """A ``.crate`` file is a dependency shipped in the tree.

    So, differently, is a committed executable: the behavioural stage builds from
    source, but a submission that ships a binary and a build script that copies
    it has arranged for the build to produce something it did not compile.
    """
    offenders = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(repo))
        if rel.startswith("target/") or rel.startswith(".git/"):
            continue
        if path.suffix in (".crate", ".rlib", ".so", ".a", ".dylib", ".exe"):
            offenders.append(rel)
        elif path.name == "miniserve" and path.stat().st_size > 100_000:
            offenders.append(f"{rel} ({path.stat().st_size} bytes)")
    assert not offenders, (
        f"these compiled or packaged artefacts are committed in the tree: "
        f"{offenders}"
    )


def test_the_new_stack_is_actually_declared(repo):
    """The other direction, and the reason this module is not a token list.

    Every check above passes on an empty repository.  This one asks whether the
    manifest declares the framework the task named, so that "nothing retired is
    here" and "something was migrated to" are two separate observations in the
    review rather than one that cannot tell them apart.
    """
    declared = srbscan.declared_deps(repo)
    wanted = ("axum", "tower", "tower-http", "hyper")
    present = sorted(name for name in wanted if name in declared)
    assert "axum" in declared, (
        f"the manifest declares none of the target stack; it declares "
        f"{sorted(declared)[:12]}..."
    )
    print(f"target stack declared: {present}")


def test_the_binary_still_has_a_main(repo):
    """A shape check, so a reviewer's first read is not spent finding the entry."""
    main = repo / "src" / "main.rs"
    assert main.is_file(), (
        "there is no src/main.rs; State A's binary target is the default one and "
        "the behavioural stage builds `cargo build --release` expecting a "
        "`miniserve` binary"
    )
    code = srbscan.code_of(srbscan.read(main))
    assert re.search(r"\bfn\s+main\s*\(", code), (
        "src/main.rs defines no `fn main`"
    )
