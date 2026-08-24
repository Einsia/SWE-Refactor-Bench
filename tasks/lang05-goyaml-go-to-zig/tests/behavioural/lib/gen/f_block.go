package main

import (
	"fmt"
	"strings"
)

// Block scalars are a three-way cross: style, chomping, and an optional
// explicit indentation indicator.  The emitter recomputes the indicator against
// its own indent -- ">2" read at column 5 comes back out as ">4" -- so every
// case is observed both as a tree and as re-emitted text.
func familyScalarBlock(g *gen) {
	const fam = "scalar-block"

	bodies := []struct {
		label string
		lines []string
	}{
		{"one", []string{"x"}},
		{"two", []string{"x", "y"}},
		{"blank-between", []string{"x", "", "y"}},
		{"two-blank-between", []string{"x", "", "", "y"}},
		{"trailing-blank", []string{"x", ""}},
		{"trailing-blanks", []string{"x", "", ""}},
		{"leading-blank", []string{"", "x"}},
		{"more-indented", []string{"x", "  y", "z"}},
		{"deeper-first", []string{"  x", "y"}},
		{"tabbed", []string{"x\ty"}},
		{"trailing-space", []string{"x "}},
		{"only-blank", []string{""}},
		{"hash", []string{"# not a comment"}},
		{"dash", []string{"- not a sequence"}},
		{"colon", []string{"k: not a mapping"}},
		{"quote", []string{"\"not quoted\""}},
		{"backslash", []string{"a\\nb"}},
	}

	for _, style := range []string{"|", ">"} {
		for _, chomp := range []string{"", "-", "+"} {
			for _, body := range bodies {
				var b strings.Builder
				fmt.Fprintf(&b, "k: %s%s\n", style, chomp)
				for _, ln := range body.lines {
					if ln == "" {
						b.WriteString("\n")
					} else {
						b.WriteString("  " + ln + "\n")
					}
				}
				label := fmt.Sprintf("%s%s %s", style, chomp, body.label)
				g.nodeEmit(fam, label, b.String())
			}
		}
	}

	// Explicit indentation indicators, including the case where the indicator
	// disagrees with the actual indentation and the case where content is
	// indented further than the indicator claims.
	for _, ind := range []string{"1", "2", "3", "4", "9"} {
		for _, chomp := range []string{"", "-", "+"} {
			for _, style := range []string{"|", ">"} {
				for _, body := range []string{"  x\n", "   x\n", "    x\n  y\n", "\n  x\n"} {
					src := fmt.Sprintf("k: %s%s%s\n%s", style, ind, chomp, body)
					g.nodeEmit(fam, fmt.Sprintf("%s%s%s body=%q", style, ind, chomp, body), src)
				}
			}
		}
	}

	// Indicator order: YAML allows chomp-then-indent as well as
	// indent-then-chomp.
	for _, head := range []string{"|-2", "|2-", "|+2", "|2+", ">-2", ">2-"} {
		g.nodeEmit(fam, "indicator order "+head, "k: "+head+"\n  x\n")
	}

	// A block scalar as the whole document, and in a sequence, where the
	// indentation baseline differs.
	for _, style := range []string{"|", ">", "|-", ">-", "|+", ">+"} {
		g.nodeEmit(fam, "doc "+style, style+"\n  x\n  y\n")
		g.nodeEmit(fam, "in-seq "+style, "- "+style+"\n    x\n    y\n")
		g.nodeEmit(fam, "nested "+style, "a:\n  b: "+style+"\n      x\n      y\n")
	}

	// Folding rules for ">": a more-indented line is not folded, and a blank
	// line becomes a newline rather than a space.
	folds := []string{
		"  a\n  b\n", "  a\n\n  b\n", "  a\n   b\n", "  a\n  b\n\n  c\n",
		"  a\n\n\n  b\n", "   a\n  b\n", "  a \n  b\n", "  a\n  b \n",
	}
	for i, body := range folds {
		g.nodeEmit(fam, fmt.Sprintf("fold %d", i), "k: >\n"+body)
		g.nodeEmit(fam, fmt.Sprintf("literal %d", i), "k: |\n"+body)
	}
}

// Block collections: the indentation and compact-notation rules, plus explicit
// key syntax.
func familyBlockCollection(g *gen) {
	const fam = "block-collection"

	shapes := []string{
		"- 1\n- 2\n",
		"- 1\n-\n- 3\n",
		"-\n-\n",
		"- - 1\n",
		"- - - 1\n",
		"- a: 1\n",
		"- a: 1\n  b: 2\n",
		"- a: 1\n- b: 2\n",
		"- - a: 1\n",
		"a: 1\nb: 2\n",
		"a:\nb:\n",
		"a:\n  b: 1\n",
		"a:\n  - 1\n",
		"a:\n- 1\n",
		"a:\n   - 1\n",
		"a:\n  b:\n    c: 1\n",
		"a: 1\nb:\n  - 2\n  - c: 3\n",
		"? a\n: b\n",
		"? a\n",
		"?\n: b\n",
		"? a\n: b\n? c\n: d\n",
		"? a\nb: c\n",
		"a: b\n? c\n: d\n",
		"? >\n  a\n: b\n",
		"? [a]\n: b\n",
		"? {a: 1}\n: b\n",
		"a: b\n\nc: d\n",
		"a: b\n \nc: d\n",
		"  a: 1\n  b: 2\n",
		"a:\n\n  b: 1\n",
		"- 1\n\n- 2\n",
		"a: -1\n",
		"a: -\n",
		"a:- 1\n",
		"a :b\n",
		"a\t: b\n",
		"a: b\t\n",
		"a:  b\n",
		"a:\t b\n",
	}
	for _, s := range shapes {
		g.nodeEmit(fam, fmt.Sprintf("%q", s), s)
	}

	// Deeper nesting at several widths, because the emitter's indent decision
	// and the scanner's indent tracking are separate mechanisms.
	for _, width := range []int{1, 2, 3, 4, 8} {
		pad := strings.Repeat(" ", width)
		for depth := 1; depth <= 5; depth++ {
			var b strings.Builder
			for i := 0; i < depth; i++ {
				b.WriteString(strings.Repeat(pad, i) + fmt.Sprintf("k%d:\n", i))
			}
			b.WriteString(strings.Repeat(pad, depth) + "leaf: 1\n")
			g.nodeEmit(fam, fmt.Sprintf("map w=%d d=%d", width, depth), b.String())

			var s strings.Builder
			for i := 0; i < depth; i++ {
				s.WriteString(strings.Repeat(pad, i) + "-\n")
			}
			s.WriteString(strings.Repeat(pad, depth) + "- 1\n")
			g.nodeEmit(fam, fmt.Sprintf("seq w=%d d=%d", width, depth), s.String())
		}
	}
}
