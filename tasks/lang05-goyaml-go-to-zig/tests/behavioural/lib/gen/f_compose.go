package main

import (
	"fmt"
	"strings"
)

// The compose family crosses axes on purpose.
//
// Every other family isolates one thing, which is what makes a failure
// diagnosable: if `scalar-block` is red and nothing else is, the block scalar
// reader is where to look.  But isolation is also a blind spot.  A port can hold
// every axis separately and still lose a chomping indicator when the block scalar
// sits inside a sequence inside a tagged mapping, because the bug is in how two
// pieces of state interact rather than in either piece.
//
// So: a small set of scalar bodies chosen for being awkward, wrapped in a set of
// contexts chosen for changing the emitter's decisions, each observed three ways.
// Small tables, multiplied -- 30 x 16 x 3 finds interaction bugs that 1,440
// hand-written shapes would not, because I would not have thought to write these
// 1,440.

// Bodies whose rendering is context-dependent.  A plain `1` is a plain `1`
// everywhere; these are not.
var composeBodies = []struct{ label, text string }{
	{"empty", ""},
	{"plain", "x"},
	{"int", "1"},
	{"bool", "true"},
	{"yes", "yes"},
	{"null", "null"},
	{"tilde", "~"},
	{"float", "1.5"},
	{"inf", ".inf"},
	{"leading-zero", "0755"},
	{"hex", "0x1f"},
	{"underscored", "1_000"},
	{"date", "2001-12-14"},
	{"colon-space", "a: b"},
	{"colon-nospace", "a:b"},
	{"hash", "a # b"},
	{"dash", "- x"},
	{"leading-space", " x"},
	{"trailing-space", "x "},
	{"only-space", " "},
	{"tab", "\tx"},
	{"quote", "\"x\""},
	{"apostrophe", "'x'"},
	{"backslash", "a\\b"},
	{"newline", "a\nb"},
	{"indicator-at", "@x"},
	{"indicator-backtick", "`x"},
	{"indicator-star", "*x"},
	{"indicator-amp", "&x"},
	{"indicator-bang", "!x"},
	{"indicator-percent", "%x"},
	{"indicator-question", "?x"},
	{"multiword", "the quick brown fox"},
	{"accented", "caf\u00e9"},
	{"emoji", "\U0001f600"},
	{"cjk", "\u4f60\u597d"},
	{"long", strings.Repeat("word ", 24)},
}

// A context takes a body and returns a document.  The body arrives already
// quoted or not by the context: some contexts must quote to stay valid at all
// (a plain `a: b` inside a flow mapping is not one scalar), and forcing every
// context to accept a raw body would mean half the table was parse errors rather
// than composition tests.
var composeContexts = []struct {
	label string
	wrap  func(body string) string
	// quote says the context needs its body double-quoted to remain one scalar.
	quote bool
}{
	{"bare doc", func(b string) string { return dq(b) + "\n" }, true},
	{"map value", func(b string) string { return "k: " + dq(b) + "\n" }, true},
	{"map key", func(b string) string { return dq(b) + ": v\n" }, true},
	{"seq item", func(b string) string { return "- " + dq(b) + "\n" }, true},
	{"nested map", func(b string) string { return "a:\n  b:\n    c: " + dq(b) + "\n" }, true},
	{"seq in map", func(b string) string { return "a:\n  - " + dq(b) + "\n" }, true},
	{"map in seq", func(b string) string { return "- a: " + dq(b) + "\n  b: 1\n" }, true},
	{"flow seq", func(b string) string { return "[" + dq(b) + "]\n" }, true},
	{"flow map value", func(b string) string { return "{k: " + dq(b) + "}\n" }, true},
	{"flow map key", func(b string) string { return "{" + dq(b) + ": v}\n" }, true},
	{"deep flow", func(b string) string { return "[[{k: [" + dq(b) + "]}]]\n" }, true},
	{"tagged", func(b string) string { return "!!str " + dq(b) + "\n" }, true},
	{"anchored", func(b string) string { return "a: &x " + dq(b) + "\nb: *x\n" }, true},
	{"tag and anchor", func(b string) string { return "a: !!str &x " + dq(b) + "\n" }, true},
	{"commented", func(b string) string { return "# h\nk: " + dq(b) + " # l\n# f\n" }, true},
	{"second doc", func(b string) string { return "--- first\n--- " + dq(b) + "\n" }, true},
	{"directive", func(b string) string {
		return "%TAG !e! tag:example.com,2000:\n---\nk: " + dq(b) + "\n"
	}, true},
	{"explicit key", func(b string) string { return "? " + dq(b) + "\n: v\n" }, true},
	// Plain rather than quoted, so the resolver runs on the body instead of being
	// short-circuited by the double quotes.  Only bodies that are valid plain
	// scalars in this position reach it; plainSafe decides.
	{"plain map value", func(b string) string { return "k: " + b + "\n" }, false},
	{"plain seq item", func(b string) string { return "- " + b + "\n" }, false},
	{"plain bare doc", func(b string) string { return b + "\n" }, false},
	{"plain flow seq", func(b string) string { return "[" + b + "]\n" }, false},
	// Block scalars, where the body becomes indented content.  Only single-line
	// bodies without leading whitespace go here: leading whitespace in a block
	// scalar is content-indentation, which is a different test (and is covered by
	// the explicit-indicator cases in scalar-block).
	{"literal block", func(b string) string { return "k: |\n  " + b + "\n" }, false},
	{"folded block", func(b string) string { return "k: >\n  " + b + "\n" }, false},
	{"literal keep", func(b string) string { return "k: |+\n  " + b + "\n" }, false},
	{"literal strip", func(b string) string { return "k: |-\n  " + b + "\n" }, false},
	// Single quotes: a different escape language from double quotes, and the one
	// the emitter reaches for when a plain scalar will not do but no escape is
	// needed.
	{"single quoted", func(b string) string { return "k: '" + sq(b) + "'\n" }, false},
}

// dq renders a body as the inside of a double-quoted scalar.  Only the two
// characters double quotes actually reserve are escaped, plus the C0 controls
// YAML will not carry raw -- everything else is left literal so the case tests
// the parser's handling of the byte rather than its handling of an escape.
func dq(b string) string {
	var out strings.Builder
	out.WriteByte('"')
	for _, r := range b {
		switch r {
		case '"':
			out.WriteString(`\"`)
		case '\\':
			out.WriteString(`\\`)
		case '\n':
			out.WriteString(`\n`)
		case '\t':
			out.WriteString(`\t`)
		default:
			out.WriteRune(r)
		}
	}
	out.WriteByte('"')
	return out.String()
}

func sq(b string) string {
	// Inside single quotes only the quote itself is special, and it doubles.
	// A newline would end the scalar's line rather than being an escape, so a
	// body containing one is filtered out before it gets here.
	return strings.ReplaceAll(b, "'", "''")
}

// plainSafe says whether a body can appear as an unquoted scalar in the given
// context without changing what is being tested.  It is deliberately
// conservative: the point of the plain contexts is to run the resolver, and a
// body that turns into a parse error there tests the error path instead, which
// the error family already owns.
func plainSafe(label, body string) bool {
	if body == "" {
		return false
	}
	if strings.ContainsAny(body, "\n\t") {
		return false
	}
	if strings.TrimSpace(body) != body {
		return false // leading or trailing space is not preserved plain
	}
	switch body[0] {
	// The c-indicator set: a plain scalar may not start with any of these.
	case '-', '?', ':', ',', '[', ']', '{', '}', '#', '&', '*', '!', '|',
		'>', '\'', '"', '%', '@', '`':
		return false
	}
	if strings.Contains(body, ": ") || strings.HasSuffix(body, ":") {
		return false
	}
	if strings.Contains(body, " #") {
		return false
	}
	switch label {
	case "plain flow seq":
		// Inside flow context the indicators are also forbidden mid-scalar.
		if strings.ContainsAny(body, ",[]{}") {
			return false
		}
	case "single quoted":
		// Handled by sq, but a newline still cannot appear.
		return !strings.Contains(body, "\n")
	case "literal block", "folded block", "literal keep", "literal strip":
		// A block scalar carries any byte, but a body that is only whitespace
		// would be indistinguishable from an empty block.
		return strings.TrimSpace(body) != ""
	}
	return true
}

func familyCompose(g *gen) {
	const fam = "compose"
	for _, ctx := range composeContexts {
		for _, body := range composeBodies {
			if !ctx.quote && !plainSafe(ctx.label, body.text) {
				continue
			}
			src := ctx.wrap(body.text)
			label := fmt.Sprintf("%s / %s", ctx.label, body.label)
			g.node(fam, label, src)
			g.add(fam, "emit", label, src, nil)
			// roundtrip is the one that earns the multiplication.  node says the
			// tree is right and emit says one rendering is right; roundtrip says
			// the rendering is one the port's own parser reads back the same way,
			// which is the property a user of the library actually depends on.
			g.add(fam, "roundtrip", label, src, nil)
		}
	}
}
