///|
test "CORS XML roundtrip handles entities and namespace without expanding markup" {
  let xml = "<?xml version=\"1.0\"?><CORSConfiguration xmlns='http://s3.amazonaws.com/doc/2006-03-01/'><CORSRule><ID>a&amp;b&#x1F680;</ID><AllowedOrigin>https://*.example.test</AllowedOrigin><AllowedMethod>PUT</AllowedMethod><AllowedHeader>x-amz-*</AllowedHeader><ExposeHeader>ETag</ExposeHeader><MaxAgeSeconds>300</MaxAgeSeconds></CORSRule></CORSConfiguration>"
  let rules = parse_cors_xml(xml).unwrap()
  assert_eq(rules[0].id, Some("a&b🚀"))
  assert_true(valid_cors_rules(rules))
  assert_eq(parse_cors_xml(cors_xml(rules)), Some(rules))
  for
    invalid in [
      "<!DOCTYPE x><CORSConfiguration></CORSConfiguration>", "<CORSConfigurationxmlns='http://s3.amazonaws.com/doc/2006-03-01/'></CORSConfiguration>",
      "<CORSConfiguration><CORSRule><ID>&unknown;</ID></CORSRule></CORSConfiguration>",
      "<CORSConfiguration><CORSRule><ID>one</ID><ID>two</ID></CORSRule></CORSConfiguration>",
      "<CORSConfiguration><CORSRule><ID>&#0;</ID></CORSRule></CORSConfiguration>",
      "<CORSConfiguration><CORSRule><ID><nested/></ID></CORSRule></CORSConfiguration>",
      "<CORSConfiguration></CORSConfiguration>trailing",
    ] {
    assert_eq(parse_cors_xml(invalid), None)
  }
}

///|
test "CORS validates rules and matches whole origins and headers" {
  let rule : CorsRule = {
    id: None,
    origins: ["https://*.example.test"],
    methods: ["PUT"],
    headers: ["X-Amz-*", "Content-Type"],
    expose: ["ETag"],
    max_age: Some(0),
  }
  assert_true(valid_cors_rules([rule]))
  assert_eq(
    match_cors([rule], "https://a.example.test", "PUT", [
      "x-amz-date", "content-type",
    ]),
    Some(rule),
  )
  assert_eq(match_cors([rule], "https://a.example.test.evil", "PUT", []), None)
  assert_eq(match_cors([rule], "https://a.example.test", "GET", []), None)
  assert_eq(
    match_cors([rule], "https://a.example.test", "PUT", ["authorization"]),
    None,
  )
  assert_eq(match_cors([rule], "", "PUT", []), None)
  assert_false(cors_pattern("a*b", "ab-extra"))
  assert_true(cors_pattern("*", "https://example.test"))
  assert_false(valid_cors_rules([]))
  for origins in [[""], ["https://**.test"], ["https://example.test\r\nX: bad"]] {
    assert_false(valid_cors_rules([{ ..rule, origins, }]))
  }
  assert_false(valid_cors_rules([{ ..rule, methods: ["OPTIONS"], }]))
  assert_false(valid_cors_rules([{ ..rule, expose: ["*"], }]))
  assert_false(valid_cors_rules([{ ..rule, headers: ["bad header"], }]))
  let identified = { ..rule, id: Some("duplicate"), }
  assert_false(valid_cors_rules([identified, identified]))
}
