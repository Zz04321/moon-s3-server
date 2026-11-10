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
