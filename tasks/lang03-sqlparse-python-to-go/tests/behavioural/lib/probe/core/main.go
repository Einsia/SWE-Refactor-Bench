// Code generated from _work/lang03/probe-monolith.go by split_emit.py. DO NOT EDIT.
//
// The core probe tier: the top-level API only: the smallest surface that can be graded
//
// This tier names only the root package, and it means it: the binary does
// not import sql, so it reaches Parse's statements through a one-method
// interface. A submission whose sql.Node is missing every accessor still
// answers every case here, which is what the split exists to show.
package main

import (
	"bytes"
	"strings"

	sqlparse "github.com/andialbrecht/sqlparse-go"
	"probe/probeutil"
)

func main() {
	r := probeutil.NewRegistry()
	r.Register("rt", opRT)
	r.Register("rt-enc", opRTEnc)
	r.Register("rt-raw", opRTRaw)
	r.Register("split", opSplit)
	r.Register("split-strip", opSplitStrip)
	r.Register("parsestream", opParseStream)
	r.Register("fmt", opFmt)
	r.Register("tostr", opToStr)
	r.Register("err-encoding", opErrEncoding)
	r.Register("version", opVersion)
	r.RenderError = portableError
	probeutil.Main(r)
}

// statement is the core tier's view of a parsed statement.
//
// The tier imports the root package and nothing else, so it cannot write
// `*sql.Node`. It does not need to: Go requires an import to name a type, not to
// call a method on a value of it, and the two methods below are the only ones
// the ten core ops use. Both are contracted on *Node.
//
// The consequence is the point of the tier: a submission whose sql package does
// not compile is still graded on everything the root package can answer.
type statement interface {
	String() string
	TypeName() string
}

// views converts a slice of concrete statements to the local view.
//
// Generic over the element type so the concrete type is never written down; a
// []T does not convert to []statement implicitly, and a loop is the whole cost.
func views[T statement](in []T) []statement {
	out := make([]statement, len(in))
	for i, v := range in {
		out[i] = v
	}
	return out
}

func opVersion(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	out := &probeutil.Answer{}
	out.Add("version\t%s", probeutil.EncField(sqlparse.Version))
	return out, nil
}

// parseDoc is the shared entry: bytes plus declared encoding when the case
// carries one, plain string otherwise. Which entry point a case uses is part of
// what it grades, so this mirrors probe.py's branch exactly.
func parseDoc(c *probeutil.Context, a *probeutil.Args, i int) ([]statement, string, error) {
	id, err := a.DocID(i)
	if err != nil {
		return nil, "", err
	}
	enc, err := a.DocEncoding(i)
	if err != nil {
		return nil, "", err
	}
	data, err := c.DocBytes(id)
	if err != nil {
		return nil, "", err
	}
	if enc != "" {
		nodes, perr := sqlparse.ParseBytes(data, enc)
		return views(nodes), string(data), perr
	}
	text, terr := c.DocText(id)
	if terr != nil {
		return nil, "", terr
	}
	nodes, perr := sqlparse.Parse(text)
	return views(nodes), text, perr
}

func opRT(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	nodes, source, err := parseDoc(c, a, 0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(nodes))
	var joined strings.Builder
	for i, node := range nodes {
		text := node.String()
		joined.WriteString(text)
		out.Add("stmt\t%d\t%d\t%s", i, probeutil.RuneLen(text), probeutil.EncField(text))
	}
	total := joined.String()
	lossless := 0
	if total == source {
		lossless = 1
	}
	out.Add("lossless\t%d", lossless)
	if total != source {
		out.Add("joined\t%s", probeutil.EncField(total))
	}
	return out, nil
}

func opRTRaw(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	data, err := a.Doc(0)
	if err != nil {
		return nil, err
	}
	// No declared encoding: the submission's own fallback decides. The
	// reference tries UTF-8 then unicode-escape, and whichever it lands on is
	// observable in the token values.
	nodes, perr := sqlparse.ParseBytes(data, "")
	if perr != nil {
		return nil, perr
	}
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(nodes))
	for i, node := range nodes {
		text := node.String()
		out.Add("stmt\t%d\t%d\t%s", i, probeutil.RuneLen(text), probeutil.EncField(text))
	}
	return out, nil
}

func opToStr(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	nodes, _, err := parseDoc(c, a, 0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	if len(nodes) == 0 {
		out.Add("len\tnone")
		return out, nil
	}
	for i, node := range nodes {
		text := node.String()
		// Both units, deliberately: this is the case that separates a port
		// counting code points from one counting bytes.
		out.Add("len\t%d\t%d\t%d", i, probeutil.RuneLen(text), len(text))
	}
	return out, nil
}

func opSplit(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	id, err := a.DocID(0)
	if err != nil {
		return nil, err
	}
	enc, err := a.DocEncoding(0)
	if err != nil {
		return nil, err
	}
	var parts []string
	if enc != "" {
		data, derr := c.DocBytes(id)
		if derr != nil {
			return nil, derr
		}
		parts, err = sqlparse.SplitBytes(data, enc, false)
	} else {
		text, terr := c.DocText(id)
		if terr != nil {
			return nil, terr
		}
		parts, err = sqlparse.Split(text, false)
	}
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(parts))
	for i, p := range parts {
		out.Add("part\t%d\t%d\t%s", i, probeutil.RuneLen(p), probeutil.EncField(p))
	}
	return out, nil
}

func opSplitStrip(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	data, err := a.Doc(0)
	if err != nil {
		return nil, err
	}
	parts, serr := sqlparse.SplitBytes(data, "", true)
	if serr != nil {
		return nil, serr
	}
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(parts))
	for i, p := range parts {
		out.Add("part\t%d\t%d\t%s", i, probeutil.RuneLen(p), probeutil.EncField(p))
	}
	return out, nil
}

func opParseStream(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	id, err := a.DocID(0)
	if err != nil {
		return nil, err
	}
	enc, err := a.DocEncoding(0)
	if err != nil {
		return nil, err
	}
	data, err := c.DocBytes(id)
	if err != nil {
		return nil, err
	}
	nodes, perr := sqlparse.ParseStream(bytes.NewReader(data), enc)
	if perr != nil {
		return nil, perr
	}
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(nodes))
	for i, node := range nodes {
		out.Add("stmt\t%d\t%s\t%s", i, probeutil.EncField(node.TypeName()),
			probeutil.EncField(node.String()))
	}
	return out, nil
}

func opFmt(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	name, err := a.Name(0)
	if err != nil {
		return nil, err
	}
	preset, ok := c.Spec.FormatPresets[name]
	if !ok {
		return nil, probeutil.Defectf("unknown format preset: %s", name)
	}
	options, err := probeutil.DecodeOptions(preset)
	if err != nil {
		return nil, err
	}
	id, err := a.DocID(1)
	if err != nil {
		return nil, err
	}
	enc, err := a.DocEncoding(1)
	if err != nil {
		return nil, err
	}
	var result string
	var ferr error
	if enc != "" {
		data, derr := c.DocBytes(id)
		if derr != nil {
			return nil, derr
		}
		result, ferr = sqlparse.FormatBytes(data, enc, options)
	} else {
		text, terr := c.DocText(id)
		if terr != nil {
			return nil, terr
		}
		result, ferr = sqlparse.Format(text, options)
	}
	out := &probeutil.Answer{}
	if ferr != nil {
		// The error is the answer. The reference raises SQLParseError with a
		// specific message for several of these presets, and the contract says
		// Error.Error() reproduces it.
		out.Add("status\terr")
		for _, line := range errorLines(ferr) {
			out.Add("%s", line)
		}
		return out, nil
	}
	out.Add("status\tok")
	out.Add("len\t%d", probeutil.RuneLen(result))
	out.Add("out\t%s", probeutil.EncField(result))
	return out, nil
}

// errorLines renders a submission error as the lines probe.py's error_lines
// produces, in the same order and under the same field names.
//
// Three shapes, and which one a failure gets is part of the contract. The
// not-implemented test comes first: Python's NotImplementedError and
// SQLParseError are disjoint classes, but a Go port may return one value that is
// both, and checking *Error first would then answer SQLParseError where the
// reference answers not-implemented -- failing a correct port. The contract
// fixes this order and says so.
//
// A previous version of this returned only a type name, and returned
// "SQLParseError" from both branches of its own type assertion -- so every error
// answered SQLParseError under the field name `type`, where the reference emits
// `kind`. That is 114 catalog cases, none of which any port could pass.
func errorLines(err error) []string {
	if sqlparse.IsNotImplemented(err) {
		return []string{"kind\tnot-implemented"}
	}
	var target *sqlparse.Error
	if errorsAs(err, &target) {
		// The message is graded verbatim: it is the library's published API and
		// the contract requires Error() to reproduce the reference's wording.
		return []string{"kind\tSQLParseError",
			"message\t" + probeutil.EncField(err.Error())}
	}
	// Anything else is whatever the runtime raised, reported as a category so
	// the comparison stays a comparison of behavior rather than of wording. No
	// message line: an absent field cannot disagree.
	return []string{"kind\t" + probeutil.ErrorCategory(err)}
}

// errorsAs is errors.As, spelled out to keep the import list to the packages
// the contract names plus the standard library the protocol needs.
func errorsAs(err error, target **sqlparse.Error) bool {
	for err != nil {
		if e, ok := err.(*sqlparse.Error); ok {
			*target = e
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}

func opErrEncoding(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	data, err := a.Doc(0)
	if err != nil {
		return nil, err
	}
	enc, err := a.Text(1)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	if _, perr := sqlparse.ParseBytes(data, enc); perr != nil {
		out.Add("status\terr")
		out.Add("category\t%s", probeutil.ErrorCategory(perr))
		return out, nil
	}
	out.Add("status\tok")
	return out, nil
}

// portableError mirrors probe.py's _portable_error.
//
// The library's own error is reported verbatim, because the contract requires
// Error.Error() to reproduce the reference's SQLParseError message. Anything
// else is reported as a category: a runtime failure's wording is the runtime's,
// and grading it would put a case on one side of the pair that the other side
// cannot answer.
// This is the Registry.RenderError hook, assigned in each tier's main. The hook
// exists because probeutil must not import the submission -- the library's error
// type lives there -- so probeutil's own default can only render a category, and
// a tier that leaves the hook unassigned silently answers that default. Three
// tiers did.
func portableError(err error) string {
	// Ordered as errorLines is, and for the same reason: a port may return one
	// value that is both its *Error and a not-implemented signal, and the
	// reference answers not-implemented for that condition.
	if sqlparse.IsNotImplemented(err) {
		return "category\tnot-implemented"
	}
	var target *sqlparse.Error
	if errorsAs(err, &target) {
		return "SQLParseError\t" + target.Error()
	}
	if strings.HasPrefix(err.Error(), "panic:") {
		return "category\tpanic"
	}
	return "category\t" + probeutil.ErrorCategory(err)
}

// opRTEnc round trips under an encoding named by the caller, not by the
// document.
//
// The Go half of probe.py's op of the same name. `rt` and `rt-raw` between them
// only ever apply the four encodings the document set declares, which left five of the
// eight the gate accepts and 33 of the 37 published aliases never exercised by
// any case. This op names them explicitly.
//
// Two failures are kept apart, because a port can get either one wrong on its
// own: the gate rejecting a name it should accept, which the reference reports as
// `unknown-encoding`, and the codec rejecting bytes that are genuinely not valid
// in it, which is `undecodable`. A port with a complete alias table and a broken
// cp1251 table fails the second and passes the first.
//
// The canonical name is not asked for separately. There is no API that exposes
// the resolution, and adding one would be adding surface for the grader's
// convenience; instead the *decoded text* is reported, which is what a resolution
// is for. A port that resolves `latin_1` to latin-1 and one that resolves it to
// cp1251 disagree here on any document with a high byte, which is why the
// catalog pairs every alias with documents whose bytes the eight codecs disagree
// on.
func opRTEnc(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	id, err := a.DocID(0)
	if err != nil {
		return nil, err
	}
	name, err := a.Text(1)
	if err != nil {
		return nil, err
	}
	data, err := c.DocBytes(id)
	if err != nil {
		return nil, err
	}
	nodes, perr := sqlparse.ParseBytes(data, name)
	if perr != nil {
		// Answered as a category, matching probe.py: the reference's wording for
		// a bad codec name is CPython's and is not portable, but the
		// distinction between an unknown name and undecodable bytes is
		// behavior, and that is what crosses the wire.
		out := &probeutil.Answer{}
		out.Add("status\terr")
		out.Add("category\t%s", probeutil.ErrorCategory(perr))
		return out, nil
	}
	// The source text is recovered by joining the statements rather than by
	// decoding again, because the tier has no decoder of its own to call: the
	// root package exposes ParseBytes and not a decode entry point. That makes
	// the `lossless` line weaker here than in probe.py, where source and joined
	// are computed independently -- so probe.py reports `source` and this does
	// not, and the catalog does not compare a `source` line for this op.
	//
	// What is still compared, and is the whole point: the per-statement text and
	// its length in code points. A port whose alias table sends `latin_1` to the
	// wrong codec produces different text for the same bytes, and every stmt
	// line disagrees.
	out := &probeutil.Answer{}
	out.Add("status\tok")
	out.Add("count\t%d", len(nodes))
	var joined strings.Builder
	for i, node := range nodes {
		text := node.String()
		joined.WriteString(text)
		out.Add("stmt\t%d\t%d\t%s", i, probeutil.RuneLen(text),
			probeutil.EncField(text))
	}
	out.Add("len\t%d", probeutil.RuneLen(joined.String()))
	return out, nil
}
