// Remove a list of package names from an already-built registry mirror.
//
//   node prune-mirror.mjs <mirror-dir> <retired-list>
//
// build-mirror.mjs takes --ban and skips those names before fetching anything,
// which is right when one mirror is being built. It is wrong when two are wanted
// from the same bytes: warming twice can fetch a different tarball for a version
// that was re-published in between, and the two mirrors would then differ in more
// than the retirement. So the complete mirror is built once, copied, and the copy
// is pruned here.
//
// The mirror has two things per package, and they are not siblings -- the first
// version of this file assumed they were, pruned 48 packuments, left all 1309
// tarballs in place, and asserted itself green because it only ever looked for
// the packument:
//
//   <name>.json                        the packument, pruned to the closure's
//                                      versions
//   _tarballs/<name>/-/<base>-<v>.tgz  the bytes its dist.tarball points at
//
// Both go. npm cannot install from a tarball it has no packument for, so leaving
// the bytes does not make the retired stack reachable -- but a mirror that 404s
// on metadata while still holding the payload is a shape npm never produces, and
// the pruned copy exists precisely to be a faithful stand-in for the agent's
// registry. A stand-in that differs in 39MB of payload is worth less than the
// two lines it costs to be exact.
//
// Scoped names need care in both trees: `@fastify/express` is `@fastify/express.json`
// beside `_tarballs/@fastify/express/`, and `@fastify/` holds packages that stay.
// So a scope directory is removed only if pruning emptied it.

import fs from 'node:fs'
import path from 'node:path'

const [mirror, retiredList] = process.argv.slice(2)
if (!mirror || !retiredList) {
  console.error('usage: prune-mirror.mjs <mirror-dir> <retired-list>')
  process.exit(2)
}

const retired = fs
  .readFileSync(retiredList, 'utf8')
  .split('\n')
  .map((l) => l.replace(/#.*$/, '').trim())
  .filter(Boolean)

const tarballRoot = path.join(mirror, '_tarballs')

const removed = []
const absent = []
const scopes = new Set()
let tarballsRemoved = 0

for (const name of retired) {
  const packument = path.join(mirror, `${name}.json`)
  const tarballs = path.join(tarballRoot, name)
  let hit = false

  if (fs.existsSync(packument)) {
    fs.rmSync(packument)
    hit = true
  }
  if (fs.existsSync(tarballs) && fs.statSync(tarballs).isDirectory()) {
    tarballsRemoved += fs
      .readdirSync(path.join(tarballs, '-'), { withFileTypes: true })
      .filter((e) => e.isFile() && e.name.endsWith('.tgz')).length
    fs.rmSync(tarballs, { recursive: true, force: true })
    hit = true
  }
  if (name.startsWith('@')) scopes.add(name.split('/')[0])
  ;(hit ? removed : absent).push(name)
}

// A scope directory left behind empty is not wrong, but it is noise in a mirror
// listing and it makes `test ! -e` assertions on the scope ambiguous.
for (const scope of scopes) {
  for (const dir of [path.join(mirror, scope), path.join(tarballRoot, scope)]) {
    try {
      if (fs.readdirSync(dir).length === 0) fs.rmdirSync(dir)
    } catch {
      /* already gone, or not empty */
    }
  }
}

console.log(
  `pruned ${removed.length} of ${retired.length} retired name(s), ` +
    `${tarballsRemoved} tarball(s)`,
)
if (removed.length) console.log(`  removed: ${removed.sort().join(' ')}`)
if (absent.length) {
  console.log(
    `  not in this mirror (nothing in either lockfile resolved them): ` +
      absent.sort().join(' '),
  )
}

// A retired name still reachable would make the pruned mirror the wrong thing to
// compare against, so say so loudly rather than let the build pass. Checked on
// both halves, because the packument-only check is the one that passed while the
// payloads were all still there.
const survivors = retired.filter(
  (n) =>
    fs.existsSync(path.join(mirror, `${n}.json`)) ||
    fs.existsSync(path.join(tarballRoot, n)),
)
if (survivors.length) {
  console.error(`FATAL: still present after pruning: ${survivors.join(' ')}`)
  process.exit(1)
}
