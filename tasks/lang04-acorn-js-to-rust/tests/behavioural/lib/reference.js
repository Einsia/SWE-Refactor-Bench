#!/usr/bin/env node
/*
 * The reference side of the differential test: acorn 8.14.0, in JavaScript,
 * speaking exactly the NDJSON protocol instruction.md specifies for
 * `acorn-probe`.
 *
 * This file is the only authority for what a response should contain.  The
 * expectations frozen into the verifier image are this program's output, so a
 * disagreement between the spec prose and this program is resolved in this
 * program's favour -- which is the same rule the instruction states: behaviour
 * is defined against the implementation, not against the description.
 *
 * Three modes, and all three run at image build time -- nothing here is invoked
 * while a submission is being graded, except the last one under `--identity`:
 *   --batch REQ OUT        read a request file, write a response file.  This is
 *                          what freezes the corpus's expected answers.
 *   --fixtures MAN OUT DIR digest the responses for the six real-world bundles,
 *                          whose full output is 320 MB and is compared by block
 *                          digest rather than stored.
 *   (default)              act as the probe on stdin/stdout, so the reference can
 *                          be graded against itself as an identity check.
 */
"use strict"

const fs = require("fs")
const path = require("path")
const crypto = require("crypto")

const REPO = process.env.ACORN_REFERENCE_REPO || "/opt/swerefactor/reference"
const acorn = require(path.join(REPO, "acorn"))
const looseMod = require(path.join(REPO, "acorn-loose"))
const walk = require(path.join(REPO, "acorn-walk"))

// ---------------------------------------------------------------------------
// Canonical encoding
// ---------------------------------------------------------------------------
// JSON.stringify already gives us insertion-ordered keys, ECMAScript number
// formatting, omission of undefined and function values, and minimal string
// escaping.  The single deviation the protocol defines is BigInt, which
// JSON.stringify throws on.
function replacer(key, value) {
  return typeof value === "bigint" ? {$bigint: value.toString()} : value
}

function encode(response) {
  return JSON.stringify(response, replacer)
}

// ---------------------------------------------------------------------------
// Option handling
// ---------------------------------------------------------------------------
// acorn mutates the options object it is given (it fills in defaults, and the
// CLI relies on `options.program`).  Every request gets its own copy so cases
// cannot leak into each other.
function opts(req) {
  return req.options ? JSON.parse(JSON.stringify(req.options)) : {}
}

// `console.warn` is how acorn reports a missing ecmaVersion.  During a batch run
// that would pollute stderr with thousands of lines, so it is captured and
// discarded here; the CLI family is where that warning is graded, on the real
// binary's real stderr.
const realWarn = console.warn
function quietly(fn) {
  console.warn = () => {}
  try { return fn() } finally { console.warn = realWarn }
}

function nodeTypeTest(name) {
  return name === null || name === undefined ? undefined : name
}

// ---------------------------------------------------------------------------
// Operations
// ---------------------------------------------------------------------------
const ops = {
  version: () => acorn.version,

  default_options: () => acorn.defaultOptions,

  token_types: () => {
    const out = {}
    for (const key of Object.keys(acorn.tokTypes)) out[key] = acorn.tokTypes[key]
    return out
  },

  keyword_types: () => {
    const out = {}
    for (const key of Object.keys(acorn.keywordTypes)) out[key] = acorn.keywordTypes[key]
    return out
  },

  parse: (req) => acorn.parse(req.source, opts(req)),

  parse_collect: (req) => {
    const o = opts(req)
    const comments = [], tokens = []
    o.onComment = comments
    o.onToken = tokens
    const ast = acorn.parse(req.source, o)
    return {ast, comments, tokens}
  },

  parse_expression_at: (req) => acorn.parseExpressionAt(req.source, req.pos, opts(req)),

  tokenize: (req) => {
    const tokenizer = acorn.tokenizer(req.source, opts(req))
    const out = []
    let token
    do {
      token = tokenizer.getToken()
      out.push(token)
    } while (token.type !== acorn.tokTypes.eof)
    return out
  },

  loose_parse: (req) => looseMod.parse(req.source, opts(req)),

  walk_full: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    const out = []
    walk.full(ast, (node) => out.push([node.type, node.start, node.end]))
    return out
  },

  walk_full_ancestor: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    const out = []
    walk.fullAncestor(ast, (node, state, ancestors) =>
      out.push([node.type, node.start, node.end, ancestors.map((a) => a.type)]))
    return out
  },

  walk_simple: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    const out = []
    const visitors = {}
    for (const name of req.visitors || []) {
      visitors[name] = (node) => out.push([node.type, node.start, node.end])
    }
    walk.simple(ast, visitors)
    return out
  },

  walk_recursive: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    const out = []
    const visitors = {}
    // A visitor that records and does not call `c` stops the descent there.
    // Everything without a visitor falls through to `base`.
    for (const name of req.stop_at || []) {
      visitors[name] = (node) => out.push([node.type, node.start, node.end])
    }
    walk.recursive(ast, undefined, visitors)
    return out
  },

  find_node_at: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    return walk.findNodeAt(ast, req.start === null ? undefined : req.start,
                           req.end === null ? undefined : req.end,
                           nodeTypeTest(req.test))
  },

  find_node_around: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    return walk.findNodeAround(ast, req.pos, nodeTypeTest(req.test))
  },

  find_node_after: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    return walk.findNodeAfter(ast, req.pos, nodeTypeTest(req.test))
  },

  find_node_before: (req) => {
    const ast = acorn.parse(req.source, opts(req))
    return walk.findNodeBefore(ast, req.pos, nodeTypeTest(req.test))
  },

  get_line_info: (req) => acorn.getLineInfo(req.source, req.offset),

  is_identifier_start: (req) => acorn.isIdentifierStart(req.code, req.astral),

  is_identifier_char: (req) => acorn.isIdentifierChar(req.code, req.astral),

  is_new_line: (req) => acorn.isNewLine(req.code),

  line_break_test: (req) => acorn.lineBreak.test(req.text),

  nonascii_whitespace_test: (req) => acorn.nonASCIIwhitespace.test(req.text),
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------
function errorPayload(e) {
  const kind = (e && e.constructor && e.constructor.name) || "Error"
  const out = {kind, message: String(e && e.message)}
  if (e instanceof SyntaxError && typeof e.pos === "number") {
    out.pos = e.pos
    out.raisedAt = e.raisedAt
    out.loc = e.loc
  }
  return out
}

function handle(req) {
  const response = {id: req.id, ok: true}
  const op = ops[req.op]
  if (!op) {
    response.ok = false
    response.error = {kind: "ProtocolError", message: "unknown op: " + req.op}
    return response
  }
  let result
  try {
    result = quietly(() => op(req))
  } catch (e) {
    response.ok = false
    response.error = errorPayload(e)
    return response
  }
  // `undefined` is a real answer for find_node_*: JSON omits the key, and the
  // response is {"id":N,"ok":true}.  Assigning it keeps that behaviour without
  // a special case.
  response.result = result
  return response
}

// Written with the synchronous fd API rather than a WriteStream: at process
// exit a stream's pending buffer is simply abandoned, which silently produces a
// short (or empty) expectation file.  A truncated expectation file is worse than
// a crash, because every case past the truncation point grades as a mismatch.
function batch(reqPath, outPath) {
  const lines = fs.readFileSync(reqPath, "utf8").split("\n")
  const fd = fs.openSync(outPath, "w")
  let count = 0
  try {
    let chunk = [], chunkBytes = 0
    for (const line of lines) {
      if (!line) continue
      let req
      try {
        req = JSON.parse(line)
      } catch (e) {
        chunk.push(encode({id: null, ok: false,
                           error: {kind: "ProtocolError", message: "bad request line"}}))
        continue
      }
      let text
      try {
        text = encode(handle(req))
      } catch (e) {
        // Encoding itself failed -- a value the protocol cannot represent.  That
        // is a defect in this driver, not in a submission, so it must be loud.
        realWarn(`reference: cannot encode response for id=${req.id} op=${req.op}: ${e.message}`)
        process.exit(3)
      }
      chunk.push(text)
      chunkBytes += text.length
      count++
      // Flush by size, not by line count: one response can be megabytes.
      if (chunkBytes >= (1 << 22)) {
        fs.writeSync(fd, chunk.join("\n") + "\n")
        chunk = []
        chunkBytes = 0
      }
    }
    if (chunk.length) fs.writeSync(fd, chunk.join("\n") + "\n")
  } finally {
    fs.closeSync(fd)
  }
  const written = fs.readFileSync(outPath, "utf8").split("\n").filter((l) => l).length
  if (written !== count) {
    realWarn(`reference: answered ${count} requests but wrote ${written} lines`)
    process.exit(3)
  }
  process.stderr.write(`reference: answered ${count} requests\n`)
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------
// The six real-world bundles are graded by digest: ember.js parsed with
// locations and ranges is over 30 MB of JSON, and six bundles times six
// operations of that does not belong in a container image.  Storing only the
// whole-response digest would make a failure unactionable, though -- "your
// 13 MB answer is wrong somewhere" is not a bug report.  So each entry also
// carries a digest per fixed-size block, which localises the first divergence
// to one block without keeping any of the bytes.  Sixteen hex characters is
// ample for that: these are being compared against a specific expected value,
// not defended against a forger.
const BLOCK = 32768
function blocksFor(buf) {
  const out = []
  for (let at = 0; at < buf.length; at += BLOCK) {
    out.push(crypto.createHash("sha256").update(buf.subarray(at, at + BLOCK))
             .digest("hex").slice(0, 16))
  }
  return out
}

function fixtures(manifestPath, outPath, fixtureDir) {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")).fixtures || []
  const digests = {}
  for (const entry of manifest) {
    const source = fs.readFileSync(path.join(fixtureDir, entry.fixture), "utf8")
    const req = {id: 1, op: entry.op, source, options: entry.options}
    const started = Date.now()
    let text
    try {
      text = encode(handle(req))
    } catch (e) {
      realWarn(`reference: cannot encode fixture ${entry.key}: ${e.message}`)
      process.exit(3)
    }
    // The probe's response is one line, so a line's worth of bytes is what is
    // hashed -- the terminating newline is the verifier's business, and it
    // compares the line without it.  Hashing goes through a Buffer so the
    // offsets reported to a submission are byte offsets: a block boundary in
    // the middle of a multi-byte character would otherwise be counted
    // differently on the two sides.
    const buf = Buffer.from(text, "utf8")
    digests[entry.key] = {
      bytes: buf.length,
      sha256: crypto.createHash("sha256").update(buf).digest("hex"),
      block: BLOCK,
      blocks: blocksFor(buf),
      reference_ms: Date.now() - started,
    }
    process.stderr.write(
      `reference: ${entry.key} ${digests[entry.key].bytes} bytes ` +
      `in ${digests[entry.key].reference_ms}ms\n`)
  }
  fs.writeFileSync(outPath, JSON.stringify({digests}, null, 1))
}

function serve() {
  let buffer = ""
  process.stdin.setEncoding("utf8")
  process.stdin.on("data", (chunk) => {
    buffer += chunk
    let nl
    while ((nl = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, nl)
      buffer = buffer.slice(nl + 1)
      if (!line) continue
      let req
      try {
        req = JSON.parse(line)
      } catch (e) {
        process.stdout.write(encode({id: null, ok: false,
          error: {kind: "ProtocolError", message: "bad request line"}}) + "\n")
        continue
      }
      process.stdout.write(encode(handle(req)) + "\n")
    }
  })
  process.stdin.on("end", () => process.exit(0))
}

const argv = process.argv.slice(2)
if (argv[0] === "--batch") batch(argv[1], argv[2])
else if (argv[0] === "--fixtures") fixtures(argv[1], argv[2], argv[3])
else serve()
