///|
fn has_query_auth(params : Array[@core.QueryParam]) -> Bool {
  params.any(param => param.name.to_lower().has_prefix("x-amz-"))
}

///|
fn authorize_presigned(
  request : @http.Request,
  raw_path : String,
  params : Array[@core.QueryParam],
  body : Bytes,
  access_key : String,
  secret_key : String,
  now : Int64,
) -> Bool {
  if !(request.meth is Get || request.meth is Head || request.meth is Put) ||
    !header(request.headers, "authorization").is_empty() {
    return false
  }
  let (_, object_key) = @core.split_bucket_key(raw_path)
  if object_key is None {
    return false
  }
  let auth_params : Map[String, String] = Map([])
  let canonical_params : Array[@core.QueryParam] = []
  for param in params {
    if param.name.has_prefix("response-") {
      return false
    }
    if param.name.to_lower().has_prefix("x-amz-") {
      if auth_params.contains(param.name) {
        return false
      }
      match param.name {
        "X-Amz-Algorithm"
        | "X-Amz-Credential"
        | "X-Amz-Date"
        | "X-Amz-Expires"
        | "X-Amz-SignedHeaders"
        | "X-Amz-Signature" => auth_params[param.name] = param.value
        _ => return false
      }
    }
    if param.name != "X-Amz-Signature" {
      canonical_params.push(param)
    }
  }
  if auth_params.length() != 6 ||
    auth_params.get("X-Amz-Algorithm") != Some("AWS4-HMAC-SHA256") {
    return false
  }
  let date = auth_params.get("X-Amz-Date").unwrap_or("")
  let expires = auth_params.get("X-Amz-Expires").unwrap_or("")
  if !presigned_time_valid(date, expires, now) {
    return false
  }
  let credential = auth_params.get("X-Amz-Credential").unwrap_or("")
  let scope_parts = credential.split("/").map(part => part.to_owned()).collect()
  if scope_parts.length() != 5 ||
    scope_parts[0] != access_key ||
    scope_parts[1][:] != date[:8] ||
    scope_parts[2].is_empty() ||
    scope_parts[3] != "s3" ||
    scope_parts[4] != "aws4_request" {
    return false
  }
  let signed_headers = auth_params.get("X-Amz-SignedHeaders").unwrap_or("")
  let pairs : Array[(String, String)] = []
  let mut previous = ""
  let mut has_host = false
  for name in signed_headers.split(";") {
    if name.is_empty() || previous[:].lexical_compare(name) >= 0 {
      return false
    }
    for character in name.to_owned().to_array() {
      if !((character >= 'a' && character <= 'z') ||
        (character >= '0' && character <= '9') ||
        character == '-') {
        return false
      }
    }
    let value = header(request.headers, name.to_owned())
    if value.trim().is_empty() {
      return false
    }
    pairs.push((name.to_owned(), value))
    if name == "host" {
      has_host = true
    }
    previous = name.to_owned()
  }
  if !has_host {
    return false
  }
  let payload_hash = header(request.headers, "x-amz-content-sha256")
  if !payload_hash.is_empty() &&
    payload_hash != "UNSIGNED-PAYLOAD" &&
    payload_hash != @core.sha256_hex(body.clamped_view()) {
    return false
  }
  let signature = auth_params.get("X-Amz-Signature").unwrap_or("")
  if signature.length() != 64 {
    return false
  }
  let canonical_request = method_name(request.meth) +
    "\n" +
    raw_path +
    "\n" +
    @core.canonical_query(canonical_params) +
    "\n" +
    @core.canonical_headers(signed_headers, pairs) +
    "\n" +
    signed_headers +
    "\nUNSIGNED-PAYLOAD"
  let scope = scope_parts[1] + "/" + scope_parts[2] + "/s3/aws4_request"
  let string_to_sign = "AWS4-HMAC-SHA256\n" +
    date +
    "\n" +
    scope +
    "\n" +
    @core.sha256_hex(@utf8.encode(canonical_request).clamped_view())
  let key = @core.signing_key(secret_key, scope_parts[1], scope_parts[2], "s3")
  let expected = @core.hmac_sha256_hex(
    Bytes::from_array(key[:]).clamped_view(),
    @utf8.encode(string_to_sign).clamped_view(),
  )
  let mut difference = 0
  for index in 0..<expected.length() {
    difference = difference |
      (expected[index].to_int() ^ signature[index].to_int())
  }
  difference == 0
}
