package main

import (
	"fmt"
	"strings"
)

// Tags: shorthand, verbatim, local, and the %TAG directive that rewrites a
// handle.  shortTag/longTag round-trip through "tag:yaml.org,2002:", so a tag
// written verbatim in its long form comes back out in short form.
func familyTag(g *gen) {
	const fam = "tag"

	tags := []string{
		"!!str", "!!int", "!!float", "!!bool", "!!null", "!!timestamp",
		"!!binary", "!!merge", "!!seq", "!!map", "!!set", "!!omap",
		"!!python/none", "!!unknown", "!!", "!",
		"!local", "!local-tag", "!local/path", "!x!y",
		"!<tag:yaml.org,2002:str>", "!<tag:yaml.org,2002:int>",
		"!<!local>", "!<x>", "!<>", "!<tag:example.com,2000:foo>",
		"!e!foo", "!e!",
	}
	scalars := []string{"1", "abc", "", "true", "~", "1.5"}

	for _, tag := range tags {
		for _, s := range scalars {
			src := "k: " + tag + " " + s + "\n"
			if s == "" {
				src = "k: " + tag + "\n"
			}
			g.nodeEmit(fam, fmt.Sprintf("%s %q", tag, s), src)
		}
		// On a collection, where the tag lands on the seq or map node rather
		// than on a scalar.
		g.nodeEmit(fam, tag+" on seq", "k: "+tag+"\n  - 1\n")
		g.nodeEmit(fam, tag+" on flow seq", "k: "+tag+" [1]\n")
		g.nodeEmit(fam, tag+" on map", "k: "+tag+"\n  j: 1\n")
		g.nodeEmit(fam, tag+" on doc", "--- "+tag+"\n1\n")
	}

	// Tag and anchor together, in both orders.
	for _, tag := range []string{"!!str", "!local", "!<x>"} {
		g.nodeEmit(fam, "anchor-then-tag "+tag, "k: &a "+tag+" 1\n")
		g.nodeEmit(fam, "tag-then-anchor "+tag, "k: "+tag+" &a 1\n")
	}

	// %TAG directives: a handle defined and used, defined and unused, redefined,
	// and the two reserved handles.
	directives := []string{
		"%TAG !e! tag:example.com,2000:\n---\n!e!foo x\n",
		"%TAG !e! tag:example.com,2000:\n---\nx\n",
		"%TAG ! tag:example.com,2000:\n---\n!foo x\n",
		"%TAG !! tag:example.com,2000:\n---\n!!foo x\n",
		"%TAG !e! !local-\n---\n!e!foo x\n",
		"%TAG !e! tag:example.com,2000:\n%TAG !f! tag:other,2000:\n---\n[!e!a, !f!b]\n",
		"%TAG !e! tag:example.com,2000:\n---\n!e!foo x\n--- \n!e!bar y\n",
	}
	for _, s := range directives {
		g.nodeEmit(fam, fmt.Sprintf("directive %q", s), s)
		g.add(fam, "stream", fmt.Sprintf("stream %q", s), s, nil)
	}

	// Binary, which resolve.go decodes only in the typed path -- on the node
	// surface the base64 text stays as written, which is exactly the kind of
	// thing a port might over-implement.
	for _, s := range []string{
		"k: !!binary aGk=\n",
		"k: !!binary |\n  aGk=\n",
		"k: !!binary not-base64\n",
		"k: !!binary \"\"\n",
	} {
		g.nodeEmit(fam, fmt.Sprintf("binary %q", s), s)
	}
}

// Comments are go-yaml v3's most intricate addition over libyaml: every comment
// is attached to a node as head, line or foot, and the rules for which one it
// lands on depend on blank lines and on indentation.
func familyComment(g *gen) {
	const fam = "comment"

	shapes := []string{
		"# head\nk: v\n",
		"k: v # line\n",
		"k: v\n# foot\n",
		"# head\nk: v # line\n# foot\n",
		"# a\n# b\nk: v\n",
		"# a\n\n# b\nk: v\n",
		"k: v\n\n# foot after blank\n",
		"k: v\n# foot\n\nj: w\n",
		"k: v\n\n# head of j\nj: w\n",
		"a: 1 # one\nb: 2 # two\n",
		"# doc head\n---\nk: v\n",
		"---\n# after marker\nk: v\n",
		"k: v\n...\n# after end\n",
		"# only a comment\n",
		"#\n",
		"# \n",
		"#no space\n",
		"    # indented\nk: v\n",
		"k:\n  # head of nested\n  j: v\n",
		"k:\n  j: v\n  # foot of nested\n",
		"- # line on entry\n  1\n",
		"- 1 # line\n- 2\n",
		"# head\n- 1\n",
		"a:\n# head at outer indent\n  b: 1\n",
		"a:\n  # head at inner indent\n  b: 1\n",
		"[1, # in flow\n 2]\n",
		"{k: v # in flow map\n}\n",
		"? k # on explicit key\n: v\n",
		"k: # line on key with empty value\n",
		"k: |\n  x\n# foot after block\n",
		"k: > # line on folded header\n  x\n",
		"a: 1\n\n\n# two blanks before\nb: 2\n",
		"a: 1\n# between\n# two comments\nb: 2\n",
		"# 1\na: 1\n# 2\nb: 2\n# 3\n",
	}
	for _, s := range shapes {
		g.nodeEmit(fam, fmt.Sprintf("%q", s), s)
		// Comments are the part of the tree most likely to move on a
		// round-trip, so every shape is also checked for emitter stability.
		g.add(fam, "roundtrip", fmt.Sprintf("rt %q", s), s, nil)
	}
}

// Documents, markers and directives, observed through `stream` so every
// document in the input is graded rather than just the first.
func familyDocument(g *gen) {
	const fam = "document"

	shapes := []string{
		"a\n",
		"---\na\n",
		"--- a\n",
		"---\n",
		"---\n---\n",
		"--- a\n--- b\n",
		"a\n---\nb\n",
		"...\n",
		"a\n...\n",
		"a\n...\nb\n",
		"a\n...\n---\nb\n",
		"--- a\n...\n--- b\n",
		"---a\n",
		"----\n",
		"--- \n",
		"---\t\n",
		"... \n",
		"...x\n",
		"a\n... \nb\n",
		"%YAML 1.1\n---\na\n",
		"%YAML 1.2\n---\na\n",
		"%YAML 1.0\n---\na\n",
		"%YAML 2.0\n---\na\n",
		"%YAML 1.1\n%YAML 1.1\n---\na\n",
		"%YAML 1.1\n---\na\n---\nb\n",
		"%FOO bar\n---\na\n",
		"%YAML 1.1\na\n",
		"\n\n---\na\n",
		"\n",
		"",
		" ",
		"\n\n\n",
		"# comment only\n",
		"---\n# comment only doc\n",
		"--- |\n  x\n",
		"--- >\n  x\n",
		"--- !!str\n",
		"--- &a 1\n",
		"--- *a\n",
		"--- [1]\n--- {k: v}\n",
		"a: 1\n--- \nb: 2\n...\n--- \nc: 3\n",
	}
	for _, s := range shapes {
		g.add(fam, "stream", fmt.Sprintf("%q", s), s, nil)
		g.add(fam, "emit_stream", fmt.Sprintf("emit %q", s), s, nil)
		g.node(fam, fmt.Sprintf("first %q", s), s)
	}

	// Many documents, to check the separator logic rather than the parse.
	for _, n := range []int{2, 3, 10} {
		var b strings.Builder
		for i := 0; i < n; i++ {
			fmt.Fprintf(&b, "--- doc%d\n", i)
		}
		g.add(fam, "stream", fmt.Sprintf("%d docs", n), b.String(), nil)
		g.add(fam, "emit_stream", fmt.Sprintf("emit %d docs", n), b.String(), nil)
	}
}
