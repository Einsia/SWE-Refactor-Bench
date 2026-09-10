// Code generated from _work/lang03/probe-monolith.go by split_emit.py. DO NOT EDIT.
//
// The parts probe tier: the lower-level packages a downstream consumer reaches into
//
// `validate` is here rather than in core because it calls
// formatter.ValidateOptions directly, which is where the reference's
// rejection messages live -- sqlparse.format only reaches them through the
// whole pipeline, and grading the messages needs the validator itself. An
// op's tier follows its imports; putting it in core would have meant core
// importing formatter, which would cost core its only claim, that it names
// the root package alone.
package main

import (
	"sort"
	"strings"

	sqlparse "github.com/andialbrecht/sqlparse-go"
	"github.com/andialbrecht/sqlparse-go/engine"
	"github.com/andialbrecht/sqlparse-go/filters"
	"github.com/andialbrecht/sqlparse-go/formatter"
	"github.com/andialbrecht/sqlparse-go/lexer"
	"github.com/andialbrecht/sqlparse-go/sql"
	"github.com/andialbrecht/sqlparse-go/tokens"
	"github.com/andialbrecht/sqlparse-go/utils"
	"probe/probeutil"
)

func main() {
	r := probeutil.NewRegistry()
	r.Register("lex", opLex)
	r.Register("kwlex", opKwLex)
	r.Register("filter", opFilter)
	r.Register("stack", opStack)
	r.Register("remove-quotes", opRemoveQuotes)
	r.Register("split-unquoted-newlines", opSplitUnquotedNewlines)
	r.Register("optkeys", opOptKeys)
	r.Register("optkeys-sorted", opOptKeysSorted)
	r.Register("lexstate", opLexState)
	r.Register("validate", opValidate)
	r.RenderError = portableError
	probeutil.Main(r)
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

func opValidate(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	name, err := a.Name(0)
	if err != nil {
		return nil, err
	}
	entry, ok := c.Spec.ValidateCases[name]
	if !ok {
		return nil, probeutil.Defectf("unknown validate case: %s", name)
	}
	options, err := probeutil.DecodeOptions(entry)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	if _, verr := formatter.ValidateOptions(options); verr != nil {
		out.Add("status\terr")
		for _, line := range errorLines(verr) {
			out.Add("%s", line)
		}
		return out, nil
	}
	out.Add("status\tok")
	return out, nil
}

func opKwLex(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	data, err := a.Doc(0)
	if err != nil {
		return nil, err
	}
	items, lerr := lexer.Default().Tokenize(string(data))
	if lerr != nil {
		return nil, lerr
	}
	out := &probeutil.Answer{}
	for i, item := range items {
		out.Add("t\t%d\t%s\t%d", i, probeutil.EncField(item.TType().String()),
			probeutil.RuneLen(item.Value()))
	}
	if out.Len() == 0 {
		out.Add("t\tnone")
	}
	return out, nil
}

// -- parts tier -------------------------------------------------------------

func opLex(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
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
	var items []sql.Item
	var lerr error
	if enc != "" {
		items, lerr = lexer.Default().TokenizeBytes(data, enc)
	} else {
		items, lerr = lexer.Default().Tokenize(string(data))
	}
	if lerr != nil {
		return nil, lerr
	}
	out := &probeutil.Answer{}
	for i, item := range items {
		out.Add("t\t%d\t%s\t%s", i, probeutil.EncField(item.TType().String()),
			probeutil.EncField(item.Value()))
	}
	if out.Len() == 0 {
		out.Add("t\tnone")
	}
	return out, nil
}

func buildFilter(entry probeutil.FilterSpec) (filters.Filter, error) {
	params := map[string]any{}
	for k, v := range entry.Params {
		val, err := probeutil.DecodeOption(v)
		if err != nil {
			return nil, err
		}
		params[k] = val
	}
	str := func(key, fallback string) string {
		if v, ok := params[key].(string); ok {
			return v
		}
		return fallback
	}
	num := func(key string, fallback int) int {
		if v, ok := params[key].(int); ok {
			return v
		}
		return fallback
	}
	yes := func(key string) bool {
		v, _ := params[key].(bool)
		return v
	}
	switch entry.Go {
	case "NewKeywordCase":
		return filters.NewKeywordCase(str("case", "")), nil
	case "NewIdentifierCase":
		return filters.NewIdentifierCase(str("case", "")), nil
	case "NewTruncateStrings":
		return filters.NewTruncateStrings(num("width", 0), str("char", "")), nil
	case "NewStripComments":
		return filters.NewStripComments(), nil
	case "NewStripWhitespace":
		return filters.NewStripWhitespace(), nil
	case "NewStripTrailingSemicolon":
		return filters.NewStripTrailingSemicolon(), nil
	case "NewSpacesAroundOperators":
		return filters.NewSpacesAroundOperators(), nil
	case "NewReindent":
		// The reference's defaults, restated: width 2, a space, no wrapping.
		// The config struct is how the port expresses Python's keyword
		// arguments, so unset fields must land on the same defaults.
		cfg := filters.ReindentConfig{
			Width:            num("width", 2),
			Char:             str("char", " "),
			WrapAfter:        num("wrap_after", 0),
			Newline:          str("n", "\n"),
			CommaFirst:       yes("comma_first"),
			IndentAfterFirst: yes("indent_after_first"),
			IndentColumns:    yes("indent_columns"),
			Compact:          yes("compact"),
		}
		return filters.NewReindent(cfg), nil
	case "NewAlignedIndent":
		return filters.NewAlignedIndent(str("char", " "), str("n", "\n")), nil
	case "NewRightMargin":
		return filters.NewRightMargin(num("width", 79)), nil
	case "NewSerializerUnicode":
		return filters.NewSerializerUnicode(), nil
	case "NewOutputPython":
		return filters.NewOutputPython(str("varname", "sql")), nil
	case "NewOutputPHP":
		return filters.NewOutputPHP(str("varname", "sql")), nil
	}
	return nil, probeutil.Defectf("unknown filter constructor: %s", entry.Go)
}

func opFilter(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	name, err := a.Name(0)
	if err != nil {
		return nil, err
	}
	entry, ok := c.Spec.Filters[name]
	if !ok {
		return nil, probeutil.Defectf("unknown filter: %s", name)
	}
	instance, err := buildFilter(entry)
	if err != nil {
		return nil, err
	}
	stack := engine.NewFilterStack(false)
	switch entry.Stage {
	case "pre":
		stack.SetPreprocess([]filters.Filter{instance})
		stack.SetPostprocess([]filters.Filter{filters.NewSerializerUnicode()})
	case "stmt":
		stack.EnableGrouping()
		stack.SetStmtprocess([]filters.Filter{instance})
		stack.SetPostprocess([]filters.Filter{filters.NewSerializerUnicode()})
	case "post":
		stack.EnableGrouping()
		stack.SetPostprocess([]filters.Filter{instance})
	default:
		return nil, probeutil.Defectf("unknown filter stage: %s", entry.Stage)
	}
	id, err := a.DocID(1)
	if err != nil {
		return nil, err
	}
	text, err := c.DocText(id)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	results, rerr := stack.Run(text, "")
	if rerr != nil {
		out.Add("status\terr")
		for _, line := range errorLines(rerr) {
			out.Add("%s", line)
		}
		return out, nil
	}
	out.Add("status\tok")
	out.Add("count\t%d", len(results))
	for i, node := range results {
		out.Add("r\t%d\t%s", i, probeutil.EncField(node.String()))
	}
	return out, nil
}

func opStack(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	name, err := a.Name(0)
	if err != nil {
		return nil, err
	}
	shape, ok := c.Spec.Stacks[name]
	if !ok {
		return nil, probeutil.Defectf("unknown stack: %s", name)
	}
	stack := engine.NewFilterStack(shape.StripSemicolon)
	if shape.Grouping {
		stack.EnableGrouping()
	}
	var pre, stmt, post []filters.Filter
	for _, filterName := range shape.Filters {
		entry, ok := c.Spec.Filters[filterName]
		if !ok {
			return nil, probeutil.Defectf("unknown filter in stack %s: %s", name, filterName)
		}
		instance, err := buildFilter(entry)
		if err != nil {
			return nil, err
		}
		switch entry.Stage {
		case "pre":
			pre = append(pre, instance)
		case "stmt":
			stmt = append(stmt, instance)
		case "post":
			post = append(post, instance)
		default:
			return nil, probeutil.Defectf("unknown filter stage: %s", entry.Stage)
		}
	}
	stack.SetPreprocess(pre)
	stack.SetStmtprocess(stmt)
	stack.SetPostprocess(post)
	id, err := a.DocID(1)
	if err != nil {
		return nil, err
	}
	text, err := c.DocText(id)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	results, rerr := stack.Run(text, "")
	if rerr != nil {
		out.Add("status\terr")
		for _, line := range errorLines(rerr) {
			out.Add("%s", line)
		}
		return out, nil
	}
	out.Add("status\tok")
	out.Add("count\t%d", len(results))
	for i, node := range results {
		out.Add("r\t%d\t%s", i, probeutil.EncField(node.String()))
	}
	return out, nil
}

func opRemoveQuotes(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	text, err := a.Text(0)
	if err != nil {
		return nil, err
	}
	result := utils.RemoveQuotes(text)
	out := &probeutil.Answer{}
	out.Add("in\t%s", probeutil.EncField(text))
	out.Add("out\t%s", probeutil.EncField(result))
	out.Add("len\t%d", probeutil.RuneLen(result))
	return out, nil
}

func opSplitUnquotedNewlines(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	text, err := a.Text(0)
	if err != nil {
		return nil, err
	}
	parts := utils.SplitUnquotedNewlines(text)
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(parts))
	for i, p := range parts {
		out.Add("p\t%d\t%d\t%s", i, probeutil.RuneLen(p), probeutil.EncField(p))
	}
	return out, nil
}

func opOptKeys(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	keys := formatter.OptionKeys()
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(keys))
	for _, k := range keys {
		out.Add("k\t%s", k)
	}
	return out, nil
}

func opOptKeysSorted(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	keys := append([]string(nil), formatter.OptionKeys()...)
	sort.Strings(keys)
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(keys))
	for _, k := range keys {
		out.Add("k\t%s", k)
	}
	return out, nil
}

// opLexState drives the lexer's mutable keyword state.
//
// The reference lexer is a process-wide singleton with an add/clear/reinitialize
// protocol, and each step observably changes tokenization. A port that bakes the
// tables into a package-level map passes every other keyword case and fails
// every script here. Each script restores the default before returning, because
// the scripts share one process and a leak would make the next script's answer
// depend on the order.
func opLexState(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	script, err := a.Name(0)
	if err != nil {
		return nil, err
	}
	lex := lexer.Default()
	defer lex.DefaultInitialization()

	const probeSQL = "select mycustomkw from t where x = 1"
	out := &probeutil.Answer{}
	snapshot := func(label string) {
		items, lerr := lex.Tokenize(probeSQL)
		if lerr != nil {
			// Tab-joined onto the label, the way probe.py joins error_lines
			// here, rather than one line per field: this op reports several
			// labelled snapshots and each is one line. The bare category was
			// missing the `kind` field name the reference emits.
			out.Add("%s\terr\t%s", label,
				strings.Join(errorLines(lerr), "\t"))
			return
		}
		parts := make([]string, len(items))
		for i, item := range items {
			parts[i] = probeutil.EncField(item.TType().String()) + ":" + probeutil.EncField(item.Value())
		}
		out.Add("%s\tok\t%d\t%s", label, len(items), strings.Join(parts, " "))
	}
	kw := func(label, word string) {
		ttype, value := lex.IsKeyword(word)
		out.Add("%s\t%s\t%s\t%s", label, probeutil.EncField(word),
			probeutil.EncField(ttype.String()), probeutil.EncField(value))
	}

	switch script {
	case "default":
		snapshot("stream")
		kw("kw", "SELECT")
		kw("kw", "MYCUSTOMKW")
	case "clear-then-lex":
		lex.Clear()
		snapshot("stream")
	case "clear-then-default":
		lex.Clear()
		snapshot("cleared")
		lex.DefaultInitialization()
		snapshot("restored")
	case "add-custom":
		lex.AddKeywords(map[string]tokens.TokenType{"MYCUSTOMKW": tokens.Keyword})
		snapshot("stream")
		kw("kw", "MYCUSTOMKW")
	case "add-then-clear":
		lex.AddKeywords(map[string]tokens.TokenType{"MYCUSTOMKW": tokens.Keyword})
		snapshot("added")
		lex.Clear()
		snapshot("cleared")
	case "add-twice":
		lex.AddKeywords(map[string]tokens.TokenType{"MYCUSTOMKW": tokens.Keyword})
		lex.AddKeywords(map[string]tokens.TokenType{
			"MYCUSTOMKW": tokens.Name.Sub("Builtin")})
		snapshot("stream")
		kw("kw", "MYCUSTOMKW")
	case "add-overrides-builtin":
		lex.AddKeywords(map[string]tokens.TokenType{"SELECT": tokens.Name})
		kw("kw", "SELECT")
		snapshot("stream")
	case "default-idempotent":
		lex.DefaultInitialization()
		snapshot("once")
		lex.DefaultInitialization()
		snapshot("twice")
	case "clear-idempotent":
		lex.Clear()
		lex.Clear()
		snapshot("stream")
		kw("kw", "SELECT")
	case "add-empty-table":
		lex.AddKeywords(map[string]tokens.TokenType{})
		snapshot("stream")
	case "add-then-reinit":
		lex.AddKeywords(map[string]tokens.TokenType{"MYCUSTOMKW": tokens.Keyword})
		snapshot("added")
		lex.DefaultInitialization()
		snapshot("reinit")
		kw("kw", "MYCUSTOMKW")
	case "is-keyword-after-clear":
		lex.Clear()
		kw("kw", "SELECT")
		kw("kw", "MYCUSTOMKW")
	case "add-lowercase-key":
		lex.AddKeywords(map[string]tokens.TokenType{"mycustomkw": tokens.Keyword})
		kw("kw", "MYCUSTOMKW")
		kw("kw", "mycustomkw")
		snapshot("stream")
	case "add-many":
		lex.AddKeywords(map[string]tokens.TokenType{
			"MYCUSTOMKW": tokens.Keyword,
			"OTHERKW":    tokens.Name.Sub("Builtin"),
		})
		lex.AddKeywords(map[string]tokens.TokenType{
			"MYCUSTOMKW": tokens.Wildcard,
			"THIRDKW":    tokens.Comment,
		})
		for _, word := range []string{"MYCUSTOMKW", "OTHERKW", "THIRDKW", "SELECT"} {
			kw("kw", word)
		}
		snapshot("stream")
	default:
		return nil, probeutil.Defectf("unknown lexstate script: %s", script)
	}
	if out.Len() == 0 {
		out.Add("none")
	}
	return out, nil
}

// ---------------------------------------------------------------------------
// The loop.
// ---------------------------------------------------------------------------

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
