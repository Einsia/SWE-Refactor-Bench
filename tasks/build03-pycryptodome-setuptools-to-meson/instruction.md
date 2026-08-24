# Migrate pycryptodome from setuptools/distutils to Meson

You are working in `/workspace/repo`, a frozen snapshot of **pycryptodome 3.20.0**.

Its build system is a hand-written `setup.py` driving `distutils`/`setuptools`.
`distutils` was removed from the standard library in Python 3.12, and the
project's compile-time feature detection is a bespoke module (`compiler_opt.py`)
that shells out to the compiler, mutates a list of `Extension` objects, and
deletes entries from it when the compiler turns out to lack an instruction set.

Your job is to **replace that build system with Meson**, driven through the
`meson-python` PEP 517 backend, so that the project builds and installs on a
Python where `distutils` and `setuptools` do not exist.

This is a build-system migration, not a feature change. The library's behaviour,
its public API, and its installed layout must come out the other side
bit-for-bit equivalent. **Do not modify anything under `src/` or `lib/`** — not
the C, not the Python, not the type stubs, not the test suite. The migration lives
entirely in build files, and §3 and §4 say exactly which files are which.

---

## 1. What State A looks like

Facts you can rely on. All of them were measured on this exact snapshot,
built on this exact image.

**Build entry points**

| File | Role |
| --- | --- |
| `setup.py` | 543 lines; declares 41 `Extension(...)` objects, all with `py_limited_api=True` |
| `compiler_opt.py` | 406 lines; 11 compile probes, plus `remove_extension()` |
| `setup.cfg` | `[bdist_wheel] py-limited-api = cp35`, `[metadata] project_urls`, plus `[flake8]` |
| `MANIFEST.in` | sdist file selection |
| `pyproject.toml` | `requires = ["setuptools"]`, `build-backend = "setuptools.build_meta"` |

**Native artifacts.** 41 shared libraries built from 47 translation units. 38 are
one `.c` file each; these three are the rest:

```
Crypto.Math._modexp        src/modexp.c src/mont2.c
Crypto.PublicKey._ec_ws    src/ec_ws.c src/mont.c src/p256_table.c
                           src/p384_table.c src/p521_table.c
Crypto.PublicKey._ed448    src/ed448.c src/mont1.c
```

They install next to the Python modules that load them, under
`Crypto/{Cipher,Hash,Math,Protocol,PublicKey,Util}/`, named `<module>.abi3.so`.

**These are not CPython extension modules.** Read
`lib/Crypto/Util/_raw_api.py:293`. They are plain C shared libraries opened with
`ctypes`, and found by walking `importlib.machinery.EXTENSION_SUFFIXES`:

```python
def load_pycryptodome_raw_lib(name, cdecl):
    for ext in extension_suffixes:
        try:
            filename = basename + ext
            full_name = pycryptodome_filename(dir_comps, filename)
            ...
            return load_lib(full_name, cdecl)
```

Consequences, all verified on the State A build:

- Not one of the 41 defines `PyInit_*`. `FAKE_INIT` (`src/common.h:119`) expands
  to nothing outside Windows.
- Not one of them links `libpython`. `NEEDED` is exactly `libc.so.6`.
- `Python.h` is only reached under `_MSC_VER` / `__MINGW32__`.
- `py_limited_api=True` is used **solely** to get the `.abi3.so` filename, which
  is what makes the `ctypes` search above find them.
- Between them they export **277 symbols** through `.dynsym`, summed per object —
  217 distinct names, 29 of which are arithmetic helpers statically linked into
  several objects at once. E.g. `_raw_aes.abi3.so` → `AES_start_operation`,
  `AES_stop_operation`; `_cpuid_c.abi3.so` → `have_aes_ni`, `have_clmul`.
  `EXPORT_SYM` (`src/common.h:138`) is **empty** outside Windows, so every one
  of those symbols depends on default ELF visibility. Nothing in State A's build
  passes `-fvisibility=hidden`; a build system that does it by default produces
  41 files of the right size that export nothing.

**Compile flags.** Ten macros are defined on all 47 units:

```
HAVE_CPUID_H  HAVE_POSIX_MEMALIGN  HAVE_STDINT_H  HAVE_UINT128
HAVE_X86INTRIN_H  LTC_NO_ASM  NDEBUG  PYCRYPTO_LITTLE_ENDIAN
SYS_BITS=64  USE_SSE2
```

Two are module-scoped, on `_ghash_clmul` only:
`HAVE_WMMINTRIN_H`, `HAVE_TMMINTRIN_H`.

`-msse2` is on all 47 units. Two units get elevated ISA:

| Unit | Extra flags |
| --- | --- |
| `src/AESNI.c` → `_raw_aesni` | `-maes` |
| `src/ghash_clmul.c` → `_ghash_clmul` | `-mpclmul -mssse3` |

**Probes decide the artifact set, not just the flags.** When the compiler
rejects `-maes` and `-mpclmul -mssse3`, `compiler_opt.py` calls
`remove_extension()` and the two modules **are not built and do not ship**:
the wheel goes from 41 `.so` / 336 members to 39 `.so` / 334 members. This was
measured, not inferred.

**Wheel.** `pip wheel .` produces `pycryptodome-3.20.0-cp35-abi3-linux_x86_64.whl`
with 336 members: a 330-file payload — 41 `.so`, 192 `.py`, 96 `.pyi`, 1
`py.typed` — plus six in `.dist-info/`. Three of those six (`LICENSE.rst`,
`AUTHORS.rst`, `top_level.txt`) are setuptools-only and §5 does not ask for
them, so the same payload under Meson is a 333-member wheel.

**The library's own test suite.** `Crypto.SelfTest` collects 42,784 test
instances over 39,245 distinct test ids. All pass.

---

## 2. What State B must contain

### 2.1 A Meson build, driven by meson-python

`pyproject.toml` must declare:

```toml
[build-system]
build-backend = "mesonpy"
requires = ["meson-python>=0.15.0", "meson>=1.3.0"]
```

`meson.build` at the repository root, and

```
python -m build --wheel --no-isolation
```

must succeed **without `setuptools` and without `distutils` in the build closure**.
That is the exact command every stage uses. The grading image does have setuptools
installed — it has to, because State A is the reference every measurement is
compared against and State A is a setuptools build — so a build that still reaches
for it will not fail with an import error. It fails as §3, by reading, and that is
the whole result. `--no-isolation` because there is no network to build an isolated
environment from, so everything named in `[build-system] requires` has to be
satisfied by what is already installed — check that what you pin is what is there.

Project metadata moves to PEP 621 static metadata in `[project]`. The version
must keep coming from `lib/Crypto/__init__.py` — that file is the single source
of truth and you must not edit it, so read it from the build.

### 2.2 The 41 native artifacts, at the right paths, with the right names

Every one of these must exist in the wheel and in the installed tree:

```
Crypto/Cipher/    _ARC4 _Salsa20 _chacha20 _pkcs1_decode _raw_aes _raw_aesni
                  _raw_arc2 _raw_blowfish _raw_cast _raw_cbc _raw_cfb _raw_ctr
                  _raw_des _raw_des3 _raw_ecb _raw_eksblowfish _raw_ocb _raw_ofb
Crypto/Hash/      _BLAKE2b _BLAKE2s _MD2 _MD4 _MD5 _RIPEMD160 _SHA1 _SHA224
                  _SHA256 _SHA384 _SHA512 _ghash_clmul _ghash_portable _keccak
                  _poly1305
Crypto/Math/      _modexp
Crypto/Protocol/  _scrypt
Crypto/PublicKey/ _ec_ws _ed25519 _ed448 _x25519
Crypto/Util/      _cpuid_c _strxor
```

each as `<name>.abi3.so`. The `.abi3` infix is load-bearing: it is how
`load_pycryptodome_raw_lib()` finds the file. A file named `_raw_aes.so` or
`_raw_aes.cpython-312-x86_64-linux-gnu.so` will not be found.

They must remain plain shared libraries: **no `PyInit_*`, no `libpython` in
`NEEDED`**, and all 277 symbols still visible in `.dynsym`. Meson's Python
module has an opinion here that differs from distutils' — check the built
artifact with `nm -D --defined-only`, do not assume.

### 2.3 Real compile-time probing

Every one of `compiler_opt.py`'s decisions must be reproduced by Meson's own
probing (`cc.has_header`, `cc.compiles`, `cc.has_function`,
`cc.has_multi_arguments`, …). Reproduce the behaviour, not the numbers:

- The ten global macros must be derived, in the same order of preference. For
  SSE2 that means trying `intrin.h`, then `x86intrin.h` with `-msse2`, then
  `xmmintrin.h`+`emmintrin.h` with `-msse2`, and taking the macro from whichever
  succeeds. For aligned allocation, `posix_memalign` before `memalign`.
- `-msse2` globally; `-maes` on `src/AESNI.c` alone; `-mpclmul -mssse3` plus
  `HAVE_WMMINTRIN_H` and `HAVE_TMMINTRIN_H` on `src/ghash_clmul.c` alone.
- **If the compiler cannot do AES-NI, `_raw_aesni` must not be built or
  shipped. Same for CLMUL and `_ghash_clmul`.** The build must still succeed.
  You will be graded with a compiler wrapper that rejects those flags.

Hardcoding the macro list is the failure mode this is designed to catch.

### 2.4 The full Python payload

192 `.py`, 96 `.pyi` and `Crypto/py.typed`, across 20 packages, installed to the
same paths as State A. Meson cannot glob; enumerate them.

### 2.5 Metadata parity

The wheel's `METADATA` must keep these fields, with these values:

| Field | Value |
| --- | --- |
| `Name` | `pycryptodome` |
| `Version` | `3.20.0` |
| `Summary` | `Cryptographic library for Python` |
| `License` | `BSD, Public Domain` |
| `Requires-Python` | `>=2.7, !=3.0.*, !=3.1.*, !=3.2.*, !=3.3.*, !=3.4.*` (compared semantically) |
| `Classifier` | all 20, as a set |
| `Project-URL` | must include the `Source` and `Changelog` URLs from `setup.cfg` |

`Author`/`Author-email` may be merged into PEP 621's single
`Author-email: Helder Eijs <helderijs@gmail.com>` form; both name and address
must survive. `Home-page` may move into `Project-URL: Homepage, …`.

### 2.6 A stable-ABI wheel

The wheel's ABI tag must be `abi3`, and its `WHEEL` file must declare exactly one
tag whose ABI component is `abi3`.

This follows from §1: not one of the 41 objects touches the Python C API, so a
version-specific ABI tag would claim a constraint that does not exist — one build
serves every CPython from 3.5 up, and a wheel tagged `cp312-cp312` installs on
one minor version for no benefit while every other interpreter falls back to
building from source. State A gets this from `setup.cfg`'s
`[bdist_wheel] py-limited-api = cp35`, which goes away with the old build system,
so the new one has to ask for it. The backend supports it; the setting is in the
backend's own source, which is installed in your image.

The **interpreter** component is free — see §5.

The platform component must stay specific. A wheel containing 41 shared objects
must not be tagged `any`.

---

## 3. What must no longer exist

The old build system must be gone from the source and dependency closure, not
merely bypassed.

- `setup.py`, `compiler_opt.py` and `MANIFEST.in` must be **deleted**.
- No file in the repository may import `distutils`, `setuptools`,
  `pkg_resources`, `numpy.distutils`, or `Cython.Distutils`.
- `setuptools`, `distutils`, `wheel` and `pkg_resources` must not appear in
  `[build-system] requires`, and `build-backend` must not be a setuptools
  backend.
- No build file may shell out to the old path — no `python setup.py`, no
  `bdist_wheel` invocation, no `pip install` of setuptools, no vendored copy of
  `distutils` or `compiler_opt.py` under another name.
- `setup.cfg` may stay for its `[flake8]` section, but its setuptools build
  sections (`[bdist_wheel]`, `[metadata]`, `[build_sphinx]`, `[egg_info]`) must
  go.

---

## 4. What must not change

Checksummed. Any edit to these fails the audit gate outright.

583 files are checksummed against State A:

- **All of `src/`** — every `.c` and `.h`, including `src/test/CMakeLists.txt`
  (a pre-existing C-level test harness, unrelated to packaging; leave it alone).
- **All of `lib/`** — every `.py`, `.pyi`, and `py.typed`, which includes the
  entire `Crypto.SelfTest` tree. In particular `lib/Crypto/Util/_raw_api.py`,
  which loads the 41 objects, and `lib/Crypto/__init__.py`, which holds the
  version.
- **All of `Doc/`.**
- `README.rst`, `LICENSE.rst`, `AUTHORS.rst`, `Changelog.rst`,
  `FuturePlans.rst`, `INSTALL.rst`.

Adding a file to `src/` or `lib/` counts too: the contract is that the thing being
built is what State A shipped, so a new C source compiled into an artifact, or a
`.py` that shadows one of the 192, is the same finding as an edit.

You are migrating the build. You are not fixing, reformatting, or improving the
library.

---

## 5. How the result is judged

Three things happen to the repository you leave behind, in order.

First it is **read**. State A's build system and yours are compared to decide
whether the migration described above actually happened — whether setuptools and
`distutils` left rather than moved, whether Meson describes the build rather than
driving something else, whether the eleven toolchain questions are asked rather
than remembered, whether the 41 objects are declared as the ctypes libraries they
are, and whether the C and Python sources came through untouched. Nothing is
built at this stage; it is a reading of what you wrote. **If this stage decides
the migration did not happen, that is the whole result** — however well the
library works.

Then it is **built**. A wheel is produced from your sources five times over:
twice with a full compiler, from the same sources into separate trees, and three
more times against compilers that reject `-maes`, that reject `-mpclmul -mssse3`,
and that reject all three. Each is installed into its own tree and measured
against State A — the
artifact set, the exported symbols, the macro set and ISA flags in the delivered
objects, the 330-file payload, the wheel and its `RECORD`, the distribution
metadata, and pycryptodome's own 39,245 test ids run against the installed
library. This stage carries no partial credit: a migration that gets the build
right and the metadata wrong pays exactly what one that got neither pays, and the
run ends there. The report still names the area, so you can tell the two apart —
the score cannot.

Then it is **attacked**. Independent attempts are made to find something the
setuptools build does correctly and yours does not — given both trees, both
builds, and a working compiler for either. An attempt has to produce a test that
passes against State A and fails against yours, and reproduce it, before it
counts.

Everything is rebuilt from your sources on a clean image. A build directory you
leave behind is ignored, and a wheel you built and committed is not used. The
grading interpreter does have `setuptools` — State A is a setuptools build and it
is the reference everything is measured against, so the image that grades you has
to be able to build it. That is not a loophole: whether the old toolchain left is
decided by the reading above, over both trees, and a build that still needs it
scores nothing regardless of what it produces. There is no list of strings to
avoid — write the build system you would ship.

### Explicitly out of scope

Not required, not graded, not penalised:

- **The interpreter component of the wheel tag.** State A tags
  `cp35-abi3-linux_x86_64`. The backend derives the interpreter component from
  whichever interpreter built the wheel, so `cp312-abi3-linux_x86_64` is a
  correct result and is not penalised. The **ABI** component is not out of scope:
  see §2.6.
- **sdist.** Nothing builds or grades one, and `MANIFEST.in` goes away with the
  rest of the old build system (§3). Note before you try it anyway: `meson dist`
  hard-requires a Git or Mercurial checkout. Your tree has the `state-a` git
  repository from §6 and so it would appear to work here, but grading collects
  the working tree **without** `.git`, so a build that reaches for `meson dist`
  works in front of you and fails where it is measured. Do not make `--sdist`
  part of the build.
- **`.dist-info/LICENSE.rst` and `AUTHORS.rst`.** Reaching them requires PEP 639
  `license-files`, which in turn requires an SPDX `license` expression, which
  would replace the required `License: BSD, Public Domain` field. Keep the
  field; drop the files.
- **`.dist-info/top_level.txt`.** Legacy; meson-python does not emit it.
- **`Platform:`** — PEP 621 has no equivalent.
- **The `Cryptodome` / `pycryptodomex` dual-namespace build.** `setup.py` grows
  a second distribution when a `.separate_namespace` marker is present. That
  marker is not in this snapshot, so State A builds only `pycryptodome`. The
  machinery goes away with `setup.py`; do not reimplement it.

---

## 6. Ground rules

- Work directly in `/workspace/repo`. Do not produce a patch.
- **No network.** Everything you need is installed: `meson`, `ninja`,
  `meson-python`, a C toolchain, `patchelf`, `binutils`.
- A git repository exists with a single commit tagged `state-a`, for your own
  diffing. It has no history beyond that snapshot and no remote. Commit or don't
  commit; only the working tree is collected, and `.git` is **not** part of it —
  so nothing in the build may depend on being in a checkout.
- The graders run in a separate container from a clean image. Anything you leave
  in a build directory is discarded; only the repository source is carried over.
  A wheel you built and committed will not be used — the verifier builds from
  your source.
- `pycryptodome-test-vectors` is installed, which `Crypto.SelfTest` needs.

A useful first move is to build State A once and read the compiler command
lines, so you are migrating something you have measured rather than something
you have assumed.

