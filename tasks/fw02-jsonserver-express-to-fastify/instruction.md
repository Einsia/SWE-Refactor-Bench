# Migrate json-server from Express to Fastify

You are working in `/workspace/repo`, a frozen checkout of **json-server 0.17.4** — the
tool that turns a JSON file into a full REST API. It is an **Express 4**
application written in **CommonJS** and compiled to `lib/` by **Babel**, built out of
the classic connect middleware stack: `body-parser`, `compression`, `cors`,
`method-override`, `morgan`, `errorhandler`, `express-urlrewrite` and
`express.static`, with an `express.Router()` for each kind of resource.

Your job is to migrate the **entire repository** to **Fastify 5**, retiring Express
and its middleware stack completely — while keeping the service's observable
behaviour identical.

This is a behaviour-preserving migration. **Do not add features, do not remove
routes or CLI options, and do not change the public HTTP contract.** Every
response the service produces today it must still produce, byte for byte, where
this document says so.

---

## 1. The two states

|                      | State A (now)                                     | State B (required)                       |
| -------------------- | ------------------------------------------------- | ---------------------------------------- |
| Web framework        | Express 4.18                                      | Fastify 5                                |
| Module system        | CommonJS (`require`), Babel-compiled to `lib/`    | your choice — native ESM preferred        |
| Body parsing         | `body-parser` (json + urlencoded)                 | Fastify content-type parsers             |
| Compression          | `compression`                                     | `@fastify/compress`                      |
| CORS                 | `cors`                                            | `@fastify/cors`                          |
| Static files         | `express.static` / `serve-static`                 | `@fastify/static`                        |
| ETag                 | Express's built-in weak ETag                      | your own (see §4)                        |
| Method override      | `method-override`                                 | your own hook                            |
| Logging              | `morgan`                                          | Fastify's logger, or your own            |
| Error rendering      | `errorhandler` + Express's `finalhandler`         | your own                                 |
| URL rewriting        | `express-urlrewrite`                              | your own hook                            |
| Routing              | `express.Router()` per resource kind              | Fastify plugins                          |
| Data layer           | `lowdb` 1 + `lodash-id`                           | unchanged                                |

The data layer is **not** part of the migration. `lowdb`, `lodash-id`, `lodash`,
`pluralize`, `nanoid`, `yargs` and `chalk` all stay. What changes is everything
between the socket and them.

### Retired dependencies

These 54 distributions must not appear anywhere in the delivered project — not as
a declared dependency, not in the lockfile, not as an import, not vendored into
the tree:

```
express            express-urlrewrite  path-to-regexp   array-flatten
merge-descriptors  utils-merge         methods          finalhandler
parseurl           body-parser         raw-body         bytes
content-type       compression         compressible     on-headers
connect-pause      cors                errorhandler     method-override
morgan             basic-auth          on-finished      ee-first
serve-static       send                destroy          encodeurl
fresh              range-parser        qs               etag
type-is            accepts             negotiator       media-typer
vary               cookie-signature    proxy-addr       forwarded
unpipe             supertest           superagent       cookiejar
formidable         dezalgo             hexoid           fast-safe-stringify
@fastify/express   @fastify/middie     middie           connect
router             express-promise-router
```

The full list, with the reasoning per group, is at
`$SRB_RETIRED_PACKAGES` (`/opt/srb/retired-packages.txt`). Four groups deserve a
note here:

* **The bridges** — `@fastify/express`, `@fastify/middie`, `middie`, `connect`,
  `router`, `express-promise-router` — are retired because they are the shortcut
  this task exists to rule out. Mounting the existing application inside Fastify
  through an adapter puts `fastify` in the manifest while the Express router, the
  middleware stack and the response plumbing all survive underneath. That is a
  translated interface, not a migration. There is no supported way to keep an
  `app.use(req, res, next)` middleware chain: the connect signature has to go.
* **`qs`, `etag`, `type-is`, `accepts`, `negotiator`, `vary`, `proxy-addr`,
  `send`, `fresh`, `range-parser`, `finalhandler`, `parseurl`, `path-to-regexp`**
  are Express's own plumbing, listed explicitly because re-assembling Express out
  of its parts is not a migration either. The delivered code must contain no
  Express API surface at all (no `express.Router()`, `express.static()`,
  `express.json()`, `res.jsonp()`, `res.sendStatus()`, `res.sendFile()`,
  `app.set('json spaces')`, `app.locals`).
* **`supertest`, `superagent`** and their transitive deps are retired because they
  are an Express-shaped test client. The project's own Jest suite is written
  against `supertest` (see §5). Fastify's `inject()` is the natural replacement.
* **`connect-pause`** implements `--delay`. It is retired, so that flag needs a
  hook of your own.

Note that Fastify's own dependency tree ships `@fastify/send`,
`@fastify/proxy-addr`, `@fastify/forwarded` and `@fastify/accept-negotiator` —
forks under a different name. Those are fine; the bare names above are not.

### Available dependencies

The environment is **offline**. The only installable packages are the pinned ones
in the local registry mirror, and these are the versions the target closure was
verified against:

```
fastify 5.10.0        @fastify/static 10.1.2    @fastify/cors 11.3.0
@fastify/compress 9.1.0   @fastify/formbody 8.0.2   @fastify/etag 6.2.0
lowdb 1.0.0           lodash 4.x                lodash-id 0.14.1
pluralize 8.0.0       nanoid 3.x                yargs 17.x
chalk 4.1.2           json-parse-helpfulerror   please-upgrade-node
server-destroy 1.0.1
jest 29.7.0           cross-env 7.0.3           server-ready 0.3.1
temp-write 4.0.0      os-tmpdir 2.0.0
@babel/cli @babel/core @babel/node @babel/preset-env
```

That is the target closure. The mirror *also* still carries State A's own
development toolchain at its inherited versions — `eslint` and its plugins,
`prettier`, `standard`, `husky`, `markdown-toc`, `mkdirp`, `npm-run-all`,
`rimraf`, `jest` 26 — because the retired list is what was removed, and those were
not. Keeping them, upgrading them or dropping them is your call; only the retired
list is enforced. Be aware that a State A dev dependency can still fail to install
if something in *its* tree is retired, which is why the versions listed above are
the ones the target closure was pinned and verified against.

**`@fastify/etag` is in the list, and it is a trap.** See §4: it does not produce
Express's ETag format. It is installable because the mirror serves the whole
plausible target closure, not because every member of that closure is a good idea.

Anything the mirror does not carry cannot be installed and must not be declared.
Run `srb-npm install <name>` to see for yourself: a retired or absent package
returns a hard 404. The shipped `package.json` is one of the things that does not
install — it declares `express` — so the first `srb-npm install` you run fails by
design, naming a retired package.

Babel is installable, so the choice of module system is yours. Native ESM with no
transpile step is the cleaner end state and what State B is described as above; a
CommonJS tree built by Babel into `lib/` also passes every gate. What is graded is
that `main` and `bin` point at something that exists after a clean install, that
`npm pack` ships it, and that it boots (§2, points 4 and 5). If you declare a
`build` script the grader runs it before scoring.

---

## 2. What "migrated" has to mean

Evaluation happens in **separate, offline containers**. Nothing you build in place
is trusted: your `node_modules` is deleted, the project is reinstalled from your
manifest against the grader's own registry mirror, and the result is exercised
from the outside.

So the deliverable is judged as a repository, not as a diff. Six things have to be
true of it:

1. **It installs.** `npm ci` — or `npm install`, if you ship no lockfile —
   succeeds with no network against the target mirror, and the resulting closure
   (manifest, lockfile and installed `node_modules`) contains none of the retired
   distributions.
2. **The old stack is gone from the source**, not merely unreferenced from the
   happy path: no imports, no vendored or renamed copies, no connect middleware
   surface, no paths named after it, and no mention left in `package.json`, the
   lockfile, the lint configuration or the CI workflows. Prose (`*.md`) and the
   HTML and assets under `public/` are the exception — describe the migration in
   documentation freely, including naming Express.
3. **Fastify is what actually serves.** When a request arrives it is Fastify that
   routes and answers it — through routes, plugins, hooks and content-type
   parsers. A Fastify shell whose request handling is still a chain of
   `(req, res, next)` functions dispatched in order is a translated interface, not
   a migration, and so is one catch-all Fastify route with your own matcher inside
   it. Using Fastify's `onRequest` / `preHandler` / `onSend` hooks is not a
   workaround — that is the lifecycle, and using it is the point.
4. **The repository publishes a working way to start itself, and that is the way
   it gets started.** Both the behavioural and the verification stage boot the
   service through an entry point the project itself publishes: `serve.sh` first,
   then the launcher `package.json`'s `bin` field resolves to. Keeping `serve.sh`
   working is the least surprising choice and it is the one State A ships. What
   does not count is a `node` command line someone would have to know to type: if
   nothing the repository publishes starts the service, it does not run.
5. **What you ship is what runs.** `npm pack` produces a tarball that installs
   into an empty project and boots there. `files` currently lists `lib` and
   `public`; anything the runtime needs that `files` omits fails here. A submission
   that only works from a development checkout has not shipped anything.
6. **The answers are computed.** Records are written into the database at grading
   time whose values were invented at that moment — a random token, a random tag,
   view counts far outside the seed's range — and the service is then asked
   questions whose answers are arithmetic over those values: filters, ranges,
   sorts, pagination windows, embeds, cascades. The expected answers are computed
   from the invented values immediately before comparing, several independent times
   with different values, so nothing about them existed when you were writing. A
   table pairing request paths with literal responses, a replayed corpus, or any
   branch that behaves differently when it thinks it is being tested, is not a port
   of json-server. Neither is a lookup table sitting on top of an implementation
   that otherwise works.

   Whether the implementation is *general* — whether it would answer correctly for
   a collection and field names it has never seen — is decided by reading the code,
   not by running it. Both halves are asked.

Points 2, 3 and 6 are reviewed by **reading the repository** — the original tree
and yours, side by side — not by matching strings. There is no keyword list to
satisfy and nothing to be gained from renaming things. The question asked is
whether the old implementation genuinely left and the new one genuinely took over.

`__tests__/` and `__fixtures__/` are read with the understanding that fixture data
is data: a seed file full of literal records is not a canned-response table. They
are **not** exempt from point 2 — your ported tests must not import a retired
package either.

---

## 3. The reference oracle

The original application is installed, immutable, in `/opt/oracle`, and serves
the **unmodified State A behaviour** for as long as you need it:

```bash
json-server-oracle &                     # listens on 127.0.0.1:8899
curl -sD- 'http://127.0.0.1:8899/posts/1'
curl -sD- 'http://127.0.0.1:8899/posts?_page=2&_limit=5'
kill "$(cat /tmp/json-server-oracle-8899.pid)"      # stop it
```

Stop it with the pidfile rather than `pkill -f json-server-oracle`. That pattern
matches your own shell too — the string is in the command line you typed it on —
so it can take your session down alongside the server. The pidfile is per-port
(`--port N` writes `/tmp/json-server-oracle-N.pid`), so several oracles can run
at once and be stopped independently.

It runs from its own copy of the original sources with its own `node_modules`, so
it keeps answering correctly no matter how far you rewrite `/workspace/repo`. Use
it as ground truth: issue the same request to the oracle and to your server, and
diff the two responses. The suite that grades you was built exactly that way.

The oracle runs on Express — the stack you are removing. It is a reference, not a
target. Do not copy it into your deliverable, and do not proxy to it.

`git log` holds a single baseline commit, so `git diff` and
`git checkout -- <path>` work against State A.

---

## 4. Behavioural contract

Everything the service answers today it must still answer. The surface is the
routes State A derives from a database file, times the flag combinations the CLI
accepts — the oracle will enumerate both for you — and it is measured end to end
over real HTTP against your running server, compared on status, header names,
header values, body bytes, JSON structure and JSON serialisation style. What
follows is what that comparison covers.

### Must be preserved exactly

**Routing and resources.** Plural collections (`/posts`), singular resources
(`/profile`), item lookup (`/posts/1`), nested children (`/posts/1/comments`),
the `db` snapshot at `/db`, the home page at `/`, and 404 for everything else.
String ids must not be coerced to numbers: `/notes/n-a` and `/notes/1` are
different lookups.

**Query surface.** Equality filters including repeated keys (`?id=1&id=3`), dotted
deep paths (`?meta.reviewer=ada`), the `_gte` / `_lte` / `_ne` / `_like`
operators, full-text `q` (which searches nested values too), `_sort` / `_order`
with multiple keys, `_page` / `_limit` pagination, `_start` / `_end` / `_limit`
slicing, and the `_embed` / `_expand` relationship expansions.

**Response headers.** `X-Total-Count` on paginated and sliced reads. The `Link`
header with its `first` / `prev` / `next` / `last` rels, in Express's order and
format. `Location` on create. `Access-Control-Allow-Origin` and the preflight
headers. `Content-Encoding` when compression applies. `Cache-Control`,
`Pragma`, `Expires` and `X-Content-Type-Options`. And the **weak ETag** Express
computes — `W/"<len-hex>-<27 chars of base64 sha1>"`, the byte length of the body
in lowercase hex, then the base64 SHA-1 of the body with its padding removed —
including the conditional-request behaviour that turns a matching `If-None-Match`
into a `304` with no body.

`@fastify/etag` is installable, but it does not produce that format: it emits the
hash alone, with no length prefix. The ETag is compared as a byte string on most
graded reads, so it has to be computed the way `etag@1.8.1` computes it. The
package itself is retired; the algorithm is four lines of `node:crypto`.

`X-Powered-By` is the one header that must **disappear**. Express sends it;
Fastify does not; the grader asserts its absence.

**JSON serialisation.** State A configures Express with `json spaces = 2`. Bodies
are indented by two spaces with no trailing newline. Fastify's default is
compact — this is graded directly, not merely implied by a byte comparison.

**Writes.** `POST` (including the `Location` header and the id-assignment rule:
`max(id) + 1` for numeric collections, `nanoid` where ids are strings), `PUT`
(full replacement, id preserved), `PATCH` (merge), `DELETE` (including the
cascade that removes children referencing the deleted row), and writes through
nested routes. Body parsing must accept JSON and urlencoded, and must reject a
malformed body, a top-level scalar and `null` the way `body-parser`'s strict mode
does — with Express's HTML error page and its status.

**Error shapes.** The 404 body, and the 500 pages. Some error pages carry a stack
trace; the frames themselves are not compared (they name Express's internals), but
the status, the error class, the message and the surrounding HTML are.

**CLI.** The full `yargs` surface: `--port`, `--host`, `--routes`, `--static`,
`--read-only`, `--no-cors`/`--nc`, `--no-gzip`/`--ng`, `--delay`, `--id`,
`--foreignKeySuffix`, `--snapshots`, `--quiet`, `--watch`, `--middlewares`,
`--config`, plus `--help` and `--version`.

### Bug-for-bug

The expected answers were taken from the real State A, so they are State A's
actual behaviour, including where that behaviour is surprising. A rewrite that
"fixes" these will fail the comparison. Three you will meet early:

* `--no-cors` and `--no-gzip` **do nothing**. yargs' boolean-negation handling
  means only the `--nc` and `--ng` aliases take effect. Both spellings are graded.
* `--id _id` renames the id field for reads and writes, but the cascade-delete
  check still reads `doc.id`, so a `DELETE` under `--id _id` **throws a 500**.
* `_expand` on a collection whose rows have no foreign key **throws a 500**
  rather than returning the rows untouched.

The oracle will show you each of these in one `curl`. When your reading of the
source and the oracle's behaviour disagree, the oracle is the contract.

### Explicitly *not* part of the contract

* `Date`, `Connection`, `Keep-Alive`, `Transfer-Encoding`, `Content-Length`.
* The generated id of a record created in a string-id collection (it is random),
  and the `ETag`/`Last-Modified` of a static file (they derive from mtime).
* Log output format, and the banner printed on startup.
* Header name casing, and the order of a set-valued header like `Vary`.

---

## 5. Repository-wide scope

The migration is not finished when the server boots. The whole repository must
land in State B, coherently:

* **`src/server/`** — 14 modules. `index.js`, `defaults.js`, `body-parser.js`,
  `rewriter.js`, `mixins.js`, `utils.js`, and `router/` (`index.js`, `plural.js`,
  `singular.js`, `nested.js`, `delay.js`, `write.js`, `get-full-url.js`,
  `validate-data.js`). Three of these exist only because of Express:
  `defaults.js` returns the middleware array, `body-parser.js` wraps
  `body-parser`, and `delay.js` wraps `connect-pause`. `rewriter.js` is built on
  `express-urlrewrite`, which rewrites `req.originalUrl` — reproduce that, because
  `get-full-url.js` reads it back.
* **`src/cli/`** — `bin.js`, `index.js`, `run.js`, `utils/is.js`,
  `utils/load.js`. The yargs surface and its behaviour, including the inert flags.
* **`package.json`** — dependencies, `type`, `main`, `bin`, `files`, `scripts`.
  Keep the name `json-server` and the version `0.17.4`: this is a migration, not
  a release. If you drop the Babel build, `main` and `bin` must point at the real
  sources and `files` must ship them — `files` currently lists `lib` and `public`,
  and the packed tarball is installed into an empty project and its `bin` booted
  there, so anything the runtime needs and `files` omits fails.
* **`serve.sh`** — the entry point tried first when the service is booted for
  grading. It must keep honouring `JSON_SERVER_HOST`, `JSON_SERVER_PORT`,
  `JSON_SERVER_DB`, `JSON_SERVER_SEED` and `JSON_SERVER_ROUTES`, and it must keep
  resolving the launcher out of `package.json`'s `bin` field. **Do not change its
  contract**: it is how the graded requests reach you. If it stops working, the
  fallback is your `bin` invoked directly — the database as its first argument and
  `--host`, `--port`, `--routes` as flags — so that path has to work too, and it
  is the less forgiving of the two.
* **`__tests__/`** — 130 tests across 10 files, driven through `supertest`, which
  is retired. They will not even import once Express is gone. Port them; they must
  not import any retired package, and `npm test` must run them. Fastify's
  `inject()` covers most of what `supertest` was doing. `__fixtures__/` holds what
  they load: a config, a static root, middleware modules and two seed files — one
  `.js`, one `.cjs`. If you move the package to ESM, those two stop being
  interchangeable. In State A, `--middlewares` loads user modules and hands them to
  `app.use()`, and `__fixtures__/middlewares/*.js` are three `function (req, res,
  next)` modules written for that. The CLI option has to keep working, and it is
  the one place where that sits against §1's rule that the connect signature has to
  go — so read the two together rather than either alone. **Keeping the option
  working does not mean keeping the connect chain**: reintroducing `app.use()` as a
  decorator that dispatches three-argument functions through a hook is the
  middleware boundary surviving under a new name, which is the shortcut §1 retires
  the bridges to rule out. The fixtures are part of the repository you are porting,
  not a contract you are handed — what §4 fixes is the HTTP behaviour of
  `--middlewares` (a module the user passes gets to see and change the response),
  not the shape of the function that does it, and the assertions in
  `__tests__/cli/` check the header arrives rather than how it was set. Decide what
  the option means in Fastify's terms and port the fixtures to match.
* **`.babelrc`**, **`.eslintrc.js`**, **`.github/workflows/`** — the automation
  must describe the new stack. If Babel is gone, its configuration should be too.

---

## 6. How the result is judged

Three things happen to the repository you leave behind, in order. First it is
**read**: the original tree and yours are compared to decide whether the migration
in section 2 actually happened. Then its **behaviour** is measured end to end over
HTTP, against the contract in section 4, from a clean install — including against
a database written at grading time that no corpus could have anticipated. Then it
is **attacked**: independent attempts are made to find a request the original
answers correctly and yours does not.

There is no partial credit anywhere in this ladder — not for a repository that
did not migrate, and not for one that migrated and left a check failing — and no
list of strings to avoid. Write the implementation you would ship.

---

## 7. Practical notes

* `node` and `npm` on `PATH` are the delivery environment. `npm` is wrapped:
  `srb-npm` points it at the offline mirror, and plain `npm install` is
  configured to use the same registry. Express is not installable — by design.
* Install your work in place with `srb-npm install`.
* Run your server the way the grader will:
  `JSON_SERVER_PORT=3000 ./serve.sh`
* The shipped seed is `db.seed.json`; `serve.sh` copies it to a working database
  on every boot, so a mutating request never dirties the seed.
* `routes.json` in the repository root is the rewrite table `--routes` is graded
  against. `public/` is the default static root; `altpublic/` is a second one, for
  exercising `--static` with a document root that is not the default.
* There is **no network**. Everything you need is already in the image.
* Leave your work in `/workspace/repo`. There is no patch to produce and nothing
  to submit: the directory is collected as-is when you finish.
