// Typed arrays, everything about them that is not the byte order of their storage.
//
// Construction, conversion, clamping, the coercion rules at the edges of each
// width, subarray and slice arithmetic, sort, and the element-type conversions that
// go through a float. ECMAScript fixes all of it exactly, so every key here is a
// candidate invariant and none is `nat-`.
//
// Its job is to catch over-reach. A submission that decided the safe repair was to
// byte-swap on every typed-array store has changed what these answer -- on x86-64 as
// well, which is what makes `cross-consistency` able to see it -- and a submission
// that got the storage order right without breaking any of this is the one that
// understood the problem.

function j(a) { return Array.prototype.join.call(a, ","); }

// ---- construction ------------------------------------------------------------
print("u8-from-array: " + j(new Uint8Array([1, 2, 3, 250])));
print("u8-from-length: " + j(new Uint8Array(4)));
print("u8-from-iter: " + j(new Uint8Array([0, 1, 2].values ? [0, 1, 2] : [0, 1, 2])));
print("u8-of: " + j(Uint8Array.of(9, 8, 7)));
print("u8-from-mapped: " + j(Uint8Array.from([1, 2, 3], function (x) { return x * 3; })));
print("u8-from-string-keys: " + j(Uint8Array.from({length: 3, 0: 5, 1: 6, 2: 7})));

// ---- the coercion rules at each width ---------------------------------------
print("u8-wrap: " + j(new Uint8Array([255, 256, 257, -1, -2])));
print("i8-wrap: " + j(new Int8Array([127, 128, 129, -128, -129])));
print("u16-wrap: " + j(new Uint16Array([65535, 65536, 65537, -1])));
print("i16-wrap: " + j(new Int16Array([32767, 32768, -32768, -32769])));
print("u32-wrap: " + j(new Uint32Array([4294967295, 4294967296, -1])));
print("i32-wrap: " + j(new Int32Array([2147483647, 2147483648, -2147483648])));

// ---- Uint8ClampedArray, whose rounding is its own -----------------------------
print("clamped: " + j(new Uint8ClampedArray([-1, 0, 255, 256, 1.4, 1.5, 2.5, 3.5])));
print("clamped-nan: " + j(new Uint8ClampedArray([NaN, Infinity, -Infinity])));

// ---- float conversion --------------------------------------------------------
print("f32-round: " + j(new Float32Array([0.1, 1 / 3, 1e40, -1e40])));
print("f64-exact: " + j(new Float64Array([0.1, 1 / 3, 1e308])));
print("f32-nan: " + j(new Float32Array([NaN, Infinity, -Infinity, -0])));
print("f32-negzero-is-zero: " + (new Float32Array([-0])[0] === 0));
print("f32-negzero-sign: " + (1 / new Float32Array([-0])[0]));

// ---- integer coercion of non-numbers ----------------------------------------
print("i32-from-strings: " + j(new Int32Array(["7", "-7", "0x10", "1e3"])));
print("i32-from-oddities: " + j(new Int32Array([null, true, false, 1.9, -1.9])));
print("i32-from-nan: " + j(new Int32Array([NaN, Infinity, -Infinity])));

// ---- BigInt widths -----------------------------------------------------------
print("bi64-wrap: " + j(new BigInt64Array([
    9223372036854775807n, -9223372036854775808n, 0n, -1n])));
print("bu64-wrap: " + j(new BigUint64Array([
    18446744073709551615n, 0n, 1n])));
print("bu64-from-negative: " + new BigUint64Array([-1n])[0]);

// ---- views over one buffer, and the arithmetic of offsets -------------------
var ab = new ArrayBuffer(24);
var whole = new Uint8Array(ab);
for (var i = 0; i < 24; i++) whole[i] = i;
var v = new Uint32Array(ab, 8, 3);
print("view-length: " + v.length);
print("view-byteOffset: " + v.byteOffset);
print("view-byteLength: " + v.byteLength);
print("view-BYTES_PER_ELEMENT: " + Uint32Array.BYTES_PER_ELEMENT + "," +
      Float64Array.BYTES_PER_ELEMENT + "," + BigInt64Array.BYTES_PER_ELEMENT);

var sub = v.subarray(1);
print("subarray-length: " + sub.length);
print("subarray-byteOffset: " + sub.byteOffset);
print("subarray-shares: " + (sub.buffer === v.buffer));

// slice copies; the *values* are the invariant, not the bytes
var sl = new Uint32Array([1, 2, 3, 4, 5]).slice(1, 4);
print("slice-values: " + j(sl));
print("slice-length: " + sl.length);

// ---- set, copyWithin, fill --------------------------------------------------
var t = new Uint8Array(8);
t.set([1, 2, 3], 2);
print("set-at-offset: " + j(t));
t.copyWithin(0, 2, 5);
print("copyWithin: " + j(t));
t.fill(9, 4, 6);
print("fill-range: " + j(t));
var t2 = new Uint32Array([5, 6, 7]);
var t3 = new Uint8Array(3);
t3.set(t2);
print("set-across-types: " + j(t3));

// ---- the read/write methods --------------------------------------------------
var r = new Int16Array([5, -5, 300, -300]);
print("indexOf: " + r.indexOf(-5) + "," + r.indexOf(999));
print("includes: " + r.includes(300) + "," + r.includes(301));
print("join: " + r.join("|"));
print("reverse: " + j(Int16Array.prototype.reverse.call(new Int16Array([1, 2, 3]))));
print("sort-signed: " + j(new Int16Array([3, -1, 2, -5]).sort()));
print("sort-float: " + j(new Float64Array([3, NaN, -0, 0, -1]).sort()));
print("sort-comparator: " + j(new Uint8Array([1, 5, 3]).sort(function (a, b) {
    return b - a; })));
print("reduce: " + new Uint8Array([1, 2, 3, 4]).reduce(function (a, b) {
    return a + b; }, 0));
print("map: " + j(new Uint8Array([1, 2, 3]).map(function (x) { return x + 1; })));
print("filter: " + j(new Uint8Array([1, 2, 3, 4]).filter(function (x) {
    return x % 2 === 0; })));
print("find: " + new Uint8Array([1, 2, 3]).find(function (x) { return x > 1; }));
print("every-some: " + new Uint8Array([2, 4]).every(function (x) {
    return x % 2 === 0; }) + "," + new Uint8Array([1, 3]).some(function (x) {
    return x % 2 === 0; }));

// ---- toString / JSON --------------------------------------------------------
print("toString: " + new Uint16Array([1, 2, 3]).toString());
print("json: " + JSON.stringify(new Uint16Array([1, 2, 3])));
print("json-f64: " + JSON.stringify(new Float64Array([1.5, -0.25])));
print("spread: " + j([].concat(Array.prototype.slice.call(
    new Uint8Array([1, 2, 3])))));

// ---- identity and errors -----------------------------------------------------
print("ctor-name: " + new Uint8Array(0).constructor.name);
print("tag: " + Object.prototype.toString.call(new Float64Array(1)));
print("is-view: " + ArrayBuffer.isView(new Uint8Array(1)) + "," +
      ArrayBuffer.isView(new ArrayBuffer(1)));
try { new Uint32Array(new ArrayBuffer(7)); print("misaligned-len: no throw"); }
catch (e) { print("misaligned-len: " + e.name); }
try { new Uint32Array(new ArrayBuffer(8), 2, 1); print("misaligned-off: no throw"); }
catch (e) { print("misaligned-off: " + e.name); }
try { new Uint8Array(new ArrayBuffer(4), 8); print("oob-off: no throw"); }
catch (e) { print("oob-off: " + e.name); }
try { new BigInt64Array([1]); print("bigint-from-number: no throw"); }
catch (e) { print("bigint-from-number: " + e.name); }

// ---- ArrayBuffer itself ------------------------------------------------------
var buf = new ArrayBuffer(8);
print("ab-byteLength: " + buf.byteLength);
var copy = buf.slice(2, 6);
print("ab-slice-byteLength: " + copy.byteLength);
print("ab-slice-is-copy: " + (copy !== buf));
