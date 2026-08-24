// Code generated from _work/lang03/probe-monolith.go by split_emit.py. DO NOT EDIT.
//
// The model probe tier: the parse tree and the token lattice
package main

import (
	"fmt"
	"strconv"
	"strings"

	sqlparse "github.com/andialbrecht/sqlparse-go"
	"github.com/andialbrecht/sqlparse-go/sql"
	"github.com/andialbrecht/sqlparse-go/tokens"
	"probe/probeutil"
)

func main() {
	r := probeutil.NewRegistry()
	r.Register("tok", opTok)
	r.Register("tree", opTree)
	r.Register("stmt", opStmt)
	r.Register("api", opAPI)
	r.Register("ttype", opTType)
	r.RenderError = portableError
	probeutil.Main(r)
}

// parseDoc is the shared entry: bytes plus declared encoding when the case
// carries one, plain string otherwise. Which entry point a case uses is part of
// what it grades, so this mirrors probe.py's branch exactly.
func parseDoc(c *probeutil.Context, a *probeutil.Args, i int) ([]*sql.Node, string, error) {
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
		return nodes, string(data), perr
	}
	text, terr := c.DocText(id)
	if terr != nil {
		return nil, "", terr
	}
	nodes, perr := sqlparse.Parse(text)
	return nodes, text, perr
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

func opTok(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	nodes, _, err := parseDoc(c, a, 0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	for si, node := range nodes {
		for ti, leaf := range node.Flatten() {
			out.Add("t\t%d\t%d\t%s\t%s", si, ti,
				probeutil.EncField(leaf.TType().String()), probeutil.EncField(leaf.Value()))
		}
	}
	if out.Len() == 0 {
		out.Add("t\tnone")
	}
	return out, nil
}

func opTree(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	nodes, _, err := parseDoc(c, a, 0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	for si, node := range nodes {
		renderNode(out, node, si, 0, nil)
	}
	if out.Len() == 0 {
		out.Add("n\tnone")
	}
	return out, nil
}

func renderNode(out *probeutil.Answer, node *sql.Node, si, depth int, path []int) {
	addr := "-"
	if len(path) > 0 {
		parts := make([]string, len(path))
		for i, p := range path {
			parts[i] = strconv.Itoa(p)
		}
		addr = strings.Join(parts, ".")
	}
	out.Add("n\t%d\t%d\t%s\t%s\t%s\t%s%s%s%s\t%s\t%s",
		si, depth, addr,
		node.Kind(),
		probeutil.EncField(node.TType().String()),
		probeutil.BoolDigit(node.IsGroup()),
		probeutil.BoolDigit(node.IsKeyword()),
		probeutil.BoolDigit(node.IsWhitespace()),
		probeutil.BoolDigit(node.IsNewline()),
		probeutil.EncField(node.Normalized()),
		probeutil.EncField(node.Value()),
	)
	if node.IsGroup() {
		for i, child := range node.Tokens() {
			renderNode(out, child, si, depth+1, append(path, i))
		}
	}
}

func opStmt(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	nodes, _, err := parseDoc(c, a, 0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	if len(nodes) == 0 {
		out.Add("type\tnone")
		return out, nil
	}
	for si, node := range nodes {
		out.Add("type\t%d\t%s", si, probeutil.EncField(node.TypeName()))
		out.Add("tokens\t%d\t%d", si, len(node.Tokens()))
		out.Add("flat\t%d\t%d", si, len(node.Flatten()))
		out.Add("group\t%d\t%s", si, probeutil.BoolDigit(node.IsGroup()))
		out.Add("ws\t%d\t%s", si, probeutil.BoolDigit(node.IsWhitespace()))
		out.Add("len\t%d\t%d", si, probeutil.RuneLen(node.String()))
		// TokenFirst returns the token, not a pair: the reference's
		// token_first is asymmetric with token_next and the port keeps that.
		for _, probe := range []struct {
			label          string
			skipWS, skipCM bool
		}{
			{"first", true, true},
			{"firstws", false, false},
			{"firstnocm", true, false},
		} {
			tok := node.TokenFirst(probe.skipWS, probe.skipCM)
			if tok == nil {
				out.Add("%s\t%d\tnil\t", probe.label, si)
			} else {
				out.Add("%s\t%d\t%s\t%s", probe.label, si,
					probeutil.EncField(tok.Value()), probeutil.EncField(tok.TType().String()))
			}
		}
		for _, skipWS := range []bool{true, false} {
			for _, skipCM := range []bool{true, false} {
				var seq []string
				idx := -1
				for {
					next, tok := node.TokenNext(idx, skipWS, skipCM)
					if tok == nil {
						break
					}
					seq = append(seq, strconv.Itoa(next))
					idx = next
					if len(seq) > 4096 {
						break
					}
				}
				out.Add("walk\t%d\t%s%s\t%s", si,
					probeutil.BoolDigit(skipWS), probeutil.BoolDigit(skipCM), strings.Join(seq, ","))
			}
		}
	}
	return out, nil
}

func opAPI(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	id, err := a.DocID(0)
	if err != nil {
		return nil, err
	}
	text, err := c.DocText(id)
	if err != nil {
		return nil, err
	}
	nodes, perr := sqlparse.Parse(text)
	if perr != nil {
		return nil, perr
	}
	out := &probeutil.Answer{}
	limit := len(nodes)
	if limit > 3 {
		limit = 3
	}
	for si := 0; si < limit; si++ {
		for _, addr := range apiAddresses {
			label := addr
			if label == "" {
				label = "-"
			}
			node := resolveAddr(nodes[si], addr)
			if node == nil {
				out.Add("a\t%d\t%s\tunresolved", si, label)
				continue
			}
			for _, method := range apiMethods {
				value, verr := apiValueSafe(c, node, nodes[si], method)
				if verr != nil {
					return nil, verr
				}
				out.Add("a\t%d\t%s\t%s\t%s", si, label, method, value)
			}
		}
	}
	if out.Len() == 0 {
		out.Add("a\tnone")
	}
	return out, nil
}

func resolveAddr(stmt *sql.Node, addr string) *sql.Node {
	if addr == "" {
		return stmt
	}
	node := stmt
	for _, part := range strings.Split(addr, ".") {
		if !node.IsGroup() {
			return nil
		}
		idx, err := strconv.Atoi(part)
		if err != nil {
			return nil
		}
		children := node.Tokens()
		if idx < 0 {
			idx += len(children)
		}
		if idx < 0 || idx >= len(children) {
			return nil
		}
		node = children[idx]
	}
	return node
}

func renderTokenOnly(tok *sql.Node) string {
	if tok == nil {
		return "nil"
	}
	return fmt.Sprintf("node\t%s\t%s", tok.Kind(), probeutil.EncField(tok.String()))
}

func renderTokenPair(idx int, tok *sql.Node) string {
	if tok == nil {
		return fmt.Sprintf("tok\t%d\t", idx)
	}
	return fmt.Sprintf("tok\t%d\t%s", idx, probeutil.EncField(tok.Value()))
}

func renderNodeList(items []*sql.Node) string {
	parts := []string{fmt.Sprintf("list\t%d", len(items))}
	for _, item := range items {
		parts = append(parts, probeutil.EncField(item.String()))
	}
	return strings.Join(parts, "\t")
}

func renderNodeLists(groups [][]*sql.Node) string {
	parts := []string{fmt.Sprintf("list\t%d", len(groups))}
	for _, group := range groups {
		inner := make([]string, len(group))
		for i, item := range group {
			inner[i] = probeutil.EncField(item.String())
		}
		parts = append(parts, "["+strings.Join(inner, ",")+"]")
	}
	return strings.Join(parts, "\t")
}

func joinNodes(items []*sql.Node) string {
	if items == nil {
		return "nil"
	}
	var b strings.Builder
	for _, item := range items {
		b.WriteString(item.String())
	}
	return probeutil.EncField(b.String())
}

func opTType(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	name, err := a.Text(0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	out.Add("name\t%s", probeutil.EncField(name))
	resolved, ok := resolveTType(name)
	if !ok {
		out.Add("resolved\t0")
		return out, nil
	}
	out.Add("resolved\t1")
	out.Add("render\t%s", probeutil.EncField(resolved.String()))
	out.Add("parent\t%s", probeutil.EncField(resolved.Parent().String()))
	out.Add("depth\t%d", ttypeDepth(resolved))
	for _, other := range c.Spec.TTypeNames {
		target, ok := resolveTType(other)
		if !ok {
			out.Add("c\t%s\tunresolved", probeutil.EncField(other))
			continue
		}
		out.Add("c\t%s\t%s\t%s\t%s", probeutil.EncField(other),
			probeutil.BoolDigit(resolved.Contains(target)),
			probeutil.BoolDigit(target.Contains(resolved)),
			probeutil.BoolDigit(resolved == target))
	}
	return out, nil
}

// resolveTType walks a rendered name back to a token type.
//
// The reference creates types lazily on attribute access, so every well-formed
// name resolves -- `Token.Nonexistent` is not an error there, it is a new type.
// The port must do the same, which is what Sub is for.
func resolveTType(name string) (tokens.TokenType, bool) {
	parts := strings.Split(name, ".")
	if len(parts) == 0 || parts[0] != "Token" {
		return tokens.TokenType{}, false
	}
	node := tokens.Token
	for _, part := range parts[1:] {
		if part == "" {
			return tokens.TokenType{}, false
		}
		node = node.Sub(part)
	}
	return node, true
}

func ttypeDepth(t tokens.TokenType) int {
	s := t.String()
	if s == "Token" {
		return 0
	}
	return strings.Count(s, ".")
}

// -- keywords tier ----------------------------------------------------------

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

// ---------------------------------------------------------------------------
// The Node read surface.
//
// Hand-written rather than transformed from the monolith, because the monolith's
// version predated the current label list and the current accessor-scope design:
// it was missing 13 of the 46 labels and asked the submission a
// `HasAccessor(string) bool` question that no longer exists -- and should not,
// since it would be public API added for the grader and a self-description from
// the thing under test.
//
// Applicability comes from spec.json's accessor_scope, the same table probe.py
// reads, and excluded pairs render `no-method` on both sides. What a node of a
// given kind answers is therefore graded: a port whose Node answers everything
// everywhere fails exactly the excluded positions.
// ---------------------------------------------------------------------------

// The value renderers used below -- a node as kind+text, an (index, node)
// pair, and a node list -- are renderTokenOnly, renderTokenPair and
// renderNodeList, carried into this tier by the split rather than redeclared
// here.  Second copies under different names would collide with `renderNode`,
// which is the tree printer opTree recurses through and is a different
// function entirely.

// optStr renders one of the six accessors that return (value, ok).
//
// Three outcomes, and they are three different answers: the accessor does not
// apply to this kind (`no-method`), it applies and the reference has no value
// (`nil`), or it applies and the value is a string that may well be empty
// (`str` with an empty field). The last two are not the same: `select "" from t`
// parses to an Identifier whose get_name() is the empty string, while a bare
// Token's get_name() is None, which is why the contract has these six returning
// (string, bool) rather than collapsing both to "".
func optStr(value string, ok bool) string {
	if !ok {
		return "nil"
	}
	return "str\t" + probeutil.EncField(value)
}

// apiValue is one label's answer at one node.
//
// The error return is for defects only -- a label with no branch here, or a scope
// the spec does not define. A submission answering wrongly is not an error: it is
// a different string, which is what the comparison is for.
func apiValue(c *probeutil.Context, node, root *sql.Node, label string) (string, error) {
	applies, err := c.AccessorApplies(label, node.Kind().String())
	if err != nil {
		return "", err
	}
	if !applies {
		return "no-method", nil
	}
	switch label {
	// --- every kind answers ---------------------------------------------
	case "kind":
		return "str\t" + probeutil.EncField(node.Kind().String()), nil
	case "ttype":
		return "str\t" + probeutil.EncField(node.TType().String()), nil
	case "value":
		return "str\t" + probeutil.EncField(node.Value()), nil
	case "normalized":
		return "str\t" + probeutil.EncField(node.Normalized()), nil
	case "is_group":
		return "bool\t" + probeutil.BoolDigit(node.IsGroup()), nil
	case "is_keyword":
		return "bool\t" + probeutil.BoolDigit(node.IsKeyword()), nil
	case "is_whitespace":
		return "bool\t" + probeutil.BoolDigit(node.IsWhitespace()), nil
	case "is_newline":
		return "bool\t" + probeutil.BoolDigit(node.IsNewline()), nil
	case "str":
		return "str\t" + probeutil.EncField(node.String()), nil
	case "flatten_count":
		return fmt.Sprintf("int\t%d", len(node.Flatten())), nil

	// match() is the reference's own predicate and each of these grades a
	// different clause of it. The keyword comparison folds case and the others
	// do not; the type test is identity rather than containment, so a DML token
	// answers false against Token.Keyword; and the regex mode runs with
	// IGNORECASE off on a type that is not a keyword.
	case "match_kw_lower":
		return "bool\t" + probeutil.BoolDigit(node.Match(
			tokens.Keyword, []string{"from", "table", "as"}, false)), nil
	case "match_kw_subtype":
		return "bool\t" + probeutil.BoolDigit(node.Match(
			tokens.Keyword, []string{"SELECT", "INSERT"}, false)), nil
	case "match_punct_regex":
		return "bool\t" + probeutil.BoolDigit(node.Match(
			tokens.Punctuation, []string{`[(),]`}, true)), nil

	case "multiline_str":
		return "bool\t" + probeutil.BoolDigit(
			strings.Contains(node.String(), "\n")), nil
	case "within_function":
		return "bool\t" + probeutil.BoolDigit(node.Within(sql.KindFunction)), nil
	case "within_parenthesis":
		return "bool\t" + probeutil.BoolDigit(
			node.Within(sql.KindParenthesis)), nil
	case "parent_kind":
		if p := node.Parent(); p != nil {
			return "str\t" + probeutil.EncField(p.Kind().String()), nil
		}
		// The empty string, not `nil`: probe.py renders
		// `type(node.parent).__name__ if node.parent else ''` into a str field.
		return "str\t", nil
	case "has_ancestor_stmt":
		return "bool\t" + probeutil.BoolDigit(node.HasAncestor(root)), nil
	case "is_child_of_root":
		return "bool\t" + probeutil.BoolDigit(node.IsChildOf(root)), nil
	case "token_index_self":
		parent := node.Parent()
		if parent == nil {
			// A precondition the reference evaluates at run time rather than a
			// property of the kind, so it renders apart from `no-method`.
			return "no-parent", nil
		}
		// 0 is the reference's default `start`: probe.py calls
		// `parent.token_index(node)` with no second argument, and upstream
		// declares `token_index(self, token, start=0)`.
		return fmt.Sprintf("int\t%d", parent.TokenIndex(node, 0)), nil

	// --- groups only -----------------------------------------------------
	case "token_count":
		return fmt.Sprintf("int\t%d", len(node.Tokens())), nil
	case "get_real_name":
		return optStr(node.RealName()), nil
	case "get_name":
		return optStr(node.Name()), nil
	case "get_parent_name":
		return optStr(node.ParentName()), nil
	case "get_alias":
		return optStr(node.Alias()), nil
	case "has_alias":
		return "bool\t" + probeutil.BoolDigit(node.HasAlias()), nil
	case "get_sublists":
		return fmt.Sprintf("int\t%d", len(node.Sublists())), nil
	case "token_first":
		return renderTokenOnly(node.TokenFirst(true, true)), nil
	case "token_first_ws":
		return renderTokenOnly(node.TokenFirst(false, false)), nil
	case "token_next_0":
		idx, n := node.TokenNext(0, true, true)
		return renderTokenPair(idx, n), nil
	case "token_prev_last":
		idx, n := node.TokenPrev(1, true, true)
		return renderTokenPair(idx, n), nil
	case "token_at_offset_0":
		return renderTokenOnly(node.TokenAtOffset(0)), nil
	case "token_at_offset_mid":
		// Halfway through the node's own text, in code points -- the reference
		// indexes a str, so a byte offset would address a different token in
		// any document with a multi-byte character.
		return renderTokenOnly(node.TokenAtOffset(
			probeutil.RuneLen(node.String()) / 2)), nil

	// --- one kind each ---------------------------------------------------
	case "get_type":
		return "str\t" + probeutil.EncField(node.TypeName()), nil
	case "get_ordering":
		return optStr(node.Ordering()), nil
	case "get_typecast":
		return optStr(node.Typecast()), nil
	case "get_array_indices":
		return renderNodeList(node.ArrayIndices()), nil
	case "is_wildcard":
		return "bool\t" + probeutil.BoolDigit(node.IsWildcard()), nil
	case "get_identifiers":
		return renderNodeList(node.Identifiers()), nil
	case "get_parameters":
		return renderNodeList(node.Parameters()), nil
	case "get_window":
		// The one contracted accessor that returns an error, because the
		// reference raises here for every function with no OVER clause (see
		// apiValueSafe). Both shapes a conforming port can produce are answered
		// the same way probe.py answers them: a returned error renders as
		// `raise` plus its category, and a port that panics instead is caught by
		// apiValueSafe and renders identically.
		win, err := node.Window()
		if err != nil {
			return "raise\t" + strings.Join(errorLines(err), "\t"), nil
		}
		return renderTokenOnly(win), nil
	case "get_cases":
		return renderCases(node.Cases(false)), nil
	case "get_cases_skip":
		return renderCases(node.Cases(true)), nil
	case "left":
		return renderTokenOnly(node.Left()), nil
	case "right":
		return renderTokenOnly(node.Right()), nil
	case "comment_is_multiline":
		return "bool\t" + probeutil.BoolDigit(node.IsMultiline()), nil
	}
	return "", probeutil.Defectf("api label %q has no branch", label)
}

// renderCases is get_cases: a list of (condition, value) pairs.
//
// Rendered structurally because str() on the reference's tuples emits Python
// reprs, which carry object addresses -- different on every run and impossible to
// produce from Go.
func renderCases(cases [][2][]*sql.Node) string {
	parts := []string{fmt.Sprintf("cases\t%d", len(cases))}
	for _, pair := range cases {
		parts = append(parts, side(pair[0])+"=>"+side(pair[1]))
	}
	return strings.Join(parts, "\t")
}

// side is one half of a case: nil where the reference has None, otherwise the
// concatenated text of the tokens.
//
// A nil slice and an empty one are both `nil` here. The reference distinguishes
// None from [], but get_cases never produces an empty list -- it appends a token
// before it ever closes a side -- so the collapse is unobservable, and a
// distinction that cannot arise is not worth a wire format that carries it.
func side(nodes []*sql.Node) string {
	if nodes == nil {
		return "nil"
	}
	var b strings.Builder
	for _, n := range nodes {
		b.WriteString(n.String())
	}
	return probeutil.EncField(b.String())
}

// apiValueSafe is apiValue with the submission's panics turned into answers.
//
// One accessor in the reference raises for a whole class of nodes, and it is
// graded rather than repaired: Function.get_window dereferences the (None, None)
// that token_next_by returns when there is no OVER clause, because
// `not (None, None)` is false -- a populated tuple is truthy -- so the guard never
// fires. Every function without a window fails that way, and probe.py answers
// `raise` plus the portable category `nil-deref`.
//
// A Go port replicating that reaches the same place and panics with a nil pointer
// dereference, which is recoverable. Recovering it *per label* rather than per op
// is what makes the answers line up: a panic caught at the op level would lose
// the other 45 labels at that node and every label at every node after it, so a
// port with one bug would score zero on the whole family instead of failing the
// positions that actually differ.
//
// The category comes from probeutil.ErrorCategory, which maps a Go nil-pointer
// panic to `nil-deref` -- the same string probe.py produces for the
// AttributeError. A panic that is not a nil dereference renders as its own
// category, so a port that panics somewhere the reference does not still fails
// only where it panics.
func apiValueSafe(c *probeutil.Context, node, root *sql.Node, label string) (out string, err error) {
	defer func() {
		if rec := recover(); rec != nil {
			if e, ok := rec.(error); ok && probeutil.IsDefect(e) {
				// A defect is the probe disagreeing with itself and must not be
				// absorbed into an answer.
				err = e
				return
			}
			// Through errorLines rather than hand-rendering the category
			// shape, which is what this did. A recovered panic always takes
			// the category branch, so the answer is unchanged today -- but
			// probe.py joins all three of error_lines' shapes onto `raise`
			// here, and a second renderer that happens to agree on one of
			// them is a renderer that will disagree on the others.
			out = "raise\t" + strings.Join(errorLines(
				fmt.Errorf("panic: %v", rec)), "\t")
			err = nil
		}
	}()
	return apiValue(c, node, root, label)
}

// Generated from probe.py by _work/lang03/gen_model_methods.py. DO NOT EDIT.
//
// The label list and the address list are the probe's own structure, and both
// halves must walk them in the same order or every answer has its lines in a
// different place. They are read out of probe.py rather than written down twice.

// 46 labels, in probe.py's order.
var apiMethods = []string{
	"kind",
	"ttype",
	"value",
	"normalized",
	"is_group",
	"is_keyword",
	"is_whitespace",
	"is_newline",
	"str",
	"flatten_count",
	"match_kw_lower",
	"match_kw_subtype",
	"match_punct_regex",
	"multiline_str",
	"within_function",
	"within_parenthesis",
	"parent_kind",
	"has_ancestor_stmt",
	"is_child_of_root",
	"token_index_self",
	"token_count",
	"get_real_name",
	"get_name",
	"get_parent_name",
	"get_alias",
	"has_alias",
	"get_sublists",
	"token_first",
	"token_first_ws",
	"token_next_0",
	"token_prev_last",
	"token_at_offset_0",
	"token_at_offset_mid",
	"get_type",
	"get_ordering",
	"get_typecast",
	"get_array_indices",
	"is_wildcard",
	"get_identifiers",
	"get_parameters",
	"get_window",
	"get_cases",
	"get_cases_skip",
	"left",
	"right",
	"comment_is_multiline",
}

// 14 child-index paths from the statement; "" is the statement itself.
var apiAddresses = []string{
	"",
	"0",
	"1",
	"2",
	"0.0",
	"1.0",
	"2.0",
	"2.1",
	"2.2",
	"0.0.0",
	"2.0.0",
	"-1",
	"-2",
	"-1.-1",
}
