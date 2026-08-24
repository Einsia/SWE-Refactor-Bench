#!/usr/bin/env python3
"""Generate a compilable Go module that has the contract's API and no behavior.

Four jobs, all of them things that otherwise get found late:

  1. It type-checks the four probe tier binaries.  A probe that does not compile
     against the published API is a probe that can never run, and the only way to
     know is to compile it against something.  There is no reference Go
     implementation to compile against, so this is it.

  2. It proves the contract is a compilable API rather than prose that looks like
     one.  A signature that mentions a type nobody declares, or a method on a
     type that was never listed, fails here instead of in a model's run.

  3. It round-trips: apidump over this skeleton must reproduce the contract's
     symbol lists exactly.  That makes the structural gate machine-checked in
     both directions -- the contract can express what apidump can read, and
     apidump can read what the contract expresses.

  4. It is the honest rejection case.  A submission with the whole public
     surface and no implementation must pass the API gate and score zero on
     behavior; running this through the real verifier is how that gets proven,
     rather than asserted in a design document.

Every body panics.  The one exception is `const Version`, which needs a value to
compile at all, and whose value is contractual anyway.

This lives in the verifier's own tree rather than in an authoring script because
freeze.py runs it: the generator, the API dump and the four tier compiles all
happen while the grading image is being built, so a contract that stopped being
expressible fails the build instead of every submission that follows.  Nothing it
writes survives into the image -- the skeleton is built in a scratch directory
and discarded, and shipping it would be shipping a third of the answer.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = "github.com/andialbrecht/sqlparse-go"

# Which import a package qualifier needs.  `io` is the only non-module one in the
# contract; anything else appearing as a qualifier is an error rather than a
# guess, because guessing would invent an import the contract never asked for.
STDLIB_QUALIFIERS = {"io": "io"}


class Signature:
    """One parsed contract symbol line."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        self.kind = ""        # func | method | type | value
        self.recv = ""        # "*Node", "Item", "TokenType"
        self.name = ""
        self.params: list[str] = []
        self.results: list[str] = []
        self.underlying = ""  # struct | interface | int
        self._parse()

    def _parse(self) -> None:
        raw = self.raw.strip()
        if raw.startswith("func ("):
            # Two shapes start with "func (": a method, "func (*Node).Name()",
            # and a func whose first parameter list is empty, which does not
            # occur.  The ").": is what separates them.
            close = raw.index(")")
            self.recv = raw[len("func ("):close]
            rest = raw[close + 1:]
            if not rest.startswith("."):
                raise ValueError(f"unparsed signature: {raw}")
            self.kind = "method"
            self._parse_call(rest[1:])
            return
        head, _, rest = raw.partition(" ")
        if head == "func":
            self.kind = "func"
            self._parse_call(rest)
        elif head == "type":
            self.kind = "type"
            parts = rest.split()
            self.name = parts[0]
            self.underlying = parts[1] if len(parts) > 1 else "struct"
        elif head == "value":
            # One shape for var and const both. The contract does not pick
            # between them -- apidump renders them the same way so the gate
            # never enforces a storage class -- so the skeleton picks whichever
            # is natural per family, below.
            self.kind = "value"
            self.name = rest.strip()
        else:
            raise ValueError(f"unknown symbol kind: {raw}")

    def _parse_call(self, text: str) -> None:
        open_paren = text.index("(")
        self.name = text[:open_paren]
        depth = 0
        for i in range(open_paren, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    params = text[open_paren + 1:i]
                    results = text[i + 1:].strip()
                    break
        else:
            raise ValueError(f"unbalanced parentheses: {text}")
        self.params = split_types(params)
        if results.startswith("(") and results.endswith(")"):
            results = results[1:-1]
        self.results = split_types(results)


def split_types(text: str) -> list[str]:
    """Split a type list on top-level commas.

    Top-level: `map[string]any` has no comma but `[][2][]*Node` has brackets, and
    a naive split on "," would cut `map[string]func(int, int)` in half.  No such
    type is in the contract today, and the depth counter is here so that adding
    one is a change to the contract rather than a silent miscompile.
    """
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    depth = 0
    current = ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
            continue
        current += ch
    if current.strip():
        out.append(current.strip())
    return out


def qualifiers(types: list[str]) -> set[str]:
    """Which package qualifiers a list of types mentions."""
    found = set()
    for t in types:
        for m in re.finditer(r"\b([a-z][a-z0-9]*)\.[A-Z]", t):
            found.add(m.group(1))
    return found


# ---------------------------------------------------------------------------
# Hand-written bodies.
#
# Three declarations cannot be generated from a signature alone, and each is
# hand-written here rather than guessed:
#
#   tokens.TokenType   the contract requires == and map-key usability, so the
#                      field cannot be a slice.  A string path with interning
#                      through Sub satisfies both.
#   tokens' 22 vars    "var Keyword" carries no type in the contract, because
#                      there is only one type it could have.
#   filters.Filter     the method set is explicitly unconstrained, so the
#                      skeleton picks an unexported one, which keeps it
#                      unimplementable from outside and makes the point that the
#                      13 constructors are the only way in.
# ---------------------------------------------------------------------------

TOKENS_PRELUDE = '''
// TokenType is a string path rather than a slice of segments, because the
// contract requires == and map-key usability and a slice field would make the
// struct uncomparable. The reference's lazy __getattr__ interning is modelled by
// Sub deriving the path deterministically: equal paths compare equal without a
// cache, which is a stronger guarantee than the reference's identity caching and
// indistinguishable through the published API.
type TokenType struct{ path string }

func (t TokenType) String() string {
	if t.path == "" {
		return "Token"
	}
	return "Token." + t.path
}

func (t TokenType) Sub(name string) TokenType {
	if t.path == "" {
		return TokenType{path: name}
	}
	return TokenType{path: t.path + "." + name}
}

func (t TokenType) Parent() TokenType {
	i := strings.LastIndex(t.path, ".")
	if i < 0 {
		return TokenType{}
	}
	return TokenType{path: t.path[:i]}
}

func (t TokenType) Contains(o TokenType) bool {
	if t.path == "" {
		return true
	}
	return o.path == t.path || strings.HasPrefix(o.path, t.path+".")
}

func Lookup(name string) (TokenType, bool) {
	if name == "Token" {
		return TokenType{}, true
	}
	if !strings.HasPrefix(name, "Token.") {
		return TokenType{}, false
	}
	return TokenType{path: name[len("Token."):]}, true
}
'''

FILTERS_PRELUDE = '''
// Filter's method set is unconstrained by the contract: the 13 constructors are
// the only way to obtain one, and what is graded is the output of the composed
// stack. The skeleton picks an unexported method, which keeps the interface
// closed to outside implementations and makes that explicit in the type system.
type Filter interface {
	filter()
}

type skeletonFilter struct{}

func (skeletonFilter) filter() {}
'''

# The generated declarations these preludes already provide, so the generator
# does not emit a second copy.
PRELUDE_PROVIDES = {
    "tokens": {"type TokenType struct",
               "func (TokenType).String() string",
               "func (TokenType).Sub(string) TokenType",
               "func (TokenType).Parent() TokenType",
               "func (TokenType).Contains(TokenType) bool",
               "func Lookup(string) (TokenType, bool)"},
    "filters": {"type Filter interface"},
}

# The 22 token type vars, with the paths the reference's names imply.  Written
# down because "var Keyword" does not say what Keyword is; the paths themselves
# are graded through the ttype family, and this skeleton getting one wrong shows
# up there rather than being silently accepted.
TOKEN_VAR_PATHS = {
    "Token": "", "Text": "Text", "Whitespace": "Text.Whitespace",
    "Newline": "Text.Whitespace.Newline", "Error": "Error", "Other": "Other",
    "Keyword": "Keyword", "Name": "Name", "Literal": "Literal",
    "String": "Literal.String", "Number": "Literal.Number",
    "Punctuation": "Punctuation", "Operator": "Operator",
    "Comparison": "Operator.Comparison", "Wildcard": "Wildcard",
    "Comment": "Comment", "Assignment": "Assignment", "Generic": "Generic",
    "Command": "Generic.Command", "DML": "Keyword.DML", "DDL": "Keyword.DDL",
    "CTE": "Keyword.CTE",
}

ZERO = {
    "string": '""', "int": "0", "bool": "false", "error": "nil",
    "any": "nil", "map[string]any": "nil",
}


def zero_value(typ: str) -> str:
    """A compilable zero for a result type.

    Only reached for the results of panicking bodies, so it is never evaluated at
    run time -- but `panic()` alone does not satisfy Go's return analysis in
    every shape, and a named type needs its own zero rather than nil.
    """
    if typ in ZERO:
        return ZERO[typ]
    if typ.startswith(("*", "[]", "map[", "chan ", "func(")):
        return "nil"
    if typ.endswith("Kind"):
        return "0"
    return typ + "{}"


def emit_package(pkg_dir: str, spec: dict, module: str) -> str:
    """One package's source text."""
    pkg_name = spec["package_name"]
    short = pkg_dir.rsplit("/", 1)[-1] if pkg_dir != "." else "sqlparse"
    prelude = ""
    provided: set[str] = set()
    if short in PRELUDE_PROVIDES:
        provided = PRELUDE_PROVIDES[short]
        prelude = {"tokens": TOKENS_PRELUDE,
                   "filters": FILTERS_PRELUDE}[short]

    sigs = [Signature(s) for s in spec["symbols"] if s not in provided]
    body: list[str] = []
    needs: set[str] = set()
    if short == "tokens":
        needs.add("strings")

    # Types first: a method's receiver has to be declared in the same package,
    # and putting them in signature order would emit methods before their type.
    for sig in sigs:
        if sig.kind != "type":
            continue
        if sig.underlying == "struct":
            # A struct with declared fields gets them; the probe constructs
            # filters.ReindentConfig by field name, so a skeleton without the
            # fields fails to compile the probe rather than answering `defect`
            # for every case -- and the skeleton is the rejection case, so it
            # has to fail the way a wrong submission fails, not the way a broken
            # verifier fails.
            decl = spec.get("fields", {}).get(sig.name, [])
            if decl:
                lines = [f"type {sig.name} struct {{"]
                for line in decl:
                    rest = line[len("field "):]
                    name, gotype = rest.split(".", 1)[1].split(" ", 1)
                    lines.append(f"\t{name} {gotype}")
                lines.append("}")
                body.append("\n".join(lines))
            else:
                body.append(f"type {sig.name} struct{{}}")
        elif sig.underlying == "interface":
            body.append(f"type {sig.name} interface{{ {sig.name.lower()}() }}")
        else:
            body.append(f"type {sig.name} {sig.underlying}")
    kind_consts = [s.name for s in sigs
                   if s.kind == "value" and s.name.startswith("Kind")]
    if kind_consts:
        # Emitted as one iota block in the contract's declared order, not
        # alphabetically, because the contract's `kinds` list is ordered and the
        # order is the reference's class declaration order. Nothing graded
        # depends on the numeric values -- Kind.String() is what crosses the
        # wire -- but a block is how a port would write it, and the skeleton is
        # also a worked example of the API.
        declared = spec.get("kinds", [])
        ordered = [n for n in declared if n in set(kind_consts)]
        if len(ordered) != len(kind_consts):
            # The two lists come from one place -- the assembler folds `kinds`
            # into `symbols` -- so a disagreement means the assembler changed
            # and this generator did not.
            missing = sorted(set(kind_consts) - set(ordered))
            raise SystemExit(f"skeleton: Kind constants absent from the "
                             f"contract's ordered kinds list: {missing}")
        lines = ["const ("]
        lines.append(f"\t{ordered[0]} Kind = iota")
        lines += [f"\t{n}" for n in ordered[1:]]
        lines.append(")")
        body.append("\n".join(lines))
    for sig in sigs:
        if sig.kind != "value" or sig.name.startswith("Kind"):
            continue
        if sig.name == "Version":
            # The one string value in the skeleton, because it needs one to
            # compile and this one is contractual: the version is the
            # reference's, and preserving it is part of preserving the release.
            body.append('const Version = "0.5.3"')
        elif short == "tokens":
            path = TOKEN_VAR_PATHS[sig.name]
            body.append(f'var {sig.name} = TokenType{{path: "{path}"}}')
        else:
            raise SystemExit(f"skeleton: unhandled value {short}.{sig.name}")
    for sig in sigs:
        if sig.kind not in ("func", "method"):
            continue
        needs |= {STDLIB_QUALIFIERS[q] for q in qualifiers(sig.params + sig.results)
                  if q in STDLIB_QUALIFIERS}
        for q in qualifiers(sig.params + sig.results):
            if q not in STDLIB_QUALIFIERS and q != short:
                needs.add(module + "/" + q)
        params = ", ".join(f"a{i} {t}" for i, t in enumerate(sig.params))
        if len(sig.results) == 0:
            rets = ""
        elif len(sig.results) == 1:
            rets = " " + sig.results[0]
        else:
            rets = " (" + ", ".join(sig.results) + ")"
        recv = ""
        if sig.kind == "method":
            recv_type = sig.recv
            base = recv_type.lstrip("*")
            recv = f"(r {recv_type}) "
            _ = base
        body.append(
            f"func {recv}{sig.name}({params}){rets} {{\n"
            f"\tpanic(\"skeleton: {short}.{sig.name} has no implementation\")\n"
            f"}}"
        )
        _ = zero_value  # kept for shapes panic alone cannot satisfy

    imports = ""
    if needs:
        lines = "\n".join(f'\t"{n}"' for n in sorted(needs))
        imports = f"import (\n{lines}\n)\n\n"
    header = (
        "// Code generated by _work/lang03/mkskeleton.py. DO NOT EDIT.\n"
        "//\n"
        "// The published API with no implementation. Every body panics: this\n"
        "// module exists to type-check the probe and to prove the contract is a\n"
        "// compilable surface, not to parse SQL.\n"
        f"package {pkg_name}\n\n"
    )
    return header + imports + prelude.strip() + "\n\n" + "\n\n".join(body) + "\n"


def generate(contract: dict, out: Path) -> tuple[int, int]:
    """Write the skeleton module to `out`.  Returns (packages, symbols).

    The directory is replaced rather than merged: a stale file from an earlier
    contract would compile, and the whole point of the freeze-time run is that
    what compiles is what the contract says today.
    """
    go = contract["go_contract"]
    module = go["module_path"]
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "go.mod").write_text(f"module {module}\n\ngo 1.22\n")

    written = 0
    for pkg_dir, spec in sorted(go["packages"].items()):
        target = out if pkg_dir == "." else out / pkg_dir
        target.mkdir(parents=True, exist_ok=True)
        if not spec["symbols"] and spec["package_name"] == "main":
            # cmd/sqlformat has no exported surface by design: a main package's
            # contract is its command-line behavior, which the cli family grades
            # by running the binary. It still needs to compile, so it gets a main.
            (target / "main.go").write_text(
                "// Code generated by _work/lang03/mkskeleton.py. DO NOT EDIT.\n"
                "package main\n\nfunc main() {\n"
                "\tpanic(\"skeleton: sqlformat has no implementation\")\n}\n"
            )
            written += 1
            continue
        name = "sqlparse.go" if pkg_dir == "." else \
            spec["package_name"] + ".go"
        (target / name).write_text(emit_package(pkg_dir, spec, module))
        written += 1
    n = sum(len(s["symbols"]) for s in go["packages"].values())
    return written, n


CHANGELOG_ENTRY = (
    "Release 0.6.0 (unreleased)\n"
    "--------------------------\n"
    "\n"
    "* The implementation was ported from Python to Go. The library is now a Go\n"
    "  module (github.com/andialbrecht/sqlparse-go) with no dependencies outside\n"
    "  the standard library, and sqlformat is a statically linked binary.\n"
    "\n"
)

SKELETON_TEST = '''package sqlparse

import "testing"

// The skeleton's bodies all panic, so this recovers.  It exists so the module
// carries a test at all -- which is what the advisory tests-exist gate looks for
// -- and so `go test ./...` has a green path to report.  A real submission's tests
// would assert behavior; this asserts only that the entry point is reachable.
func TestFormatIsReachable(t *testing.T) {
\tdefer func() {
\t\tif r := recover(); r != nil {
\t\t\tt.Logf("skeleton panicked as expected: %v", r)
\t\t}
\t}()
\tif _, err := Format("select 1", nil); err != nil {
\t\tt.Logf("Format returned %v", err)
\t}
}
'''


def dress(out: Path, baseline: Path, contract: dict) -> None:
    """Turn the bare API into a plausible State B tree.

    The skeleton is a module with a public surface and no behavior, which is also
    what a submission that gave up looks like -- so it is the honest rejection
    case, and the two validation passes are about scoring, not about the
    case.  That only holds if it carries what the contract says survives: the
    preserved files come from the baseline byte for byte (a copy that reformatted
    the licence would be flagged by the audit stage, and the report would be
    about this function), the changelog keeps its history and gains an entry, and
    the man page
    is placed where the install inventory expects it because `go install` cannot
    put it there.
    """
    for rel in sorted(contract["preserved_paths"]):
        src = baseline / rel
        if not src.is_file():
            raise SystemExit(f"skeleton: baseline has no {rel} to preserve")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    # The man page, at the installed path as well as the source one.  `go install`
    # places one file and it is the binary, so a submission that satisfies the
    # inventory has to ship this itself; the case does the same thing.
    manpage = baseline / "docs" / "sqlformat.1"
    if manpage.is_file():
        installed = out / "share" / "man" / "man1" / "sqlformat.1"
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(manpage.read_bytes())
    changelog = out / "CHANGELOG"
    if changelog.is_file():
        changelog.write_text(CHANGELOG_ENTRY + changelog.read_text())
    (out / "sqlparse_test.go").write_text(SKELETON_TEST)


def main() -> int:
    # HERE is lib/, and the contract ships in the suite's data/ directory -- the
    # same place freeze.py resolves it from.  This entry point is how job 4 above
    # gets carried out (generate the skeleton, grade it through the real verifier
    # to prove the honest rejection case), so it has to find the contract without
    # the caller already holding one; freeze.py imports generate() directly and
    # passes a contract it loaded itself, which is why this path can rot unnoticed.
    tests = HERE.parent
    contract = json.loads((tests / "data" / "source-contract.json").read_text())
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "skeleton"
    packages, symbols = generate(contract, out)
    print(f"wrote {out}: {packages} packages, {symbols} contract symbols")
    return 0


if __name__ == "__main__":
    sys.exit(main())
