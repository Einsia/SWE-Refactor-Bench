// Command reference is the reference side of the lang05 differential test:
// gopkg.in/yaml.v3 v3.0.1, in Go, answering the NDJSON protocol that
// instruction.md specifies for `yaml-probe`.
//
// The protocol itself is not implemented here.  It is `gopkg.in/yaml.v3/probe`,
// a package of the library under test, and this program is a driver around it --
// the same package State A's own `cmd/yaml-probe` is a driver around.  That is
// deliberate and it is the whole reason the reference can be trusted: there is
// one implementation of the response encoding, it ships in the repository the
// agent is given, and a submission is graded against the bytes it produces
// rather than against a private copy that could drift from it.
//
// What this file adds is the two bulk modes the grading pipeline needs and a
// probe binary has no use for.  Freezing ten thousand expectations through a pipe
// would work; doing it in one process is faster and cannot lose a line to a
// transport bug.
//
// Modes:
//
//	--batch REQ OUT          answer a request file into a response file, used
//	                         at image build time to freeze expectations
//	--documents MAN OUT DIR  digest the responses for the twelve real-world
//	                         files, which are graded by hash rather than kept
//	serve (default)          act as the probe on stdin/stdout, so the reference
//	                         can be graded against itself as an identity check
//
// The expectations frozen into the verifier image are this program's output, so
// a disagreement between the prose and the library is resolved in the library's
// favour -- the same rule the instruction states: behaviour is defined against
// the implementation, not against the description.
package main

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3/probe"
)

// The version of gopkg.in/yaml.v3 this driver is linked against, read from the
// library rather than restated here.  `hello` is asked through the protocol at
// image build time and checked against the frozen manifest, so a reference built
// against the wrong module is caught there.
const upstreamVersion = probe.Version

func batch(reqPath, outPath string) {
	in, err := os.Open(reqPath)
	must(err)
	defer in.Close()
	out, err := os.Create(outPath)
	must(err)
	w := bufio.NewWriterSize(out, 1<<20)

	sc := probe.NewScanner(in)
	count := 0
	for sc.Scan() {
		line := sc.Bytes()
		if len(line) == 0 {
			continue
		}
		if _, err := w.WriteString(probe.Answer(line)); err != nil {
			fatal("write: %v", err)
		}
		if err := w.WriteByte('\n'); err != nil {
			fatal("write: %v", err)
		}
		count++
	}
	must(sc.Err())
	// Flush and close explicitly, and check both.  A buffered writer abandoned
	// at exit produces a short expectation file, and every case past the
	// truncation point would then grade as a mismatch -- a harness failure
	// wearing a submission's clothes.
	must(w.Flush())
	must(out.Close())
	fmt.Fprintf(os.Stderr, "reference: answered %d requests\n", count)
}

// Documents are graded by digest: a whole Kubernetes manifest rendered as a node
// tree is megabytes of JSON, and keeping those bytes in a container image to
// compare them once is not a good trade.  Storing only a whole-response digest
// would make a failure unactionable though -- "your 4 MB answer is wrong
// somewhere" is not a bug report -- so each entry also carries a digest per
// fixed-size block, which localises the first divergence to one block without
// keeping any of the bytes.
const blockSize = 32768

func blocksFor(buf []byte) []string {
	var out []string
	for at := 0; at < len(buf); at += blockSize {
		end := at + blockSize
		if end > len(buf) {
			end = len(buf)
		}
		sum := sha256.Sum256(buf[at:end])
		out = append(out, hex.EncodeToString(sum[:])[:16])
	}
	return out
}

type documentEntry struct {
	Key    string `json:"key"`
	File   string `json:"file"`
	Op     string `json:"op"`
	Indent *int   `json:"indent"`
}

func documents(manPath, outPath, dir string) {
	blob, err := os.ReadFile(manPath)
	must(err)
	var man struct {
		Documents []documentEntry `json:"documents"`
	}
	must(json.Unmarshal(blob, &man))

	digests := map[string]any{}
	for _, e := range man.Documents {
		src, err := os.ReadFile(filepath.Join(dir, e.File))
		must(err)
		req := probe.Request{ID: 1, Op: e.Op, Source: string(src)}
		if e.Indent != nil {
			req.Indent = *e.Indent
			req.HasIndent = true
		}
		text := probe.AnswerRequest(req)
		buf := []byte(text)
		sum := sha256.Sum256(buf)
		digests[e.Key] = map[string]any{
			"bytes":  len(buf),
			"sha256": hex.EncodeToString(sum[:]),
			"block":  blockSize,
			"blocks": blocksFor(buf),
		}
		fmt.Fprintf(os.Stderr, "reference: %s %d bytes\n", e.Key, len(buf))
	}
	enc, err := json.MarshalIndent(map[string]any{"digests": digests}, "", " ")
	must(err)
	must(os.WriteFile(outPath, append(enc, '\n'), 0o644))
}

// serve is the identity path: the reference answering on stdin/stdout exactly as
// a submission's probe must.  The transport is the library's own, so this is the
// same loop `cmd/yaml-probe` runs.
func serve() {
	if err := probe.Serve(os.Stdin, os.Stdout); err != nil {
		os.Exit(1)
	}
}

func must(err error) {
	if err != nil {
		fatal("%v", err)
	}
}

func fatal(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "reference: "+format+"\n", args...)
	os.Exit(3)
}

func main() {
	args := os.Args[1:]
	switch {
	case len(args) == 3 && args[0] == "--batch":
		batch(args[1], args[2])
	case len(args) == 4 && args[0] == "--documents":
		documents(args[1], args[2], args[3])
	case len(args) == 0 || (len(args) == 1 && args[0] == "serve"):
		serve()
	case len(args) == 1 && args[0] == "--version":
		fmt.Println(upstreamVersion)
	default:
		fatal("usage: reference [--batch REQ OUT | --documents MAN OUT DIR | serve]")
	}
}
