#!/usr/bin/env python3
"""ELF / archive / symbol inspection used by the behavioural suites."""
import os
import re
import subprocess

from swerefactor.contract import submission_env

_cache = {}


def _run(argv):
    key = tuple(argv)
    if key in _cache:
        return _cache[key]
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=300)
        out = p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        out = "ERROR: %s" % exc
    _cache[key] = out
    return out


def objdump_p(path):
    return _run(["objdump", "-p", path])


def soname(path):
    m = re.search(r"^\s*SONAME\s+(\S+)\s*$", objdump_p(path), re.M)
    return m.group(1) if m else None


def needed(path):
    return sorted(set(re.findall(r"^\s*NEEDED\s+(\S+)\s*$", objdump_p(path), re.M)))


def has_textrel(path):
    txt = objdump_p(path)
    return bool(re.search(r"^\s*TEXTREL\b", txt, re.M))


def dynamic_exports(path):
    """Exported (defined, global/weak) dynamic symbol names of a shared object."""
    out = _run(["nm", "-D", "--defined-only", "--format=posix", path])
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", parts[0]):
            if parts[1] in ("T", "W", "D", "B", "R", "V", "G", "S", "i"):
                names.add(parts[0])
    return names


def dynamic_undefined(path):
    """Undefined dynamic symbols, with any @GLIBC_x.y version stripped.

    The frozen State-A set is unversioned; the version tag is a property of the
    libc the object was linked against, not of what the library needs.
    """
    out = _run(["nm", "-D", "--undefined-only", "--format=posix", path])
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] in ("U", "w", "v"):
            names.add(parts[0].split("@")[0])
    return names


def program_headers(path):
    return _run(["readelf", "-lW", path])


def has_gnu_relro(path):
    """True when the linker emitted a GNU_RELRO segment (-Wl,-z,relro)."""
    return bool(re.search(r"^\s*GNU_RELRO\b", program_headers(path), re.M))


def has_bind_now(path):
    """True for full RELRO: DT_BIND_NOW or DF_BIND_NOW in DT_FLAGS (-z now)."""
    txt = _run(["readelf", "-dW", path])
    if re.search(r"\(BIND_NOW\)", txt):
        return True
    for m in re.finditer(r"\(FLAGS(?:_1)?\)\s+(.*)$", txt, re.M):
        if "BIND_NOW" in m.group(1) or "NOW" in m.group(1).split():
            return True
    return False


def stack_flags(path):
    """The GNU_STACK segment's permission string, or None when absent.

    `readelf -lW` prints one segment per line as
    `Type Offset VirtAddr PhysAddr FileSiz MemSiz Flg Align`, so the flags are
    the second-to-last field.
    """
    for line in program_headers(path).splitlines():
        parts = line.split()
        if parts[:1] == ["GNU_STACK"] and len(parts) >= 8:
            return parts[-2]
    return None


def stack_is_executable(path):
    """True when the GNU_STACK segment is executable (-z noexecstack lost)."""
    fl = stack_flags(path)
    return bool(fl) and "E" in fl


def archive_members(path):
    out = _run(["ar", "t", path])
    return sorted(m.strip() for m in out.splitlines() if m.strip())


def archive_defined_symbols(path):
    """Global defined symbols in a static archive."""
    out = _run(["nm", "--defined-only", "--format=posix", path])
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", parts[0]):
            if parts[1] in ("T", "W", "D", "B", "R", "V", "G", "S", "i"):
                names.add(parts[0])
    return names


def is_pic_shared(path):
    txt = _run(["file", "-b", path])
    return "shared object" in txt


def file_type(path):
    return _run(["file", "-b", path]).strip()


def readelf_dyn_version(path):
    """Version definitions (from a version script), if any."""
    out = _run(["readelf", "-VW", path])
    return sorted(set(re.findall(r"Name:\s*(\S+)", out)))


def strtab_contains(path, needle):
    out = _run(["strings", "-a", path])
    return needle in out


def link_consumer(cc, src, out_bin, cflags, libs, cwd=None, extra_env=None):
    """Compile+link a consumer program. Returns (rc, output)."""
    argv = [cc, src, "-o", out_bin] + list(cflags) + list(libs)
    env = submission_env()
    if extra_env:
        env.update(extra_env)
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=300)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "ERROR: %s" % exc


def run_binary(path, cwd=None, env=None, timeout=120, args=()):
    e = submission_env()
    if env:
        e.update(env)
    try:
        p = subprocess.run([path] + list(args), cwd=cwd, env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "ERROR: %s" % exc
