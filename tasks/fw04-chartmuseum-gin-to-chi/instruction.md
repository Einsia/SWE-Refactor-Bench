# Remove the Gin ecosystem from ChartMuseum

`/workspace/repo` is [ChartMuseum](https://github.com/helm/chartmuseum) v0.15.0,
frozen at commit `460d8ec9`. It is a Helm chart repository server, and its whole
HTTP layer is built on **Gin 1.8.1** plus two Gin-coupled satellites:
`gin-contrib/size`, which enforces the upload limit, and
`zsais/go-gin-prometheus`, which is the entire `/metrics` surface.

Your job is to take Gin out — the router, the middleware chain, the context
object, the size limiter and the Prometheus instrumentation — and rebuild that
layer on **chi v5 and/or `net/http`**, across the whole repository.

This is a behaviour-preserving migration. **Do not add features, do not remove
routes or CLI flags, and do not change the public HTTP contract.** A response
whose status, headers or bytes differ from State A's is a regression, including
where State A's answer is surprising.

---

## 1. The two states

| | State A (now) | State B (deliver) |
| --- | --- | --- |
| HTTP framework | Gin 1.8.1 | chi v5 and/or `net/http` |
| Dispatch | `engine.NoRoute(rootHandler)` + the project's own 207-line `match()` | your router, same matching semantics |
| Handler signature | `func(c *gin.Context)` | your own, `http.Handler`-shaped |
| Response bodies | `c.JSON`, `c.Data`, `gin.H{...}` | `net/http` writes, identical bytes |
| Route params | `gin.Param` / `gin.Params` | your own carrier |
| Framework files | 13 of 35 `.go` files import Gin | 0 |
| Panic recovery | `gin.Recovery()` | your own middleware |
| Request logging | Gin-shaped middleware in `router/middleware.go` | your own, same log fields |
| Request id | Gin middleware writing `X-Request-Id` into the context | your own |
| Upload limit | `gin-contrib/size` `RequestSizeLimiter` | your own limiter |
| Metrics | `zsais/go-gin-prometheus` | your own `promhttp` wiring |
| Trailing-slash redirect | `engine.RedirectTrailingSlash = false` | **off**, whatever your router defaults to |
| Test contexts | `gin.CreateTestContext` in `_test.go` | your own harness |

Everything else stays: the storage backends, `pkg/repo`, the cache layer, the
config surface, the CLI, the Makefile targets, the release scripts. This is not a
Go-version upgrade, not a dependency refresh, and not a redesign of the storage
or index code.

### The interesting part is not the router

ChartMuseum registers **zero routes** with Gin. `pkg/chartmuseum/router/router.go`
installs a single catch-all — `engine.NoRoute(router.rootHandler)` — and every
request is dispatched inside `pkg/chartmuseum/router/match.go`, 207 lines of
hand-written matching. The file says why in its own comment: multitenancy depth
is dynamic, a repo name may contain slashes, and Gin's radix tree cannot express
that (it cites `gin-gonic/gin#388`).

So Gin here is a **middleware chain and a context object**, not a router.
`match()` itself ports across nearly unchanged. What does not port for free is
everything Gin was quietly deciding on your behalf: the serialisation of
`c.JSON`, the shape of a `gin.H` error body, the status and body of a rejected
oversized upload, the exposition and label cardinality of the metrics endpoint,
and the trailing-slash behaviour that State A explicitly turned off. Those are
all observable, and all graded.

### Retired dependencies

These three modules are retired. Nothing in the delivered repository may declare,
import, vendor, re-export or link them:

```
github.com/gin-gonic/gin
github.com/gin-contrib/size
github.com/zsais/go-gin-prometheus
```

The list is also on disk, at `$SRB_RETIRED_MODULES`. **Here, the offline module
proxy has no archive for them**, so `go build` fails and names them, and there is
no registry to reach past. That is the condition you work under rather than the
way the retirement is judged — the grader's mirror does stock these archives, and
what judges the retirement is a read of the two trees that zeroes the run when it
goes against you (§2, and the gate described at the end of this file). Treat the
failing build as the fastest signal available to you locally, not as the exam.
Try it:

```bash
cd /workspace/repo && go build ./...
# github.com/gin-gonic/gin@v1.8.1: reading
#   file:///opt/goproxy/github.com/gin-gonic/gin/@v/v1.8.1.zip: no such file or directory
```

Their `.mod` files are deliberately retained, so `go mod tidy` still works while
a stale `require` is present. That is a convenience, not a loophole: the archive
is what a build needs.

There is no adapter and no shim, and writing one is the shortcut this task exists
to rule out. Note what is technically true and still not a migration: `*gin.Engine`
satisfies `http.Handler`, and a `*gin.Context` can be built from a
`(http.ResponseWriter, *http.Request)` pair. So a new router with the old engine
mounted behind one catch-all route compiles, serves correctly, and has migrated
nothing — as does a `replace github.com/gin-gonic/gin => ./internal/gin`, or a
copy of the context type and handler chain into an internal package under new
names. The `gin.Context` signature has to go, and so does the machinery behind
it.

### Available dependencies

The environment is **offline**. The only module source is the pinned file proxy
at `$GOPROXY` (`file:///opt/goproxy`). The grader's mirror lives at the same path
and is *not* the same mirror: it is a superset, and it stocks the retired archives
this one withholds, because the pre-migration tree is the oracle every expectation
was recorded from and a mirror that cannot compile Gin could never record them.
Nothing you can resolve here will fail to resolve there. What follows from that is
in §2: the retirement is not enforced by the grading build.

This mirror stocks the destination generously, because a Gin-free port need not be
a *chi* port:

```
github.com/go-chi/chi/v5    v5.0.12, v5.1.0, v5.3.1
github.com/go-chi/cors      v1.2.2
github.com/gorilla/mux      v1.8.0, v1.8.1
github.com/gorilla/handlers v1.5.1, v1.5.2
github.com/julienschmidt/httprouter v1.3.0
github.com/google/uuid      v1.3.0, v1.6.0
```

plus every module State A already resolves, at the versions `go.sum` pins — every
one except the three being retired, whose archives this mirror is the one place
that withholds. Anything not in the mirror cannot be resolved and must not be
declared — `go mod download <module>@<version>` tells you in one command.

**One real decision with a real consequence.** chi v5.3.1 declares `go 1.23`;
v5.0.12 and v5.1.0 declare `go 1.14`. State A's `go.mod` says `go 1.17`. Take the
newest chi and you must raise the `go` directive; keep the directive and you must
pin an older chi. Both paths are stocked and both are acceptable. The toolchain
in the image is Go 1.24 with `GOTOOLCHAIN=local`, so nothing will be downloaded
on your behalf either way.

`net/http` alone is also a complete answer. `match()` does the routing already.

---

## 2. What "migrated" has to mean

Evaluation happens in **separate, offline containers**. Nothing you build in place
is trusted: the repository is re-materialised, `vendor/`, `bin/`, `_dist/`,
`testbin/` and the other build and dependency directories are deleted, the module
graph is resolved from the grader's own mirror, and the binary is built from your
source. A compiled binary you leave somewhere the collector does not name is not
deleted, and it is not read as source either — but it is dead weight in the
artifact, so build outside the tree.

So the deliverable is judged as a repository, not as a diff. Six things have to be
true of it:

1. **It resolves and builds offline.** `go mod download` and `go build ./...`
   succeed, and so does `go build ./cmd/chartmuseum`. Against the grader's mirror,
   which stocks the retired archives — so do not mistake this for the check that
   the retirement happened. A tree that still imports Gin compiles there and goes
   on to be measured for behaviour like any other. Whether it needed an archive you
   were never given is recorded, but not charged for, and it is not what decides
   the point below.
2. **The retired stack is gone from the source**, not merely unreferenced from the
   happy path: no import, no `require`, no `replace` on either side, no `go.sum`
   line, no vendored copy, and no hand-written reimplementation of the retired
   context and handler chain under a new name. Prose is the exception —
   `*.md`, comments and CHANGELOG entries may name Gin freely. Describing what you
   removed and why is encouraged.
3. **Dispatch has left Gin.** Follow one request from the process entry point to
   the handler that answers it: Gin must be no part of deciding which handler runs
   or what the parameters are. The old engine mounted behind a catch-all, a helper
   that rebuilds a `*gin.Context` from a `(w, r)` pair so old handler bodies can be
   called unchanged, or Gin's router tree and middleware chain pasted into an
   internal package under new names — none of those is a migration. What is
   expected: `match()` carried across and retyped onto your stack. State A already
   registers no routes with Gin and dispatches inside `match()` from a single
   `NoRoute`, so one catch-all with your own matcher behind it is a correct answer,
   and so is `net/http` with no router library at all. The count of registered
   routes is not what is being judged. Handlers keeping a familiar shape behind a
   thin adapter of your own is a style choice.
4. **The repository publishes a working way to start itself, and that is the way
   it gets started.** The graded stages build `./cmd/chartmuseum` and run the
   binary, and the release path is `make build`. One server, not two and a switch:
   no build tag, no environment variable and no error-path fallback that selects
   between an old implementation and a new one.
5. **The answers are computed.** The server will be asked for charts whose names
   were invented when the grader started, at upload sizes jittered by the same
   value, on paths generated rather than replayed. A map from paths to literal
   bodies, a table of status codes, a canned `index.yaml` or a metrics exposition
   written out as a string literal is not a port of ChartMuseum. Genuine constants
   are fine — the HTML templates, the error strings, the document shapes this
   project already ships.
6. **The server does not recognise the grader.** No branch on a harness
   environment variable or filesystem path, on whether a test binary is running,
   or on a request header used for anything other than its documented purpose.
   Reading configuration is not that: this server is configured almost entirely by
   environment variable, and every one of those variables is declared in
   `pkg/config`.

Points 2, 3, 5 and 6 are decided by **reading** the original tree against yours,
not by matching strings. There is no keyword list to satisfy and nothing to be
gained from renaming: a tree with no occurrence of the string `gin` is not
thereby a successful port, and a comment that mentions it is not thereby a failed
one. The question asked is whether the old layer genuinely left and yours
genuinely took over.

### Checking your own work

The toolchain in this image is the same one the grader uses, so you can measure
most of this directly:

```bash
go build ./...                                          # resolves offline
go build -o /tmp/cm ./cmd/chartmuseum
go version -m /tmp/cm | grep -i gin                     # what the linker actually put in
grep -rn 'gin-gonic\|gin-contrib/size\|go-gin-prometheus' --include='*.go' .
MOD_PROXY_URL="file://$SRB_GOPROXY_ROOT" make build     # the release path

# The repository's own suite. Seed testbin/helm first: setup-test-environment.sh
# guards its download with `if [ ! -f testbin/helm ]` and otherwise fetches helm
# from get.helm.sh, which this container cannot reach -- you would get a
# name-resolution error that looks like a broken repository but is only the
# missing seed.
mkdir -p testbin && cp "$(command -v helm)" testbin/helm
make setup-test-environment && go test -race -count=1 ./...
```

`go version -m` is worth a moment: it reads the build information Go embeds in
every binary it links, written by the linker rather than by you. If a retired
module is listed there, it is in the artifact, whatever the source looks like.

---

## 3. The reference oracle

The original binary is installed, immutable and read-only, at
`/opt/oracle/chartmuseum`. It is **State A**, and it will keep answering
correctly no matter how far you rewrite `/workspace/repo`.

ChartMuseum's behaviour depends on its flags, so the oracle is not one running
server: it is a binary you launch per configuration. `oracle-serve` does that,
with a fresh storage directory seeded from the frozen chart bytes at
`/opt/testdata` on every launch:

```bash
oracle-serve start --port 8899 --depth 1 --enable-metrics \
                   --seed 'myrepo=charts/mychart-0.1.0.tgz'
curl -sD- 'http://127.0.0.1:8899/myrepo/index.yaml'
curl -sD- 'http://127.0.0.1:8899/api/myrepo/charts'
oracle-serve stop --port 8899
```

Use it as ground truth: issue the same request to the oracle and to your server
and diff the two responses. Every claim in §4 was measured against it rather than
read out of the source, and where §4 is silent the oracle is still the answer —
launch it with the flags you care about and look.

The oracle runs on Gin — the stack you are removing. It is a reference, not a
target. **Do not copy it into your deliverable, and do not proxy to it.** It is
not present in the grading container.

The charts under `/opt/testdata` are the *exact bytes* the grader serves,
packaged and signed once at image build time. Do not repackage them: `helm
package` output depends on mtime and gzip framing, so a repackaged chart has a
different digest and every index comparison would fail for a reason that has
nothing to do with your port.

`git log` holds a single baseline commit, so `git diff` and `git checkout --
<path>` work against State A.

---

## 4. Behavioural contract

Your server is compared with State A over real HTTP, across the flag
configurations ChartMuseum's own surface produces, on status, the exact set of
header names, header values, body bytes, JSON structure and JSON serialisation
style. A configuration is a flag set plus a seeded storage directory plus an
*ordered* request sequence — a push changes what the next `index.yaml` says, so
order is part of the contract.

### Must be preserved exactly

**Routing.** Every shape `match()` accepts, and every shape it rejects. The
context path peeled from the front (`--context-path`), the tenant prefix of
configurable depth (`--depth` 0–3, negative, and `--depth-dynamic`), a repo name
containing slashes, then the remainder matched against the route table. Doubled
separators, trailing slashes, over-long suffixes and unknown prefixes are all
graded, and paths are generated at grading time rather than replayed from a list.

**The trailing-slash redirect stays off.** `router.go` sets
`engine.RedirectTrailingSlash = false` with a comment saying it was making
`/health` redirect to `/health/`. chi and `gorilla/mux` both redirect by default,
so this is something your port has to switch back off. The one redirect State A
does emit is the static-file handler under `--web-template-path` sending `static`
to `static/`, which is a different mechanism; every other trailing-slash variant
is a 404.

**HEAD is declared on exactly two route shapes, and is a 404 everywhere else.**
`routes.go` gives HEAD to `/api/{repo}/charts/{name}` and
`/api/{repo}/charts/{name}/{version}` and to nothing else, so State A answers
**404 to HEAD on every path where GET returns 200** — `/index.yaml`, `/health`,
`/`, a chart download, all of it. **OPTIONS is 404 everywhere**, and **no `Allow`
header is sent anywhere.** A router that synthesises HEAD from GET, or answers
OPTIONS with an `Allow` list, is a large and immediately visible regression; both
are defaults in the replacement stack. `Allow` is in the always-compared header
set precisely so that its absence is graded rather than assumed.

**JSON serialisation.** `c.JSON` emits **compact** JSON — no indentation, no
trailing newline — with `Content-Type: application/json; charset=utf-8`. The
universal error body is `gin.H{"error": "..."}`, serialised as
`{"error":"..."}`. `encoding/json`'s `MarshalIndent`, or a `json.NewEncoder` that
appends `\n`, differs from that on every response that carries a body.

**Content types.** `application/x-yaml` for `index.yaml`, `application/x-tar` for
a chart archive, `application/pgp-signature` for a provenance file, `text/html`
for the welcome page, `application/yaml` in the places State A spells it
differently, and `text/plain; version=0.0.4; charset=utf-8` for `/metrics`. These
are not interchangeable and they are compared as strings.

**Headers.** `content-type`, `content-length`, `www-authenticate`,
`access-control-allow-origin`, `location` and `allow` are compared on every case,
present or absent. Beyond that, the full header *set* is compared for equality
both ways: a header State A did not send is a failure even though it looks
additive. Specifically:

* `X-Request-Id` on every response — echoed if the request carried one, else a
  fresh UUID v4. The value is masked, but a value that is not UUID-shaped keeps
  its literal text and fails the comparison, and a port that stopped sending the
  header fails the header-set comparison.
* `WWW-Authenticate` on a 401, when the authorizer supplies it.
* `Access-Control-Allow-Origin` **only** on API routes, where "API route" means
  the path starts with `/api/` **and** does not end in `.yaml`, `.tgz` or `.prov`.
  Emitting it everywhere is a common regression.
* `Content-Length` is compared, and separately every response is checked for
  agreement between the length it declared and the bytes it sent.

**Statuses.** Reproduce the status State A chose, not the one it should have
chosen. `507 Insufficient Storage` is `--max-storage-objects` refusing a push;
409 is a duplicate version; an invalid provenance file and an unsupported file
extension are reported as **500**, not 400 — handled errors that State A answers
with a 500 and a JSON body.

**The upload limit.** `gin-contrib/size` is retired and has no drop-in
replacement, so this comes back as code you write. The property to reproduce: an
over-limit upload is refused with **413, `Content-Type: text/plain;
charset=utf-8`, and a body of exactly `request too large`** — the only non-JSON
error this API produces. It is graded at sizes drawn at grading time, so "which
bodies are too big" cannot be a fixed rule, and a refused upload must leave
nothing in storage.

**`/metrics`.** Absent — 404 — unless `--enable-metrics`, and 404 for HEAD and
POST even then. With it: the Go runtime and process collectors, plus six of
ChartMuseum's own — `chartmuseum_requests_total`,
`chartmuseum_request_duration_seconds`, `chartmuseum_request_size_bytes`,
`chartmuseum_response_size_bytes`, `chartmuseum_charts_served_total` and
`chartmuseum_chart_versions_served_total`. The last two are gauges, and they are
checked as exact arithmetic over storage with charts pushed during the run, so
they must be computed rather than reported.

The label cardinality mapping matters, and it is the single most likely thing in
the tree to end up almost right. `router.go` hands `go-gin-prometheus` a
`ReqCntURLLabelMappingFn` — `mapURLWithParamsBackToRouteTemplate` — which rewrites
each route parameter *value* back to `:key` before the path becomes the `url`
label, so a request for `/myrepo` is recorded as `url="/:repo"` and a thousand
repositories do not mint a thousand time series. The retired library supplied the
matched route template for free; nothing in the replacement stack does, so
recovering it is real work rather than a hack, and a port that leaves raw paths in
that label is visibly wrong.

There is a second label on those series whose value is an internal Go symbol
derived from the handler at run time. It is on the wire in State A and it is part
of the graded body. It is not stated here because you can read it off the oracle
in one command:

```bash
oracle-serve start --port 8899 --enable-metrics
curl -s http://127.0.0.1:8899/metrics | grep chartmuseum_requests_total
```

Reproducing it means keeping the shape that produces it, not hard-coding the
string.

**Logging.** The `"Request served"` line with its `path`, `comment`, `clientIP`,
`method` and `statusCode` fields, `--log-json` vs. the console format, and
`--log-latency-integer` switching `latency` between an int64 and a
`time.Duration`. `--log-health` controls whether `/health` is logged at all. Log
lines are graded on the CLI side, by needles rather than byte equality.

**The CLI.** `--help`, `-h`, `--version`, `-v`, no arguments, `--gen-index`,
`--config` including a malformed file, a missing file, a bad extension and no
extension, every storage backend missing its required flag, an unknown flag, an
unknown storage backend, an unknown cache, `--storage=LOCAL`
case-insensitivity, the deprecation warnings, and the cases that start a server
and check it answers. Exit codes, the flag table, diagnostics located on the
stream that produced them, the version line and the generated index are all
compared. The flag table is compared as a *set of flags*, so reflowing help text
is free and dropping a flag is not. `--version` must print exactly

```
ChartMuseum version 0.15.0 (build <rev>)
```

with the revision masked but the version arriving only through `-ldflags`: the
grader builds with `-X main.Version=0.15.0 -X main.Revision=<rev>`, so those two
variables must stay settable that way. It builds from `./cmd/chartmuseum` and
without a `.git` directory, so a version derived from `git rev-parse` at build
time will not work.

### Bug-for-bug

State A's actual behaviour is the contract, including where that behaviour is
surprising. Three you will meet early:

* **HEAD is 404 where GET is 200.** Not an oversight; it is what the route table
  declares.
* **`GET /info` under `--depth` ≥ 1 serves the HTML welcome page**, because
  `info` is peeled off as the repository name. A port that returned a JSON error
  instead has changed the answer.
* **`--web-template-path` pointing at an empty or nonexistent directory** does not
  fail at startup; it changes what `/` serves. Both are graded.

When your reading of the source and the oracle's behaviour disagree, the oracle
is the contract.

### Explicitly *not* part of the contract

* `Date`, `Connection`, `Keep-Alive`, `Transfer-Encoding`.
* Header name casing, and the order of a set-valued header
  (`Vary`, `Allow`, `Cache-Control`, the CORS lists).
* The `X-Request-Id` value, as long as it is a UUID (or the echoed request value).
* Bound addresses and absolute filesystem paths in bodies and logs — the port,
  the temp directory and the checkout path are normalised on both sides.
* Prometheus timestamps and the `host` label's port.
* Any header whose value proved unstable across repeated runs of State A itself.
  Such a field is recorded as volatile and is not graded in either direction, so a
  timestamp never fails a submission.

---

## 5. Repository-wide scope

The migration is not finished when the server boots. 35 Go files, 7710 lines;
13 of them touch Gin. The whole repository must land in State B, coherently.

* **`pkg/chartmuseum/router/`** — the centre of the work.
  * `router.go` builds the `gin.Engine`, installs the middleware chain and
    `NoRoute(rootHandler)`, sets `RedirectTrailingSlash = false`, and wires
    `limits.RequestSizeLimiter` and `ginprometheus`. All of it goes.
  * `match.go` — 207 lines of routing that is *yours*, not Gin's. It ports across
    almost unchanged; only `gin.Param`/`gin.Params` in its signature and return
    type need a carrier of your own. Read its comment about `gin#388` before you
    decide to throw it away for `chi.URLParam`: repo names contain slashes.
  * `middleware.go` — request id, logging, CORS-on-API-routes-only, auth. These
    are `gin.HandlerFunc`s reading and writing `*gin.Context`; they become
    `func(http.Handler) http.Handler` or whatever shape you choose, with the same
    observable effects. Two details in here are easy to lose: `setupContext`
    stores `requestcount` and `requestid` on the context and the logger reads
    them back through `Debugc`/`Infoc`, so the context values need somewhere to
    live; and the `"Request served"` line's `comment` field is
    `c.Errors.ByType(gin.ErrorTypePrivate).String()` — Gin's own per-request error
    list, which nothing in this repository ever appends to, so it is always the
    empty string. Reproduce the field, not the machinery.
* **`pkg/chartmuseum/server/multitenant/`** — `api.go`, `handlers.go`,
  `server.go`, `cache.go`. This is where `gin.Context` appears most and where
  every `c.JSON` / `c.Data` / `gin.H` lives. `routes.go` is the route table —
  `{Method, Path, Handler, Action}` per entry, where `Handler` is typed
  `gin.HandlerFunc` and `Action` feeds the authorizer; its shapes and methods
  must survive exactly, including the two HEAD entries and nothing more.
  `index.go`, `storage.go` and `artifacthub.go` are Gin-free and should stay that
  way.
* **`pkg/chartmuseum/logger/logger.go`** — imports Gin only for its context type,
  but `pkg/config` imports the logger, so `go build ./pkg/config/...` fails until
  this file is clean. It is a good first move.
* **`pkg/chartmuseum/server.go`**, **`pkg/config/`**, **`pkg/cache/`**,
  **`pkg/repo/`**, **`cmd/chartmuseum/main.go`** — the flag surface, the server
  lifecycle and the domain logic. `main.go` holds `Version` and `Revision`, which
  `-ldflags` sets; keep both, keep the exact `--version` line, keep every flag.
  Note that the flags are negative where you might expect positive
  (`--disable-api`, `--disable-delete`), and that every one of them is declared
  with an `EnvVar` tag rather than read through `os.Getenv` — keep that property.
* **The Go test suite** — 8 packages have tests, and 5 `_test.go` files import Gin
  (`logger_test.go`, `match_test.go`, `router_test.go`, `handlers_test.go`,
  `server_test.go`), between them 27 uses of `gin.CreateTestContext`. They will not
  compile once Gin is gone. Port them: a ported handler that still imports Gin is a
  port that did not happen, and a ported *test* that still imports Gin is a test
  that was left behind. You may rename, split or restructure freely — names are not
  graded, a rename is not a regression — but the packages that had tests must still
  have tests, the tree must still run under `-race`, and the number of distinct
  tests may not fall. Keep `make setup-test-environment` working; it is what
  stages `testdata/`.
* **`Makefile`** — `TARGETS` must still name all 11 platforms
  (`darwin/amd64`, `darwin/arm64`, `linux/amd64`, `linux/386`, `linux/arm`,
  `linux/arm64`, `linux/mips64le`, `linux/ppc64le`, `linux/s390x`,
  `windows/amd64`, `linux/loong64`), `make build` must still produce its six
  binaries, and `make get-version` must still print `0.15.0`. Keep the name and
  the version: this is a migration, not a release. Each platform is compiled
  separately when you are graded, and a port can pass on `linux/amd64` and fail on
  `windows/amd64` over path separators or on a 32-bit target over integer width —
  the retired framework abstracted none of that away. Note that every build target
  exports `GOPROXY=$(MOD_PROXY_URL)`, so that variable has to keep meaning what it
  means.
* **`scripts/`** — `test.sh`, `setup-test-environment.sh`, `acceptance.sh`,
  `release-artifacts.sh`, `sbom.sh`, `get-chartmuseum`. If a script names the old
  stack, it is out of date.
* **`.github/workflows/build.yml`** and **`build-pr.yml`** — the automation must
  describe the new stack. It is not executed by the grader, but a repository whose
  CI still installs Gin tooling has not landed in State B.
* **`Dockerfile`**, **`README.md`** — the shipped image and the documentation.
  Prose may name Gin freely; describing what you removed is not a violation of
  anything.
* **`acceptance_tests/`** (Robot Framework) and **`loadtesting/`** (locust) drive
  the server over HTTP and do not import Go. They are not graded, and they should
  not need to change — if they do, that is a signal your HTTP contract moved.

---

## 6. How the result is judged

Three things happen to the repository you leave behind, in order.

First it is **read**. The original tree and yours are opened side by side and the
questions in §2 are answered by reading the code — whether the retired layer
genuinely left, whether dispatch genuinely left it too, whether the answers are
computed, whether anything in the tree is addressed to the grader rather than to a
user. This stage is a **gate, not a weight**: it does not add points, and failing
it ends the run at zero however much behaviour the submission reproduces.

Then its **behaviour** is measured, end to end, over real HTTP and through the
real CLI, against the contract in §4, from a binary the grader compiled from your
source. It is modular in its reporting — the storage lifecycle, the index
document, routing, multitenancy, authorisation, the metrics exposition, the upload
limit, the platform matrix and the repository's own test suite are separate
modules, so a port that is strong in nine areas and weak in one is *reported* that
way — but it is not modular in what it pays. Anything short of the contract in
full stops here with nothing from this stage, and the attack below is asked only
of a submission that met all of it.

Then it is **attacked**. Independent adversaries get both trees and both compiled
binaries, and each tries to construct a request the original answers one way and
yours answers another. Every one that fails to find a divergence is worth points;
the divergences that are found are not. Only differences inside the contract count
— the exclusions in §4 are excluded here too — and a claimed divergence has to
reproduce and has to pass against the original before it counts against you.

There is no list of strings to avoid, and no partial credit anywhere in this
ladder — not for a repository that did not migrate, and not for one that migrated
and left a check failing. Write the implementation you would ship.

---

## 7. Practical notes

* **There is no network.** Everything you need is in the image. `go`, `helm`
  3.8.0, `git`, `gpg`, `make`, `curl` are on `PATH`, including in a login shell.
* The module source is `GOPROXY=file:///opt/goproxy` with `GOSUMDB=off`,
  `GOFLAGS=-mod=mod`, `GOTOOLCHAIN=local` and an empty `GOPRIVATE`. Do not set
  `GOPRIVATE` — it forces direct VCS mode and silently bypasses the file proxy,
  which then looks like a network failure rather than a configuration mistake.
* Do not commit a `vendor/` directory. `make bootstrap` will create one, and
  `go build -mod=vendor` never consults `GOPROXY` — so a vendored tree bypasses
  the module mirror entirely, which is why the artifact collector drops it and why
  the grader deletes it before building.
* Your solution has to live in tracked source files. Everything the collector
  drops — `vendor/`, `bin/`, `_dist/`, `testbin/`, `.cover/`, the packaged charts
  under `testdata/charts/` — is gone before anything is graded. Build to a path
  outside the repository, as the snippet below does: a binary left in the tree
  survives collection and just inflates the artifact.
* Build and run your server the way the grader does:
  ```bash
  go build -o /tmp/cm --ldflags="-w -X main.Version=0.15.0 -X main.Revision=dev" \
      ./cmd/chartmuseum
  /tmp/cm --storage=local --storage-local-rootdir=/tmp/store --port=8080 --depth=1
  ```
* The frozen charts at `/opt/testdata` are what the grader serves. Copy them into
  a storage root to seed a server; never repackage them (§3).
* `/opt/webtemplate/template` and `/opt/webtemplate/empty` are the two
  `--web-template-path` roots used when your server is exercised.
* Leave your work in `/workspace/repo`. There is no patch to produce and nothing
  to submit: the directory is collected as-is when you finish.
