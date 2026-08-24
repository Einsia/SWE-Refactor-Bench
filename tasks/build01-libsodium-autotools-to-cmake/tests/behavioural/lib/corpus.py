#!/usr/bin/env python3
"""Behavioural verification from the verifier's own pristine test corpus.

The agent's `test/` directory is never trusted. This module unpacks the frozen
corpus (80 .c programs, 80 .exp goldens, cmptest.h, quirks.h) shipped inside
the suite's own data/corpus.tar.gz, compiles each program against the library the
agent's build *installed*, runs it in a private directory, and checks both the
exit status and the byte-exact hash of the .res file it produced.

Each program writes "<name>.res" into its cwd and compares it against
"$TEST_SRCDIR/<name>.exp" itself, exiting 99 on any mismatch. We additionally
hash the .res to catch a library that somehow satisfies the program's own
comparison but produces different bytes.
"""
import hashlib
import json
import os
import shutil
import subprocess
import tarfile

from swerefactor.contract import submission_env

SUITE_DIR = os.environ.get("SRB_SUITE_DIR", "/tests/behavioural")
WORK = os.environ.get("SRB_SUITE_WORK") or os.environ.get("SRB_WORK", "/tmp/srb-work")
CORPUS_TGZ = os.path.join(SUITE_DIR, "data", "corpus.tar.gz")
CORPUS_SHA = "89560958f40c2b86d3b12602b903ecbabb558ec63ce26d01d94c8387a74ea90f"

_corpus_root = None
_verified = False


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def corpus_root():
    """Unpack the corpus once into a verifier-private location.

    The checksum is re-verified on every call, not just on the first unpack, so
    that a tampered archive can never be silently reused.
    """
    global _corpus_root, _verified
    actual = sha256_file(CORPUS_TGZ)
    if actual != CORPUS_SHA:
        _verified = False
        raise RuntimeError("test corpus checksum mismatch: %s" % actual)
    _verified = True
    if _corpus_root:
        return _corpus_root
    dest = os.path.join(WORK, "_corpus")
    if not os.path.isdir(dest):
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(CORPUS_TGZ, "r:gz") as tf:
            tf.extractall(dest)
        os.chmod(dest, 0o755)
    _corpus_root = dest
    return dest


def corpus_verified():
    return _verified


def corpus_paths():
    """(default_dir, quirks_dir) inside the unpacked corpus."""
    root = corpus_root()
    for base, _dirs, files in os.walk(root):
        if "cmptest.h" in files:
            default = base
            break
    else:
        raise RuntimeError("cmptest.h not found in corpus")
    quirks = None
    for base, _dirs, files in os.walk(root):
        if "quirks.h" in files:
            quirks = base
            break
    if quirks is None:
        raise RuntimeError("quirks.h not found in corpus")
    return default, quirks


def load_tests_json():
    with open(os.path.join(SUITE_DIR, "data", "tests.json")) as fh:
        return json.load(fh)


class CorpusRunner:
    """Compiles and runs the corpus against one installed prefix."""

    def __init__(self, build, linkage="shared", cc="cc"):
        self.build = build
        self.linkage = linkage
        self.cc = cc
        self.default, self.quirks = corpus_paths()
        self.verified = corpus_verified()
        self.root = os.path.join(WORK, "_corpusrun", "%s-%s" % (build.name, linkage))
        os.makedirs(self.root, exist_ok=True)
        self._results = {}

    # ------------------------------------------------------------------ env --
    def include_dir(self):
        return os.path.join(self.build.prefix, "include")

    def run_env(self):
        env = submission_env()
        libdir = self.build.libdir()
        if self.linkage == "shared":
            prev = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = libdir + ((":" + prev) if prev else "")
        return env

    def compile_argv(self, name, src, out_bin):
        inc = self.include_dir()
        argv = [self.cc, "-O1", "-w",
                '-DTEST_SRCDIR="%s"' % self.default,
                "-I" + inc, "-I" + os.path.join(inc, "sodium"),
                "-I" + self.default, "-I" + self.quirks,
                src, "-o", out_bin]
        libdir = self.build.libdir()
        if self.linkage == "static":
            static = self.build.static_lib()
            argv += [static or os.path.join(libdir, "libsodium.a")]
        else:
            argv += ["-L" + libdir, "-lsodium",
                     "-Wl,-rpath," + libdir]
        argv += ["-lpthread"]
        return argv

    # ------------------------------------------------------------------ run --
    def run_one(self, name, timeout=300):
        """Returns dict(compiled, rc, res_sha, res_bytes, output)."""
        if name in self._results:
            return self._results[name]
        src = os.path.join(self.default, name + ".c")
        rundir = os.path.join(self.root, name)
        shutil.rmtree(rundir, ignore_errors=True)
        os.makedirs(rundir, exist_ok=True)
        out_bin = os.path.join(rundir, name)
        res = {"compiled": False, "rc": None, "res_sha": None,
               "res_bytes": None, "output": "", "name": name}

        if not os.path.isfile(src):
            res["output"] = "corpus source missing: %s" % src
            self._results[name] = res
            return res

        argv = self.compile_argv(name, src, out_bin)
        try:
            p = subprocess.run(argv, cwd=rundir, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout,
                               env=self.run_env())
            res["output"] = p.stdout.decode("utf-8", "replace")
            res["compiled"] = p.returncode == 0 and os.path.isfile(out_bin)
        except (OSError, subprocess.TimeoutExpired) as exc:
            res["output"] = "compile error: %s" % exc
            self._results[name] = res
            return res
        if not res["compiled"]:
            self._results[name] = res
            return res

        try:
            r = subprocess.run([out_bin], cwd=rundir, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=timeout,
                               env=self.run_env())
            res["rc"] = r.returncode
            res["output"] += r.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            res["rc"] = 124
            res["output"] += "\n*** TIMEOUT ***"
        except OSError as exc:
            res["rc"] = 127
            res["output"] += "\nrun error: %s" % exc

        resfile = os.path.join(rundir, name + ".res")
        if os.path.isfile(resfile):
            res["res_sha"] = sha256_file(resfile)
            res["res_bytes"] = os.path.getsize(resfile)
        self._results[name] = res
        return res

    def run_all(self, names, jobs=None):
        """Compile+run every named program concurrently. Returns {name: result}."""
        from concurrent.futures import ThreadPoolExecutor
        if jobs is None:
            try:
                jobs = max(1, min(len(os.sched_getaffinity(0)), 12))
            except AttributeError:
                jobs = max(1, min(os.cpu_count() or 1, 12))
        pending = [n for n in names if n not in self._results]
        if pending:
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(self.run_one, pending))
        return {n: self._results.get(n) for n in names}
