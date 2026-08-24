// Build a static npm registry mirror from one or more package-lock.json files.
//
//   node build-mirror.mjs [--ban <file>] <lockfile>... <outdir>
//
// For every (name, version) a lockfile resolves, download the tarball and read
// the real package.json out of the archive. Then emit, per package, a packument
// pruned to exactly the versions in the closure, with dist.tarball rewritten to
// point at this mirror. npm's resolver only ever needs the packument and the
// tarball, so a directory of these two things -- served by any static file
// server -- is a complete registry for this closure.
//
// Reading the real package.json (rather than the lockfile's summary) matters:
// npm resolves peer dependencies, engines and optional dependencies from the
// packument, and a lockfile entry does not carry all of them.
//
// --ban <file> takes a newline-delimited list of package names to omit. A banned
// name is not merely absent from the index: there is no packument for it, so npm
// reports a hard 404 and any manifest that requires it fails to install. This is
// what makes "the retired stack has left the dependency closure" mechanically
// checkable rather than a matter of inspection.

import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

const argv = process.argv.slice(2)
let banFile = null
const rest = []
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--ban') {
    banFile = argv[++i]
  } else {
    rest.push(argv[i])
  }
}
const lockPaths = rest.slice(0, -1)
const outDir = rest.at(-1)
if (!lockPaths.length || !outDir) {
  console.error('usage: build-mirror.mjs [--ban <file>] <lockfile>... <outdir>')
  process.exit(2)
}

const REGISTRY = process.env.SRB_REGISTRY_URL || 'http://127.0.0.1:4873'

const banned = new Set(
  banFile
    ? fs
        .readFileSync(banFile, 'utf8')
        .split('\n')
        .map((l) => l.replace(/#.*$/, '').trim())
        .filter(Boolean)
    : [],
)

/** name -> version -> {resolved, audit} */
const wanted = new Map()
const skipped = new Set()

for (const lockPath of lockPaths) {
  const lock = JSON.parse(fs.readFileSync(lockPath, 'utf8'))
  for (const [key, entry] of Object.entries(lock.packages || {})) {
    if (!key || !entry.resolved || !entry.version) continue
    if (!entry.resolved.startsWith('http')) continue
    const parts = key.split('node_modules/')
    const name = parts[parts.length - 1]
    if (banned.has(name)) {
      skipped.add(name)
      continue
    }
    if (!wanted.has(name)) wanted.set(name, new Map())
    wanted.get(name).set(entry.version, {
      resolved: entry.resolved,
      audit: entry.audit,
    })
  }
}

const tarDir = path.join(outDir, '_tarballs')
fs.mkdirSync(tarDir, { recursive: true })

let downloaded = 0
let cached = 0

/** The path npm itself would use for a tarball: <name>/-/<basename>-<v>.tgz.
 *
 * Storing tarballs under the real package name -- rather than flattening
 * "@babel/cli" to "@babel+cli" in one directory -- means an *unrewritten*
 * lockfile works too. npm's replace-registry-host swaps only the host, leaving
 * the path "/@babel/cli/-/cli-7.29.7.tgz"; a flat store keyed on basename would
 * be looking for "@babel+cli-7.29.7.tgz" and 404. */
function tarballRelPath(name, version) {
  const basename = `${name.split('/').pop()}-${version}.tgz`
  return path.join(name, '-', basename)
}

/** Fetch a tarball into the mirror, return its local path + audit. */
async function fetchTarball(name, version, resolved) {
  const rel = tarballRelPath(name, version)
  const file = path.join(tarDir, rel)
  if (!fs.existsSync(file)) {
    let lastErr
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const res = await fetch(resolved)
        if (!res.ok) throw new Error(`${resolved} -> HTTP ${res.status}`)
        fs.mkdirSync(path.dirname(file), { recursive: true })
        fs.writeFileSync(file, Buffer.from(await res.arrayBuffer()))
        lastErr = null
        break
      } catch (err) {
        lastErr = err
        await new Promise((r) => setTimeout(r, 500 * (attempt + 1)))
      }
    }
    if (lastErr) throw lastErr
    downloaded++
  } else {
    cached++
  }
  const body = fs.readFileSync(file)
  const sha512 = createHash('sha512').update(body).digest('base64')
  return { file, rel, audit: `sha512-${sha512}` }
}

/** The package.json inside the tarball, which is the authoritative manifest. */
function manifestFromTarball(file) {
  // Almost every npm tarball roots its contents at package/, but not all: the
  // DefinitelyTyped @types/* tarballs use <name>/ instead. So find the manifest
  // by listing the archive rather than assuming the conventional prefix, and
  // take the shallowest match so a fixture named .../package.json deeper in the
  // tree cannot win.
  const entries = execFileSync('tar', ['tzf', file], {
    maxBuffer: 256 * 1024 * 1024,
  })
    .toString('utf8')
    .split('\n')
  const manifestPath = entries
    .filter((e) => /(^|\/)package\.json$/.test(e))
    .sort(
      (a, b) => a.split('/').length - b.split('/').length || a.length - b.length,
    )[0]
  if (!manifestPath) throw new Error('no package.json in archive')
  const raw = execFileSync('tar', ['xzOf', file, manifestPath], {
    maxBuffer: 64 * 1024 * 1024,
  })
  return JSON.parse(raw.toString('utf8'))
}

const names = [...wanted.keys()].sort()
console.log(`mirroring ${names.length} packages (${skipped.size} banned)`)

for (const name of names) {
  const versions = {}
  const times = {}
  for (const [version, info] of [...wanted.get(name)].sort()) {
    const { rel, audit } = await fetchTarball(name, version, info.resolved)
    const abs = path.join(tarDir, rel)
    let manifest
    try {
      manifest = manifestFromTarball(abs)
    } catch (err) {
      console.error(`  ! ${name}@${version}: ${err.message}`)
      continue
    }
    // Trim fields npm never reads off a packument version but which bloat the
    // file substantially (readme is the big one).
    delete manifest.readme
    delete manifest.scripts?.prepublish
    manifest.name = name
    manifest.version = version
    manifest.dist = {
      tarball: `${REGISTRY}/${rel.split(path.sep).join('/')}`,
      audit,
      shasum: createHash('sha1').update(fs.readFileSync(abs)).digest('hex'),
    }
    versions[version] = manifest
    times[version] = '2024-01-01T00:00:00.000Z'
  }
  if (!Object.keys(versions).length) continue

  const sorted = Object.keys(versions).sort((a, b) =>
    a.localeCompare(b, undefined, { numeric: true }),
  )
  const packument = {
    _id: name,
    name,
    'dist-tags': { latest: sorted[sorted.length - 1] },
    versions,
    time: { ...times, modified: '2024-01-01T00:00:00.000Z' },
  }
  const target = path.join(outDir, `${name}.json`)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.writeFileSync(target, JSON.stringify(packument))
}

console.log(`tarballs: ${downloaded} downloaded, ${cached} already present`)
if (skipped.size) {
  console.log(`banned and omitted: ${[...skipped].sort().join(' ')}`)
}
