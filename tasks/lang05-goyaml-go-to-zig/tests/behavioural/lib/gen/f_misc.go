package main

import (
	"fmt"
	"strings"
)

// The indent family is the emit_indent op crossed with shapes whose rendering
// actually depends on the indent.  Three separate fallbacks meet here: 0 becomes
// 4 in encode.go, 1 and anything above 9 become 2 in emitterc.go, and 2 to 9
// pass through.
func familyIndent(g *gen) {
	const fam = "indent"

	shapes := []struct{ label, src string }{
		{"nested map", "a:\n  b:\n    c: 1\n"},
		{"seq in map", "a:\n  - 1\n  - 2\n"},
		{"map in seq", "- a: 1\n  b: 2\n"},
		{"deep", "a:\n  b:\n    c:\n      d:\n        e: 1\n"},
		{"seq of seq", "- - 1\n  - 2\n"},
		{"block scalar", "a: |\n  x\n  y\n"},
		{"folded scalar", "a: >\n  x\n  y\n"},
		{"block indicator", "a: |2\n   x\n"},
		{"flow kept flow", "a: [1, 2]\n"},
		{"flow map", "a: {b: 1}\n"},
		{"anchor", "a: &x\n  b: 1\nc: *x\n"},
		{"long value", "a:\n  b: " + strings.Repeat("word ", 30) + "\n"},
		{"comment", "# head\na:\n  b: 1 # line\n"},
		{"explicit key", "? a\n: b\n"},
	}
	for _, sh := range shapes {
		for n := 0; n <= 11; n++ {
			g.indentAt(fam, sh.label, sh.src, n)
		}
	}
}

// Unicode: the printable-byte test in yamlprivateh.go is a byte-range check, not
// a code-point classification, and it excludes every four-byte sequence.  That
// is why an emoji comes back escaped and an accented letter does not.
func familyUnicode(g *gen) {
	const fam = "unicode"

	values := []string{
		"\u00e9", "\u00ff", "\u0100", "\u07ff", "\u0800", "\u0fff",
		"\u1000", "\u4f60\u597d", "\ucfff", "\ud7ff", "\ue000", "\uf8ff",
		"\ufffd", "\ufffe", "\ufeff", "\uffff",
		"\U00010000", "\U0001f600", "\U0010ffff",
		"a\u00e9b", "a\U0001f600b", "\u00e9\U0001f600",
		"\u00a0", "\u1680", "\u2000", "\u3000",
		"\u0085", "\u2028", "\u2029",
		"\u200b", "\u200e", "\u202e", "\u2060",
		"\u0301", "e\u0301", "\u0640",
		"\u05d0\u05d1", "\u0627\u0628",
	}
	for _, v := range values {
		// As a value, as a key, and inside double quotes, because the emitter's
		// decision differs between plain and quoted context.
		g.nodeEmit(fam, fmt.Sprintf("value %q", v), "k: "+v+"\n")
		g.nodeEmit(fam, fmt.Sprintf("key %q", v), v+": v\n")
		g.nodeEmit(fam, fmt.Sprintf("quoted %q", v), "k: \""+v+"\"\n")
		g.nodeEmit(fam, fmt.Sprintf("single %q", v), "k: '"+v+"'\n")
		g.nodeEmit(fam, fmt.Sprintf("block %q", v), "k: |\n  "+v+"\n")
	}

	// A BOM is stripped at the start of a stream, kept anywhere else.
	for _, s := range []string{
		"\ufeffa: 1\n", "a: 1\n\ufeffb: 2\n", "a: \ufeff1\n", "\ufeff\n",
		"\ufeff---\na\n", "---\n\ufeffa\n",
	} {
		g.nodeEmit(fam, fmt.Sprintf("bom %q", s), s)
		g.add(fam, "stream", fmt.Sprintf("bom stream %q", s), s, nil)
	}

	// The three non-ASCII line breaks the scanner honours.  Each ends a line, so
	// a value followed by one of them is empty and the next token starts a new
	// line -- which also means the reported line numbers move.
	for _, br := range []string{"\u0085", "\u2028", "\u2029"} {
		g.nodeEmit(fam, fmt.Sprintf("break %q as separator", br), "a: 1"+br+"b: 2\n")
		g.nodeEmit(fam, fmt.Sprintf("break %q in quoted", br), "a: \"x"+br+"y\"\n")
		g.nodeEmit(fam, fmt.Sprintf("break %q in block", br), "a: |\n  x"+br+"  y\n")
	}
}

// Line endings and tabs.  CR-only and CRLF are both accepted and normalised; a
// tab is legal as separating whitespace but never as indentation.
func familyWhitespace(g *gen) {
	const fam = "whitespace"

	base := []string{"a: 1\nb: 2\n", "- 1\n- 2\n", "a:\n  b: 1\n", "a: |\n  x\n  y\n", "[1,\n2]\n"}
	for _, s := range base {
		g.nodeEmit(fam, fmt.Sprintf("lf %q", s), s)
		g.nodeEmit(fam, fmt.Sprintf("crlf %q", s), strings.ReplaceAll(s, "\n", "\r\n"))
		g.nodeEmit(fam, fmt.Sprintf("cr %q", s), strings.ReplaceAll(s, "\n", "\r"))
		g.nodeEmit(fam, fmt.Sprintf("mixed %q", s), strings.Replace(s, "\n", "\r\n", 1))
		g.nodeEmit(fam, fmt.Sprintf("no-final-newline %q", s), strings.TrimSuffix(s, "\n"))
	}

	shapes := []string{
		"a:\tb\n", "a: b\t\n", "a:  \tb\n", "a: \t b\n",
		"\ta: b\n", "a:\n\tb: 1\n", "- \t1\n", "-\t1\n",
		"a: 1   \n", "a: 1\t\t\n", "   \na: 1\n", "\n   \na: 1\n",
		"a: 1\n   \nb: 2\n", "a: 1\n\t\nb: 2\n",
		"a: \"x\ty\"\n", "a: 'x\ty'\n", "a: |\n  x\ty\n", "a: >\n  x\ty\n",
		"[1,\t2]\n", "{k:\tv}\n", "{k: v\t}\n",
		"a: 1 \r\n", "a: 1\t\r\n", "\r\n\r\na: 1\r\n",
		"a: |\r\n  x\r\n", "a: >\r\n  x\r\n",
	}
	for _, s := range shapes {
		g.nodeEmit(fam, fmt.Sprintf("%q", s), s)
	}
}

// The error family is the message table.  Each entry is an input chosen to reach
// one distinct diagnostic, because an error message that no case produces is an
// error message a port can get wrong for free.
func familyError(g *gen) {
	const fam = "error"

	shapes := []string{
		// scanner
		"a: b\n\tc: d\n",
		"\ta: b\n",
		"a:\n\tb\n",
		"@invalid\n",
		"`invalid\n",
		"a: @x\n",
		"a: `x\n",
		"'unterminated\n",
		"\"unterminated\n",
		"'unterminated",
		"\"unterminated",
		"\"bad \\q escape\"\n",
		"\"bad \\x0 escape\"\n",
		"\"bad \\xZZ\"\n",
		"\"bad \\u00\"\n",
		"\"bad \\uZZZZ\"\n",
		"\"bad \\U0000\"\n",
		"\"surrogate \\ud800\"\n",
		"\"surrogate \\udfff\"\n",
		"\"beyond \\U00110000\"\n",
		"k: |x\n  y\n",
		"k: |0\n  y\n",
		"k: |10\n  y\n",
		"k: |--\n  y\n",
		"k: |++\n  y\n",
		"k: >2>\n  y\n",
		"a: *\n",
		"a: &\n",
		"a: & b\n",
		"a: * b\n",
		"a: *x y\n",
		"%TAG\n---\nx\n",
		"%TAG !\n---\nx\n",
		"%TAG ! \n---\nx\n",
		"%TAG !e tag:x\n---\nx\n",
		"%YAML\n---\nx\n",
		"%YAML 1\n---\nx\n",
		"%YAML 1.1.1\n---\nx\n",
		"%YAML 1.1 extra\n---\nx\n",
		"!<>\nx\n",
		"! !\nx\n",
		"a: !<not uri>\n",
		"a: !x!y!z 1\n",
		// parser
		"[1, 2",
		"{k: v",
		"[1, 2}",
		"{k: v]",
		"]\n",
		"}\n",
		",\n",
		"a: b\n- c\n",
		"a:\n- 1\n b: 2\n",
		"- 1\n  - 2\n",
		"a: 1\n b: 2\n",
		"a: 1\nb: 2\n c: 3\n",
		"? a\n? b\n: c\n",
		": a\n",
		"a: b: c\n",
		"a: b\nc: d: e\n",
		"[a: b: c]\n",
		"*x\n",
		"a: *nope\n",
		"[*nope]\n",
		"--- *nope\n",
		"a: 1\n---\nb: *nope\n",
		"%YAML 1.2\n---\nx\n",
		"a: 1\n%YAML 1.1\n---\nb: 2\n",

		// The rest of this list closes the coverage gap the first draft left.
		// Reading every yaml_parser_set_*_error call in scannerc.go and parserc.go
		// gives 36 distinct `problem` strings; the shapes above reached 26 of them,
		// and a diagnostic no case produces is one a port can get wrong for free.
		// Two of the remaining ten turned out to be *contexts* whose problem is
		// "exceeded max depth of 10000" (already covered by the limit family) and
		// one, "did not find expected <stream-start>", is unreachable because the
		// parser always emits STREAM-START first.  These reach the other seven.
		"%\n---\nx\n",           // could not find expected directive name
		"% \n---\nx\n",          // the same, with the name position blank
		"%!\n---\nx\n",          // and with a non-name character there
		"%YAML 111.1\n---\nx\n", // found extremely long version number (max 2 digits)
		"%YAML 1.111\n---\nx\n", // the minor number is bounded too
		"%YAML\"x\"\n---\nx\n",  // found unexpected non-alphabetical character
		"%YAML-1.1\n---\nx\n",   // a directive name may not contain '-'
		"%TAG!e!x\n---\nx\n",    // nor may the name run into its argument
		"!!str\"x\"\n",          // did not find expected whitespace or line break
		"k: !!str\"x\"\n",       // the same in value position
		"!!str{a: 1}\n",         // a tag running straight into a flow mapping
		"\"a\n---\nb\"\n",       // found unexpected document indicator
		"'a\n---\nb'\n",         // in a single-quoted scalar as well
		"\"a\n...\nb\"\n",       // ... counts as one too
		"- 1\n?\n",              // did not find expected '-' indicator
		"a:\n  - 1\n  ? x\n",    // the same, nested
		"- 1\nx: 2\n- 3\n",      // a mapping key interrupting a block sequence
		"a: [1] ? b\n",          // mapping keys are not allowed in this context
		"a: \"x\" ? b\n",        // a simple key is not allowed after a value
		"[1] ? b\n",             // at the top level too
		"- [1] ? b\n",           // and inside a sequence entry
		"a:\n  b: [1] ? c\n",    // nested, so the reported line is not line 1
		"a: [1] : b\n",          // the value-indicator form of the same rule
		"a: [1] - b\n",          // and the block-entry form
		"- 1\n- 2\nx\n",         // could not find expected ':'
		"- - 1\n  x\n",          // the same one level down
		"? a\n- b\n",            // did not find expected key
	}
	for _, s := range shapes {
		g.node(fam, fmt.Sprintf("%q", s), s)
		// Also through `stream`, where an error part way through a multi-document
		// input has to be reported alongside the documents that did parse.
		g.add(fam, "stream", fmt.Sprintf("stream %q", s), s, nil)
	}

	// An error in the second document, so the first is graded as a success and
	// the error as a failure in the same case.
	for _, bad := range []string{"[1", "\tx: 1\n", "*nope\n", "%YAML 1.2\n---\nx\n"} {
		src := "--- good\n--- " + bad
		g.add(fam, "stream", fmt.Sprintf("late %q", bad), src, nil)
		g.add(fam, "emit_stream", fmt.Sprintf("late emit %q", bad), src, nil)
	}
}

// Limits: go-yaml refuses to nest past 10000 and says so.  The number is in the
// contract, so a port knows it has to survive that depth rather than discovering
// its own stack limit here.
func familyLimit(g *gen) {
	const fam = "limit"

	for _, d := range []int{1, 2, 10, 100, 500, 1000} {
		g.node(fam, fmt.Sprintf("seq depth %d", d), strings.Repeat("- ", d)+"x\n")
		g.node(fam, fmt.Sprintf("flow seq depth %d", d),
			strings.Repeat("[", d)+"x"+strings.Repeat("]", d)+"\n")
		g.node(fam, fmt.Sprintf("flow map depth %d", d),
			strings.Repeat("{a: ", d)+"x"+strings.Repeat("}", d)+"\n")
	}
	// Just over the limit, where the message itself is the answer.  Only the
	// flow forms are used: a block sequence at that depth would be a 20 KB line
	// of "- " and the response is the same error either way.
	for _, d := range []int{10000, 10001, 12000} {
		g.node(fam, fmt.Sprintf("flow seq depth %d", d),
			strings.Repeat("[", d)+"x"+strings.Repeat("]", d)+"\n")
	}

	// Wide rather than deep.
	for _, n := range []int{100, 1000} {
		var b strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "k%d: %d\n", i, i)
		}
		g.node(fam, fmt.Sprintf("%d keys", n), b.String())
		g.add(fam, "emit", fmt.Sprintf("emit %d keys", n), b.String(), nil)

		var s strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&s, "- %d\n", i)
		}
		g.node(fam, fmt.Sprintf("%d entries", n), s.String())
	}

	// Long single scalars, where the emitter's line-width logic decides where to
	// fold.  80 columns is the default and these straddle it.
	for _, n := range []int{40, 79, 80, 81, 120, 400, 4000} {
		g.nodeEmit(fam, fmt.Sprintf("plain %d chars", n), "k: "+strings.Repeat("a", n)+"\n")
		g.nodeEmit(fam, fmt.Sprintf("words %d chars", n),
			"k: "+strings.Repeat("ab ", n/3)+"\n")
		g.nodeEmit(fam, fmt.Sprintf("quoted %d chars", n),
			"k: \""+strings.Repeat("a", n)+"\"\n")
	}
	for _, n := range []int{100, 1000} {
		g.add(fam, "stream", fmt.Sprintf("%d docs", n),
			strings.Repeat("--- x\n", n), nil)
	}
}

// The roundtrip op over a spread of shapes: emit, re-parse, emit again.  The
// emitter's output is a different input distribution from anything the generator
// writes by hand -- canonically indented, canonically quoted -- and a port whose
// emitter writes text its own parser reads differently shows up only here.
func familyRoundtrip(g *gen) {
	const fam = "roundtrip"

	shapes := []string{
		"a: 1\n",
		"a: 1\nb: [1, 2]\nc: {d: e}\n",
		"a: |\n  x\n  y\n",
		"a: >\n  x\n  y\n",
		"a: |+\n  x\n\n",
		"a: |-\n  x\n\n",
		"# head\na: 1 # line\n# foot\n",
		"a: &x 1\nb: *x\n",
		"&x [*x]\n",
		"a: !!str 1\n",
		"a: !local 1\n",
		"%TAG !e! tag:example.com,2000:\n---\n!e!foo x\n",
		"? [a, b]\n: c\n",
		"a: \"\\u00e9\\U0001f600\"\n",
		"a: '\ufeff'\n",
		"a: \"x\\ty\"\n",
		"a: \"  lead\"\n",
		"a: \"trail  \"\n",
		"a: \"\"\n",
		"a: ''\n",
		"a:\n",
		"~\n",
		"a: ~\n",
		"a: null\n",
		"<<: {a: 1}\n",
		"a: !!set {x, y}\n",
		"a: !!omap [{x: 1}]\n",
		"a: " + strings.Repeat("word ", 40) + "\n",
		"a: " + strings.Repeat("a", 200) + "\n",
		"a: 2001-12-14\n",
		"a: 08\n",
		"a: 0x1f\n",
		"a: 1_000\n",
		"a: y\n",
		"a: yes\n",
		"a: .inf\n",
		"a: 1e400\n",
		"a: [\"y\", 'y', y]\n",
	}
	for _, s := range shapes {
		g.add(fam, "roundtrip", fmt.Sprintf("%q", s), s, nil)
	}
}
