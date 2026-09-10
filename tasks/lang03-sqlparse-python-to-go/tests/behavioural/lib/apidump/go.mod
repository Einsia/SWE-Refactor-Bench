// The API dumper is a module of its own, depending on nothing.
//
// It parses the submission's Go source with go/ast instead of loading it, so the
// interface contract can be graded even when the submission does not build --
// which is the case that matters most, because a submission that fails to
// compile would otherwise score zero on the interface half for a reason the
// interface half is not measuring.
//
// Keeping it out of the probe module matters for the same reason the probe is
// out of the submission: the probe module carries a `replace` pointing at the
// submission, so anything living there would stop being buildable the moment the
// submission does.
module apidump

go 1.25
