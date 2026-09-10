#!/usr/bin/env python3
"""Populate a Maven-layout local repository from a pinned coordinate manifest.

Used by the verifier image, which ships no Maven on purpose. Every coordinate in
the manifest is fully pinned (group:artifact:version:packaging[:classifier]), so
laying the files out by hand is equivalent to what `mvn dependency:get` would do
and needs no Maven.

Runs at image-build time only.
"""
import argparse
import concurrent.futures as futures
import os
import sys
import urllib.error
import urllib.request

CENTRAL = 'https://repo1.maven.org/maven2'
# Nothing the repository under test produces may be fetched into the closure.  A
# prebuilt gson 2.10.1 in ~/.m2/repository is resolvable, the Gradle init script
# gives every project mavenLocal() as its only repository, and a build that never
# compiled gson would then resolve one and look like it had.
#
# Matched on the group directory and the version rather than on artifact ids.  The
# group plus the version cannot be got wrong that way -- gson 2.8.5 and 2.8.7 are
# legitimate plugin dependencies and stay fetchable, and every 2.10.1 coordinate
# under the group is refused whatever it is called.
BANNED_GROUP = 'com/google/code/gson/'
BANNED_VERSION = '2.10.1'


def banned(rel):
    """True if `rel` is an artifact of the repository under test."""
    if not rel.startswith(BANNED_GROUP):
        return False
    parts = rel.split('/')
    return len(parts) > 2 and parts[-2] == BANNED_VERSION


def split(line):
    parts = line.split(':')
    if len(parts) == 4:
        group, artifact, version, ext = parts
        classifier = ''
    elif len(parts) == 5:
        group, artifact, version, ext, classifier = parts
    else:
        raise ValueError('bad coordinate: %r' % line)
    return group, artifact, version, ext, classifier


def rel_path(group, artifact, version, ext, classifier=''):
    name = '%s-%s' % (artifact, version)
    if classifier:
        name += '-' + classifier
    name += '.' + ext
    return '%s/%s/%s/%s' % (group.replace('.', '/'), artifact, version, name)


def parse(line):
    return rel_path(*split(line))


def expand(coords):
    """Coordinates -> repository paths, adding the POM that Maven implies.

    `mvn dependency:get` writes the artifact *and* its POM, so a repository
    populated by Maven holds both. The manifest lists one line per artifact, so
    fetching it literally would leave a repository with jars and no module
    metadata — which Maven tolerates and Gradle does not: Gradle refuses to
    resolve a module whose POM is absent, and a plugin marker is resolved from
    its POM alone.

    Deriving the POM here, rather than listing it in the manifest, keeps the two
    images' repositories identical no matter which downloader built them.
    """
    rels, seen = [], set()

    def add(rel):
        if rel not in seen:
            seen.add(rel)
            rels.append(rel)

    for coord in coords:
        group, artifact, version, ext, classifier = split(coord)
        add(rel_path(group, artifact, version, ext, classifier))
        if ext != 'pom':
            add(rel_path(group, artifact, version, 'pom'))
    return rels


def fetch(rel, repo, retries=4):
    dest = os.path.join(repo, rel)
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    url = '%s/%s' % (CENTRAL, rel)
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                data = resp.read()
            if not data:
                raise IOError('empty body')
            tmp = dest + '.part'
            with open(tmp, 'wb') as fh:
                fh.write(data)
            os.replace(tmp, dest)
            return None
        except (urllib.error.URLError, IOError, OSError) as exc:
            last = exc
    return '%s  (%s)' % (rel, last)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--repo', required=True)
    ap.add_argument('--jobs', type=int, default=12)
    args = ap.parse_args()

    with open(args.manifest) as fh:
        coords = [ln.strip() for ln in fh
                  if ln.strip() and not ln.lstrip().startswith('#')]
    rels = expand(coords)
    for rel in rels:
        if banned(rel):
            sys.exit('FATAL: manifest lists a migration artifact: %s' % rel)

    print('fetch_deps: %d coordinates -> %d files -> %s'
          % (len(coords), len(rels), args.repo))
    errors = []
    with futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for err in pool.map(lambda r: fetch(r, args.repo), rels):
            if err:
                errors.append(err)
    if errors:
        for e in errors[:20]:
            print('  FAILED %s' % e, file=sys.stderr)
        sys.exit('fetch_deps: %d artifact(s) failed' % len(errors))

    missing = [r for r in rels
               if not os.path.isfile(os.path.join(args.repo, r))]
    if missing:
        sys.exit('fetch_deps: %d artifact(s) missing after fetch' % len(missing))

    # Walked rather than checked path by path: this catches a 2.10.1 artifact of
    # the repository under test however it got here, including one a base image
    # shipped or an earlier layer left behind, not only one this manifest asked
    # for.
    group_root = os.path.join(args.repo, BANNED_GROUP.rstrip('/'))
    for dirpath, _dirnames, filenames in os.walk(group_root):
        if os.path.basename(dirpath) == BANNED_VERSION and filenames:
            sys.exit('FATAL: repository contains %s (%d files)'
                     % (os.path.relpath(dirpath, args.repo), len(filenames)))

    jars = sum(1 for r in rels if r.endswith('.jar'))
    poms = sum(1 for r in rels if r.endswith('.pom'))
    print('fetch_deps: ok — %d files (%d jars, %d poms)'
          % (len(rels), jars, poms))
    return 0


if __name__ == '__main__':
    sys.exit(main())
