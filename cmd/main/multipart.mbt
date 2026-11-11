///|
struct MultipartUpload {
  id : String
  key : String
  content_type : String
  initiated : Int64
}

///|
fn is_multipart_request(params : Array[@core.QueryParam]) -> Bool {
  params.any(param => {
    param.name == "uploads" ||
    param.name == "uploadId" ||
    param.name == "partNumber"
  })
}

///|
fn valid_upload_id(id : String) -> Bool {
  if id.length() != 64 {
    return false
  }
  id
  .to_array()
  .all(digit => (digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f'))
}

///|
fn upload_dir(config : Config, bucket : String, id : String) -> String {
  bucket_dir(config, bucket) + "/.uploads/" + id
}

///|
async fn load_upload(
  config : Config,
  bucket : String,
  id : String,
) -> MultipartUpload? {
  if !valid_upload_id(id) {
    return None
  }
  let text = read_text(upload_dir(config, bucket, id) + "/manifest")
  let key = match @core.filename_to_key(meta_value(text, "key")) {
    Some(key) => key
    None => return None
  }
  let initiated = match range_number(meta_value(text, "initiated")) {
    Some(initiated) => initiated
    None => return None
  }
  Some({ id, key, initiated, content_type: meta_value(text, "content-type"), })
}

///|
async fn cleanup_multipart(config : Config) -> Unit {
  let now = (@env.now() / 1000UL).reinterpret_as_int64()
  if now < config.multipart_cleanup_at {
    return
  }
  for bucket in @fs.readdir(config.root, include_hidden=false) {
    if !@core.valid_bucket_name(bucket) {
      continue
    }
    let base = bucket_dir(config, bucket) + "/.uploads"
    if !@fs.exists(base) {
      continue
    }
    for id in @fs.readdir(base, include_hidden=false) {
      if !valid_upload_id(id) {
        continue
      }
      match load_upload(config, bucket, id) {
        Some(upload) =>
          if upload.initiated <= now - config.multipart_ttl {
            @fs.rmdir(
              upload_dir(config, bucket, id),
              recursive=true,
              allow_missing=true,
            )
          }
        None =>
          if @fs.mtime(upload_dir(config, bucket, id)).0 <=
            now - config.multipart_ttl {
            @fs.rmdir(
              upload_dir(config, bucket, id),
              recursive=true,
              allow_missing=true,
            )
          }
      }
    }
  }
  config.multipart_cleanup_at = now + 3600L
}

///|
async fn part_digest(path : String) -> (String, Int64, FixedArray[Byte]) {
  let file = @fs.open(path, mode=ReadOnly)
  defer file.close()
  let hash = @crypto.MD5::new()
  while file.read_some(max_len=65536) is Some(chunk) {
    hash.update(chunk)
  }
  let digest = hash.finalize()
  (@crypto.bytes_to_hex_string(digest), file.size(), digest)
}

///|
async fn initiate_upload(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : @http.ServerConnection,
) -> Unit {
  let random = match @env.rand(32) {
    Some(random) => random
    None => {
      send_error(
        conn,
        500,
        "InternalError",
        "Could not create an upload identifier.",
        config.cors_origin,
      )
      return
    }
  }
  let id = @crypto.bytes_to_hex_string(random)
  let base = bucket_dir(config, bucket) + "/.uploads"
  @fs.mkdir(base, allow_exist=true)
  let directory = upload_dir(config, bucket, id)
  @fs.mkdir(directory)
  let content_type = match header(request.headers, "content-type") {
    "" => "application/octet-stream"
    value => value
  }
  @fs.write_file(
    directory + "/manifest",
    "key=" +
    @core.key_to_filename(key) +
    "\ncontent-type=" +
    content_type +
    "\ninitiated=" +
    (@env.now() / 1000UL).to_string() +
    "\n",
    sync=Full,
  )
  send_xml(
    conn,
    200,
    "OK",
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?><InitiateMultipartUploadResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Bucket>" +
    @core.xml_escape(bucket) +
    "</Bucket><Key>" +
    @core.xml_escape(key) +
    "</Key><UploadId>" +
    id +
    "</UploadId></InitiateMultipartUploadResult>",
    config.cors_origin,
  )
}

///|
async fn upload_part(
  config : Config,
  bucket : String,
  upload : MultipartUpload,
  number : Int32,
  body : Bytes,
  conn : @http.ServerConnection,
) -> Unit {
  let path = upload_dir(config, bucket, upload.id) +
    "/part." +
    number.to_string()
  let temporary = path + ".tmp"
  @fs.write_file(temporary, body, sync=Full)
  @fs.rename(temporary, path, replace=true)
  conn.send_response(200, "OK", extra_headers={
    "ETag": "\"" + @core.md5_hex(body.clamped_view()) + "\"",
    "Access-Control-Allow-Origin": config.cors_origin,
    "Access-Control-Expose-Headers": "ETag",
  })
  conn.end_response()
}

///|
async fn list_parts(
  config : Config,
  bucket : String,
  upload : MultipartUpload,
  params : Array[@core.QueryParam],
  conn : @http.ServerConnection,
) -> Unit {
  let maximum = parse_list_number(@core.query_value(params, "max-parts"), 1000)
  let marker = parse_list_number(
    @core.query_value(params, "part-number-marker"),
    0,
  )
  let (maximum, marker) = match (maximum, marker) {
    (Some(maximum), Some(marker)) =>
      (if maximum > 1000 { 1000 } else { maximum }, marker)
    _ => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Invalid part pagination parameters.",
        config.cors_origin,
      )
      return
    }
  }
  let directory = upload_dir(config, bucket, upload.id)
  let numbers : Array[Int32] = []
  for entry in @fs.readdir(directory, include_hidden=false) {
    if entry.has_prefix("part.") {
      match parse_list_number(Some(entry[5:].to_owned()), 0) {
        Some(number) =>
          if number > marker && number <= 10000 {
            numbers.push(number)
          }
        None => ()
      }
    }
  }
  numbers.sort()
  let count = if maximum < numbers.length() {
    maximum
  } else {
    numbers.length()
  }
  let truncated = maximum > 0 && count < numbers.length()
  let next = if count == 0 { marker } else { numbers[count - 1] }
  let mut xml = "<ListPartsResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Bucket>" +
    @core.xml_escape(bucket) +
    "</Bucket><Key>" +
    @core.xml_escape(upload.key) +
    "</Key><UploadId>" +
    upload.id +
    "</UploadId><PartNumberMarker>" +
    marker.to_string() +
    "</PartNumberMarker><NextPartNumberMarker>" +
    next.to_string() +
    "</NextPartNumberMarker><MaxParts>" +
    maximum.to_string() +
    "</MaxParts><IsTruncated>" +
    truncated.to_string() +
    "</IsTruncated>"
  for index in 0..<count {
    let number = numbers[index]
    let path = directory + "/part." + number.to_string()
    let (etag, size, _) = part_digest(path)
    xml += "<Part><PartNumber>" +
      number.to_string() +
      "</PartNumber><LastModified>" +
      iso_time(file_modified(path)) +
      "</LastModified><ETag>\"" +
      etag +
      "\"</ETag><Size>" +
      size.to_string() +
      "</Size></Part>"
  }
  send_xml(conn, 200, "OK", xml + "</ListPartsResult>", config.cors_origin)
}

///|
async fn complete_upload(
  config : Config,
  bucket : String,
  upload : MultipartUpload,
  body : Bytes,
  conn : @http.ServerConnection,
) -> Unit {
  let parts = if body.length() <= 2097152 {
    parse_completed_parts(@utf8.decode_lossy(body.clamped_view()))
  } else {
    None
  }
  let parts = match parts {
    Some(parts) => parts
    None => {
      send_error(
        conn,
        400,
        "MalformedXML",
        "Expected a non-empty CompleteMultipartUpload part list.",
        config.cors_origin,
      )
      return
    }
  }
  let directory = upload_dir(config, bucket, upload.id)
  let digests : Array[Byte] = []
  let mut previous = 0
  let mut total = 0L
  for (number, _) in parts {
    if number <= previous {
      send_error(
        conn,
        400,
        "InvalidPartOrder",
        "Parts must be in ascending order without duplicates.",
        config.cors_origin,
      )
      return
    }
    previous = number
  }
  for index in 0..<parts.length() {
    let (number, expected) = parts[index]
    let path = directory + "/part." + number.to_string()
    if !@fs.exists(path) {
      send_error(
        conn,
        400,
        "InvalidPart",
        "A requested part is missing.",
        config.cors_origin,
      )
      return
    }
    let (etag, size, digest) = part_digest(path)
    if expected != etag {
      send_error(
        conn,
        400,
        "InvalidPart",
        "A requested part ETag does not match.",
        config.cors_origin,
      )
      return
    }
    if index < parts.length() - 1 && size < 5242880L {
      send_error(
        conn,
        400,
        "EntityTooSmall",
        "All parts except the last must be at least 5 MiB.",
        config.cors_origin,
      )
      return
    }
    total += size
    if total > 5497558138880L {
      send_error(
        conn,
        400,
        "EntityTooLarge",
        "The completed object exceeds 5 TiB.",
        config.cors_origin,
      )
      return
    }
    for byte in digest {
      digests.push(byte)
    }
  }
  let etag = @core.md5_hex(Bytes::from_array(digests).clamped_view()) +
    "-" +
    parts.length().to_string()
  let assembled = directory + "/assembled"
  let file = @fs.create(assembled, allow_existing=true)
  defer file.close()
  for (number, _) in parts {
    let part = @fs.open(
      directory + "/part." + number.to_string(),
      mode=ReadOnly,
    )
    defer part.close()
    file.write_reader(part)
  }
  file.sync()
  let staged_meta = directory + "/assembled.meta"
  @fs.write_file(
    staged_meta,
    "etag=" + etag + "\ncontent-type=" + upload.content_type + "\n",
    sync=Full,
  )
  @fs.rename(assembled, object_file(config, bucket, upload.key), replace=true)
  @fs.rename(staged_meta, meta_file(config, bucket, upload.key), replace=true)
  @fs.rmdir(directory, recursive=true, allow_missing=true)
  send_xml(
    conn,
    200,
    "OK",
    "<CompleteMultipartUploadResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Location>/" +
    @core.xml_escape(bucket) +
    "/" +
    @core.xml_escape(@core.uri_encode(upload.key, allow_slash=true)) +
    "</Location><Bucket>" +
    @core.xml_escape(bucket) +
    "</Bucket><Key>" +
    @core.xml_escape(upload.key) +
    "</Key><ETag>\"" +
    etag +
    "\"</ETag></CompleteMultipartUploadResult>",
    config.cors_origin,
  )
}

///|
async fn list_uploads(
  config : Config,
  bucket : String,
  params : Array[@core.QueryParam],
  conn : @http.ServerConnection,
) -> Unit {
  if @core.query_value(params, "delimiter").unwrap_or("") != "" {
    send_error(
      conn,
      501,
      "NotImplemented",
      "Multipart upload listing does not support delimiter.",
      config.cors_origin,
    )
    return
  }
  let maximum = match
    parse_list_number(@core.query_value(params, "max-uploads"), 1000) {
    Some(value) => if value > 1000 { 1000 } else { value }
    None => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Invalid max-uploads.",
        config.cors_origin,
      )
      return
    }
  }
  let prefix = @core.query_value(params, "prefix").unwrap_or("")
  let key_marker = @core.query_value(params, "key-marker").unwrap_or("")
  let id_marker = @core.query_value(params, "upload-id-marker").unwrap_or("")
  let directory = bucket_dir(config, bucket) + "/.uploads"
  let uploads : Array[MultipartUpload] = []
  let now = (@env.now() / 1000UL).reinterpret_as_int64()
  if @fs.exists(directory) {
    for id in @fs.readdir(directory, include_hidden=false) {
      match load_upload(config, bucket, id) {
        Some(upload) => {
          if upload.initiated <= now - config.multipart_ttl {
            continue
          }
          let key_order = @utf8.encode(upload.key)
            .clamped_view()
            .lexical_compare(@utf8.encode(key_marker).clamped_view())
          if upload.key.has_prefix(prefix) &&
            (
              key_order > 0 ||
              (
                key_order == 0 &&
                !id_marker.is_empty() &&
                upload.id[:].lexical_compare(id_marker) > 0
              )
            ) {
            uploads.push(upload)
          }
        }
        None => ()
      }
    }
  }
  uploads.sort_by((left, right) => {
    let order = @utf8.encode(left.key)
      .clamped_view()
      .lexical_compare(@utf8.encode(right.key).clamped_view())
    if order == 0 {
      left.id[:].lexical_compare(right.id)
    } else {
      order
    }
  })
  let count = if maximum < uploads.length() {
    maximum
  } else {
    uploads.length()
  }
  let truncated = maximum > 0 && count < uploads.length()
  let mut xml = "<ListMultipartUploadsResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Bucket>" +
    @core.xml_escape(bucket) +
    "</Bucket><Prefix>" +
    @core.xml_escape(prefix) +
    "</Prefix><KeyMarker>" +
    @core.xml_escape(key_marker) +
    "</KeyMarker><UploadIdMarker>" +
    @core.xml_escape(id_marker) +
    "</UploadIdMarker><MaxUploads>" +
    maximum.to_string() +
    "</MaxUploads><IsTruncated>" +
    truncated.to_string() +
    "</IsTruncated>"
  if truncated {
    let next = uploads[count - 1]
    xml += "<NextKeyMarker>" +
      @core.xml_escape(next.key) +
      "</NextKeyMarker><NextUploadIdMarker>" +
      next.id +
      "</NextUploadIdMarker>"
  }
  for index in 0..<count {
    let upload = uploads[index]
    xml += "<Upload><Key>" +
      @core.xml_escape(upload.key) +
      "</Key><UploadId>" +
      upload.id +
      "</UploadId><Initiated>" +
      iso_time(upload.initiated) +
      "</Initiated><StorageClass>STANDARD</StorageClass></Upload>"
  }
  send_xml(
    conn,
    200,
    "OK",
    xml + "</ListMultipartUploadsResult>",
    config.cors_origin,
  )
}

///|
async fn handle_multipart(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String?,
  params : Array[@core.QueryParam],
  body : Bytes,
  conn : @http.ServerConnection,
) -> Unit {
  cleanup_multipart(config)
  let seen : Map[String, Bool] = Map([])
  for param in params {
    if seen.contains(param.name) {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Duplicate multipart query parameters.",
        config.cors_origin,
      )
      return
    }
    seen[param.name] = true
  }
  if !@fs.exists(objects_dir(config, bucket)) {
    send_error(
      conn,
      404,
      "NoSuchBucket",
      "The bucket does not exist.",
      config.cors_origin,
    )
    return
  }
  let has_uploads = @core.query_value(params, "uploads") is Some(_)
  let id = @core.query_value(params, "uploadId")
  let part_number = @core.query_value(params, "partNumber")
  if has_uploads && id is None && part_number is None {
    match (request.meth, key) {
      (Post, Some(key)) => initiate_upload(config, request, bucket, key, conn)
      (Get, None) => list_uploads(config, bucket, params, conn)
      _ =>
        send_error(
          conn,
          405,
          "MethodNotAllowed",
          "Invalid multipart operation.",
          config.cors_origin,
        )
    }
    return
  }
  let (id, key) = match (id, key) {
    (Some(id), Some(key)) => (id, key)
    _ => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "uploadId and an object key are required.",
        config.cors_origin,
      )
      return
    }
  }
  if has_uploads {
    send_error(
      conn,
      400,
      "InvalidArgument",
      "uploads and uploadId cannot be combined.",
      config.cors_origin,
    )
    return
  }
  let upload = match load_upload(config, bucket, id) {
    Some(upload) => upload
    None => {
      send_error(
        conn,
        404,
        "NoSuchUpload",
        "The multipart upload does not exist.",
        config.cors_origin,
      )
      return
    }
  }
  if upload.key != key {
    send_error(
      conn,
      404,
      "NoSuchUpload",
      "The multipart upload does not exist for this key.",
      config.cors_origin,
    )
    return
  }
  if upload.initiated <=
    (@env.now() / 1000UL).reinterpret_as_int64() - config.multipart_ttl {
    @fs.rmdir(
      upload_dir(config, bucket, id),
      recursive=true,
      allow_missing=true,
    )
    send_error(
      conn,
      404,
      "NoSuchUpload",
      "The multipart upload has expired.",
      config.cors_origin,
    )
    return
  }
  if request.meth is Put {
    let number = match parse_list_number(part_number, 0) {
      Some(number) => number
      None => 0
    }
    if number < 1 || number > 10000 {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "partNumber must be between 1 and 10000.",
        config.cors_origin,
      )
      return
    }
    upload_part(config, bucket, upload, number, body, conn)
    return
  }
  if part_number is Some(_) {
    send_error(
      conn,
      400,
      "InvalidArgument",
      "partNumber is only valid for UploadPart.",
      config.cors_origin,
    )
    return
  }
  match request.meth {
    Get => list_parts(config, bucket, upload, params, conn)
    Post => complete_upload(config, bucket, upload, body, conn)
    Delete => {
      @fs.rmdir(
        upload_dir(config, bucket, id),
        recursive=true,
        allow_missing=true,
      )
      conn.send_response(204, "No Content", extra_headers={
        "Access-Control-Allow-Origin": config.cors_origin,
      })
      conn.end_response()
    }
    _ =>
      send_error(
        conn,
        405,
        "MethodNotAllowed",
        "Invalid multipart operation.",
        config.cors_origin,
      )
  }
}
