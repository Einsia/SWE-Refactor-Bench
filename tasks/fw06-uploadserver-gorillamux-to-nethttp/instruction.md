# Migrate go-simple-upload-server from gorilla/mux to net/http

You are working in `/workspace/repo`, a frozen checkout of
**go-simple-upload-server v2.2.0** — a small HTTP server that accepts file
uploads and serves them back, with optional token auth, range requests,
conditional requests and a size limit. Its HTTP layer is **gorilla/mux 1.8.1**.

Your job is to migrate the **entire repository** to **`net/http.ServeMux`** from
the standard library, retiring gorilla/mux completely — while keeping the
server's observable behaviour identical.

This is a behaviour-preserving migration. **Do not add features, do not remove
configuration options, and do not change the public HTTP contract.** Every
response the server produces today it must still produce, byte for byte.

It is a small repository — 803 Go lines outside tests, one router import, and a
dispatch block of thirteen lines holding five route registrations and two
fallback handlers. It is small on purpose. The work is not volume: three of those
five registrations mean something `ServeMux` has no pattern for, and the
difficulty is in rebuilding that faithfully rather than in the number of lines you
touch. The 1858 lines of tests that exercise those routes are part of the job too.

---

## 1. The two states

|                       | State A (now)                                            | State B (required)                                     |
| --------------------- | -------------------------------------------------------- | ------------------------------------------------------ |
| Router                | `github.com/gorilla/mux` v1.8.1                           | `net/http.ServeMux`                                     |
| Route registration    | `r.HandleFunc(path, h).Methods(...)`                      | method patterns: `mux.Handle("POST /upload", h)`        |
| Prefix routes         | `r.PathPrefix("/files").Methods(...)`                     | **no equivalent — see §4**                              |
| Path parameters       | not used (the handlers read `r.URL.Path` directly)        | `{name}` / `{name...}` wildcards + `r.PathValue`, if you want them |
| Unmatched route       | `r.NotFoundHandler`                                       | a `"/"` registration                                    |
| Wrong method          | `r.MethodNotAllowedHandler`                               | ServeMux synthesises its own — **see §4**               |
| Middleware            | `r.Use(...)` — runs **after** matching                    | yours; the ordering is observable — see §4              |
| Unclean-path redirect | `mux.CleanPath` (default on)                              | yours, or `http.ServeMux`'s — they differ               |
| Everything else       | `net/http`, `afero`, `uuid`, `mergo`                       | unchanged                                               |

File storage, the naming scheme, auth token checking, range handling,
conditional requests, the size limit, logging format and the CLI flags are
**not** part of the migration. `afero`, `github.com/google/uuid` and
`dario.cat/mergo` all stay. What changes is the dispatch layer and everything
that depended on how the old one behaved.

### Retired module

This module must not appear anywhere in the delivered project — not in `go.mod`,
not in `go.sum`, not as an import, not vendored into the tree:

```
github.com/gorilla/mux
```

It is not available to build against. The offline module proxy holds **no `.zip`**
for it, so `go build` cannot resolve it and there is no network to reach past the
proxy. Its `.mod` **is** kept, deliberately: while the stale `require` is still in
`go.mod`, `go mod tidy` needs it to compute the module graph. Once your last
import is gone, `go mod tidy` drops the require by itself, offline.

Twenty-seven other third-party routers are banned by name (`$SRB_BANNED_ROUTERS`) —
chi, gin, echo, httprouter, fiber, fasthttp and the rest. **The target is the
standard library, not a different dependency.** None of them is in the proxy
either.

### Available modules

Everything already in `go.mod` stays available: `github.com/spf13/afero`,
`github.com/google/uuid`, `dario.cat/mergo` and `golang.org/x/text`. The proxy at
`$SRB_GOPROXY_ROOT` holds those and nothing else you will want. You should not
need to add a dependency; if you think you do, the answer is in `net/http`.

One detail about the proxy that is easier to read here than to debug: it carries
State A's **exact** dependency closure, at the versions `go.mod` and `go.sum`
pin — including the `// indirect` requirement on `golang.org/x/text`. If you drop
that line and let `go mod tidy` re-derive it, version selection can settle on a
lower version that the proxy has no `.zip` for, and the failure names `x/text`
rather than anything you changed.

---

## 2. What "identical behaviour" means here

Grading replays HTTP requests against your server and against State A, and
compares what came back: status, headers, and body bytes. It also builds the
project and runs the repository's own test suite.

Whether the router was actually retired is judged separately, by reading both
trees rather than by running them — so a tree that keeps gorilla/mux answers every
replayed request correctly and still fails the task. It also would not have
compiled for you: the module mirror in this environment serves no archive for it.

So the contract is the wire, plus the one code question named above: what does
the dispatching. There is no required file layout, no required function name, no
required *number* of registrations — four or nine both pass. Reorganise the
package however you like — split the router out, keep it in `Start`, introduce
helpers, delete helpers. What is not negotiable is that the responses do not
move, and that `ServeMux` is what routes.

That second one has a floor, and it is worth being plain about since §4 will
tempt you away from it. The floor is about one axis in particular: **`ServeMux`
has to be what decides the path** — which endpoint a request reached, `/upload`
against `/files` against neither. Deciding the method behind an
already-chosen endpoint is not that decision, however cleanly it is done.

Three shapes miss the floor, and the second and third are the ones worth naming,
because both can be built while believing the rule is satisfied:

- a catch-all registration with your own matcher behind it;
- a hand-written path test *in front of* `ServeMux` — a `switch` on
  `r.URL.Path`, or a chain of `strings.HasPrefix`, choosing between per-endpoint
  muxes that each register only `METHOD /`. `ServeMux` is doing real work there,
  and it is still never handed a path decision, because every pattern it holds is
  rooted at `/` and the endpoint was settled before it was consulted;
- registered patterns that do not name the endpoints being served. Reading your
  own registrations back is the fastest check: if none of them contains `/upload`
  or `/files`, `ServeMux` is not routing.

All three retire the import and leave the dispatch hand-written, which reads as
the old router with a new name — including when the wire output is perfect,
because the wire cannot see the difference. That is why the two axes are graded
separately, and why a tree can answer every replayed request correctly and score
zero.

Some matching must live behind a pattern, and that is expected rather than
tolerated: §4.1 is a rule `ServeMux` cannot express, so its residue has to go
somewhere, and a fallback that inspects the path is the right home for it. The
distinction is whether that fallback is taking the *residue* of a path decision
`ServeMux` made, or standing in for the decision itself.

Two consequences worth being explicit about:

- **Hardcoding a response table is not a migration.** A `switch` on the request
  path that returns recorded bytes reproduces the corpus and fails the task.
  Requests not in any recorded set are fair game for grading, and a rewrite that
  only knows the ones it was shown answers them wrong. Note that this is the
  narrower of the two problems with a path `switch`, not the only one: dispatching
  from it to real handlers avoids *this* bullet and still misses the floor above,
  for the separate reason given there.
- **Passing tests is not the goal, and neither is a green build.** A route that
  is never reachable, a flag that is silently ignored, a middleware that no
  longer runs — these all compile.

---

## 3. The oracle: how to find out what State A does

You cannot build State A yourself — gorilla/mux is not in the proxy. Instead the
image carries a **pinned State A binary**, built before the router was retired,
and a script to drive it:

```
oracle-serve start [--port N] [--] [...any server flag]
oracle-serve run   [--port N] [--] [...]    # as start, and print the pid
oracle-serve stop  [--port N]
oracle-serve docroot [--port N]             # this launch's document root
oracle-serve log     [--port N]
```

Neither `start` nor `run` blocks: both seed a fresh document root, launch the
server, wait until it answers a readiness probe, and return. Neither detaches
either — the server is a background child of the shell that launched it, so if
your shell's process group gets reaped between commands, launch it under
`setsid` to keep it alive for the next one.

Point any HTTP client at it and read the answer:

```
oracle-serve run -- -document_root "$(mktemp -d)" -enable_cors=true
curl -isS http://127.0.0.1:8899/filesabc
```

Launch it once per configuration — this server's observable surface moves with
its flags, so `-enable_auth`, `-enable_cors`, `-read_only_tokens`,
`-read_write_tokens`, `-max_upload_size` and `-file_naming_strategy` each change
what the same request returns. Every launch gets a fresh document root copied
from the frozen fixture tree at `$SRB_FIXTURE_ROOT`, so an upload in one
configuration cannot leak into the next.

The oracle answers **any** question you ask it, including questions no test in
this repository asks. That is the point of shipping it. What it will not tell you
is whether your migration passes — there is no self-check, and no list of graded
requests. Build the comparison you need; the reference is right there to compare
against.

Two practical notes. The server has no health endpoint: the readiness probe is
`OPTIONS /upload`, which returns 204 in every configuration (a probe on
`GET /files/...` would 401 under `-enable_auth` and report a healthy server as
dead). And fixture mtimes are pinned to `$SRB_FIXTURE_MTIME`, because
`http.ServeContent` turns modtime into `Last-Modified` and answers
`If-Modified-Since` against it.

---

## 4. The behavioural contract

Everything below is observable through the oracle. It is written down because
these are the places where `ServeMux` and gorilla genuinely disagree, and where a
reasonable-looking rewrite silently changes an answer. Knowing the rule is the
cheap part; reproducing it is not.

### 4.1 `PathPrefix` matches a string, not a segment

`r.PathPrefix("/files")` matches any path **beginning with the characters**
`/files`. ServeMux has no pattern that does this: `"/files/"` is a subtree match
on segment boundaries, and `"/files"` is exact. The gap is directly observable:

| Request                  | State A                                                   |
| ------------------------ | --------------------------------------------------------- |
| `GET /files/a.txt`       | 200, the file                                             |
| `GET /files/`            | 404 `{"ok":false,"error":"file not found"}`                |
| `GET /files`             | 404 `{"ok":false,"error":"file not found"}`                |
| `GET /filesabc`          | 404 `{"ok":false,"error":"file not found"}`                |
| `GET /filesabc/x/y`      | 404 `{"ok":false,"error":"file not found"}`                |
| `GET /file`              | 404 `{"ok":false,"error":"not found"}`                     |
| `PUT /filesabc`          | 405 `{"ok":false,"error":"PUT is accepted on /files/:name"}`, **no** `Allow` |

Read rows two through six together. `/filesabc` **routes** — it reaches the file
handler, which re-derives the filename from the path and finds none, so the reply
is the handler's `file not found`. `/file` does not route at all, so it gets the
fallback's `not found`. Two different 404 bodies, and which one you get says
whether the request was dispatched.

The `PUT /filesabc` row is a third distinct answer: a 405 with **no** `Allow`
header, because it comes from the write handler rather than from the router's
method-not-allowed path. Compare `DELETE /files/a.txt`, which is the router's:
405, `Allow: GET, PUT`, body `{"ok":false,"error":"DELETE is not allowed on /files"}`.

Getting `/filesabc` right is the design work this task is really about. There are
several honest ways to do it. Any of them is fine; guessing is not.

### 4.2 405 bodies belong to the application, not the router

State A installs its own `MethodNotAllowedHandler`, which derives the endpoint
name from the request path (`/upload`, or anything prefixed `/files`) and builds
both `Allow` and a JSON body from it:

- `GET /upload` → 405, `Allow: POST`, `{"ok":false,"error":"GET is not allowed on /upload"}`
- `DELETE /files/a.txt` → 405, `Allow: GET, PUT`, `{"ok":false,"error":"DELETE is not allowed on /files"}`

`ServeMux` synthesises a 405 of its own when a path matches but the method does
not — with its own `Allow` header and its own plain-text body. It is a
convenience, and here it is a regression: it replaces the application's response
with the standard library's. That the substitute is *also* a 405 with *also* an
`Allow` header is what makes this easy to miss.

### 4.3 `/upload` is exact

- `POST /upload` with a multipart body → 201
- `POST /upload/` → 404 `{"ok":false,"error":"not found"}` — the fallback, not a redirect

A `"/upload/"` subtree pattern, or anything that redirects `/upload/` to
`/upload`, changes this.

### 4.4 Unclean paths are redirected, and the body is empty

gorilla's `CleanPath` is on by default. It answers a **bodiless 301** with the
cleaned target in `Location`, and it **keeps the query string**:

| Request                          | State A                                          |
| -------------------------------- | ------------------------------------------------ |
| `GET /files/../a.txt`            | 301 → `/a.txt`, empty body                       |
| `GET /files/./a.txt`             | 301 → `/files/a.txt`, empty body                 |
| `GET /files//a.txt`              | 301 → `/files/a.txt`, empty body                 |
| `GET /files/sub/../a.txt`        | 301 → `/files/a.txt`, empty body                 |
| `GET /files/%2e%2e/a.txt`        | 301 → `/a.txt`, empty body                       |
| `GET /files/../a.txt?x=1&y=2`    | 301 → `/a.txt?x=1&y=2`, empty body               |

`http.ServeMux` also redirects unclean paths — but with `http.Redirect`, which
writes an HTML body (`<a href="...">Moved Permanently</a>.` plus a newline) for
GET requests. Same status, same `Location`, different bytes on the wire.

Note the redirect targets: cleaning happens before routing, so `/files/../a.txt`
becomes `/a.txt` and leaves the `/files` space entirely. Do not "fix" this.

### 4.5 `%2F` is a separator

gorilla matches on `r.URL.Path`, which is **decoded**. So `%2F` behaves as a path
separator on both read and write:

- `GET /files/sub%2Fb.txt` → 200, the contents of `sub/b.txt`
- `PUT /files/sub%2Fput-enc.txt` → 201 `{"ok":true,"path":"/files/sub/put-enc.txt"}`

`http.ServeMux` matches on the **escaped** path and treats `%2F` as a literal
character inside one segment. Whichever way you build the route, the requests
above must still resolve into the subdirectory.

### 4.6 Middleware runs after matching, and OPTIONS is open

`r.Use(...)` in gorilla wraps the **matched** handler, so middleware does not run
for requests that fail to route. Both middlewares are affected: access logging,
and (under `-enable_auth`) authentication. Consequences to preserve:

- `GET /nope` under `-enable_auth`, no token → **404, not 401**. It matched
  nothing, so the auth middleware never ran.
- `GET /filesabc` under `-enable_auth`, no token → **401, not 404**. It *did*
  match (§4.1), so auth ran and refused before the handler could report the
  missing file. This pair is the sharpest test of the ordering: the same 404 body
  in one configuration and a 401 in another, from one routing decision.
- `DELETE /files/a.txt` under `-enable_auth`, no token → **405, not 401**. Method
  rejection also precedes auth.
- `GET /files/a.txt` under `-enable_auth`, no token → 401
  `{"ok":false,"error":"unauthorized"}`.
- `OPTIONS /upload` → 204 even unauthenticated, in every configuration. This is
  the property the readiness probe depends on.
- Cleaning precedes all of it: `GET /files/../a.txt?token=ro1` still 301s to
  `/a.txt?token=ro1`. An authenticate-first port turns this into a 401.

A middleware installed as an outer wrapper around the whole mux changes most of
those.

### 4.7 OPTIONS does not touch the filesystem

`OPTIONS /files/nosuch.txt` → **204**, with `Access-Control-Allow-Methods: GET,
PUT, HEAD`. It does not stat the file and does not 404.

`Access-Control-Allow-Origin` is the only header gated on `-enable_cors`;
`Access-Control-Allow-Methods` is written unconditionally, and its value depends
on the path — `POST` for `/upload`, `GET, PUT, HEAD` for anything prefixed
`/files` (that same string prefix again), empty otherwise. So with
`-enable_cors=false`, `OPTIONS /upload` still answers 204 with
`Access-Control-Allow-Methods: POST` and no `Allow-Origin`. A single flag-gated
CORS middleware that writes both headers together breaks this.

And CORS is per-branch, not per-response-code. With `-enable_cors=true`:

- `GET /files/sub` → 404 `{"ok":false,"error":"sub is a directory"}` **with**
  `Access-Control-Allow-Origin: *`
- `GET /filesabc` → 404 `{"ok":false,"error":"file not found"}` **without** it

Two 404s from the same handler, one carrying CORS and one not. Pairing them is
what pins the header to the correct branch — reproducing the §4.1 routing while
attaching CORS to the whole 404 path gets one of these two wrong.

### 4.8 Everything else

Range requests, `If-Modified-Since` / `If-None-Match`, `Last-Modified`, ETags,
`Content-Type` sniffing, the size limit's 413, the directory 404
(`{"ok":false,"error":"sub is a directory"}`), the two file-naming strategies,
the 201 bodies and the access-log format are all unchanged by the migration —
which means they must all still work afterwards. They are the part of the surface
most likely to break by accident, because nothing about them looks
router-related.

---

## 5. Repository scope

```
app.go              CLI flags, config merge, server lifecycle
app_test.go         its tests
pkg/server.go       the HTTP layer -- router, middleware, handlers  (the migration)
pkg/file_naming.go  naming strategies
pkg/server_test.go       unit tests
pkg/server_it_test.go    integration tests -- exercises the routes directly
go.mod / go.sum     the dependency set
README.md, Dockerfile, config.json, .github/, .golangci.yml
```

The router import is at `pkg/server.go:20`. That is the only gorilla import in
the module, and `pkg/server.go` is the only file that has to change for the
server to work — but it is not the only file that has to change for the task to
be done:

- **`go.mod` / `go.sum`** must no longer require or record gorilla/mux. `go mod
  tidy` does this once the last import is gone.
- **The tests are part of the repository, and they are yours to carry.**
  `pkg/server_it_test.go` (1201 lines) and `pkg/server_test.go` (490 lines)
  exercise the routes; where they construct requests through gorilla types they
  will not compile, and where they assert on behaviour they should keep asserting
  on it. Port them. Deleting a test to make the suite green is the one repair
  that is worse than leaving it broken — the coverage those files represent is
  part of what is being migrated, and the delivered suite is expected to still
  cover the surface described in §4.
- **Docs, CI config and the Dockerfile** should still be coherent afterwards.
  None of them names the router today, so there may be nothing to do here —
  check rather than assume, and do not leave a stale reference behind if your
  rewrite introduces one.

Keep the CLI surface exactly as it is: `-config`, `-document_root`, `-addr`,
`-enable_cors`, `-max_upload_size`, `-file_naming_strategy`, `-shutdown_timeout`,
`-enable_auth`, `-read_only_tokens`, `-read_write_tokens`, `-read_timeout`,
`-write_timeout`. Same names, same defaults, same config-file merge order.

---

## 6. Definition of done

1. The project builds offline, and `go test ./...` passes — with the
   repository's own tests ported rather than removed.
2. `go.mod` is what `go mod tidy` produces: no stale `require`, and no gorilla/mux
   in `go.mod`, in `go.sum`, in an import, or vendored into the tree. A note in the
   README or CHANGELOG saying what was replaced is fine — the question is whether
   the code is there, not whether the name is mentioned.
3. No third-party router has been added in its place, and none has been rebuilt by
   hand under new names. A route table walked in registration order, with a
   per-request variables map and overridable not-found and method-not-allowed
   handlers, is the retired router with the import removed.
4. The server's responses match State A's — including every row in §4, and
   including requests §4 does not mention.

Run `go vet ./...` too. It is worth reading on a rewritten dispatch layer,
though a vet finding is not itself a failure.

Work in `/workspace/repo` and leave your result there. There is nothing to
submit and nothing to register; the tree as you leave it is the deliverable. It
is a git checkout — commit if you find it useful, or don't.

One last note on how to spend your time. The migration itself is a couple of
hours of careful work. The trap in §4.1 is worth more than that, because it is
the one place where the obvious rewrite is confidently, silently wrong. Ask the
oracle before you assume — about `/filesabc`, about redirect bodies, about what
`ServeMux` adds that gorilla never did.

