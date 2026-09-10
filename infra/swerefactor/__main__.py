"""Entry point for ``python3 -m swerefactor``.

The package has no console_scripts entry point and no wrapper in ``infra/bin``,
deliberately: nothing here is installed, and every stage image puts the package
on ``PYTHONPATH`` at ``/opt/swerefactor`` instead.  ``-m`` is therefore the one
invocation that works identically on the host and inside all three stage images,
so it is the one the docs quote.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
