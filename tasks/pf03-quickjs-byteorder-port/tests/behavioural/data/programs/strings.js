// Strings, encoding, and the regular-expression engine.
//
// QuickJS stores a string as either 8-bit or 16-bit units and switches
// representation as needed, and its regexp engine compiles to its own bytecode with
// multi-byte operands. Both are places where a byte-order change lands on something
// that is not a typed array, so a submission that swapped too broadly shows up here
// as a wrong character rather than as a wrong number.
//
// Every answer is fixed by ECMAScript. Nothing is `nat-`.

// ---- representation boundaries ----------------------------------------------
print("ascii: " + "hello".length + " " + "hello".charCodeAt(0));
print("latin1: " + "café".length + " " + "café".charCodeAt(3));
print("bmp: " + "中文".length + " " + "中文".charCodeAt(0).toString(16));
print("astral-length: " + "😀".length);
print("astral-codePointAt: " + "😀".codePointAt(0).toString(16));
print("astral-charCodeAt: " + "😀".charCodeAt(0).toString(16) + "," +
      "😀".charCodeAt(1).toString(16));
print("fromCodePoint: " + String.fromCodePoint(0x1f600).length);
print("fromCharCode: " + String.fromCharCode(0x4e2d, 0x6587));
print("mixed-concat: " + ("a" + "中" + "b").length);
print("lone-surrogate: " + "\ud800".charCodeAt(0).toString(16));

// ---- iteration is by code point, indexing by unit --------------------------
var it = [];
for (var ch of "a中😀") it.push(ch.length);
print("iter-unit-lengths: " + it.join(","));
print("index-vs-iter: " + "a😀"[1].charCodeAt(0).toString(16));

// ---- the methods -------------------------------------------------------------
print("slice: " + "abcdef".slice(1, -1) + " " + "abcdef".slice(-2));
print("substring: " + "abcdef".substring(4, 1));
print("split-empty: " + "abc".split("").join("|"));
print("split-limit: " + "a,b,c".split(",", 2).join("|"));
print("split-regexp: " + "a1b22c".split(/\d+/).join("|"));
print("split-captures: " + "a1b".split(/(\d)/).join("|"));
print("repeat: " + "ab".repeat(3));
print("pad: " + "5".padStart(3, "0") + " " + "5".padEnd(3, "xy"));
print("trim: [" + "  a\t\n ".trim() + "] [" + " a ".trimStart() + "]");
print("case: " + "ß".toUpperCase() + " " + "I".toLowerCase() + " " +
      "İ".toLowerCase().length);
print("normalize: " + "é".normalize("NFC").length + " " +
      "é".normalize("NFD").length);
// Only the sign of localeCompare is specified. Upstream returns libc memcmp's
// raw magnitude, and that magnitude is implementation-defined: glibc on s390x
// answers -2 where glibc on x86-64 answers -1, in a correct build of either.
// Asking for the magnitude would fail a correct big-endian port over its C
// library, so the question is the sign.
print("localeCompare: " + Math.sign("a".localeCompare("b")) + " " +
      Math.sign("b".localeCompare("a")) + " " +
      Math.sign("a".localeCompare("a")) + " " +
      Math.sign("ab".localeCompare("a")));
print("codePointOrder: " + ("ａ" < "ｂ") + " " + ("Z" < "a"));
print("index-access: " + "abc"[2] + " " + "abc"[5] + " " + "abc".charAt(5) +
      "|" + "abc".charAt(-1));
print("includes: " + "abcdef".includes("cd") + " " + "abc".startsWith("b", 1) +
      " " + "abc".endsWith("b", 2));
print("indexOf: " + "abcabc".indexOf("c") + " " + "abcabc".lastIndexOf("c"));
print("concat-many: " + "".concat("a", 1, null, undefined, true));
print("raw: " + String.raw({raw: ["a", "b"]}, 1));

// ---- replace, including the substitution patterns ---------------------------
print("replace-string: " + "aXbXc".replace("X", "-"));
print("replace-all-regexp: " + "aXbXc".replace(/X/g, "-"));
print("replace-dollar: " + "abc".replace(/(b)/, "[$1|$&|$`|$']"));
print("replace-dollar-dollar: " + "abc".replace("b", "$$"));
print("replace-function: " + "a1b2".replace(/\d/g, function (m, off) {
    return "<" + m + "@" + off + ">"; }));
print("replace-named: " + "2020-11-08".replace(
    /(?<y>\d{4})-(?<m>\d{2})-(?<d>\d{2})/, "$<d>/$<m>/$<y>"));

// ---- the regexp engine -------------------------------------------------------
print("re-exec: " + JSON.stringify(/(\d+)-(\d+)/.exec("x 12-34 y")));
print("re-index: " + /(\d+)/.exec("ab123").index);
print("re-lastIndex: " + (function () {
    var r = /a/g; r.exec("aab"); return r.lastIndex; })());
print("re-global-match: " + "a1b22c333".match(/\d+/g).join("|"));
print("re-matchAll: " + (function () {
    var out = [];
    for (var m of "a1b22".matchAll(/(\d+)/g)) out.push(m[1] + "@" + m.index);
    return out.join("|"); })());
print("re-groups: " + JSON.stringify(
    /(?<a>x)(?<b>y)/.exec("xy").groups));
print("re-backref: " + /(\w)\1/.test("aa") + " " + /(\w)\1/.test("ab"));
print("re-lookahead: " + /a(?=b)/.test("ab") + " " + /a(?!b)/.test("ab"));
print("re-lookbehind: " + /(?<=a)b/.test("ab") + " " + /(?<!a)b/.test("ab"));
print("re-unicode-prop: " + /\p{Letter}/u.test("中") + " " +
      /\p{Nd}/u.test("5"));
print("re-unicode-astral: " + /^.$/u.test("😀") + " " +
      /^.$/.test("😀"));
print("re-sticky: " + (function () {
    var r = /a/y; r.lastIndex = 1; return r.test("ba"); })());
print("re-multiline: " + /^b/m.test("a\nb") + " " + /^b/.test("a\nb"));
print("re-dotall: " + /a.b/s.test("a\nb") + " " + /a.b/.test("a\nb"));
print("re-ignorecase-unicode: " + /é/i.test("É"));
print("re-class-range: " + /[一-鿿]/.test("中"));
print("re-quantifier: " + /^a{2,3}$/.test("aa") + " " + /^a{2,3}$/.test("aaaa"));
print("re-alternation-order: " + "abc".replace(/b|bc/, "-"));
print("re-source-flags: " + /ab/gi.source + " " + /ab/gi.flags);
print("re-escape: " + /\x41B\101/.test("AB" + "\x41"));
print("re-empty-match-advance: " + "abc".replace(/(?:)/g, "-"));

// ---- encodeURI / escape, byte-oriented conversions --------------------------
print("encodeURIComponent: " + encodeURIComponent("a b中/?"));
print("encodeURI: " + encodeURI("a b中/?"));
print("decodeURIComponent: " + decodeURIComponent("%E4%B8%AD"));
print("escape: " + escape("a b中"));
print("unescape: " + unescape("%u4e2d"));
print("encodeURI-astral: " + encodeURIComponent("😀"));
print("decode-invalid: " + (function () {
    try { return decodeURIComponent("%E4%B8"); } catch (e) { return e.name; } })());

// ---- JSON, which is where a string round-trips through bytes ----------------
print("json-string: " + JSON.stringify("a\"b\\c\nd\tef"));
print("json-unicode: " + JSON.stringify("中😀"));
print("json-lone-surrogate: " + JSON.stringify("\ud800"));
print("json-parse: " + JSON.stringify(JSON.parse('{"b":1,"a":[2,{"c":null}]}')));
print("json-number: " + JSON.stringify({a: 1e21, b: 0.1, c: -0, d: 1e-7}));
print("json-nested: " + JSON.stringify({a: {b: {c: [1, [2, [3]]]}}}));
print("json-indent: " + JSON.stringify({a: 1, b: [2]}, null, 2).length);
print("json-replacer: " + JSON.stringify({a: 1, b: 2}, ["a"]));
print("json-reviver: " + JSON.parse('{"a":1}', function (k, v) {
    return typeof v === "number" ? v * 2 : v; }).a);
print("json-toJSON: " + JSON.stringify({toJSON: function () { return "x"; }}));
print("json-parse-error: " + (function () {
    try { JSON.parse("{"); } catch (e) { return e.name; } })());
