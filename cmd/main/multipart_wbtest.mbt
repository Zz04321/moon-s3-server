///|
test "multipart completion XML accepts SDK layouts and validates structure" {
  let etag = "d41d8cd98f00b204e9800998ecf8427e"
  assert_eq(
    parse_completed_parts(
      "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>\"" +
      etag +
      "\"</ETag></Part></CompleteMultipartUpload>",
    ),
    Some([(1, etag)]),
  )
  assert_eq(
    parse_completed_parts(
      "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<CompleteMultipartUpload xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">\n<Part><ETag>&quot;" +
      etag +
      "&quot;</ETag><PartNumber>10000</PartNumber></Part>\n</CompleteMultipartUpload>",
    ),
    Some([(10000, etag)]),
  )
  for
    text in [
      "",
      "<CompleteMultipartUpload/>",
      "<CompleteMultipartUpload></CompleteMultipartUpload>",
      "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber></Part></CompleteMultipartUpload>",
      "<CompleteMultipartUpload><Part><PartNumber>0</PartNumber><ETag>" +
      etag +
      "</ETag></Part></CompleteMultipartUpload>",
      "<CompleteMultipartUpload><Part><PartNumber>10001</PartNumber><ETag>" +
      etag +
      "</ETag></Part></CompleteMultipartUpload>",
      "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><PartNumber>2</PartNumber></Part></CompleteMultipartUpload>",
      "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>bad</ETag></Part></CompleteMultipartUpload>",
      "<CompleteMultipartUpload><Part><PartNumber>1</PartNumber><ETag>" +
      etag +
      "</ETag><Extra/></Part></CompleteMultipartUpload>",
      "<!DOCTYPE x><CompleteMultipartUpload></CompleteMultipartUpload>",
    ] {
    assert_eq(parse_completed_parts(text), None)
  }
}

///|
test "upload identifiers cannot escape the upload directory" {
  assert_true(
    valid_upload_id(
      "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ),
  )
  for
    value in [
      "", "../objects", "/tmp", "123", "G123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ] {
    assert_false(valid_upload_id(value))
  }
}
