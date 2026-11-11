///|
test "copy sources decode bucket and key once and reject versioned sources" {
  assert_eq(
    parse_copy_source("/source-bucket/folder/a%2Bb%20%25%3F%23"),
    Some(("source-bucket", "folder/a+b %?#")),
  )
  assert_eq(
    parse_copy_source("source-bucket/%252F"),
    Some(("source-bucket", "%2F")),
  )
  for
    value in [
      "", "/", "/source-bucket", "/source-bucket/", "/bad_bucket/key", "/source-bucket/key?versionId=x",
      "/source-bucket/key#fragment", "https://example.test/bucket/key",
    ] {
    assert_eq(parse_copy_source(value), None)
  }
}
