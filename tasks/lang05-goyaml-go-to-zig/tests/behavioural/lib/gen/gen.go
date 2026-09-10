// Command generator writes the differential cases: a request file, a case
// index, and a document manifest.
//
// It never consults the reference implementation.  It produces inputs only, and
// the reference turns those inputs into expectations in a separate pass, so a
// mistake here shows up as a weak case rather than as a case that grades a
// submission against the generator's own idea of the answer.
//
// The set is systematic rather than random.  Every family enumerates a
// hand-written axis of the input language and crosses it with the operations
// that can observe it, which makes it reviewable: a reader can ask "is the
// escape table complete" and check, where a fuzzer's output only answers "did
// anything crash".
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"
)

// A case is one request plus the metadata the verifier scores it by.  The
// request itself is written to a separate file so the reference can answer them
// all in one pass without carrying the metadata through.
type Case struct {
	ID     int    `json:"id"`
	Family string `json:"family"`
	Op     string `json:"op"`
	Label  string `json:"label"`
	Source string `json:"source,omitempty"`
	Indent *int   `json:"indent,omitempty"`
}

type gen struct {
	cases []Case
	next  int
	seen  map[string]bool // family|op|source|indent, to catch accidental duplicates
	dups  int
}

func (g *gen) add(family, op, label, source string, indent *int) {
	key := family + "\x00" + op + "\x00" + source
	if indent != nil {
		key += fmt.Sprintf("\x00%d", *indent)
	}
	// A duplicate is not harmless: it inflates a family's weight without adding
	// evidence, so one defect would cost two cases.  Counted and reported
	// rather than silently dropped, because a duplicate usually means two axes
	// overlap and the generator is what needs fixing.
	if g.seen[key] {
		g.dups++
		return
	}
	g.seen[key] = true
	g.next++
	g.cases = append(g.cases, Case{
		ID: g.next, Family: family, Op: op, Label: label, Source: source, Indent: indent,
	})
}

// node is the shorthand for the common shape: one input, observed as a node
// tree.  Most families use it plus emit.
func (g *gen) node(family, label, source string) {
	g.add(family, "node", label, source, nil)
}

func (g *gen) nodeEmit(family, label, source string) {
	g.add(family, "node", label, source, nil)
	g.add(family, "emit", label, source, nil)
}

func (g *gen) indentAt(family, label, source string, n int) {
	v := n
	g.add(family, "emit_indent", fmt.Sprintf("%s indent=%d", label, n), source, &v)
}

func main() {
	out := flag.String("out", "", "directory to write the cases into")
	harvest := flag.String("harvest", "", "harvest.ndjson: upstream's own test inputs")
	seed := flag.Uint64("seed", 0, "with --count, generate the fresh cases from this seed")
	count := flag.Int("count", 0, "number of fresh cases to generate")
	flag.Parse()
	if *out == "" {
		fmt.Fprintln(os.Stderr, "generator: --out is required")
		os.Exit(2)
	}

	// The fresh mode reuses the systematic cases as mutation seeds, so the
	// systematic pass runs either way.  That also keeps one property true: the
	// fresh set for a given seed is a function of this binary alone, not of
	// which files happened to be lying around next to it.
	g := &gen{seen: map[string]bool{}}
	familyScalarPlain(g)
	familyScalarQuoted(g)
	familyScalarBlock(g)
	familyFlow(g)
	familyBlockCollection(g)
	familyAnchorAlias(g)
	familyTag(g)
	familyComment(g)
	familyDocument(g)
	familyIndent(g)
	familyUnicode(g)
	familyWhitespace(g)
	familyError(g)
	familyLimit(g)
	familyRoundtrip(g)
	familyCompose(g)

	// The upstream family is optional so this generator still runs without the
	// harvest file, but not silently: a set that quietly lost its most valuable
	// family would still look like a set, and catalog.py's missing-family check
	// is the backstop rather than the first line of defence.
	if *harvest != "" {
		rows, err := readHarvest(*harvest)
		if err != nil {
			fmt.Fprintf(os.Stderr, "generator: harvest: %v\n", err)
			os.Exit(1)
		}
		familyUpstream(g, rows)
		fmt.Fprintf(os.Stderr, "generator: harvest contributed %d inputs\n", len(rows))
	} else {
		fmt.Fprintln(os.Stderr, "generator: no --harvest given; the upstream family is absent")
	}

	if *count > 0 {
		// Mutation seeds are the systematic sources, deduplicated and in case
		// order so the fresh set is reproducible from the seed alone.
		seeds := make([]string, 0, len(g.cases))
		seen := map[string]bool{}
		for _, c := range g.cases {
			if !seen[c.Source] {
				seen[c.Source] = true
				seeds = append(seeds, c.Source)
			}
		}
		fresh := freshCases(*seed, *count, seeds)
		if err := writeFresh(*out, fresh, *seed); err != nil {
			fmt.Fprintf(os.Stderr, "generator: %v\n", err)
			os.Exit(1)
		}
		return
	}

	if err := write(*out, g); err != nil {
		fmt.Fprintf(os.Stderr, "generator: %v\n", err)
		os.Exit(1)
	}
}

// writeFresh emits the same two files under fresh- names.  Separate names rather
// than a separate directory: both sets are read by the same verifier in the same
// pass, and a path that differs only by its parent directory is a path that gets
// crossed.
func writeFresh(dir string, g *gen, seed uint64) error {
	if err := writeRequests(dir+"/fresh-requests.ndjson", g); err != nil {
		return err
	}
	if err := writeCases(dir+"/fresh-cases.json", g, seed); err != nil {
		return err
	}
	report(g, fmt.Sprintf("fresh cases, seed %d", seed))
	return nil
}

func counts(g *gen) (fams map[string]int, ops map[string]int) {
	fams, ops = map[string]int{}, map[string]int{}
	for _, c := range g.cases {
		fams[c.Family]++
		ops[c.Op]++
	}
	return fams, ops
}

// requests.ndjson: exactly what the probe is fed, one JSON object per line.
func writeRequests(path string, g *gen) error {
	var reqs strings.Builder
	for _, c := range g.cases {
		m := map[string]any{"id": c.ID, "op": c.Op, "source": c.Source}
		if c.Indent != nil {
			m["indent"] = *c.Indent
		}
		blob, err := json.Marshal(m)
		if err != nil {
			return fmt.Errorf("case %d: %w", c.ID, err)
		}
		reqs.Write(blob)
		reqs.WriteByte('\n')
	}
	return os.WriteFile(path, []byte(reqs.String()), 0o644)
}

// cases.json: the metadata, without the source text.  The verifier reads this to
// know a case's family and to render a label; keeping a second copy of every
// input in it would carry every source twice in the image.
//
// `families` is written alongside `cases` because catalog.py needs the counts to
// turn family budgets into per-case weights, and deriving them there would mean
// two files had to be read in the right order to know what anything is worth.
func writeCases(path string, g *gen, seed uint64) error {
	slim := make([]Case, len(g.cases))
	for i, c := range g.cases {
		slim[i] = Case{ID: c.ID, Family: c.Family, Op: c.Op, Label: c.Label, Indent: c.Indent}
	}
	fams, ops := counts(g)
	// `count` is redundant with len(cases) on purpose.  A case file truncated in
	// transit is still valid JSON up to the truncation point, so without a declared
	// total the verifier would happily grade a short file as a small one.
	payload := map[string]any{
		"count":      len(slim),
		"cases":      slim,
		"families":   fams,
		"ops":        ops,
		"duplicates": g.dups,
	}
	if seed != 0 {
		payload["seed"] = seed
	}
	blob, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(blob, '\n'), 0o644)
}

func report(g *gen, label string) {
	fams, ops := counts(g)
	names := make([]string, 0, len(fams))
	for k := range fams {
		names = append(names, k)
	}
	sort.Strings(names)
	fmt.Fprintf(os.Stderr, "generator: %s: %d cases, %d duplicates suppressed\n",
		label, len(g.cases), g.dups)
	for _, n := range names {
		fmt.Fprintf(os.Stderr, "  %-20s %5d\n", n, fams[n])
	}
	opNames := make([]string, 0, len(ops))
	for k := range ops {
		opNames = append(opNames, k)
	}
	sort.Strings(opNames)
	fmt.Fprintf(os.Stderr, "  ops:")
	for _, n := range opNames {
		fmt.Fprintf(os.Stderr, " %s=%d", n, ops[n])
	}
	fmt.Fprintln(os.Stderr)
}

func write(dir string, g *gen) error {
	if err := writeRequests(dir+"/requests.ndjson", g); err != nil {
		return err
	}
	if err := writeCases(dir+"/cases.json", g, 0); err != nil {
		return err
	}
	// The manifest names the inputs; the reference turns it into
	// document-digests.json.  Two filenames rather than one, because a single
	// documents.json on both sides of that step is a file the wrong half of the
	// pipeline overwrites.
	if err := os.WriteFile(dir+"/document-manifest.json", documentManifest(), 0o644); err != nil {
		return err
	}
	report(g, "systematic cases")
	return nil
}
