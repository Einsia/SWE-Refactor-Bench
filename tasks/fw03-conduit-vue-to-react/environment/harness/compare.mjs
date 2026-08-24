// Compare a candidate bundle against the State A oracle on paths you choose.
//
// This is the same server, the same settling rule, the same DOM reduction and the
// same off-origin sealing the grader uses, driven through the same runScenario().
// The only thing you supply is which paths to look at -- which is the part that is
// yours to work out.
//
// Usage:
//   node compare.mjs <oracleDist> <candidateDist> <path> [path...]
//   node compare.mjs <oracleDist> <candidateDist> --paths-from <file>
//
// Paths are app paths including the leading hash, e.g. '#/' or '#/@alice'. A path
// may carry a session by prefixing it with 'alice:' / 'bob:' / 'carol:', e.g.
// 'alice:#/settings'.
//
// Exit status is 0 when every path matched, 1 when any differed.

import fs from "node:fs";
import path from "node:path";
import { chromium } from "playwright";
import { startServer } from "./server.mjs";
import { runScenario } from "./drive.mjs";

function parseTarget(raw) {
  const m = /^(alice|bob|carol):(.*)$/.exec(raw);
  const token = m ? m[1] : null;
  const hash = m ? m[2] : raw;
  return {
    id: raw,
    group: "compare",
    token,
    steps: [{ goto: hash.startsWith("#") ? hash : `#${hash}` }],
  };
}

/** Render one snapshot node as a single comparable line. */
function nodeLine(n) {
  if (n.kind === "text") return `${n.path}\t#text ${JSON.stringify(n.text)}`;
  const attrs = Object.entries(n.attrs || {})
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
    .join(" ");
  // Classes are compared as a set, so they are already sorted in the snapshot.
  const classes = (n.classes || []).length ? ` class=[${n.classes.join(" ")}]` : "";
  return `${n.path}\t<${n.tag}>${classes}${attrs ? ` ${attrs}` : ""}`;
}

/** Flatten a snapshot's node list into comparable lines. */
function lines(observed) {
  if (!observed || !Array.isArray(observed.nodes)) return ["<no snapshot captured>"];
  return observed.nodes.map(nodeLine);
}

/** A compact first-difference report, capped so it stays readable. */
function report(name, want, got, limit = 25) {
  const out = [];
  const max = Math.max(want.length, got.length);
  let shown = 0;
  for (let i = 0; i < max && shown < limit; i += 1) {
    if (want[i] !== got[i]) {
      out.push(`  @${i}`);
      out.push(`    oracle:    ${want[i] === undefined ? "<missing>" : want[i]}`);
      out.push(`    candidate: ${got[i] === undefined ? "<missing>" : got[i]}`);
      shown += 1;
    }
  }
  if (shown === limit) out.push(`  ... more differences suppressed`);
  return out;
}

function requestLines(requests) {
  return requests.map((r) => {
    const auth = r.auth ? ` auth=${r.auth}` : "";
    const body = r.body === null || r.body === undefined ? "" : ` body=${JSON.stringify(r.body)}`;
    return `${r.method} ${r.path}${r.search || ""} -> ${r.status}${auth}${body}`;
  });
}

async function capture(distDir, targets) {
  const srv = await startServer({ distDir: path.resolve(distDir) });
  const browser = await chromium.launch({ args: ["--no-sandbox"] });
  const out = new Map();
  try {
    for (const target of targets) {
      out.set(target.id, await runScenario(browser, srv, target));
    }
  } finally {
    await browser.close();
    await srv.close();
  }
  return out;
}

async function main() {
  const argv = process.argv.slice(2);
  const [oracleDist, candidateDist, ...rest] = argv;
  if (!oracleDist || !candidateDist || rest.length === 0) {
    console.error("usage: node compare.mjs <oracleDist> <candidateDist> <path> [path...]");
    console.error("       node compare.mjs <oracleDist> <candidateDist> --paths-from <file>");
    process.exit(2);
  }

  let raw = rest;
  if (rest[0] === "--paths-from") {
    if (!rest[1]) { console.error("--paths-from needs a file"); process.exit(2); }
    // '#' cannot introduce a comment here: every app path starts with one.
    raw = fs.readFileSync(rest[1], "utf8")
      .split("\n")
      .map((s) => s.trim())
      .filter((s) => s.length > 0 && !s.startsWith("//"));
  }

  const targets = raw.map(parseTarget);
  console.log(`comparing ${targets.length} path(s)\n`);

  const oracle = await capture(oracleDist, targets);
  const candidate = await capture(candidateDist, targets);

  let bad = 0;
  for (const target of targets) {
    const a = oracle.get(target.id);
    const b = candidate.get(target.id);
    const problems = [];

    if (b.stepErrors.length) {
      problems.push(`  candidate could not be driven: ${b.stepErrors[0]}`);
    }
    const domA = lines(a.observed);
    const domB = lines(b.observed);
    if (domA.join("\n") !== domB.join("\n")) {
      problems.push(`  DOM differs (oracle ${domA.length} nodes, candidate ${domB.length}):`);
      problems.push(...report(target.id, domA, domB));
    }
    const reqA = requestLines(a.requests);
    const reqB = requestLines(b.requests);
    if (reqA.join("\n") !== reqB.join("\n")) {
      problems.push(`  requests differ (oracle ${reqA.length}, candidate ${reqB.length}):`);
      problems.push(...report(target.id, reqA, reqB));
    }

    if (problems.length) {
      bad += 1;
      console.log(`DIFF  ${target.id}`);
      console.log(problems.join("\n"));
      console.log("");
    } else {
      console.log(`same  ${target.id}  (${domA.length} nodes, ${reqA.length} requests)`);
    }
  }

  console.log(`\n${targets.length - bad}/${targets.length} path(s) matched the oracle`);
  process.exit(bad === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(err);
  process.exit(2);
});
