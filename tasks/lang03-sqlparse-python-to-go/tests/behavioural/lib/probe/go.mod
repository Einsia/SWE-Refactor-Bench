// The probe is its own module, and it never becomes part of the submission.
//
// A submission tree with a probe directory in it would be a submission the model
// can read, and reading the probe is reading the answers. Keeping the probe in a
// module of its own means the verifier can point it at the submission with a
// `replace` directive computed at grading time, compile it in a scratch
// directory, and leave the submission tree byte-identical to what was collected.
//
// The require line has no version because the replace target is a filesystem
// path; `v0.0.0` is the conventional placeholder Go accepts for a module that is
// only ever resolved locally. GOFLAGS=-mod=mod lets the toolchain write the
// resolved requirement without a checksum, and GOPROXY=off with GOSUMDB=off is
// what makes the whole build offline: a submission that adds a dependency does
// not fail to download it, it fails to build, which is the stdlib-only closure
// being enforced by the toolchain rather than by a grep.
module probe

go 1.25

require github.com/andialbrecht/sqlparse-go v0.0.0

replace github.com/andialbrecht/sqlparse-go => ../submission
