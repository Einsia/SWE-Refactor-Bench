/*
 * Runs inside the sandbox realm, after realm-bootstrap.js.
 *
 * Builds the capability object described in instruction.md 1.2 out of a JSON
 * string the host assigned to __VFS_JSON__ and __PLATFORM_JSON__.  Strings are
 * the only things that cross the realm boundary, so the resulting platform --
 * including every function on it -- belongs to this realm.
 *
 * Also records an access log.  The log is how the verifier tells a compiler
 * that genuinely routes IO through the capability object from one that found
 * another way to the bytes: if the CSS is right but the log is empty, the bytes
 * came from somewhere they should not have.
 */
'use strict';

(function installPlatform(g) {
  var def = function (obj, name, value) {
    Object.defineProperty(obj, name, {
      value: value, writable: true, enumerable: true, configurable: true,
    });
  };

  var spec = JSON.parse(g.__PLATFORM_JSON__);
  var raw = JSON.parse(g.__VFS_JSON__);

  // path -> Uint8Array, decoded once up front so a read is pure computation.
  var files = new Map();
  Object.keys(raw).forEach(function (p) {
    var bin = atob(raw[p]);
    var u8 = new Uint8Array(bin.length);
    for (var i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i) & 0xff;
    files.set(p, u8);
  });

  // Directory index derived from the file list; no separate directory records.
  var dirs = new Map();
  var noteDir = function (d) { if (!dirs.has(d)) dirs.set(d, new Set()); };
  noteDir('/');
  files.forEach(function (_v, p) {
    var parts = p.split('/');
    var name = parts.pop();
    var cur = '';
    for (var i = 1; i < parts.length; i++) {
      var parent = cur === '' ? '/' : cur;
      cur = cur + '/' + parts[i];
      noteDir(cur);
      dirs.get(parent).add(parts[i]);
    }
    noteDir(cur === '' ? '/' : cur);
    dirs.get(cur === '' ? '/' : cur).add(name);
  });

  var log = [];
  var record = function (op, path, outcome) { log.push(op + ' ' + outcome + ' ' + path); };

  var mkError = function (code, op, path) {
    var e = new Error(code + ': ' + op + " '" + path + "'");
    e.code = code;
    e.path = path;
    return e;
  };

  var readFileSyncImpl = function (path) {
    path = String(path);
    if (!files.has(path)) { record('readFile', path, 'ENOENT'); throw mkError('ENOENT', 'readFile', path); }
    record('readFile', path, 'ok');
    var src = files.get(path);
    return src.slice(0);            // a fresh copy: callers must not mutate the VFS
  };
  var readDirSyncImpl = function (path) {
    path = String(path).replace(/\/+$/, '') || '/';
    if (!dirs.has(path)) { record('readDir', path, 'ENOENT'); throw mkError('ENOENT', 'readDir', path); }
    record('readDir', path, 'ok');
    var names = [];
    dirs.get(path).forEach(function (n) { names.push(n); });
    // Deliberately not sorted: instruction.md 1.2 says unordered, and a port
    // that depends on host ordering should fail here rather than in production.
    if (spec.readDirOrder === 'reverse') names.reverse();
    return names;
  };
  var statSyncImpl = function (path) {
    var p = String(path);
    var trimmed = p.replace(/\/+$/, '') || '/';
    if (files.has(p)) {
      record('stat', p, 'file');
      return { isFile: true, isDirectory: false, size: files.get(p).length };
    }
    if (dirs.has(trimmed)) {
      record('stat', p, 'dir');
      return { isFile: false, isDirectory: true, size: 0 };
    }
    record('stat', p, 'null');
    return null;                    // absence is a value, not a throw
  };
  // Captured before the global is withdrawn, so submitted code cannot reach the
  // digest routine except through platform.sha1.
  var sha1Raw = g.__sha1Bytes;
  var sha1Impl = function (bytes) {
    var u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
    var d = sha1Raw(u8);
    var hex = '';
    for (var i = 0; i < d.length; i++) hex += (d[i] < 16 ? '0' : '') + d[i].toString(16);
    record('sha1', String(u8.length) + 'B', 'ok');
    return hex;
  };

  var platform = {};
  if (spec.sync) {
    def(platform, 'sync', true);
    def(platform, 'readFile', readFileSyncImpl);
    def(platform, 'readDir', readDirSyncImpl);
    def(platform, 'stat', statSyncImpl);
    def(platform, 'sha1', sha1Impl);
  } else {
    def(platform, 'sync', false);
    // Resolved on a later microtask turn, so a port that assumes the value is
    // already there fails here instead of intermittently.
    var async1 = function (fn) {
      return function (arg) {
        return Promise.resolve().then(function () {
          return Promise.resolve().then(function () { return fn(arg); });
        });
      };
    };
    def(platform, 'readFile', async1(readFileSyncImpl));
    def(platform, 'readDir', async1(readDirSyncImpl));
    def(platform, 'stat', async1(statSyncImpl));
    def(platform, 'sha1', async1(sha1Impl));
  }
  def(platform, 'cwd', spec.cwd);
  def(platform, 'runtimeRoot', spec.runtimeRoot);

  def(g, '__platform', platform);
  def(g, '__drainAccessLog', function () {
    var s = JSON.stringify(log); log.length = 0; return s;
  });
  def(g, '__vfsPaths', function () {
    var out = []; files.forEach(function (_v, p) { out.push(p); }); return JSON.stringify(out.sort());
  });

  delete g.__VFS_JSON__;
  delete g.__PLATFORM_JSON__;
  delete g.__sha1Bytes;
}(this));
