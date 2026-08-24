/*
 * Host-side driver for the sandboxed core.
 *
 *   node --experimental-vm-modules sandbox.mjs <job.json> <out.json>
 *
 * Reads the submitted src/core sources from disk, compiles them into a realm
 * built by realm-bootstrap.js, and runs a batch of operations against them.
 * Results come back as JSON.
 *
 * Two invariants make the sandbox worth trusting:
 *
 *   1. No host object enters the realm.  Globals and the capability object are
 *      built by in-realm scripts (see the two bootstrap files); the only values
 *      that cross are strings.  A host function would be an escape hatch --
 *      hostFn.constructor("return typeof process")() reaches the host realm.
 *
 *   2. No expectation enters this process.  The driver reports what the
 *      submission produced and nothing else; comparison happens in the pytest
 *      process against a State A oracle this code never sees.  An escape
 *      therefore buys nothing: there is nothing here to read.
 */
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REALM_BOOTSTRAP = fs.readFileSync(path.join(HERE, 'realm-bootstrap.js'), 'utf8');
const PLATFORM_BOOTSTRAP = fs.readFileSync(path.join(HERE, 'platform-bootstrap.js'), 'utf8');

const jobPath = process.argv[2];
const outPath = process.argv[3];
const job = JSON.parse(fs.readFileSync(jobPath, 'utf8'));

const CORE_ROOT = path.resolve(job.coreRoot);

// ---------------------------------------------------------------- realm setup
function createRealm() {
  const context = vm.createContext(Object.create(null), {
    name: 'web-platform-realm',
    codeGeneration: { strings: true, wasm: false },
  });
  vm.runInContext(REALM_BOOTSTRAP, context, { filename: 'realm-bootstrap.js' });
  return context;
}

function installPlatform(context, vfs, spec) {
  // Strings only.  base64 keeps the payload transport-safe for binary assets
  // such as the PNG/GIF/JPEG fixtures image-size() has to parse.
  const vfsJson = JSON.stringify(Object.fromEntries(
    Object.entries(vfs).map(([p, v]) => [p, typeof v === 'string' ? v : Buffer.from(v).toString('base64')]),
  ));
  vm.runInContext(
    `this.__VFS_JSON__ = ${JSON.stringify(vfsJson)};`
    + `this.__PLATFORM_JSON__ = ${JSON.stringify(JSON.stringify(spec))};`,
    context, { filename: 'platform-payload.js' },
  );
  vm.runInContext(PLATFORM_BOOTSTRAP, context, { filename: 'platform-bootstrap.js' });
}

// ------------------------------------------------------------- module linking
const SPECIFIER_OK = /^\.{1,2}\//;

class LinkError extends Error {}

function resolveSpecifier(spec, referrerPath) {
  if (!SPECIFIER_OK.test(spec)) {
    throw new LinkError(
      `src/core must not import ${JSON.stringify(spec)} `
      + `(from ${path.relative(CORE_ROOT, referrerPath)}): only ./ and ../ specifiers exist here`,
    );
  }
  let target = path.resolve(path.dirname(referrerPath), spec);
  const rel = path.relative(CORE_ROOT, target);
  if (rel.startsWith('..') || path.isAbsolute(rel)) {
    throw new LinkError(`src/core must not import outside itself: ${spec} -> ${rel}`);
  }
  const candidates = [target];
  if (!/\.[cm]?js$/.test(target) && !target.endsWith('.json')) {
    candidates.push(`${target}.js`, path.join(target, 'index.js'));
  } else if (fs.existsSync(target) && fs.statSync(target).isDirectory()) {
    candidates.push(path.join(target, 'index.js'));
  }
  for (const c of candidates) {
    if (fs.existsSync(c) && fs.statSync(c).isFile()) return c;
  }
  throw new LinkError(`unresolved import ${JSON.stringify(spec)} from ${path.relative(CORE_ROOT, referrerPath)}`);
}

async function loadCore(context) {
  const cache = new Map();

  const importModuleDynamically = () => {
    throw new LinkError('dynamic import() is not available in this realm');
  };

  const make = (absPath) => {
    if (cache.has(absPath)) return cache.get(absPath);
    const source = fs.readFileSync(absPath, 'utf8');
    const identifier = `core:${path.relative(CORE_ROOT, absPath)}`;
    let mod;
    if (absPath.endsWith('.json')) {
      mod = new vm.SyntheticModule(['default'], function () {
        this.setExport('default', vm.runInContext(`(${source})`, context, { filename: identifier }));
      }, { context, identifier });
    } else {
      mod = new vm.SourceTextModule(source, {
        context, identifier, importModuleDynamically,
      });
    }
    cache.set(absPath, mod);
    return mod;
  };

  const entry = resolveSpecifier('./index.js', path.join(CORE_ROOT, 'x.js'));
  const root = make(entry);
  await root.link((spec, referrer) => {
    const referrerPath = path.join(CORE_ROOT, referrer.identifier.replace(/^core:/, ''));
    return make(resolveSpecifier(spec, referrerPath));
  });
  await root.evaluate();
  return { namespace: root.namespace, moduleCount: cache.size };
}

// ------------------------------------------------------------------ operations
function errShape(e) {
  if (e === null || typeof e !== 'object') return { message: String(e), name: 'Thrown' };
  return {
    name: typeof e.name === 'string' ? e.name : undefined,
    message: typeof e.message === 'string' ? e.message : String(e),
    lineno: typeof e.lineno === 'number' ? e.lineno : undefined,
    column: typeof e.column === 'number' ? e.column : undefined,
    filename: typeof e.filename === 'string' ? e.filename : undefined,
    stylusStack: typeof e.stylusStack === 'string' ? e.stylusStack : undefined,
  };
}

function drain(context, fnName) {
  try {
    const raw = vm.runInContext(`this.${fnName}()`, context, { filename: `${fnName}.js` });
    return JSON.parse(raw);
  } catch { return null; }
}

async function main() {
  const results = [];
  let context, api, moduleCount = 0, loadError = null;

  try {
    context = createRealm();
    installPlatform(context, job.vfs || {}, job.platform || {
      sync: false, cwd: '/proj', runtimeRoot: '/runtime',
    });
    const loaded = await loadCore(context);
    moduleCount = loaded.moduleCount;
    api = loaded.namespace.default;
    if (api == null) throw new LinkError('src/core/index.js has no default export');
    // Published to the realm under a harness-reserved name so in-realm probe
    // scripts can reach the API without the host handing them a host object.
    // instruction.md §1.1 makes reading a `__` name a Gate 1 failure.
    context.__api = api;
  } catch (e) {
    loadError = errShape(e);
  }

  /*
   * Console is drained after every op rather than once at the end.  `p()` and
   * `warn()` return null and exist only for what they print, so a per-op slice is
   * the only way to compare them; the whole-batch log is rebuilt from the slices
   * for the checks that ask about the run as a whole.
   */
  const consoleLog = [];
  const drainOpConsole = () => {
    if (!context) return [];
    const lines = drain(context, '__drainConsole') || [];
    consoleLog.push(...lines);
    return lines;
  };
  drainOpConsole();  // anything printed during module load belongs to no op

  for (const op of job.ops) {
    if (loadError) { results.push({ id: op.id, ok: false, error: loadError, phase: 'load' }); continue; }
    try {
      const value = await runOp(context, api, op, job.vfs || {});
      results.push({ id: op.id, ok: true, value: { ...value, console: drainOpConsole() } });
    } catch (e) {
      results.push({ id: op.id, ok: false, error: errShape(e), phase: 'run', console: drainOpConsole() });
    }
  }

  fs.writeFileSync(outPath, JSON.stringify({
    loadError,
    moduleCount,
    results,
    accessLog: context ? drain(context, '__drainAccessLog') : null,
    consoleLog,
  }));
}

/*
 * Values named in a job reach the realm as arguments to `define()`.  Only primitives
 * are allowed through: a host object would hand submitted code `obj.constructor` and,
 * from there, the host `Function`.  Ops that need a non-primitive (a JS function
 * passed to `.define`, say) belong in nodeapi.mjs, where there is no sandbox to
 * breach.
 *
 * `set()` and the options object are not covered by this: both take structured
 * values legitimately, and both cross by being rebuilt from JSON inside the realm --
 * see `inRealmSetValue`.  Rejecting them here instead is what silently cost 238
 * checks.
 */
function assertPrimitive(where, v) {
  const t = typeof v;
  if (v === null || t === 'string' || t === 'number' || t === 'boolean' || t === 'undefined') return v;
  throw new Error(
    `sandbox job tried to pass a ${t} into the realm at ${where}; only primitives may cross`,
  );
}

/*
 * A `set()` value that is not a primitive, rebuilt inside the realm.
 *
 * `paths` is a list, and `renderer.set('paths', [...])` is how upstream's own suite
 * configures lookup, so the op vocabulary carries it -- oracle.cjs and nodeapi.mjs
 * both accept it.  Passing it through `assertPrimitive` instead made this driver
 * reject every op that sets it, which is not a stricter reading of the security
 * invariant but a different behaviour from the two faces it is compared against:
 * measured 238 refused checks, lost identically by two independently ported trees
 * and by construction unreachable for any submission, since this runs on job data
 * before the submission is called at all.  Under an all-or-nothing stage 2 that is
 * the whole 40 points, not a fraction of them.  It was invisible to the identity
 * control because a pre-migration tree is routed to oracle.cjs by
 * `runners.sandbox`, so State A never reaches this line.
 *
 * The invariant is kept the way `inRealmOptions` keeps it, and for the same reason:
 * the value is serialised out here and parsed by the realm's own JSON, so what the
 * core receives is an in-realm object whose prototype chain is the realm's.  No host
 * object crosses.  JSON is also the whole vocabulary -- a value that will not
 * round-trip cannot be expressed in a job file to begin with -- so a function still
 * cannot arrive by this route, and `define` keeps its primitive-only rule.
 */
function inRealmSetValue(context, where, v) {
  let json;
  try {
    json = JSON.stringify(v);
  } catch {
    json = undefined;
  }
  if (json === undefined) {
    throw new Error(
      `sandbox job tried to pass a ${typeof v} into the realm at ${where}; `
      + 'only JSON-expressible values may cross',
    );
  }
  return vm.runInContext(
    `JSON.parse(${JSON.stringify(json)})`, context, { filename: 'setvalue.js' },
  );
}

/* Rebuilt in-realm from JSON so the options object itself is not a host object. */
function inRealmOptions(context, options, platformExpr = 'this.__platform') {
  return vm.runInContext(
    `(function () { var o = JSON.parse(${JSON.stringify(JSON.stringify(options || {}))});`
    + ` o.platform = ${platformExpr}; return o; }).call(this)`,
    context, { filename: 'options.js' },
  );
}

/*
 * The mirror of oracle.cjs's `sourceOf`: an op may name a file instead of
 * carrying its text.  Read here, from the same VFS payload the realm is given,
 * so both runs compile identical bytes.  Only the decoded string crosses
 * inward, which keeps the no-host-objects invariant.
 */
function sourceOf(vfs, op) {
  if (typeof op.sourceFile !== 'string') return op.source;
  const raw = vfs[op.sourceFile];
  if (raw === undefined) {
    throw new Error(`op ${op.id} named sourceFile ${op.sourceFile}, which is not mounted`);
  }
  return Buffer.from(raw, 'base64').toString('utf8');
}

async function runOp(context, api, op, vfs) {
  const buildRenderer = (o) => {
    const options = inRealmOptions(context, o.options);
    const src = sourceOf(vfs, o);
    const renderer = api.stylus ? api.stylus(src, options) : api(src, options);
    // Primitives cross as themselves; anything else is rebuilt in-realm.  Paths stay
    // virtual on this face -- the realm's platform speaks virtual paths -- so unlike
    // oracle.cjs there is nothing to devirtualise, only the object to reconstruct.
    for (const [k, v] of Object.entries(o.set || {})) {
      const t = typeof v;
      const primitive = v === null || t === 'string' || t === 'number'
        || t === 'boolean' || t === 'undefined';
      renderer.set(k, primitive ? v : inRealmSetValue(context, `set.${k}`, v));
    }
    for (const p of o.include || []) renderer.include(String(p));
    for (const [n, v] of Object.entries(o.define || {})) renderer.define(n, assertPrimitive(`define.${n}`, v));
    for (const [n, v] of Object.entries(o.defineRaw || {})) {
      renderer.define(n, assertPrimitive(`defineRaw.${n}`, v), true);
    }
    /*
     * `defineFn` compiles JS source into a function.  Doing that here would put a
     * host function inside the realm, which is exactly the thing this driver
     * exists to prevent, so those ops are rejected rather than silently dropped:
     * a dropped `define` would produce plausible CSS and a meaningless row.  The
     * plugin-facing API is nodeapi.mjs's dimension anyway.
     */
    for (const k of ['defineFn', 'defineFnRaw']) {
      if (o[k] && Object.keys(o[k]).length) {
        throw new Error(`op ${o.id} carries ${k}; compiled functions cannot cross into the realm`);
      }
    }
    if (o.defineResolver) renderer.define('url', api.resolver(inRealmOptions(context, o.resolverOptions)));
    // See oracle.cjs: `urlName` is how the plugin's `encoding` argument is
    // reached, since the lexer swallows commas inside `url(`.
    if (o.defineUrl) {
      renderer.define(o.urlName || 'url', api.url(inRealmOptions(context, o.urlOptions)));
    }
    return renderer;
  };

  switch (op.kind) {
    case 'render': {
      const renderer = buildRenderer(op);
      const css = op.sync ? renderer.renderSync() : await renderer.render();
      return { css: String(css) };
    }
    case 'renderTopLevel': {
      const css = await api.render(sourceOf(vfs, op), inRealmOptions(context, op.options));
      return { css: String(css) };
    }
    case 'deps': {
      const deps = await buildRenderer(op).deps();
      return { deps: Array.from(deps).map(String) };
    }
    case 'convertCSS':
      return { styl: String(api.convertCSS(op.css)) };

    case 'sourcemap': {
      /*
       * The sourcemapper is a Compiler subclass under `lib/visitor/`, so it is
       * core, and a browser host has to get the same map out of it -- including
       * `sourcesContent`, which upstream fills with `fs.readFileSync` and a port
       * must fill through `platform.readFile`.  That read is the reason this op
       * exists on the sandbox path and not only through the Node adapter.
       */
      const renderer = buildRenderer(op);
      const css = op.sync ? renderer.renderSync() : await renderer.render();
      /*
       * `renderer.sourcemap` is an in-realm object.  It is serialised *inside* the
       * realm and parsed outside, rather than handing it to the host's
       * `JSON.stringify`: that keeps the boundary one-directional in the same way
       * `inRealmOptions` does, and a submission whose map carries a getter or a
       * bare-object prototype is then read the same way on both sides.
       */
      const raw = vm.runInContext(
        '(function (m) { return m === undefined || m === null ? null : JSON.stringify(m); })',
        context, { filename: 'dump-map.js' },
      )(renderer.sourcemap);
      return { css: String(css), sourcemap: raw === null ? null : JSON.parse(String(raw)) };
    }

    case 'apiSurface': {
      // Reading an in-realm object from the host is safe; the invariant runs the
      // other way (no *host* object may enter the realm).
      const keys = Object.keys(api).map(String).sort();
      const types = {};
      for (const k of keys) {
        try { types[k] = typeof api[k]; } catch { types[k] = 'THREW'; }
      }
      return { keys, types, callable: typeof api === 'function' };
    }
    case 'stageParity': {
      /*
       * §2.4, dynamically.  Every method on the named stage's prototype is
       * replaced with a thrower, then both render paths run.  If the two paths
       * share one implementation they both break; if only one breaks, there are
       * two implementations of that stage and the submission has kept a second
       * compiler.  If neither breaks the probe learned nothing -- the stage may
       * not be a prototype-method class at all -- and says so rather than
       * guessing.
       */
      const raw = vm.runInContext(`
        (function () {
          var api = this.__api;
          var Stage = api[${JSON.stringify(op.stage)}];
          if (typeof Stage !== 'function' || !Stage.prototype) {
            return JSON.stringify({ instrumented: 0 });
          }
          var proto = Stage.prototype, n = 0;
          Object.getOwnPropertyNames(proto).forEach(function (k) {
            if (k === 'constructor') return;
            var d = Object.getOwnPropertyDescriptor(proto, k);
            if (!d || !d.writable || typeof d.value !== 'function') return;
            proto[k] = function () { throw new Error('__PF03_STAGE_SENTINEL__'); };
            n++;
          });
          return JSON.stringify({ instrumented: n });
        }).call(this)
      `, context, { filename: 'stage-parity.js' });
      const { instrumented } = JSON.parse(raw);

      /*
       * `inRealmOptions` rather than a host `{ platform }` literal, for the two
       * reasons that idiom exists everywhere else in this file: the object handed
       * to submitted code must be the realm's, and `platform` is not a binding
       * here -- the earlier version of this line referenced it bare, so every
       * attempt threw ReferenceError.
       *
       * That is worth more than the one-word fix suggests.  The throw was caught
       * below and reported as `tripped: false`, which is exactly what this probe
       * reports when a stage legitimately has no prototype methods to instrument
       * -- a case the comment above documents as "learned nothing".  So a broken
       * probe and an inapplicable one were indistinguishable in the result.
       *
       * No module emits this op: §2.4 is enforced in stage 1, by
       * `test_entrypoint.py`'s two duplicate-implementation reviews.  It is left
       * here, working, because the vocabulary is shared and the next caller
       * should not inherit a silent false negative.
       */
      const attempt = async (sync) => {
        try {
          const opts = inRealmOptions(context, op.options);
          const renderer = api.stylus ? api.stylus(op.source, opts) : api(op.source, opts);
          const css = sync ? renderer.renderSync() : await renderer.render();
          return { tripped: false, css: String(css).length };
        } catch (e) {
          const msg = String((e && e.message) || e);
          return { tripped: msg.includes('__PF03_STAGE_SENTINEL__'), message: msg.slice(0, 300) };
        }
      };
      return { stage: op.stage, instrumented, async: await attempt(false), sync: await attempt(true) };
    }
    case 'pathAlgebra': {
      // The core is expected to publish its path helpers so a host can share
      // them; if it does not, the family is reported as unavailable rather than
      // as a wrong answer.
      const p = api.path || api.posix || (api.utils && api.utils.path);
      if (!p) return { unavailable: 'no path helpers exported from src/core' };
      const out = {};
      for (const v of op.vectors) {
        try {
          out[v.id] = String(p[v.fn].apply(p, v.args));
        } catch (e) { out[v.id] = `THREW:${e && e.message}`; }
      }
      return { values: out };
    }
    case 'version':
      return { version: String(api.version) };

    case 'probeGlobals': {
      const raw = vm.runInContext(`
        (function () {
          var own = Object.getOwnPropertyNames(this);
          var typeOf = {};
          own.forEach(function (n) { try { typeOf[n] = typeof this[n]; } catch (e) { typeOf[n] = 'THREW'; } }, this);
          return JSON.stringify({ own: own.sort(), typeOf: typeOf });
        }).call(this)
      `, context, { filename: 'probe-globals.js' });
      return JSON.parse(raw);
    }
    case 'probeReachability': {
      // Asks the realm whether any host capability is reachable from the values
      // the submission was handed.  Reported, not judged, here.
      const raw = vm.runInContext(`
        (function () {
          var findings = [];
          var probe = function (label, fn) {
            try { var r = fn(); if (r !== undefined && r !== null && r !== 'undefined') findings.push(label + '=' + String(r)); }
            catch (e) { }
          };
          probe('typeof process', function () { return typeof process === 'undefined' ? null : typeof process; });
          probe('typeof Buffer', function () { return typeof Buffer === 'undefined' ? null : typeof Buffer; });
          probe('typeof require', function () { return typeof require === 'undefined' ? null : typeof require; });
          probe('typeof global', function () { return typeof global === 'undefined' ? null : typeof global; });
          probe('typeof __dirname', function () { return typeof __dirname === 'undefined' ? null : typeof __dirname; });
          probe('platform.readFile.constructor', function () {
            var F = this.__platform.readFile.constructor;
            return F === Function ? null : 'foreign';
          }.bind(this));
          probe('escape via platform fn', function () {
            try { return this.__platform.readFile.constructor('return typeof process')(); }
            catch (e) { return null; }
          }.bind(this));
          return JSON.stringify(findings);
        }).call(this)
      `, context, { filename: 'probe-reach.js' });
      return { findings: JSON.parse(raw) };
    }
    case 'evalInRealm': {
      const raw = vm.runInContext(`(function(){ ${op.code} }).call(this)`, context, { filename: 'eval.js' });
      return { value: raw === undefined ? null : String(raw) };
    }
    default:
      throw new Error(`unknown op kind: ${op.kind}`);
  }
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
