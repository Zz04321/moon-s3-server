///|
struct RequestBody {
  path : String?
  mut data : Bytes
  mut buffered_size : Int32
  mut staged_size : Int32
  mut md5 : String
  mut sha256 : String
  mut crc32 : String
}

///|
fn RequestBody::new(
  config : Config,
  request : @http.Request,
  params : Array[@core.QueryParam],
  identifier : Int64,
) -> RequestBody {
  let (path, _) = @core.split_raw_path(request.path)
  let (bucket, key) = @core.split_bucket_key(path)
  let streaming = request.meth is Put &&
    bucket is Some(_) &&
    key is Some(_) &&
    !request.headers.contains(@http.CaseInsensitiveString("x-amz-copy-source")) &&
    !unsupported_query(params) &&
    valid_operation_query(request, params)
  {
    path: if streaming {
      Some(config.root + "/.request-" + identifier.to_string() + ".tmp")
    } else {
      None
    },
    data: b"",
    buffered_size: 0,
    staged_size: 0,
    md5: "",
    sha256: "",
    crc32: "",
  }
}

///|
fn crc32_update(checksum : UInt32, body : Bytes) -> UInt32 {
  let mut checksum = checksum
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
  checksum
}

///|
fn crc32_encoded(checksum : UInt32) -> String {
  let checksum = checksum ^ 0xffffffffU
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
async fn RequestBody::receive(
  self : RequestBody,
  config : Config,
  request : @http.Request,
  body : &@io.Reader,
  conn : ResponseConnection,
) -> Bool {
  let file = match self.path {
    Some(path) =>
      Some(
        @fs.open(path, mode=WriteOnly, create_mode=CreateNew, permission=0o600),
      )
    None => None
  }
  defer (if file is Some(file) { file.close() })
  let window = if self.path is Some(_) {
    if config.max_request_bytes < 16384 {
      config.max_request_bytes
    } else {
      16384
    }
  } else {
    0
  }
  if window > config.max_buffered_bytes - config.runtime.buffered_bytes {
    conn.trace.close_after = true
    send_error(
      conn,
      503,
      "SlowDown",
      "No upload buffer window is available.",
      config.cors_origin,
    )
    return false
  }
  config.runtime.buffered_bytes += window
  defer {
    config.runtime.buffered_bytes -= window
  }
  let md5 = @crypto.MD5::new()
  let sha256 = @crypto.SHA256::new()
  let checksum_requested = request.headers.contains(
      @http.CaseInsensitiveString("x-amz-checksum-crc32"),
    ) ||
    header(request.headers, "x-amz-sdk-checksum-algorithm") == "CRC32"
  let mut crc32 = 0xffffffffU
  let buffer = Buffer()
  let mut size = 0
  while body.read_some(max_len=if window > 0 { window } else { 16384 })
        is Some(chunk) {
    let too_large = chunk.length() > config.max_request_bytes - size
    let exhausted = if file is Some(_) {
      chunk.length() > config.max_staged_bytes - config.runtime.staged_bytes
    } else {
      chunk.length() > config.max_buffered_bytes - config.runtime.buffered_bytes
    }
    if too_large || exhausted {
      conn.trace.close_after = true
      send_error(
        conn,
        if too_large {
          413
        } else {
          503
        },
        if too_large {
          "EntityTooLarge"
        } else {
          "SlowDown"
        },
        "Request body exceeds the request or shared resource limit.",
        config.cors_origin,
      )
      return false
    }
    size += chunk.length()
    md5.update(chunk)
    sha256.update(chunk)
    if checksum_requested {
      crc32 = crc32_update(crc32, chunk)
    }
    match file {
      Some(file) => {
        self.staged_size += chunk.length()
        config.runtime.staged_bytes += chunk.length()
        file.write(chunk)
      }
      None => {
        self.buffered_size += chunk.length()
        config.runtime.buffered_bytes += chunk.length()
        buffer.write_bytes(chunk.clamped_view())
      }
    }
  }
  if file is Some(file) {
    file.sync()
  } else {
    self.data = buffer.to_bytes()
  }
  self.md5 = @crypto.bytes_to_hex_string(md5.finalize())
  self.sha256 = @crypto.bytes_to_hex_string(sha256.finalize())
  self.crc32 = crc32_encoded(crc32)
  true
}
