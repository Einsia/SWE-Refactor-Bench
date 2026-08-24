// The observation server: serves a built SPA bundle plus a deterministic
// Conduit API on one origin, and records every API request it answers.
//
// One origin means no CORS and no proxy config, so the bundle under test only
// has to be a correct static build. The recorded request log is half the
// behavioural contract: it captures exactly which endpoints the app calls, in
// what order, with what payloads and what Authorization header.

import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { makeDb, stampNonce } from "./fixtures.mjs";
import { handle } from "./api-routes.mjs";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".txt": "text/plain; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
};

// A 1x1 transparent PNG, so avatar <img> tags resolve without network access.
const PIXEL = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk" +
  "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

function readBody(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
  });
}

function send(res, status, type, body) {
  res.writeHead(status, {
    "content-type": type,
    "content-length": Buffer.byteLength(body),
    // No caching anywhere: every page load must re-fetch, so the request log is
    // complete and identical between the two states.
    "cache-control": "no-store, no-cache, must-revalidate",
  });
  res.end(body);
}

/**
 * @param {{distDir: string, nonce?: string, port?: number}} opts
 * @returns {Promise<{url: string, log: object[], reset: () => void,
 *                    db: object, close: () => Promise<void>}>}
 */
export async function startServer(opts) {
  const distDir = path.resolve(opts.distDir);
  // With a nonce, every fixture string the app renders is prefixed with a
  // value that did not exist when the submission was written. Default runs
  // pass no nonce, so behavioural capture sees the fixtures unchanged.
  const build = () => (opts.nonce ? stampNonce(makeDb(), opts.nonce) : makeDb());
  const state = { db: build(), log: [] };

  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    const rawPath = url.pathname;

    if (rawPath === "/static" || rawPath.startsWith("/static/")) {
      send(res, 200, "image/png", PIXEL);
      return;
    }

    if (rawPath === "/api" || rawPath.startsWith("/api/")) {
      const raw = await readBody(req);
      let parsed = null;
      if (raw) {
        try { parsed = JSON.parse(raw); } catch { parsed = { __unparsed: raw }; }
      }
      // Empty segments are dropped, which is what makes `/api/tags/` and
      // `/api/user/` route the same as their slashless forms.
      const segments = rawPath.slice(5).split("/").filter((s) => s !== "");
      const query = Object.fromEntries(url.searchParams.entries());
      const headers = { authorization: req.headers.authorization || "" };
      let out;
      try {
        out = handle(state.db, req.method, segments, query, parsed, headers);
      } catch (err) {
        out = { status: 500, body: { errors: { server: [String(err && err.message)] } } };
      }
      state.log.push({
        method: req.method,
        path: rawPath,
        search: url.search,
        query,
        body: parsed,
        auth: req.headers.authorization || null,
        status: out.status,
      });
      send(res, out.status, TYPES[".json"], JSON.stringify(out.body));
      return;
    }

    // Static asset, then history-mode fallback. A path whose final segment
    // contains a dot is treated as an asset request and is allowed to 404, so a
    // build with a broken script reference fails loudly instead of silently
    // being handed index.html.
    const rel = decodeURIComponent(rawPath).replace(/^\/+/, "");
    const candidate = path.resolve(distDir, rel);
    const inside = candidate === distDir || candidate.startsWith(distDir + path.sep);
    if (inside && rel !== "" && fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      const type = TYPES[path.extname(candidate).toLowerCase()] || "application/octet-stream";
      send(res, 200, type, fs.readFileSync(candidate));
      return;
    }
    const last = rel.split("/").pop() || "";
    if (last.includes(".")) {
      send(res, 404, TYPES[".txt"], `not found: ${rawPath}\n`);
      return;
    }
    const index = path.join(distDir, "index.html");
    if (!fs.existsSync(index)) {
      send(res, 500, TYPES[".txt"], "no index.html in dist\n");
      return;
    }
    send(res, 200, TYPES[".html"], fs.readFileSync(index));
  });

  // Port 0 by default: capture runs many servers and must never collide with
  // each other or with anything the agent left listening. A caller that wants a
  // browsable URL asks for a fixed port instead.
  const requested = Number(opts.port) || 0;
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(requested, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  const { port } = server.address();

  return {
    url: `http://127.0.0.1:${port}`,
    port,
    get log() { return state.log; },
    get db() { return state.db; },
    reset() { state.db = build(); state.log = []; },
    close() { return new Promise((r) => server.close(r)); },
  };
}
