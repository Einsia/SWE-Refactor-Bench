// Where did the build put its output?
//
//   node find-dist.mjs /workspace/repo   ->  /workspace/repo/dist   (or nothing)
//
// Used by swerefactor-serve and swerefactor-diff so neither has to be told, and so
// neither cares what the new build tool calls its output directory. `dist` is
// Vite's default and vue-cli-service's too, but a submission that configures
// `build.outDir` is not wrong, and a helper that only understood `dist` would
// make it look as though nothing had been built.
//
// The conventional names are tried in order first; the fallback is any directory
// one level down that holds an index.html. node_modules is excluded, because a
// dependency shipping its own demo page is not this project's build output.
//
// Exits 0 having printed the absolute path, or 1 having printed nothing. The
// grader locates the output the same way, from its own copy of this logic, so a
// layout that works here is a layout that will be found when it counts.

import fs from "node:fs";
import path from "node:path";

const CONVENTIONAL = ["dist", "build", "out", ".output/public", "public/build"];

function isFile(p) {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

export function findDist(repo) {
  const root = path.resolve(repo);
  for (const name of CONVENTIONAL) {
    const candidate = path.join(root, name);
    if (isFile(path.join(candidate, "index.html"))) return candidate;
  }
  let entries;
  try {
    entries = fs.readdirSync(root, { withFileTypes: true });
  } catch {
    return null;
  }
  for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
    if (!entry.isDirectory() || entry.name === "node_modules") continue;
    const candidate = path.join(root, entry.name);
    if (isFile(path.join(candidate, "index.html"))) return candidate;
  }
  return null;
}

// Only when run directly, so the function above stays importable.
if (import.meta.url === `file://${process.argv[1]}`) {
  const found = findDist(process.argv[2] || "/workspace/repo");
  if (!found) process.exit(1);
  process.stdout.write(`${found}\n`);
}
