///|
test "strict target decoding preserves valid unicode and rejects invalid escapes" {
  assert_eq(
    strict_decode("头像/%E6%96%87%E4%BB%B6.txt"),
    Some("头像/文件.txt"),
  )
  assert_eq(strict_decode("a+b%2Bc"), Some("a+b+c"))
  for value in ["%FF", "%FE", "%C0%AF", "%ED%A0%80", "%", "%2", "%ZZ"] {
    assert_eq(strict_decode(value), None)
  }
  assert_true(valid_target("/bucket/头像?prefix=%E6%96%87"))
  assert_true(!valid_target("/bucket/key?prefix=%FF"))
}

///|
test "long keys have deterministic short storage names without changing legacy names" {
  assert_eq(storage_filename("hello"), "68656c6c6f")
  let short = "a".repeat(117)
  let long = short + "b"
  assert_eq(storage_filename(short), @core.key_to_filename(short))
  assert_eq(
    storage_filename(long),
    "k-" + @core.sha256_hex(@utf8.encode(long).clamped_view()),
  )
  assert_true(valid_object_key("界".repeat(341) + "a"))
  assert_true(!valid_object_key("界".repeat(342)))
}

///|
test "CRC32 uses the S3 big endian Base64 representation" {
  assert_eq(crc32_base64(b"123456789"), "y/Q5Jg==")
  assert_eq(crc32_base64(b""), "AAAAAA==")
}
