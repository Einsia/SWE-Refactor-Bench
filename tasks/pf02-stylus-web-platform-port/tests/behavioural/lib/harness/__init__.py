"""Shared machinery for the pf02 behavioural modules.

The stage's design in one paragraph: every module compiles the same stylesheets
twice -- once through the submission, once through a frozen State A built into
this image -- and compares the bytes that come out.  Nothing here reads the
submission's source text, and nothing here asserts that a particular identifier
exists in it; what a source file says is stage 1's question, and stage 1 has both
trees and a reviewer for it.

    layout        where everything is, and the two virtual roots
    cases         the upstream stylesheets and the options each is compiled with
    vfs           the in-memory filesystem handed to the realm
    runners       the three drivers: realm core, node adapter, State A
    optionmatrix  option combinations the plain sweep never reaches
    pathvectors   POSIX path algebra vectors
    resolution    purpose-built @import trees
    builtinfns    per-function probes for the built-in library
    jsapi         the JS API surface, call shape included
    cli           command-line invocations, exit codes and written files
    errors        diagnostics: message, position, and what is thrown
    sourcemaps    source maps and data URIs
"""
