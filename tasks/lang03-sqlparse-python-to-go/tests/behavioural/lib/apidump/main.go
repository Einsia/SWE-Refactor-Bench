// Dumps the exported API of a Go source tree using only the standard library,
// so the verifier needs no module downloads to check the interface contract.
package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type pkgAPI struct {
	Name    string   `json:"name"`
	Files   int      `json:"files"`
	Symbols []string `json:"symbols"`
	// Fields is kept separate from Symbols because the two are checked
	// differently: Symbols is a closed world (anything extra fails the gate),
	// Fields is consulted only for the structs whose contract entry declares a
	// field list.  Merging them would make every internal field of every
	// exported struct a gate violation.
	Fields []string `json:"fields"`
	// PackageDoc is true when any file of the package carries a package comment.
	// Go's convention puts it on one file only, so this is a package-level
	// property that cannot be derived from a per-file walk after the fact.
	PackageDoc bool `json:"package_doc"`
	// Documented and Undocumented carry bare identifier names -- `Format`,
	// `Node.Alias` -- rather than the rendered signatures in Symbols.  The doc
	// case asks a different question from the signature case ("is this
	// explained", not "is this the right shape"), and a signature mismatch
	// should not also read as a missing doc comment.
	Documented   []string `json:"documented"`
	Undocumented []string `json:"undocumented"`
}

// note records one exported symbol's doc-comment status under its bare name.
func (a *pkgAPI) note(name string, documented bool) {
	if documented {
		a.Documented = append(a.Documented, name)
		return
	}
	a.Undocumented = append(a.Undocumented, name)
}

func main() {
	root := os.Args[1]
	out := map[string]*pkgAPI{}
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			base := d.Name()
			if base == "vendor" || base == "testdata" || strings.HasPrefix(base, ".") && base != "." {
				return fs.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		rel, _ := filepath.Rel(root, filepath.Dir(p))
		fset := token.NewFileSet()
		// ParseComments is required: without it every Doc field is nil and every
		// exported symbol reads as undocumented, which is indistinguishable from
		// a submission that wrote no comments at all.
		f, perr := parser.ParseFile(fset, p, nil,
			parser.SkipObjectResolution|parser.ParseComments)
		if perr != nil {
			return nil
		}
		api := out[rel]
		if api == nil {
			// Every slice starts empty rather than nil: `"undocumented": []` and a
			// missing key both mean "nothing", but only one of them says so, and
			// the reader on the other side distinguishes absent from empty.
			api = &pkgAPI{
				Name:         f.Name.Name,
				Symbols:      []string{},
				Fields:       []string{},
				Documented:   []string{},
				Undocumented: []string{},
			}
			out[rel] = api
		}
		api.Files++
		if f.Doc != nil && strings.TrimSpace(f.Doc.Text()) != "" {
			api.PackageDoc = true
		}
		for _, decl := range f.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if !d.Name.IsExported() {
					continue
				}
				recv := ""
				bare := d.Name.Name
				if d.Recv != nil && len(d.Recv.List) > 0 {
					recv = typeString(d.Recv.List[0].Type)
					if !ast.IsExported(strings.TrimLeft(recv, "*")) {
						continue
					}
					bare = strings.TrimLeft(recv, "*") + "." + d.Name.Name
					recv = "(" + recv + ")."
				}
				api.Symbols = append(api.Symbols,
					"func "+recv+d.Name.Name+sigString(d.Type))
				api.note(bare, hasDoc(d.Doc))
			case *ast.GenDecl:
				for _, spec := range d.Specs {
					switch s := spec.(type) {
					case *ast.TypeSpec:
						if s.Name.IsExported() {
							api.Symbols = append(api.Symbols,
								"type "+s.Name.Name+" "+kindOf(s.Type))
							// A one-spec `type X struct{...}` usually carries its
							// comment on the GenDecl, a spec inside a `type (...)`
							// block carries its own.  Either counts.
							api.note(s.Name.Name, hasDoc(s.Doc) || hasDoc(d.Doc))
							// Exported fields of exported structs are emitted too.
							// A struct literal with field names is a compile-time
							// dependency exactly like a function signature, and the
							// probe has one (filters.ReindentConfig).  The dumper
							// reports every one it finds; deciding which ones are
							// binding is the gate's job, not the dumper's.
							if st, ok := s.Type.(*ast.StructType); ok {
								for _, f := range st.Fields.List {
									if len(f.Names) == 0 {
										// Embedded field: the type is the
										// name.  Rendered so an embedding
										// is visible rather than skipped.
										et := typeString(f.Type)
										if ast.IsExported(strings.TrimLeft(et, "*")) {
											api.Fields = append(api.Fields,
												"field "+s.Name.Name+"."+et)
										}
										continue
									}
									for _, n := range f.Names {
										if n.IsExported() {
											api.Fields = append(api.Fields,
												"field "+s.Name.Name+"."+n.Name+
													" "+typeString(f.Type))
										}
									}
								}
							}
						}
					case *ast.ValueSpec:
						for _, n := range s.Names {
							if n.IsExported() {
								// `var` and `const` are deliberately not
								// distinguished.  The gate checks that an
								// exported identifier of the right name exists;
								// which storage class a port picks for its Kind
								// values is its own business, and a
								// struct-valued token type cannot be a const at
								// all -- so enforcing one would demand different
								// renderings for two lists that are the same
								// kind of thing.
								api.Symbols = append(api.Symbols,
									"value "+n.Name)
								// A comment on the enclosing `var (...)` block
								// documents every name in it.  That is the
								// convention for a set of related values -- the 22
								// token kinds are one such set -- and demanding a
								// separate sentence per constant would be asking
								// for worse Go than the reference's own style.
								api.note(n.Name, hasDoc(s.Doc) || hasDoc(d.Doc))
							}
						}
					}
				}
			}
		}
		return nil
	})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, a := range out {
		sort.Strings(a.Symbols)
		sort.Strings(a.Fields)
		sort.Strings(a.Documented)
		sort.Strings(a.Undocumented)
	}
	json.NewEncoder(os.Stdout).Encode(out)
}

// hasDoc reports whether a comment group carries any prose.  An empty group and
// a nil one are the same thing to a reader, and `//` on its own line parses as
// the former.
func hasDoc(g *ast.CommentGroup) bool {
	return g != nil && strings.TrimSpace(g.Text()) != ""
}

func kindOf(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.StructType:
		return "struct"
	case *ast.InterfaceType:
		return "interface"
	default:
		_ = t
		return typeString(e)
	}
}

func sigString(ft *ast.FuncType) string {
	var in, outp []string
	if ft.Params != nil {
		for _, p := range ft.Params.List {
			n := len(p.Names)
			if n == 0 {
				n = 1
			}
			for i := 0; i < n; i++ {
				in = append(in, typeString(p.Type))
			}
		}
	}
	if ft.Results != nil {
		for _, p := range ft.Results.List {
			n := len(p.Names)
			if n == 0 {
				n = 1
			}
			for i := 0; i < n; i++ {
				outp = append(outp, typeString(p.Type))
			}
		}
	}
	s := "(" + strings.Join(in, ", ") + ")"
	if len(outp) == 1 {
		s += " " + outp[0]
	} else if len(outp) > 1 {
		s += " (" + strings.Join(outp, ", ") + ")"
	}
	return s
}

func typeString(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return "*" + typeString(t.X)
	case *ast.SelectorExpr:
		return typeString(t.X) + "." + t.Sel.Name
	case *ast.ArrayType:
		if t.Len == nil {
			return "[]" + typeString(t.Elt)
		}
		// The length is part of the written signature, so it is rendered
		// rather than elided: `[2]*Node` and `[3]*Node` are different
		// types and the gate compares written signatures. A length
		// expression this cannot render becomes `[?]`, which fails the
		// skeleton round-trip loudly instead of colliding with another
		// type's rendering.
		switch n := t.Len.(type) {
		case *ast.BasicLit:
			return "[" + n.Value + "]" + typeString(t.Elt)
		case *ast.Ident:
			return "[" + n.Name + "]" + typeString(t.Elt)
		}
		return "[?]" + typeString(t.Elt)
	case *ast.MapType:
		return "map[" + typeString(t.Key) + "]" + typeString(t.Value)
	case *ast.Ellipsis:
		return "..." + typeString(t.Elt)
	case *ast.FuncType:
		return "func" + sigString(t)
	case *ast.InterfaceType:
		if t.Methods == nil || len(t.Methods.List) == 0 {
			return "any"
		}
		return "interface{...}"
	case *ast.ChanType:
		return "chan " + typeString(t.Value)
	case *ast.StructType:
		return "struct{...}"
	case *ast.IndexExpr:
		return typeString(t.X) + "[" + typeString(t.Index) + "]"
	default:
		return "?"
	}
}
