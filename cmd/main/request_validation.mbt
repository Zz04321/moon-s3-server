///|
fn strict_decode(value : StringView) -> String? {
  let bytes = @utf8.encode(value.to_owned())
  let decoded : Array[Byte] = []
  let mut index = 0
  while index < bytes.length() {
    if bytes[index] == b'%' {
      if index + 2 >= bytes.length() {
        return None
      }
      let hex = bytes[index + 1:index + 3]
      if !hex
        .iter()
        .all(byte => {
          (byte >= b'0' && byte <= b'9') ||
          (byte >= b'a' && byte <= b'f') ||
          (byte >= b'A' && byte <= b'F')
        }) {
        return None
      }
      fn digit(byte : Byte) -> Int32 {
        if byte <= b'9' {
          byte.to_int() - 48
        } else if byte <= b'F' {
          byte.to_int() - 55
        } else {
          byte.to_int() - 87
        }
      }
      decoded.push(
        (digit(bytes[index + 1]) * 16 + digit(bytes[index + 2])).to_byte(),
      )
      index += 3
    } else {
      decoded.push(bytes[index])
      index += 1
    }
  }
  Some(@utf8.decode(Bytes::from_array(decoded).clamped_view())) catch {
    _ => None
  }
}

///|
fn valid_target(target : String) -> Bool {
  let (path, query) = @core.split_raw_path(target)
  if !path.has_prefix("/") || path.contains("#") || strict_decode(path) is None {
    return false
  }
  for part in query.split("&") {
    let (name, value) = match part.split_once("=") {
      Some(pair) => pair
      None => (part, "")
    }
    if strict_decode(name) is None || strict_decode(value) is None {
      return false
    }
  }
  true
}

///|
fn valid_object_key(key : String) -> Bool {
  let size = @utf8.encode(key).length()
  size >= 1 && size <= 1024
}

///|
fn unsupported_query(params : Array[@core.QueryParam]) -> Bool {
  params.any(param => {
    ![
      "cors", "versioning", "versions", "versionId", "policy", "uploads", "uploadId",
      "partNumber", "list-type", "prefix", "delimiter", "start-after", "continuation-token",
      "encoding-type", "max-keys", "max-parts", "part-number-marker", "max-uploads",
      "key-marker", "version-id-marker", "upload-id-marker", "X-Amz-Algorithm", "X-Amz-Credential",
      "X-Amz-Date", "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature", "x-id",
    ].contains(param.name)
  })
}

///|
fn unsupported_headers(request : @http.Request) -> Bool {
  request.headers
  .keys()
  .any(name => {
    let name = name.to_string().to_lower()
    name.has_prefix("x-amz-server-side-encryption") ||
    name.has_prefix("x-amz-copy-source-server-side-encryption") ||
    (
      name.has_prefix("x-amz-checksum-") &&
      name != "x-amz-checksum-crc32" &&
      !(name == "x-amz-checksum-mode" &&
      (request.meth is Get || request.meth is Head) &&
      header(request.headers, name) == "ENABLED")
    ) ||
    (
      name == "x-amz-sdk-checksum-algorithm" &&
      header(request.headers, name) != "CRC32"
    ) ||
    name == "x-amz-acl" ||
    name.has_prefix("x-amz-grant-") ||
    name.has_prefix("x-amz-meta-") ||
    name.has_prefix("x-amz-object-lock-") ||
    name == "x-amz-tagging" ||
    name == "x-amz-tagging-directive" ||
    name == "x-amz-storage-class" ||
    name == "x-amz-trailer" ||
    name == "x-amz-decoded-content-length" ||
    name == "x-amz-website-redirect-location" ||
    name == "x-amz-security-token"
  })
}

///|
fn crc32_base64(body : Bytes) -> String {
  let mut checksum = 0xffffffffU
  for byte in body {
    checksum = checksum ^ byte.to_uint()
    for _ in 0..<8 {
      checksum = if (checksum & 1U) == 1U {
        (checksum >> 1) ^ 0xedb88320U
      } else {
        checksum >> 1
      }
    }
  }
  checksum = checksum ^ 0xffffffffU
  @base64.encode(
    Bytes::from_array([
      (checksum >> 24).to_byte(),
      (checksum >> 16).to_byte(),
      (checksum >> 8).to_byte(),
      checksum.to_byte(),
    ]).clamped_view(),
  )
}

///|
fn valid_operation_query(
  request : @http.Request,
  params : Array[@core.QueryParam],
) -> Bool {
  let mut allowed : Array[String] = []
  if params.any(param => ["cors", "policy", "versioning"].contains(param.name)) {
    allowed = ["cors", "policy", "versioning"]
  } else if @core.query_value(params, "versions") is Some(_) {
    allowed = [
      "versions", "prefix", "delimiter", "encoding-type", "max-keys", "key-marker",
      "version-id-marker",
    ]
  } else if is_multipart_request(params) {
    allowed = [
      "uploads", "uploadId", "partNumber", "prefix", "delimiter", "max-uploads",
      "key-marker", "upload-id-marker", "max-parts", "part-number-marker",
    ]
  } else {
    let (path, _) = @core.split_raw_path(request.path)
    let (_, key) = @core.split_bucket_key(path)
    if key is Some(_) {
      allowed = ["versionId"]
    } else if request.meth is Get {
      allowed = [
        "list-type", "prefix", "delimiter", "start-after", "continuation-token",
        "encoding-type", "max-keys",
      ]
    }
  }
  params.all(param => {
    allowed.contains(param.name) ||
    param.name == "x-id" ||
    param.name.has_prefix("X-Amz-")
  })
}

///|
fn valid_request_headers(request : @http.Request) -> Bool {
  let mut size = @utf8.encode(request.path).length()
  for name, value in request.headers {
    size += @utf8.encode(name.to_string()).length() +
      @utf8.encode(value).length() +
      4
    if size > 16384 ||
      value
      .to_array()
      .any(character => {
        (character.to_int() < 32 && character != '\t') ||
        character.to_int() == 127
      }) {
      return false
    }
  }
  @utf8.encode(header(request.headers, "content-type")).length() <= 1024 &&
  !(request.headers.contains(@http.CaseInsensitiveString("content-length")) &&
  request.headers.contains(@http.CaseInsensitiveString("transfer-encoding")))
}

///|
async fn check_write_precondition(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : ResponseConnection,
) -> Bool {
  let matching = header(request.headers, "if-match")
  let absent = header(request.headers, "if-none-match")
  if matching.is_empty() && absent.is_empty() {
    return true
  }
  if !absent.is_empty() && absent != "*" {
    send_error(
      conn,
      400,
      "InvalidArgument",
      "Conditional writes support If-None-Match: * only.",
      config.cors_origin,
    )
    return false
  }
  recover_object(config, bucket, key)
  let versions = all_key_versions(config, bucket, key)
  let (_, query) = @core.split_raw_path(request.path)
  let selected = @core.query_value(@core.query_pairs(query), "versionId")
  let versions = match selected {
    Some(id) => versions.filter(version => version.id == id)
    None => versions
  }
  let exists = !versions.is_empty() && !versions[0].marker
  if (
      !matching.is_empty() &&
      (!exists || !etag_matches(matching, versions[0].etag, weak=false))
    ) ||
    (absent == "*" && exists) {
    send_error(
      conn,
      412,
      "PreconditionFailed",
      "The object does not satisfy the write conditions.",
      config.cors_origin,
    )
    return false
  }
  true
}
