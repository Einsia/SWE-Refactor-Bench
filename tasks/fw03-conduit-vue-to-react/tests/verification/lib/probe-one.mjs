// Drive one ad-hoc scenario against one bundle and write the observation.
//
//   node probe-one.mjs <distDir> <scenarioFile> <outFile>
//
// The scenario file is JSON in exactly the shape stage 2's corpus uses:
//
//   {"id": "...", "token": "alice" | null, "steps": [...], "probes": ["..."]}
//
// This is the only way a stage-3 candidate reaches the artifact under test, and
// it deliberately offers nothing stage 2 does not already do. It imports the same
// server, the same fixtures, the same DOM reduction and the same runScenario --
// there is no second definition of "settled", no second normaliser and no second
// notion of what a request log entry is. A candidate that finds a divergence here
// has found one stage 2's driver would also have seen, had stage 2 thought to
// visit that screen in that order.
//
// The one addition is `probes`: stage 2 asks 35 fixed selectors, and a candidate
// may ask its own instead. That is the point of the stage -- the corpus is a
// sample of the observable surface, and an adversary's job is to find the part of
// it nobody sampled.
//
// What this does NOT print, anywhere, is which bundle it was given. The path
// arrives as argv from run-candidate.sh and is not echoed into the observation.

import fs from "node:fs";
import { chromium } from "playwright";
import { startServer } from "./harness/server.mjs";
import { runScenario } from "./harness/drive.mjs";
import { PROBE_SELECTORS } from "./harness/normalize.mjs";

const [distDir, scenarioFile, outFile] = process.argv.slice(2);
if (!distDir || !scenarioFile || !outFile) {
  console.error("usage: node probe-one.mjs <distDir> <scenarioFile> <outFile>");
  process.exit(2);
}

let scenario;
try {
  scenario = JSON.parse(fs.readFileSync(scenarioFile, "utf8"));
} catch (err) {
  console.error(`probe-one: cannot read the scenario: ${String(err && err.message)}`);
  process.exit(2);
}
if (!Array.isArray(scenario.steps) || scenario.steps.length === 0) {
  console.error("probe-one: the scenario declares no steps");
  process.exit(2);
}
// A candidate that spends 400 steps is not describing a user's behaviour, and one
// scenario is not allowed to consume the round's whole clock.
if (scenario.steps.length > 60) {
  console.error(`probe-one: ${scenario.steps.length} steps is more than 60`);
  process.exit(2);
}

// A candidate's own selectors are appended to the standard 35 rather than
// replacing them, so an observation always carries the baseline surface too --
// which is what makes a candidate's own probe interpretable next to it.
const extra = Array.isArray(scenario.probes) ? scenario.probes.map(String) : [];
if (extra.length > 40) {
  console.error(`probe-one: ${extra.length} extra probes is more than 40`);
  process.exit(2);
}
const probes = [...new Set([...PROBE_SELECTORS, ...extra])];

// The nonce is stage 2's device, not this stage's: here the two sides are driven
// in separate processes and compared by a test the adversary wrote, so the data
// has to be the same on both runs rather than merely unpredictable. Unstamped
// fixtures are identical every time, which is what `require_deterministic` needs.
const srv = await startServer({ distDir });
const browser = await chromium.launch({
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

let result;
try {
  result = await runScenario(browser, srv, {
    id: String(scenario.id || "candidate"),
    group: "candidate",
    token: scenario.token || null,
    steps: scenario.steps,
  }, probes);
} catch (err) {
  result = {
    id: String(scenario.id || "candidate"),
    group: "candidate",
    token: scenario.token || null,
    stepErrors: [`driver: ${String(err && err.message)}`],
    pageErrors: [],
    requests: [],
    observed: null,
  };
}

await browser.close();
await srv.close();

fs.writeFileSync(outFile, `${JSON.stringify(result, null, 1)}\n`);
process.stderr.write(
  `probe-one: ${result.id} nodes=${result.observed ? result.observed.nodeCount : 0} ` +
  `req=${result.requests.length} stepErrors=${result.stepErrors.length}\n`,
);
