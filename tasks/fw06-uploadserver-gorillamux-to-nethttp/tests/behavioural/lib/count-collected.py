#!/usr/bin/env python3
"""Read `pytest --collect-only -q` output on stdin, print the number collected.

Exists because that output has two shapes and which one you get depends on the
pytest version and on the verbosity:

    modules/routing/test_routing.py: 116     <- per file, what -q gives on 8.x/9.x
    478 tests collected in 0.64s             <- the summary line, quieter settings

A `sed` that matches only the second prints nothing when pytest emits the first,
and an empty string compares unequal to any expected count -- so the assertion
that was meant to catch a collection error instead fails every build, or, with
the comparison written the other way round, passes every build.  Measured: this
suite's pytest.ini carries `-q` in addopts, so 8.3.4 emits only the per-file form.

Prints a single integer and nothing else.  Prints 0 rather than failing when the
output matches neither shape, because the caller's business is comparing the
number to an expected one and "0" is the answer that makes that comparison fail
loudly.
"""
import re
import sys

text = sys.stdin.read()

summary = re.search(r"(\d+) tests? collected", text)
if summary:
    print(summary.group(1))
else:
    per_file = re.findall(r"^\S+\.py: (\d+)$", text, re.M)
    print(sum(int(n) for n in per_file))
