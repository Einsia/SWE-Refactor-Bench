package main

import (
	"fmt"
	"strings"
)

// The fresh cases: inputs composed from a seed the submission has never seen,
// answered by the reference in the grading container.
//
// They exist because the frozen cases are, unavoidably, a published set.  A
// submission that embedded a table mapping those 20,000 requests to their answers
// would score full marks on it while containing no YAML parser at all.  The
// audit gates look for such a table directly, but a gate is a pattern match
// and this is not: there is nothing to embed, because the requests do not exist
// until grading time.  A submission scores here only by being right.
//
// The PRNG is written out rather than taken from math/rand.  The fresh cases have
// to be reproducible from their seed by whoever rebuilds this image later, and
// math/rand's stream is a property of the Go release that happens to be
// installed.  splitmix64 is eleven lines and pins the sequence to this file.

type rng struct{ state uint64 }

func newRNG(seed uint64) *rng { return &rng{state: seed} }

func (r *rng) next() uint64 {
	r.state += 0x9e3779b97f4a7c15
	z := r.state
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
	z = (z ^ (z >> 27)) * 0x94d049bb133111eb
	return z ^ (z >> 31)
}

// n returns a value in [0,limit).  Modulo is fine here: the bias at these limits
// is around one part in 2^58, and this is choosing test inputs, not keys.
func (r *rng) n(limit int) int {
	if limit <= 0 {
		return 0
	}
	return int(r.next() % uint64(limit))
}

func (r *rng) pick(xs []string) string { return xs[r.n(len(xs))] }
func (r *rng) chance(oneIn int) bool   { return r.n(oneIn) == 0 }

// -- composition -------------------------------------------------------------

// The scalar pool for composed documents.  Drawn from the same behavioural
// tables the systematic families use, because the point of the fresh cases is
// new *combinations*, not new atoms: an atom the reference and the port disagree
// about is worth finding once, and the frozen cases already look for it.
var randScalars = []string{
	"x", "y", "1", "0", "-1", "1.5", "true", "false", "yes", "no", "on", "off",
	"null", "~", "", "0x1f", "0o17", "0755", "1_000", ".inf", "-.inf", ".nan",
	"2001-12-14", "2001-12-14t21:59:43.10-05:00", "1:30", "a b c", "café",
	"你好", "\U0001f600", "a:b", "a#b", "@x", "*x", "&x", "!x", "%x",
	"- x", "? x", ": x", "x:", "x ", " x", "\tx", "a\nb", "'q'", "\"q\"",
	"a\\b", "<<", "===", "y", "n", "Y", "N", "True", "TRUE", "Null", "NULL",
	"0b101", "+1", "+.inf", "12:34:56", "very long " + strings.Repeat("word ", 12),
}

var randKeys = []string{
	"a", "b", "k", "key", "name", "1", "true", "null", "~", "on", "off",
	"a b", "a:b", "é", "\U0001f600", "", "0", "-", "?",
}

var randTags = []string{
	"", "!!str", "!!int", "!!float", "!!bool", "!!null", "!!seq", "!!map",
	"!!binary", "!!merge", "!local", "!<tag:example.com,2000:x>", "!",
	"!!timestamp", "!!set", "!!omap", "!e!suffix",
}

type composer struct {
	r       *rng
	b       strings.Builder
	anchors []string
	next    int
	// depth budget, so a composed document is a document rather than an
	// exhaustion test.  The limit family owns depth.
	budget int
}

func (c *composer) anchorName() string {
	c.next++
	name := fmt.Sprintf("a%d", c.next)
	c.anchors = append(c.anchors, name)
	return name
}

// scalarText renders one scalar in one of the four styles, choosing a style the
// content can actually survive.  A composer that emitted an invalid document
// nine times in ten would be a generator of error cases; those are wanted, but
// deliberately and at a controlled rate, not as the default outcome.
func (c *composer) scalarText(indent int) string {
	s := c.r.pick(randScalars)
	switch c.r.n(6) {
	case 0:
		return dq(s)
	case 1:
		if strings.Contains(s, "\n") {
			return dq(s)
		}
		return "'" + sq(s) + "'"
	case 2, 3:
		// Block scalar, if there is a line to put it on.
		if indent < 0 || strings.TrimSpace(s) == "" {
			return dq(s)
		}
		style := c.r.pick([]string{"|", ">", "|-", ">-", "|+", ">+"})
		pad := strings.Repeat(" ", indent+2)
		var body strings.Builder
		for _, line := range strings.Split(s, "\n") {
			body.WriteString(pad)
			body.WriteString(line)
			body.WriteByte('\n')
		}
		return style + "\n" + strings.TrimSuffix(body.String(), "\n")
	default:
		if plainSafe("plain map value", s) {
			return s
		}
		return dq(s)
	}
}

func (c *composer) prefix() string {
	var out strings.Builder
	if c.r.chance(6) {
		if tag := c.r.pick(randTags); tag != "" {
			out.WriteString(tag)
			out.WriteByte(' ')
		}
	}
	if c.r.chance(7) {
		out.WriteString("&" + c.anchorName() + " ")
	}
	return out.String()
}

// value writes a node at the given indent, inline after whatever the caller
// already wrote.  Returns nothing: the text accumulates in c.b, which keeps the
// indentation arithmetic in one place.
func (c *composer) value(indent int) {
	c.budget--
	if c.budget <= 0 {
		c.b.WriteString(c.scalarText(indent))
		return
	}
	// An alias, if there is anything to point at.  Aliases are what make a
	// composed document interesting to a port: the parser has to have kept the
	// anchor table, and the emitter has to decide whether to re-emit an alias or
	// expand it.
	if len(c.anchors) > 0 && c.r.chance(9) {
		c.b.WriteString("*" + c.anchors[c.r.n(len(c.anchors))])
		return
	}
	pre := c.prefix()
	switch c.r.n(10) {
	case 0, 1, 2, 3:
		c.b.WriteString(pre)
		c.b.WriteString(c.scalarText(indent))
	case 4, 5:
		// Block mapping.
		c.b.WriteString(pre)
		n := 1 + c.r.n(3)
		pad := strings.Repeat(" ", indent+2)
		for i := 0; i < n; i++ {
			c.b.WriteString("\n" + pad)
			c.writeKey()
			c.b.WriteString(":")
			if c.r.chance(8) {
				continue // an empty value, which resolves to null
			}
			c.b.WriteString(" ")
			c.value(indent + 2)
		}
	case 6, 7:
		// Block sequence.
		c.b.WriteString(pre)
		n := 1 + c.r.n(3)
		pad := strings.Repeat(" ", indent+2)
		for i := 0; i < n; i++ {
			c.b.WriteString("\n" + pad + "-")
			if c.r.chance(8) {
				continue
			}
			c.b.WriteString(" ")
			c.value(indent + 4)
		}
	case 8:
		// Flow sequence.  Flow context has no indentation to track, and a block
		// scalar cannot appear inside it, so -1 tells scalarText so.
		c.b.WriteString(pre + "[")
		n := c.r.n(4)
		for i := 0; i < n; i++ {
			if i > 0 {
				c.b.WriteString(", ")
			}
			c.flowValue()
		}
		c.b.WriteString("]")
	default:
		c.b.WriteString(pre + "{")
		n := c.r.n(3)
		for i := 0; i < n; i++ {
			if i > 0 {
				c.b.WriteString(", ")
			}
			c.writeKey()
			c.b.WriteString(": ")
			c.flowValue()
		}
		c.b.WriteString("}")
	}
}

func (c *composer) flowValue() {
	c.budget--
	if c.budget <= 0 {
		c.b.WriteString(dq(c.r.pick(randScalars)))
		return
	}
	if len(c.anchors) > 0 && c.r.chance(9) {
		c.b.WriteString("*" + c.anchors[c.r.n(len(c.anchors))])
		return
	}
	switch c.r.n(6) {
	case 0:
		c.b.WriteString("[")
		for i := 0; i < c.r.n(3); i++ {
			if i > 0 {
				c.b.WriteString(", ")
			}
			c.flowValue()
		}
		c.b.WriteString("]")
	case 1:
		c.b.WriteString("{")
		for i := 0; i < c.r.n(3); i++ {
			if i > 0 {
				c.b.WriteString(", ")
			}
			c.writeKey()
			c.b.WriteString(": ")
			c.flowValue()
		}
		c.b.WriteString("}")
	default:
		s := c.r.pick(randScalars)
		if c.r.chance(3) && plainSafe("plain flow seq", s) {
			c.b.WriteString(s)
		} else {
			c.b.WriteString(dq(s))
		}
	}
}

func (c *composer) writeKey() {
	k := c.r.pick(randKeys)
	switch {
	case c.r.chance(10):
		c.b.WriteString("? " + dq(k) + "\n" + strings.Repeat(" ", 0))
	case plainSafe("plain map value", k) && c.r.chance(2):
		c.b.WriteString(k)
	default:
		c.b.WriteString(dq(k))
	}
}

// composeDoc builds one whole input: optional directives, one to three
// documents, optional comments, optional end markers.
func composeDoc(r *rng) string {
	var out strings.Builder
	if r.chance(8) {
		out.WriteString("%YAML 1.1\n")
	}
	if r.chance(6) {
		out.WriteString("%TAG !e! tag:example.com,2000:\n")
	}
	docs := 1
	if r.chance(4) {
		docs = 1 + r.n(3)
	}
	explicit := out.Len() > 0 || docs > 1 || r.chance(3)
	for i := 0; i < docs; i++ {
		if r.chance(5) {
			out.WriteString("# comment " + fmt.Sprint(i) + "\n")
		}
		if explicit {
			out.WriteString("---")
			if r.chance(3) {
				out.WriteString(" ")
			} else {
				out.WriteString("\n")
			}
		}
		c := &composer{r: r, budget: 3 + r.n(6)}
		c.value(0)
		text := c.b.String()
		// A document body that begins with a newline is a block collection whose
		// first key is on the next line; at the top level that leading newline
		// is fine, but after `--- ` it would put the key in the wrong column.
		if strings.HasSuffix(out.String(), "--- ") && strings.HasPrefix(text, "\n") {
			text = strings.TrimPrefix(text, "\n")
		}
		out.WriteString(text)
		if !strings.HasSuffix(out.String(), "\n") {
			out.WriteString("\n")
		}
		if r.chance(6) {
			out.WriteString("...\n")
		}
		if r.chance(7) {
			out.WriteString("# trailing\n")
		}
	}
	return out.String()
}

// -- mutation ----------------------------------------------------------------

// Mutation is the other half, and it is the half that reaches the error paths.
// Composition produces documents that are almost always valid, because it builds
// them out of valid pieces; a parser's error reporting is a third of its
// observable behaviour and a composed document barely touches it.  A byte-level
// edit to a valid document lands on token boundaries, inside escapes, in the
// middle of indentation -- the places where the diagnostics live.
//
// Edits are on runes rather than bytes, and the interesting invalid-UTF-8 cases
// are produced deliberately instead: a random byte flip inside a multi-byte
// sequence would make almost every mutated case a UTF-8 error, which is one
// behaviour, repeated.
// The non-ASCII entries are written as escapes rather than as literals.  A BOM
// or a U+2028 typed into a source file is a character a reader cannot see and a
// tool may reject outright, and this table is the one place where what the byte
// is has to be unambiguous.
var mutationChars = []string{
	" ", "\t", "\n", "\r", ":", "-", "?", ",", "[", "]", "{", "}", "#", "&",
	"*", "!", "|", ">", "'", "\"", "%", "@", "`", "\\", "0", "a", ".", "=",
	"\u00e9", "\U0001f600", "\ufeff", "\u2028", "\u0085", "\u00a0",
}

func mutate(r *rng, src string) string {
	runes := []rune(src)
	if len(runes) == 0 {
		return r.pick(mutationChars)
	}
	edits := 1 + r.n(3)
	for i := 0; i < edits; i++ {
		if len(runes) == 0 {
			runes = []rune(r.pick(mutationChars))
			continue
		}
		at := r.n(len(runes))
		switch r.n(4) {
		case 0: // delete
			runes = append(runes[:at], runes[at+1:]...)
		case 1: // insert
			ins := []rune(r.pick(mutationChars))
			runes = append(runes[:at], append(ins, runes[at:]...)...)
		case 2: // replace
			ins := []rune(r.pick(mutationChars))
			tail := append(ins, runes[at+1:]...)
			runes = append(runes[:at], tail...)
		default: // duplicate a span, which is how indentation gets broken
			end := at + 1 + r.n(4)
			if end > len(runes) {
				end = len(runes)
			}
			span := append([]rune{}, runes[at:end]...)
			runes = append(runes[:end], append(span, runes[end:]...)...)
		}
	}
	return string(runes)
}

// freshCases builds the fresh set.  Half composed, half mutated from the
// systematic cases, and every case observed under one op chosen at random --
// one rather than six, because the fresh cases are graded live and their cost
// is paid out of the verifier's wall clock.
func freshCases(seed uint64, count int, seeds []string) *gen {
	r := newRNG(seed)
	g := &gen{seen: map[string]bool{}}
	ops := []string{"node", "emit", "stream", "emit_stream", "roundtrip", "emit_indent"}

	for len(g.cases) < count {
		var src, fam string
		if r.chance(2) && len(seeds) > 0 {
			src = mutate(r, seeds[r.n(len(seeds))])
			fam = "fresh-mutated"
		} else {
			src = composeDoc(r)
			fam = "fresh-composed"
		}
		op := r.pick(ops)
		var indent *int
		if op == "emit_indent" {
			v := r.n(12)
			indent = &v
		}
		before := len(g.cases)
		g.add(fam, op, fmt.Sprintf("seed=%d/%d", seed, len(g.cases)+1), src, indent)
		if len(g.cases) == before {
			// A duplicate.  The generator keeps going rather than counting it as
			// progress, and cannot loop forever: composeDoc draws from a space
			// far larger than any count this is called with.
			continue
		}
	}
	return g
}
