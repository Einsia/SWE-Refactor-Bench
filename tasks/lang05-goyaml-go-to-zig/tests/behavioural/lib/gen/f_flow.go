package main

import (
	"fmt"
	"strings"
)

// Flow context has its own scanner rules: a plain scalar ends at , [ ] { }, the
// ": " requirement relaxes for JSON-like keys, and entries may be empty.
func familyFlow(g *gen) {
	const fam = "flow"

	shapes := []string{
		"[]", "[1]", "[1, 2]", "[1,2]", "[ 1 , 2 ]", "[1, 2,]", "[,]", "[, 1]",
		"[1, , 2]", "[[]]", "[[1]]", "[[1], [2]]", "[[[[1]]]]",
		"{}", "{k: v}", "{k: v, j: w}", "{k: v,}", "{,}", "{k}", "{k, j}",
		"{k: }", "{: v}", "{:}", "{k:v}", "{k :v}", "{ k : v }",
		"{k: {j: v}}", "{k: [1]}", "[{k: v}]", "[{k: v}, {j: w}]",
		"{? k: v}", "{? k}", "{? : v}", "[? k]",
		"{\"k\": v}", "{'k': v}", "{\"k\":v}", "{1: v}", "{true: v}", "{null: v}",
		"[a b, c]", "[a: b]", "[a: b, c: d]", "[a: ]", "[: b]",
		"{k: v} ", " {k: v}", "[1]\n", "[1] \n",
		"[1,\n2]", "[1,\n 2]", "[\n1,\n2\n]", "{\nk: v\n}", "{k:\nv}",
		"[a\nb]", "[a\n\nb]", "['a\nb']", "[\"a\nb\"]",
		"[# c\n1]", "[1 # c\n]", "[1, # c\n2]", "{k: v # c\n}",
		"[*a]", "[&a 1]", "[!!str 1]", "[!x 1]",
		"[1, [2, [3, [4]]]]", "{a: {b: {c: {d: 1}}}}",
		"[a, {b: [c, {d: e}]}]",
		"[:]", "[::]", "[a::b]", "[a:b:c]",
		"[]\n[]", "[1] [2]",
	}
	for _, s := range shapes {
		src := s
		if !strings.HasSuffix(src, "\n") {
			src += "\n"
		}
		g.nodeEmit(fam, fmt.Sprintf("%q", s), src)
		// Nested under a key as well: the flow scanner is entered from block
		// context there, and the emitter has to decide indentation for it.
		g.nodeEmit(fam, fmt.Sprintf("under-key %q", s), "k: "+s+"\n")
	}

	// Flow inside block inside flow, to exercise the state stack rather than a
	// single level.
	for _, s := range []string{
		"a:\n  - [1, {b: 2}]\n  - {c: [3, 4]}\n",
		"- [a, b]\n- {c: d}\n",
		"[{a: [b, {c: d}]}]",
		"{a: [{b: c}, [d]]}",
	} {
		src := s
		if !strings.HasSuffix(src, "\n") {
			src += "\n"
		}
		g.nodeEmit(fam, fmt.Sprintf("mixed %q", s), src)
	}
}

// Anchors and aliases, including the shapes that make a naive recursive walker
// loop: an anchor whose alias is inside its own value.
func familyAnchorAlias(g *gen) {
	const fam = "anchor-alias"

	shapes := []string{
		"a: &x 1\nb: *x\n",
		"&x 1\n",
		"- &x 1\n- *x\n",
		"- &x 1\n- *x\n- *x\n",
		"a: &x\n  b: 1\nc: *x\n",
		"a: &x [1]\nb: *x\n",
		"a: &x {k: v}\nb: *x\n",
		"&x [*x]\n",
		"&x {k: *x}\n",
		"&x *x\n",
		"a: &x\n  b: *x\n",
		"&a &b 1\n",
		"&x 1\n&x 2\n",
		"a: &x 1\nb: &x 2\nc: *x\n",
		"*x\n",
		"a: *x\nb: &x 1\n",
		"&x\n",
		"&x\n1\n",
		"a: &x !!str 1\nb: *x\n",
		"a: !!str &x 1\nb: *x\n",
		"a: &x1 1\nb: *x1\n",
		"a: &x-y 1\nb: *x-y\n",
		"a: &x_y 1\nb: *x_y\n",
		"a: &1 1\nb: *1\n",
		"[&x 1, *x]",
		"{&x k: v, j: *x}",
		"{k: &x v, j: *x}",
		"a: &x 1\nb: *x extra\n",
		"a: &x 1\nb: [*x]\n",
		"? &x k\n: *x\n",
		"<<: {a: 1}\n",
		"a: &r {x: 1}\nb:\n  <<: *r\n",
		"a: &r {x: 1}\nb:\n  <<: *r\n  y: 2\n",
		"a: &r {x: 1}\nb:\n  y: 2\n  <<: *r\n",
		"a: &r {x: 1}\nb: &s {y: 2}\nc:\n  <<: [*r, *s]\n",
		"<<: *nope\n",
		"a: !!merge <<\n",
	}
	for _, s := range shapes {
		src := s
		if !strings.HasSuffix(src, "\n") {
			src += "\n"
		}
		g.nodeEmit(fam, fmt.Sprintf("%q", s), src)
		g.add(fam, "roundtrip", fmt.Sprintf("rt %q", s), src, nil)
	}

	// Many aliases to one anchor: the emitter has to keep emitting the alias
	// rather than expanding it, and the count is what a port that expands gets
	// wrong.
	for _, n := range []int{2, 5, 20} {
		var b strings.Builder
		b.WriteString("base: &b {k: v}\nuses:\n")
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "  - *b\n")
		}
		g.nodeEmit(fam, fmt.Sprintf("%d aliases", n), b.String())
	}
}
