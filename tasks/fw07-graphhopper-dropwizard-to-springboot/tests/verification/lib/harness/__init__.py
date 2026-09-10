"""Stage 2's harness, the three modules stage 3 needs, unmodified.

  wire    how a question is asked -- an HTTP client that does not help
  maven   how a tree is built, which jar came out, and what is removed first
  server  how a jar is started, without knowing which framework built it

All three are byte-identical to `tests/behavioural/lib/harness/`.  That identity is
what makes a stage-3 finding mean anything: a divergence found by a different
client, or on a server booted by a different command, is a divergence between two
harnesses rather than between two repositories.  Every earlier draft of this file
that "simplified" one of the three produced exactly that.

Where the identity is actually enforced, because this docstring named the wrong
place for a while and the invariant broke behind it.  The image build asserts a
pinned sha256 per file (Dockerfile, assertion 2), but its build context is
tests/verification, so tests/behavioural is not there to compare against: those
constants pin THESE copies against a local edit and cannot see the files they are
meant to equal.  Stage 2 adopted swerefactor.contract's submission_env in maven.py
and server.py, these copies kept `dict(os.environ)`, and every pin passed while the
two were twelve lines apart.  The comparison is in infra/tests/test_layout.py --
`test_a_harness_module_copied_between_stages_stays_identical` -- which is the only
place both directories exist at once.

The stage 2 modules NOT copied, and why not:

  corpus     781 lines naming 88 fixed cases.  Stage 3's job is to find the
             eighty-ninth, so the list is not what it needs -- and a third copy
             would be a third thing to keep in step.  What an adversary actually
             needs from it is knowing where stage 2 already looked, so that six
             rounds are not spent re-deriving it; that is in prompt.txt, as prose.
  capture    a ledger of a fixed corpus run.  There is no fixed corpus here.
  normalize  measured volatility masking.  Stage 3 gets the same protection by a
             different route: `reruns = 3` with `require_deterministic`, so a
             candidate that keys on `info.took` or a `Date` header fails to
             reproduce and is rejected before scope is even considered.
  diff       compares two recorded answers.  A candidate compares them itself,
             in whatever way its claim requires.
  cli        stage 2's command-line module.  Out of scope here: probe.toml's
             deny list excludes argparse4j's usage text, and a candidate cannot
             run commands at all.

Nothing here reads the submission's source.  A candidate gets servers and the
bytes they answer with; the two trees are what the ADVERSARY reads, through the
read-only toolbox, before it writes a candidate.
"""
