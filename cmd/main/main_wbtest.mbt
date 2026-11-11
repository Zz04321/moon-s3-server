///|
test "list numbers reject malformed and overflowing input" {
  assert_eq(parse_list_number(None, 1000), Some(1000))
  assert_eq(parse_list_number(Some("0"), 1000), Some(0))
  assert_eq(parse_list_number(Some("0012"), 1000), Some(12))
  assert_eq(parse_list_number(Some("2147483647"), 0), Some(2147483647))
  for
    text in [
      "", "-1", "+1", "1.5", " 1", "1x", "2147483648", "4294967296", "999999999999999999999",
    ] {
    assert_eq(parse_list_number(Some(text), 1000), None)
  }
}

///|
test "download ranges support closed open and suffix forms" {
  assert_eq(download_range("bytes=2-5", 10L), Partial(2L, 5L))
  assert_eq(download_range("bytes=2-", 10L), Partial(2L, 9L))
  assert_eq(download_range("bytes=-3", 10L), Partial(7L, 9L))
  assert_eq(download_range("bytes=2-100", 10L), Partial(2L, 9L))
  assert_eq(download_range("bytes=-100", 10L), Partial(0L, 9L))
  assert_eq(
    download_range("bytes=-9999999999999999999999", 10L),
    Partial(0L, 9L),
  )
  for value in ["bytes=10-", "bytes=-0", "bytes=9999999999999999999999-"] {
    assert_eq(download_range(value, 10L), Unsatisfiable)
  }
  for value in ["bytes=0-", "bytes=-1", "bytes=0-0"] {
    assert_eq(download_range(value, 0L), Unsatisfiable)
  }
  for
    value in [
      "", "items=0-1", "bytes=5-2", "bytes=a-b", "bytes=0-1,3-4", "bytes=-",
    ] {
    assert_eq(download_range(value, 10L), Full)
  }
}

///|
test "ETag read conditions use strong and weak comparisons with precedence" {
  assert_eq(read_precondition("", "", "abc"), 200)
  assert_eq(read_precondition("*", "", "abc"), 200)
  assert_eq(read_precondition("\"other\", \"abc\"", "", "abc"), 200)
  assert_eq(read_precondition("W/\"abc\"", "", "abc"), 412)
  assert_eq(read_precondition("\"other\"", "*", "abc"), 412)
  assert_eq(read_precondition("", "*", "abc"), 304)
  assert_eq(read_precondition("", "W/\"abc\"", "abc"), 304)
  assert_eq(read_precondition("", "\"other\", \"abc\"", "abc"), 304)
  assert_eq(read_precondition("", "\"other\"", "abc"), 200)
}

///|
test "list entries group directories and filter start-after in UTF-8 order" {
  let keys = [
    "docs/a", "docs/nested/b", "docs/nested/c", "docs/", "root", "photos/a",
  ]
  assert_eq(list_entries(keys, "", "/", ""), [
    ("docs/", true),
    ("photos/", true),
    ("root", false),
  ])
  assert_eq(list_entries(keys, "docs/", "/", ""), [
    ("docs/", false),
    ("docs/a", false),
    ("docs/nested/", true),
  ])
  assert_eq(list_entries(keys, "docs/", "/", "docs/nested/"), [])
  assert_eq(list_entries(keys, "", "/", "docs/"), [
    ("photos/", true),
    ("root", false),
  ])
  assert_eq(list_entries(["aa", "z"], "", "", "aa"), [("z", false)])
  assert_eq(list_entries(["a::b", "a::c"], "", "::", ""), [("a::", true)])
}

///|
test "list URL encoding protects XML control characters and preserves slashes" {
  assert_eq(
    list_xml_value("目录/a+b &\u0001.txt", true),
    "%E7%9B%AE%E5%BD%95/a%2Bb%20%26%01.txt",
  )
  assert_eq(list_xml_value("a&b<c>", false), "a&amp;b&lt;c&gt;")
}

///|
test "signature time window validates calendar dates and exact boundaries" {
  let now = @time.PlainDateTime::of(2024, 3, 1).to_unix_second()
  for date in ["20240229T234500Z", "20240301T000000Z", "20240301T001500Z"] {
    assert_true(signature_time_valid(date, now))
  }
  for
    date in [
      "20240229T234459Z", "20240301T001501Z", "20230229T000000Z", "20241301T000000Z",
      "20240431T000000Z", "20240301T240000Z", "20240301T006000Z", "20240301T000060Z",
      "20240301T00000xZ", "20240301",
    ] {
    assert_false(signature_time_valid(date, now))
  }
}

///|
test "credentials must be configured together with non-empty values" {
  assert_true(valid_credentials(None, None))
  assert_true(valid_credentials(Some("access"), Some("secret")))
  assert_false(valid_credentials(Some("access"), None))
  assert_false(valid_credentials(None, Some("secret")))
  assert_false(valid_credentials(Some(""), Some("secret")))
  assert_false(valid_credentials(Some("access"), Some(" \t")))
  assert_false(valid_credentials(Some(""), Some("")))
}

///|
test "presigned expiry uses its lifetime instead of the header signing window" {
  let now = @time.PlainDateTime::of(2024, 3, 1).to_unix_second()
  assert_true(presigned_time_valid("20240229T000000Z", "604800", now))
  assert_true(presigned_time_valid("20240301T001500Z", "60", now))
  assert_false(presigned_time_valid("20240301T001501Z", "60", now))
  assert_true(presigned_time_valid("20240229T235900Z", "61", now))
  assert_false(presigned_time_valid("20240229T235900Z", "60", now))
  assert_false(presigned_time_valid("20230229T000000Z", "604800", now))
  for
    expires in [
      "", "0", "-1", "604801", "1.5", "2147483648", "99999999999999999",
    ] {
    assert_false(presigned_time_valid("20240301T000000Z", expires, now))
  }
}

///|
test "object times format in UTC and reject invalid HTTP dates" {
  let epoch = 0L
  assert_eq(iso_time(epoch), "1970-01-01T00:00:00.000Z")
  assert_eq(http_time(epoch), "Thu, 01 Jan 1970 00:00:00 GMT")
  let leap = @time.PlainDateTime::of(2024, 2, 29, hour=12, minute=34, second=56).to_unix_second()
  assert_eq(iso_time(leap), "2024-02-29T12:34:56.000Z")
  assert_eq(http_time(leap), "Thu, 29 Feb 2024 12:34:56 GMT")
  assert_eq(parse_http_time(http_time(leap)), Some(leap))
  for
    date in [
      "", "invalid", "Wed, 29 Feb 2024 12:34:56 GMT", "Thu, 29 Feb 2023 12:34:56 GMT",
      "Thu, 29 Feb 2024 24:34:56 GMT", "Thu, 29 Feb 2024 12:34:56 UTC",
    ] {
    assert_eq(parse_http_time(date), None)
  }
}

///|
test "date conditions have second precision and ETag conditions take precedence" {
  let modified = @time.PlainDateTime::of(2024, 2, 29).to_unix_second()
  let before = http_time(modified - 1L)
  let equal = http_time(modified)
  let after = http_time(modified + 1L)
  assert_eq(
    read_precondition("", "", "abc", if_modified_since=before, modified~),
    200,
  )
  assert_eq(
    read_precondition("", "", "abc", if_modified_since=equal, modified~),
    304,
  )
  assert_eq(
    read_precondition("", "", "abc", if_modified_since=after, modified~),
    304,
  )
  assert_eq(
    read_precondition("", "", "abc", if_unmodified_since=before, modified~),
    412,
  )
  assert_eq(
    read_precondition("", "", "abc", if_unmodified_since=equal, modified~),
    200,
  )
  assert_eq(
    read_precondition("", "", "abc", if_unmodified_since=after, modified~),
    200,
  )
  assert_eq(
    read_precondition(
      "\"abc\"",
      "",
      "abc",
      if_unmodified_since=before,
      modified~,
    ),
    200,
  )
  assert_eq(
    read_precondition(
      "",
      "\"other\"",
      "abc",
      if_modified_since=after,
      modified~,
    ),
    200,
  )
  assert_eq(
    read_precondition(
      "",
      "",
      "abc",
      if_unmodified_since="invalid",
      if_modified_since="invalid",
      modified~,
    ),
    200,
  )
  assert_true(if_range_matches(equal, "abc", modified))
  assert_false(if_range_matches(before, "abc", modified))
  assert_false(if_range_matches(after, "abc", modified))
  assert_false(if_range_matches("invalid", "abc", modified))
}
