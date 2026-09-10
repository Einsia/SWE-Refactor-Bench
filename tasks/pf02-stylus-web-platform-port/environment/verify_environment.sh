#!/bin/sh
# =============================================================================
# Image-build sanity check for the pf02 agent environment.
#
# Runs against a throwaway copy so that /workspace/repo is handed to the agent
# exactly as unpacked, and is deleted from the image in the same layer that
# runs it.
#
# Everything here is a fact about the *environment*, not about a submission:
#
#   1. the pinned upstream version is what we think it is
#   2. the library entry point loads and compiles
#   3. the command line runs
#   4. the upstream test suite passes in full
#   5. State A reads no clock and no random source, so comparing a port's
#      output against State A's output byte-for-byte is a sound thing to do
#
# Nothing here scores anything and nothing here is a grading gate.  It exists so
# that a broken image fails at build time instead of being blamed on the agent.
# =============================================================================
set -eu

ROOT="${1:?usage: verify_environment.sh <repo-root>}"
cd "$ROOT"

fail() { echo "FATAL(environment): $*" >&2; exit 1; }

# 1. The package identity is the pinned upstream release.
VERSION="$(node -p "require('./package.json').version")"
[ "$VERSION" = "0.63.0" ] || fail "expected stylus 0.63.0, got $VERSION"

# 2. The library loads through its documented entry point and compiles.
node -e '
  const stylus = require("./");
  if (stylus.version !== "0.63.0") throw new Error("version mismatch: " + stylus.version);
  const css = stylus.render("a\n  color red\n");
  if (css.trim() !== "a {\n  color: #f00;\n}") throw new Error("render mismatch: " + JSON.stringify(css));
' || fail "library entry point does not compile"

# 3. The command line runs.
printf 'a\n  color red\n' | node ./bin/stylus > ./.cli-out.css || fail "CLI failed"
grep -q '#f00' ./.cli-out.css || fail "CLI output unexpected: $(cat ./.cli-out.css)"
rm -f ./.cli-out.css

# 4. The upstream suite passes in full.
OUT="$(npx --no-install mocha test/ test/middleware/ --require chai --reporter dot 2>&1)" \
  || { echo "$OUT" >&2; fail "upstream mocha suite failed"; }
echo "$OUT" | tail -3
PASSING="$(printf '%s' "$OUT" | sed -n 's/.*[^0-9]\([0-9][0-9]*\) passing.*/\1/p' | tail -1)"
[ -n "$PASSING" ] || fail "could not read passing count"
[ "$PASSING" -ge 380 ] || fail "expected >=380 upstream tests passing, got $PASSING"

# 5. Determinism.  If State A read a clock or an RNG, no differential
#    comparison against it would mean anything.
node -e '
  const fs = require("fs"), path = require("path");
  const bad = [];
  const walk = (d) => {
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) { walk(p); continue; }
      if (!/\.js$/.test(p)) continue;
      const src = fs.readFileSync(p, "utf8");
      for (const pat of [/Date\.now/, /new Date\b/, /Math\.random/, /process\.hrtime/]) {
        if (pat.test(src)) bad.push(p + " :: " + pat);
      }
    }
  };
  walk("lib");
  const cli = fs.readFileSync("bin/stylus", "utf8");
  if (/Math\.random/.test(cli)) bad.push("bin/stylus :: /Math\\.random/");
  if (bad.length) throw new Error("nondeterminism in State A: " + bad.join(", "));
' || fail "State A is not deterministic; differential scoring would be unsound"

# 6. The case files the graded stages read exist and are plentiful.  A snapshot
#    that unpacked without test/cases would make stage 2 look like a submission
#    failure rather than a broken image.
CASES="$(find test/cases -name '*.styl' | wc -l)"
[ "$CASES" -ge 300 ] || fail "expected >=300 .styl cases, found $CASES"

echo "environment OK: stylus $VERSION, $PASSING upstream tests passing, $CASES cases"
