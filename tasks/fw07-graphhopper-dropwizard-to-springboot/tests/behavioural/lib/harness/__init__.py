"""fw07 stage-2 harness.

The split between these modules follows the one question each answers:

  corpus     what gets asked
  wire       how it is asked (an HTTP client that does not help)
  server     how a jar is started, without knowing which framework built it
  maven      how a tree is built and which jar came out
  capture    running the corpus against both sides once, and the ledger
  normalize  what a difference means (measured volatility, header maps)
  diff       the single comparison every graded case goes through
  cli        the command-line surface, graded on exit codes and effects

Nothing here reads /workspace/repo.  The capture holds responses; there is no
path from a graded check to the submission's source.  That is stage 2's
primitive, enforced by what the data structures contain.
"""
