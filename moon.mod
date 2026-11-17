// Learn more about moon.mod configuration:
// https://docs.moonbitlang.com/en/latest/toolchain/moon/module.html
//
// To add a dependency, run this command in your terminal:
//   moon add moonbitlang/x
//
// Or manually declare it in `import`, for example:
// import {
//   "moonbitlang/x@0.4.6",
// }

name = "Zz04321/moon-s3-server"

version = "0.1.0"

readme = "README.mbt.md"

repository = "https://github.com/zzSmall6/moon-s3-server"

license = "Apache-2.0"

keywords = [ "s3", "object-storage", "http", "native" ]

preferred_target = "native"

description = "A small, deployable S3-compatible object storage server written in MoonBit."

import {
  "moonbitlang/async@0.22.9",
  "moonbitlang/x@0.5.7",
}
