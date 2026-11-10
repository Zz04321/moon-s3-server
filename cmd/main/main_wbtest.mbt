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
