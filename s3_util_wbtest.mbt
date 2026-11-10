///|
test "percent decoding and object-name round trip" {
  assert_eq(percent_decode("hello%20world%2Fdemo"), "hello world/demo")
  assert_eq(percent_decode("头像/文件.txt"), "头像/文件.txt")
  assert_eq(
    percent_decode("头像/%E6%96%87%E4%BB%B6.txt"),
    "头像/文件.txt",
  )
  assert_eq(percent_decode("a+b%2Bc"), "a+b+c")
  assert_eq(percent_decode("bad%2X%"), "bad%2X%")
  let key = "folder/头像 1.txt"
  let filename = key_to_filename(key)
  assert_eq(filename_to_key(filename), Some(key))
}

///|
test "bucket names follow the local compatibility profile" {
  assert_true(valid_bucket_name("avatars"))
  assert_true(valid_bucket_name("team-assets"))
  assert_false(valid_bucket_name("ab"))
  assert_false(valid_bucket_name("Upper"))
  assert_false(valid_bucket_name("bad_name"))
}

///|
test "query parameters are decoded and sorted for signatures" {
  let params = query_pairs("prefix=photos%2F&list-type=2&max-keys=10")
  assert_eq(query_value(params, "prefix"), Some("photos/"))
  assert_eq(canonical_query(params), "list-type=2&max-keys=10&prefix=photos%2F")
}

///|
test "S3 query canonicalization preserves literal and encoded plus signs" {
  let params = query_pairs("prefix=a+b%2Bc%20d&%2B=+&empty")
  assert_eq(query_value(params, "prefix"), Some("a+b+c d"))
  assert_eq(query_value(params, "+"), Some("+"))
  assert_eq(canonical_query(params), "%2B=%2B&empty=&prefix=a%2Bb%2Bc%20d")
  assert_eq(
    canonical_query(query_pairs("z=1&aa=2&a=z&a=aa")),
    "a=aa&a=z&aa=2&z=1",
  )
  assert_eq(
    split_bucket_key("/photos/头像%2B1.txt"),
    (Some("photos"), Some("头像+1.txt")),
  )
}

///|
test "XML output escapes user-controlled values" {
  assert_eq(xml_escape("a&b<c>\"d\""), "a&amp;b&lt;c&gt;&quot;d&quot;")
}

///|
test "hash helpers match standard vectors" {
  assert_eq(
    sha256_hex(@utf8.encode("abc").clamped_view()),
    "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
  )
  assert_eq(
    md5_hex(@utf8.encode("").clamped_view()),
    "d41d8cd98f00b204e9800998ecf8427e",
  )
  assert_eq(
    hmac_sha256_hex(
      @utf8.encode("key").clamped_view(),
      @utf8.encode("The quick brown fox jumps over the lazy dog").clamped_view(),
    ),
    "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8",
  )
}

///|
test "canonical headers preserve signed order and trim values" {
  let headers = [("host", " example.com "), ("x-amz-date", "20130524T000000Z")]
  assert_eq(
    canonical_headers("host;x-amz-date", headers),
    "host:example.com\nx-amz-date:20130524T000000Z\n",
  )
}

///|
test "AWS signing key matches the published derivation" {
  let key = signing_key(
    "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20130524", "us-east-1", "s3",
  )
  assert_eq(
    @crypto.bytes_to_hex_string(key),
    "f117494eff5d09da21cbf7f0339559ea04fc9582d31299cb992be70a6b27c97a",
  )
}
