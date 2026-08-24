// Date, measured with TZ=UTC pinned by the runner.
//
// Dates are here for two reasons. The obvious one: `Date` reaches libc
// (`gmtime_r`, `mktime`) and a port that changed how the build reaches libc can
// break it. The less obvious one: every value below is fixed by the spec given a
// fixed zone, so if a build disagrees with the oracle here, the disagreement is
// about the C library or the compiler flags, not about byte order -- which is a
// distinction the failure report should be able to make.
//
// Nothing reads the wall clock. `Date.now()` and `new Date()` are absent on
// purpose; a program that prints the current time is not an answer sheet.

var EPOCH = 0;
var MID = 1604793600000;          // 2020-11-08T00:00:00Z, the upstream VERSION date
var NEG = -86400000;              // 1969-12-31T00:00:00Z
var BIG = 8640000000000000;       // the largest representable time value

// ---- construction ------------------------------------------------------------
print("from-epoch: " + new Date(EPOCH).toISOString());
print("from-mid: " + new Date(MID).toISOString());
print("from-negative: " + new Date(NEG).toISOString());
print("from-max: " + new Date(BIG).toISOString());
print("from-overmax: " + new Date(BIG + 1).getTime());
print("from-parts: " + new Date(Date.UTC(2020, 10, 8, 1, 2, 3, 456)).toISOString());
print("from-parts-local: " + new Date(2020, 10, 8, 1, 2, 3, 456).getTime());
print("from-string-iso: " + new Date("2020-11-08T00:00:00Z").getTime());
print("from-string-date: " + new Date("2020-11-08").getTime());
print("from-string-offset: " + new Date("2020-11-08T00:00:00+05:30").getTime());
print("from-string-nonsense: " + new Date("not a date").getTime());
print("from-clip: " + new Date(2020, 13, 40).toISOString());
print("from-two-digit-year: " + new Date(Date.UTC(99, 0, 1)).toISOString());

// ---- Date.UTC and Date.parse -------------------------------------------------
print("utc-full: " + Date.UTC(2020, 10, 8, 1, 2, 3, 456));
print("utc-defaults: " + Date.UTC(2020, 0));
print("utc-year-only: " + Date.UTC(2020));
print("parse-iso: " + Date.parse("2020-11-08T12:34:56.789Z"));
print("parse-iso-nomillis: " + Date.parse("2020-11-08T12:34:56Z"));
print("parse-iso-nozone: " + Date.parse("2020-11-08T12:34:56"));
print("parse-year-month: " + Date.parse("2020-11"));
print("parse-year: " + Date.parse("2020"));
print("parse-extended-year: " + Date.parse("+020020-11-08T00:00:00Z"));
print("parse-negative-year: " + Date.parse("-000001-01-01T00:00:00Z"));
print("parse-invalid: " + Date.parse("2020-13-01T00:00:00Z"));
print("parse-tostring-roundtrip: " + (function () {
    var d = new Date(MID); return Date.parse(d.toString()) === d.getTime(); })());
print("parse-toisostring-roundtrip: " + (function () {
    var d = new Date(MID + 789);
    return Date.parse(d.toISOString()) === d.getTime(); })());
print("parse-toutcstring-roundtrip: " + (function () {
    var d = new Date(MID); return Date.parse(d.toUTCString()) === d.getTime(); })());

// ---- getters, UTC and local (identical because TZ=UTC) ----------------------
var d = new Date(MID + 3723456);  // 2020-11-08T01:02:03.456Z
print("get-utc: " + [d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate(),
    d.getUTCDay(), d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(),
    d.getUTCMilliseconds()].join(","));
print("get-local: " + [d.getFullYear(), d.getMonth(), d.getDate(), d.getDay(),
    d.getHours(), d.getMinutes(), d.getSeconds(), d.getMilliseconds()].join(","));
print("get-time-and-value: " + d.getTime() + "," + d.valueOf() + "," +
      Number(d) + "," + (+d));
print("timezone-offset: " + d.getTimezoneOffset());
print("get-year-legacy: " + d.getYear());

// ---- setters -----------------------------------------------------------------
print("set-utc-full-year: " + (function () {
    var x = new Date(MID); x.setUTCFullYear(1999); return x.toISOString(); })());
print("set-utc-month-overflow: " + (function () {
    var x = new Date(MID); x.setUTCMonth(14); return x.toISOString(); })());
print("set-utc-date-zero: " + (function () {
    var x = new Date(MID); x.setUTCDate(0); return x.toISOString(); })());
print("set-utc-time-cascade: " + (function () {
    var x = new Date(MID); x.setUTCHours(25, 61, 61, 1001);
    return x.toISOString(); })());
print("set-time: " + (function () {
    var x = new Date(0); x.setTime(MID); return x.toISOString(); })());
print("set-nan: " + (function () {
    var x = new Date(MID); x.setUTCDate(NaN); return x.getTime(); })());
print("set-out-of-range: " + (function () {
    var x = new Date(0); x.setUTCFullYear(300000); return x.getTime(); })());
print("set-year-legacy: " + (function () {
    var x = new Date(MID); x.setYear(95); return x.getUTCFullYear(); })());

// ---- leap years, month lengths, day-of-week ---------------------------------
print("leap-2000: " + new Date(Date.UTC(2000, 1, 29)).toISOString());
print("leap-1900: " + new Date(Date.UTC(1900, 1, 29)).toISOString());
print("leap-2020: " + new Date(Date.UTC(2020, 1, 29)).toISOString());
print("month-lengths: " + (function () {
    var out = [];
    for (var m = 0; m < 12; m++) out.push(new Date(Date.UTC(2021, m + 1, 0)).getUTCDate());
    return out.join(","); })());
print("weekdays-of-epoch-week: " + (function () {
    var out = [];
    for (var i = 0; i < 7; i++) out.push(new Date(i * 86400000).getUTCDay());
    return out.join(","); })());
print("century-days: " + (function () {
    var a = Date.UTC(1900, 0, 1), b = Date.UTC(2000, 0, 1);
    return (b - a) / 86400000; })());

// ---- formatting --------------------------------------------------------------
print("to-iso: " + new Date(MID).toISOString());
print("to-iso-millis: " + new Date(MID + 7).toISOString());
print("to-iso-negative-year: " + new Date(Date.UTC(-1, 0, 1)).toISOString());
print("to-iso-far-future: " + new Date(Date.UTC(275760, 8, 13)).toISOString());
print("to-json: " + JSON.stringify({when: new Date(MID)}));
print("to-json-invalid: " + JSON.stringify({when: new Date(NaN)}));
print("to-utc-string: " + new Date(MID).toUTCString());
print("to-string: " + new Date(MID).toString());
print("to-date-string: " + new Date(MID).toDateString());
print("to-time-string: " + new Date(MID).toTimeString());
print("to-string-invalid: " + new Date(NaN).toString());
print("to-iso-invalid-throws: " + (function () {
    try { new Date(NaN).toISOString(); return "no throw"; }
    catch (e) { return e.name; } })());

// ---- coercion ----------------------------------------------------------------
print("date-plus-string: " + (new Date(MID) + "").slice(0, 3));
print("date-minus-date: " + (new Date(MID + 1000) - new Date(MID)));
print("date-to-primitive: " + [
    typeof new Date(MID)[Symbol.toPrimitive]("number"),
    typeof new Date(MID)[Symbol.toPrimitive]("string"),
    typeof new Date(MID)[Symbol.toPrimitive]("default")].join(","));
print("date-compare: " + (new Date(1) < new Date(2)));
print("date-prototype-tostring: " + Object.prototype.toString.call(new Date(0)));
print("date-called-as-function-is-string: " + (typeof Date(0)));
