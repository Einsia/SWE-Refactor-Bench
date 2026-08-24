/*
 * Drives the submission's src/node adapter the way an existing Node user would.
 *
 *   node --experimental-vm-modules nodeapi.mjs <job.json> <out.json>
 *
 * The point of this runner is the synchronous path.  State A's `render()`
 * returns a String; §1.5 requires the adapter to keep doing that on top of a
 * core that is fundamentally async.  Here that promise is either kept or it is
 * not -- a returned Promise is reported as such rather than silently awaited.
 */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const outPath = process.argv[3];

/*
 * A Stylus error names the file it was compiling -- in `.message`, in `.filename`
 * and once per frame in `.stylusStack` -- and this driver hands the adapter real
 * absolute paths.  They are mapped back for the same reason the oracle maps its
 * own: otherwise every failure comparison fails on where the verifier unpacked
 * the tree, and the submission gets blamed for the harness.
 *
 * Declared below `revirt`/`revirtInText`, which are function declarations and so
 * hoist; nothing is shaped before the job starts running.
 */
function errShape(e) {
  if (e === null || typeof e !== 'object') return { message: revirtInText(String(e)), name: 'Thrown' };
  return {
    name: typeof e.name === 'string' ? e.name : undefined,
    message: typeof e.message === 'string' ? revirtInText(e.message) : String(e),
    lineno: typeof e.lineno === 'number' ? e.lineno : undefined,
    column: typeof e.column === 'number' ? e.column : undefined,
    filename: typeof e.filename === 'string' ? revirt(e.filename) : undefined,
    stylusStack: typeof e.stylusStack === 'string' ? revirtInText(e.stylusStack) : undefined,
  };
}

const mounts = job.mounts || {};
function devirt(p) {
  if (typeof p !== 'string') return p;
  for (const [virt, real] of Object.entries(mounts)) {
    if (p === virt) return path.resolve(real);
    const pref = virt.endsWith('/') ? virt : `${virt}/`;
    if (p.startsWith(pref)) return path.resolve(real, p.slice(pref.length));
  }
  return p;
}
function devirtDeep(v) {
  if (typeof v === 'string') return devirt(v);
  if (Array.isArray(v)) return v.map(devirtDeep);
  if (v && typeof v === 'object') {
    const out = {};
    for (const [k, val] of Object.entries(v)) out[k] = devirtDeep(val);
    return out;
  }
  return v;
}
function revirt(p) {
  if (typeof p !== 'string') return p;
  for (const [virt, real] of Object.entries(mounts)) {
    const abs = path.resolve(real);
    if (p === abs) return virt;
    if (p.startsWith(`${abs}/`)) return `${virt.replace(/\/$/, '')}/${p.slice(abs.length + 1)}`;
  }
  return p;
}

/*
 * `linenos` writes the source path into the CSS, and this driver hands the
 * adapter a real absolute path.  Map it back, the way oracle.cjs does, or the
 * comparison fails on the verifier's unpack location rather than on the port.
 * The submission's own tree is collapsed too: instruction.md 1.4 does not pin
 * where under `src/core/` the built-in `.styl` library lives, so its path is not
 * something a row may require.
 */
function revirtInText(text) {
  if (typeof text !== 'string') return text;
  let out = text;
  const roots = Object.entries(mounts).map(([v, r]) => [path.resolve(r), v.replace(/\/$/, '')]);
  roots.sort((a, b) => b[0].length - a[0].length);
  for (const [abs, v] of roots) {
    out = out.split(abs).join(v);
    out = out.split(abs.replace(/[/:]/g, (c) => `\\${c}`)).join(v.replace(/[/:]/g, (c) => `\\${c}`));
  }
  const repo = path.resolve(job.repoRoot || '.');
  if (repo && repo !== '/') out = out.split(repo).join('<repo>');
  return out;
}

/*
 * A plugin function, compiled from the same source the oracle compiles.  There
 * is no sandbox here -- this driver imports the submission as an ordinary module
 * -- so `new Function` costs nothing that importing the adapter did not already.
 */
function compileFn(name, src) {
  try {
    // eslint-disable-next-line no-new-func
    return new Function(`return (${src});`)();
  } catch (e) {
    throw new Error(`defineFn ${name} did not compile: ${e && e.message}`);
  }
}

/*
 * The mirror of oracle.cjs's `sourceOf`.  An op may name a fixture instead of
 * carrying its text, which several sourcemap cases need: `sourcesContent` is
 * filled by reading `node.filename` off disk, so those cases only mean something
 * when the file they name is the file they compiled.
 */
function sourceOf(op) {
  if (typeof op.sourceFile === 'string') return fs.readFileSync(devirt(op.sourceFile), 'utf8');
  return op.source;
}

function buildRenderer(api, op) {
  const options = devirtDeep(op.options || {});
  const factory = typeof api === 'function' ? api : api.stylus;
  if (typeof factory !== 'function') throw new Error('src/node default export is not callable and has no .stylus');
  const renderer = factory(sourceOf(op), options);
  for (const [k, v] of Object.entries(op.set || {})) {
    renderer.set(k, k === 'filename' || k === 'paths' ? devirtDeep(v) : v);
  }
  for (const p of op.include || []) renderer.include(devirt(p));
  for (const [name, value] of Object.entries(op.define || {})) renderer.define(name, value);
  for (const [name, value] of Object.entries(op.defineRaw || {})) renderer.define(name, value, true);
  for (const [name, src] of Object.entries(op.defineFn || {})) renderer.define(name, compileFn(name, src));
  for (const [name, src] of Object.entries(op.defineFnRaw || {})) {
    renderer.define(name, compileFn(name, src), true);
  }
  if (op.defineResolver) renderer.define('url', api.resolver(devirtDeep(op.resolverOptions || {})));
  if (op.defineUrl) renderer.define('url', api.url(devirtDeep(op.urlOptions || {})));
  return renderer;
}

async function runOp(api, op) {
  switch (op.kind) {
    case 'render': {
      const renderer = buildRenderer(api, op);
      const out = renderer.render();
      if (out && typeof out.then === 'function') {
        // Reported, not rescued: §1.5 says this call returns a String.
        const css = String(await out);
        return { css: op.revirtCss ? revirtInText(css) : css, wasPromise: true };
      }
      const css = String(out);
      return { css: op.revirtCss ? revirtInText(css) : css, wasPromise: false };
    }
    case 'renderAsync': {
      const renderer = buildRenderer(api, op);
      const out = await renderer.render();
      return { css: String(out) };
    }
    case 'renderCallback': {
      const renderer = buildRenderer(api, op);
      /*
       * Upstream fires the callback before `render()` returns.  `sync` records
       * whether this one did too, measured by checking the flag on the line
       * after the call rather than inside it -- so the answer is comparable
       * against the oracle instead of being a claim made here.
       */
      let sync = false;
      const captured = await new Promise((resolve) => {
        let settled = false;
        renderer.render((err, css) => {
          settled = true;
          resolve(err ? { error: errShape(err) } : { css: String(css) });
        });
        sync = settled;
        if (!settled) setTimeout(() => resolve({ error: { name: 'Timeout', message: 'callback never fired' } }), 30000);
      });
      if (captured.error) { const e = new Error(captured.error.message); Object.assign(e, captured.error); throw e; }
      return { ...captured, sync };
    }
    case 'renderTopLevel': {
      const out = api.render(sourceOf(op), devirtDeep(op.options || {}));
      if (out && typeof out.then === 'function') {
        const css = String(await out);
        return { css: op.revirtCss ? revirtInText(css) : css, wasPromise: true };
      }
      const css = String(out);
      return { css: op.revirtCss ? revirtInText(css) : css, wasPromise: false };
    }
    case 'deps': {
      const renderer = buildRenderer(api, op);
      const deps = await renderer.deps();
      return { deps: Array.from(deps).map((d) => revirt(String(d))) };
    }
    case 'convertCSS':
      return { styl: String(api.convertCSS(op.css)) };

    case 'sourcemap': {
      const renderer = buildRenderer(api, op);
      const css = await new Promise((resolve, reject) => {
        renderer.render((err, out) => (err ? reject(err) : resolve(out)));
      });
      const map = renderer.sourcemap ? JSON.parse(JSON.stringify(renderer.sourcemap)) : null;
      if (map && Array.isArray(map.sources)) map.sources = map.sources.map(revirt);
      return { css: String(css), sourcemap: map };
    }
    case 'version':
      return { version: String(api.version) };

    case 'apiSurface': {
      const keys = new Set(Object.keys(api));
      if (typeof api === 'function') for (const k of Object.getOwnPropertyNames(api)) keys.add(k);
      return { keys: Array.from(keys).filter((k) => !['length', 'name', 'prototype'].includes(k)).sort() };
    }
    case 'callable':
      return { callable: typeof api === 'function' };

    case 'middlewarePresent':
      return { present: typeof api.middleware === 'function' };

    default:
      throw new Error(`unknown op kind: ${op.kind}`);
  }
}

async function main() {
  let api = null;
  let loadError = null;
  try {
    const ns = await import(pathToFileURL(job.entry).href);
    api = ns.default != null ? ns.default : ns;
  } catch (e) {
    loadError = errShape(e);
  }

  const results = [];
  for (const op of job.ops) {
    if (loadError) { results.push({ id: op.id, ok: false, error: loadError, phase: 'load' }); continue; }
    try { results.push({ id: op.id, ok: true, value: await runOp(api, op) }); }
    catch (e) { results.push({ id: op.id, ok: false, error: errShape(e), phase: 'run' }); }
  }
  fs.writeFileSync(outPath, JSON.stringify({ loadError, results }));
}

main().then(() => process.exit(0)).catch((e) => {
  try {
    fs.writeFileSync(outPath, JSON.stringify({
      loadError: { name: 'DriverError', message: e && e.stack ? e.stack : String(e) },
      results: [],
    }));
  } catch { /* nothing left to do */ }
  process.exit(0);
});
