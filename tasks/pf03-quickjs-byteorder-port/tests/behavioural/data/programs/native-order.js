// The target's own byte order, observed through every width and every API.
//
// Every key here is `nat-`, because every one of them is an observation a
// big-endian target is *supposed* to answer differently. Nothing compares these
// across targets.
//
// What they are for is consistency. The first key establishes the order from a
// 16-bit store; each one after it asks the same question through a different width
// or a different interface, and a correct build -- of either order -- answers all of
// them the same way. A submission that swapped 32-bit accesses and not 64-bit ones,
// or typed arrays and not DataView's default path, is internally inconsistent, and
// that shows up here as a set of answers that cannot all be true of one machine.
//
// The `order` key is the anchor. `native-order` reads it, checks it against the
// order the target's ABI specifies -- which the harness knows from the target name
// and not from anything in the tree -- and then requires every other key to agree.

function bytesOf(buf, n) {
    var u8 = new Uint8Array(buf), out = [];
    for (var i = 0; i < (n === undefined ? u8.length : n); i++) out.push(u8[i]);
    return out.join(",");
}

// ---- the anchor --------------------------------------------------------------
// 0x0102 through a Uint16Array. First byte 1 means big-endian.
var probe = new Uint16Array([0x0102]);
var probeBytes = new Uint8Array(probe.buffer);
print("nat-order: " + (probeBytes[0] === 0x01 ? "big" : "little"));
print("nat-u16-0102: " + bytesOf(probe.buffer));

// ---- the same question, every integer width ---------------------------------
print("nat-u32-01020304: " + bytesOf(new Uint32Array([0x01020304]).buffer));
print("nat-i32-minus2: " + bytesOf(new Int32Array([-2]).buffer));
print("nat-i16-minus2: " + bytesOf(new Int16Array([-2]).buffer));
// Not `nat-`: an 8-bit element has no byte order, so this answer is the same
// everywhere and is graded as an invariant.  It stays in this program because it is
// the control for the ones above -- if it ever moves, a swap reached past the
// element width it was supposed to apply to.
print("u8-onetwo: " + bytesOf(new Uint8Array([1, 2]).buffer));

// ---- floats ------------------------------------------------------------------
print("nat-f32-one: " + bytesOf(new Float32Array([1.0]).buffer));
print("nat-f32-minusone: " + bytesOf(new Float32Array([-1.0]).buffer));
print("nat-f64-onepointfive: " + bytesOf(new Float64Array([1.5]).buffer));
print("nat-f64-one: " + bytesOf(new Float64Array([1.0]).buffer));

// ---- BigInt64, which is the widest native store there is ---------------------
print("nat-bi64: " + bytesOf(new BigInt64Array([0x0102030405060708n]).buffer));
print("nat-bu64: " + bytesOf(new BigUint64Array([0xf1f2f3f4f5f6f7f8n]).buffer));

// ---- DataView with no endianness argument on the *set* side ------------------
// dv.setUint32(0, v) is spec'd big-endian and lives in dataview-explicit.js. What
// is native here is the aliasing: writing through a typed array and reading through
// a DataView that was told an order.
var ab = new ArrayBuffer(8);
var u32 = new Uint32Array(ab);
var dv = new DataView(ab);
u32[0] = 0x01020304;
print("nat-alias-u32-then-dv-be: " + dv.getUint32(0, false).toString(16));
print("nat-alias-u32-then-dv-le: " + dv.getUint32(0, true).toString(16));

var f64arr = new Float64Array(1);
var f64dv = new DataView(f64arr.buffer);
f64arr[0] = 1.5;
print("nat-alias-f64-then-dv-be: " + f64dv.getFloat64(0, false));
print("nat-alias-f64-then-dv-le: " + f64dv.getFloat64(0, true));

// ---- reinterpretation across widths on one buffer ---------------------------
// The classic: store one 32-bit value, read it as two 16-bit ones.
var re = new ArrayBuffer(4);
new Uint32Array(re)[0] = 0x01020304;
var asU16 = new Uint16Array(re);
print("nat-u32-as-u16: " + asU16[0].toString(16) + "," + asU16[1].toString(16));
var re2 = new ArrayBuffer(8);
new Float64Array(re2)[0] = 1.5;
print("nat-f64-as-u32: " + new Uint32Array(re2)[0].toString(16) + "," +
      new Uint32Array(re2)[1].toString(16));

// ---- toString over a reinterpreted buffer -----------------------------------
// This is the exact shape of upstream's tests/test_builtin.js:442, which has the
// little-endian answer hardcoded. A correct big-endian build answers it reversed and
// fails that assertion, which is why `upstream-suites` grades against the measured
// baseline rather than a fixed list.
var story = new ArrayBuffer(16);
var s32 = new Uint32Array(story, 12, 1); s32[0] = -1;
var s16 = new Uint16Array(story, 2); s16[0] = -1;
var sf32 = new Float32Array(story, 8, 1); sf32[0] = 1;
print("nat-builtin442: " + new Uint8Array(story).toString());

// ---- a typed array over a slice ---------------------------------------------
var sliced = new ArrayBuffer(16);
var mid = new Uint32Array(sliced, 4, 2);
mid[0] = 0x0a0b0c0d; mid[1] = 0x01020304;
print("nat-slice: " + bytesOf(sliced, 12));
// Not `nat-`: a byteOffset is an integer the spec fixes, not a byte pattern.
print("slice-byteOffset: " + mid.byteOffset);

// ---- Atomics, which are native-order stores by definition -------------------
if (typeof SharedArrayBuffer !== "undefined" && typeof Atomics !== "undefined") {
    try {
        var sab = new SharedArrayBuffer(8);
        var sa32 = new Int32Array(sab);
        Atomics.store(sa32, 0, 0x01020304);
        print("nat-atomics: " + bytesOf(sab, 4));
        // Not `nat-`: storing and loading through the same view returns the value,
        // whatever order the bytes were laid down in.
        print("atomics-roundtrip: " + Atomics.load(sa32, 0).toString(16));
        print("atomics-ops: " + [Atomics.add(sa32, 0, 1), Atomics.load(sa32, 0),
              Atomics.exchange(sa32, 0, 7), Atomics.load(sa32, 0),
              Atomics.compareExchange(sa32, 0, 7, 9), Atomics.load(sa32, 0)]
              .join(","));
    } catch (e) {
        print("nat-atomics: unavailable");
        print("atomics-roundtrip: unavailable");
        print("atomics-ops: unavailable");
    }
} else {
    print("nat-atomics: unavailable");
    print("atomics-roundtrip: unavailable");
    print("atomics-ops: unavailable");
}
