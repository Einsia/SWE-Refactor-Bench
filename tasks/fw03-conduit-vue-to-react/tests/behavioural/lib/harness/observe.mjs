// Entry point: run every scenario against a built bundle and write the result
// as one JSON file.
//
//   node observe.mjs <distDir> <outFile> [--nonce X] [--filter substring]
//
// Called twice per grading run with the same --nonce: once against the State A
// bundle unpacked from data/oracle-dist.tgz, once against the bundle the
// submission just built. That symmetry is the whole point. There is no second
// code path, no recorded expectation and no branch that could let the two states
// be measured differently -- the reference is not a file minted months ago, it is
// what the State A bundle does on this machine, this run, against this data.
//
// The nonce is why both sides are driven live rather than one being frozen.
// startServer() stamps it through every title, description, body, bio, tag and
// comment the API serves, so the strings the app has to render did not exist
// when the submission was written. A port that hard-codes what the home feed
// says cannot pass here, and cannot pass by luck either: it has no way to learn
// this run's nonce.

import fs from "node:fs";
import { chromium } from "playwright";
import { startServer } from "./server.mjs";
import { SCENARIOS } from "./scenarios.mjs";
import { runScenario } from "./drive.mjs";

const argv = process.argv.slice(2);
const positional = [];
let nonce = "";
let filter = "";
for (let i = 0; i < argv.length; i += 1) {
  if (argv[i] === "--nonce") { nonce = argv[i + 1] || ""; i += 1; continue; }
  if (argv[i] === "--filter") { filter = argv[i + 1] || ""; i += 1; continue; }
  positional.push(argv[i]);
}
const [distDir, outFile] = positional;
if (!distDir || !outFile) {
  console.error("usage: node observe.mjs <distDir> <outFile> [--nonce X] [--filter S]");
  process.exit(2);
}
// The nonce reaches a tag name, which reaches a URL the app navigates to.
// Anything needing escaping would make the two sides differ for a reason that is
// not the submission's.
if (nonce && !/^[a-z0-9]{1,32}$/.test(nonce)) {
  console.error(`observe: --nonce must be lowercase alphanumeric, got "${nonce}"`);
  process.exit(2);
}

const wanted = filter ? SCENARIOS.filter((s) => s.id.includes(filter)) : SCENARIOS;
if (wanted.length === 0) {
  console.error(`observe: --filter "${filter}" matched no scenario`);
  process.exit(2);
}

const srv = await startServer({ distDir, nonce: nonce || undefined });
const browser = await chromium.launch({ args: ["--no-sandbox", "--disable-dev-shm-usage"] });

const results = [];
let failures = 0;
for (const [i, scenario] of wanted.entries()) {
  let out;
  try {
    out = await runScenario(browser, srv, scenario);
  } catch (err) {
    out = {
      id: scenario.id, group: scenario.group, token: scenario.token || null,
      stepErrors: [`driver: ${String(err && err.message)}`],
      pageErrors: [], requests: [], observed: null,
    };
  }
  results.push(out);
  const bad = out.stepErrors.length > 0 || !out.observed;
  if (bad) failures += 1;
  const nodes = out.observed ? out.observed.nodeCount : 0;
  process.stderr.write(
    `[${String(i + 1).padStart(3)}/${wanted.length}] ${out.id} ` +
    `nodes=${nodes} req=${out.requests.length}` +
    `${bad ? ` FAILED: ${out.stepErrors[0] || "no snapshot"}` : ""}\n`,
  );
}

await browser.close();
await srv.close();

fs.writeFileSync(
  outFile,
  `${JSON.stringify({ nonce: nonce || null, dist: distDir, scenarios: results }, null, 1)}\n`,
);
process.stderr.write(
  `\nwrote ${outFile}: ${results.length} scenarios, ${failures} with driver problems\n`,
);
