// DataView with an explicit endianness argument.
//
// Every accessor here is told which order to use, so ECMAScript fixes the answer on
// every machine it runs on. No key in this file is `nat-`: a big-endian target that
// answers any of these differently from a little-endian one is wrong, full stop.
//
// This is the file that catches the trap in quickjs.c. The two DataView sites are
// `#ifndef WORDS_BIGENDIAN` -- inverted relative to the six `#ifdef` ones -- so a
// submission that found the macro by grepping for `#ifdef` fixes six sites, leaves
// these two, and produces an interpreter whose explicit-endianness reads are
// backwards on s390x while its typed arrays look fine.

function hex(n, width) {
    var s = (n >>> 0).toString(16);
    while (s.length < width) s = "0" + s;
    return s;
}

var ab = new ArrayBuffer(16);
var dv = new DataView(ab);
var u8 = new Uint8Array(ab);

function clear() { for (var i = 0; i < u8.length; i++) u8[i] = 0; }
function bytes(n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(u8[i]);
    return out.join(" ");
}

// ---- set with an explicit order, read the bytes back --------------------------
clear(); dv.setUint16(0, 0x0102, false);
print("set-u16-be: " + bytes(2));
clear(); dv.setUint16(0, 0x0102, true);
print("set-u16-le: " + bytes(2));

clear(); dv.setUint32(0, 0x01020304, false);
print("set-u32-be: " + bytes(4));
clear(); dv.setUint32(0, 0x01020304, true);
print("set-u32-le: " + bytes(4));

clear(); dv.setFloat32(0, 1.0, false);
print("set-f32-be: " + bytes(4));
clear(); dv.setFloat32(0, 1.0, true);
print("set-f32-le: " + bytes(4));

clear(); dv.setFloat64(0, 1.5, false);
print("set-f64-be: " + bytes(8));
clear(); dv.setFloat64(0, 1.5, true);
print("set-f64-le: " + bytes(8));

clear(); dv.setBigUint64(0, 0x0102030405060708n, false);
print("set-bu64-be: " + bytes(8));
clear(); dv.setBigUint64(0, 0x0102030405060708n, true);
print("set-bu64-le: " + bytes(8));

// ---- write known bytes, read with an explicit order --------------------------
clear();
for (var i = 0; i < 8; i++) u8[i] = 0x11 * (i + 1);

print("get-u16-be: " + hex(dv.getUint16(0, false), 4));
print("get-u16-le: " + hex(dv.getUint16(0, true), 4));
print("get-i16-be: " + dv.getInt16(0, false));
print("get-i16-le: " + dv.getInt16(0, true));
print("get-u32-be: " + hex(dv.getUint32(0, false), 8));
print("get-u32-le: " + hex(dv.getUint32(0, true), 8));
print("get-i32-be: " + dv.getInt32(0, false));
print("get-i32-le: " + dv.getInt32(0, true));
print("get-bu64-be: " + dv.getBigUint64(0, false).toString(16));
print("get-bu64-le: " + dv.getBigUint64(0, true).toString(16));
print("get-bi64-be: " + dv.getBigInt64(0, false).toString());
print("get-bi64-le: " + dv.getBigInt64(0, true).toString());

// Float reads of a known bit pattern, both orders. The values are exact in binary,
// so there is nothing to round.
clear();
u8[0] = 0x3f; u8[1] = 0xf8; u8[2] = 0x00; u8[3] = 0x00;
u8[4] = 0x00; u8[5] = 0x00; u8[6] = 0x00; u8[7] = 0x00;
print("get-f64-be: " + dv.getFloat64(0, false));
print("get-f64-le: " + dv.getFloat64(0, true));
clear();
u8[0] = 0x3f; u8[1] = 0x80; u8[2] = 0x00; u8[3] = 0x00;
print("get-f32-be: " + dv.getFloat32(0, false));
print("get-f32-le: " + dv.getFloat32(0, true));

// ---- 8-bit accessors take no order and must not acquire one ------------------
clear(); u8[0] = 0xff; u8[1] = 0x7f; u8[2] = 0x80;
print("get-u8-0: " + dv.getUint8(0));
print("get-i8-0: " + dv.getInt8(0));
print("get-i8-2: " + dv.getInt8(2));
clear(); dv.setInt8(0, -1); dv.setUint8(1, 200);
print("set-8bit: " + bytes(2));

// ---- offsets, so a swap that ignores the offset shows up ---------------------
clear();
dv.setUint32(4, 0xdeadbeef, false);
print("set-u32-be-off4: " + bytes(8));
clear();
dv.setUint32(4, 0xdeadbeef, true);
print("set-u32-le-off4: " + bytes(8));
clear();
for (var i = 0; i < 12; i++) u8[i] = i + 1;
print("get-u32-be-off5: " + hex(dv.getUint32(5, false), 8));
print("get-u32-le-off5: " + hex(dv.getUint32(5, true), 8));
print("get-u16-be-off7: " + hex(dv.getUint16(7, false), 4));
print("get-u16-le-off7: " + hex(dv.getUint16(7, true), 4));

// ---- a DataView over a slice of a larger buffer ------------------------------
var big = new ArrayBuffer(32);
var sub = new DataView(big, 8, 16);
var bigU8 = new Uint8Array(big);
sub.setUint32(0, 0x01020304, false);
print("sub-be-at8: " + bigU8[8] + " " + bigU8[9] + " " + bigU8[10] + " " + bigU8[11]);
sub.setUint32(0, 0x01020304, true);
print("sub-le-at8: " + bigU8[8] + " " + bigU8[9] + " " + bigU8[10] + " " + bigU8[11]);
print("sub-byteOffset: " + sub.byteOffset);
print("sub-byteLength: " + sub.byteLength);

// ---- the default: an *absent* argument means big-endian, per spec ------------
// Not `nat-`. ECMAScript says a missing `littleEndian` is false, which is
// big-endian, on every machine. This is the single most direct probe of the two
// inverted sites: State A on s390x answers it byte-reversed.
clear(); dv.setUint32(0, 0x01020304);
print("set-u32-default: " + bytes(4));
clear();
for (var i = 0; i < 4; i++) u8[i] = 0x11 * (i + 1);
print("get-u32-default: " + hex(dv.getUint32(0), 8));
print("get-u16-default: " + hex(dv.getUint16(0), 4));
clear(); dv.setUint32(0, 0x01020304, undefined);
print("set-u32-undefined: " + bytes(4));
clear(); dv.setUint32(0, 0x01020304, 0);
print("set-u32-zero: " + bytes(4));
clear(); dv.setUint32(0, 0x01020304, "");
print("set-u32-emptystr: " + bytes(4));
clear(); dv.setUint32(0, 0x01020304, "x");
print("set-u32-truthystr: " + bytes(4));
