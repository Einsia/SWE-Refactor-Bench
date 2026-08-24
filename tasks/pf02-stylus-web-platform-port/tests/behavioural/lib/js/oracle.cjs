/*
 * One CommonJS Stylus package, driven over the shared op vocabulary.
 *
 *   node oracle.cjs <job.json> <out.json>
 *
 * Same vocabulary as sandbox.mjs, so a case is described once and answered twice
 * -- by upstream Stylus 0.63.0 here, and by the submission there.  Nothing is
 * precomputed and shipped with the task: the agent has no expectation table to
 * fit to, because the table does not exist until the verifier runs.
 *
 * Two callers, and the difference between them is one flag.
 *
 * *The oracle.*  `job.stateARoot` is /opt/state-a, the immutable copy baked into
 * the stage-2 image, and `job.instrument` is absent.  Nothing below the flag runs,
 * so what this driver reports as an expectation is what it reported before the
 * flag existed.
 *
 * *A pre-migration submission.*  `layout.face()` returns `pre-migration` for a
 * tree that still presents one CommonJS package -- the shape State A had -- and
 * `harness.runners` then points this same driver at the staged tree with
 * `instrument: true`.  The flag adds the three things the realm driver reports and
 * a CommonJS require does not: the access log, the module count, and a load error
 * instead of a dead process.  It adds nothing to how an op is answered, which is
 * the property that makes the two comparable.
 *
 * The instrumented mode must be pointed at the *staged* tree, never at
 * $SRB_REPO: `harness.runners` addresses the submission through the copy
 * `prepare` built, so that a run cannot write inside the artifact it is grading.
 */
'use strict';

const fs = require('fs');
const path = require('path');

const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const outPath = process.argv[3];

/*
 * Error text names files.  A Stylus error message embeds the filename and a
 * source excerpt, and `stylusStack` names one file per frame -- all of them
 * absolute, and all of them pointing into the oracle's own tree.  They are
 * re-virtualised here for the same reason paths are: otherwise every comparison
 * of a failure would fail on the prefix, and the submission would be blamed for
 * where the verifier happened to unpack State A.
 *
 * `revirtInText` is declared below; function declarations hoist, and no error is
 * shaped before the job starts running.
 */
function errShape(e) {
  if (e === null || typeof e !== 'object') {
    return { message: revirtInText(String(e)), name: 'Thrown' };
  }
  return {
    name: typeof e.name === 'string' ? e.name : undefined,
    message: typeof e.message === 'string' ? revirtInText(e.message) : String(e),
    lineno: typeof e.lineno === 'number' ? e.lineno : undefined,
    column: typeof e.column === 'number' ? e.column : undefined,
    filename: typeof e.filename === 'string' ? revirt(e.filename) : undefined,
    stylusStack: typeof e.stylusStack === 'string' ? revirtInText(e.stylusStack) : undefined,
  };
}

// The sandbox addresses files through a virtual root; State A addresses them on
// disk.  Ops carry virtual paths, and these two helpers translate.  Keeping the
// translation in one place is what makes the two runs comparable.
/*
 * Mounts, longest real path first.  Order matters because one mount's target can
 * sit inside another's: the built-in library is `<stateA>/lib/functions`, which is
 * also under the `/proj` mount at `<stateA>`.  The sandbox reaches that library
 * at `platform.runtimeRoot`, so it has to be translated as `/runtime/...`, and
 * only the more specific mount produces that.  Matching shortest-first would
 * rewrite it to `/proj/lib/functions/...` and no later pass could recover it.
 */
const MOUNTS = Object.entries(job.mounts || {}).sort(
  (a, b) => path.resolve(b[1]).length - path.resolve(a[1]).length,
);

/*
 * The access log, for the instrumented mode only.
 *
 * `platform-bootstrap.js` records every call the realm's capability object
 * receives, as `"<op> <outcome> <path>"` with the path virtual.  The same shape is
 * produced here by wrapping the filesystem the package uses, and for the same
 * purpose: right CSS with an empty log means the bytes did not come from the
 * files the harness mounted.
 *
 * Only paths that fall inside a mount are recorded.  `revirt` returns its argument
 * unchanged for anything else -- a `node_modules` read, the package's own
 * JavaScript -- and logging those would put the host's real layout into a report
 * that is compared against a virtual one, besides burying the reads that matter
 * under a few hundred that do not.
 *
 * The wrap is installed before the package is required, so a module that
 * destructured `readFileSync` off `fs` at load time captures the wrapper rather
 * than escaping it.
 */
const accessLog = [];

function isMounted(p) {
  if (typeof p !== 'string') return false;
  for (const [, real] of MOUNTS) {
    const abs = path.resolve(real);
    if (p === abs || p.startsWith(`${abs}/`)) return true;
  }
  return false;
}

function note(op, target, outcome) {
  const p = typeof target === 'string' ? target : String(target);
  if (!isMounted(p)) return;
  accessLog.push(`${op} ${outcome} ${revirt(p)}`);
}

/*
 * `<fs name>` -> the capability the realm would have answered it with.  Upstream
 * resolves imports with `statSync` and `glob.sync`, reads sources and binary
 * assets with `readFileSync`, and lists directories with `readdirSync`; the port
 * does all three through `platform.readFile`, `readDir` and `stat`.  Mapping the
 * names is what lets one assertion describe both.
 */
const FS_WRAPS = {
  readFileSync: 'readFile',
  readFile: 'readFile',
  readdirSync: 'readDir',
  readdir: 'readDir',
  statSync: 'stat',
  stat: 'stat',
  lstatSync: 'stat',
  existsSync: 'stat',
  accessSync: 'stat',
};

/*
 * A directory listing handed back reversed, when the job asks for it.
 *
 * `platform.readDir` is documented unordered, and the realm driver hands the core
 * a reversed listing to make sure a port does not forward the host's order into
 * its output.  The same perturbation belongs at the same boundary here: upstream's
 * globs come from `glob@7`, which sorts, so it is a question a pre-migration tree
 * genuinely answers rather than one that only has a State-B form.
 *
 * Applied inside the mounts only, for the reason `note` is: reversing what
 * `node_modules` looks like would perturb the module loader rather than the
 * compiler.
 */
function maybeReverse(target, names) {
  if (job.readDirOrder !== 'reverse' || !Array.isArray(names)) return names;
  if (!isMounted(typeof target === 'string' ? target : String(target))) return names;
  return names.slice().reverse();
}

function instrumentFs() {
  for (const [name, op] of Object.entries(FS_WRAPS)) {
    const original = fs[name];
    if (typeof original !== 'function') continue;
    fs[name] = function wrapped(target, ...rest) {
      try {
        let out = original.call(fs, target, ...rest);
        if (name === 'readdirSync') out = maybeReverse(target, out);
        // `existsSync` reports absence by returning false rather than throwing,
        // so its outcome comes from the value; every other name throws.
        note(op, target, name === 'existsSync' && out === false ? 'ENOENT' : 'ok');
        return out;
      } catch (e) {
        note(op, target, (e && e.code) || 'error');
        throw e;
      }
    };
  }
}

/*
 * Load the package.  In the instrumented mode a failure to load is a fact about
 * the submission, so it is reported the way the realm driver reports it -- one
 * `loadError` and a `phase: 'load'` result for every op -- rather than by exiting
 * without an output file, which `harness.runners` would raise as a harness fault.
 *
 * `moduleCount` is the size of the require cache the load added.  It answers the
 * same question `sandbox.mjs` answers with the size of its module map: something
 * was linked, and this is how much of it.
 */
let stylus = null;
let loadError = null;
let moduleCount = 0;

if (job.instrument) instrumentFs();

try {
  const before = Object.keys(require.cache).length;
  stylus = require(path.resolve(job.stateARoot));
  moduleCount = Object.keys(require.cache).length - before;
} catch (e) {
  if (!job.instrument) throw e;   // the oracle failing to load is a harness fault
  loadError = errShape(e);
}

function devirt(p) {
  if (typeof p !== 'string') return p;
  for (const [virt, real] of MOUNTS) {
    if (p === virt) return path.resolve(real);
    if (p.startsWith(virt.endsWith('/') ? virt : `${virt}/`)) {
      return path.resolve(real, p.slice(virt.length).replace(/^\/+/, ''));
    }
  }
  return p;
}

/*
 * An op either carries its source inline or names a file to compile.  The second
 * form exists because several options (`linenos`, `firebug`, inline sourcemaps)
 * stat and read the source file, so they can only be exercised on a path that
 * really resolves -- and because compiling the corpus's own bytes is what makes
 * the two runs comparable: the sandbox has the same file mounted in its VFS.
 */
function sourceOf(op) {
  if (typeof op.sourceFile === 'string') {
    return fs.readFileSync(devirt(op.sourceFile), 'utf8');
  }
  return op.source;
}

function buildRenderer(op) {
  const options = {};
  // `dest` joins `filename` and `paths` as a location rather than a value: the
  // sourcemapper computes `relative(dest, ...)` against it, so a virtual `dest`
  // measured against a real `filename` would differ by the mount prefix alone.
  for (const [k, v] of Object.entries(op.options || {})) {
    options[k] = k === 'filename' || k === 'paths' || k === 'dest' ? devirtDeep(v) : v;
  }
  if (op.options && op.options.imports) options.imports = devirtDeep(op.options.imports);
  // `sourcemap` carries paths of its own (`basePath`, `sourceRoot`).
  if (op.options && op.options.sourcemap && typeof op.options.sourcemap === 'object') {
    options.sourcemap = devirtDeep(op.options.sourcemap);
  }
  const renderer = stylus(sourceOf(op), options);
  // `filename` and `paths` both name locations, and `paths` may be a list, so
  // both go through the deep translation.  Anything else is a value, not a path,
  // and is passed through untouched.
  for (const [k, v] of Object.entries(op.set || {})) {
    renderer.set(k, k === 'filename' || k === 'paths' ? devirtDeep(v) : v);
  }
  for (const p of op.include || []) renderer.include(devirt(p));
  for (const [name, value] of Object.entries(op.define || {})) renderer.define(name, value);
  for (const [name, value] of Object.entries(op.defineRaw || {})) renderer.define(name, value, true);
  // `defineFn` carries JS source rather than a value: a plugin function, which is
  // the thing `.define` mainly exists for and the thing JSON cannot express.  The
  // same text is compiled on both sides, so what differs is what Stylus hands it.
  for (const [name, src] of Object.entries(op.defineFn || {})) renderer.define(name, compileFn(name, src));
  for (const [name, src] of Object.entries(op.defineFnRaw || {})) {
    renderer.define(name, compileFn(name, src), true);
  }
  if (op.defineResolver) renderer.define('url', stylus.resolver(devirtDeep(op.resolverOptions || {})));
  // `urlName` defaults to 'url'.  It exists because the lexer treats everything
  // inside `url(` as url characters -- the comma included -- so the plugin's
  // second `encoding` argument is only reachable under a different name.
  if (op.defineUrl) {
    renderer.define(op.urlName || 'url', stylus.url(devirtDeep(op.urlOptions || {})));
  }
  return renderer;
}

/*
 * Job-supplied JS source, compiled in the host realm.  Safe here in a way it is
 * not in sandbox.mjs: this driver runs the task's own oracle, never submitted
 * code, and the sources come from the catalog in lib/harness/jsapi.py.
 */
function compileFn(name, src) {
  try {
    // eslint-disable-next-line no-new-func
    return new Function(`return (${src});`)();
  } catch (e) {
    throw new Error(`defineFn ${name} did not compile: ${e && e.message}`);
  }
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

// Paths that leave the oracle must be re-virtualised, or every comparison of a
// dependency list or a sourcemap `sources` array would fail on the prefix alone.
function revirt(p) {
  if (typeof p !== 'string') return p;
  for (const [virt, real] of MOUNTS) {
    const abs = path.resolve(real);
    if (p === abs) return virt;
    if (p.startsWith(`${abs}/`)) return `${virt.replace(/\/$/, '')}/${p.slice(abs.length + 1)}`;
  }
  return p;
}

/*
 * `linenos` and `firebug` write the source path into the CSS itself, so the
 * output cannot be compared until the oracle's real prefix is mapped back to the
 * virtual one.  `firebug` additionally escapes the path for a font-family value
 * -- every `/` becomes `\/` and `:` becomes `\:` -- so both spellings have to be
 * rewritten or the escaped copy survives and the probe fails on the prefix.
 */
function revirtInText(text) {
  if (typeof text !== 'string') return text;
  let out = text;
  for (const [virt, real] of MOUNTS) {
    const abs = path.resolve(real);
    const v = virt.replace(/\/$/, '');
    out = out.split(abs).join(v);
    // The escaped spelling: `\/a\/b` and `file\:\/\/\/a\/b`.
    const absEsc = abs.replace(/[/:]/g, (c) => `\\${c}`);
    const vEsc = v.replace(/[/:]/g, (c) => `\\${c}`);
    out = out.split(absEsc).join(vEsc);
  }
  // An inline sourcemap carries its `sources` inside base64 JSON, where a text
  // substitution cannot reach them.  Decode, rewrite, re-encode.
  return out.replace(
    /(sourceMappingURL=data:application\/json;base64,)([A-Za-z0-9+/=]+)/g,
    (whole, prefix, b64) => {
      try {
        const json = JSON.parse(Buffer.from(b64, 'base64').toString('utf8'));
        if (Array.isArray(json.sources)) json.sources = json.sources.map(revirt);
        if (typeof json.file === 'string') json.file = revirt(json.file);
        return prefix + Buffer.from(JSON.stringify(json), 'utf8').toString('base64');
      } catch (e) {
        return whole;
      }
    },
  );
}

function runOp(op) {
  switch (op.kind) {
    case 'render': {
      const renderer = buildRenderer(op);
      const out = renderer.render();
      /*
       * State A cannot return a Promise here, so this flag always comes back
       * false from the oracle.  It is reported anyway rather than asserted in
       * Python, because a differential row states the contract in the same
       * voice as every other row: whatever State A does, do that.  §1.5 is the
       * clause at stake, and a port whose `render()` went async fails here.
       */
      const wasPromise = !!(out && typeof out.then === 'function');
      // `revirtCss` is set for the options that write the source path into the
      // output (`linenos`, `firebug`, inline sourcemaps).
      const css = String(out);
      return { css: op.revirtCss ? revirtInText(css) : css, wasPromise };
    }
    case 'renderTopLevel': {
      /*
       * `stylus.render(str, opts)` is its own export -- `new Renderer(str,
       * opts).render(fn)` -- and it takes no renderer configuration, so an op
       * that carried some would be silently ignoring it.
       */
      for (const k of ['set', 'define', 'defineFn', 'defineRaw', 'defineFnRaw', 'include']) {
        if (op[k] && Object.keys(op[k]).length) {
          throw new Error(`renderTopLevel op ${op.id} carries ${k}, which stylus.render() cannot honour`);
        }
      }
      const options = {};
      for (const [k, v] of Object.entries(op.options || {})) {
        options[k] = k === 'filename' || k === 'paths' ? devirtDeep(v) : v;
      }
      const out = stylus.render(sourceOf(op), options);
      const wasPromise = !!(out && typeof out.then === 'function');
      const css = String(out);
      return { css: op.revirtCss ? revirtInText(css) : css, wasPromise };
    }
    case 'renderCallback': {
      const renderer = buildRenderer(op);
      let captured;
      renderer.render((err, css) => { captured = err ? { error: errShape(err) } : { css: String(css) }; });
      /*
       * Upstream calls back before `render()` returns.  Recorded rather than
       * thrown so the submission is compared against it: a port that made the
       * callback asynchronous still produces the right CSS, and the difference
       * would otherwise be invisible.
       */
      const sync = captured !== undefined;
      if (!sync) throw new Error('render(cb) did not call back synchronously');
      if (captured.error) { const e = new Error(captured.error.message); Object.assign(e, captured.error); throw e; }
      return { ...captured, sync };
    }
    case 'deps': {
      const renderer = buildRenderer(op);
      return { deps: renderer.deps().map(revirt) };
    }
    case 'convertCSS':
      return { styl: String(stylus.convertCSS(op.css)) };

    case 'sourcemap': {
      const renderer = buildRenderer(op);
      let css = null;
      renderer.render((err, out) => { if (err) throw err; css = out; });
      const map = renderer.sourcemap ? JSON.parse(JSON.stringify(renderer.sourcemap)) : null;
      if (map && Array.isArray(map.sources)) map.sources = map.sources.map(revirt);
      /*
       * `sources` is revirted above whatever the op says, because a map's paths
       * are the artifact.  The CSS needs the same treatment only when an option
       * writes a path into it -- `linenos` composed with a map does, plain
       * `sourcemap` does not -- and that is per-case, so it comes from the op.
       * Without it such a row compares this tree's absolute prefix against the
       * `/runtime` and `/proj` the realm is the only names for, and fails on the
       * harness's directory layout rather than on the port.
       */
      const out = String(css);
      return { css: op.revirtCss ? revirtInText(out) : out, sourcemap: map };
    }
    case 'pathAlgebra': {
      const out = {};
      for (const v of op.vectors) {
        try { out[v.id] = String(path.posix[v.fn].apply(path.posix, v.args)); }
        catch (e) { out[v.id] = `THREW:${e && e.message}`; }
      }
      return { values: out };
    }
    case 'version':
      return { version: String(stylus.version) };

    case 'apiSurface':
      return { keys: Object.keys(stylus).sort() };

    /*
     * `module.exports = render`, so the export is a function with properties
     * hung off it.  A port that exported a plain object would break every
     * `stylus(str)` call site while passing an `Object.keys` comparison, which
     * is why this is its own op rather than a field of apiSurface.
     */
    case 'callable':
      return { callable: typeof stylus === 'function' };

    case 'middlewarePresent':
      return { present: typeof stylus.middleware === 'function' };

    /*
     * State A's own list of built-ins, so the probe catalog can be checked
     * against it at verify time rather than against a number written down once.
     * If upstream ever gained a built-in, a hard-coded count would go stale
     * silently; this makes the suite notice.
     */
    case 'builtinRegistry': {
      const fns = require(path.resolve(job.stateARoot, 'lib/functions/index.js'));
      const js = Object.keys(fns).filter((k) => typeof fns[k] === 'function').sort();
      const styl = fs.readFileSync(
        path.resolve(job.stateARoot, 'lib/functions/index.styl'), 'utf8',
      );
      const declared = [...new Set(
        [...styl.matchAll(/^([a-zA-Z-][\w-]*)\(/gm)].map((m) => m[1]),
      )].sort();
      return { js, styl: declared };
    }

    default:
      throw new Error(`unknown op kind: ${op.kind}`);
  }
}

/*
 * `p()` and `warn()` exist only for their console output and both return null,
 * so the return value says nothing about whether they work.  Console is captured
 * here in the same shape the realm reports it ("<level> <formatted>"), which is
 * what makes those two comparable at all.
 */
const util = require('util');
const consoleLog = [];
function captureConsole(fn) {
  const saved = {};
  const levels = ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir'];
  const start = consoleLog.length;
  for (const level of levels) {
    saved[level] = console[level];
    console[level] = (...args) => { consoleLog.push(`${level} ${util.format(...args)}`); };
  }
  try { return fn(); } finally {
    for (const level of levels) console[level] = saved[level];
    // Per-op slice, so a probe sees only what its own compile emitted.
    fn.captured = consoleLog.slice(start);
  }
}

const results = [];
for (const op of job.ops) {
  // A load that failed leaves nothing to ask, and every op says so in the shape
  // `sandbox.mjs` uses, so a module comparing the two does not need to know which
  // driver answered.
  if (loadError) {
    results.push({ id: op.id, ok: false, error: loadError, phase: 'load' });
    continue;
  }
  const run = () => runOp(op);
  try {
    const value = captureConsole(run);
    results.push({ id: op.id, ok: true, value: { ...value, console: run.captured } });
  } catch (e) {
    results.push({ id: op.id, ok: false, error: errShape(e), console: run.captured || [] });
  }
}
fs.writeFileSync(outPath, JSON.stringify({
  results,
  version: stylus ? String(stylus.version) : null,
  consoleLog,
  // Only in the instrumented mode: an oracle run reports what it always reported,
  // so an expectation cannot change because this flag was added.
  ...(job.instrument ? { loadError, moduleCount, accessLog } : {}),
}));
