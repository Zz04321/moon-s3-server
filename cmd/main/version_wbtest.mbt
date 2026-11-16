///|
test "version transactions only reference safe storage identifiers" {
  let storage = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  assert_eq(
    parse_transaction("version\n" + storage + "\n-\n"),
    Some(PublishVersion(storage, None)),
  )
  assert_eq(
    parse_transaction("version\n" + storage + "\n" + storage + "\n"),
    Some(PublishVersion(storage, Some(storage))),
  )
  assert_eq(
    parse_transaction("remove-version\n" + storage + "\n"),
    Some(RemoveVersion(storage)),
  )
  for
    value in [
      "version\n../outside\n-\n",
      "remove-version\nnull\n",
      "version\n" + storage + "\n../uploads\n",
    ] {
    assert_eq(parse_transaction(value), None)
  }
  assert_true(valid_version_id("null"))
  assert_false(valid_version_id("../outside"))
}

///|
test "copy sources preserve version selection without allowing unrelated query parameters" {
  assert_eq(
    parse_versioned_copy_source("/source-bucket/key?versionId=null"),
    Some(("source-bucket", "key", Some("null"))),
  )
  assert_eq(
    parse_versioned_copy_source(
      "/source-bucket/key?versionId=null&versionId=null",
    ),
    None,
  )
  assert_eq(parse_versioned_copy_source("/source-bucket/key?other=null"), None)
}

///|
test "version records require canonical numbers and bounded multipart ETags" {
  assert_eq(version_number("9223372036854775807"), Some(9223372036854775807L))
  for
    value in ["", "01", "-1", "9223372036854775808", "99999999999999999999999"] {
    assert_eq(version_number(value), None)
  }
  let digest = "d41d8cd98f00b204e9800998ecf8427e"
  assert_true(valid_version_etag(digest))
  assert_true(valid_version_etag(digest + "-10000"))
  for value in [digest + "-0", digest + "-10001", digest + "-01", "<invalid>"] {
    assert_false(valid_version_etag(value))
  }
}
