"""Are the published declarations emitted from the port, or carried across?

The narrowest module in this stage, and the one whose gate is least answerable by
grepping, which is why it is separate rather than folded into `contract`.

State A ships a hand-written `jsonata.d.ts` at the repository root: 2,297 bytes, 81
lines, describing an `export =` callable merged with a namespace over six members.
State B must publish declarations at `dist/jsonata.d.ts`, and the contract requires
they be *emitted by tsc from the ported source* -- which is why `declaration: true`
is in `required_true` rather than merely permitted.

The distinction is invisible from outside the tree.  A correct emit and a copied
hand-written file both put a working `.d.ts` at the right path; a consumer compiles
against either; `tsc` will happily emit nothing at all if `declaration` is off, and a
build step that copies the old file into place leaves stage 2 with nothing to
complain about.  Stage 2 checks that the path exists and that 24 type-surface rows
compile against it, and a copied State A file passes all 24 -- it is, after all, an
accurate description of the same API.

So this module reports the four things that separate the two routes, and none of them
is a verdict:

    the root jsonata.d.ts    still there? still State A's bytes?
    copy operations          anything in the build path that writes a .d.ts
    committed dist/          declarations already present before the build runs
    the type names           does src/ declare what the declarations describe

The last is the one that cannot be faked cheaply.  A `.d.ts` emitted from source
describes types the source declares; a `.d.ts` that names `ExprNode` while no file
under `src/` ever declares an `ExprNode` was not produced from that source.  It is
still not proof -- a port may name its types differently and re-export, and the check
says so in its own message -- but it is the question `declarations-generated` needs
asked, and it needs the inventory to ask it.
"""

from __future__ import annotations

import re

import pytest

import srbscan
from srbscan import ORIGINAL, REPO, rel

pytestmark = pytest.mark.scan

MISSING = "<state-a-unreadable>"

#: State A's hand-written declarations, at the root.  The path comes from the
#: contract's prose rather than a key, so it is asserted against ORIGINAL below
#: rather than trusted.
STATE_A_DECLARATIONS = "jsonata.d.ts"

#: Ways to put a file somewhere without compiling it.  Reported with citations; a
#: build script that copies a README is not a finding and will appear here.
COPY_OPERATIONS = (
    "copyFileSync", "copyFile(", "cpSync", "cp -", "cp(",
    "writeFileSync", "createWriteStream", "appendFileSync",
    "renameSync", "rename(", "linkSync", "symlinkSync",
    "concat(", "cat ",
)

#: Shell redirection into the output directory, for the case where the build is a
#: string in `package.json` rather than a script.  A regex and not a token, because
#: `> dist` also occurs inside `' -> dist/'` -- State A's build prints exactly that
#: when it finishes, and reporting a progress message as a copy operation is the
#: kind of hit that teaches a reviewer to stop reading the list.
REDIRECTION = re.compile(r"(?<![-=<>])>>?\s*(?:\./)?dist\b")

#: Where a build lives in this repository.  `tools/` is State A's own build helper
#: directory, and `package.json` names whatever runs.
BUILD_PATHS = ("package.json", "tools", "scripts", "build", "Makefile", "makefile")

DECLARATION_SUFFIX = ".d.ts"


def _surface(key: str) -> list[str]:
    node = srbscan.CONTRACT.get("declarations_contract", {})
    surface = node.get("surface") if isinstance(node, dict) else None
    names = surface.get(key) if isinstance(surface, dict) else None
    if not isinstance(names, list) or not names:
        return [MISSING]
    return [str(n) for n in names]


NAMESPACE_MEMBERS = _surface("namespace_members")
EXPRESSION_MEMBERS = _surface("expression_members")


def _published_path() -> str:
    node = srbscan.CONTRACT.get("declarations_contract", {})
    return str(node.get("published_at") or "dist/jsonata.d.ts")


def _build_files() -> list[srbscan.Path]:
    """Everything that could plausibly run during `npm run build`.

    A superset on purpose. The alternative is parsing `package.json`'s script to
    find what it invokes and following that, which is a resolver -- and a resolver
    that misses one hop reports a clean build path for a build that copies. A
    superset over-reports into a message a reviewer reads with citations in hand.
    """
    out = []
    for path in srbscan.walk_source(REPO):
        if not path.is_file() or srbscan.is_declared_output(path, REPO):
            continue
        relpath = rel(REPO, path)
        if relpath.split("/")[0] in BUILD_PATHS or relpath in BUILD_PATHS:
            out.append(path)
    return out


def test_state_a_declarations_are_gone_or_unchanged():
    """The root `jsonata.d.ts`: removed, or still State A's bytes. Not edited.

    It is on the contract's `removable_paths` list -- its purpose ends with the
    JavaScript, because State B's declarations are emitted -- so removal is the
    expected end state and passes silently.

    Still present and byte-identical is reported, and what it means depends entirely
    on whether anything copies it, which is the next check's subject. A leftover file
    nothing touches is untidy; the same file with a `cp` in the build script is the
    published declarations not being emitted at all.

    Still present and *edited* is the more interesting row. Editing State A's
    declarations is what an author does when the emitted ones do not match and the
    hand-written file is what gets shipped -- there is no other reason to improve a
    file that is supposed to be deleted.
    """
    theirs = REPO / STATE_A_DECLARATIONS
    ours = ORIGINAL / STATE_A_DECLARATIONS
    assert ours.is_file(), (
        f"State A has no {STATE_A_DECLARATIONS} at {ours}; the original mount is "
        f"wrong and this module cannot compare anything")
    if not theirs.exists():
        return
    mine, upstream = srbscan.sha256(theirs), srbscan.sha256(ours)
    if mine == upstream:
        pytest.fail(
            f"{STATE_A_DECLARATIONS} is still present, byte-identical to State A "
            f"({upstream[:16]}, {ours.stat().st_size} bytes). It is on the "
            f"contract's removable list because State B's declarations are emitted "
            f"from the port. Leftover on its own; the copy sweep in this module "
            f"reports whether the build reaches it.")
    pytest.fail(
        f"{STATE_A_DECLARATIONS} was edited: State A is {upstream[:16]} "
        f"({ours.stat().st_size}B), the submission ships {mine[:16]} "
        f"({theirs.stat().st_size}B). This file is supposed to be deleted, so an "
        f"edit means it is still doing work -- the reviewer should find out what "
        f"consumes it.")


def test_no_declaration_copying_in_the_build_path():
    """Anything in the build path that writes, copies or concatenates a `.d.ts`.

    The mechanism `declarations-generated` is about. `tsc` emits declarations when
    `declaration` is true and emits none when it is false; a build that turns it off
    and copies the hand-written file produces a `dist/jsonata.d.ts` that is correct,
    complete, and not derived from the port at all.

    Reported with citations and not judged, because the honest version exists: a
    build that copies `README.md` into `dist/`, or writes a generated version file,
    touches the same functions. What makes a hit matter is its subject, so each
    finding quotes its own line -- a per-file "this file mentions .d.ts" note cannot
    separate the write that ships a module from the copy that ships declarations,
    and on State A's build script those are two lines apart.

    Every occurrence is reported, not the first per token. A build that writes
    modules on one line and copies the declaration on another would otherwise show
    only the module write, which is the honest half of the pair.
    """
    findings = []
    for path in _build_files():
        text = srbscan.read_text(path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            hits = [token for token in COPY_OPERATIONS if token in line]
            redirect = REDIRECTION.search(line)
            if redirect:
                hits.append(redirect.group(0).strip())
            if not hits:
                continue
            quoted = " ".join(line.split())
            if len(quoted) > 110:
                quoted = quoted[:110] + "..."
            findings.append(
                f"{rel(REPO, path)}:{lineno} {'/'.join(hits)}"
                + (" [.d.ts]" if DECLARATION_SUFFIX in line else "")
                + f"  {quoted}")
    assert not findings, (
        f"{len(findings)} file-writing operation(s) in the build path: "
        f"{'; '.join(findings[:12])}{' ...' if len(findings) > 12 else ''}. "
        f"The contract requires dist/jsonata.d.ts be emitted by tsc from the "
        f"ported source, not copied. A build script writes files -- that is what "
        f"it is for -- so the question each line answers is *what* it writes. "
        f"For reference, State A's tools/build.js writes its modules on one line "
        f"and copies the hand-written jsonata.d.ts into dist/ on the next, and it "
        f"is the copy that State B may not keep."
    )


def test_declarations_were_not_shipped_prebuilt():
    """`dist/jsonata.d.ts` present in the submission, before any build has run.

    The grader runs `npm run build` itself, so a `dist/` in the tree is at best
    unnecessary. Where declarations are concerned it is more than that: a prebuilt
    `dist/jsonata.d.ts` is a file whose provenance nothing downstream can check,
    since a real build would overwrite it and a build that emits nothing would leave
    it in place, and stage 2 cannot distinguish those outcomes -- both leave a
    correct file at the correct path.
    """
    published = REPO / _published_path()
    if not published.exists():
        return
    ours = ORIGINAL / STATE_A_DECLARATIONS
    same = ours.is_file() and srbscan.sha256(published) == srbscan.sha256(ours)
    pytest.fail(
        f"{_published_path()} was shipped with the submission "
        f"({published.stat().st_size} bytes)"
        + (", byte-identical to State A's hand-written jsonata.d.ts. The published "
           "declarations are the original's file under a new path."
           if same else
           ". The grader builds the tree itself; a real build overwrites this and "
           "a build that emits no declarations leaves it, and the outcome looks "
           "the same from stage 2.")
    )


@pytest.mark.parametrize("name", NAMESPACE_MEMBERS)
def test_namespace_member_is_declared_in_source(name: str):
    """One of the six namespace members, looked for in the submission's own `src/`.

    `JsonataOptions`, `Expression`, `ExprNode`, `JsonataError`, `Environment`,
    `Focus`. Declarations emitted by `tsc` describe types the source declares, so
    each of these should be declarable somewhere under `src/` -- as an `interface`,
    a `type`, a `class`, or an `enum`.

    This is the weakest-looking check in the module and the one that catches the
    cheat the others miss, so it is worth being precise about what it does and does
    not establish. A port that named its own types differently and re-exported them
    under these names is legitimate and will fail this check; the message says so.
    What the check makes visible is the opposite case: a `dist/jsonata.d.ts` that
    names all six while `src/` declares none of them was not emitted from `src/`.

    Searched by declaration keyword rather than by bare occurrence, so a name in a
    comment or in an import does not satisfy it -- an `import { Expression }` is the
    submission consuming the name, not declaring it.
    """
    if name == MISSING:
        pytest.fail("the contract's namespace_members list was unreadable")
    declares = re.compile(
        r"\b(?:interface|type|class|enum|declare\s+(?:interface|type|class|const))"
        r"\s+" + re.escape(name) + r"\b")
    sources = [p for p in srbscan.walk_source(REPO)
               if p.suffix in srbscan.TYPESCRIPT_SUFFIXES
               and not srbscan.is_declared_output(p, REPO)]
    for path in srbscan.authored(sources):
        text = srbscan.read_text(path)
        match = declares.search(text)
        if match:
            return
    mentioned = sorted(
        f"{rel(REPO, p)}:{srbscan.locate(p, name)}"
        for p in sources if name in srbscan.read_text(p))
    pytest.fail(
        f"no file under src/ declares {name} as an interface, type, class or "
        f"enum. "
        + (f"It is mentioned at {mentioned[:6]} without being declared. "
           if mentioned else "It does not appear in the source at all. ")
        + f"Declarations emitted by tsc describe types the source declares. A "
        f"port that named its types differently and re-exported them under the "
        f"published names is legitimate -- check whether it did before treating "
        f"this as a finding."
    )


def test_expression_members_are_reachable():
    """`evaluate`, `assign`, `registerFunction`, `ast`, `errors`, in the source.

    The five members of the `Expression` the library returns. Unlike the namespace
    types these are runtime members, so they must exist as properties or methods in
    the ported code -- an emitted `.d.ts` that declares `errors()` while nothing in
    `src/` defines it describes an API the port does not have.

    `errors` is the one to watch. Upstream's own `jsonata.d.ts` omits it; State A's
    copy adds it, because `src/probe.js` calls it and the probe is what every stage
    of grading speaks to. A port that worked from upstream's published types rather
    than from State A's file would plausibly leave it out, and the protocol needs
    it -- so this check failing on `errors` alone points at a specific and likely
    mistake rather than at dishonesty.

    One check for all five rather than five parametrized checks, because the useful
    finding is which of them are missing *together*: none missing is silence, all
    five missing means the source was not searched correctly, and one missing is the
    row above.
    """
    if EXPRESSION_MEMBERS == [MISSING]:
        pytest.fail("the contract's expression_members list was unreadable")
    sources = srbscan.authored([
        p for p in srbscan.walk_source(REPO)
        if p.suffix in srbscan.TYPESCRIPT_SUFFIXES
        and not srbscan.is_declared_output(p, REPO)])
    corpus = {rel(REPO, p): srbscan.read_text(p) for p in sources}
    if not corpus:
        pytest.fail("no authored TypeScript under src/ to search")

    missing = []
    for name in EXPRESSION_MEMBERS:
        defines = re.compile(
            r"(?:\b" + re.escape(name) + r"\s*[:(=]"          # member or property
            r"|\bfunction\s+" + re.escape(name) + r"\b"       # function decl
            r"|\b" + re.escape(name) + r"\s*\()")             # method shorthand
        if not any(defines.search(text) for text in corpus.values()):
            missing.append(name)

    assert not missing, (
        f"{missing} do not appear as a definition anywhere in the "
        f"{len(corpus)} authored source file(s), though the published "
        f"declarations must describe all of {EXPRESSION_MEMBERS}. "
        + ("`errors` in particular: upstream's published types omit it and State "
           "A's jsonata.d.ts adds it, because src/probe.js calls it and the probe "
           "is what every graded stage speaks to. A port working from upstream's "
           "types rather than State A's file would leave it out."
           if "errors" in missing else "")
    )
