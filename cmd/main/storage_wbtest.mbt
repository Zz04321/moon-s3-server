///|
test "transaction records accept only known operations and safe upload IDs" {
  assert_eq(parse_transaction("put\n"), Some(ReplaceObject(None)))
  assert_eq(parse_transaction("delete\n"), Some(RemoveObject))
  let id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  assert_eq(
    parse_transaction("put\n" + id + "\n"),
    Some(ReplaceObject(Some(id))),
  )
  for
    text in [
      "",
      "put",
      "delete",
      "unknown\n",
      "put\n../objects\n",
      "put\n\n",
      "delete\nextra\n",
      "put\n" + id,
    ] {
    assert_eq(parse_transaction(text), None)
  }
}
