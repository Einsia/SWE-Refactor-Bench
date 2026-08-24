// Serve a built bundle plus the mock Conduit API on a fixed port, and stay up.
//
// Usage: node serve.mjs <distDir> [port] [label]
//
// The port is fixed rather than ephemeral so you can point a browser at it. Every
// request is logged, which is often the fastest way to see that a component is
// fetching something it should not, or fetching it twice.

import path from "node:path";
import { startServer } from "./server.mjs";

async function main() {
  const [distDir, portArg, label] = process.argv.slice(2);
  if (!distDir) {
    console.error("usage: node serve.mjs <distDir> [port] [label]");
    process.exit(2);
  }
  const port = Number(portArg || 8080);
  const resolved = path.resolve(distDir);

  const srv = await startServer({ distDir: resolved, port });
  const name = label || resolved;
  console.log(`${name}`);
  console.log(`  serving ${resolved}`);
  console.log(`  app  ${srv.url}/#/`);
  console.log(`  api  ${srv.url}/api/articles`);
  console.log(`\nrequest log (Ctrl-C to stop):`);

  let seen = 0;
  setInterval(() => {
    while (seen < srv.log.length) {
      const r = srv.log[seen];
      seen += 1;
      const auth = r.auth ? ` auth=${r.auth}` : "";
      console.log(`  ${r.method} ${r.path}${r.search || ""} -> ${r.status}${auth}`);
    }
  }, 200).unref?.();

  const stop = async () => { await srv.close(); process.exit(0); };
  process.on("SIGINT", stop);
  process.on("SIGTERM", stop);
  // Hold the process open.
  await new Promise(() => {});
}

main().catch((err) => { console.error(err); process.exit(1); });
