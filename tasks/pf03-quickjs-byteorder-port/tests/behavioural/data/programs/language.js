// The language itself: control flow, closures, classes, generators, destructuring,
// property order, prototypes, exceptions.
//
// None of this has anything to do with byte order, which is exactly why it is here.
// It is the control that says a submission fixed the interpreter rather than
// perturbed it -- a change that reaches the object layout, the atom table or the
// opcode dispatch shows up as a wrong answer here on *every* target, including
// x86-64, and `cross-consistency` is the module that notices that pattern.
//
// Everything is fixed by ECMAScript. Nothing is `nat-`.

// ---- closures and scope ------------------------------------------------------
print("closure-counter: " + (function () {
    var n = 0; return function () { return ++n; }; })()() );
print("closure-loop-var: " + (function () {
    var fs = []; for (var i = 0; i < 3; i++) fs.push(function () { return i; });
    return fs.map(function (f) { return f(); }).join(","); })());
print("closure-loop-let: " + (function () {
    var fs = []; for (let i = 0; i < 3; i++) fs.push(function () { return i; });
    return fs.map(function (f) { return f(); }).join(","); })());
print("tdz: " + (function () {
    try { x; let x = 1; return "no throw"; } catch (e) { return e.name; } })());
print("hoisting: " + (function () { return typeof f; function f() {} })());
print("arguments-alias: " + (function (a) { arguments[0] = 9; return a; })(1));
print("strict-arguments: " + (function (a) { "use strict";
    arguments[0] = 9; return a; })(1));
print("this-undefined: " + (function () { "use strict"; return typeof this; })());
print("arrow-this: " + (function () {
    return (() => typeof this)(); }).call(undefined));

// ---- property order, which is specified ------------------------------------
var o = {}; o.b = 1; o[2] = 2; o.a = 3; o[1] = 4; o["-1"] = 5;
print("key-order: " + Object.keys(o).join(","));
print("key-order-json: " + JSON.stringify(o));
print("for-in-order: " + (function () {
    var out = []; for (var k in o) out.push(k); return out.join(","); })());
print("delete-reinsert: " + (function () {
    var p = {a: 1, b: 2}; delete p.a; p.a = 3; return Object.keys(p).join(","); })());
print("proto-chain-for-in: " + (function () {
    var base = {z: 1}; var d = Object.create(base); d.a = 2;
    var out = []; for (var k in d) out.push(k); return out.join(","); })());

// ---- destructuring -----------------------------------------------------------
print("destr-array: " + (function () {
    var [a, , b = 9, ...r] = [1, 2, undefined, 4, 5];
    return [a, b, r.join("+")].join(","); })());
print("destr-object: " + (function () {
    var {a, b: {c} = {c: 7}, ...rest} = {a: 1, d: 2, e: 3};
    return [a, c, Object.keys(rest).join("+")].join(","); })());
print("destr-swap: " + (function () {
    var a = 1, b = 2; [a, b] = [b, a]; return a + "," + b; })());
print("destr-default-order: " + (function () {
    var seen = []; var f = function (n) { seen.push(n); return n; };
    var [x = f(1), y = f(2)] = [undefined, 5];
    return seen.join(",") + "|" + x + "," + y; })());
print("spread-call: " + Math.max(...[1, 5, 3]));
print("spread-object: " + JSON.stringify({...{a: 1}, ...{b: 2}, a: 3}));

// ---- classes -----------------------------------------------------------------
class Base {
    constructor(v) { this.v = v; }
    get doubled() { return this.v * 2; }
    static make(v) { return new this(v); }
    toString() { return "Base(" + this.v + ")"; }
}
class Derived extends Base {
    constructor(v) { super(v + 1); }
    get doubled() { return super.doubled + 100; }
}
print("class-basic: " + new Base(2).doubled);
print("class-super: " + new Derived(2).doubled);
print("class-static-this: " + (Derived.make(1) instanceof Derived));
print("class-tostring: " + String(new Base(5)));
print("class-name: " + Base.name + "," + Derived.name);
print("class-proto: " + (Object.getPrototypeOf(Derived) === Base));
print("class-not-callable: " + (function () {
    try { Base(1); return "called"; } catch (e) { return e.name; } })());
print("class-field-enumerable: " + Object.keys(new Base(1)).join(","));
print("getter-descriptor: " + JSON.stringify(
    Object.keys(Object.getOwnPropertyDescriptor(Base.prototype, "doubled"))));

// ---- generators and iterators ------------------------------------------------
function* gen() { var x = yield 1; yield x * 2; return 99; }
print("generator: " + (function () {
    var g = gen(), out = [];
    out.push(JSON.stringify(g.next()));
    out.push(JSON.stringify(g.next(5)));
    out.push(JSON.stringify(g.next()));
    out.push(JSON.stringify(g.next()));
    return out.join("|"); })());
print("generator-delegate: " + (function () {
    function* inner() { yield 1; yield 2; return 3; }
    function* outer() { var r = yield* inner(); yield r; }
    return Array.from(outer()).join(","); })());
print("generator-throw: " + (function () {
    function* g2() { try { yield 1; } catch (e) { yield "caught:" + e; } }
    var g = g2(); g.next(); return JSON.stringify(g.throw("x")); })());
print("iterator-protocol: " + (function () {
    var obj = {}; obj[Symbol.iterator] = function () {
        var i = 0; return {next: function () {
            return i < 3 ? {value: i++, done: false} : {value: undefined, done: true};
        }}; };
    return [...obj].join(","); })());
print("for-of-break-return: " + (function () {
    var closed = false;
    var obj = {}; obj[Symbol.iterator] = function () {
        return {next: function () { return {value: 1, done: false}; },
                return: function () { closed = true; return {done: true}; }}; };
    for (var v of obj) break;
    return closed; })());

// ---- exceptions --------------------------------------------------------------
print("try-finally-override: " + (function () {
    try { return "try"; } finally { /* no return */ } })());
print("try-finally-return: " + (function () {
    try { return "try"; } finally { return "finally"; } })());
print("error-types: " + [
    (function () { try { null.x; } catch (e) { return e.name; } })(),
    (function () { try { undefinedName; } catch (e) { return e.name; } })(),
    (function () { try { (1)(); } catch (e) { return e.name; } })(),
    (function () { try { new Array(-1); } catch (e) { return e.name; } })(),
    (function () { try { JSON.parse("~"); } catch (e) { return e.name; } })()
].join(","));
print("error-instanceof: " + (new TypeError("x") instanceof Error));
print("error-message: " + new RangeError("boom").message);
print("throw-non-error: " + (function () {
    try { throw 42; } catch (e) { return typeof e + ":" + e; } })());
print("stack-is-string: " + (typeof new Error("x").stack));

// ---- operators and coercion --------------------------------------------------
print("plus-coercion: " + (1 + "2") + " " + ("3" - 1) + " " + ([] + {}) + " " +
      (+[]) + " " + (+{}));
print("equality: " + (null == undefined) + " " + (null === undefined) + " " +
      (NaN == NaN) + " " + ("" == 0) + " " + ("0" == false));
print("relational: " + ("10" < "9") + " " + (10 < 9) + " " + (null >= 0) + " " +
      (undefined < 1));
print("typeof: " + [typeof undefined, typeof null, typeof 1n, typeof Symbol(),
      typeof (() => 1), typeof {}].join(","));
print("in-and-delete: " + ("a" in {a: 1}) + " " + (0 in [1]) + " " +
      delete ({a: 1}).a);
print("optional-chain: " + (function () {
    var x = null; return (x?.y?.z) + "|" + (x?.[0]) + "|" + (x?.f?.()); })());
print("nullish: " + (null ?? "d") + " " + (0 ?? "d") + " " + (undefined ?? "d"));
print("comma-and-void: " + ((1, 2)) + " " + void 0);
print("exponent: " + (2 ** 10) + " " + ((-2) ** 2));
print("logical-assign: " + (function () {
    var a = null; a ??= 5; var b = 1; b ||= 9; var c = 1; c &&= 7;
    return [a, b, c].join(","); })());

// ---- objects, symbols, proxies ----------------------------------------------
print("symbol-desc: " + String(Symbol("tag")) + " " + Symbol("t").description);
print("wellknown-symbol: " + (function () {
    var o2 = {}; o2[Symbol.toPrimitive] = function (h) { return h === "number" ? 7 : "s"; };
    return (+o2) + "," + ("" + o2); })());
print("toStringTag: " + (function () {
    var o3 = {}; o3[Symbol.toStringTag] = "Zed";
    return Object.prototype.toString.call(o3); })());
print("proxy-get: " + (function () {
    var p = new Proxy({}, {get: function (t, k) { return "got:" + String(k); }});
    return p.anything; })());
print("proxy-has-ownKeys: " + (function () {
    var p = new Proxy({a: 1}, {ownKeys: function () { return ["a", "b"]; },
        getOwnPropertyDescriptor: function () {
            return {value: 1, enumerable: true, configurable: true}; }});
    return Object.keys(p).join(","); })());
print("reflect: " + Reflect.has({a: 1}, "a") + " " +
      Reflect.ownKeys({a: 1, [Symbol("s")]: 2}).length);
print("freeze-seal: " + (function () {
    var f = Object.freeze({a: 1}); try { f.a = 2; } catch (e) {}
    return f.a + "," + Object.isFrozen(f) + "," + Object.isSealed(f); })());
print("defineProperty: " + (function () {
    var d = {}; Object.defineProperty(d, "x", {value: 1});
    return Object.keys(d).length + "," + d.x; })());
print("getters-setters: " + (function () {
    var g = {_v: 0, get v() { return this._v; }, set v(x) { this._v = x * 2; }};
    g.v = 4; return g.v; })());
print("map-set: " + (function () {
    var m = new Map([[1, "a"], [NaN, "b"]]);
    var s = new Set([1, 1, NaN, NaN, -0, 0]);
    return m.get(NaN) + "," + m.size + "," + s.size; })());
print("weak: " + (function () {
    var k = {}; var w = new WeakMap(); w.set(k, 1); return w.get(k); })());

// ---- sorting and array methods ----------------------------------------------
print("array-sort-default: " + [10, 9, 1, 2].sort().join(","));
print("array-sort-stable: " + (function () {
    var a = [{k: 1, i: 0}, {k: 0, i: 1}, {k: 1, i: 2}, {k: 0, i: 3}];
    return a.sort(function (x, y) { return x.k - y.k; })
            .map(function (x) { return x.i; }).join(","); })());
print("array-holes: " + (function () {
    var a = [1, , 3]; return a.length + "," + (1 in a) + "," +
      a.map(function (x) { return x; }).length; })());
print("array-flat: " + [1, [2, [3, [4]]]].flat(2).join(","));
print("array-flatMap: " + [1, 2].flatMap(function (x) { return [x, x * 2]; }).join(","));
print("array-from-string: " + Array.from("a😀").length);
print("array-methods: " + [3, 1, 2].findIndex(function (x) { return x === 1; }) +
      "," + [1, 2, 3].includes(2) + "," + [1, 2].concat([3]).join("") +
      "," + [1, 2, 3].fill(0, 1).join(""));
print("array-reduceRight: " + ["a", "b", "c"].reduceRight(function (a, b) {
    return a + b; }));
print("array-large-join: " + new Array(1000).fill(7).join("").length);
