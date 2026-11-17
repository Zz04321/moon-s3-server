///|
test "batch deletion XML validates the entire bounded document" {
  assert_eq(
    parse_delete_objects(
      "<Delete><Object><Key>a&amp;b</Key></Object><Quiet>true</Quiet><Object><VersionId>null</VersionId><Key> c </Key></Object></Delete>",
    ),
    Some(([("a&b", None), (" c ", Some("null"))], true)),
  )
  assert_eq(
    parse_delete_objects(
      "<?xml version=\"1.0\"?><Delete xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Object><Key>&#x4E2D;</Key></Object></Delete>",
    ),
    Some(([("中", None)], false)),
  )
  for
    source in [
      "<Delete/>", "<Delete></Delete>", "<DeleteExtra><Object><Key>a</Key></Object></Delete>",
      "<Delete><Object><Key>a</Key><Key>b</Key></Object></Delete>", "<Delete><Object><Key>a</Key><VersionId></VersionId></Object></Delete>",
      "<Delete><Object><Key>a</Key><ETag>x</ETag></Object></Delete>", "<Delete><Object><Key>&external;</Key></Object></Delete>",
      "<Delete><Object><Key>a</Key></Object><Quiet>true</Quiet><Quiet>false</Quiet></Delete>",
      "<!DOCTYPE Delete><Delete><Object><Key>a</Key></Object></Delete>", "<Delete><Object><Key>a</Key></Object></Delete>garbage",
      "<Delete><Object><Key></Key></Object></Delete>",
    ] {
    assert_eq(parse_delete_objects(source), None)
  }
  let entry = "<Object><Key>a</Key></Object>"
  assert_true(
    parse_delete_objects("<Delete>" + entry.repeat(1000) + "</Delete>")
    is Some(_),
  )
  assert_eq(
    parse_delete_objects("<Delete>" + entry.repeat(1001) + "</Delete>"),
    None,
  )
}
