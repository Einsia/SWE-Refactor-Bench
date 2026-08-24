/*
 * Runs *inside* the sandbox realm, before any submitted code.
 *
 * Everything the realm offers beyond raw V8 intrinsics is defined here, in
 * plain JavaScript, using only intrinsics of this realm.  Nothing is passed in
 * from the host.
 *
 * That is not fussiness.  A host function handed into a vm realm is a complete
 * escape: `hostFn.constructor("return typeof process")()` reaches the host
 * realm's Function constructor and from there the real global object.  Measured
 * on this Node build, that expression returns "object".  So the rule is
 * absolute -- no host object crosses the boundary, in either direction, except
 * strings and numbers.
 *
 * The realm's observable surface is asserted separately by the audit
 * suite, which compares it against the list published in instruction.md 1.1.
 */
'use strict';

(function bootstrap(g) {
  // ---------------------------------------------------------------- utilities
  var def = function (obj, name, value) {
    Object.defineProperty(obj, name, {
      value: value, writable: true, enumerable: false, configurable: true,
    });
  };

  // ------------------------------------------------------------------- console
  // Collected in-realm; the host reads it back as one JSON string at the end.
  var logLines = [];
  var str = function (a) {
    if (typeof a === 'string') return a;
    try { return JSON.stringify(a); } catch (e) { return String(a); }
  };
  /*
   * printf-style substitution, as both Node and browsers do it.  Stylus's `p()`
   * and `warn()` build their output with `%s`, so a console that merely joined
   * its arguments would report different text here than upstream does -- and the
   * submission would be blamed for the harness's formatting.
   */
  var fmt = function (args) {
    var out = [];
    var i = 0;
    if (typeof args[0] === 'string' && /%[sdifjo%]/.test(args[0])) {
      i = 1;
      out.push(args[0].replace(/%([sdifjo%])/g, function (m, k) {
        if (k === '%') return '%';
        if (i >= args.length) return m;
        var a = args[i++];
        if (k === 's') return str(a);
        if (k === 'd' || k === 'i') return String(parseInt(a, 10));
        if (k === 'f') return String(parseFloat(a));
        try { return JSON.stringify(a); } catch (e) { return String(a); }
      }));
    }
    for (; i < args.length; i++) out.push(str(args[i]));
    return out.join(' ');
  };
  var mkLog = function (level) {
    return function () { logLines.push(level + ' ' + fmt(arguments)); };
  };
  var consoleObj = {};
  ['log', 'info', 'warn', 'error', 'debug', 'trace', 'dir'].forEach(function (m) {
    def(consoleObj, m, mkLog(m));
  });
  def(consoleObj, 'group', mkLog('group'));
  def(consoleObj, 'groupEnd', function () {});
  def(consoleObj, 'table', mkLog('table'));
  def(consoleObj, 'time', function () {});
  def(consoleObj, 'timeEnd', function () {});
  def(consoleObj, 'assert', function (ok) {
    if (!ok) logLines.push('assert ' + fmt([].slice.call(arguments, 1)));
  });
  def(g, 'console', consoleObj);

  // ------------------------------------------------------- queueMicrotask
  def(g, 'queueMicrotask', function (fn) {
    if (typeof fn !== 'function') throw new TypeError('queueMicrotask requires a function');
    Promise.resolve().then(function () { fn(); });
  });

  // ------------------------------------------------------------ UTF-8 codec
  // Hand-rolled so that no host TextEncoder leaks in.  Matches the WHATWG
  // encoding standard for the cases a CSS preprocessor can produce, including
  // surrogate pairs and lone-surrogate replacement.
  function encodeUtf8(str) {
    str = String(str);
    var bytes = [];
    for (var i = 0; i < str.length; i++) {
      var cp = str.charCodeAt(i);
      if (cp >= 0xd800 && cp <= 0xdbff) {
        var next = i + 1 < str.length ? str.charCodeAt(i + 1) : 0;
        if (next >= 0xdc00 && next <= 0xdfff) {
          cp = 0x10000 + ((cp - 0xd800) << 10) + (next - 0xdc00);
          i++;
        } else {
          cp = 0xfffd;
        }
      } else if (cp >= 0xdc00 && cp <= 0xdfff) {
        cp = 0xfffd;
      }
      if (cp < 0x80) {
        bytes.push(cp);
      } else if (cp < 0x800) {
        bytes.push(0xc0 | (cp >> 6), 0x80 | (cp & 0x3f));
      } else if (cp < 0x10000) {
        bytes.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
      } else {
        bytes.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f),
                   0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
      }
    }
    return new Uint8Array(bytes);
  }

  function decodeUtf8(input, opts) {
    var bytes = toBytes(input);
    var ignoreBOM = !!(opts && opts.ignoreBOM);
    var fatal = !!(opts && opts.fatal);
    var start = 0;
    if (!ignoreBOM && bytes.length >= 3
        && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) start = 3;
    var out = '';
    for (var i = start; i < bytes.length;) {
      var b = bytes[i];
      var cp, len;
      if (b < 0x80) { cp = b; len = 1; }
      else if ((b & 0xe0) === 0xc0) { cp = b & 0x1f; len = 2; }
      else if ((b & 0xf0) === 0xe0) { cp = b & 0x0f; len = 3; }
      else if ((b & 0xf8) === 0xf0) { cp = b & 0x07; len = 4; }
      else {
        if (fatal) throw new TypeError('invalid UTF-8');
        out += '�'; i++; continue;
      }
      if (i + len > bytes.length) {
        if (fatal) throw new TypeError('truncated UTF-8');
        out += '�'; break;
      }
      var ok = true;
      for (var k = 1; k < len; k++) {
        if ((bytes[i + k] & 0xc0) !== 0x80) { ok = false; break; }
        cp = (cp << 6) | (bytes[i + k] & 0x3f);
      }
      if (!ok) {
        if (fatal) throw new TypeError('invalid UTF-8 continuation');
        out += '�'; i++; continue;
      }
      i += len;
      if (cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff)) {
        if (fatal) throw new TypeError('invalid code point');
        out += '�'; continue;
      }
      if (cp < 0x10000) {
        out += String.fromCharCode(cp);
      } else {
        cp -= 0x10000;
        out += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff));
      }
    }
    return out;
  }

  function toBytes(input) {
    if (input == null) return new Uint8Array(0);
    if (input instanceof Uint8Array) return input;
    if (input instanceof ArrayBuffer) return new Uint8Array(input);
    if (ArrayBuffer.isView(input)) {
      return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
    }
    throw new TypeError('expected BufferSource');
  }
  def(g, '__toBytes', toBytes);   // removed again at the end of bootstrap

  function TextEncoder() {}
  Object.defineProperty(TextEncoder.prototype, 'encoding', {
    get: function () { return 'utf-8'; }, configurable: true,
  });
  def(TextEncoder.prototype, 'encode', function (str) {
    return encodeUtf8(str === undefined ? '' : str);
  });
  def(TextEncoder.prototype, 'encodeInto', function (str, dest) {
    var src = encodeUtf8(str);
    var n = Math.min(src.length, dest.length);
    for (var i = 0; i < n; i++) dest[i] = src[i];
    return { read: str.length, written: n };
  });
  def(g, 'TextEncoder', TextEncoder);

  function TextDecoder(label, options) {
    var enc = String(label === undefined ? 'utf-8' : label).toLowerCase();
    if (enc !== 'utf-8' && enc !== 'utf8' && enc !== 'unicode-1-1-utf-8') {
      var e = new RangeError('unsupported encoding: ' + enc);
      e.name = 'RangeError';
      throw e;
    }
    this._opts = options || {};
  }
  Object.defineProperty(TextDecoder.prototype, 'encoding', {
    get: function () { return 'utf-8'; }, configurable: true,
  });
  def(TextDecoder.prototype, 'decode', function (input) {
    return decodeUtf8(input, this._opts);
  });
  def(g, 'TextDecoder', TextDecoder);

  // ---------------------------------------------------------------- base64
  var B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  def(g, 'btoa', function (data) {
    var s = String(data), out = '';
    for (var i = 0; i < s.length; i++) {
      if (s.charCodeAt(i) > 0xff) {
        var e = new Error('btoa: character out of Latin-1 range');
        e.name = 'InvalidCharacterError';
        throw e;
      }
    }
    for (var j = 0; j < s.length; j += 3) {
      var c0 = s.charCodeAt(j);
      var c1 = j + 1 < s.length ? s.charCodeAt(j + 1) : NaN;
      var c2 = j + 2 < s.length ? s.charCodeAt(j + 2) : NaN;
      out += B64[c0 >> 2];
      out += B64[((c0 & 3) << 4) | (isNaN(c1) ? 0 : c1 >> 4)];
      out += isNaN(c1) ? '=' : B64[((c1 & 15) << 2) | (isNaN(c2) ? 0 : c2 >> 6)];
      out += isNaN(c2) ? '=' : B64[c2 & 63];
    }
    return out;
  });
  def(g, 'atob', function (data) {
    var s = String(data).replace(/[\t\n\f\r ]/g, '');
    if (s.length % 4 === 1) {
      var e = new Error('atob: invalid length');
      e.name = 'InvalidCharacterError';
      throw e;
    }
    s = s.replace(/=+$/, '');
    var out = '', bits = 0, acc = 0;
    for (var i = 0; i < s.length; i++) {
      var v = B64.indexOf(s[i]);
      if (v < 0) {
        var e2 = new Error('atob: invalid character');
        e2.name = 'InvalidCharacterError';
        throw e2;
      }
      acc = (acc << 6) | v; bits += 6;
      if (bits >= 8) { bits -= 8; out += String.fromCharCode((acc >> bits) & 0xff); }
    }
    return out;
  });

  // ------------------------------------------------------------ structuredClone
  def(g, 'structuredClone', function (value) {
    var seen = new Map();
    var walk = function (v) {
      if (v === null || typeof v !== 'object') {
        if (typeof v === 'function' || typeof v === 'symbol') {
          var e = new Error('could not be cloned'); e.name = 'DataCloneError'; throw e;
        }
        return v;
      }
      if (seen.has(v)) return seen.get(v);
      var out;
      if (v instanceof Date) { out = new Date(v.getTime()); seen.set(v, out); return out; }
      if (v instanceof RegExp) { out = new RegExp(v.source, v.flags); seen.set(v, out); return out; }
      if (v instanceof ArrayBuffer) { out = v.slice(0); seen.set(v, out); return out; }
      if (ArrayBuffer.isView(v)) {
        out = new v.constructor(walk(v.buffer), v.byteOffset, v.length !== undefined ? v.length : undefined);
        seen.set(v, out); return out;
      }
      if (v instanceof Map) {
        out = new Map(); seen.set(v, out);
        v.forEach(function (val, key) { out.set(walk(key), walk(val)); });
        return out;
      }
      if (v instanceof Set) {
        out = new Set(); seen.set(v, out);
        v.forEach(function (val) { out.add(walk(val)); });
        return out;
      }
      if (Array.isArray(v)) {
        out = new Array(v.length); seen.set(v, out);
        for (var i = 0; i < v.length; i++) out[i] = walk(v[i]);
        return out;
      }
      out = {}; seen.set(v, out);
      Object.keys(v).forEach(function (k) { out[k] = walk(v[k]); });
      return out;
    };
    return walk(value);
  });

  // -------------------------------------------------------------- SubtleCrypto
  // SHA-1 and SHA-256 in-realm.  The compiler is expected to hash through
  // platform.sha1, so this exists to make the realm honestly web-shaped rather
  // than because the port needs it.
  function sha1Bytes(bytes) {
    var ml = bytes.length;
    var withPad = ((ml + 8) >> 6 << 6) + 64;
    var w = new Uint8Array(withPad);
    w.set(bytes); w[ml] = 0x80;
    var bitLenHi = Math.floor(ml / 0x20000000);
    var bitLenLo = (ml << 3) >>> 0;
    w[withPad - 8] = (bitLenHi >>> 24) & 0xff; w[withPad - 7] = (bitLenHi >>> 16) & 0xff;
    w[withPad - 6] = (bitLenHi >>> 8) & 0xff;  w[withPad - 5] = bitLenHi & 0xff;
    w[withPad - 4] = (bitLenLo >>> 24) & 0xff; w[withPad - 3] = (bitLenLo >>> 16) & 0xff;
    w[withPad - 2] = (bitLenLo >>> 8) & 0xff;  w[withPad - 1] = bitLenLo & 0xff;

    var h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0];
    var arr = new Int32Array(80);
    for (var off = 0; off < withPad; off += 64) {
      for (var i = 0; i < 16; i++) {
        arr[i] = (w[off + i * 4] << 24) | (w[off + i * 4 + 1] << 16)
               | (w[off + i * 4 + 2] << 8) | w[off + i * 4 + 3];
      }
      for (var j = 16; j < 80; j++) {
        var n = arr[j - 3] ^ arr[j - 8] ^ arr[j - 14] ^ arr[j - 16];
        arr[j] = (n << 1) | (n >>> 31);
      }
      var a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];
      for (var k = 0; k < 80; k++) {
        var f, kk;
        if (k < 20)      { f = (b & c) | (~b & d);          kk = 0x5a827999; }
        else if (k < 40) { f = b ^ c ^ d;                   kk = 0x6ed9eba1; }
        else if (k < 60) { f = (b & c) | (b & d) | (c & d); kk = 0x8f1bbcdc; }
        else             { f = b ^ c ^ d;                   kk = 0xca62c1d6; }
        var t = (((a << 5) | (a >>> 27)) + f + e + kk + arr[k]) | 0;
        e = d; d = c; c = (b << 30) | (b >>> 2); b = a; a = t;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0;
      h[3] = (h[3] + d) | 0; h[4] = (h[4] + e) | 0;
    }
    var out = new Uint8Array(20);
    for (var m = 0; m < 5; m++) {
      out[m * 4] = (h[m] >>> 24) & 0xff; out[m * 4 + 1] = (h[m] >>> 16) & 0xff;
      out[m * 4 + 2] = (h[m] >>> 8) & 0xff; out[m * 4 + 3] = h[m] & 0xff;
    }
    return out;
  }

  var K256 = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2];

  function sha256Bytes(bytes) {
    var ml = bytes.length;
    var withPad = ((ml + 8) >> 6 << 6) + 64;
    var w = new Uint8Array(withPad);
    w.set(bytes); w[ml] = 0x80;
    var bitHi = Math.floor(ml / 0x20000000), bitLo = (ml << 3) >>> 0;
    w[withPad - 8] = (bitHi >>> 24) & 0xff; w[withPad - 7] = (bitHi >>> 16) & 0xff;
    w[withPad - 6] = (bitHi >>> 8) & 0xff;  w[withPad - 5] = bitHi & 0xff;
    w[withPad - 4] = (bitLo >>> 24) & 0xff; w[withPad - 3] = (bitLo >>> 16) & 0xff;
    w[withPad - 2] = (bitLo >>> 8) & 0xff;  w[withPad - 1] = bitLo & 0xff;

    var h = [0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
             0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19];
    var s = new Int32Array(64);
    for (var off = 0; off < withPad; off += 64) {
      for (var i = 0; i < 16; i++) {
        s[i] = (w[off + i * 4] << 24) | (w[off + i * 4 + 1] << 16)
             | (w[off + i * 4 + 2] << 8) | w[off + i * 4 + 3];
      }
      for (var j = 16; j < 64; j++) {
        var x = s[j - 15], y = s[j - 2];
        var s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
        var s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
        s[j] = (s[j - 16] + s0 + s[j - 7] + s1) | 0;
      }
      var a = h[0], b = h[1], c = h[2], d = h[3];
      var e = h[4], f = h[5], gg = h[6], hh = h[7];
      for (var k = 0; k < 64; k++) {
        var S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        var ch = (e & f) ^ (~e & gg);
        var t1 = (hh + S1 + ch + K256[k] + s[k]) | 0;
        var S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        var mj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + mj) | 0;
        hh = gg; gg = f; f = e; e = (d + t1) | 0;
        d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0;
      h[3] = (h[3] + d) | 0; h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
      h[6] = (h[6] + gg) | 0; h[7] = (h[7] + hh) | 0;
    }
    var out = new Uint8Array(32);
    for (var m = 0; m < 8; m++) {
      out[m * 4] = (h[m] >>> 24) & 0xff; out[m * 4 + 1] = (h[m] >>> 16) & 0xff;
      out[m * 4 + 2] = (h[m] >>> 8) & 0xff; out[m * 4 + 3] = h[m] & 0xff;
    }
    return out;
  }
  def(g, '__sha1Bytes', sha1Bytes);   // used by the platform bootstrap

  var subtle = {};
  def(subtle, 'digest', function (algo, data) {
    var name = (typeof algo === 'string' ? algo : (algo && algo.name) || '').toUpperCase();
    var bytes = toBytes(data);
    if (name === 'SHA-1') return Promise.resolve(sha1Bytes(bytes).buffer);
    if (name === 'SHA-256') return Promise.resolve(sha256Bytes(bytes).buffer);
    var err = new Error('Unrecognized algorithm name: ' + name);
    err.name = 'NotSupportedError';
    return Promise.reject(err);
  });

  // Deterministic by design: a fixed-seed xorshift.  A verifier that compares
  // output byte-for-byte cannot afford a real entropy source, and Stylus has no
  // legitimate use for one.
  var rngState = 0x2545f491;
  var cryptoObj = {};
  def(cryptoObj, 'subtle', subtle);
  def(cryptoObj, 'getRandomValues', function (view) {
    if (!ArrayBuffer.isView(view)) throw new TypeError('expected an integer TypedArray');
    var u8 = new Uint8Array(view.buffer, view.byteOffset, view.byteLength);
    for (var i = 0; i < u8.length; i++) {
      rngState ^= rngState << 13; rngState ^= rngState >>> 17; rngState ^= rngState << 5;
      u8[i] = rngState & 0xff;
    }
    return view;
  });
  def(g, 'crypto', cryptoObj);

  // ------------------------------------------------------- URLSearchParams
  function decodeForm(s) {
    return decodeURIComponent(String(s).replace(/\+/g, ' '));
  }
  function encodeForm(s) {
    return encodeURIComponent(String(s)).replace(/%20/g, '+').replace(/[!'()~]/g, function (ch) {
      return '%' + ch.charCodeAt(0).toString(16).toUpperCase();
    });
  }
  function URLSearchParams(init) {
    var pairs = [];
    if (typeof init === 'string') {
      var q = init.charAt(0) === '?' ? init.slice(1) : init;
      if (q) {
        q.split('&').forEach(function (part) {
          if (!part) return;
          var eq = part.indexOf('=');
          if (eq < 0) pairs.push([decodeForm(part), '']);
          else pairs.push([decodeForm(part.slice(0, eq)), decodeForm(part.slice(eq + 1))]);
        });
      }
    } else if (Array.isArray(init)) {
      init.forEach(function (p) { pairs.push([String(p[0]), String(p[1])]); });
    } else if (init && typeof init === 'object') {
      Object.keys(init).forEach(function (k) { pairs.push([k, String(init[k])]); });
    }
    def(this, '_p', pairs);
  }
  def(URLSearchParams.prototype, 'append', function (k, v) { this._p.push([String(k), String(v)]); });
  def(URLSearchParams.prototype, 'get', function (k) {
    k = String(k);
    for (var i = 0; i < this._p.length; i++) if (this._p[i][0] === k) return this._p[i][1];
    return null;
  });
  def(URLSearchParams.prototype, 'getAll', function (k) {
    k = String(k);
    return this._p.filter(function (p) { return p[0] === k; }).map(function (p) { return p[1]; });
  });
  def(URLSearchParams.prototype, 'has', function (k) { return this.get(String(k)) !== null; });
  def(URLSearchParams.prototype, 'set', function (k, v) {
    k = String(k); v = String(v);
    var done = false;
    var next = [];
    this._p.forEach(function (p) {
      if (p[0] !== k) { next.push(p); return; }
      if (!done) { next.push([k, v]); done = true; }
    });
    if (!done) next.push([k, v]);
    def(this, '_p', next);
  });
  def(URLSearchParams.prototype, 'delete', function (k) {
    k = String(k);
    def(this, '_p', this._p.filter(function (p) { return p[0] !== k; }));
  });
  def(URLSearchParams.prototype, 'sort', function () {
    this._p.sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0; });
  });
  def(URLSearchParams.prototype, 'forEach', function (fn, thisArg) {
    var self = this;
    this._p.slice().forEach(function (p) { fn.call(thisArg, p[1], p[0], self); });
  });
  def(URLSearchParams.prototype, 'keys', function () {
    return this._p.map(function (p) { return p[0]; })[Symbol.iterator]();
  });
  def(URLSearchParams.prototype, 'values', function () {
    return this._p.map(function (p) { return p[1]; })[Symbol.iterator]();
  });
  def(URLSearchParams.prototype, 'entries', function () {
    return this._p.map(function (p) { return [p[0], p[1]]; })[Symbol.iterator]();
  });
  def(URLSearchParams.prototype, Symbol.iterator, URLSearchParams.prototype.entries);
  def(URLSearchParams.prototype, 'toString', function () {
    return this._p.map(function (p) { return encodeForm(p[0]) + '=' + encodeForm(p[1]); }).join('&');
  });
  Object.defineProperty(URLSearchParams.prototype, 'size', {
    get: function () { return this._p.length; }, configurable: true,
  });
  def(g, 'URLSearchParams', URLSearchParams);

  // --------------------------------------------------- host-readable channel
  // Primitives only.  The host calls these and receives strings.
  def(g, '__drainConsole', function () {
    var s = JSON.stringify(logLines); logLines.length = 0; return s;
  });

  def(g, 'globalThis', g);

  // Withdraw what the realm should not offer.  `vm`'s codeGeneration.wasm flag
  // stops compilation but leaves the namespace object in place; instruction.md
  // promises no WebAssembly, so the promise is made true here.
  delete g.WebAssembly;

  // Scratch helper used above; the platform bootstrap has its own copy of what
  // it needs, so this must not survive into submitted code.
  delete g.__toBytes;
}(this));
