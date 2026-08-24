// Serve a static npm registry mirror on 127.0.0.1:4873.
//
// Two routes are all npm needs:
//   GET /<name>                      -> the packument
//   GET /<name>/-/<file>.tgz         -> the tarball
//
// Loopback only, read-only, no external network involved.

import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'

const ROOT = process.argv[2] || '/opt/npm-registry'
const PORT = Number(process.env.SRB_REGISTRY_PORT || 4873)

const server = http.createServer((req, res) => {
  let pathname
  try {
    pathname = decodeURIComponent(new URL(req.url, 'http://x').pathname)
  } catch {
    res.writeHead(400).end('bad request')
    return
  }

  // Tarball: /<name>/-/<file>.tgz  (name may be scoped: /@scope/pkg/-/...)
  //
  // Tarballs are stored under their real package path, so this serves both the
  // mirror's own dist.tarball URLs and an unrewritten registry.npmjs.org path
  // whose host npm has swapped for ours.
  const tar = pathname.match(/^\/(.+)\/-\/([^/]+\.tgz)$/)
  if (tar) {
    const rel = path.join(tar[1], '-', path.basename(tar[2]))
    const file = path.resolve(ROOT, '_tarballs', rel)
    if (!file.startsWith(path.resolve(ROOT, '_tarballs') + path.sep)
        || !fs.existsSync(file)) {
      res.writeHead(404, { 'content-type': 'application/json' })
      res.end('{"error":"Not found"}')
      return
    }
    res.writeHead(200, {
      'content-type': 'application/octet-stream',
      'content-length': fs.statSync(file).size,
    })
    fs.createReadStream(file).pipe(res)
    return
  }

  // Packument: /<name>
  const name = pathname.replace(/^\//, '').replace(/\/$/, '')
  // Reject traversal before touching the filesystem.
  if (!name || name.includes('..')) {
    res.writeHead(404, { 'content-type': 'application/json' })
    res.end('{"error":"Not found"}')
    return
  }
  const file = path.join(ROOT, `${name}.json`)
  if (!file.startsWith(path.resolve(ROOT)) || !fs.existsSync(file)) {
    res.writeHead(404, { 'content-type': 'application/json' })
    res.end(`{"error":"Not found","name":${JSON.stringify(name)}}`)
    return
  }
  res.writeHead(200, {
    'content-type': 'application/json',
    'content-length': fs.statSync(file).size,
  })
  fs.createReadStream(file).pipe(res)
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(`registry mirror: http://127.0.0.1:${PORT} <- ${ROOT}`)
})
