package main

import "fmt"

// The plain-scalar family is the resolver's truth table.
//
// resolve.go decides an implicit tag in two stages: a hint byte taken from the
// *original* first byte, then a parse of the value with every underscore
// removed.  That split is why "_1" is a string while "1_" is an int, and it is
// the single most likely thing for a port to get wrong, so the axis is
// enumerated rather than sampled.
var plainScalars = []string{
	// canonical forms
	"0", "1", "-1", "+1", "007", "1000",
	// underscores: stripped before parsing, but the hint comes from byte 0
	"1_000", "1_0_0", "1_", "_1", "1__0", "__1", "1_000_000", "0x_1f", "0b_1", "-1_0",
	// base prefixes, both cases, both signs
	"0o17", "0O17", "-0o17", "+0o17", "0x1f", "0X1F", "-0x1f", "+0x1f",
	"0b1010", "0B1010", "-0b1010", "+0b1010",
	// prefixes with nothing after them
	"0b", "0o", "0x", "0B", "0O", "0X", "-0x", "0_",
	// leading zero is octal to strconv, so 08 and 09 fall through to float
	"017", "08", "09", "010", "0777", "0888", "00", "000",
	// int64 and uint64 boundaries: ParseInt, then ParseUint, then ParseFloat
	"9223372036854775807", "9223372036854775808", "-9223372036854775808",
	"-9223372036854775809", "18446744073709551615", "18446744073709551616",
	"99999999999999999999999999", "-99999999999999999999999999",
	// floats
	"1.5", "-1.5", "+1.5", ".5", "-.5", "+.5", "5.", "-5.", "0.0", "-0.0",
	"1e3", "1E3", "1e+3", "1e-3", "1.5e3", "1.5E-3", ".5e3", "5.e3",
	"1e400", "-1e400", "1e-400", "1.7976931348623159e308",
	"1.", ".", "..", "...", "1.2.3", ".1.2",
	// float shapes strconv accepts that YAML's own regexp does not
	"0x1p4", "1_0.5", "Inf", "inf", "-Inf", "NaN", "nan",
	// the .inf/.nan table: exactly three spellings each, nothing else
	".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF", "-.inf", "-.Inf", "-.INF",
	".nan", ".NaN", ".NAN", ".iNf", ".Nan", ".INf", "+.nan", "-.nan",
	// bool table: exactly three spellings each
	"true", "True", "TRUE", "false", "False", "FALSE",
	"tRue", "TRue", "truE", "fAlse", "FaLSE",
	// YAML 1.1 spellings go-yaml v3 dropped
	"y", "Y", "yes", "Yes", "YES", "yEs", "n", "N", "no", "No", "NO",
	"on", "On", "ON", "off", "Off", "OFF",
	// null table
	"~", "null", "Null", "NULL", "nULL", "NuLL", "none", "None", "NONE", "nil",
	// merge
	"<<", "<<<", "< <",
	// sexagesimal, dropped in 1.2 and unsupported here
	"1:2", "1:2:3", "190:20:30", "-1:2",
	// timestamps: the accepted layout list, and near misses
	"2001-12-14", "2001-2-3", "2001-02-03", "0000-01-01", "9999-12-31",
	"2001-13-14", "2001-12-45", "2001-02-29", "2000-02-29", "1900-02-29",
	"2001-12-14T21:59:43Z", "2001-12-14t21:59:43z", "2001-12-14T21:59:43",
	"2001-12-14 21:59:43", "2001-12-14  21:59:43", "2001-12-14\t21:59:43",
	"2001-12-14T21:59:43.1Z", "2001-12-14T21:59:43.123456789Z",
	"2001-12-14T21:59:43.10-05:00", "2001-12-14t21:59:43.10-05:00",
	"2001-12-14 21:59:43.10 -5", "2001-12-14 21:59:43.10 -05:00",
	"2001-12-14T21:59:43+05:30", "2001-12-14T21:59:43+5:30",
	"2001-12-14T21:59:43-0500", "2001-12-14T25:00:00Z", "2001-12-14T21:60:00Z",
	"2001-12-14T21:59:60Z", "01-02-03", "20011214", "2001-12-14x", "2001-12-",
	"2001-12-14T", "2001-12-14 ", " 2001-12-14",
	// plain scalars that merely start with a hinted byte
	"+", "-", "+-", "-+", "--", "typo", "note", "off-by-one", "yesterday",
	"None of it", "Truely", "0 0", "1 2", ".5 x", "-x", "~x", "~ x",
	// empty and blank
	"", " ", "  ",
}

func familyScalarPlain(g *gen) {
	const fam = "scalar-plain"
	for _, s := range plainScalars {
		// As a whole document: the resolver sees exactly this string, with no
		// surrounding structure to change the scanner's mind about it.
		g.node(fam, fmt.Sprintf("bare %q", s), s+"\n")
		// As a mapping value: the same string, reached by the block-context
		// scanner rather than the document one.  A plain scalar's end differs
		// between the two -- ": " and " #" terminate it here.
		g.node(fam, fmt.Sprintf("value %q", s), "k: "+s+"\n")
		// Emitted, because the emitter has to decide whether the value needs
		// quoting to survive a re-read, and that decision is driven by the tag
		// the resolver just picked.
		g.add(fam, "emit", fmt.Sprintf("emit value %q", s), "k: "+s+"\n", nil)
	}
	// As a key, where the resolver runs on the key too and the emitter has to
	// decide about quoting in key position.
	for _, s := range []string{
		"1", "true", "null", "~", "y", "on", "1.5", ".inf", "2001-12-14", "<<",
		"0x1f", "", " ", "a b", "?", "-", ":",
	} {
		g.nodeEmit(fam, fmt.Sprintf("key %q", s), s+": v\n")
	}
	// Plain scalars spanning lines: folding, and the rule that a plain scalar
	// cannot contain ": " or " #".
	for _, s := range []string{
		"a\n b", "a\nb", "a\n\nb", "a \n b", "a\n  b\n c", "a\n\n\nb",
		"a#b", "a #b", "a# b", "a:b", "a:\tb", "a :b", "a\t: b",
	} {
		g.nodeEmit(fam, fmt.Sprintf("multiline %q", s), "k: "+s+"\n")
	}
}

// Quoted scalars are the escape table plus the folding rules that only apply
// inside quotes.  Every escape go-yaml recognises is listed; the ones it
// rejects are in the error family.
var doubleQuoted = []string{
	`plain`, `a\nb`, `a\tb`, `a\\b`, `a\"b`, `a\'b`, `a\ b`, `a\/b`,
	`a\0b`, `a\ab`, `a\bb`, `a\fb`, `a\rb`, `a\vb`, `a\eb`,
	`a\Nb`, `a\_b`, `a\Lb`, `a\Pb`,
	`a\x41b`, `a\x00b`, `a\x7fb`, `a\xffb`, `a\x20b`,
	`a\u0041b`, `a\u00e9b`, `a\uffffb`, `a\ufeffb`, `a\u0000b`, `a\u2028b`,
	`a\U0001F600b`, `a\U00000041b`, `a\U0010FFFFb`,
	// folding inside double quotes
	"a\n b", "a\n\n b", "a \n b", "a\\\n b", "a\n\n\n b",
	// escapes at the edges
	`\n`, `\\`, `\"`, `\ `, `\t`, ``, ` `, `  `,
	// a quoted scalar that looks like every other type
	`1`, `true`, `null`, `~`, `1.5`, `.inf`, `2001-12-14`,
}

var singleQuoted = []string{
	`plain`, `it''s`, `a\nb`, `a\\b`, `a"b`, `a#b`, `a: b`,
	"a\n b", "a\n\n b", "a \n b", "a\n\n\n b",
	``, ` `, `  `, `''`, `1`, `true`, `~`,
}

func familyScalarQuoted(g *gen) {
	const fam = "scalar-quoted"
	for _, s := range doubleQuoted {
		src := "k: \"" + s + "\"\n"
		g.nodeEmit(fam, fmt.Sprintf("double %q", s), src)
	}
	for _, s := range singleQuoted {
		src := "k: '" + s + "'\n"
		g.nodeEmit(fam, fmt.Sprintf("single %q", s), src)
	}
	// Quoted keys, and quoted scalars as whole documents, where the scanner is
	// in a different context.
	for _, s := range []string{`a`, `a b`, `1`, ``, `a: b`, `a\nb`} {
		g.nodeEmit(fam, fmt.Sprintf("double doc %q", s), "\""+s+"\"\n")
		g.nodeEmit(fam, fmt.Sprintf("single doc %q", s), "'"+s+"'\n")
		g.nodeEmit(fam, fmt.Sprintf("double key %q", s), "\""+s+"\": v\n")
		g.nodeEmit(fam, fmt.Sprintf("single key %q", s), "'"+s+"': v\n")
	}
}
