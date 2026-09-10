"""Where the platform layer is, and where the old one went.

Every check here is advisory.  The point is to hand the reviewer the locations it
would otherwise spend an hour finding, not to decide anything: a `unixOpen` in a
comment and a `unixOpen` in a function body are the same string, and only one of
them means the POSIX layer is still in the tree.

The POSIX layer is looked for by the vocabulary of its own code rather than by its
file name, because the file name is the one thing a submission that wanted to hide
it would change first.
"""

from __future__ import annotations

import pytest
import srbscan as S

# `srb_skip_ok` suite-wide.  Several checks here are conditional on a lead the
# previous check already reported -- there is no point looking for POSIX calls in
# the new platform layer if no file defines a VFS -- and without the marker an
# unlicensed skip is rewritten by `pytest_module` into `verdict=fail,
# summary="skipped"`.  On a do-nothing tree that is what
# `test_posix_interfaces_are_not_in_the_new_platform_layer` reaches the reviewer as:
# a finding that reads "skipped" and names nothing.
pytestmark = [pytest.mark.scan, pytest.mark.srb_skip_ok]

# The two platform implementations State A shipped.  Reported by name because the
# reviewer wants to know, not because absence proves anything -- the content checks
# below are the ones that survive a rename.
#
# `src/os_win.h` is deliberately not here.  It is a header the rest of the tree
# includes (`src/os.h` pulls it in under `_WIN32`), the task says it stays, and
# listing it would report every correct port for keeping a file it was told to keep.
# It belongs with the scaffolding below instead.
STATE_A_LAYER = ("src/os_unix.c", "src/os_win.c")

# Files State A shipped that a port keeps.  os.c dispatches through sqlite3_vfs and
# is not platform-specific; os_setup.h and os_common.h are the shared scaffolding;
# os_win.h is a header, not an implementation.
STATE_A_SHARED = ("src/os.c", "src/os.h", "src/os_common.h", "src/os_setup.h",
                  "src/os_win.h")


def test_state_a_layer_files_by_name(repo, delivered):
    """Which of State A's platform files are still delivered, by name."""
    present = [p for p in STATE_A_LAYER if p in delivered]
    if present:
        S.flag("State A's platform layer is still delivered under its own name",
               present,
               note="The weakest possible version of the question -- a rename "
                    "passes it -- so it is reported for completeness, and the "
                    "content checks in this module are the ones worth reading.")


def test_shared_os_scaffolding_still_present(repo, delivered):
    """os.c and friends dispatch through sqlite3_vfs and are not platform code."""
    missing = [p for p in STATE_A_SHARED if p not in delivered]
    if missing:
        S.flag("State A's VFS-neutral scaffolding is not delivered", missing,
               note="These are not the POSIX layer -- os.c is the dispatcher every "
                    "VFS is reached through, os_win.h is a header the task said to "
                    "keep -- so their absence is a lead about how the port is "
                    "structured, not about whether the old layer left.")


def test_no_file_carries_the_unix_vfs_vocabulary(repo):
    """The POSIX VFS by its own symbol names, in any delivered file.

    Three or more of the listed names in one file is the threshold.  One is
    explicable -- a comment, a porting note, a name that happens to collide -- and
    three is a copy of the implementation.
    """
    found: dict[str, list[str]] = {}
    for path, rel in S.text_files(repo):
        got = S.introduced(rel, S.UNIX_VFS_SYMBOLS)
        if len(got) >= 3:
            found[rel] = got[:4]
    if found:
        S.flag("delivered files carry State A's POSIX VFS symbol names", found,
               note="These names are unique to State A's src/os_unix.c, and every "
                    "one listed is in the submission's copy of the file and not in "
                    "State A's -- so this is not the tree reporting its own "
                    "history. A file with several of them in it is that "
                    "implementation, whatever the file is called. Open it: if the "
                    "names are in live code, the POSIX layer was renamed rather "
                    "than retired.")


def test_no_file_carries_the_windows_vfs_vocabulary(repo):
    """The same for os_win.c.  Both layers were to go."""
    found: dict[str, list[str]] = {}
    for path, rel in S.text_files(repo):
        got = S.introduced(rel, S.WIN_VFS_SYMBOLS)
        if len(got) >= 3:
            found[rel] = got[:4]
    if found:
        S.flag("delivered files carry State A's Windows VFS symbol names", found,
               note="Same reading as the POSIX version above. The Windows layer was "
                    "to go too, and a port that deleted one and kept the other has "
                    "not finished -- but check first whether what you are looking "
                    "at is src/os_win.h, which the task said to keep.")


def test_no_file_carries_the_unix_layer_banner(repo):
    """The file banner, verbatim.  A renamed copy keeps it.

    Reported only where the submission introduced it, which is what makes this
    check about the submission.  `src/os_unix.c` still being there is a finding, and
    it is `test_state_a_layer_files_by_name` above that says so, by name -- not this
    one, quoting SQLite's own copyright header back at the reviewer.
    """
    found = []
    for path, rel in S.text_files(repo):
        if S.introduced(rel, (S.UNIX_BANNER,)):
            found.append(f"{rel}:{S.first_line_of(path, S.UNIX_BANNER)}")
    if found:
        S.flag("State A's os_unix.c banner comment now appears in", found,
               note="Nobody hiding a file edits its copyright header, so this is a "
                    "strong indication of where that file went. It is reported only "
                    "where the submission introduced the line.")


def test_no_file_carries_the_windows_layer_banner(repo):
    """As above.  `src/os_win.h` opens with the same banner and was to be kept, so
    scoping this by token rather than by file is what keeps it quiet."""
    found = []
    for path, rel in S.text_files(repo):
        if S.introduced(rel, (S.WIN_BANNER,)):
            found.append(f"{rel}:{S.first_line_of(path, S.WIN_BANNER)}")
    if found:
        S.flag("State A's os_win.c banner comment now appears in", found,
               note="Reported only where the submission introduced the line, which "
                    "is what keeps src/os_win.h -- kept by instruction, and opening "
                    "with the same sentence -- out of this list.")


def test_some_delivered_file_defines_a_vfs(repo):
    """A lead, not a gate: is there anything here that looks like a VFS at all?

    Written to fail on absence rather than on presence, because absence is the
    surprising case. If no delivered file defines a sqlite3_vfs, the port either
    registers one through a mechanism this pattern does not recognise -- possible,
    and stage 2 settles it by opening a database, which cannot work without a
    VFS -- or has no platform layer, in which case nothing else in this review
    matters. Either way the reviewer should look, and this says so.
    """
    found = [f"{rel}:{S.first_line_of(path, 'sqlite3_vfs')}"
             for path, rel in S.vfs_files(repo)]
    if not found:
        S.flag("no file the submission wrote defines anything recognisable as a "
               "platform layer -- start by finding what registers a VFS, if "
               "anything does",
               note="What was looked for: a reference to sqlite3_vfs beside any of "
                    f"{', '.join(S.VFS_EVIDENCE)}. State A's own src/test_demovfs.c, "
                    "src/test_vfs.c and ext/async/sqlite3async.c match that and are "
                    "excluded, because they arrived with the tree. A port whose VFS "
                    "this misses is possible; stage 2 settles it by opening a "
                    "database, which no port can do without one.")


def test_sqlite_os_other_is_selected_somewhere(repo):
    """SQLITE_OS_OTHER means 'the application supplies the OS layer'.

    Also an absence check. With none of SQLITE_OS_UNIX, SQLITE_OS_WIN or
    SQLITE_OS_OTHER set, os_setup.h picks a built-in layer by inspecting predefined
    compiler macros -- so a build that never mentions it is either relying on a
    default that does not exist for wasm32-wasi, or setting it somewhere this scan
    does not read.
    """
    found = []
    for path, rel in S.authored_text_files(repo):
        if "SQLITE_OS_OTHER" in S.read(path):
            found += S.cite(path, rel, "SQLITE_OS_OTHER", limit=2)
    if not found:
        S.flag("no file the submission wrote mentions SQLITE_OS_OTHER -- read what "
               "this build selects instead",
               note="State A's src/os_setup.h picks a built-in platform layer by "
                    "inspecting predefined compiler macros when none of "
                    "SQLITE_OS_UNIX, SQLITE_OS_WIN or SQLITE_OS_OTHER is set. A "
                    "build that never mentions it is either relying on a default "
                    "that does not exist for wasm32-wasi, or setting it somewhere "
                    "this scan does not read -- a compiler flag in a makefile the "
                    "submission did not touch, for instance.")


def test_posix_interfaces_are_not_in_the_new_platform_layer(repo):
    """POSIX calls a WASI guest does not have, in the files that define a VFS.

    Scoped twice, and both scopes were measured against a do-nothing tree. Without
    the first -- only the files that define a VFS -- the whole tree names `pthread_`
    in the mutex header and `getcwd` in Tcl test scripts, 200 lines of noise around
    the two that matter. Without the second -- only the VFS files the submission
    wrote -- the list is State A's own src/os_unix.c, src/test_demovfs.c,
    src/test_syscall.c and ext/async/sqlite3async.c, which is a description of
    SQLite 3.31.1 rather than of the submission.

    What is left is one of these inside the code that is supposed to be the *new*
    layer -- either a dead branch left behind a macro, or a layer that was adapted
    rather than written.
    """
    layer_files = list(S.vfs_files(repo))
    if not layer_files:
        pytest.skip("no file defines a VFS; the previous check reports that")

    found: dict[str, list[str]] = {}
    for path, rel in layer_files:
        got = S.hits(path, S.POSIX_INTERFACES)
        if got:
            found[rel] = got[:4]
    if found:
        S.flag("the file(s) defining a VFS also name POSIX interfaces a WASI guest "
               "does not have", found,
               note="Read where they appear. Behind `#ifndef SQLITE_OS_OTHER`, or in "
                    "a comment describing what was replaced, this is nothing. On a "
                    "live path it is either code that cannot compile for this target "
                    "or a layer that was adapted rather than written.")


def test_wasm_import_attributes_in_delivered_code(repo):
    """clang's spellings for 'this function comes from a host module'.

    A wasm module can only call host functions it imports, so a port that shells
    out to native code has to declare the import somewhere. Stage 2 reads the
    delivered module's own import section, which is proof rather than evidence --
    but stage 2 only runs if this gate passes.
    """
    found: dict[str, list[str]] = {}
    for path, rel in S.text_files(repo):
        got = S.introduced(rel, S.IMPORT_ATTRIBUTES)
        if got:
            found[rel] = got
    if found:
        S.flag("delivered files declare a wasm module import", found,
               note="An import outside wasi_snapshot_preview1 is a call into native "
                    "code. Read which module name is being declared: a port that "
                    "imports `wasi_snapshot_preview1` explicitly is doing something "
                    "ordinary, and stage 2 reads the built module's own import "
                    "section either way.")
