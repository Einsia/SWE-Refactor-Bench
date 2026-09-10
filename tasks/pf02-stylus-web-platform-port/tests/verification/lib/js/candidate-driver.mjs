/*
 * Drives whichever tree it is pointed at through the package's PUBLISHED entry
 * point, and reports the answers as JSON.
 *
 *   cd <consumer dir> && node candidate-driver.mjs <job.json> <out.json>
 *
 * The consumer directory holds `node_modules/stylus` -> the tree under test, and
 * this file does `await import('stylus')`.  Nothing here names a path inside
 * either tree, which is the whole point: Node's own package resolution picks
 * `exports["."]` when the tree publishes one and `main` when it does not, so the
 * original (CommonJS, `main: ./index.js`) and a submission (ESM, `exports` ->
 * `src/node/index.js`) are reached by the same line of code.  A candidate cannot
 * tell the two apart by how its questions are asked, only by the answers.
 *
 * This is deliberately the same op vocabulary stage 2 uses, so a divergence found
 * here can be handed to that suite as a regression module without translation.
 *
 * ---------------------------------------------------------------------------
 * Paths, and why they are collapsed rather than compared
 * ---------------------------------------------------------------------------
 * A Stylus error names the file it was compiling, in `.message`, in `.filename`
 * and once per frame in `.stylusStack`.  `linenos` and `firebug` write source
 * paths into the CSS.  Source maps list them in `sources`.  Every one of those is
 * an absolute path in whatever directory the harness staged the tree into -- and
 * the harness stages the two trees into DIFFERENT directories, named by a hash,
 * on purpose.  Left alone, `render(..., {linenos: true})` differs between the two
 * trees for no reason but the unpack location, and every candidate in the stage
 * is a free break worth ten points that says nothing about the migration.
 *
 * So paths are virtualised on the way out:
 *
 *   the candidate's own project dir  ->  /proj/...     path PRESERVED
 *   anywhere inside the tree itself  ->  /tree/<internal>   path COLLAPSED
 *
 * The asymmetry is the interesting part.  A candidate's own stylesheets keep
 * their exact relative paths, because "which file did the error blame, and at
 * which line" is a real question and this stage's to ask.  Paths inside the tree
 * are collapsed to a single token instead, because the one such path that shows
 * up in output is the built-in `.styl` library -- State A serves it from
 * `lib/functions/index.styl`, and instruction.md §2.1 requires `lib/` to be gone
 * while §1.4 declines to say where under `src/core/` its replacement lives.  The
 * two trees therefore MUST disagree about that path, by construction, and a
 * candidate comparing it would be scoring the submission on the one thing the
 * task deliberately left free.  Collapsing makes that candidate impossible rather
 * than merely against the rules; probe.toml denies it as well, and a rule the
 * mechanism already enforces is a rule nobody has to adjudicate.
 */
import fs from 'node:fs';
import path from 'node:path';

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const outPath = process.argv[3];

/* Absolute, and the candidate knows it as `/proj`. */
const PROJ = job.projRoot ? path.resolve(job.projRoot) : null;
/* Absolute, and the candidate never sees it under any name. */
const TREE = job.treeRoot ? path.resolve(job.treeRoot) : null;

const VPROJ = '/proj';
const VTREE = '/tree/<internal>';

const mounts = PROJ ? { [VPROJ]: PROJ } : {};

/* A virtual path as written by a candidate -> the real one to hand the API. */
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

/* Collapse every path under the tree root to one token. See the header. */
function collapseTree(text) {
  if (!TREE || typeof text !== 'string') return text;
  let out = text;
  for (const root of [TREE, TREE.replace(/[/:]/g, (c) => `\\${c}`)]) {
    let cut = out.split(`${root}/`);
    out = cut.length > 1 ? cut.map((s, i) => (i === 0 ? s : s.replace(/^[^\s'")]*/, ''))).join(VTREE) : out;
    out = out.split(root).join(VTREE);
  }
  return out;
}

/* A real path as produced by the API -> what the candidate is shown. */
function revirt(p) {
  if (typeof p !== 'string') return p;
  for (const [virt, real] of Object.entries(mounts)) {
    const abs = path.resolve(real);
    if (p === abs) return virt;
    if (p.startsWith(`${abs}/`)) return `${virt.replace(/\/$/, '')}/${p.slice(abs.length + 1)}`;
  }
  return collapseTree(p);
}

/*
 * The same mapping applied inside a larger string -- an error message, a CSS
 * comment, a source map's JSON.  Longest root first so a nested mount cannot be
 * half-replaced by its parent, and the backslash-escaped form too, because a
 * source map is JSON and its separators arrive escaped.
 */
function revirtInText(text) {
  if (typeof text !== 'string') return text;
  let out = text;
  const roots = Object.entries(mounts)
    .map(([v, r]) => [path.resolve(r), v.replace(/\/$/, '')])
    .sort((a, b) => b[0].length - a[0].length);
  for (const [abs, v] of roots) {
    out = out.split(abs).join(v);
    out = out.split(abs.replace(/[/:]/g, (c) => `\\${c}`)).join(v.replace(/[/:]/g, (c) => `\\${c}`));
  }
  return collapseTree(out);
}

/*
 * What a candidate is told about a thrown thing.  `.stack` is deliberately not
 * here: it is a list of absolute paths into the implementation, it differs
 * between two trees that are supposed to be organised differently, and a
 * candidate asserting on it would be asserting on the implementation -- which is
 * stage 1's subject and explicitly out of scope here.
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

/*
 * A plugin body or a `define`d function, compiled from source the candidate
 * wrote.  There is no sandbox here and none is wanted: this driver imports the
 * tree as an ordinary module, so `new Function` grants nothing that importing it
 * did not already grant.
 *
 * `stylus` is in scope for the compiled body, bound to the package's own entry
 * point, because a function that returns a value into a stylesheet often needs
 * `stylus.nodes.Unit` or `stylus.nodes.RGBA` to say what it means.  `require` is
 * NOT in scope -- this driver is an ES module, so there is none to give -- and
 * that is true identically on both trees, which is what matters here.  Without
 * the binding, a candidate reaching for `nodes` gets a ReferenceError on the
 * original as well as on the submission and loses the attempt to the harness
 * rather than to the migration.
 */
function compileFn(what, src, api) {
  try {
    // eslint-disable-next-line no-new-func
    return new Function('stylus', `return (${src});`)(api);
  } catch (e) {
    throw new Error(`${what} did not compile: ${e && e.message}`);
  }
}

function sourceOf(op) {
  if (typeof op.sourceFile === 'string') return fs.readFileSync(devirt(op.sourceFile), 'utf8');
  return op.source;
}

function buildRenderer(api, op) {
  const options = devirtDeep(op.options || {});
  const factory = typeof api === 'function' ? api : api.stylus;
  if (typeof factory !== 'function') {
    throw new Error("the package's entry point is not callable and has no .stylus");
  }
  const renderer = factory(sourceOf(op), options);
  for (const [k, v] of Object.entries(op.set || {})) {
    renderer.set(k, k === 'filename' || k === 'paths' ? devirtDeep(v) : v);
  }
  for (const p of op.include || []) renderer.include(devirt(p));
  for (const p of op.import || []) renderer.import(devirt(p));
  for (const [name, value] of Object.entries(op.define || {})) renderer.define(name, value);
  for (const [name, value] of Object.entries(op.defineRaw || {})) renderer.define(name, value, true);
  for (const [name, src] of Object.entries(op.defineFn || {})) {
    renderer.define(name, compileFn(`defineFn ${name}`, src, api));
  }
  for (const [name, src] of Object.entries(op.defineFnRaw || {})) {
    renderer.define(name, compileFn(`defineFnRaw ${name}`, src, api), true);
  }
  for (const [i, src] of (op.use || []).entries()) renderer.use(compileFn(`use[${i}]`, src, api));
  if (op.defineResolver) renderer.define('url', api.resolver(devirtDeep(op.resolverOptions || {})));
  if (op.defineUrl) renderer.define('url', api.url(devirtDeep(op.urlOptions || {})));
  return renderer;
}

async function runOp(api, op) {
  switch (op.kind) {
    /*
     * The synchronous face.  instruction.md §1.5 says this returns a String on
     * top of a core that is fundamentally async, so a returned Promise is
     * REPORTED as one -- `wasPromise` -- rather than quietly awaited.  A
     * candidate that awaits the answer would never see the difference.
     */
    case 'render': {
      const out = buildRenderer(api, op).render();
      const wasPromise = !!(out && typeof out.then === 'function');
      const css = String(wasPromise ? await out : out);
      return { css: revirtInText(css), wasPromise };
    }

    case 'renderAsync': {
      const css = await buildRenderer(api, op).render();
      return { css: revirtInText(String(css)) };
    }

    /*
     * Upstream fires the callback before `render()` returns.  Whether this one
     * did too is recorded by reading a flag on the line after the call, so the
     * answer is measured rather than claimed.
     */
    case 'renderCallback': {
      const renderer = buildRenderer(api, op);
      let sync = false;
      const captured = await new Promise((resolve) => {
        let settled = false;
        renderer.render((err, css) => {
          settled = true;
          resolve(err ? { error: errShape(err) } : { css: revirtInText(String(css)) });
        });
        sync = settled;
        if (!settled) {
          setTimeout(
            () => resolve({ error: { name: 'Timeout', message: 'callback never fired' } }),
            30000,
          );
        }
      });
      if (captured.error) {
        const e = new Error(captured.error.message);
        Object.assign(e, captured.error);
        throw e;
      }
      return { ...captured, sync };
    }

    case 'renderTopLevel': {
      const out = api.render(sourceOf(op), devirtDeep(op.options || {}));
      const wasPromise = !!(out && typeof out.then === 'function');
      const css = String(wasPromise ? await out : out);
      return { css: revirtInText(css), wasPromise };
    }

    case 'deps': {
      const deps = await buildRenderer(api, op).deps();
      return { deps: Array.from(deps).map((d) => revirt(String(d))) };
    }

    case 'sourcemap': {
      const renderer = buildRenderer(api, op);
      const css = await new Promise((resolve, reject) => {
        const out = renderer.render((err, c) => (err ? reject(err) : resolve(c)));
        if (out && typeof out.then === 'function') out.then(resolve, reject);
      });
      let map = renderer.sourcemap ? JSON.parse(JSON.stringify(renderer.sourcemap)) : null;
      /*
       * `sources` arrive relative to the process cwd, which is the consumer
       * directory -- measured: `['../../scratch/proj/m.styl']`.  Resolving before
       * virtualising turns that into `/proj/m.styl`, which is what a candidate
       * would naturally write down and what actually means something.
       *
       * Not merely cosmetic.  The unresolved form is a chain of `..` whose length
       * depends on where the staged tree sits relative to the project, so it is
       * only comparable between the two trees for as long as their directory
       * depths agree.  They do agree today -- both staged names are one
       * `sha256(...)[:16]` -- but that is an invariant of the runner, held one
       * refactor away from here.  Resolve-then-virtualise does not depend on it.
       */
      if (map && Array.isArray(map.sources)) {
        map.sources = map.sources.map((s) =>
          typeof s === 'string' ? revirt(path.resolve(process.cwd(), s)) : s,
        );
      }
      if (map && Array.isArray(map.sourcesContent)) {
        map.sourcesContent = map.sourcesContent.map((s) => (typeof s === 'string' ? revirtInText(s) : s));
      }
      return { css: revirtInText(String(css)), sourcemap: map };
    }

    case 'convertCSS':
      return { styl: String(api.convertCSS(op.css)) };

    case 'version':
      return { version: String(api.version) };

    /*
     * The names the package publishes.  A candidate may compare the two sets, and
     * a name present on one tree and absent on the other is a real finding -- an
     * existing Node consumer that reaches for it gets `undefined`.  What a
     * candidate may NOT do is assert on names it invented an expectation for:
     * the original's set is whatever the original has, and `try_test` will tell
     * it so.
     *
     * FIVE names are removed, and two of them are the reason this filter is not
     * just tidiness.  `length`, `name` and `prototype` are on every function and
     * say nothing.  `arguments` and `caller` are own properties of a function
     * declared in SLOPPY mode and do not exist on one declared in STRICT mode --
     * and the original is CommonJS, which is sloppy, while every legal submission
     * is an ES module, which is strict.  Measured, not assumed:
     *
     *     node        -e 'function f(){}; Object.getOwnPropertyNames(f)'
     *       -> length,name,arguments,caller,prototype
     *     node --input-type=module -e (the same)
     *       -> length,name,prototype
     *
     * So `assert "arguments" in surface` passes on the original, fails on every
     * correct submission, and reproduces perfectly.  Unfiltered, that one line is
     * a break available to all six rounds, costing any submission the whole 30
     * points for having done exactly what instruction.md §1.5 told it to do.  It
     * is denied in probe.toml as well; it is removed here because a rule the
     * mechanism enforces is a rule nobody has to notice.
     */
    case 'apiSurface': {
      const DIALECT = ['length', 'name', 'prototype', 'arguments', 'caller'];
      const keys = new Set(Object.keys(api));
      if (typeof api === 'function') for (const k of Object.getOwnPropertyNames(api)) keys.add(k);
      return { keys: Array.from(keys).filter((k) => !DIALECT.includes(k)).sort() };
    }

    case 'callable':
      return { callable: typeof api === 'function' };

    /* `.get()` after `.set()`, including the keys the renderer defaults. */
    case 'get': {
      const renderer = buildRenderer(api, op);
      const out = {};
      for (const k of op.keys || []) {
        const v = renderer.get(k);
        out[k] = typeof v === 'string' ? revirtInText(v) : v;
      }
      return { got: out };
    }

    /*
     * `stylus.middleware` as Connect would drive it: a bare req/res pair, a GET
     * for a `.css` path, and the compiled file the middleware leaves on disk.
     * It is the one part of the published surface that is neither the JS API nor
     * the CLI, it is in §1.5's list of things that must keep working, and no
     * stage-2 module drives it -- `middlewarePresent` only asks whether the name
     * exists.  Its answer here is the CSS that arrived on disk, not the response
     * object, because the response is Connect's business and the file is Stylus's.
     */
    case 'middleware': {
      if (typeof api.middleware !== 'function') throw new Error('api.middleware is not a function');
      const src = devirt(op.src || VPROJ);
      const dest = devirt(op.dest || VPROJ);
      const mw = api.middleware({ src, dest, compile: undefined, ...devirtDeep(op.middlewareOptions || {}) });
      const url = op.url || '/style.css';
      const req = { url, method: op.method || 'GET', headers: {} };
      const res = { setHeader() {}, end() {}, writeHead() {} };
      const outcome = await new Promise((resolve) => {
        let done = false;
        const finish = (v) => { if (!done) { done = true; resolve(v); } };
        try {
          mw(req, res, (err) => finish(err ? { error: errShape(err) } : { called: 'next' }));
        } catch (e) {
          finish({ error: errShape(e) });
        }
        setTimeout(() => finish({ error: { name: 'Timeout', message: 'middleware never called next' } }), 30000);
      });
      const written = path.join(dest, url.replace(/^\//, ''));
      let css = null;
      try { css = fs.readFileSync(written, 'utf8'); } catch { /* it did not write one */ }
      return { ...outcome, css: css === null ? null : revirtInText(css), wrote: css !== null };
    }

    default:
      throw new Error(`unknown op kind: ${op.kind}`);
  }
}

async function main() {
  let api = null;
  let loadError = null;
  try {
    /*
     * The published entry point, resolved by Node from `node_modules/stylus`.
     * Not a path into the tree: see the header.
     */
    const ns = await import('stylus');
    api = ns.default != null ? ns.default : ns;
  } catch (e) {
    loadError = errShape(e);
  }

  const results = [];
  for (const op of job.ops) {
    if (loadError) {
      results.push({ id: op.id, ok: false, error: loadError, phase: 'load' });
      continue;
    }
    try {
      results.push({ id: op.id, ok: true, value: await runOp(api, op) });
    } catch (e) {
      results.push({ id: op.id, ok: false, error: errShape(e), phase: 'run' });
    }
  }
  fs.writeFileSync(outPath, JSON.stringify({ loadError, results }));
}

main().then(
  () => process.exit(0),
  (e) => {
    /*
     * Exit 0 even here.  The helper reads the output file and turns a missing one
     * into a DriverFailure with this text in it; exiting non-zero as well would
     * make a broken driver look like a failing candidate.
     */
    try {
      fs.writeFileSync(
        outPath,
        JSON.stringify({
          loadError: { name: 'DriverError', message: e && e.stack ? e.stack : String(e) },
          results: [],
        }),
      );
    } catch { /* nothing left to do */ }
    process.exit(0);
  },
);
