"""The WASI contract: what the port must do that the reference cannot.

Every other family records its expectation from native sqlite3 3.31.1, which is
the right way round -- the reference decides, and the port has to agree.  This
family is the exception, and it exists because a handful of the shell's promises
are answered by the *operating system* rather than by SQLite.  Native has pipes,
``dlopen`` and an unrestricted filesystem; a wasm32-wasi module has none of the
three.  Asking the reference what ``.shell echo hi`` should print gets an answer
that is correct for Linux and meaningless for the port.

So these cases carry a ``literal``: the expectation is written down, from the
platform's own rules, and the module compares against it directly.  That is a
weaker instrument than a differential -- a literal cannot notice that upstream
would have done something else -- so the bar for putting a case here is high.  A
difference earns a literal only when it comes from one of three places:

1. **WASI has no such concept.**  There are no permission bits (wasi-libc's
   ``chmod`` is a stub returning ``ENOTSUP``), no ``mmap``, no ``dlopen``, no
   ``fork``/``exec``, and no threads in this build.  The port cannot invent them,
   and a port that *pretends* to have them by faking the observable value is
   doing something worse than failing.
2. **Upstream provides a porting macro and the port is required to use it.**
   ``SQLITE_OS_OTHER=1``, ``SQLITE_OMIT_POPEN`` and ``SQLITE_NOHAVE_SYSTEM`` are
   3.31.1's own answers to "this platform lacks that", and each one changes the
   shell's output in a way upstream chose deliberately.  The literal here is
   upstream's text, not ours -- e.g. ``"Error: pipes are not supported in this
   OS"`` comes from shell.c:16717, not from this file.
3. **This benchmark pins a name the source leaves open.**  Exactly one entry is
   of this kind: the VFS is named ``wasi``.  Upstream does not care what an
   ``SQLITE_OS_OTHER`` VFS calls itself, so the requirement is stated in
   instruction.md and asserted here.  Everything else about the VFS --
   ``szOsFile``, ``mxPathname``, whether WAL's shared-memory region is a file or
   heap, where temp files land -- is deliberately *not* asserted, because those
   are the port's decisions to make.

Anything that merely *differs* is not enough.  A candidate that both targets
answer identically belongs in one of the differential modules, where a regression
in either direction is caught.  Of 52 candidates probed on both targets, 48 agreed;
a case belongs here because the two targets answer it differently, not because it
touches the platform layer.

The probe, which is the measurement the whole suite rests on
--------------------------------------------------------------
3.31.1 was built twice from the same source -- native gcc 12 and wasm32-wasi
clang 18 -- and 52 behavioural probes were diffed between the two binaries.

**48 identical, 4 different.**  All four differences reduced to build
configuration rather than to platform semantics, and after pinning the feature set
to the native build's ``compile_options``, three survive.  Each is forced::

    THREADSAFE=1 -> 0          wasi-libc has no usable pthreads in this target
    OMIT_LOAD_EXTENSION added  WASI has no dlopen, so load_extension() cannot exist
    COMPILER=gcc-* -> clang-*  unavoidable; normalised out rather than asserted

Everything else came out byte-identical: int64 and float formatting, ``printf``,
collation, ``strftime``, json1, fts4, fts5, rtree, error message texts, pragma
output, and the whole dot-command surface.  That result is what licenses the other
four case sets to compare the two builds byte for byte and treat any difference as
the port's.

Two things were separately verified *reachable*, so this suite requires them
rather than excusing them.  **WAL works**: with a heap-backed ``xShmMap``,
``journal_mode=wal`` returns ``wal`` and native sqlite3 reads the resulting file
back with ``audit_check ok`` -- the wal-index has to be shared between
connections, and within one wasm instance the heap satisfies that, so no shared
memory is needed.  **zlib cross-compiles** for wasm32-wasi with
``-DHAVE_UNISTD_H``, which keeps ``.archive``, ``zipfile`` and ``sqlar`` alive and
interoperable in both directions.

The four ``box.dotdot.*`` cases are the deliberate exception: they agree, but only
by accident -- native returns NULL because no file happens to sit next to the
sandbox directory, not because it refused to look.  They assert a security
property of the port, so the expectation is stated rather than sampled.

A difference the source leaves to the platform *and* does not pin gets a
normaliser in normalise.py, or gets left out of the comparison entirely.  The
split is the whole design, and it is why this file describes the smallest of the
five case sets rather than the largest.

Every literal below was measured on both targets before it was written down; the
comment on each group names the source line that produces it, and the ``DIFFER:``
line records what native answered.
"""

from __future__ import annotations

from case import Case, CaseError

#: A database in the first preopen.  Cases that only need the engine use
#: ``:memory:`` so that no VFS is involved in the answer.
DB = "/data/plat.db"

#: key -> the observation the port must produce, in the same shape
#: ``execute.Observation.select`` returns.  Each entry was measured on both
#: targets before it was written down, and the ``DIFFER:`` comment above it
#: records what the native side answered -- so a reader can check that the pair
#: really does diverge, rather than taking the classification on trust.  A case
#: whose key is missing here is a hard error rather than a pass, because a
#: platform case with no expectation asserts nothing.
_LITERALS: dict[str, dict[str, object]] = {
    # DIFFER: native exit=0  stdout='     load_extension on\n'  stderr=''
    'plat.ext.dbconfig': {
        'stdout': '     load_extension off\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: near line 1: x.so: cannot open shared object file: No such file or directory\n'
    'plat.ext.fn.one': {
        'stdout': '',
        'stderr': 'Error: near line 1: no such function: load_extension\n',
        'exit': 1,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: near line 1: x.so: cannot open shared object file: No such file or directory\n'
    'plat.ext.fn.two': {
        'stdout': '',
        'stderr': 'Error: near line 1: no such function: load_extension\n',
        'exit': 1,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: ./x.so: cannot open shared object file: No such file or directory\n'
    'plat.ext.dot.load': {
        'stdout': '',
        'stderr': 'Error: unknown command or invalid arguments:  "load". Enter ".help" for help\n',
        'exit': 1,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: ./x.so: cannot open shared object file: No such file or directory\n'
    'plat.ext.dot.load.entry': {
        'stdout': '',
        'stderr': 'Error: unknown command or invalid arguments:  "load". Enter ".help" for help\n',
        'exit': 1,
    },
    # DIFFER: native exit=1  stdout='     load_extension on\n     load_extension on\n'  stderr='Error: near line 3: x.so: cannot open shared object file: No such file or directory\n'
    'plat.ext.enable': {
        'stdout': '     load_extension on\n'
                  '     load_extension on\n',
        'stderr': 'Error: near line 3: no such function: load_extension\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='0\n'  stderr=''
    'plat.ext.compileoption': {
        'stdout': '1\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: near line 1: no editor for edit()\n'
    'plat.ext.edit': {
        'stdout': '',
        'stderr': 'Error: near line 1: no such function: edit\n',
        'exit': 1,
    },
    # DIFFER: native exit=-9  stdout=''  stderr='<timeout after 60.0s>'
    'plat.ext.edit.two': {
        'stdout': '',
        'stderr': 'Error: near line 1: no such function: edit\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='edit\nedit\nload_extension\nload_extension\nreadfile\nshell_add_schema\nshell_module_schema\nshell_putsnl\...  stderr=''
    'plat.ext.fn.list': {
        'stdout': 'readfile\n'
                  'shell_add_schema\n'
                  'shell_module_schema\n'
                  'shell_putsnl\n'
                  'sqlar_compress\n'
                  'sqlar_uncompress\n'
                  'writefile\n'
                  'zipfile\n'
                  'zipfile_cds\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='138\n'  stderr=''
    'plat.ext.fn.count': {
        'stdout': '134\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='.load FILE ?ENTRY?       Load an extension library\n'  stderr=''
    'plat.ext.help.load': {
        'stdout': '.open ?OPTIONS? ?FILE?   Close existing database and reopen FILE\n'
                  '     Options:\n'
                  '        --append        Use appendvfs to append database to the end of FILE\n'
                  '        --deserialize   Load into memory useing sqlite3_deserialize()\n'
                  '        --hexdb         Load the output of "dbtotxt" as an in-memory db\n'
                  '        --maxsize N     Maximum size for --hexdb or --deserialized database\n'
                  '        --new           Initialize FILE to an empty database\n'
                  '        --nofollow      Do not follow symbolic links\n'
                  '        --readonly      Open FILE readonly\n'
                  '        --zip           FILE is a ZIP archive\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1\nf.txt|33188\n'  stderr=''
    'plat.mode.file': {
        'stdout': '1\n'
                  'f.txt|32768\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\nd|16877\n'  stderr=''
    'plat.mode.dir': {
        'stdout': '\n'
                  'd|16384\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\nl.txt|41471\n'  stderr=''
    'plat.mode.symlink': {
        'stdout': '\n'
                  'l.txt|40960\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1\n1\n33024\nx\n'  stderr=''
    'plat.mode.chmod.ignored': {
        'stdout': '1\n'
                  '1\n'
                  '32768\n'
                  'x\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='10\n33261\n'  stderr=''
    'plat.mode.chmod.executable': {
        'stdout': '10\n'
                  '32768\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1\n33204\nx\n'  stderr=''
    'plat.mode.chmod.zero': {
        'stdout': '1\n'
                  '32768\n'
                  'x\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='5\n6\n\np|16877\np/a.txt|33188\np/b.txt|33188\n'  stderr=''
    'plat.mode.sqlar.store': {
        'stdout': '5\n'
                  '6\n'
                  '\n'
                  'p|16384\n'
                  'p/a.txt|32768\n'
                  'p/b.txt|32768\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='5\n6\n\nout|16893\nout/p|16877\nout/p/a.txt|33188\nout/p/b.txt|33188\n'  stderr=''
    'plat.mode.sqlar.extract': {
        'stdout': '5\n'
                  '6\n'
                  '\n'
                  'out|16384\n'
                  'out/p|16384\n'
                  'out/p/a.txt|32768\n'
                  'out/p/b.txt|32768\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='5\n6\n\np/|16877\np/a.txt|33188\np/b.txt|33188\n'  stderr=''
    'plat.mode.zip.store': {
        'stdout': '5\n'
                  '6\n'
                  '\n'
                  'p/|16384\n'
                  'p/a.txt|32768\n'
                  'p/b.txt|32768\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='5\n\ndrwxr-xr-x          0  2020-01-26 00:53:20  p\n-rw-r--r--          5  2020-01-26 00:53:20  p/a.txt\n'  stderr=''
    'plat.mode.listing': {
        'stdout': '5\n'
                  '\n'
                  'd---------          0  2020-01-26 00:53:20  p\n'
                  '----------          5  2020-01-26 00:53:20  p/a.txt\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='src|16893\nsrc/a.txt|33204\nsrc/nested|16893\nsrc/nested/c.txt|33204\n'  stderr=''
    'plat.mode.fsdir.tree': {
        'stdout': 'src|16384\n'
                  'src/a.txt|32768\n'
                  'src/nested|16384\n'
                  'src/nested/c.txt|32768\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr='<pipe>:1: expected 2 columns but found 1 - filling the rest with NULL\n'
    'plat.pipe.import': {
        'stdout': '0\n',
        'stderr': 'Error: pipes are not supported in this OS\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.pipe.import.csv': {
        'stdout': '0\n',
        'stderr': 'Error: pipes are not supported in this OS\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.pipe.output': {
        'stdout': '1\n',
        'stderr': 'Error: pipes are not supported in this OS\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.pipe.once': {
        'stdout': '1\n',
        'stderr': 'Error: pipes are not supported in this OS\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n2\n'  stderr=''
    'plat.pipe.output.recover': {
        'stdout': '1\n'
                  '2\n',
        'stderr': 'Error: pipes are not supported in this OS\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='hi\n'  stderr=''
    'plat.sys.shell': {
        'stdout': '',
        'stderr': 'Error: unknown command or invalid arguments:  "shell". Enter ".help" for help\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='hi\n'  stderr=''
    'plat.sys.system': {
        'stdout': '',
        'stderr': 'Error: unknown command or invalid arguments:  "system". Enter ".help" for help\n',
        'exit': 1,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Usage: .system COMMAND\n'
    'plat.sys.shell.noargs': {
        'stdout': '',
        'stderr': 'Error: unknown command or invalid arguments:  "shell". Enter ".help" for help\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='\n'  stderr='sh: 1: xdg-open: not found\nFailed: [xdg-open tempNNNN.csv]\n'
    'plat.sys.excel': {
        'stdout': './-x\n'
                  '1\n'
                  '\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\n'  stderr='sh: 1: xdg-open: not found\nFailed: [xdg-open tempNNNN.csv]\n'
    'plat.sys.once.dashx': {
        'stdout': 'a|b\n'
                  '\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\n'  stderr='sh: 1: xdg-open: not found\nFailed: [xdg-open tempNNNN.txt]\n'
    'plat.sys.once.dashe': {
        'stdout': '1\n'
                  '\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\n'  stderr='sh: 1: xdg-open: not found\nFailed: [xdg-open tempNNNN.csv]\n'
    'plat.sys.once.dashdashx': {
        'stdout': '1\n'
                  '\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='.shell CMD ARGS...       Run CMD ARGS... in a system shell\n'  stderr=''
    'plat.sys.help.shell': {
        'stdout': "Nothing matches 'shell'\n",
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='.system CMD ARGS...      Run CMD ARGS... in a system shell\n'  stderr=''
    'plat.sys.help.system': {
        'stdout': '.once (-e|-x|FILE)       Output for the next SQL command only to FILE\n'
                  "     If FILE begins with '|' then open as a pipe\n"
                  '     Other options:\n'
                  '       -e    Invoke system text editor\n'
                  '       -x    Open in a spreadsheet\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='3\n'  stderr=''
    'plat.sys.help.absent': {
        'stdout': '0\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='63\n'  stderr=''
    'plat.sys.help.lines': {
        'stdout': '60\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='63\n'  stderr=''
    'plat.sys.help.commands': {
        'stdout': '60\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='<this-host>\n\n'  stderr=''
    'plat.box.read.shadowed': {
        'stdout': 'sandboxed\n'
                  '\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout="X'<this-host>'\n"  stderr=''
    'plat.box.read.absent': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout="X'7F454C4602010100000000000000000003003E0001000000F04E000000000000400000000000000078E301000000000000000000...  stderr=''
    'plat.box.read.bin': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout="X''\n"  stderr=''
    'plat.box.read.proc': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # AGREE: native exit=0  stdout='NULL\n'  stderr=''
    'plat.box.dotdot.plain': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # AGREE: native exit=0  stdout='NULL\n'  stderr=''
    'plat.box.dotdot.sub': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # AGREE: native exit=0  stdout='NULL\n'  stderr=''
    'plat.box.dotdot.data': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # AGREE: native exit=0  stdout='NULL\n'  stderr=''
    'plat.box.dotdot.root': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout="X'<this-host>'\n"  stderr=''
    'plat.box.dotdot.deep': {
        'stdout': 'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout="1\nX'78'\n"  stderr=''
    'plat.box.dotdot.write': {
        'stdout': '\n'
                  'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout="\nX'<this-host>'\n"  stderr=''
    'plat.box.symlink.abs': {
        'stdout': '',
        'stderr': 'Error: near line 1: failed to create symlink: link\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout="\nX'<this-host>'\n"  stderr=''
    'plat.box.symlink.dotdot': {
        'stdout': '\n'
                  'NULL\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\n\n\n'  stderr=''
    'plat.box.write.abs': {
        'stdout': '7\n'
                  'payload\n'
                  'payload\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='\n\n'  stderr=''
    'plat.box.write.newroot': {
        'stdout': '7\n'
                  'payload\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: near line 1: cannot read directory: \n'
    'plat.box.fsdir.root': {
        'stdout': '/\n'
                  '//src\n'
                  '//src/a.txt\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: near line 1: cannot read directory: \n'
    'plat.box.fsdir.outside': {
        'stdout': '',
        'stderr': 'Error: near line 1: cannot stat file: /etc\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='..\n../data\n../tmp\n'  stderr=''
    'plat.box.fsdir.dotdot': {
        'stdout': '',
        'stderr': 'Error: near line 1: cannot stat file: ..\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.box.open.argv': {
        'stdout': '',
        'stderr': 'Error: unable to open database "/etc/hostname": unable to open database file\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.box.open.dot': {
        'stdout': '1\n',
        'stderr': 'Error: unable to open database "/etc/hostname": unable to open database file\n',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.box.open.dotdot': {
        'stdout': '1\n',
        'stderr': 'Error: unable to open database "../escape.db": unable to open database file\n',
        'exit': 0,
    },
    # DIFFER: native exit=1  stdout=''  stderr='Error: near line 1: file is not a database\n'
    'plat.box.attach': {
        'stdout': '',
        'stderr': 'Error: near line 1: unable to open database: /etc/hostname\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout=''  stderr=''
    'plat.box.attach.dotdot': {
        'stdout': '',
        'stderr': 'Error: near line 1: unable to open database: ../escape.db\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='unix\n'  stderr=''
    'plat.vfs.name': {
        'stdout': 'wasi\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='unix\n'  stderr=''
    'plat.vfs.name.main': {
        'stdout': 'wasi\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='vfs.zName      = "unix"\nvfs.zName      = "apndvfs"\nvfs.zName      = "memdb"\nvfs.zName      = "unix-excl...  stderr=''
    'plat.vfs.list': {
        'stdout': 'vfs.zName      = "wasi"\n'
                  'vfs.zName      = "apndvfs"\n'
                  'vfs.zName      = "memdb"\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='6\n'  stderr=''
    'plat.vfs.list.count': {
        'stdout': '3\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.vfs.unix': {
        'stdout': '',
        'stderr': 'no such VFS: "(null)"\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.vfs.unix.dotfile': {
        'stdout': '',
        'stderr': 'no such VFS: "(null)"\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.vfs.unix.excl': {
        'stdout': '',
        'stderr': 'no such VFS: "(null)"\n',
        'exit': 1,
    },
    # DIFFER: native exit=0  stdout='1\n'  stderr=''
    'plat.vfs.unix.none': {
        'stdout': '',
        'stderr': 'no such VFS: "(null)"\n',
        'exit': 1,
    },
    # DIFFER: native exit=1  stdout=''  stderr='no such VFS: "(null)"\n'
    'plat.vfs.explicit': {
        'stdout': '1\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='1048576\n1048576\n'  stderr=''
    'plat.abi.mmap.set': {
        'stdout': '0\n'
                  '0\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout=''  stderr=''
    'plat.abi.mmap.memory': {
        'stdout': '0\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='483\n'  stderr=''
    'plat.abi.cache_spill': {
        'stdout': '489\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='4\n4\n'  stderr=''
    'plat.abi.threads.set': {
        'stdout': '0\n'
                  '0\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='0\n'  stderr=''
    'plat.abi.threadsafe': {
        'stdout': '1\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='ENABLE_DBSTAT_VTAB\nENABLE_FTS4\nENABLE_FTS5\nENABLE_JSON1\nENABLE_RTREE\nENABLE_STMTVTAB\nENABLE_UNKNOWN_...  stderr=''
    'plat.abi.compile_options': {
        'stdout': 'ENABLE_DBSTAT_VTAB\n'
                  'ENABLE_FTS4\n'
                  'ENABLE_FTS5\n'
                  'ENABLE_JSON1\n'
                  'ENABLE_RTREE\n'
                  'ENABLE_STMTVTAB\n'
                  'ENABLE_UNKNOWN_SQL_FUNCTION\n'
                  'HAVE_ISNAN\n'
                  'OMIT_LOAD_EXTENSION\n'
                  'THREADSAFE=0\n',
        'stderr': '',
        'exit': 0,
    },
    # DIFFER: native exit=0  stdout='10\n'  stderr=''
    'plat.abi.compile_options.count': {
        'stdout': '11\n',
        'stderr': '',
        'exit': 0,
    },
}


def _lit(key: str) -> dict[str, object]:
    """The measured expectation for one platform case, or a hard error.

    There is deliberately no fallback and no "unmeasured" mode.  A placeholder for a
    missing literal would be a case that runs, reports a pass, and asserts nothing.
    The keys and the table are both fixed, so the only way to reach this branch is to
    add a case and forget its expectation -- which should stop the module, not be
    absorbed by it.
    """
    try:
        return _LITERALS[key]
    except KeyError:
        raise CaseError(
            f"{key}: platform case has no measured expectation.  Measure it on "
            f"both targets and add it to cases_platform._LITERALS with the "
            f"source line that produces it; do not guess it from the rules."
        ) from None


def _plat(key: str, operation: str, stdin: str, *,
          argv: tuple[str, ...] = (":memory:",),
          files: dict[str, str] | None = None,
          checks: tuple[str, ...] = ("stdout", "stderr", "exit"),
          **kw: object) -> Case:
    """One platform case.

    ``stderr`` is in the default check set here, unlike the recorded families:
    the whole point of most of these cases is the *message*, and upstream chose
    the message.  A case that only cares about the outcome passes a narrower
    ``checks``.
    """
    return Case(
        key=f"plat.{key}", family="platform", operation=operation,
        kind="platform", argv=argv,
        stdin=stdin if stdin.endswith("\n") or not stdin else stdin + "\n",
        files=files or {}, checks=checks, literal=_lit(f"plat.{key}"), **kw,
    )


def _lines(path: str) -> str:
    """SQL that splits a file's text back into one row per line.

    The same helper as cases_extensions._lines, for the same reason: it lets a dot
    command's output be filtered, counted and sorted in SQL before it is
    asserted.  Used here to keep ``.vfslist`` to its ``vfs.zName`` lines -- the
    other three lines per VFS report ``szOsFile`` and ``mxPathname``, which are
    the port's own numbers -- and to ask structural questions of ``.help``
    without pinning two hundred lines of text into a literal.
    """
    return (
        "with recursive lines(rest, line) as ("
        f" select cast(readfile('{path}') as text), null"
        " union all"
        " select substr(rest, instr(rest, char(10))+1),"
        "        substr(rest, 1, instr(rest, char(10))-1)"
        " from lines where instr(rest, char(10))>0)"
    )


# --------------------------------------------------------------------------
# No dlopen.  wasm32-wasi has no dynamic linker, so the build defines
# SQLITE_OMIT_LOAD_EXTENSION and the whole extension-loading surface goes with
# it: the `load_extension()` SQL function (sqlite3.c:126896, inside the #ifndef),
# the `.load` dot command (shell.c:16443), and the `edit()` function the shell
# registers only when it can shell out (shell.c:11455).
#
# This is the one group where "make it pass" and "make it work" pull hardest in
# opposite directions: a port could register a stub `load_extension()` that
# returns NULL and satisfy a weaker test.  Asserting the *absence* -- "no such
# function" -- is what makes a stub fail, and asserting `.dbconfig` alongside it
# means the port cannot flip the flag on either.
# --------------------------------------------------------------------------
def _extensions() -> list[Case]:
    cases = [
        _plat("ext.dbconfig", "plat.loadext.dbconfig", ".dbconfig load_extension"),
        _plat("ext.fn.one", "plat.loadext.function",
              "select load_extension('x');"),
        _plat("ext.fn.two", "plat.loadext.function",
              "select load_extension('x','sqlite3_x_init');"),
        _plat("ext.dot.load", "plat.loadext.dotcmd", ".load ./x"),
        _plat("ext.dot.load.entry", "plat.loadext.dotcmd",
              ".load ./x sqlite3_x_init"),
        # The dbconfig *flag* is independent of the compiled-out code, so turning
        # it on succeeds on both targets and reports "on" on both.  What must still
        # differ is what the flag then buys you: nothing, because there is no
        # function to authorise.  Measured alone, `.dbconfig load_extension on`
        # agrees with the reference and would belong in cases_shell; it is the third
        # line that makes this a platform claim, and it is the claim a port would
        # most like to fake.
        _plat("ext.enable", "plat.loadext.dbconfig",
              ".dbconfig load_extension on\n.dbconfig load_extension\n"
              "select load_extension('x');"),
        _plat("ext.compileoption", "plat.loadext.compileopt",
              "select sqlite_compileoption_used('SQLITE_OMIT_LOAD_EXTENSION');"),
        _plat("ext.edit", "plat.system.edit", "select edit('x');"),
        _plat("ext.edit.two", "plat.system.edit", "select edit('x','vi');"),
        # The function table, restated as a set.  `edit` and `load_extension` are
        # the two entries this build must not have; the rest of the shell's own
        # functions must all still be there, so a port cannot pass by dropping
        # everything.  Names only -- pragma_function_list also reports narg,
        # flags and enc, and those are upstream's business.
        _plat("ext.fn.list", "plat.loadext.fnlist",
              "select name from pragma_function_list where name in"
              " ('edit','load_extension','readfile','writefile','fsdir',"
              "'sqlar_compress','sqlar_uncompress','zipfile','zipfile_cds',"
              "'shell_add_schema','shell_module_schema','shell_putsnl')"
              " order by 1;"),
        _plat("ext.fn.count", "plat.loadext.fnlist",
              "select count(*) from pragma_function_list;"),
        # `.help load` has no entry to find (shell.c:12104 is inside the #ifndef),
        # so the lookup falls through to a full-text search over the remaining
        # help text and lands on `.open`'s --deserialize line.  Asserted as the
        # literal it is, because it is a direct consequence of the macro.
        _plat("ext.help.load", "plat.loadext.help", ".help load"),
    ]
    return cases

# --------------------------------------------------------------------------
# No permission bits.  This is the deepest of the WASI absences and the easiest to
# get wrong.  wasi-libc ships `chmod` as a stub that returns ENOTSUP (52) and
# `path_filestat_get` reports no permission bits at all, so a stat says only what
# *kind* of thing a path is: S_IFREG 32768, S_IFDIR 16384, S_IFLNK 40960, with the
# low nine bits zero.  Native reports 33188, 16877 and 41471 for the same three.
#
# Two consequences the port must get right rather than paper over:
#
#   * `writeFile()` calls chmod() *after* the bytes are on disk and returns 2 on
#     failure (shell.c:2467-2469), which would lose `.archive -x` entirely.  The
#     port must let a mode request succeed-and-be-ignored, not fail.
#   * `.archive -tv` renders the stored mode through lsmode() (shell.c:14290), so
#     the listing shows `----------` and `d---------`.  lsmode() itself is pure
#     arithmetic and portable -- it is asserted on both targets in cases_extensions -- so
#     what is platform-specific is only the *number* going into it.
#
# The mtime column of the same listing is NOT platform-specific: WASI does have
# `path_filestat_set_times`, so `writefile(name,data,mode,mtime)` really does set
# the stamp.  Those cases live in cases_extensions with recorded expectations; only
# the mode column is here.  The split is the general rule of this file -- a call WASI
# implements gets a differential case, a call it does not gets a literal.
# --------------------------------------------------------------------------
#: A tree laid down from inside the run so both mode and mtime are pinned, with
#: the directory stamped last (creating a file inside it bumps the directory).
_LAY = ("select writefile('p/a.txt','hello',420,1580000000);"
        "select writefile('p/b.txt','second',420,1580000000);"
        "select writefile('p',null,16877,1580000000);\n")

#: The same, with a single child.  `.archive -c p` stores entries in the order
#: fsdir yields them, which for the children of a directory is the host's readdir
#: order, and that order was measured to differ between ext4 and tmpfs.  Cases that
#: read the archive back through SQL can impose `order by`, but `.archive -tv`
#: prints rows in stored order and has no such knob, so the only way to make that
#: listing a function of the port rather than of the container's filesystem is to
#: leave no sibling pair to be ordered.  One directory and one file still exercise
#: both shapes lsmode() can print.  Note that the two-child form *passed* the
#: filesystem probe -- two names have only two possible orders and these two
#: coincided -- so this is a structural fix, not a reaction to a failure.  Directory
#: order is the host filesystem's, not the port's: probe.toml denies it to stage 3's
#: adversaries for the same reason it is engineered out here.
_LAY1 = ("select writefile('p/a.txt','hello',420,1580000000);"
         "select writefile('p',null,16877,1580000000);\n")


def _permissions() -> list[Case]:
    cases = [
        # What a stat reports, straight from fsdir.
        _plat("mode.file", "plat.mode.stat",
              "select writefile('f.txt','x',420);"
              "select name, mode from fsdir('f.txt');"),
        _plat("mode.dir", "plat.mode.stat",
              "select writefile('d',null,16877);"
              "select name, mode from fsdir('d');"),
        _plat("mode.symlink", "plat.mode.symlink",
              "select writefile('l.txt','target',41471);"
              "select name, mode from fsdir('l.txt');"),
        # chmod is a no-op rather than an error: the request must be accepted and
        # the bytes must survive it.  A port that propagates ENOTSUP fails here.
        _plat("mode.chmod.ignored", "plat.mode.chmod",
              "select writefile('f.txt','x');"
              "select writefile('f.txt','x',256);"
              "select mode from fsdir('f.txt');"
              "select cast(readfile('f.txt') as text);"),
        _plat("mode.chmod.executable", "plat.mode.chmod",
              "select writefile('f.txt','#!/bin/sh\n',493);"
              "select mode from fsdir('f.txt');"),
        _plat("mode.chmod.zero", "plat.mode.chmod",
              "select writefile('f.txt','x',0);"
              "select mode from fsdir('f.txt');"
              "select cast(readfile('f.txt') as text);"),
        # What the archive stores, and what extraction puts back.  Both sides of
        # the round trip are asserted because a port could get one right alone.
        _plat("mode.sqlar.store", "plat.mode.sqlar",
              f"{_LAY}.archive -c p\nselect name, mode from sqlar order by 1;",
              argv=(DB,)),
        _plat("mode.sqlar.extract", "plat.mode.sqlar",
              f"{_LAY}.archive -c p\n.archive -x -C out\n"
              "select name, mode from fsdir('out') order by 1;",
              argv=(DB,)),
        _plat("mode.zip.store", "plat.mode.zipfile",
              f"{_LAY}.archive -c -f a.zip p\n"
              "select name, mode from zipfile('a.zip') order by 1;",
              argv=(DB,)),
        # The listing, through lsmode().  Stamps are pinned so the date column is
        # a constant and the whole line can be asserted, alignment included.  This
        # is the one case that uses the single-child layout, for the reason given
        # at _LAY1: `-tv` prints in stored order and cannot be sorted.
        _plat("mode.listing", "plat.mode.listing",
              f"{_LAY1}.archive -c p\n.archive -tv", argv=(DB,)),
        # fsdir walking a tree it did not create: the modes come from the port's
        # stat of files the harness laid down, not from writefile.
        _plat("mode.fsdir.tree", "plat.mode.stat",
              "select name, mode from fsdir('src') order by 1;",
              argv=(DB,),
              files={"src/a.txt": "hello\n", "src/nested/c.txt": "deeper\n"}),
    ]
    return cases

# --------------------------------------------------------------------------
# No popen, no system.  WASI has no process creation at all, and upstream has a
# macro for each half:
#
#   SQLITE_OMIT_POPEN    replaces every popen() call site with the message
#                        "Error: pipes are not supported in this OS"
#                        (shell.c:12889 for .import, 16717 for .output/.once).
#   SQLITE_NOHAVE_SYSTEM removes .shell and .system (shell.c:17960), the edit()
#                        function (shell.c:11455), and the -e/-x handling inside
#                        .once/.excel (shell.c:16700-16713).
#
# The last one has a consequence worth asserting on its own.  `.excel` is
# rewritten to `.once -x` at shell.c:16677 -- *outside* the #ifndef -- so with the
# -x branch compiled out, `-x` falls through to output_file_open() as an ordinary
# filename.  The result is a file literally named `-x` in the working directory
# and nothing launched.  That is upstream's behaviour for this configuration, odd
# as it looks, and a port that "helpfully" errors instead has diverged.
# --------------------------------------------------------------------------
def _pipes() -> list[Case]:
    cases = [
        # .import from a pipe.  The message, then the shell carries on.
        _plat("pipe.import", "plat.popen.import",
              "create table t(a,b);\n.import '|echo 1,2' t\nselect count(*) from t;",
              argv=(DB,)),
        _plat("pipe.import.csv", "plat.popen.import",
              ".mode csv\ncreate table t(a,b);\n.import '|printf \"x,y\\n\"' t\n"
              "select count(*) from t;", argv=(DB,)),
        # .output and .once to a pipe: the message, and stdout stays usable
        # because shell.c sets p->out back to stdout on the way out.
        _plat("pipe.output", "plat.popen.output", ".output '|cat'\nselect 1;"),
        _plat("pipe.once", "plat.popen.output", ".once '|cat'\nselect 1;"),
        _plat("pipe.output.recover", "plat.popen.output",
              ".output '|cat'\nselect 1;\n.output stdout\nselect 2;"),
        # .shell and .system are simply not there.
        _plat("sys.shell", "plat.system.dotcmd", ".shell echo hi"),
        _plat("sys.system", "plat.system.dotcmd", ".system echo hi"),
        _plat("sys.shell.noargs", "plat.system.dotcmd", ".shell"),
        # .excel and .once -e/-x, which under NOHAVE_SYSTEM are filenames.
        # fsdir confirms the file exists and readfile confirms it holds the query
        # output in list mode -- not CSV, because the -x branch that would have
        # switched modes is compiled out.
        # `tempname` is carried by these four alone.  Under NOHAVE_SYSTEM the port
        # never launches a viewer, so its output holds no temp name and the
        # normaliser is a no-op on the scored path; the pre-migration build does
        # launch one and prints the random name from shell.c:5188, which is why
        # the DIFFER: comments above struck the digits out by hand.  See
        # normalise.tempname.
        _plat("sys.excel", "plat.system.excel",
              ".excel\nselect 1;\n.output stdout\n"
              "select name from fsdir('.') where name like '%-x' order by 1;"
              "select cast(readfile('-x') as text);", argv=(DB,),
              normalisers=("tempname",)),
        _plat("sys.once.dashx", "plat.system.excel",
              ".once -x\nselect 'a','b';\n.output stdout\n"
              "select cast(readfile('-x') as text);", argv=(DB,),
              normalisers=("tempname",)),
        _plat("sys.once.dashe", "plat.system.excel",
              ".once -e\nselect 1;\n.output stdout\n"
              "select cast(readfile('-e') as text);", argv=(DB,),
              normalisers=("tempname",)),
        # The double-dash strip at shell.c:16699 happens before the removed
        # branch, so `--x` becomes `-x` here too.
        _plat("sys.once.dashdashx", "plat.system.excel",
              ".once --x\nselect 1;\n.output stdout\n"
              "select cast(readfile('-x') as text);", argv=(DB,),
              normalisers=("tempname",)),
        # The help text with the entries removed.  `.help shell` finds nothing at
        # all; `.help system` falls through to a full-text search and matches
        # `.once`'s "Invoke system text editor" line.
        _plat("sys.help.shell", "plat.system.help", ".help shell"),
        _plat("sys.help.system", "plat.system.help", ".help system"),
        # The whole help text, structurally: the three removed commands must not
        # be listed, every other command must still be, and the total line count
        # pins the rest.  This is the cheapest place to notice a port that
        # restored a command by stubbing it -- the entry would come back.
        _plat("sys.help.absent", "plat.system.help",
              f".output h.txt\n.help\n.output stdout\n{_lines('h.txt')}"
              " select count(*) from lines where line like '.shell%'"
              " or line like '.system%' or line like '.load%';", argv=(DB,)),
        _plat("sys.help.lines", "plat.system.help",
              f".output h.txt\n.help\n.output stdout\n{_lines('h.txt')}"
              " select count(*) from lines where line is not null;", argv=(DB,)),
        _plat("sys.help.commands", "plat.system.help",
              f".output h.txt\n.help\n.output stdout\n{_lines('h.txt')}"
              " select count(*) from lines where line like '.%';", argv=(DB,)),
    ]
    return cases

# --------------------------------------------------------------------------
# The preopen contract.  A wasm32-wasi module has no ambient filesystem: it can
# reach exactly the directories the host handed it, and the invocation this
# benchmark fixes hands it three (see environment/scripts/wasi-run and
# runner.WasmTarget.argv):
#
#     --dir <sandbox>::/data     --dir <tmp>::/tmp     --dir <sandbox>::.
#
# The third is the sandbox again, mounted as ".", so that a relative path has
# something to resolve against -- the reference runs with cwd set to the sandbox,
# and without this every `.archive`, `fsdir` and `.import` case would be
# untestable.  It grants nothing extra; it is the same directory as the first.
#
# What is asserted here is narrower than "paths outside are rejected", because
# that is not what happens and claiming it would be a lie in the test suite.
# Measured: `writefile('/etc/x','payload')` *succeeds* and returns 7, and the
# bytes are then readable as both `/etc/x` and `etc/x` -- the same file, inside
# the sandbox.  An absolute path that is not a preopen root is resolved *into*
# the sandbox rather than refused.  So the three assertable properties are:
#
#   1. nothing outside the preopens is reachable -- a host file that certainly
#      exists reads back as the sandbox's version, or as NULL if absent;
#   2. `..` cannot escape, in any spelling, including through a symlink whose
#      target points out of the sandbox;
#   3. an out-of-sandbox absolute path is redirected, not refused.
#
# The native column for these is accidental: `readfile('../f')` is NULL on native
# only because no file called `f` happens to sit next to the sandbox directory.
# That is why these are literals and not recordings -- the reference agrees for a
# reason that has nothing to do with the property being asserted.
# --------------------------------------------------------------------------
def _sandbox() -> list[Case]:
    cases = [
        # A host file that exists on every Linux box, shadowed by a sandbox file
        # of the same name.  Under the port the sandbox copy is what comes back;
        # native reads the real one.  No host side effects either way.
        _plat("box.read.shadowed", "plat.sandbox.read",
              "select cast(readfile('/etc/hostname') as text);",
              files={"etc/hostname": "sandboxed\n"}),
        _plat("box.read.absent", "plat.sandbox.read",
              "select quote(readfile('/etc/hostname'));"),
        _plat("box.read.bin", "plat.sandbox.read",
              "select quote(readfile('/bin/sh'));"),
        _plat("box.read.proc", "plat.sandbox.read",
              "select quote(readfile('/proc/self/environ'));"),
        # `..`, in every spelling.  None of them may reach outside.
        _plat("box.dotdot.plain", "plat.sandbox.dotdot",
              "select quote(readfile('../f'));"),
        _plat("box.dotdot.sub", "plat.sandbox.dotdot",
              "select quote(readfile('sub/../../f'));"),
        _plat("box.dotdot.data", "plat.sandbox.dotdot",
              "select quote(readfile('/data/../f'));"),
        _plat("box.dotdot.root", "plat.sandbox.dotdot",
              "select quote(readfile('/../f'));"),
        _plat("box.dotdot.deep", "plat.sandbox.dotdot",
              "select quote(readfile('../../../../../../etc/hostname'));"),
        _plat("box.dotdot.write", "plat.sandbox.dotdot",
              "select writefile('../escaped.txt','x');"
              "select quote(readfile('../escaped.txt'));"),
        # A symlink is the one path resolution the guest does not perform itself,
        # so it is where a port that hands raw strings to the host would leak.
        # The link is created inside the sandbox and points out of it; reading
        # through it must not produce the host's file.
        _plat("box.symlink.abs", "plat.sandbox.symlink",
              "select writefile('link','/etc/hostname',41471);"
              "select quote(readfile('link'));"),
        _plat("box.symlink.dotdot", "plat.sandbox.symlink",
              "select writefile('link','../../../etc/hostname',41471);"
              "select quote(readfile('link'));"),
        # An out-of-sandbox absolute path is redirected into the sandbox.  Both
        # spellings name the same file, which is the property, and the return
        # value 7 says the write happened rather than being silently dropped.
        _plat("box.write.abs", "plat.sandbox.redirect",
              "select writefile('/etc/x','payload');"
              "select cast(readfile('/etc/x') as text);"
              "select cast(readfile('etc/x') as text);"),
        _plat("box.write.newroot", "plat.sandbox.redirect",
              "select writefile('/nowhere/deep/x','payload');"
              "select cast(readfile('nowhere/deep/x') as text);"),
        # The root the guest can see *is* the sandbox, so a walk of "/" and a walk
        # of "." return the same set.  Sorted, because fsdir walks in readdir
        # order and that is a host-filesystem property (see cases_extensions._lines).
        # A walk of "/" reaches the sandbox and nothing else, so it returns the
        # same *set* as a walk of "." -- the names differ only in the prefix fsdir
        # concatenates ("//src", because it joins dir + "/" + name).  The native
        # reference cannot answer this at all: it walks the real root and fails
        # with "cannot read directory:".  The "." half of the pair is portable and
        # lives in cases_extensions (ext.fsdir), recorded rather than written down.
        _plat("box.fsdir.root", "plat.sandbox.fsdir",
              "select name from fsdir('/') order by 1;",
              files={"src/a.txt": "hello\n"}),
        _plat("box.fsdir.outside", "plat.sandbox.fsdir",
              "select name from fsdir('/etc') order by 1;"),
        _plat("box.fsdir.dotdot", "plat.sandbox.fsdir",
              "select name from fsdir('..') order by 1;"),
        # Opening a database outside the sandbox, by all three routes.
        _plat("box.open.argv", "plat.sandbox.open",
              "select 1;", argv=("/etc/hostname",)),
        _plat("box.open.dot", "plat.sandbox.open",
              ".open /etc/hostname\nselect 1;"),
        _plat("box.open.dotdot", "plat.sandbox.open",
              ".open ../escape.db\nselect 1;"),
        _plat("box.attach", "plat.sandbox.attach",
              "attach '/etc/hostname' as x;"),
        _plat("box.attach.dotdot", "plat.sandbox.attach",
              "attach '../escape.db' as x;"),
    ]
    return cases

# --------------------------------------------------------------------------
# The VFS.  SQLITE_OS_OTHER=1 removes os_unix.c from the build, which is upstream's
# documented porting hook (sqlite3.c:14367) and takes the whole unix VFS family
# with it: `unix`, `unix-dotfile`, `unix-excl`, `unix-none`, `unix-nolock`.  In
# their place the port registers exactly one, and this benchmark pins its name to
# `wasi` -- the single entry in this file that is a benchmark decision rather than
# a platform fact.  A name has to be pinned for `.vfsname` and `-vfs` to be
# testable at all, and upstream does not choose one for an OS_OTHER build.
#
# What is deliberately NOT asserted, because the source leaves it to the port:
#
#   szOsFile      the size of the port's own file handle struct.
#   mxPathname    the longest path the port chooses to accept.
#   iVersion      2 is required for WAL, but it is asserted through *behaviour*
#                 (the WAL cases in cases_storage) rather than through this number.
#   the shm backing  whether xShmMap uses a real -shm file or process-local heap
#                 is a genuine choice under WASI, which has no mmap.  cases_storage
#                 asserts that WAL works and that a `-wal` file appears; it does
#                 not assert that a `-shm` file does.
#
# So `.vfslist` is filtered down to the zName lines before it is compared.  The
# two extra VFSes are the shell's own, registered by main() regardless of OS:
# `apndvfs` (shell.c:5148, the appendvfs extension) and `memdb` (shell.c:6273,
# backing `.open --hexdb` and deserialise).  Their presence is evidence the port
# did not disturb the shell's own registration order while replacing the OS layer.
# --------------------------------------------------------------------------
def _vfs() -> list[Case]:
    cases = [
        _plat("vfs.name", "plat.vfs.name",
              "create table t(a);\n.vfsname", argv=(DB,)),
        _plat("vfs.name.main", "plat.vfs.name",
              "create table t(a);\n.vfsname main", argv=(DB,)),
        # `.vfsname` on an in-memory database prints nothing on either target --
        # there is no file, so SQLITE_FCNTL_VFSNAME has nothing to report.  That is
        # portable and covered in cases_shell; it is not a platform claim.
        # The registered set, names only.
        _plat("vfs.list", "plat.vfs.list",
              f".output v.txt\n.vfslist\n.output stdout\n{_lines('v.txt')}"
              " select line from lines where line like 'vfs.zName%';",
              argv=(DB,)),
        _plat("vfs.list.count", "plat.vfs.list",
              f".output v.txt\n.vfslist\n.output stdout\n{_lines('v.txt')}"
              " select count(*) from lines where line like 'vfs.zName%';",
              argv=(DB,)),
        # The unix family is gone.  `argv[i]` is read past the end of the option
        # loop at shell.c:18907, so 3.31.1 prints the NULL rather than the name it
        # was given -- an upstream bug, and reproducing it is evidence the port
        # left the shell's option handling alone.
        _plat("vfs.unix", "plat.vfs.missing", "select 1;",
              argv=("-vfs", "unix", DB)),
        _plat("vfs.unix.dotfile", "plat.vfs.missing", "select 1;",
              argv=("-vfs", "unix-dotfile", DB)),
        _plat("vfs.unix.excl", "plat.vfs.missing", "select 1;",
              argv=("-vfs", "unix-excl", DB)),
        _plat("vfs.unix.none", "plat.vfs.missing", "select 1;",
              argv=("-vfs", "unix-none", DB)),
        # `unix-nolock` is deliberately not here.  It is registered only under
        # SQLITE_ENABLE_LOCKING_STYLE, which the release build does not set, so the
        # reference rejects it too -- measured identical.  It is a recorded case in
        # cases_shell (cli.flag.vfs.unregistered) instead, which is stronger: the
        # reference has to keep agreeing.
        # The pinned name has to actually work as a -vfs argument.
        _plat("vfs.explicit", "plat.vfs.name", "select 1;",
              argv=("-vfs", "wasi", DB)),
    ]
    return cases


# --------------------------------------------------------------------------
# The ABI: 32-bit pointers, no mmap, no threads.
#
# `SQLITE_MAX_MMAP_SIZE` is 0 for wasm32 by upstream's own platform list
# (sqlite3.c:14393-14404), so `pragma mmap_size` reports 0 and stays 0 however it
# is set.  That is not the port's choice and not something it may change.
#
# `pragma cache_spill` is the more interesting one: it reports a threshold in
# *pages*, derived from the cache byte budget divided by the per-page overhead,
# and that overhead is a struct full of pointers.  32-bit pointers make the
# per-page cost smaller, so more pages fit in the same budget and the number
# moves.  It is here as direct evidence of the pointer width, visible through an
# ordinary pragma.
#
# `SQLITE_THREADSAFE=0` because there are no pthreads in this build; `pragma
# threads` is then pinned at 0 and a request to raise it is ignored.
# --------------------------------------------------------------------------
def _abi() -> list[Case]:
    cases = [
        # The *default* is 0 on both targets -- the release build sets no
        # SQLITE_DEFAULT_MMAP_SIZE -- so reading it unset says nothing about the
        # platform and is covered in cases_engine's pragma sweep.  The claim is that it
        # cannot be raised, which is the next case.
        _plat("abi.mmap.set", "plat.abi.mmap",
              "pragma mmap_size=1048576;pragma mmap_size;", argv=(DB,)),
        _plat("abi.mmap.memory", "plat.abi.mmap", "pragma mmap_size;"),
        _plat("abi.cache_spill", "plat.abi.pointer", "pragma cache_spill;",
              argv=(DB,)),
        # Same shape as mmap: `pragma threads` reads 0 on both (the reference's
        # default worker count is 0 too), so only the attempt to raise it is a
        # platform claim.  The plain read is in cases_engine's _VALUE_PRAGMAS.
        _plat("abi.threads.set", "plat.abi.threads",
              "pragma threads=4;pragma threads;"),
        _plat("abi.threadsafe", "plat.abi.threads",
              "select sqlite_compileoption_used('THREADSAFE=0');"),
        # The build's own feature list, restated at run time.  The COMPILER row is
        # excluded: it names the toolchain, and pinning it would fail a port that
        # used a different wasi-sdk build -- which is its choice.  Its *presence*
        # is asserted separately, so a port
        # cannot remove the row to make the list match.
        _plat("abi.compile_options", "plat.abi.compileopts",
              "select * from pragma_compile_options"
              " where compile_options not like 'COMPILER=%' order by 1;"),
        # That exactly one COMPILER= row exists is asserted in cases_engine
        # (pragma.compile_options.compiler-present), where it is recorded: both
        # targets report one, and the point is only that a port cannot delete the
        # row to make the list above match.
        _plat("abi.compile_options.count", "plat.abi.compileopts",
              "select count(*) from pragma_compile_options;"),
    ]
    return cases


def build() -> list[Case]:
    """Every WASI-contract case, in a stable order."""
    cases: list[Case] = []
    for part in (_extensions, _permissions, _pipes, _sandbox, _vfs, _abi):
        cases.extend(part())
    return cases
