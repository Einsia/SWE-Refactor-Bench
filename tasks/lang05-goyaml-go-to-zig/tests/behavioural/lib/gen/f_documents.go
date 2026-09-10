package main

import (
	"encoding/json"
	"fmt"
)

// The document manifest names the twelve whole-file inputs and the ops each one
// is observed under.  They are graded by digest rather than by bytes: the node
// tree of a real Kubernetes manifest is megabytes of JSON, and shipping those
// bytes in an image to compare them once is not a good trade.
//
// They are not redundant with the generated cases either.  A generated case
// isolates one axis, which is what makes a failure diagnosable; a real file
// crosses forty axes in one document, which is where interactions between them
// surface.  A port can pass every isolated case and still mis-indent a sequence
// nested inside a mapping inside a document with a %TAG directive.

// documentFiles is the set shipped in tests/behavioural/data/documents.  Each
// entry says what the file is for, so a reader can tell whether the set still
// covers what it claims.
var documentFiles = []struct {
	name string
	why  string
}{
	{"k8s-deployment.yaml", "multi-document manifests: nested maps, block scalars, quoted ints"},
	{"docker-compose.yaml", "anchors, aliases, merge keys, port strings that look sexagesimal"},
	{"github-workflow.yaml", "bool-looking keys, shell in block scalars, flow matrices"},
	{"openapi.yaml", "deep nesting, folded prose, refs, mixed number forms"},
	{"ansible-playbook.yaml", "templates inside quotes, yes/no spellings, seqs of maps"},
	{"gitlab-ci.yaml", "anchor reuse with merges, script arrays, hidden keys"},
	{"comments.yaml", "head, line and foot comments at every position"},
	{"unicode-locale.yaml", "scripts, emoji, combining marks, RTL, escape forms"},
	{"edge-cases.yaml", "tags, directives, explicit and complex keys, sets, omaps"},
	{"inventory.yaml", "wide and repetitive, large enough to span response blocks"},
	{"types.yaml", "the resolver's whole spelling table in one document"},
	{"stream-multi.yaml", "many documents, per-document directives, end markers"},
}

// documentOps is applied to every document.  emit_indent is included at one
// width only: the three-way SetIndent fallback is already enumerated case by case
// in the indent family, and what a document adds is the interaction of a
// non-default indent with deep real structure, which one width shows as well as
// six.
var documentOps = []struct {
	op     string
	indent *int
}{
	{"node", nil},
	{"emit", nil},
	{"emit_indent", intp(4)},
	{"stream", nil},
	{"emit_stream", nil},
	{"roundtrip", nil},
}

func intp(n int) *int { return &n }

func documentManifest() []byte {
	type entry struct {
		Key    string `json:"key"`
		File   string `json:"file"`
		Op     string `json:"op"`
		Indent *int   `json:"indent,omitempty"`
		Why    string `json:"why"`
	}
	var out []entry
	for _, f := range documentFiles {
		for _, o := range documentOps {
			key := f.name + "|" + o.op
			if o.indent != nil {
				key += fmt.Sprintf("|%d", *o.indent)
			}
			out = append(out, entry{
				Key: key, File: f.name, Op: o.op, Indent: o.indent, Why: f.why,
			})
		}
	}
	blob, err := json.MarshalIndent(map[string]any{"documents": out}, "", " ")
	if err != nil {
		panic(err)
	}
	return append(blob, '\n')
}
