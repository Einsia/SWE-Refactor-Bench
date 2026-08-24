The credentials file the --auth-file sessions are measured with.

All four line forms miniserve's parser accepts, one each, on purpose:

    joe:pw123           plain
    bob:sha256:<hex>    sha256 of "pw123"
    sue:sha512:<hex>    sha512 of "pw123"
    bill:               empty password, which the parser accepts deliberately
                        ("the spec does not forbid it") and which is therefore
                        part of the contract

Every user shares the same password so a session can walk all four without
carrying four secrets, while still going down four different comparison paths in
the server: a string compare, two hash compares of different widths, and the
empty case that a port written with `if password.is_empty() { reject }` breaks.

Lives outside /workspace/repo for the same reason as the TLS material: the
submission must not be able to edit what it is graded against.
