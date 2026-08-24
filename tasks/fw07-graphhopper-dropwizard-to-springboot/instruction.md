# Migrate GraphHopper's web layer from Dropwizard 4 to Spring Boot 3

You are working in `/workspace/repo`, a frozen checkout of **GraphHopper 11.0** —
a routing engine: it imports an OpenStreetMap extract into a graph and answers
routing, isochrone, map-matching, nearest-point, vector-tile and public-transit
queries over HTTP. Its entire web layer is **Dropwizard 4.0.16**.

Your job is to migrate the **whole repository** to **Spring Boot 3.5**, retiring
Dropwizard, Jersey, HK2 and JAX-RS completely — while keeping what the service
does over HTTP and on the command line exactly as it is.

This is a behaviour-preserving migration. **Do not add endpoints, do not remove
configuration options, do not change the HTTP contract.** Every response the
service produces today it must still produce.

The routing engine itself is not part of this. `core`, `web-api`, `reader-gtfs`,
`map-matching`, `client-hc`, `tools` and `example` do not name the retired stack
and should come out unchanged. What changes is the web tier: `web-bundle` (27
files), `web` (5 in main, 25 in test), `navigation` (1), and 4 of the 11 poms.

That boundary is part of the task. A submission that starts rewriting `core` has
misread the job.

---

## 1. The two states

|                            | State A (now)                                                        | State B (required)                                        |
| -------------------------- | -------------------------------------------------------------------- | --------------------------------------------------------- |
| Framework                  | Dropwizard 4.0.16                                                    | Spring Boot 3.5.16                                        |
| Entry point                | `Application<T>` + `Configuration`, `bootstrap.addBundle(...)`        | yours — `@SpringBootApplication` or otherwise             |
| HTTP resources             | JAX-RS: `@Path` / `@GET` / `@QueryParam` / `@Produces`                | Spring MVC: `@RestController` / `@GetMapping` / `@RequestParam` |
| Registration               | 25 explicit `environment.jersey().register(...)` calls                | component scanning, `@Bean`s, or explicit registration    |
| Dependency injection       | HK2 `AbstractBinder`: 16 bound types, `bindFactory`, `.named(...)`     | Spring beans                                              |
| Configuration              | Dropwizard YAML → `Configuration` subclass, Bean Validation           | yours — the **file format must not change**               |
| Errors                     | 3 `ExceptionMapper`s + a `MessageBodyWriter`                          | yours — the **documents must not change**                 |
| Admin surface              | Dropwizard's admin connector on a second port                         | yours — the **paths and port split must not change**      |
| Health checks              | codahale `HealthCheck` + `/healthcheck`                               | Actuator, or hand-written                                 |
| Metrics                    | codahale `MetricRegistry` → `/metrics`, `/threads`                    | Micrometer, or hand-written                               |
| CLI                        | argparse4j `Command`s: `server`, `check`, `import`, `match`           | yours — the **commands must still work**                   |
| Static assets              | two `AssetsBundle`s: `/maps/`, `/webjars/`                            | yours — the **URLs must not change**                      |
| CORS                       | a servlet `Filter` across every pattern and dispatcher type           | yours — the **coverage must not change**                  |
| Embedded container         | Jetty                                                                | free choice — Tomcat, Jetty or Undertow                   |
| Everything else            | the routing engine, GTFS, map matching, the graph                     | unchanged                                                  |

### Retired artifacts

None of these may appear in the delivered project — not in a pom, not as an
import, not vendored into the tree:

```
io.dropwizard.*                      the framework
org.glassfish.jersey.*               the JAX-RS implementation
org.glassfish.hk2.*                  the injection container
jakarta.ws.rs.*                      the JAX-RS API
com.fasterxml.jackson.jakarta.rs.*   its Jackson integration
```

**They are not available to build against.** The local Maven repository holds no
jar for any of them and there is no network to reach past it, so a tree that
still compiles against them is a tree you could not have built. The full list,
with the reasoning, is not something you have to take on faith — run
`mvn -o -pl web-bundle -am package` on the tree as you found it and read what
Maven says.

One asymmetry is deliberate: the **jars are gone, the POMs are kept**. The root
pom imports `dropwizard-dependencies` as a BOM, and a BOM with no POM stops Maven
before it can load the reactor at all — every command would fail on a model error
instead of on your code. With the POMs present, `mvn` runs, resolution succeeds
elsewhere, and the failure arrives where it belongs: the web modules cannot
resolve their dependencies, and the message names exactly which artifacts are
missing.

**Spring is the target, not "something that is not Dropwizard".** Quarkus,
Micronaut, Helidon, Vert.x, or Jersey standalone on a bare Jetty is a different
task, and none of them is in the repository either.

### What is available

`spring-boot-dependencies:3.5.16` is warmed, and with it:

- `spring-boot-starter-web`, `-actuator`, `-validation`, `-test`, and the
  configuration processor
- all three embedded containers — `-tomcat`, `-jetty`, `-undertow` — so the
  choice stays yours
- `jackson-dataformat-xml` (GPX is XML), `-yaml`, `jackson-datatype-jsr310`,
  `-jdk8`
- `micrometer-core`, `micrometer-registry-prometheus`
- `argparse4j` 0.9.0 — State A's argument parser is **not** retired; it is not a
  web framework, and you may keep it, replace it with Spring Boot's own
  `ApplicationRunner`/`CommandLineRunner`, or hand-roll the parsing
- `logback-classic`, `jcl-over-slf4j`

Deliberately absent: **WebFlux** and **Jersey**. This service is synchronous and
blocking, the task names Spring MVC's model, and stocking an unnamed alternative
is how a task acquires two right answers.

Everything State A already resolved is still there at the pinned versions, so the
routing engine's own dependencies need no attention.

`~/.m2/settings.xml` and `~/.npmrc` are configured for offline use. `mvn` needs
no `-o` flag; it is already offline. If you find yourself wanting a dependency
that is not warmed, the answer is almost always that the target stack has it
under a different name.

One thing that is easy to trip over and hard to debug: `web-bundle` builds the
map UI with `frontend-maven-plugin`, which runs `npm`. That works offline —
the npm cache is warmed — but only for the dependency set the checked-in
`package-lock.json` pins. Editing the frontend's dependencies is not part of this
task and will fail to resolve.

---

## 2. What "identical behaviour" means here

Grading builds your tree and State A's with the same Maven command, starts both,
and asks both the same requests — status line, headers and body. It also builds
the project, runs the repository's own test suite, exercises the command line,
and checks that the same YAML still configures the same service.

Whether the framework was actually retired is judged **separately, by reading
both trees rather than running them.** So a tree that keeps Dropwizard answers
every replayed request perfectly and still scores zero — and it would not have
compiled for you anyway.

The contract is therefore the wire, not the code. There is no required file
layout, no required class name, no required number of registrations. Reorganise
the web modules however the target framework wants: merge them, split them,
rename packages, delete the bundle concept entirely. What is not negotiable is
that the answers do not move.

Three consequences worth stating outright:

- **Hardcoding responses is not a migration.** A controller that returns recorded
  JSON for the paths someone thought to record reproduces a corpus and fails the
  task. Requests nobody showed you are fair game, and an adversary is paid to
  find one you got wrong.
- **A green build is not the goal.** An endpoint that is registered but
  unreachable, a query parameter silently ignored, a filter that no longer sees
  error responses, a health check that is never run — these all compile.
- **The routing answers have to be real.** Distances, times, instruction text and
  geometry come out of the engine you are not touching. If they changed, you
  changed something you should not have.

---

## 3. The oracle: State A itself

State A is running-ready in this container, and it is the oracle — there is no
other, and you need no other.

It arrives as a **prebuilt jar** rather than as something you compile, and the
reason is the dependency ban itself: the local Maven repository here holds no
`dropwizard-core` jar, so State A's own web layer cannot be built here. `mvn -o
-pl web -am package` on the untouched tree dies at dependency resolution naming
the retired coordinates — that is expected rather than a fault, and it is the
same wall your own build starts behind. The jar was built before the retirement
and repackaged as a fat jar, so it carries its dependencies and runs with nothing
from `~/.m2`.

```bash
state-a start base      # State A, no public transport, Andorra extract
state-a start pt        # State A with a GTFS feed  (§4.1, §4.2)
state-a stop            # `status` and `log` also work
state-a verify          # are the oracle's own files still the ones that shipped
state-a restore         # put its configs back if they are not

# ask it anything
curl -sS 'http://localhost:8989/route?point=42.51,1.53&point=42.53,1.62&profile=car' | jq .
curl -isS 'http://localhost:8989/route?point=42.51,1.53'
curl -isS http://localhost:8990/healthcheck
```

The two configurations it starts under are `/opt/oracle/config-base.yml` and
`/opt/oracle/config-pt.yml`, and their graph caches are already imported, so the
oracle answers within a few seconds. Read them: `config-pt.yml` is the one that
turns on the five conditional registrations of §4.1 and selects a `PtRouter` in
§4.2, and the difference between the two files is the whole of what §4.1 asks a
port to reproduce.

**Copy those two files; do not edit them in place.** They are the reference you
compare your work against, and you run as root, so nothing stops you from
changing them — an edited oracle keeps answering every query with exactly the
same confidence, and you would be diffing against your own mistake without a
sign that anything was wrong. `state-a verify` checks them against a digest
recorded when the image was built and `state-a restore` puts them back, so an
accident costs a command rather than a run. `start` refuses to launch an oracle
that does not match, which is the one moment the answer is still worth having.

Your own server is a different matter, and here two practicalities bite. It needs
a config of its own — copy an oracle config to a path of your own, or start from
`config-example.yml`, which documents every option — and **its first launch on a fresh
`graph.location` is slow**, because GraphHopper imports the extract and runs the
contraction-hierarchy and landmark preparations before the port binds. Point
`graph.location` somewhere you keep between runs and the loop gets fast. Watch
the log rather than guessing when it is ready.

You can skip the import for your own server too. GraphHopper loads an existing
graph directory without re-reading the extract, so
`cp -a /opt/oracle/graph-cache/base /tmp/mine` and pointing **your own config's**
`graph.location` there starts in seconds — as long as your profiles and
`graph.encoded_values`
match the config that built it, which they will if you copied that config. Copy
rather than share the directory: two servers writing one graph is its own bug,
and you do not want to spend a morning on it.

Then run the oracle and your rewrite side by side and diff the answers. That is
exactly what grading does, and nothing stops you from doing it first. The oracle
holds 8989 and 8990 while it is up, so either give yours different ports or
`state-a stop` first.

The oracle answers **any** question you ask it, including ones no test in this
repository asks. That is why you have it. What it will not tell you is whether
your migration passes: there is no self-check here, no gate list, and no list of
graded requests. Build the comparison you need.

---

## 4. The behavioural contract

Everything below is observable through the oracle. It is written down because
these are the places where Spring MVC and JAX-RS genuinely disagree, and where a
reasonable-looking rewrite silently changes an answer. Verify each of them
against State A rather than against this document — if they ever disagree, State
A is right.

### 4.1 Registration is conditional, and the condition is in the config

Five of the 25 registrations sit inside `if (config.has("gtfs.file"))` — three
resources, one filter, and the binder that chooses a `PtRouter`. Two more sit in
the mutually exclusive branches of a second condition on whether any GTFS
realtime feed is configured, so exactly one of that pair runs. So:

- with a GTFS feed configured: `/route-pt`, `/isochrone-pt` and the PT vector
  tiles exist, and a request naming `pt` in either `profile` or `vehicle` on
  `/route` or `/isochrone` is diverted into them
- without one: **those paths do not exist at all**

Read that diversion carefully, because its mechanism is observable. The filter
rewrites the request URI **in process** and drops the `vehicle` and `profile`
parameters as it does — the client gets the transit answer back from the URL it
asked for, with no `3xx` and no `Location` header. A port that reaches for
`sendRedirect` or a `RedirectView` produces a redirect the original never sends,
which any client following it will resolve into a *different* second request.

Component scanning is unconditional by default. A port that simply annotates the
PT resources and lets them be discovered exposes transit endpoints on a service
with no transit data — and they will not answer 404, they will fail some other
way. Reproducing "this endpoint exists only under this configuration" is
deliberate work.

`/healthcheck`, `/metrics`, `/threads` and the two asset trees are
unconditional. Do not make them conditional by accident.

### 4.2 One interface, several implementations, chosen by configuration

`PtRouter` has three implementations, and which one is bound depends on two
boolean flags:

```java
if (config.getBool("gtfs.free_walk", false))        bind(PtRouterFreeWalkImpl.class)
else if (config.getBool("gtfs.trip_based", false))  bind(PtRouterTripBasedImpl.class)
else                                                bind(PtRouterImpl.class)
```

and then all three are *also* bound under the names `classic`, `free_walk` and
`trip_based`, because a resource asks for a specific one by name.

A framework that resolves by type finds three candidates for one type and either
fails to start or picks one. Both outcomes are wrong, and the second is worse
because it starts, serves, and answers differently. This is the single most
likely place for a port to be quietly incorrect.

Three `reader-gtfs` classes carry `@Inject` on their constructors. That is
JSR-330, not the retired framework, and Spring honours it as it stands — leave
them alone.

### 4.3 The error document has a shape, and it is not the framework's default

State A's mappers produce a JSON body with a `message` string and a `hints`
array, where each hint carries its own `message`, a `details` field naming the
exception class, and any extra fields the exception itself supplies. A validation
failure on `/route` is a `400` with that document — not a Spring `ProblemDetail`,
not a Boot error page, not an empty body.

The good news: the code that writes that document is `MultiExceptionSerializer`
in `web-api`, which is **not** part of this migration and needs no changes. You
inherit the shape. What you have to rebuild is the part that is the framework's:
catching the exception, choosing the status, and picking the serializer for the
negotiated media type. A Spring `@ControllerAdvice` that hands a `MultiException`
to the existing Jackson module gets this right; one that builds its own body from
the exception's message does not.

`?type=gpx` changes the media type of **errors too**: a `MessageBodyWriter` turns
the same failure into a GPX document. Content negotiation applies to the error
path, not just the success path.

How that happens is worth knowing, because it has no direct counterpart on the
target stack. `TypeGPXFilter` is a `@PreMatching` request filter that, when
`type=gpx` is in the query string, **overwrites the request's `Accept` header**
with `application/gpx+xml` before routing happens. Everything downstream — which
handler method is selected, which writer serialises the result, what the error
document looks like — then follows from ordinary content negotiation on a header
the client never sent.

So `?type=gpx` and `Accept: application/gpx+xml` are not two features; they are
one feature reached two ways, and they must stay equivalent. A port that instead
inspects `type` inside each handler has to remember every handler, including the
error path, and will usually miss one.

The status codes are not uniform and not guessable. Some invalid input is a 400,
some is a 500, and some parameters are accepted and ignored rather than
rejected. **Ask the oracle for each one.** Two ways to get this wrong that are
worth naming: assuming a 400 where GraphHopper actually answers 500, and
assuming a parameter is validated when in fact it is ignored. Keeping the status
codes while losing the document shape is the easiest way to pass a smoke test and
fail this task.

Bean Validation failures on the configuration are their own path, distinct from
routing errors.

### 4.4 Two connectors, two ports

The application surface and the admin surface are on **different ports**, and the
admin surface has its own paths: `/healthcheck`, `/ping`, `/tasks`, `/metrics`,
`/threads`, and an index page. The split is configured in the YAML — under
`server:`, the lists `application_connectors:` and `admin_connectors:`, each
entry carrying `type`, `port`, `bind_host` and, on the application connector,
`max_request_header_size`. Those are the key names `config-example.yml` uses and
the ones the file you will be handed uses.

An application endpoint must not answer on the admin port, and an admin endpoint
must not answer on the application port. Actuator on a `management.server.port`
is one honest way to rebuild this; making everything answer on one port is not.

And there are **two** health surfaces, not one, which is the trap in this section:

- `/health` on the **application** port is a JAX-RS resource of the repository's
  own. It runs every registered check and collapses the result to a plain-text
  `OK` with 200 or `UNHEALTHY` with 500. No JSON, no per-check detail.
- `/healthcheck` on the **admin** port is the framework's, and it reports each
  check by name as a JSON document.

Both must survive, on their own ports, with their own bodies. Pointing Actuator's
health endpoint at both ports gives you the detailed document in the place the
terse one belongs.

`GET /` on the application port is also not nothing: it answers `303` with
`Location: maps/`.

### 4.5 The 405-versus-404 split says whether a request was dispatched

JAX-RS answers `405` when a path matches and the method does not, and `404`
when nothing matches. The boundary between those two answers is a map of which
routes exist. A framework that returns 404 where the original returned 405 — or
the reverse — has changed the observable shape of the routing table even though
every valid request still works.

Trailing slashes, doubled separators and encoded separators all resolve one way
in State A. Ask before you assume; do not "fix" what you find.

### 4.6 CORS is a servlet filter over everything

`CORSFilter` is installed with `addMappingForUrlPatterns(EnumSet.allOf(
DispatcherType.class), false, "*")`. Read that literally: **every** URL, **every**
dispatcher type. It applies to successful responses, to error responses, to 404s
on paths no resource claims, and to preflight requests.

Spring's `@CrossOrigin` and `CorsRegistry` are handler-level, so they cover
controller responses and not much else. A port that uses them narrows CORS
without changing a single visible header on the happy path.

### 4.7 The YAML file is a contract with operators

`config-example.yml` documents the file, and operators depend on it more than on
any single route parameter. The graded question is whether the same file produces
the same service: the profiles it will route on, the graph cache it creates, the
ports it binds, the oversized header it refuses.

`graphhopper:` holds a flat map of engine settings that the engine parses itself;
`server:` is the connector configuration. Both must keep working. Spring Boot's
own `application.yml` conventions are fine as an internal mechanism — what may
not change is the **file the operator writes**.

That contract includes what the file is **refused** for, and here the two blocks
differ. Dropwizard binds the top level and `server:` to typed objects, so a
misspelled key there is a startup error naming the field: `server:` written
`serverr:` does not start, and neither does `application_connectorss:` under it.
Inside `graphhopper:` the opposite holds — it is a flat map, so an unknown key is
accepted and carried. **Keep both halves.** A binder that accepts anything at the
top level turns an operator's typo into a silently ignored line, and constraint
annotations carried across but never run against the parsed object are inert. Ask
the oracle what a given file does; `check` (§4.8) is where this shows.

### 4.8 The command line

`server`, `check`, `import` and `match` all still have to work: same names, same
arguments, same exit codes, same effects on disk. `check` must still refuse a
malformed or missing config. `import` must still produce a graph cache.

The **usage text is not graded** — the two frameworks lay it out differently, and
grading it would be grading the framework rather than the migration. Exit codes
and effects are graded.

### 4.9 Static assets

`/maps/` serves the built map UI out of the jar, `index.html` as the directory
index; `/webjars/` serves webjar resources from `/META-INF/resources/webjars`.
Both URLs stay. The UI is *built* by the frontend plugin during
`mvn package` — check that it still ends up on the classpath where the asset
handler looks for it.

### 4.10 Everything else

Content negotiation (`?type=gpx`, `Accept`, gzip), localisation and `/i18n`,
`/info`, `/nearest`, `/isochrone`, `/spt`, `/mvt`, `/match`, `/navigate`, the
`hints` on a failed route, the flexible/CH/LM back-end selection and its
per-profile acceptance rules, `custom_model`, elevation, path details,
instructions, `snap_prevention`, `curbside`, `heading` — all unchanged by the
migration, which means all of them must still work afterwards. They are the part
of the surface most likely to break by accident, because nothing about them looks
framework-related.

---

## 5. Repository scope

```
pom.xml                 the reactor; imports dropwizard-dependencies as a BOM
config-example.yml      the documented configuration surface

web/                    the application: entry point, CLI commands, its tests
  .../GraphHopperApplication.java     Application, bundles, commands, CORS filter
  .../GraphHopperServerConfiguration.java
  .../cli/ImportCommand.java, MatchCommand.java
  .../resources/RootResource.java
  src/test/                           31 integration tests that boot the app

web-bundle/             the web layer proper  (the bulk of the migration)
  .../http/GraphHopperBundle.java     25 registrations, 16 bindings, the wiring
  .../resources/                      RouteResource, IsochroneResource, SPTResource,
                                      NearestResource, MVTResource, InfoResource,
                                      I18NResource, HealthCheckResource,
                                      MapMatchingResource, PtRouteResource,
                                      PtIsochroneResource, PtMVTResource
  .../http/                           the exception mappers, TypeGPXFilter,
                                      CORSFilter, PtRedirectFilter, the factories
  .../resources/com/graphhopper/maps/ the map UI sources

navigation/             NavigateResource — one file names the retired stack

core/  web-api/  reader-gtfs/  map-matching/  client-hc/  tools/  example/
                        the engine.  Not part of this migration.
```

`GraphHopperBundle.java` is where the framework's shape is most concentrated:
`initialize` installs the config plumbing, `run` does the 25 registrations and
the 16 bindings, and the two conditional blocks in it are §4.1 and §4.2. It is
the file to read first and the one whose replacement is the actual design work.

**The tests are part of the repository and they are yours to carry.** `web`
carries 31 test files and 25 of them name the retired stack — 23 stand the whole
application up through `DropwizardAppExtension` and talk to it over HTTP, which is
the pattern with no drop-in equivalent. `web-bundle` (3 files) and `navigation`
(5) name it in none of theirs. Across the three modules 211 tests pass today.

Where a test constructs its server through Dropwizard's testing support it will
not compile; where it asserts on behaviour it should keep asserting on the same
behaviour. Port them — `spring-boot-starter-test` is warmed for exactly this, and
`@SpringBootTest(webEnvironment = RANDOM_PORT)` is the shape those 23 turn into.

Deleting a test to make the suite green is the one repair that is worse than
leaving it broken: grading counts how many of the repository's own tests pass
relative to State A's own count, measured in the same run, so an idiomatic rewrite
costs you nothing and a deletion costs you the module.

Docs, `Dockerfile`s and CI config should still be coherent afterwards. Mentioning
in a README or CHANGELOG what was replaced is fine — the question is whether the
code is there, not whether the name is spoken.

---

## 6. How the result is launched

Grading does not assume how your application starts. It takes the runnable jar
your build produced and tries a small ladder of standard invocations until one
binds a port and answers HTTP:

```
java -jar <jar> server <config.yml>                                  # State A's form
java -jar <jar> --spring.config.location=file:<config.yml>
java -jar <jar> --spring.config.additional-location=file:<config.yml>
java -jar <jar> <config.yml>
java -jar <jar>                                                      # embedded config
```

Any of those is fine; which rung you land on is recorded and **not** scored. What
you must not do is make the application startable by none of them. Two things
follow:

- `mvn -DskipTests package` must produce **a jar with a `Main-Class` in its
  manifest**. Spring Boot's repackaging does this (`Main-Class` is the launcher,
  `Start-Class` your application); a plain jar without it is not launchable, and
  the largest launchable jar under any `target/` is the one that gets picked.
- The config file must reach the application by one of those routes, and the
  ports in the YAML must be the ports it binds.

---

## 7. Definition of done

1. `mvn -DskipTests package` succeeds offline from a clean tree, and produces a
   launchable jar.
2. `mvn test` passes, with the repository's own web tests **ported** rather than
   removed.
3. No `io.dropwizard`, `org.glassfish.jersey`, `org.glassfish.hk2`,
   `jakarta.ws.rs` or `com.fasterxml.jackson.jakarta.rs` artifact remains in any
   pom, any import, or the tree.
4. Spring Boot is what runs the application: Spring's dispatcher routes the
   requests, Spring's context holds the objects, Spring's configuration binding
   reads the YAML. A hand-written servlet that scans for annotations you defined
   with the old names is the retired framework with the import removed, and it is
   judged as one.
5. The HTTP surface matches State A's — on both ports, under both configurations,
   including every point in §4 and including requests §4 does not mention.
6. The CLI, the YAML file format and the asset URLs are unchanged.

Work in `/workspace/repo` and leave your result there. There is nothing to submit
and nothing to register; the tree as you leave it is the deliverable. It is a git
checkout tagged `state-a`, so `git diff state-a` shows your work — commit if you
find it useful, or don't.

Put your changes in **tracked source files**. Build output is not collected:
`target/`, `.m2/`, `node_modules/`, graph caches and loose jars are all excluded
from what is taken out of this container, so anything that exists only inside one
of those will not be there when the result is graded.

One last note on how to spend your time. The mechanical part — annotations,
imports, poms — is an afternoon and it is not where submissions fail. §4.1 and
§4.2 are worth more than the rest of the migration put together, because they are
the two places where the obvious rewrite is confidently, silently wrong: a
transit endpoint that exists when it should not, and an interface with three
implementations where the framework picks for you. Ask the oracle before you
assume.
