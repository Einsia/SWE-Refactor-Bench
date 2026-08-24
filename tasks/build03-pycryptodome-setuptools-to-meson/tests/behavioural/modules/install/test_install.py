"""What a user gets after `pip install`, exercised from outside the source tree.

Every other module in this stage reads an artifact. This one runs the library.

The distinction that matters is the working directory. `import Crypto` inside a
checkout of pycryptodome succeeds whether or not anything was built, because
`lib/` is on the path and the pure-Python half is right there. It succeeds and
then fails later, deep inside a cipher, with an error about a missing shared
object. So every subprocess here starts in a scratch directory with `-I` --
isolated mode, no `sys.path[0]`, no `PYTHONPATH`, no user site -- and is handed
exactly one importable location: the install tree.

The functions exercised are chosen to walk the whole delivery path rather than to
be a second test suite. Crypto.SelfTest is the test suite, and the selftest
module runs all 39,245 of its assertions. What is here instead is the set of
entry points that each depend on a *different* part of the build having worked:
a ctypes library that must be found by name, a module that must be found by a
different name in a different package, the GMP-backed integer path, and the
pure-Python fallback that must still work when the fast one is absent.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

#: AES-128-CBC over `b"pycryptodome" * 4`, key `bytes(range(16))`,
#: IV `bytes(range(16, 32))`, no padding. Cross-checked against
#: `openssl enc -aes-128-cbc -nopad` rather than taken from this library, so it is
#: an independent answer and not a recording of whatever the build produced.
AES_CBC_KNOWN_ANSWER = (
    "37b9b71f200a18d89f2db6bc8c28e4d2"
    "bef8cb2e727fc3dd656123c1de7f8596"
    "eb8de2a0dd6f3f2360ac3bf7a16b9d2f"
)


def _run(code: str, cwd: Path, timeout: int = 300):
    """Run `code` in `cwd` with nothing implicit on the import path."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SRB_")}
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    return subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def install(built):
    return Path(built("default").install)


@pytest.fixture(scope="module")
def elsewhere(tmp_path_factory) -> Path:
    """A directory with nothing in it, so an import can only come from the install."""
    return tmp_path_factory.mktemp("elsewhere")


def _probe(install: Path, elsewhere: Path, body: str, timeout: int = 300) -> dict:
    """Run `body` in an isolated interpreter and return the `out` dict it filled in.

    Assembled line by line at column zero rather than from an indented f-string:
    a template whose interpolation point sits at some indentation, holding a block
    that has its own, produces an IndentationError that arrives looking exactly
    like a submission whose library will not import.
    """
    lines = [
        "import json, sys",
        f"sys.path.insert(0, {str(install)!r})",
        "out = {}",
        "try:",
        *("    " + l if l.strip() else "" for l in textwrap.dedent(body).splitlines()),
        "except BaseException as exc:",
        '    out["error"] = f"{type(exc).__name__}: {exc}"',
        'print("SRBJSON" + json.dumps(out))',
    ]
    proc = _run("\n".join(lines), elsewhere, timeout=timeout)
    line = [l for l in proc.stdout.splitlines() if l.startswith("SRBJSON")]
    if not line:
        raise AssertionError(
            "the probe interpreter produced no result at all, which means it died before "
            f"reaching the end of the script.\nrc={proc.returncode}\n"
            f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-3000:]}"
        )
    return json.loads(line[-1][len("SRBJSON"):])


def test_the_package_imports_from_outside_the_source_tree(install, elsewhere):
    """The first thing a user does. Run where `lib/` is not on the path."""
    got = _probe(install, elsewhere, """
        import Crypto
        out["version"] = Crypto.__version__
        out["file"] = Crypto.__file__
        out["cwd_on_path"] = any(p in ("", ".") for p in sys.path)
    """)
    assert "error" not in got, (
        f"importing Crypto from an install tree, with the source directory nowhere on the "
        f"path, failed: {got['error']}"
    )
    assert got["file"].startswith(str(install)), (
        f"Crypto was imported from {got['file']}, which is not inside the install tree "
        f"{install}; the check is measuring some other copy of the library"
    )


def test_a_block_cipher_encrypts_and_decrypts(install, elsewhere):
    """AES through the ctypes path: `_raw_aes` and `_raw_cbc` both have to load.

    A known answer, so a build that produced a loadable object doing the wrong
    arithmetic is a failure rather than a pass.
    """
    got = _probe(install, elsewhere, """
        from Crypto.Cipher import AES
        key = bytes(range(16))
        iv = bytes(range(16, 32))
        pt = b"pycryptodome" * 4
        ct = AES.new(key, AES.MODE_CBC, iv=iv).encrypt(pt)
        out["ct"] = ct.hex()
        out["roundtrip"] = AES.new(key, AES.MODE_CBC, iv=iv).decrypt(ct) == pt
    """)
    assert "error" not in got, f"AES-CBC could not run from the install tree: {got['error']}"
    assert got["roundtrip"] is True, "AES-CBC decrypt did not invert encrypt"
    assert got["ct"] == AES_CBC_KNOWN_ANSWER, (
        f"AES-128-CBC over 48 known bytes produced {got['ct']}, and the answer for this "
        f"key and IV is {AES_CBC_KNOWN_ANSWER}. A round trip alone cannot catch this: a "
        f"cipher that is wrong in both directions inverts itself perfectly."
    )


def test_a_hash_matches_a_published_digest(install, elsewhere):
    """SHA-256 of the empty string, from `_SHA256`. A value with no room to drift."""
    got = _probe(install, elsewhere, """
        from Crypto.Hash import SHA256
        out["empty"] = SHA256.new().hexdigest()
        out["abc"] = SHA256.new(b"abc").hexdigest()
    """)
    assert "error" not in got, f"SHA256 could not run from the install tree: {got['error']}"
    assert got["empty"] == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    ), f"SHA256 of the empty string is {got['empty']}"
    assert got["abc"] == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    ), f"SHA256 of b'abc' is {got['abc']}"


def test_an_aead_mode_detects_tampering(install, elsewhere):
    """GCM, which is `_raw_aes` plus whichever `_ghash` the build selected.

    31,535 of the selftest's 39,245 ids are GCM. Here it is exercised once, for
    the property a user depends on: a modified ciphertext must not verify.
    """
    got = _probe(install, elsewhere, """
        from Crypto.Cipher import AES
        key, nonce = bytes(range(16)), bytes(range(12))
        c = AES.new(key, AES.MODE_GCM, nonce=nonce)
        c.update(b"header")
        ct, tag = c.encrypt_and_digest(b"secret message")
        d = AES.new(key, AES.MODE_GCM, nonce=nonce); d.update(b"header")
        out["roundtrip"] = d.decrypt_and_verify(ct, tag) == b"secret message"
        bad = bytearray(ct); bad[0] ^= 1
        d2 = AES.new(key, AES.MODE_GCM, nonce=nonce); d2.update(b"header")
        try:
            d2.decrypt_and_verify(bytes(bad), tag)
            out["rejected"] = False
        except ValueError:
            out["rejected"] = True
        out["tag_len"] = len(tag)
    """)
    assert "error" not in got, f"AES-GCM could not run from the install tree: {got['error']}"
    assert got["roundtrip"] is True, "AES-GCM decrypt_and_verify rejected its own ciphertext"
    assert got["rejected"] is True, (
        "AES-GCM accepted a ciphertext with a flipped bit. The library is producing output "
        "and authenticating nothing."
    )
    assert got["tag_len"] == 16, f"the GCM tag is {got['tag_len']} bytes"


def test_public_key_signing_round_trips(install, elsewhere):
    """RSA: `Crypto.Math` over the integer backend, plus `_raw_sha256` for the digest.

    A 2048-bit key is generated rather than hardcoded because generation is the
    part that exercises the modular arithmetic the build chose a backend for.
    """
    got = _probe(install, elsewhere, """
        from Crypto.PublicKey import RSA
        from Crypto.Signature import pkcs1_15
        from Crypto.Hash import SHA256
        key = RSA.generate(2048)
        h = SHA256.new(b"a message")
        sig = pkcs1_15.new(key).sign(h)
        out["bits"] = key.size_in_bits()
        out["len"] = len(sig)
        pkcs1_15.new(key.publickey()).verify(h, sig)
        out["verified"] = True
        h2 = SHA256.new(b"a different message")
        try:
            pkcs1_15.new(key.publickey()).verify(h2, sig)
            out["rejected"] = False
        except ValueError:
            out["rejected"] = True
    """, timeout=600)
    assert "error" not in got, f"RSA signing could not run from the install tree: {got['error']}"
    assert got["bits"] == 2048, f"RSA.generate(2048) produced a {got['bits']}-bit key"
    assert got["len"] == 256, f"a 2048-bit PKCS#1 v1.5 signature is {got['len']} bytes"
    assert got["verified"] is True, "the signature did not verify against its own key"
    assert got["rejected"] is True, (
        "a signature over one message verified against a different message; the "
        "verification path is not checking anything"
    )


def test_the_modular_exponentiation_helper_is_wired_up(install, elsewhere):
    """`_modexp`, the one object with a reason to exist beyond the cipher cores.

    It is separate from the ciphers and separate from the hashes, so a build that
    delivered 40 of 41 objects can still pass everything above and fail here.
    """
    got = _probe(install, elsewhere, """
        from Crypto.Math.Numbers import Integer
        out["backend"] = Integer.__module__
        b, e, m = 3, 1000003, 2 ** 521 - 1
        # inplace_pow mutates its receiver, so the expected value is computed first
        # and from Python's own pow rather than from anything the build produced.
        expected = pow(b, e, m)
        got_value = int(Integer(b).inplace_pow(Integer(e), Integer(m)))
        out["agrees"] = got_value == expected
        out["got"] = str(got_value)
        out["expected"] = str(expected)
    """)
    assert "error" not in got, f"Crypto.Math.Numbers could not run: {got['error']}"
    assert got["agrees"] is True, (
        f"the library's modular exponentiation disagrees with Python's built-in pow() for "
        f"3 ** 1000003 mod (2**521 - 1), so every public-key operation is computing the "
        f"wrong number.\n  library: {got['got']}\n  correct: {got['expected']}"
    )


def test_the_pure_python_fallback_still_works(install, elsewhere):
    """`Crypto.Math._IntegerNative`, which ships as source and needs no build.

    It is easy to lose: it is imported only when the compiled backends are
    missing, so nothing else in this stage would notice its absence.
    """
    got = _probe(install, elsewhere, """
        from Crypto.Math._IntegerNative import IntegerNative
        a = IntegerNative(2) ** 100
        out["ok"] = int(a) == 2 ** 100
        out["module"] = IntegerNative.__module__
    """)
    assert "error" not in got, (
        f"the pure-Python integer backend could not be imported from the install: "
        f"{got['error']}"
    )
    assert got["ok"] is True, "the pure-Python integer backend computes 2**100 incorrectly"


def test_the_install_lands_where_the_wheel_said_it_would(install, built):
    """Installing the wheel produces exactly the payload the wheel listed.

    Not a restatement of the wheel module's member comparison. A wheel may route
    files through `{name}-{version}.data/purelib/`, `.data/platlib/`, `.data/scripts/`
    and so on, and the installer moves those somewhere else entirely. So a wheel can
    list every correct name and still install a differently-shaped tree -- or install
    a script into `bin/`, which no member comparison would flag as odd.

    Bytecode is excluded because whether it exists depends on the installer's flags
    rather than on the wheel. Whether the *wheel* carries bytecode is asserted where
    it belongs, over the archive.
    """
    import wheelutil

    wheel = wheelutil.Wheel(Path(built("default").wheel))
    try:
        listed = set(wheel.payload)
    finally:
        wheel.close()

    landed = {
        p.relative_to(install).as_posix()
        for p in install.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not p.relative_to(install).parts[0].endswith(".dist-info")
    }
    missing, extra = sorted(listed - landed), sorted(landed - listed)
    assert not missing, (
        f"{len(missing)} members the wheel listed are not at those paths after install, so "
        f"the archive and the installed tree describe different layouts: "
        f"{', '.join(missing[:8])}"
    )
    assert not extra, (
        f"installing the wheel created {len(extra)} files at paths the wheel's payload does "
        f"not name: {', '.join(extra[:8])}"
    )


def test_the_install_tree_holds_only_the_project(install):
    """One top-level package and one `.dist-info`. Nothing else was vendored in."""
    tops = sorted(p.name for p in install.iterdir())
    unexpected = [t for t in tops if t != "Crypto" and not t.endswith(".dist-info")]
    assert not unexpected, (
        f"installing the wheel created {len(unexpected)} top-level entries that are neither "
        f"the package nor its metadata: {', '.join(unexpected)}. Each one is a name the "
        f"project has taken over in every environment it is installed into."
    )
