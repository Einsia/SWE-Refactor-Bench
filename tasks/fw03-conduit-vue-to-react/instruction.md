# Migrate Conduit from Vue 2 to React 18

`/workspace/repo` holds the RealWorld **Conduit** single-page app, frozen at an
October 2019 revision. It is built on Vue 2: 25 single-file components, four
registered Vuex modules, `vue-router` 3 in hash mode, two global template filters,
a router guard that checks authentication before every navigation, and a webpack 4
build driven by `@vue/cli-service` with a PWA plugin that emits the service
worker.

Your job is to move the whole repository onto React 18 and Vite 5, and to leave
no Vue behind — not in the source, not in the dependency closure, not in the
release bundle. The product must not change while you do it.

This is a large piece of work. Read the code before you start planning.

---

## State A → State B

| | State A (now) | State B (required) |
|---|---|---|
| view layer | `vue@2.6.10`, `.vue` single-file components | `react@18.3.1` + `react-dom@18.3.1`, JSX |
| mounting | `new Vue({ render })` into `#app` | `react-dom/client` `createRoot` into `#app` |
| routing | `vue-router@3.1.3`, `mode: "hash"` | `react-router-dom@6.28.0`, hash router |
| state | `vuex@3.1.1`, 4 modules in one global namespace | first-party store (`zustand` or `@reduxjs/toolkit` + `react-redux` are available) |
| templating | directives (`v-if`, `v-for`, `v-model`) and filters | JSX and plain functions |
| HTTP | `axios` via `vue-axios` | `axios` directly |
| build | `@vue/cli-service@3.11.0` (webpack 4) | `vite@5.4.11` + `@vitejs/plugin-react@4.3.4` |
| service worker | `@vue/cli-plugin-pwa` + `register-service-worker` | `vite-plugin-pwa@0.21.1` (`register-service-worker` is still available) |

Everything else stays where it is. `marked@0.7.0`, `date-fns@1.30.1`,
`axios@0.19.0` and `register-service-worker@1.6.2` are pinned at the versions
State A used, because the app's rendered output depends on how they behave.
Upgrading them changes the product.

---

## What must be preserved exactly

The contract is what the app **shows** and what it **asks the API for**. Two
observers — one watching State A, one watching your port — drive the same 100
scenarios and must see the same thing.

**Rendered DOM.** For every route and every interaction: the same element tree
under `#app`, the same tags in the same order, the same text, the same
semantically meaningful attributes, the same classes as a set. This includes
empty states, error states, loading states, disabled buttons, `aria-*`
attributes, and the exact strings — `"No articles are here... yet."` is part of
the contract, and so is the article-meta date format (`date-fns`
`"MMMM D, YYYY"`).

**API requests.** The same method, the same path, the same query string, in the
same order, with the same `Authorization` header (or its absence). Paths matter
down to the trailing slash, and State A is not consistent about it — some of its
requests carry one and some do not, because of how its API wrapper assembles
URLs. Reproduce what it does, not what looks tidy. If State A fires two requests
for a view, so must you; if it fires them in a particular order, keep it. If it
fires a request with `undefined` in the path, so must you.

**Hash-mode URLs.** `#/`, `#/my-feed`, `#/tag/{tag}`, `#/login`, `#/register`,
`#/settings`, `#/editor`, `#/editor/{slug}`, `#/articles/{slug}`, `#/@{username}`,
`#/@{username}/favorites`, and the pagination query. A route that matches nothing
must behave as it does now, and so must a profile or article that does not exist.
`document.title` must keep changing the way it does now.

**localStorage.** The JWT lives under the key `id_token`, as a bare string. The
app reads it at boot to decide whether it is authenticated.

**Navigation is client-side.** Moving between routes must not reload the
document.

**Release artifacts.** `npm run build` must produce `index.html`, hashed JS, a
web-app manifest, and a service worker — the same four kinds of file State A's
build produces.

### Deliberately *not* part of the contract

You are not being asked to reproduce Vue's implementation details:

- class **order** within an attribute (compared as a set)
- whitespace and text-node splitting
- `data-v-*` scoping attributes, and Vue's comment placeholders for `v-if`
- how boolean attributes are spelled (`disabled` vs `disabled=""`)
- the `router-link-active` / `router-link-exact-active` classes the Vue router
  adds, and whatever your router adds in their place
- the names of your files, components, or store slices
- asset file names and hashes

Element **structure**, though, is part of the contract, including a `div` that
exists only to wrap something. This is not a Vue detail you are being asked to
imitate: a Vue 2 template has exactly one root element, so State A's tree never
contains a wrapper that React could not reproduce, and React 18 fragments let you
match it node for node wherever you want a component boundary State A did not
have. Read the templates and mirror the tree.

Where the observer is strict, it says so above. Where it is not, you have room
to write idiomatic React.

### One quirk to keep

After logout, the app leaves the previously-set `Authorization` header on its
axios instance. Subsequent requests still carry the stale token. This is State A
behaviour, the observer records it, and reproducing it is part of the contract.
Do not "fix" it.

---

## The environment

No network. Everything you may install is already in the offline npm cache at
`/opt/npm-cache`, and npm is configured to use it:

```
axios date-fns marked register-service-worker
react react-dom react-router-dom
zustand @reduxjs/toolkit react-redux
vite @vitejs/plugin-react vite-plugin-pwa workbox-window
eslint eslint-plugin-react eslint-plugin-react-hooks prettier cross-env
```

Nothing else installs. `npm install vue` fails with `ENOTCACHED` — that is npm's
own cache lookup refusing, not a policy you can edit. Do not spend time trying
to get around it; the migration is the task.

`/workspace/repo` is a git repository with one commit, tagged `baseline`. Commit
as you go if you find it useful; only the working tree is collected.

### The State A oracle

`/opt/oracle/dist` is the production bundle built from exactly this source, and it
is write-protected because it is your reference rather than your workspace. It is
the answer key for every behavioural question you have. Three tools are installed
to interrogate it:

```
swerefactor-oracle              # serve State A + the mock API, browsable
swerefactor-serve               # serve YOUR build the same way
swerefactor-diff <paths...>     # DOM diff: oracle vs yours, on paths you choose
```

`swerefactor-diff '#/' '#/login' 'alice:#/settings'` renders those paths in both
bundles and prints the differences. The `alice:` prefix means "authenticated as
alice". This is the fastest loop available to you: change something, diff it,
move on.

`/opt/harness` holds the mock API, the fixtures and the DOM reduction the grader
uses — the same code, verbatim. What is *not* there is the list of scenarios or any
recorded result. Deciding what to compare is part of the work.

Neither the oracle nor the harness is graded and neither is collected. The grading
containers build their own copies from checksummed archives, so editing these to
make a diff come out clean changes nothing except your own view of the truth.

---

## How the result is judged

Three things happen to the repository you leave behind, in order.

First it is **read**. The original tree and yours are put side by side and the
migration is checked: whether Vue actually left rather than being renamed, vendored
or reimplemented; whether React is what renders the screens; whether the bundle is
compiled from your source by your build rather than copied from somewhere; whether
what the screens display is computed from the API's responses instead of being held
in the code; and whether anything in the tree behaves differently when it thinks it
is being graded. This is done by reading, so there is no token list to satisfy and
nothing to be gained from renaming. A migration note in prose, a changelog entry,
or a comment explaining what a piece of code replaced is not a finding.

**A repository that did not migrate scores zero, however well it behaves**, and
nothing after this stage runs. A wrapper around Vue, a bundled copy of Vue, a
committed copy of State A's bundle, a screen whose contents are a literal table,
and a build that only works with the `node_modules` you happened to have all end
here.

Then its **behaviour** is measured, from a clean install and a fresh build, against
the contract above: about 21,700 end-to-end assertions across 100 recorded
journeys, comparing the DOM node by node, the controls each screen offers, the
request sequence, the routing surface, live form state across navigation, and the
persisted JWT. The report is per screen and not per assertion, so a regression
confined to one screen is named as that screen rather than smeared across the
suite — but the stage pays only for a submission that passed every assertion in
every screen. Twenty components right and five wrong pays what none right pays.
Aim at finishing screens, not at raising an average.

Then it is **attacked**. Independent attempts are made to find a journey State A
renders and yours does not, using the same kind of observation the behavioural
stage uses. Every attempt that fails to find one pays you.

The two measured stages are worth 40 and 60 points, and the behavioural stage pays
all of its 40 or none of them: come in one screen short and it pays nothing and
the attack is never attempted. Being *right* is therefore the whole of the score,
and being close is worth what being wrong is worth.

---

## Rules

1. **Edit `/workspace/repo` in place.** No patch file, no separate output. The
   working tree is what gets collected.
2. **The solution lives in tracked source.** `node_modules`, `dist`, `build`,
   `.vite`, `.cache` and archives are deleted before grading. Anything that only
   exists in one of those paths does not exist.
3. **The grader reinstalls and rebuilds from your `package.json`.** It ignores
   the `node_modules` and the `dist` you leave behind. If your build only works
   in your tree, it does not work.
4. **Delete what you replace.** An unused `.vue` file, a dead Vuex module or a
   stale `vue-cli-service` entry in `package.json` counts as Vue still being in
   the tree even if nothing imports it. The old implementation has to be gone,
   not orphaned.
5. **Do not write to the grader.** Do not target it, detect it, or hardcode what
   you think it wants to see. Both are looked for directly, and the mock API is
   restarted with values that did not exist when you wrote your code, so a screen
   holding its own answers shows up as a screen that renders the wrong ones.
6. **Keep a lockfile.** Commit the `package-lock.json` your install produces:
   the grader prefers `npm ci`, and a closure it can reproduce is part of the
   deliverable. Remove `yarn.lock` — it still pins Vue.

---

## Suggested order

The pieces are not equally hard, and some unblock others.

1. **Read State A.** `src/main.js`, `src/router/index.js`, `src/store/`,
   `src/common/`. Note the two global filters, the `beforeEach` guard, and the
   nested route records — those are easy to miss and each one is worth points.
2. **Stand up the build first.** Vite + React + the PWA plugin, rendering an
   empty `#app`. Prove `npm run build` works offline before porting anything.
3. **Port the shell**: `App.vue`, the header, the footer, the router. Now
   `swerefactor-diff '#/'` tells you something useful.
4. **Port the store and the API layer.** Every view depends on it, and the
   request contract is graded — get the paths and the header behaviour right
   here, once.
5. **Port views one at a time**, diffing each against the oracle as you finish
   it. Home and article are the largest.
6. **Then the interactions**: favourite, follow, comment, the editor's tag
   input, settings, login and register — including their error states, which are
   a real share of the corpus.
7. **Before you stop, rebuild from clean and diff again.** Delete
   `node_modules` and your build output, reinstall, run `npm run build`, and take
   `swerefactor-diff` across the routes listed above. That is the sequence the
   grader performs, and a tree that only builds incrementally fails it at the
   first step.
