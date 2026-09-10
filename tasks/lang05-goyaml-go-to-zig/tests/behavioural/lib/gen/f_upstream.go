package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
)

// The upstream family is gopkg.in/yaml.v3's own test inputs, extracted by
// _work/lang05/harvest and read here as a file.
//
// It is the most valuable family per case and the cheapest to produce, for the
// same reason: someone else already did the thinking.  Every string in it is an
// input go-yaml's authors chose deliberately -- the timestamp near-misses, the
// 1,536-byte comment that straddles the scanner's buffer refill, the merge-key
// document out of the 1.1 spec, the emitter's own canonical output fed back in.
// A hand-written set is systematically bad at that last group: I write the
// YAML a person writes, and the emitter writes YAML no person writes.
//
// Only the inputs come across.  The expected values in those tables are Go
// values for the decode path this task does not grade, and transcribing them
// would put a second authority next to the reference.
//
// The harvest is an input to this generator rather than a shipped artifact: what
// ships is requests.ndjson, with these sources inlined like every other case.

type harvestRow struct {
	Source string `json:"source"`
	Origin string `json:"origin"`
	Marked bool   `json:"marked"`
}

func readHarvest(path string) ([]harvestRow, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 1<<16), 1<<22)
	var rows []harvestRow
	for sc.Scan() {
		if len(sc.Bytes()) == 0 {
			continue
		}
		var r harvestRow
		if err := json.Unmarshal(sc.Bytes(), &r); err != nil {
			return nil, err
		}
		rows = append(rows, r)
	}
	return rows, sc.Err()
}

// Every upstream input is observed under the full set of ops.  That is worth the
// multiplication here in a way it would not be for a hand-written shape: these
// inputs were selected for being awkward, and an awkward input is exactly the one
// whose node tree is right while its re-emission is wrong.
func familyUpstream(g *gen, rows []harvestRow) {
	const fam = "upstream"
	for _, r := range rows {
		label := fmt.Sprintf("%s %q", r.Origin, r.Source)
		g.node(fam, label, r.Source)
		g.add(fam, "emit", label, r.Source, nil)
		g.add(fam, "stream", label, r.Source, nil)
		g.add(fam, "emit_stream", label, r.Source, nil)
		g.add(fam, "roundtrip", label, r.Source, nil)
		g.indentAt(fam, label, r.Source, 4)
	}
}
