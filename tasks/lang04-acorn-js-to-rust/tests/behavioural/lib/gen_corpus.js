#!/usr/bin/env node
/*
 * Builds the differential corpus: one NDJSON request per line, plus a sidecar
 * describing which scored family each request belongs to.
 *
 *   gen_corpus.js --out DIR [--seed N] [--families a,b,c]
 *
 * Writes DIR/requests.ndjson and DIR/cases.json.
 *
 * Two sources of input:
 *
 *   harvested   the 3,393 snippets the upstream test suite feeds its own
 *               parser, lifted straight out of test/driver.js's accumulator.
 *               These are the cases acorn's authors thought worth writing down,
 *               which makes them exactly the cases a port breaks on.
 *   generated   fresh sources from a seeded grammar walker, so a submission
 *               cannot be tuned to the frozen set.
 *
 * The corpus is fully determined by --seed. Same seed, same bytes.
 */
"use strict"

const fs = require("fs")
const path = require("path")

const REPO = process.env.ACORN_REFERENCE_REPO || "/opt/swerefactor/reference"

// ---------------------------------------------------------------------------
// Deterministic PRNG (xorshift128+, so it does not depend on the Node version)
// ---------------------------------------------------------------------------
class Rng {
  constructor(seed) {
    // splitmix64-ish seeding into four 32-bit words
    let s = seed >>> 0
    this.a = (s = (s + 0x9e3779b9) >>> 0) || 1
    this.b = (s = (s ^ (s >>> 15)) * 0x85ebca6b >>> 0) || 2
    this.c = (s = (s ^ (s >>> 13)) * 0xc2b2ae35 >>> 0) || 3
    this.d = (s = (s ^ (s >>> 16)) >>> 0) || 4
    for (let i = 0; i < 16; i++) this.next()
  }
  next() {
    let t = this.d, s = this.a
    this.d = this.c; this.c = this.b; this.b = s
    t ^= t << 11; t ^= t >>> 8
    this.a = (t ^ s ^ (s >>> 19)) >>> 0
    return this.a
  }
  int(n) { return n <= 0 ? 0 : this.next() % n }
  pick(arr) { return arr[this.int(arr.length)] }
  chance(num, den) { return this.int(den) < num }
  shuffled(arr) {
    const out = arr.slice()
    for (let i = out.length - 1; i > 0; i--) {
      const j = this.int(i + 1)
      const t = out[i]; out[i] = out[j]; out[j] = t
    }
    return out
  }
}

// ---------------------------------------------------------------------------
// Emitter
// ---------------------------------------------------------------------------
class Corpus {
  constructor() {
    this.requests = []
    this.cases = []
  }
  add(family, req, meta) {
    const id = this.requests.length + 1
    this.requests.push(Object.assign({id}, req))
    const c = {id, family, op: req.op}
    if (meta) Object.assign(c, meta)
    this.cases.push(c)
    return id
  }
  get size() { return this.requests.length }
}

// ---------------------------------------------------------------------------
// Harvest the upstream suite
// ---------------------------------------------------------------------------
function harvest() {
  const testDir = path.join(REPO, "test")
  const driverPath = path.join(testDir, "driver.js")
  const driver = require(driverPath)
  const out = []
  driver.test = (code, ast, options) => out.push({code, options, kind: "ast"})
  driver.testFail = (code, message, options) => out.push({code, options, kind: "fail", message})
  driver.testAssert = (code, fn, options) => out.push({code, options, kind: "assert"})
  for (const f of fs.readdirSync(testDir).filter((f) => /^tests.*\.js$/.test(f)).sort()) {
    require(path.join(testDir, f))
  }
  // The suite's own convention: no ecmaVersion means 5.  Options are otherwise
  // passed through untouched, including the ones that change node shape
  // (locations, ranges, preserveParens, sourceFile) and the two callback
  // options, which only parse_collect can express.
  return out.map((t) => {
    const o = t.options ? JSON.parse(JSON.stringify(t.options)) : {}
    if (!o.ecmaVersion) o.ecmaVersion = 5
    delete o.loose        // driver-only filter flag, not an acorn option
    delete o.onComment    // callback options are expressed by parse_collect
    delete o.onToken
    delete o.onInsertedSemicolon
    delete o.onTrailingComma
    return {code: t.code, options: o, kind: t.kind,
            wantsComments: !!(t.options && t.options.onComment),
            wantsTokens: !!(t.options && t.options.onToken)}
  })
}

// ---------------------------------------------------------------------------
// Hand-written seeds for the surfaces upstream does not stress
// ---------------------------------------------------------------------------
// Structurally varied but small, so a walk over them is readable when it fails.
const WALK_SEEDS = [
  "a.b(c)",
  "let {x, y: [z = 1]} = obj",
  "class A extends B { #p = 1; static m() { return this.#p } }",
  "for (const k in o) if (k) continue; else break",
  "try { f() } catch ({message}) { g(message) } finally { h() }",
  "label: for (;;) { switch (x) { case 1: break label; default: } }",
  "async function* g() { for await (const v of s) yield* v }",
  "(a, b = 1, ...rest) => ({ [k]: v, ...spread })",
  "x?.y?.[z]?.(w) ?? d",
  "`a${b}c${`${d}`}e`",
  "with (o) { a = 1 }",
  "do a++; while (b)",
  "new.target; import.meta",
  "export default class extends (yield) {}",
  "function f(a = (function g(){ return 1 })()) { return a }",
  "if (a) { let b = /re[g]+/gi; b.test('x') }",
  "({ get p() {}, set p(v) {}, async *m() {} })",
  "a = b ? c ? d : e : f",
  "throw new Error('x')",
  "debugger",
]

const MODULE_WALK_SEEDS = [
  "import a, {b as c} from 'm'; export {c}; export * as ns from 'n'",
  "import('m').then(x => x)",
  "export const {a, b: [c]} = d",
  "import def from 'x' with { type: 'json' }",
]

// Sources whose line/column structure is worth probing.
const LINEINFO_SEEDS = [
  "a\nbb\nccc\n",
  "a\r\nbb\rccc",
  "\u2028a\u2029b",
  "line1\n\nline3",
  "\n",
  "",
  "no breaks at all",
  "tab\there\nand\there",
  "\u00a0nbsp\u2003emsp\n\ufeffbom",
  "a\r\r\nb",
]

const COMMENT_SEEDS = [
  "// leading\na // trailing\n/* block */ b /** doc */",
  "/*\nmulti\nline\n*/ x",
  "#!/usr/bin/env node\na",
  "a /* one */ /* two */ + /* three */ b",
  "// only a comment",
  "/**/",
  "a//\nb",
  "/* \u2028 */ a",
]

// Codepoints where the identifier tables have edges.
function identifierProbes(rng) {
  const fixed = [
    0, 9, 10, 32, 36, 45, 46, 47, 48, 57, 58, 64, 65, 90, 91, 94, 95, 96, 97,
    122, 123, 127, 128, 170, 171, 181, 183, 186, 192, 215, 216, 246, 247, 442,
    443, 452, 660, 661, 687, 688, 705, 710, 721, 736, 740, 748, 750, 768, 884,
    885, 886, 887, 890, 893, 895, 902, 903, 906, 908, 1155, 1159, 1425, 1469,
    1471, 1479, 1552, 1562, 1632, 1641, 1642, 2307, 3654, 3782, 6112, 6121,
    8204, 8205, 8206, 8231, 8232, 8233, 8234, 8255, 8256, 8276, 8305, 8319,
    8336, 8348, 8400, 8412, 8417, 8421, 8432, 11264, 12293, 12294, 12295,
    12296, 12321, 12329, 12337, 12341, 12344, 12348, 42888, 43000, 55295,
    55296, 56319, 56320, 57343, 57344, 63743, 63744, 64975, 65008, 65279,
    65280, 65313, 65338, 65535, 65536, 65547, 65548, 65574, 65576, 65594,
    66045, 66046, 66176, 66204, 66208, 66256, 66272, 66273, 68097, 68100,
    69632, 69634, 70000, 78894, 92160, 92728, 113823, 113824, 119141, 119146,
    120485, 123566, 125136, 125142, 130000, 173782, 173824, 177972, 177984,
    178205, 183969, 194560, 195101, 917505, 917536, 917631, 917632, 1114111,
    1114112, 2097151,
  ]
  const out = fixed.slice()
  // A scatter through the BMP and the astral planes, so the tables are probed
  // where nobody thought to put a boundary.
  for (let i = 0; i < 220; i++) out.push(rng.int(0x10000))
  for (let i = 0; i < 120; i++) out.push(0x10000 + rng.int(0x100000))
  return out
}

// ---------------------------------------------------------------------------
// Random source generator
// ---------------------------------------------------------------------------
// Walks a grammar to build sources nobody has seen.  It aims for variety over
// validity: roughly a fifth of what it produces does not parse, which is the
// point -- error position and message are where ports diverge.  Whatever it
// emits, the reference defines the answer, so an invalid source is as gradeable
// as a valid one.
// Plain identifiers, always legal as a binding or a reference.
const IDENTS = ["a", "b", "c", "x", "y", "z", "foo", "bar", "_", "$", "ᵃ", "π", "$_0", "of", "get", "set"]
// Contextual keywords: legal in some positions and at some versions, not
// others.  Drawn deliberately and rarely, not mixed into IDENTS, so they are a
// tested hazard rather than a background error rate.
const CONTEXTUAL = ["await", "yield", "async", "let", "static", "arguments", "eval", "undefined"]
const NUMBERS = ["0", "1", "42", "0.5", ".5", "5.", "1e3", "1e+21", "1e-7", "0x1f", "0o17", "0b101", "1_000", "0n", "10n", "0xffn", "1e21", "0.1e2", "1.7976931348623157e308", "5e-324", "0.0000001", "123456789012345678901234567890"]
const STRINGS = ["''", "\"d\"", "'\\n'", "'\\u0041'", "'\\u{1f600}'", "'\\x41'", "'\\0'", "'a\\\nb'", "'\\uD800'", "'✖'", "'\\t\\r'", "'\\''"]
const REGEXPS = ["/a/", "/a/g", "/[a-z]+/gi", "/(?<n>a)\\k<n>/", "/\\p{L}/u", "/a{2,}/y", "/(?<=a)b/", "/[\\p{ASCII}--[a-z]]/v", "/a|b/s", "/\\d{1,3}/dgi"]
// The invalid pool: drawn at a low rate, so error paths are covered without
// swamping the valid-AST coverage the shape families depend on.
const INVALID_ATOMS = ["09", "0777", "'\\8'", "/)/", "/a/gg", "/(?<n>a)(?<n>b)/",
                       "1n.5", "0b2", "0o8", "'\\u{}'", "`\\u{}`", "0x", "1__0"]
const BINOPS = ["+", "-", "*", "/", "%", "**", "==", "!=", "===", "!==", "<", ">", "<=", ">=", "&&", "||", "??", "&", "|", "^", "<<", ">>", ">>>", "instanceof", "in"]
const UNOPS = ["!", "-", "+", "~", "typeof ", "void ", "delete "]
function binopsFor(ver) {
  let pool = ["+", "-", "*", "/", "%", "==", "!=", "===", "!==", "<", ">", "<=",
              ">=", "&&", "||", "&", "|", "^", "<<", ">>", ">>>", "instanceof", "in"]
  if (ver >= 2016) pool = pool.concat(["**"])
  if (ver >= 2020) pool = pool.concat(["??"])
  return pool
}

function assignopsFor(ver) {
  let pool = ["=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", ">>>="]
  if (ver >= 2016) pool = pool.concat(["**="])
  if (ver >= 2021) pool = pool.concat(["&&=", "||=", "??="])
  return pool
}

const ASSIGNOPS = ["=", "+=", "-=", "*=", "/=", "%=", "**=", "&&=", "||=", "??=", "&=", "|=", "^=", "<<=", ">>=", ">>>="]

// The context a fragment is being generated into.  `return` needs a function,
// `break`/`continue` need a breakable, `await` needs an async function, `yield`
// needs a generator.  Emitting them without the enclosing construct is a syntax
// error every time, which is how a generator ends up producing four-fifths
// garbage.  `ver` is the numeric ecmaVersion the source will be parsed at, so a
// leaf can avoid reaching for syntax that did not exist yet; `strict` records
// that the source opened with a directive, which makes `with` an error
// everywhere in it.
const TOP = {fn: false, loop: false, brk: false, async: false, gen: false,
             ctor: false, module: false, strict: false, ver: 2025}
function ctx(base, over) { return Object.assign({}, base, over) }

// ecmaVersion as acorn takes it (3, 5, 6..16, "latest") mapped to the year form
// used for comparisons.
function verNum(v) {
  if (v === "latest") return 2025
  if (v > 2000) return v
  if (v <= 5) return v === 3 ? 3 : 5
  return 2009 + v          // 6 -> 2015, 11 -> 2020, 16 -> 2025
}

function ident(rng) {
  // A contextual keyword one time in nine: enough to hit the hazard, rare
  // enough to dominate nothing.
  return rng.chance(1, 9) ? rng.pick(CONTEXTUAL) : rng.pick(IDENTS)
}

// Atom pools filtered to what the target version accepts.  Without this the
// version choice and the syntax choice are independent, and most of the corpus
// is a version error rather than a parser test.
function numbersFor(ver) {
  let pool = ["0", "1", "42", "0.5", ".5", "5.", "1e3", "1e+21", "1e-7", "0x1f",
              "1e21", "0.1e2", "1.7976931348623157e308", "5e-324", "0.0000001",
              "123456789012345678901234567890"]
  if (ver >= 2015) pool = pool.concat(["0o17", "0b101"])
  if (ver >= 2020) pool = pool.concat(["0n", "10n", "0xffn"])
  if (ver >= 2021) pool = pool.concat(["1_000", "1_0.0_1", "0x1_f"])
  return pool
}

function stringsFor(ver) {
  let pool = ["''", "\"d\"", "'\\n'", "'\\x41'", "'\\0'", "'a\\\nb'", "'\\uD800'",
              "'\\u2028'", "'\\t\\r'", "'\\''"]
  if (ver >= 2015) pool = pool.concat(["'\\u{1f600}'", "'\\u{41}'"])
  return pool
}

function regexpsFor(ver) {
  let pool = ["/a/", "/a/g", "/[a-z]+/gi", "/a{2,}/y", "/a|b/"]
  if (ver >= 2015) pool = pool.concat(["/a/y", "/\\u{41}/u"])
  if (ver >= 2018) pool = pool.concat(["/(?<n>a)\\k<n>/", "/\\p{L}/u", "/(?<=a)b/", "/a|b/s"])
  if (ver >= 2022) pool = pool.concat(["/\\d{1,3}/dgi"])
  if (ver >= 2024) pool = pool.concat(["/[\\p{ASCII}--[a-z]]/v"])
  return pool
}

// Deliberately invalid at every version, drawn rarely.
const INVALID_ALWAYS = ["09", "0777", "'\\8'", "/)/", "/a/gg", "0b2", "0o8",
                        "'\\u{}'", "0x", "1__0"]

function genExpr(rng, depth, k) {
  k = k || TOP
  if (depth <= 0) {
    switch (rng.int(28)) {
      case 0: case 1: case 2: case 3: case 4: case 5: case 6: return ident(rng)
      case 7: case 8: case 9: case 10: return rng.pick(numbersFor(k.ver))
      case 11: case 12: case 13: return rng.pick(stringsFor(k.ver))
      case 14: case 15: return rng.pick(regexpsFor(k.ver))
      case 16: case 17: case 18: return rng.pick(["true", "false", "null", "this"])
      case 19: return rng.pick(INVALID_ALWAYS)
      default: return ident(rng)
    }
  }
  const d = depth - 1
  // Weighted: the constructs that are always valid are drawn more often than
  // the ones that depend on context.
  switch (rng.int(24)) {
    case 0: return `${genExpr(rng, d, k)} ${rng.pick(binopsFor(k.ver))} ${genExpr(rng, d, k)}`
    case 1: return `${rng.pick(UNOPS)}${genExpr(rng, d, k)}`
    case 2: return `${genExpr(rng, d, k)} ? ${genExpr(rng, d, k)} : ${genExpr(rng, d, k)}`
    case 3: return `${ident(rng)} ${rng.pick(assignopsFor(k.ver))} ${genExpr(rng, d, k)}`
    case 4: return `${genExpr(rng, d, k)}(${genArgs(rng, d, k)})`
    case 5: return `${genExpr(rng, d, k)}.${ident(rng)}`
    case 6: return `${genExpr(rng, d, k)}[${genExpr(rng, d, k)}]`
    case 7: return `[${genArgs(rng, d, k)}]`
    case 8: return `({${genProps(rng, d, k)}})`
    case 9: {
      const inner = ctx(k, {fn: true, loop: false, brk: false, async: false, gen: false, ctor: false})
      if (k.ver < 2015) return `function (${genParams(rng, d, k)}) { ${genStmt(rng, d, inner)} }`
      return `(${genParams(rng, d, k)}) => ${rng.chance(1, 2) ? genExpr(rng, d, inner) : `{ ${genStmt(rng, d, inner)} }`}`
    }
    case 10: {
      const gen = k.ver >= 2015 && rng.chance(1, 4)
      const asy = k.ver >= 2017 && rng.chance(1, 4)
      const inner = ctx(k, {fn: true, loop: false, brk: false, async: asy, gen: gen, ctor: false})
      return `${asy ? "async " : ""}function${gen ? "*" : ""} ${rng.chance(1, 2) ? ident(rng) : ""}(${genParams(rng, d, k)}) { ${genStmt(rng, d, inner)} }`
    }
    case 11: return `new ${genExpr(rng, d, k)}(${genArgs(rng, d, k)})`
    case 12: return k.ver >= 2015
             ? `\`t${rng.chance(3, 4) ? "${" + genExpr(rng, d, k) + "}" : ""}u\``
             : rng.pick(stringsFor(k.ver))
    case 13: return `(${genExpr(rng, d, k)})`
    case 14: return k.ver >= 2020 ? `${genExpr(rng, d, k)}?.${ident(rng)}`
                                  : `${genExpr(rng, d, k)}.${ident(rng)}`
    case 15: return `${rng.chance(1, 2) ? "++" : "--"}${rng.pick(IDENTS)}`
    case 16: return `${rng.pick(IDENTS)}${rng.chance(1, 2) ? "++" : "--"}`
    case 17: return k.ver >= 2015 ? `class ${rng.chance(1, 2) ? ident(rng) : ""} { ${genClassBody(rng, d, k)} }`
                                  : `(${genExpr(rng, d, k)})`
    case 18: {
      if (k.ver < 2017) return `${genExpr(rng, d, k)} ${rng.pick(binopsFor(k.ver))} ${genExpr(rng, d, k)}`
      const inner = ctx(k, {fn: true, loop: false, brk: false, async: true, gen: false, ctor: false})
      return `async (${genParams(rng, d, k)}) => ${genExpr(rng, d, inner)}`
    }
    case 19: return `${genExpr(rng, d, k)}, ${genExpr(rng, d, k)}`
    case 20: return k.ver >= 2015 ? `${genExpr(rng, d, k)}\`tag\``
                                  : `${genExpr(rng, d, k)}(${genArgs(rng, d, k)})`
    case 21: return k.async ? `await ${genExpr(rng, d, k)}` : `${genExpr(rng, d, k)} ${rng.pick(binopsFor(k.ver))} ${genExpr(rng, d, k)}`
    case 22: return k.gen ? `yield ${rng.chance(1, 3) ? "* " : ""}${genExpr(rng, d, k)}` : `${rng.pick(UNOPS)}${genExpr(rng, d, k)}`
    default: return genExpr(rng, d, k)
  }
}

function genArgs(rng, d, k) {
  const n = rng.int(4)
  const parts = []
  for (let i = 0; i < n; i++) {
    parts.push(k.ver >= 2015 && rng.chance(1, 6) ? `...${genExpr(rng, d, k)}` : genExpr(rng, d, k))
  }
  if (parts.length && rng.chance(1, 8)) parts.push("")   // trailing comma
  return parts.join(", ")
}

function genParams(rng, d, k) {
  const n = rng.int(4)
  const parts = []
  // Names are drawn without replacement: a repeated parameter name is a strict
  // mode error, which would fire on every source that has one rather than
  // testing anything about parameters.
  const pool = rng.shuffled(IDENTS)
  let next = 0
  const name = () => pool[next++ % pool.length]
  for (let i = 0; i < n; i++) {
    const last = i === n - 1
    switch (k.ver >= 2015 ? rng.int(6) : 5) {
      case 0: parts.push(`${name()} = ${genExpr(rng, 0, k)}`); break
      // Rest is only legal in final position, and only without a trailing comma.
      case 1: if (last) { parts.push(`...${name()}`) } else { parts.push(name()) } break
      case 2: parts.push(`{${name()}}`); break
      case 3: parts.push(`[${name()}, ${name()}]`); break
      case 4: parts.push(`{${name()}: ${name()} = ${genExpr(rng, 0, k)}}`); break
      default: parts.push(name())
    }
  }
  return parts.join(", ")
}

function genProps(rng, d, k) {
  const n = rng.int(4)
  const parts = []
  for (let i = 0; i < n; i++) {
    const mfn = ctx(k, {fn: true, loop: false, brk: false, ctor: false})
    switch (rng.int(8)) {
      case 0: parts.push(rng.pick(IDENTS)); break
      case 1: parts.push(`${ident(rng)}: ${genExpr(rng, d, k)}`); break
      case 2: parts.push(k.ver >= 2015 ? `[${genExpr(rng, d, k)}]: ${genExpr(rng, d, k)}`
                                       : `${ident(rng)}: ${genExpr(rng, d, k)}`); break
      case 3: parts.push(`${rng.pick(stringsFor(k.ver))}: ${genExpr(rng, d, k)}`); break
      case 4: parts.push(`${rng.pick(numbersFor(k.ver))}: ${genExpr(rng, d, k)}`); break
      case 5: parts.push(`get ${ident(rng)}() { ${genStmt(rng, 0, ctx(mfn, {async: false, gen: false}))} }`); break
      case 6: parts.push(k.ver >= 2018 ? `...${genExpr(rng, d, k)}`
                                       : `${ident(rng)}: ${genExpr(rng, d, k)}`); break
      default: {
        const mods = k.ver >= 2018 ? ["", "async ", "*", "async *"]
                   : k.ver >= 2017 ? ["", "async ", "*"]
                   : k.ver >= 2015 ? ["", "*"] : [""]
        const mod = rng.pick(mods)
        if (k.ver < 2015) { parts.push(`${ident(rng)}: ${genExpr(rng, d, k)}`); break }
        const inner = ctx(mfn, {async: mod.indexOf("async") >= 0, gen: mod.indexOf("*") >= 0})
        parts.push(`${mod}${ident(rng)}(${genParams(rng, d, k)}) { ${genStmt(rng, 0, inner)} }`)
      }
    }
  }
  return parts.join(", ")
}

function genClassBody(rng, d, k) {
  const n = rng.int(3)
  const parts = []
  const mfn = ctx(k, {fn: true, loop: false, brk: false, ctor: false})
  for (let i = 0; i < n; i++) {
    const stat = rng.chance(1, 3) ? "static " : ""
    switch (rng.int(7)) {
      case 0: parts.push(`${stat}${ident(rng)}() {}`); break
      case 1: parts.push(k.ver >= 2022 ? `${stat}${ident(rng)} = ${genExpr(rng, 0, k)}`
                                       : `${stat}${ident(rng)}() {}`); break
      case 2: parts.push(k.ver >= 2022 ? `${stat}#${rng.pick(IDENTS)} = ${genExpr(rng, 0, k)}`
                                       : `${stat}${ident(rng)} = ${genExpr(rng, 0, k)}`); break
      case 3: parts.push(k.ver >= 2022 ? `${stat}#${rng.pick(IDENTS)}() {}`
                                       : `${stat}${ident(rng)}() {}`); break
      case 4: parts.push(`${stat}get ${ident(rng)}() {}`); break
      case 5: parts.push(k.ver >= 2022 && stat
                         ? `static { ${genStmt(rng, 0, ctx(mfn, {async: false, gen: false}))} }`
                         : `${stat}${ident(rng)}() {}`); break
      default: parts.push("constructor() {}")
    }
  }
  return parts.join("; ")
}

function genStmt(rng, depth, k) {
  k = k || TOP
  if (depth <= 0) return `${genExpr(rng, 0, k)};`
  const d = depth - 1
  const loopK = ctx(k, {loop: true, brk: true})
  switch (rng.int(21)) {
    case 0: return `${k.ver >= 2015 ? rng.pick(["var", "let", "const"]) : "var"} ${rng.pick(IDENTS)} = ${genExpr(rng, d, k)};`
    case 1: return `if (${genExpr(rng, d, k)}) ${genStmt(rng, d, k)}${rng.chance(1, 2) ? ` else ${genStmt(rng, d, k)}` : ""}`
    case 2: return `for (${rng.chance(1, 2) ? "let i = 0" : ""}; ${genExpr(rng, d, k)}; ${genExpr(rng, d, k)}) ${genStmt(rng, d, loopK)}`
    case 3: return k.ver >= 2015
            ? `for (const ${rng.pick(IDENTS)} of ${genExpr(rng, d, k)}) ${genStmt(rng, d, loopK)}`
            : `for (var ${rng.pick(IDENTS)} in ${genExpr(rng, d, k)}) ${genStmt(rng, d, loopK)}`
    case 4: return `for (var ${rng.pick(IDENTS)} in ${genExpr(rng, d, k)}) ${genStmt(rng, d, loopK)}`
    case 5: return `while (${genExpr(rng, d, k)}) ${genStmt(rng, d, loopK)}`
    case 6: return `do ${genStmt(rng, d, loopK)} while (${genExpr(rng, d, k)});`
    case 7: return `{ ${genStmt(rng, d, k)} ${genStmt(rng, d, k)} }`
    case 8: return `try { ${genStmt(rng, d, k)} } catch ${k.ver >= 2019 && rng.chance(1, 3) ? "" : `(${rng.pick(IDENTS)})`} { ${genStmt(rng, d, k)} }`
    case 9: return `switch (${genExpr(rng, d, k)}) { case ${genExpr(rng, 0, k)}: ${genStmt(rng, d, ctx(k, {brk: true}))} default: ${genStmt(rng, d, ctx(k, {brk: true}))} }`
    case 10: {
      const gen = k.ver >= 2015 && rng.chance(1, 4)
      const asy = k.ver >= 2017 && rng.chance(1, 4)
      const inner = ctx(k, {fn: true, loop: false, brk: false, async: asy, gen: gen, ctor: false})
      return `${asy ? "async " : ""}function${gen ? "*" : ""} ${rng.pick(IDENTS)}(${genParams(rng, d, k)}) { ${genStmt(rng, d, inner)} }`
    }
    case 11: return k.ver >= 2015 ? `class ${ident(rng)} { ${genClassBody(rng, d, k)} }`
                                  : `function ${rng.pick(IDENTS)}() { ${genStmt(rng, 0, ctx(k, {fn: true}))} }`
    case 12: return k.fn ? `return ${rng.chance(1, 3) ? "" : genExpr(rng, d, k)};` : `${genExpr(rng, d, k)};`
    case 13: return `throw ${genExpr(rng, d, k)};`
    case 14: return `${rng.pick(IDENTS)}: ${genStmt(rng, d, k)}`
    case 15: return k.brk ? `break;` : k.loop ? `continue;` : `${genExpr(rng, d, k)};`
    case 16: return k.strict ? `'use strict';` : `debugger;`
    case 17: return `${genExpr(rng, d, k)}\n`
    case 18: return (k.strict || k.module) ? `${genExpr(rng, d, k)};`
                    : `with (${genExpr(rng, d, k)}) ${genStmt(rng, d, k)}`
    case 19: return k.loop ? `continue;` : `${genExpr(rng, d, k)};`
    default: return `${genExpr(rng, d, k)};`
  }
}

function genModuleStmt(rng, d, k) {
  switch (rng.int(9)) {
    case 0: return `import ${rng.pick(IDENTS)} from 'm';`
    case 1: return `import {${rng.pick(IDENTS)} as ${rng.pick(IDENTS)}} from 'm';`
    case 2: return `import * as ns from 'm';`
    case 3: return `export {${rng.pick(IDENTS)}};`
    case 4: return `export default ${genExpr(rng, d, k)};`
    case 5: return `export * as ns from 'm';`
    case 6: return `export const ${rng.pick(IDENTS)} = ${genExpr(rng, d, k)};`
    case 7: return `export {${rng.pick(IDENTS)} as 'str name'} from 'm';`
    default: return `import ${rng.pick(IDENTS)} from 'm' with { type: 'json' };`
  }
}

function genSource(rng, module, ecmaVersion) {
  const ver = verNum(ecmaVersion === undefined ? 2025 : ecmaVersion)
  // A module is always strict, and top-level await is only in a module at 2022+.
  const strict = module || rng.chance(1, 5)
  const k = ctx(TOP, {module: module, async: module && ver >= 2022,
                      strict: strict, ver: ver})
  const n = 1 + rng.int(4)
  const parts = []
  for (let i = 0; i < n; i++) {
    if (module && rng.chance(1, 3)) parts.push(genModuleStmt(rng, 1 + rng.int(2), k))
    else parts.push(genStmt(rng, 1 + rng.int(3), k))
  }
  let src = parts.join("\n")
  if (strict && !module) src = "'use strict';\n" + src
  // Lexical hazards, each rare on its own: a leading comment, an unterminated
  // one, a truncation, a hashbang, and the two characters that are only
  // whitespace or only a line break to a spec-correct tokenizer.
  if (rng.chance(1, 6)) src = "// c\n" + src
  if (rng.chance(1, 14)) src = src + " /* trailing"
  if (rng.chance(1, 16)) src = src.slice(0, Math.max(1, src.length - 1 - rng.int(4)))
  if (rng.chance(1, 12)) src = src.replace(" ", "\u00a0")
  if (rng.chance(1, 14)) src = src.replace("\n", "\u2028")
  const hashbang = rng.chance(1, 22)
  if (hashbang) src = "#!/usr/bin/env node\n" + src
  return {source: src, hashbang: hashbang}
}

const ECMA_VERSIONS = [3, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, "latest"]

// ---------------------------------------------------------------------------
// Families
// ---------------------------------------------------------------------------
// Each builder appends to the corpus and tags every request with its family.
// Family names are the scoring units; the verifier's weights table names them.

function familyParseUpstream(c, rng, snippets) {
  for (const s of snippets) {
    c.add("parse_upstream", {op: "parse", source: s.code, options: s.options},
          {kind: s.kind})
  }
}

// The option matrix: node shape changes with locations/ranges/preserveParens,
// and sourceFile/directSourceFile add properties.  Applied to a sample of
// upstream snippets that parse, so the shape change is observable.
function familyParseOptions(c, rng, snippets) {
  const ok = snippets.filter((s) => s.kind === "ast")
  const sample = rng.shuffled(ok).slice(0, 260)
  const matrix = [
    {locations: true},
    {ranges: true},
    {locations: true, ranges: true},
    {preserveParens: true},
    {locations: true, ranges: true, preserveParens: true},
    {sourceFile: "input.js"},
    {locations: true, sourceFile: "input.js"},
    {directSourceFile: "direct.js"},
    {allowReserved: true},
    {allowReserved: "never"},
    {allowReturnOutsideFunction: true},
    {allowImportExportEverywhere: true},
    {allowAwaitOutsideFunction: true},
    {allowSuperOutsideMethod: true},
    {allowHashBang: true},
    {checkPrivateFields: false},
    {tabSize: 2, locations: true},
    {tabSize: 8, locations: true},
  ]
  for (let i = 0; i < sample.length; i++) {
    const s = sample[i]
    const extra = matrix[i % matrix.length]
    const options = Object.assign({}, s.options, extra)
    c.add("parse_options", {op: "parse", source: s.code, options},
          {opts: Object.keys(extra).join("+")})
  }
}

// ecmaVersion is the single most behaviour-changing option: the same source is
// valid at one version and a syntax error at the one below.
function familyParseVersions(c, rng, snippets) {
  const sample = rng.shuffled(snippets).slice(0, 200)
  for (const s of sample) {
    for (const v of [5, 6, 11, "latest"]) {
      const options = Object.assign({}, s.options, {ecmaVersion: v})
      c.add("parse_versions", {op: "parse", source: s.code, options},
            {ecmaVersion: String(v)})
    }
  }
  // The absent-ecmaVersion path: warns on stderr, parses as 2020.
  for (const s of rng.shuffled(snippets).slice(0, 40)) {
    const options = Object.assign({}, s.options)
    delete options.ecmaVersion
    c.add("parse_versions", {op: "parse", source: s.code, options},
          {ecmaVersion: "absent"})
  }
}

function familyParseSourceType(c, rng, snippets) {
  const sample = rng.shuffled(snippets).slice(0, 180)
  for (const s of sample) {
    for (const st of ["script", "module", "commonjs"]) {
      const options = Object.assign({}, s.options, {sourceType: st})
      if (!options.ecmaVersion || options.ecmaVersion < 6) options.ecmaVersion = 2022
      c.add("parse_sourcetype", {op: "parse", source: s.code, options},
            {sourceType: st})
    }
  }
}

function familyParseCollect(c, rng, snippets) {
  for (const src of COMMENT_SEEDS) {
    for (const opts of [{ecmaVersion: 2022}, {ecmaVersion: 2022, locations: true},
                        {ecmaVersion: 2022, ranges: true, allowHashBang: true}]) {
      c.add("parse_collect", {op: "parse_collect", source: src, options: opts})
    }
  }
  const withCb = snippets.filter((s) => s.wantsComments || s.wantsTokens)
  for (const s of withCb) {
    c.add("parse_collect", {op: "parse_collect", source: s.code, options: s.options},
          {from: "upstream_cb"})
  }
  for (const s of rng.shuffled(snippets).slice(0, 220)) {
    c.add("parse_collect", {op: "parse_collect", source: s.code, options: s.options})
  }
}

function familyParseExpressionAt(c, rng, snippets) {
  const ok = snippets.filter((s) => s.kind === "ast" && s.code.length > 2)
  for (const s of rng.shuffled(ok).slice(0, 300)) {
    // Positions at, before and inside the first interesting token, plus a
    // deterministic scatter.
    const positions = [0, 1, Math.floor(s.code.length / 2), s.code.length - 1, s.code.length]
    const pos = positions[rng.int(positions.length)]
    c.add("parse_expression_at",
          {op: "parse_expression_at", source: s.code, pos, options: s.options},
          {pos})
  }
  // Hand-picked: a position that lands mid-token, and one past the end.
  for (const [source, pos] of [["a + b", 4], ["a + b", 2], ["a + b", 99], ["a + b", 0],
                               ["  {a: 1}  ", 2], ["x=>x", 0], ["/re/g", 0], ["10n", 0],
                               ["`t${u}v`", 0], ["(a,b)", 1], ["class{}", 0]]) {
    c.add("parse_expression_at",
          {op: "parse_expression_at", source, pos, options: {ecmaVersion: 2022}}, {pos})
  }
}

function familyTokenize(c, rng, snippets) {
  for (const s of rng.shuffled(snippets).slice(0, 420)) {
    c.add("tokenize", {op: "tokenize", source: s.code, options: s.options},
          {kind: s.kind})
  }
  // Token-context sensitive sources: whether `/` starts a regexp, and whether
  // `}` ends a template hole, both depend on the context stack.
  for (const src of ["a / b / c", "a = /re/g", "if (a) /re/.test(b)", "`a${b}c`",
                     "`${`${a}`}`", "x = {} / 2", "(a) / 2", "return /re/",
                     "typeof /re/", "a++ / 2", "case /re/:", "of / 2",
                     "let x = a\n/re/g.test(b)", "class A { m() {} / 2 }",
                     "${", "`unterminated", "`a${b`", "'\\u{110000}'"]) {
    c.add("tokenize", {op: "tokenize", source: src, options: {ecmaVersion: 2022}},
          {kind: "context"})
  }
}

function familyLooseParse(c, rng, snippets) {
  // Loose parsing is most interesting exactly where strict parsing fails.
  const failing = snippets.filter((s) => s.kind === "fail")
  for (const s of failing) {
    c.add("loose_parse", {op: "loose_parse", source: s.code, options: s.options},
          {kind: "fail"})
  }
  for (const s of rng.shuffled(snippets.filter((s) => s.kind === "ast")).slice(0, 300)) {
    c.add("loose_parse", {op: "loose_parse", source: s.code, options: s.options},
          {kind: "ast"})
  }
  // Truncations: the shape acorn-loose exists for.
  for (const base of ["function f(a, b) { return a + b }", "class A extends B { m() { return 1 } }",
                      "if (a) { b() } else { c() }", "let {x, y} = z", "a.b.c(d, e)",
                      "for (const k of o) { f(k) }", "`t${u}v`", "import {a} from 'm'"]) {
    for (let cut = 1; cut < base.length; cut += Math.max(1, Math.floor(base.length / 6))) {
      c.add("loose_parse",
            {op: "loose_parse", source: base.slice(0, cut), options: {ecmaVersion: 2022, sourceType: "module"}},
            {kind: "truncated"})
    }
  }
}

function familyWalk(c, rng, snippets) {
  const seeds = WALK_SEEDS.map((s) => ({code: s, options: {ecmaVersion: 2022}}))
    .concat(MODULE_WALK_SEEDS.map((s) => ({code: s, options: {ecmaVersion: 2022, sourceType: "module"}})))
    .concat(rng.shuffled(snippets.filter((s) => s.kind === "ast")).slice(0, 150))

  for (const s of seeds) {
    c.add("walk_full", {op: "walk_full", source: s.code, options: s.options})
    c.add("walk_full_ancestor", {op: "walk_full_ancestor", source: s.code, options: s.options})
  }

  const visitorSets = [
    ["Identifier"],
    ["Literal"],
    ["CallExpression", "MemberExpression"],
    ["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"],
    ["ClassDeclaration", "ClassExpression", "PropertyDefinition", "MethodDefinition"],
    ["Program"],
    ["VariableDeclarator", "ObjectPattern", "ArrayPattern"],
    ["ImportDeclaration", "ExportNamedDeclaration", "ExportDefaultDeclaration"],
    ["TemplateLiteral", "TemplateElement"],
    ["Expression"],          // acorn-walk's aggregate visitor names
    ["Statement"],
    ["Pattern"],
    ["ForInStatement", "ForOfStatement", "ForStatement"],
    ["NonexistentNodeType"], // must be a silent no-op, not a throw
  ]
  for (const s of seeds) {
    c.add("walk_simple", {op: "walk_simple", source: s.code, options: s.options,
                          visitors: rng.pick(visitorSets)})
  }
  const stopSets = [
    ["FunctionDeclaration"], ["CallExpression"], ["Program"], ["BlockStatement"],
    ["Identifier"], ["ClassBody"], ["ExpressionStatement"], [],
    ["FunctionDeclaration", "ArrowFunctionExpression", "FunctionExpression"],
  ]
  for (const s of seeds) {
    c.add("walk_recursive", {op: "walk_recursive", source: s.code, options: s.options,
                             stop_at: rng.pick(stopSets)})
  }
}

function familyFindNode(c, rng, snippets) {
  const seeds = WALK_SEEDS.map((s) => ({code: s, options: {ecmaVersion: 2022}}))
    .concat(rng.shuffled(snippets.filter((s) => s.kind === "ast")).slice(0, 120))
  const tests = [null, "Identifier", "Literal", "CallExpression", "Program",
                 "Expression", "Statement", "MemberExpression", "NoSuchType"]
  for (const s of seeds) {
    const L = s.code.length
    // findNodeAt with both bounds, one bound, and neither.
    for (const [start, end] of [[0, null], [null, L], [0, L], [null, null],
                                [rng.int(L + 1), null], [null, rng.int(L + 1)],
                                [L + 5, null], [null, -1]]) {
      c.add("find_node_at", {op: "find_node_at", source: s.code, options: s.options,
                             start, end, test: rng.pick(tests)})
    }
    for (const op of ["find_node_around", "find_node_after", "find_node_before"]) {
      for (const pos of [0, Math.floor(L / 2), L, L + 3]) {
        c.add(op, {op, source: s.code, options: s.options, pos, test: rng.pick(tests)})
      }
    }
  }
}

function familyUtility(c, rng) {
  c.add("utility_static", {op: "version"})
  c.add("utility_static", {op: "default_options"})
  c.add("utility_static", {op: "token_types"})
  c.add("utility_static", {op: "keyword_types"})

  for (const source of LINEINFO_SEEDS) {
    // Every offset in the short ones, a scatter in the long ones, plus the
    // out-of-range offsets on both sides.
    const L = source.length
    const offsets = L <= 24 ? Array.from({length: L + 3}, (_, i) => i - 1)
                            : [-1, 0, 1, Math.floor(L / 3), Math.floor(L / 2), L - 1, L, L + 1, L + 50]
    for (const offset of offsets) {
      c.add("line_info", {op: "get_line_info", source, offset}, {offset})
    }
  }

  for (const code of identifierProbes(rng)) {
    for (const astral of [true, false]) {
      c.add("identifier_tables", {op: "is_identifier_start", code, astral}, {code, astral})
      c.add("identifier_tables", {op: "is_identifier_char", code, astral}, {code, astral})
    }
  }

  for (const code of [-1, 0, 9, 10, 11, 12, 13, 32, 133, 160, 0x2028, 0x2029,
                      0x2000, 0xfeff, 65, 0x10000, 1114112]) {
    c.add("newline_tests", {op: "is_new_line", code}, {code})
  }
  for (const text of ["a", "a\nb", "a\rb", "a\r\nb", "a\u2028b", "a\u2029b", "",
                      "\n", "no breaks", "ab", "ab", "ab"]) {
    c.add("newline_tests", {op: "line_break_test", text})
  }
  for (const text of ["a", " ", "\u00a0", "\u1680", "\u2000", "\u200a", "\u202f",
                      "\u205f", "\u3000", "\ufeff", "\u2028", "\t", "",
                      "a\u00a0b", "\u180e"]) {
    c.add("newline_tests", {op: "nonascii_whitespace_test", text})
  }
}

function familyGenerated(c, rng, count) {
  for (let i = 0; i < count; i++) {
    const module = rng.chance(1, 3)
    // The version is chosen first and handed to the generator, so the source is
    // syntax that version actually has.  Weighted towards the modern end: the
    // old versions get their coverage from parse_versions, which sweeps every
    // upstream snippet across four versions on purpose.
    const ecmaVersion = rng.chance(3, 4)
      ? rng.pick([2015, 2017, 2020, 2021, 2022, 2024, 2025, "latest", 16, 13, 11])
      : rng.pick(ECMA_VERSIONS)
    const gen = genSource(rng, module, ecmaVersion)
    const source = gen.source
    const options = {ecmaVersion}
    if (module) options.sourceType = "module"
    // A hashbang the parser was not told to allow is an error every time, so the
    // option follows the source rather than being sampled beside it.
    if (gen.hashbang) options.allowHashBang = true
    if (rng.chance(1, 4)) options.locations = true
    if (rng.chance(1, 5)) options.ranges = true
    if (rng.chance(1, 8)) options.preserveParens = true
    if (rng.chance(1, 10)) options.allowReturnOutsideFunction = true
    if (rng.chance(1, 10)) options.allowAwaitOutsideFunction = true
    if (rng.chance(1, 12)) options.allowSuperOutsideMethod = true
    if (rng.chance(1, 14)) options.allowImportExportEverywhere = true

    // Every generated source is graded through several operations, because a
    // source that trips `parse` should also trip `tokenize` the same way.
    c.add("generated_parse", {op: "parse", source, options})
    if (rng.chance(1, 2)) c.add("generated_tokenize", {op: "tokenize", source, options})
    if (rng.chance(1, 2)) c.add("generated_loose", {op: "loose_parse", source, options})
    if (rng.chance(1, 3)) c.add("generated_collect", {op: "parse_collect", source, options})
    if (rng.chance(1, 3)) c.add("generated_walk", {op: "walk_full", source, options})
    if (rng.chance(1, 4)) {
      c.add("generated_walk", {op: "walk_full_ancestor", source, options})
    }
    if (rng.chance(1, 4)) {
      c.add("generated_find", {op: "find_node_around", source, options,
                               pos: rng.int(source.length + 2),
                               test: rng.pick([null, "Identifier", "Expression", "Statement"])})
    }
    if (rng.chance(1, 5)) {
      c.add("generated_expr_at", {op: "parse_expression_at", source, options,
                                  pos: rng.int(source.length + 2)})
    }
  }
}

// The six real-world bundles, described rather than inlined.
//
// One ember.js AST is 12.9 MB of JSON, and with locations+ranges roughly triple
// that; the full fixture family would put a few hundred megabytes of
// expectations in the image.  So the manifest names the fixture and the
// operation, both sides build the request from the file on disk, and the
// comparison is over a SHA-256 of the response line.  A digest tells you a
// fixture diverged but not where, so the verifier also records the offset of
// the first differing byte, which it can do in a single pass without holding
// either side in memory.
const FIXTURE_OPS = [
  {op: "parse", options: {ecmaVersion: 2020}},
  {op: "parse", options: {ecmaVersion: 2020, locations: true, ranges: true}},
  {op: "parse", options: {ecmaVersion: 2020, sourceType: "module"}},
  {op: "tokenize", options: {ecmaVersion: 2020}},
  {op: "walk_full", options: {ecmaVersion: 2020}},
  {op: "parse_collect", options: {ecmaVersion: 2020}},
]

function fixtureManifest() {
  const dir = path.join(REPO, "test", "bench", "fixtures")
  if (!fs.existsSync(dir)) return []
  const out = []
  for (const name of fs.readdirSync(dir).sort()) {
    if (!name.endsWith(".js")) continue
    const bytes = fs.statSync(path.join(dir, name)).size
    for (let i = 0; i < FIXTURE_OPS.length; i++) {
      const spec = FIXTURE_OPS[i]
      out.push({fixture: name, bytes, op: spec.op, options: spec.options,
                key: `${name}#${i}`})
    }
  }
  return out
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
function build(opts) {
  const rng = new Rng(opts.seed)
  const c = new Corpus()
  const snippets = harvest()
  const want = (name) => !opts.families || opts.families.includes(name)

  if (want("parse_upstream")) familyParseUpstream(c, rng, snippets)
  if (want("parse_options")) familyParseOptions(c, rng, snippets)
  if (want("parse_versions")) familyParseVersions(c, rng, snippets)
  if (want("parse_sourcetype")) familyParseSourceType(c, rng, snippets)
  if (want("parse_collect")) familyParseCollect(c, rng, snippets)
  if (want("parse_expression_at")) familyParseExpressionAt(c, rng, snippets)
  if (want("tokenize")) familyTokenize(c, rng, snippets)
  if (want("loose_parse")) familyLooseParse(c, rng, snippets)
  if (want("walk")) familyWalk(c, rng, snippets)
  if (want("find_node")) familyFindNode(c, rng, snippets)
  if (want("utility")) familyUtility(c, rng)
  if (want("generated")) familyGenerated(c, rng, opts.generated)

  return c
}

function main() {
  const argv = process.argv.slice(2)
  const opts = {seed: 20260730, out: null, generated: 700, families: null}
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--out") opts.out = argv[++i]
    else if (argv[i] === "--seed") opts.seed = parseInt(argv[++i], 10)
    else if (argv[i] === "--generated") opts.generated = parseInt(argv[++i], 10)
    else if (argv[i] === "--families") opts.families = argv[++i].split(",")
    else { process.stderr.write(`gen_corpus: unknown argument ${argv[i]}\n`); process.exit(2) }
  }
  if (!opts.out) { process.stderr.write("gen_corpus: --out DIR is required\n"); process.exit(2) }
  fs.mkdirSync(opts.out, {recursive: true})

  const c = build(opts)

  // Written with a synchronous descriptor: a createWriteStream here can be
  // abandoned mid-flush when the process exits, which silently produced an
  // empty corpus.
  const fd = fs.openSync(path.join(opts.out, "requests.ndjson"), "w")
  try {
    let chunk = []
    for (const r of c.requests) {
      chunk.push(JSON.stringify(r))
      if (chunk.length >= 512) { fs.writeSync(fd, chunk.join("\n") + "\n"); chunk = [] }
    }
    if (chunk.length) fs.writeSync(fd, chunk.join("\n") + "\n")
  } finally {
    fs.closeSync(fd)
  }

  const byFamily = {}
  for (const cs of c.cases) byFamily[cs.family] = (byFamily[cs.family] || 0) + 1

  const fixtures = fixtureManifest()
  fs.writeFileSync(path.join(opts.out, "cases.json"),
                   JSON.stringify({seed: opts.seed, count: c.size,
                                   families: byFamily, cases: c.cases}, null, 1))
  fs.writeFileSync(path.join(opts.out, "fixtures.json"),
                   JSON.stringify({fixtures}, null, 1))

  const lines = fs.readFileSync(path.join(opts.out, "requests.ndjson"), "utf8")
    .split("\n").filter((l) => l).length
  if (lines !== c.size) {
    process.stderr.write(`gen_corpus: wrote ${lines} lines for ${c.size} requests\n`)
    process.exit(3)
  }

  process.stderr.write(`gen_corpus: ${c.size} requests, ${Object.keys(byFamily).length} families, ${fixtures.length} fixture cases\n`)
  for (const k of Object.keys(byFamily).sort()) {
    process.stderr.write(`  ${k.padEnd(22)} ${byFamily[k]}\n`)
  }
}

module.exports = {Rng, Corpus, harvest, build, genSource, genExpr, genStmt,
                  WALK_SEEDS, MODULE_WALK_SEEDS, LINEINFO_SEEDS, COMMENT_SEEDS,
                  identifierProbes, ECMA_VERSIONS, fixtureManifest, FIXTURE_OPS}

if (require.main === module) main()
