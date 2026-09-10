// qjs-args: --bignum
//
// BigFloat and BigDecimal, which is to say libbf.
//
// libbf stores a number as an array of machine words -- limbs -- with a separate
// sign and exponent. Every arithmetic routine walks that array, and the walk is
// written in terms of limb index, not byte address, so it is *supposed* to be
// order-independent. Whether it actually is depends on the port: on a 32-bit
// target a limb is 32 bits and on a 64-bit target it is 64, and the radix
// conversion tables are indexed by limb width. A port that got the word size or
// the endianness definition wrong produces arithmetic that is wrong in the low
// digits and right in the high ones, which is the failure a spot check misses.
//
// So the answers here are long on purpose. `bf-third-100` prints a hundred
// digits; a limb-level error shows up in the last few and nowhere else.
//
// Everything is fixed by the requested precision. Nothing is `nat-`.

// ---- the environment ---------------------------------------------------------
print("env-default: " + BigFloatEnv.prec + "," + BigFloatEnv.expBits);
print("env-limits: " + BigFloatEnv.precMin + "," + BigFloatEnv.precMax + "," +
      BigFloatEnv.expBitsMin + "," + BigFloatEnv.expBitsMax);
print("env-rounding-modes: " + [BigFloatEnv.RNDN, BigFloatEnv.RNDZ,
      BigFloatEnv.RNDD, BigFloatEnv.RNDU, BigFloatEnv.RNDNA,
      BigFloatEnv.RNDA].join(","));
print("env-instance: " + (function () {
    var e = new BigFloatEnv(200); return e.prec + "," + e.expBits + "," +
    e.rndMode + "," + e.subnormal; })());

// ---- literals and conversion -------------------------------------------------
print("literal-l: " + 1.5l + " " + typeof 1.5l);
print("literal-m: " + 1.5m + " " + typeof 1.5m);
print("from-number: " + BigFloat(0.1));
print("from-string: " + BigFloat("0.1"));
print("number-vs-string: " + (BigFloat(0.1) === BigFloat("0.1")));
print("from-bigint: " + BigFloat(2n ** 100n));
print("to-number: " + Number(BigFloat(1) / BigFloat(3)));
print("to-bigint: " + BigInt(BigFloat.trunc(12345.9l)));
print("to-bigint-inexact-throws: " + (function () {
    try { return String(BigInt(12345.9l)); } catch (e) { return e.name; } })());
print("bigint-roundtrip: " + (BigInt(BigFloat(2n ** 90n)) === 2n ** 90n));

// ---- division at several precisions, printed long ---------------------------
print("bf-third-default: " + (1l / 3l));
print("bf-third-100: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toString(); }, 340));
print("bf-seventh-100: " + BigFloatEnv.setPrec(function () {
    return (1l / 7l).toString(); }, 340));
print("bf-third-1000bits: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toFixed(60); }, 1000));
print("bf-prec-24: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toString(); }, 24));
print("bf-prec-min: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toString(); }, BigFloatEnv.precMin));
print("bf-prec-113-eq-f128: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toString(); }, 113));

// ---- radix conversion, both directions -------------------------------------
print("bf-hex-third: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toString(16); }, 200));
print("bf-bin-third: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toString(2); }, 64));
print("bf-r36: " + BigFloatEnv.setPrec(function () {
    return (1l / 7l).toString(36); }, 200));
// Every parseFloat call below passes the radix explicitly. Upstream declares
// BigFloat.parseFloat with arity 1 but reads argv[1] unconditionally, so the
// one-argument form reads whatever the caller left on the stack -- an answer that
// can legitimately differ between two correct builds. It is not a fact about the
// port, so it is not asked.
print("bf-parse-auto: " + BigFloat.parseFloat("0x1.8p3", 0) + "," +
      BigFloat.parseFloat("0x18", 0) + "," + BigFloat.parseFloat("1.5p3", 0));
print("bf-parse-radix: " + BigFloat.parseFloat("ff", 16) + "," +
      BigFloat.parseFloat("101", 2) + "," + BigFloat.parseFloat("zz", 36));
print("bf-parse-long-decimal: " + BigFloatEnv.setPrec(function () {
    return BigFloat.parseFloat(
      "3.14159265358979323846264338327950288419716939937510582097494459230781640628620899862803482534211706798214808651328230664709384460955058223172535940812848111745",
      10).toString(); }, 512));
print("bf-radix-roundtrip: " + BigFloatEnv.setPrec(function () {
    var x = 1l / 7l;
    return (BigFloat.parseFloat(x.toString(16), 16) === x) + "," +
           (BigFloat.parseFloat(x.toString(10), 10) === x) + "," +
           (BigFloat.parseFloat(x.toString(2), 2) === x); }, 200));

// ---- the transcendentals, where the limb loops are longest ------------------
print("bf-sqrt2: " + BigFloatEnv.setPrec(function () {
    return BigFloat.sqrt(2l).toString(); }, 340));
print("bf-exp1: " + BigFloatEnv.setPrec(function () {
    return BigFloat.exp(1l).toString(); }, 340));
print("bf-log2: " + BigFloatEnv.setPrec(function () {
    return BigFloat.log(2l).toString(); }, 340));
print("bf-pi-atan: " + BigFloatEnv.setPrec(function () {
    return (4l * BigFloat.atan(1l)).toString(); }, 340));
print("bf-sin1: " + BigFloatEnv.setPrec(function () {
    return BigFloat.sin(1l).toString(); }, 200));
print("bf-cos1: " + BigFloatEnv.setPrec(function () {
    return BigFloat.cos(1l).toString(); }, 200));
print("bf-tan1: " + BigFloatEnv.setPrec(function () {
    return BigFloat.tan(1l).toString(); }, 200));
print("bf-pow: " + BigFloatEnv.setPrec(function () {
    return BigFloat.pow(2l, 0.5l).toString(); }, 200));
print("bf-pythag: " + BigFloatEnv.setPrec(function () {
    return BigFloat.sqrt(3l * 3l + 4l * 4l).toString(); }, 100));

// ---- rounding, sign, exponent extremes --------------------------------------
print("bf-rounding-modes: " + (function () {
    // The third argument to setPrec is expBits, not the rounding mode; the
    // rounding mode travels on an environment handed to the operation itself.
    var out = [];
    var modes = ["RNDN", "RNDZ", "RNDD", "RNDU", "RNDNA", "RNDA"];
    for (var i = 0; i < modes.length; i++) {
        var e = new BigFloatEnv(8);
        e.rndMode = BigFloatEnv[modes[i]];
        out.push(modes[i] + "=" + BigFloat.div(-1l, 3l, e));
    }
    return out.join(" "); })());
print("bf-add-rounded: " + (function () {
    var e = new BigFloatEnv(8); e.rndMode = BigFloatEnv.RNDZ;
    return BigFloat.add(1l, BigFloat.div(1l, 1000l, e), e) + "," +
           BigFloat.mul(1l / 3l, 3l, e) + "," +
           BigFloat.sqrt(2l, e); })());
print("bf-integer-ops: " + [BigFloat.floor(-1.5l), BigFloat.ceil(-1.5l),
      BigFloat.round(-1.5l), BigFloat.trunc(-1.5l), BigFloat.abs(-1.5l)].join(","));
print("bf-fmod-remainder: " + BigFloat.fmod(7l, 3l) + "," +
      BigFloat.fmod(-7l, 3l) + "," + BigFloat.remainder(7l, 3l) + "," +
      BigFloat.remainder(-7l, 3l));
print("bf-signed-zero: " + (1l / -0l) + "," + (0l === -0l) + "," +
      BigFloat.sign(-0l));
print("bf-specials: " + [1l / 0l, -1l / 0l, 0l / 0l,
      BigFloat.isNaN(0l / 0l), BigFloat.isFinite(1l / 0l)].join(","));
// expBits = 25 is inside expBitsMax on both a 64-bit and a 32-bit limb, so these
// two answers are the same everywhere. At the default 15 they are both Infinity,
// which tests the overflow path and nothing else.
print("bf-huge-exponent: " + BigFloatEnv.setPrec(function () {
    return BigFloat.pow(2l, 1000000l).toString(16); }, 32, 25));
print("bf-tiny-exponent: " + BigFloatEnv.setPrec(function () {
    return BigFloat.pow(2l, -1000000l).toString(16); }, 32, 25));
print("bf-default-expbits-overflows: " + BigFloat.pow(2l, 1000000l) + "," +
      BigFloat.pow(2l, -1000000l));
print("bf-comparisons: " + [(1l < 2l), (1l == 1), (1l === 1l),
      (2l ** 100l > 2n ** 99n)].join(","));

// ---- formatting --------------------------------------------------------------
print("bf-toFixed: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toFixed(40) + " " + (2l / 3l).toFixed(40); }, 200));
print("bf-toPrecision: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toPrecision(40); }, 200));
print("bf-toExponential: " + BigFloatEnv.setPrec(function () {
    return (1l / 300l).toExponential(30); }, 200));
print("bf-toFixed-radix: " + BigFloatEnv.setPrec(function () {
    return (1l / 3l).toFixed(20, BigFloatEnv.RNDN, 16); }, 200));

// ---- BigDecimal, which is limbs in base 10**9 -------------------------------
print("bd-exact-third: " + BigDecimal.div(1m, 3m,
      {maximumSignificantDigits: 100, roundingMode: "half-even"}));
print("bd-exact-seventh: " + BigDecimal.div(1m, 7m,
      {maximumSignificantDigits: 100, roundingMode: "half-even"}));
print("bd-point3-is-exact: " + (0.1m + 0.2m) + "," + ((0.1m + 0.2m) === 0.3m));
print("bd-mul-long: " + (123456789012345678901234567890m *
      987654321098765432109876543210m));
print("bd-pow: " + (2m ** 200m));
print("bd-sqrt2: " + BigDecimal.sqrt(2m, {maximumSignificantDigits: 60,
      roundingMode: "half-even"}));
print("bd-round-modes: " + ["up", "down", "half-up", "half-even", "ceiling",
      "floor"].map(function (m) {
    return m + "=" + BigDecimal.round(2.5m, {maximumFractionDigits: 0,
      roundingMode: m}) + "/" + BigDecimal.round(-2.5m,
      {maximumFractionDigits: 0, roundingMode: m}); }).join(" "));
print("bd-round-mode-unknown: " + (function () {
    try { return String(BigDecimal.round(2.5m, {maximumFractionDigits: 0,
        roundingMode: "half-ceiling"})); } catch (e) { return e.name; } })());
print("bd-mod: " + BigDecimal.mod(7m, 3m) + "," + BigDecimal.mod(-7m, 3m));
print("bd-toString-trailing: " + 1.500m + "," + BigDecimal("1.500") + "," +
      BigDecimal("1e3"));
print("bd-toFixed-toPrecision: " + (1m / 1m).toFixed(5) + "," +
      BigDecimal.div(1m, 3m, {maximumSignificantDigits: 20,
      roundingMode: "half-even"}).toPrecision(10));
print("bd-from-bigint: " + BigDecimal(2n ** 128n));
print("bd-inexact-throws: " + (function () {
    try { return String(1m / 3m); } catch (e) { return e.name; } })());
print("bd-mixed-throws: " + (function () {
    try { return String(1m + 1l); } catch (e) { return e.name; } })());
