// Package probeutil is the half of the differential probe that does not touch
// the submission.
//
// The wire protocol, the escaping, the argument decoding and the spec tables
// live here so that all four tier binaries share one implementation. That
// sharing is the point: the tiers are graded independently, and if each carried
// its own copy of encField the four could disagree about how a control character
// renders, which would show up as a behavioral difference that is really a
// verifier bug.
//
// This package imports nothing from the submission, so it compiles even when the
// submission does not. A tier binary that fails to build fails on the
// submission's API, never on the scaffolding.
package probeutil

import (
	"bufio"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Version is reported by --selftest and compared against probe.py's, so a tier
// built from a stale source cannot be mistaken for a current one.
const Version = "swerefactor-lang03-probe-v1"

// ---------------------------------------------------------------------------
// Documents and spec
// ---------------------------------------------------------------------------

type DocMeta struct {
	ID       string `json:"id"`
	Group    string `json:"group"`
	Bytes    int    `json:"bytes"`
	SHA256   string `json:"sha256"`
	Encoding string `json:"encoding"`
	UTF8     bool   `json:"utf8"`
}

type DocsFile struct {
	Documents []DocMeta `json:"documents"`
}

// Tagged is the two-element [tag, value] encoding spec.py emits. Go's JSON
// decoder turns every number into float64, so the tag is what tells an int from
// a float -- and several of the reference's option errors exist only to complain
// about a type, so the distinction has to survive the wire.
type Tagged []json.RawMessage

type FilterSpec struct {
	Py     string            `json:"py"`
	Go     string            `json:"go"`
	Params map[string]Tagged `json:"params"`
	Stage  string            `json:"stage"`
}

type StackSpec struct {
	Grouping       bool     `json:"grouping"`
	StripSemicolon bool     `json:"strip_semicolon"`
	Filters        []string `json:"filters"`
}

type LastLineInput struct {
	Text    string
	Leading int
}

// UnmarshalJSON reads the [text, leading] pair spec.py emits.
func (l *LastLineInput) UnmarshalJSON(data []byte) error {
	var pair []json.RawMessage
	if err := json.Unmarshal(data, &pair); err != nil {
		return err
	}
	if len(pair) != 2 {
		return fmt.Errorf("lastline input has %d elements, want 2", len(pair))
	}
	if err := json.Unmarshal(pair[0], &l.Text); err != nil {
		return err
	}
	return json.Unmarshal(pair[1], &l.Leading)
}

// LexStep is one step of a lexer-state script: an operation plus its argument.
type LexStep struct {
	Op    string
	Word  string
	Table map[string]string
}

func (s *LexStep) UnmarshalJSON(data []byte) error {
	var parts []json.RawMessage
	if err := json.Unmarshal(data, &parts); err != nil {
		return err
	}
	if len(parts) == 0 {
		return fmt.Errorf("empty lexstate step")
	}
	if err := json.Unmarshal(parts[0], &s.Op); err != nil {
		return err
	}
	if len(parts) == 1 {
		return nil
	}
	switch s.Op {
	case "add":
		return json.Unmarshal(parts[1], &s.Table)
	default:
		return json.Unmarshal(parts[1], &s.Word)
	}
}

type SpecFile struct {
	Schema                      string                       `json:"schema"`
	FormatPresets               map[string]map[string]Tagged `json:"format_presets"`
	ValidateCases               map[string]map[string]Tagged `json:"validate_cases"`
	Filters                     map[string]FilterSpec        `json:"filters"`
	Stacks                      map[string]StackSpec         `json:"stacks"`
	RemoveQuotesInputs          []string                     `json:"remove_quotes_inputs"`
	LastLineInputs              []LastLineInput              `json:"lastline_inputs"`
	SplitUnquotedNewlinesInputs []string                     `json:"split_unquoted_newlines_inputs"`
	TTypeNames                  []string                     `json:"ttype_names"`
	TTypeNonexistent              []string                     `json:"ttype_nonexistent"`
	EncodingAliases             map[string]string            `json:"encoding_aliases"`
	EncodingCanonical           []string                     `json:"encoding_canonical"`
	LexStateScripts             map[string][]LexStep         `json:"lexstate_scripts"`
	AccessorScope               map[string]string            `json:"accessor_scope"`
	NodeKinds                   []string                     `json:"node_kinds"`
	NonGroupKinds               []string                     `json:"non_group_kinds"`
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

type Context struct {
	docsDir string
	docs    map[string]DocMeta
	Spec    SpecFile
	cache   map[string][]byte
}

func NewContext(docsDir string, docs DocsFile, spec SpecFile) *Context {
	c := &Context{
		docsDir: docsDir,
		docs:    make(map[string]DocMeta, len(docs.Documents)),
		Spec:    spec,
		cache:   map[string][]byte{},
	}
	for _, d := range docs.Documents {
		c.docs[d.ID] = d
	}
	return c
}

// AccessorApplies reports whether a node of this kind answers this accessor.
//
// The Go half of probe.py's Context.accessor_applies, reading the same table out
// of the same spec.json. It has to be a table rather than a question asked of the
// submission: the reference spreads its read surface over a 22-class hierarchy,
// and the contracted Go *Node is one struct on which every method is callable, so
// there is nothing to ask. An earlier version of the probe called a
// `HasAccessor(string) bool` method on the submission, which would have meant
// adding a method to the public API for the grader's benefit and asking the thing
// under test to describe itself.
//
// A label with no scope, or a scope naming a kind that does not exist, is a
// defect in the spec rather than a wrong answer -- the two halves would disagree
// about the shape of every answer at that label, so it stops the run.
func (c *Context) AccessorApplies(label, kind string) (bool, error) {
	scope, ok := c.Spec.AccessorScope[label]
	if !ok {
		return false, Defectf("accessor %q has no scope in the spec", label)
	}
	switch scope {
	case "any":
		return true, nil
	case "group":
		for _, k := range c.Spec.NonGroupKinds {
			if k == kind {
				return false, nil
			}
		}
		return true, nil
	}
	for _, k := range c.Spec.NodeKinds {
		if k == scope {
			return scope == kind, nil
		}
	}
	return false, Defectf("accessor %q is scoped to unknown kind %q",
		label, scope)
}

func (c *Context) DocBytes(id string) ([]byte, error) {
	if data, ok := c.cache[id]; ok {
		return data, nil
	}
	if _, ok := c.docs[id]; !ok {
		return nil, Defectf("unknown document: %s", id)
	}
	data, err := os.ReadFile(filepath.Join(c.docsDir, id))
	if err != nil {
		return nil, Defectf("reading document %s: %v", id, err)
	}
	c.cache[id] = data
	return data, nil
}

func (c *Context) DocEncoding(id string) (string, error) {
	meta, ok := c.docs[id]
	if !ok {
		return "", Defectf("unknown document: %s", id)
	}
	return meta.Encoding, nil
}

// DocText is the document as a caller holding a string would have it.
//
// The reference decodes bytes with the declared encoding, or tries UTF-8 and
// falls back to unicode-escape. Go has no unicode-escape codec, so ops that take
// a string hand the raw bytes over and the submission's own decode path is what
// gets graded by the ops that take bytes.
func (c *Context) DocText(id string) (string, error) {
	data, err := c.DocBytes(id)
	if err != nil {
		return "", err
	}
	return string(data), nil
}

// ---------------------------------------------------------------------------
// Errors. A Defect is a bad request, not a behavioral difference: it must never
// be reported as an answer, because both halves would answer it the same way and
// the case would pass while measuring nothing.
// ---------------------------------------------------------------------------

type Defect struct{ Msg string }

func (d *Defect) Error() string { return d.Msg }

func Defectf(format string, args ...any) error {
	return &Defect{Msg: fmt.Sprintf(format, args...)}
}

func IsDefect(err error) bool {
	_, ok := err.(*Defect)
	return ok
}

// ---------------------------------------------------------------------------
// Argument decoding. Mirrors catalog.Catalog.{d,e,s,i,b,n}.
// ---------------------------------------------------------------------------

type Args struct {
	ctx *Context
	raw []string
}

func (a *Args) tagAt(i int) (string, string, error) {
	if i >= len(a.raw) {
		return "", "", Defectf("missing argument %d", i)
	}
	item := a.raw[i]
	idx := strings.IndexByte(item, ':')
	if idx < 0 {
		return "", "", Defectf("argument %d has no tag: %q", i, item)
	}
	return item[:idx], item[idx+1:], nil
}

func (a *Args) DocID(i int) (string, error) {
	tag, rest, err := a.tagAt(i)
	if err != nil {
		return "", err
	}
	if tag != "d" && tag != "e" {
		return "", Defectf("argument %d is %s:, expected d: or e:", i, tag)
	}
	return rest, nil
}

func (a *Args) Doc(i int) ([]byte, error) {
	id, err := a.DocID(i)
	if err != nil {
		return nil, err
	}
	return a.ctx.DocBytes(id)
}

// DocEncoding is only valid on an e: argument. A d: argument deliberately has no
// encoding: the two tags are how the catalog says "decode this with its declared
// encoding" versus "hand these bytes over undeclared", and conflating them would
// erase the whole encoding family's distinction.
func (a *Args) DocEncoding(i int) (string, error) {
	tag, rest, err := a.tagAt(i)
	if err != nil {
		return "", err
	}
	if tag != "e" {
		return "", Defectf("argument %d is %s:, expected e:", i, tag)
	}
	return a.ctx.DocEncoding(rest)
}

func (a *Args) Text(i int) (string, error) {
	tag, rest, err := a.tagAt(i)
	if err != nil {
		return "", err
	}
	if tag != "s" {
		return "", Defectf("argument %d is %s:, expected s:", i, tag)
	}
	return Unescape(rest)
}

func (a *Args) Num(i int) (int, error) {
	tag, rest, err := a.tagAt(i)
	if err != nil {
		return 0, err
	}
	if tag != "i" {
		return 0, Defectf("argument %d is %s:, expected i:", i, tag)
	}
	n, convErr := strconv.Atoi(rest)
	if convErr != nil {
		return 0, Defectf("argument %d is not an integer: %q", i, rest)
	}
	return n, nil
}

func (a *Args) Flag(i int) (bool, error) {
	tag, rest, err := a.tagAt(i)
	if err != nil {
		return false, err
	}
	if tag != "b" {
		return false, Defectf("argument %d is %s:, expected b:", i, tag)
	}
	return rest == "1", nil
}

func (a *Args) Name(i int) (string, error) {
	tag, rest, err := a.tagAt(i)
	if err != nil {
		return "", err
	}
	if tag != "n" {
		return "", Defectf("argument %d is %s:, expected n:", i, tag)
	}
	return rest, nil
}

// Unescape is the inverse of catalog.Catalog.s.
//
// Operates on bytes rather than runes: the escapes are all ASCII, and a byte-wise
// pass leaves multi-byte UTF-8 sequences untouched while a rune-wise pass would
// have to reassemble them.
func Unescape(s string) (string, error) {
	var b strings.Builder
	b.Grow(len(s))
	for i := 0; i < len(s); {
		ch := s[i]
		if ch != '\\' {
			b.WriteByte(ch)
			i++
			continue
		}
		i++
		if i >= len(s) {
			return "", Defectf("trailing backslash in s: argument")
		}
		switch s[i] {
		case 'n':
			b.WriteByte('\n')
			i++
		case 't':
			b.WriteByte('\t')
			i++
		case 'r':
			b.WriteByte('\r')
			i++
		case '\\':
			b.WriteByte('\\')
			i++
		case 'x':
			if i+3 > len(s) {
				return "", Defectf("truncated \\x escape")
			}
			v, err := strconv.ParseUint(s[i+1:i+3], 16, 8)
			if err != nil {
				return "", Defectf("bad \\x escape: %q", s[i+1:i+3])
			}
			b.WriteByte(byte(v))
			i += 3
		case 'u':
			if i+5 > len(s) {
				return "", Defectf("truncated \\u escape")
			}
			v, err := strconv.ParseUint(s[i+1:i+5], 16, 32)
			if err != nil {
				return "", Defectf("bad \\u escape: %q", s[i+1:i+5])
			}
			// A lone surrogate. Python can hold one and Go cannot; it is written
			// back as the WTF-8 encoding of the code point so the two halves at
			// least agree on the bytes.
			b.WriteRune(rune(v))
			i += 5
		default:
			return "", Defectf("unknown escape \\%c", s[i])
		}
	}
	return b.String(), nil
}

// ---------------------------------------------------------------------------
// Rendering. One framing for every op, identical to probe.py's.
// ---------------------------------------------------------------------------

// EncField escapes one field so a record stays on one line.
//
// Ranges over bytes, not runes, and decodes runes explicitly: a Go string can
// hold invalid UTF-8, ranging over it silently yields U+FFFD, and that would turn
// a byte-level difference into a pass. Non-UTF-8 bytes are emitted as \xNN, which
// is what probe.py emits for the same bytes.
func EncField(s string) string {
	var b strings.Builder
	b.Grow(len(s) + 8)
	for i := 0; i < len(s); {
		c := s[i]
		if c < utf8.RuneSelf {
			switch {
			case c == '\\':
				b.WriteString(`\\`)
			case c == '\t':
				b.WriteString(`\t`)
			case c == '\n':
				b.WriteString(`\n`)
			case c == '\r':
				b.WriteString(`\r`)
			case c < 0x20 || c == 0x7F:
				fmt.Fprintf(&b, `\x%02x`, c)
			default:
				b.WriteByte(c)
			}
			i++
			continue
		}
		r, size := utf8.DecodeRuneInString(s[i:])
		if r == utf8.RuneError && size == 1 {
			fmt.Fprintf(&b, `\x%02x`, s[i])
			i++
			continue
		}
		if r >= 0xD800 && r <= 0xDFFF {
			fmt.Fprintf(&b, `\u%04x`, r)
			i += size
			continue
		}
		b.WriteString(s[i : i+size])
		i += size
	}
	return b.String()
}

// RuneLen is the length Python's len() would report: code points, not bytes.
//
// Every length in every answer goes through this. A port that reaches for len(s)
// reports bytes, which agrees with Python on ASCII and disagrees on every
// non-ASCII document -- and the document set has non-ASCII documents precisely so that
// shortcut is caught here rather than passing.
func RuneLen(s string) int {
	return utf8.RuneCountInString(s)
}

func BoolDigit(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

type Answer struct{ lines []string }

func (a *Answer) Add(format string, args ...any) {
	a.lines = append(a.lines, fmt.Sprintf(format, args...))
}

// Len is how many lines have been added.
//
// Exported because several ops emit a placeholder when they produced nothing --
// probe.py's `lines(*out) if out else lines("a\tnone")` -- and the tiers are in a
// different package than the Answer.
func (a *Answer) Len() int { return len(a.lines) }

func (a *Answer) Bytes() []byte {
	if len(a.lines) == 0 {
		return []byte("\n")
	}
	return []byte(strings.Join(a.lines, "\n") + "\n")
}

// ---------------------------------------------------------------------------
// Option decoding
// ---------------------------------------------------------------------------

func DecodeOption(t Tagged) (any, error) {
	if len(t) != 2 {
		return nil, Defectf("malformed tagged option: %d elements", len(t))
	}
	var tag string
	if err := json.Unmarshal(t[0], &tag); err != nil {
		return nil, Defectf("option tag is not a string: %v", err)
	}
	switch tag {
	case "i":
		var v int
		if err := json.Unmarshal(t[1], &v); err != nil {
			return nil, Defectf("option is not an int: %v", err)
		}
		return v, nil
	case "s":
		var v string
		if err := json.Unmarshal(t[1], &v); err != nil {
			return nil, Defectf("option is not a string: %v", err)
		}
		return v, nil
	case "b":
		var v bool
		if err := json.Unmarshal(t[1], &v); err != nil {
			return nil, Defectf("option is not a bool: %v", err)
		}
		return v, nil
	case "n":
		return nil, nil
	case "f":
		var v float64
		if err := json.Unmarshal(t[1], &v); err != nil {
			return nil, Defectf("option is not a float: %v", err)
		}
		return v, nil
	}
	return nil, Defectf("unknown option tag: %q", tag)
}

func DecodeOptions(m map[string]Tagged) (map[string]any, error) {
	out := make(map[string]any, len(m))
	for k, v := range m {
		val, err := DecodeOption(v)
		if err != nil {
			return nil, err
		}
		out[k] = val
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// The operation table and the serve loop
// ---------------------------------------------------------------------------

type OpFunc func(*Context, *Args) (*Answer, error)

// Registry is one tier's operation table. Each tier registers only the ops its
// import closure can answer, and Serve reports a request for anything else as a
// defect -- which is how a misrouted case becomes a visible verifier bug rather
// than a silent zero for the submission.
type Registry struct {
	ops map[string]OpFunc
	// RenderError turns a submission error into a comparable string. It lives
	// here as a hook because the library's own error type is in the submission,
	// which this package must not import; each tier supplies it.
	RenderError func(error) string
}

func NewRegistry() *Registry {
	return &Registry{ops: map[string]OpFunc{}}
}

func (r *Registry) Register(name string, fn OpFunc) {
	if _, dup := r.ops[name]; dup {
		panic("duplicate op: " + name)
	}
	r.ops[name] = fn
}

func (r *Registry) Names() []string {
	names := make([]string, 0, len(r.ops))
	for name := range r.ops {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// ErrorCategory is the portable half of an error rendering.
//
// A runtime's wording for a decode failure is the runtime's, and grading it would
// put a case on one side of the pair the other side cannot answer. What both
// halves can agree on is the kind of failure, so that is what is compared. The
// strings here are probe.py's _error_category strings exactly.
// The categories, in the order probe.py's _error_category tests them. Order
// matters in two places and they are the same two places on both sides: a nil
// dereference is a panic, so it has to be tested before the generic panic case,
// and undecodable bytes are tested before an unknown codec name because Python's
// UnicodeDecodeError is a ValueError while a bad codec name is a LookupError, and
// a port must keep those two apart as well.
func ErrorCategory(err error) string {
	msg := strings.ToLower(err.Error())
	switch {
	case strings.Contains(msg, "nil pointer dereference") ||
		strings.Contains(msg, "invalid memory address"):
		// The reference's AttributeError from Function.get_window, which is
		// graded rather than repaired. Go's own name for reaching through
		// nothing is this, so both halves have something to call it -- unlike
		// `other`, which would also swallow failures that are not this one.
		return "nil-deref"
	case strings.Contains(msg, "index out of range") ||
		strings.Contains(msg, "slice bounds out of range"):
		// Python raises IndexError, which is a LookupError -- so
		// _error_category calls it `unknown-encoding`, which would be absurd
		// here. It reaches that branch only if an op indexes out of bounds,
		// which is a probe bug on either side, so it is named rather than
		// silently miscategorized.
		return "index"
	case strings.Contains(msg, "stack overflow") ||
		strings.Contains(msg, "recursion") || strings.Contains(msg, "depth"):
		// The nesting case. A Go stack overflow is fatal and unrecoverable, so a
		// port cannot rely on reaching here by crashing: the contract requires
		// an explicit depth limit that returns an error, and this branch is for
		// that error.
		return "depth-exceeded"
	case strings.Contains(msg, "decode") ||
		strings.Contains(msg, "invalid byte") ||
		strings.Contains(msg, "not valid in"):
		return "undecodable"
	case strings.Contains(msg, "encoding") || strings.Contains(msg, "codec"):
		return "unknown-encoding"
	case strings.Contains(msg, "panic:"):
		return "panic"
	case strings.Contains(msg, "type"):
		return "wrong-type"
	default:
		return "other"
	}
}

// Handle answers one request line.
func (r *Registry) Handle(c *Context, line string) string {
	fields := strings.Split(strings.TrimRight(line, "\n"), "\t")
	if len(fields) < 2 {
		return "?\tdefect\t" + B64("malformed request: fewer than two fields")
	}
	caseID, opName := fields[0], fields[1]
	fn, ok := r.ops[opName]
	if !ok {
		return caseID + "\tdefect\t" + B64("unknown op: "+opName)
	}
	status, payload := r.answer(opName, fn, c, &Args{ctx: c, raw: fields[2:]})
	return caseID + "\t" + status + "\t" + payload
}

// answer runs one op and renders its outcome, with every call that can reach the
// submission inside one recover.
//
// The recover has to span the rendering and not just the op, and that is the
// whole reason this is a separate function. Three of the four tiers assign
// RenderError to a hook that asks the submission's own error predicates about the
// error -- IsNotImplemented, errors.As against its error type, Error() -- so the
// rendering path runs as much submission code as the op did. A port whose
// predicates panic would take the tier's process down from there, and the
// executor would mark every remaining case in that tier abandoned: one panicking
// predicate cost 4,537 cases when this was found, which reads as a port that
// implements nothing rather than one with a single broken function.
//
// A panic is rendered as `err` with the `panic` category, which is what
// portableError already produces for a panic raised inside the op. One status for
// "the port panicked" regardless of which of its functions did it -- and never
// `defect`, which means the harness is broken and aborts the image build.
func (r *Registry) answer(opName string, fn OpFunc, c *Context,
	a *Args) (status, payload string) {
	defer func() {
		if rec := recover(); rec != nil {
			status, payload = "err", B64("category\tpanic")
		}
	}()
	result, err := fn(c, a)
	if err != nil {
		if IsDefect(err) {
			return "defect", B64(err.Error())
		}
		rendered := "category\t" + ErrorCategory(err)
		if r.RenderError != nil {
			rendered = r.RenderError(err)
		}
		return "err", B64(rendered)
	}
	if result == nil {
		// No error and no answer is this harness failing, not the submission: an
		// op has to return one or the other. Reported as a defect so it aborts the
		// freeze rather than becoming some submission's expected answer.
		return "defect", B64("op " + opName + " returned no answer and no error")
	}
	return "ok", base64.StdEncoding.EncodeToString(result.Bytes())
}

func B64(s string) string {
	return base64.StdEncoding.EncodeToString([]byte(s))
}

// Serve reads requests from stdin and writes one answer per line.
func (r *Registry) Serve(c *Context, in io.Reader, w io.Writer) error {
	// A large buffer: some answers are hundreds of kilobytes, and a short
	// scanner would truncate one into a difference that is not real.
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 0, 1<<20), 1<<26)
	out := bufio.NewWriterSize(w, 1<<20)
	for scanner.Scan() {
		line := scanner.Text()
		if strings.TrimSpace(line) == "" {
			continue
		}
		fmt.Fprintln(out, r.Handle(c, line))
		// Flushed per request: the executor writes one request and reads its
		// answer, so a buffered writer would deadlock on the first case.
		if err := out.Flush(); err != nil {
			return err
		}
	}
	if err := scanner.Err(); err != nil && err != io.EOF {
		return err
	}
	return nil
}

// Main is the entry point every tier shares.
func Main(r *Registry) {
	docsDir := flagString("docs")
	docsPath := flagString("documents")
	specPath := flagString("spec")
	selftest := false
	for _, arg := range os.Args[1:] {
		if arg == "--selftest" {
			selftest = true
		}
	}
	if selftest {
		fmt.Printf("%s ops=%d\n", Version, len(r.ops))
		for _, name := range r.Names() {
			fmt.Printf("  %s\n", name)
		}
		return
	}

	docs, err := readJSON[DocsFile](docsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "probe: documents: %v\n", err)
		os.Exit(2)
	}
	spec, err := readJSON[SpecFile](specPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "probe: spec: %v\n", err)
		os.Exit(2)
	}
	c := NewContext(docsDir, docs, spec)
	if err := r.Serve(c, os.Stdin, os.Stdout); err != nil {
		fmt.Fprintf(os.Stderr, "probe: serving: %v\n", err)
		os.Exit(2)
	}
}

func readJSON[T any](path string) (T, error) {
	var out T
	data, err := os.ReadFile(path)
	if err != nil {
		return out, err
	}
	err = json.Unmarshal(data, &out)
	return out, err
}

// flagString reads --name VALUE or --name=VALUE.
//
// Hand-rolled rather than using the flag package because a tier binary must not
// fail on a flag another tier accepts: the executor passes the same argv to all
// four, and flag.Parse would exit(2) on an unknown one.
func flagString(name string) string {
	want := "--" + name
	for i, arg := range os.Args[1:] {
		if arg == want && i+2 < len(os.Args) {
			return os.Args[i+2]
		}
		if strings.HasPrefix(arg, want+"=") {
			return strings.TrimPrefix(arg, want+"=")
		}
	}
	return ""
}
