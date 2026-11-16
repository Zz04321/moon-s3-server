///|
test "webhook endpoints reject ambiguous authorities and header injection" {
  assert_eq(
    webhook_address("https://example.test/hooks/s3?source=moon"),
    Some(("https://example.test", "/hooks/s3?source=moon")),
  )
  assert_eq(
    webhook_address("http://127.0.0.1:8080"),
    Some(("http://127.0.0.1:8080", "/")),
  )
  for
    value in [
      "", "ftp://example.test", "http://", "http://user:password@example.test/path",
      "http://example.test:0", "http://example.test:65536", "http://example.test/path#fragment",
      "http://example.test\r\nHost:other", "http://example.test?query", "http://[::1]/hook",
    ] {
    assert_eq(webhook_address(value), None)
  }
}

///|
test "event journals retain legacy transactions and reject unsafe identifiers" {
  let id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  assert_eq(parse_event_journal("put\n"), Some((None, "put\n")))
  assert_eq(
    parse_event_journal("event\n" + id + "\ndelete\n"),
    Some((Some(id), "delete\n")),
  )
  for value in ["event\n../outbox\nput\n", "event\n", "event\nnull\nput\n"] {
    assert_eq(parse_event_journal(value), None)
  }
  assert_false(valid_event_body("{\"Records\":[]}", id))
  assert_false(valid_event_body("{\"Records\":[],\"Records\":[]}", id))
}

///|
test "queued events validate stable IDs and optional object metadata" {
  let id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  let body = "{\"Records\":[{\"eventId\":\"" +
    id +
    "\",\"eventSource\":\"moon:s3\",\"eventVersion\":\"2.1\",\"eventTime\":\"2026-11-16T12:00:00.000Z\",\"eventName\":\"ObjectCreated:Put\",\"s3\":{\"bucket\":{\"name\":\"event-bucket\",\"arn\":\"arn:aws:s3:::event-bucket\"},\"object\":{\"key\":\"hello+world%2Ffile\",\"size\":0,\"versionId\":\"null\",\"eTag\":\"d41d8cd98f00b204e9800998ecf8427e\"}}}]}"
  assert_true(valid_event_body(body, id))
  for
    invalid in [
      body.replace_all(old="\"null\"", new="\"../outside\""),
      body.replace_all(
        old="d41d8cd98f00b204e9800998ecf8427e",
        new="invalid-etag",
      ),
      body.replace_all(old="2026-11-16", new="2026-02-30"),
      body.replace_all(old="\"size\":0", new="\"size\":0.5"),
      body.replace_all(old="ObjectCreated:Put", new="Unknown"),
    ] {
    assert_false(valid_event_body(invalid, id))
  }
}
