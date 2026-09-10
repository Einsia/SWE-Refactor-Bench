// Command probe-keywords answers the `keywords` tier of the differential probe.
//
// This tier imports two packages of the submission and nothing else:
//
//	github.com/andialbrecht/sqlparse-go/keywords
//	github.com/andialbrecht/sqlparse-go/tokens
//
// It is the only one of the four tiers whose closure is genuinely isolated --
// core, model and parts are nested -- and that isolation is the point. A
// submission whose lexer does not compile, or whose parser is half finished,
// still answers every case here, because a keyword table is a table. That is
// real work made visible, and the tier split exists to show it.
//
// The consequence for this file is a rule it must not break: nothing here may
// reach for the lexer, even indirectly. The reference keeps keyword resolution
// on Lexer.is_keyword, so the obvious port of the `keyword` op would call
// lexer.Default().IsKeyword -- and would drag the whole tokenizer into a binary
// whose reason to exist is that it does not need one. The contract puts
// keywords.Lookup in the API for exactly this reason: it is the same resolution,
// available without a lexer, and the two are contracted to agree.
package main

import (
	"sort"
	"strings"

	"probe/probeutil"

	"github.com/andialbrecht/sqlparse-go/keywords"
	"github.com/andialbrecht/sqlparse-go/tokens"
)

func main() {
	r := probeutil.NewRegistry()
	// No RenderError: nothing in this tier's three ops raises the library's own
	// error type. A failure here is a decode failure or a bad argument, both of
	// which ErrorCategory already renders portably, and claiming to render an
	// error type this closure cannot even name would be a lie in the code.
	r.Register("kwtable", opKwTable)
	r.Register("kwnames", opKwNames)
	r.Register("keyword", opKeyword)
	probeutil.Main(r)
}

// tableOrder is the registration order Lookup consults, read from the
// submission rather than written down here.
//
// Written down, it would be a second copy of a fact the submission already
// states, and the two would eventually disagree -- at which point a correct port
// would fail for being correct. TableNames is in the contract as "the names in
// registration order, NOT sorted" precisely so this file has something to read.
func tableOrder() []string {
	return keywords.TableNames()
}

// opKwTable dumps one whole keyword table, or answers one of two meta queries.
//
// Sorted by word, because the reference's dicts have insertion order and Go's
// maps have none: unsorted output would be a coin flip that fails on rerun. The
// resolution order across tables is a separate question, and `keyword` asks it.
func opKwTable(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	name, err := a.Text(0)
	if err != nil {
		return nil, err
	}
	out := &probeutil.Answer{}
	switch name {
	case "__order__":
		names := tableOrder()
		line := "order"
		for _, n := range names {
			line += "\t" + n
		}
		out.Add("%s", line)
		return out, nil
	case "__regex__":
		// The pattern list's length, not its contents. A regex written for Go's
		// RE2 is not the same string as one written for Python's re even when it
		// matches the same language, so the strings are not comparable and the
		// tokenizer behavior they produce is graded through `lex` instead.
		out.Add("count\t%d", len(keywords.Rules()))
		return out, nil
	}
	table := keywords.Table(name)
	if table == nil {
		// Answered, not raised, and specifically not a defect. The catalog only
		// names tables the contract publishes, so a nil here means the
		// submission does not have one it was required to have -- that is a
		// failed case. `defect` is reserved for the probe disagreeing with
		// itself, and it stops the whole run as a verifier bug; charging a
		// submission's missing table to it would report the grader as broken.
		//
		// The reference answers this name with a dump, so `missing` can never
		// match and the case fails, which is the intended outcome.
		out.Add("missing\t%s", probeutil.EncField(name))
		return out, nil
	}
	words := make([]string, 0, len(table))
	for word := range table {
		words = append(words, word)
	}
	sort.Strings(words)
	out.Add("table\t%s", probeutil.EncField(name))
	out.Add("count\t%d", len(table))
	for _, word := range words {
		out.Add("e\t%s\t%s", probeutil.EncField(word),
			probeutil.EncField(table[word].String()))
	}
	return out, nil
}

// opKwNames reports which tables exist, in registration order, with sizes.
//
// A port that merged the nine tables into one map answers every individual
// lookup correctly and fails here. That is deliberate: the tables are published
// surface, and the order they are consulted in is observable through the three
// words that live in two of them.
func opKwNames(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	names := tableOrder()
	out := &probeutil.Answer{}
	out.Add("count\t%d", len(names))
	total := 0
	for _, name := range names {
		table := keywords.Table(name)
		if table == nil {
			// A submission whose TableNames lists a name its Table cannot
			// return is internally inconsistent, which is a real defect in the
			// submission -- but it is still the submission's, so it is answered
			// rather than raised. The size line is what the reference emits
			// here, so -1 fails the case and says why in the report.
			out.Add("t\t%s\t-1", probeutil.EncField(name))
			continue
		}
		total += len(table)
		out.Add("t\t%s\t%d", probeutil.EncField(name), len(table))
	}
	out.Add("entries\t%d", total)
	return out, nil
}

// opKeyword resolves one word, and reports which tables hold it.
//
// The two-value answer is the reference's protocol: is_keyword returns a type
// and the value the lexer would carry, and on a miss the type is tokens.Name
// rather than nothing. The `tables` line is the part that catches a merged
// table: a word in two tables resolves through whichever comes first in
// registration order, and reporting the membership separately from the
// resolution means a port that gets the order wrong fails on the resolution
// while still showing the probe why.
func opKeyword(c *probeutil.Context, a *probeutil.Args) (*probeutil.Answer, error) {
	word, err := a.Text(0)
	if err != nil {
		return nil, err
	}
	ttype, found := keywords.Lookup(word)
	// The value the reference carries is the word as given, not the upper-cased
	// form it looked up with: the lookup is upper-cased, the table is not, and
	// the token keeps the source text. A port that returns the normalized form
	// passes every type comparison and fails every value comparison.
	value := word
	out := &probeutil.Answer{}
	out.Add("word\t%s", probeutil.EncField(word))
	out.Add("ttype\t%s", probeutil.EncField(ttype.String()))
	out.Add("value\t%s", probeutil.EncField(value))
	// `found` is checked against the type rather than reported. The reference's
	// is_keyword has no boolean -- it returns tokens.Name for a miss -- so a
	// `found` line would be a field only one half of the pair can produce. What
	// is portable is the invariant the contract states: a miss returns
	// (tokens.Name, false). A submission whose two return values disagree is
	// wrong, and this line is where that shows up as a failed case.
	if !found && ttype != tokens.Name {
		out.Add("inconsistent\tmiss-with-type\t%s",
			probeutil.EncField(ttype.String()))
	}
	// Membership is computed with the same upper-casing the lookup uses, and
	// against the same tables, so this line and the ttype line cannot disagree
	// about what the tables contain -- only about which one won.
	upper := strings.ToUpper(word)
	line := "tables"
	for _, name := range tableOrder() {
		if _, ok := keywords.Table(name)[upper]; ok {
			line += "\t" + name
		}
	}
	out.Add("%s", line)
	return out, nil
}

// strings.ToUpper, not an ASCII-only fold, and the choice is measured rather
// than assumed.
//
// The reference upper-cases the lookup with Python's str.upper(), which applies
// full Unicode case mapping. The catalog drives two non-ASCII words at this op
// -- `sélect` and `中文` -- and on the first, Python gives SÉLECT while an
// ASCII-only fold gives SéLECT. Go's strings.ToUpper gives SÉLECT, so it agrees.
//
// The one place the two still part is multi-character expansion: Python maps 'ß'
// to "SS" and Go leaves it alone. That cannot change an answer here, because no
// expansion of a non-ASCII letter spells a word in an ASCII keyword table, so
// both halves miss and both report tokens.Name. It is written down rather than
// left implicit because the reasoning is the only thing keeping this line
// portable, and a later document set addition could invalidate it.
