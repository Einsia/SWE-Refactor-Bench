// Number formatting, parsing and arithmetic.
//
// Every answer here is fixed by IEEE 754 plus ECMAScript's conversion algorithms,
// on any machine of any byte order. Nothing is `nat-`.
//
// The reason a byte-order port needs this file: QuickJS reads and writes doubles
// through its own conversion code, the bytecode serialiser stores them as
// eight-byte quantities, and its bignum backend keeps them as arrays of machine
// words. A submission that touched any of those in the course of fixing the byte
// order will show up here as a formatting difference long before it shows up as a
// crash -- and a formatting difference is a language change, not a rounding detail.

// ---- the two everyone knows --------------------------------------------------
print("point3: " + (0.1 + 0.2));
print("point3-eq: " + (0.1 + 0.2 === 0.3));
print("e21: " + 1e21);
print("e20: " + 1e20);
print("eminus7: " + 1e-7);
print("eminus6: " + 1e-6);

// ---- the shortest round-trip representation ---------------------------------
print("third: " + (1 / 3));
print("seventh: " + (1 / 7));
print("big: " + 123456789012345678901234567890);
print("max: " + Number.MAX_VALUE);
print("min: " + Number.MIN_VALUE);
print("epsilon: " + Number.EPSILON);
print("maxsafe: " + Number.MAX_SAFE_INTEGER);
print("minsafe: " + Number.MIN_SAFE_INTEGER);
print("neg-zero: " + (-0) + " " + (1 / -0) + " " + Object.is(-0, 0));

// ---- toString in every radix -------------------------------------------------
print("hex-255: " + (255).toString(16));
print("bin-255: " + (255).toString(2));
print("r36: " + (1234567).toString(36));
print("r7: " + (1234567).toString(7));
print("hex-neg: " + (-255).toString(16));
print("hex-frac: " + (0.5).toString(16));
print("hex-frac2: " + (1 / 3).toString(16));
print("r2-frac: " + (0.1).toString(2));
print("r3-large: " + (1e20).toString(3).length);

// ---- toFixed / toPrecision / toExponential -----------------------------------
print("fixed: " + (1.005).toFixed(2) + " " + (2.5).toFixed(0) + " " +
      (1.45).toFixed(1) + " " + (0).toFixed(2));
print("fixed-big: " + (1e21).toFixed(2));
print("fixed-neg: " + (-1.5).toFixed(0));
print("precision: " + (123.456).toPrecision(2) + " " + (0.000123).toPrecision(2) +
      " " + (123456).toPrecision(3));
print("exponential: " + (123.456).toExponential(2) + " " + (0).toExponential(2) +
      " " + (1e-7).toExponential());

// ---- parsing -----------------------------------------------------------------
print("parseInt: " + parseInt("0x1f") + " " + parseInt("08") + " " +
      parseInt("12abc") + " " + parseInt("abc"));
print("parseInt-radix: " + parseInt("ff", 16) + " " + parseInt("11", 2) + " " +
      parseInt("z", 36));
print("parseFloat: " + parseFloat("3.14abc") + " " + parseFloat(".5") + " " +
      parseFloat("1e3") + " " + parseFloat("Infinity"));
print("Number-of: " + Number("") + " " + Number("  12  ") + " " + Number("0b101") +
      " " + Number("0o17") + " " + Number("1_000"));
print("Number-nan: " + Number("12abc") + " " + Number(undefined) + " " +
      Number(null) + " " + Number([]) + " " + Number([7]));

// ---- integer ops that go through a 32-bit conversion ------------------------
print("bit-or: " + (2147483648 | 0) + " " + (4294967296 | 0) + " " + (-1 >>> 0));
print("shift: " + (1 << 31) + " " + (1 << 32) + " " + (-8 >> 1) + " " + (-8 >>> 28));
print("xor: " + (0x12345678 ^ 0x87654321));
print("not: " + (~0) + " " + (~2147483647));
print("shift-count-masked: " + (1 << 33) + " " + (1 >>> 33));
print("tonumber-bits: " + (1.9 | 0) + " " + (-1.9 | 0) + " " + (NaN | 0));

// ---- Math, where the answer is required to be exact -------------------------
print("sqrt: " + Math.sqrt(2) + " " + Math.sqrt(4) + " " + Math.sqrt(-0));
print("floorceil: " + Math.floor(-0.5) + " " + Math.ceil(-0.5) + " " +
      Math.round(-0.5) + " " + Math.round(0.5) + " " + Math.round(2.5));
print("trunc: " + Math.trunc(-1.9) + " " + Math.trunc(1.9));
print("sign: " + Math.sign(-3) + " " + Math.sign(0) + " " + Math.sign(-0));
print("abs: " + Math.abs(-2147483648) + " " + Math.abs(-0));
print("minmax: " + Math.min() + " " + Math.max() + " " + Math.min(0, -0) + " " +
      Math.max(NaN, 1));
print("pow: " + Math.pow(2, 53) + " " + Math.pow(2, -1074) + " " +
      Math.pow(-8, 1 / 3));
print("hypot: " + Math.hypot(3, 4) + " " + Math.hypot(1e300, 1e300));
print("fround: " + Math.fround(1.1) + " " + Math.fround(1e40));
print("clz32-imul: " + Math.clz32(1) + " " + Math.clz32(0) + " " +
      Math.imul(0x7fffffff, 2) + " " + Math.imul(-5, 12));
print("log-exp: " + Math.log(Math.E) + " " + Math.log2(8) + " " + Math.log10(1000) +
      " " + Math.expm1(0) + " " + Math.log1p(0));
print("trig: " + Math.sin(0) + " " + Math.cos(0) + " " + Math.atan2(1, 1) + " " +
      Math.PI + " " + Math.E);
print("cbrt: " + Math.cbrt(27) + " " + Math.cbrt(-27));

// ---- Number.isX --------------------------------------------------------------
print("isInteger: " + Number.isInteger(1) + " " + Number.isInteger(1.5) + " " +
      Number.isInteger(1e100));
print("isSafeInteger: " + Number.isSafeInteger(9007199254740991) + " " +
      Number.isSafeInteger(9007199254740992));
print("isFinite: " + Number.isFinite("1") + " " + isFinite("1") + " " +
      Number.isNaN(NaN) + " " + isNaN("x"));

// ---- BigInt arithmetic and formatting ---------------------------------------
print("bigint-mul: " + (123456789012345678901234567890n * 987654321n));
print("bigint-pow: " + (2n ** 128n));
print("bigint-div: " + (-7n / 2n) + " " + (-7n % 2n) + " " + (7n / -2n));
print("bigint-shift: " + (1n << 100n) + " " + (-1n >> 1n) + " " + (255n >> 4n));
print("bigint-bits: " + (0xff00ff00ff00ff00n & 0x00ff00ff00ff00ffn) + " " +
      (0xf0n | 0x0fn) + " " + (0xffn ^ 0x0fn));
print("bigint-radix: " + (2n ** 64n).toString(16) + " " + (255n).toString(2));
print("bigint-asIntN: " + BigInt.asIntN(8, 255n) + " " + BigInt.asUintN(8, -1n) +
      " " + BigInt.asIntN(64, 2n ** 63n));
print("bigint-compare: " + (1n < 2) + " " + (2n == 2) + " " + (2n === 2) + " " +
      (1n < 1.5));
print("bigint-from-string: " + BigInt("0x10") + " " + BigInt("  42  ") + " " +
      BigInt(true));
print("bigint-negative-pow-throws: " + (function () {
    try { return (2n ** -1n).toString(); } catch (e) { return e.name; } })());
print("bigint-mixed-throws: " + (function () {
    try { return (1n + 1).toString(); } catch (e) { return e.name; } })());
print("bigint-huge: " + (10n ** 100n).toString().length);
print("bigint-huge-tail: " + (10n ** 100n).toString().slice(-10));
print("bigint-factorial-tail: " + (function () {
    var f = 1n; for (var i = 2n; i <= 60n; i++) f *= i; return f.toString(); })());
