///|
fn parse_copy_source(value : String) -> (String, String)? {
  if value.contains("?") || value.contains("#") {
    return None
  }
  let (bucket, key) = @core.split_bucket_key(value)
  match (bucket, key) {
    (Some(bucket), Some(key)) =>
      if @core.valid_bucket_name(bucket) && !key.is_empty() {
        Some((bucket, key))
      } else {
        None
      }
    _ => None
  }
}

///|
fn copy_source_authorized(config : Config, request : @http.Request) -> Bool {
  if config.access_key is None {
    return true
  }
  let (_, query) = @core.split_raw_path(request.path)
  let params = @core.query_pairs(query)
  let signed = if has_query_auth(params) {
    @core.query_value(params, "X-Amz-SignedHeaders").unwrap_or("")
  } else {
    match @core.parse_authorization(header(request.headers, "authorization")) {
      Some(auth) => auth.signed_headers
      None => ""
    }
  }
  let names = signed.split(";").to_array()
  for name, _ in request.headers {
    let name = name.to_string().to_lower()
    if (name.has_prefix("x-amz-copy-") || name == "x-amz-metadata-directive") &&
      !names.any(signed => signed == name) {
      return false
    }
  }
  true
}

///|
async fn copy_object(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  body : Bytes,
  conn : @http.ServerConnection,
) -> Unit {
  if !copy_source_authorized(config, request) {
    send_error(
      conn,
      403,
      "AccessDenied",
      "Copy headers must be signed.",
      config.cors_origin,
    )
    return
  }
  let (source_bucket, source_key) = match
    parse_copy_source(header(request.headers, "x-amz-copy-source")) {
    Some(source) => source
    None => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Expected an encoded bucket/key copy source without a version ID.",
        config.cors_origin,
      )
      return
    }
  }
  let directive = match header(request.headers, "x-amz-metadata-directive") {
    "" | "COPY" => "COPY"
    "REPLACE" => "REPLACE"
    _ => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Metadata directive must be COPY or REPLACE.",
        config.cors_origin,
      )
      return
    }
  }
  if !body.is_empty() {
    send_error(
      conn,
      400,
      "InvalidRequest",
      "CopyObject does not accept a request body.",
      config.cors_origin,
    )
    return
  }
  for name, _ in request.headers {
    let name = name.to_string().to_lower()
    if name.has_prefix("x-amz-meta-") ||
      name == "x-amz-copy-source-range" ||
      name == "x-amz-tagging" ||
      name == "x-amz-tagging-directive" ||
      name == "x-amz-acl" ||
      name.has_prefix("x-amz-grant-") ||
      name == "x-amz-checksum-algorithm" ||
      name == "x-amz-storage-class" ||
      name.has_prefix("x-amz-server-side-encryption") ||
      name.has_prefix("x-amz-copy-source-server-side-encryption") {
      send_error(
        conn,
        501,
        "NotImplemented",
        "This copy option is not supported.",
        config.cors_origin,
      )
      return
    }
  }
  if !@fs.exists(objects_dir(config, bucket)) ||
    !@fs.exists(objects_dir(config, source_bucket)) {
    send_error(
      conn,
      404,
      "NoSuchBucket",
      "The source or destination bucket does not exist.",
      config.cors_origin,
    )
    return
  }
  recover_object(config, source_bucket, source_key)
  let source_path = object_file(config, source_bucket, source_key)
  if !@fs.exists(source_path) {
    send_error(
      conn,
      404,
      "NoSuchKey",
      "The source object does not exist.",
      config.cors_origin,
    )
    return
  }
  let source = @fs.open(source_path, mode=ReadOnly)
  defer source.close()
  if source.size() > 5368709120L {
    send_error(
      conn,
      400,
      "EntityTooLarge",
      "CopyObject supports sources up to 5 GiB.",
      config.cors_origin,
    )
    return
  }
  let (source_etag, source_content_type) = object_meta(
    config, source_bucket, source_key,
  )
  let condition = read_precondition(
    header(request.headers, "x-amz-copy-source-if-match"),
    header(request.headers, "x-amz-copy-source-if-none-match"),
    source_etag,
    if_modified_since=header(
      request.headers,
      "x-amz-copy-source-if-modified-since",
    ),
    if_unmodified_since=header(
      request.headers,
      "x-amz-copy-source-if-unmodified-since",
    ),
    modified=source.mtime().0,
  )
  if condition != 200 {
    send_error(
      conn,
      412,
      "PreconditionFailed",
      "The copy source does not satisfy the conditions.",
      config.cors_origin,
    )
    return
  }
  if bucket == source_bucket && key == source_key && directive == "COPY" {
    send_error(
      conn,
      400,
      "InvalidRequest",
      "Copying an object onto itself requires REPLACE metadata.",
      config.cors_origin,
    )
    return
  }
  let content_type = if directive == "COPY" {
    source_content_type
  } else {
    match header(request.headers, "content-type") {
      "" => "application/octet-stream"
      value => value
    }
  }
  prepare_object(config, bucket, key)
  let destination = @fs.create(object_file(config, bucket, key) + ".pending")
  defer destination.close()
  let digest = @crypto.MD5::new()
  while source.read_some(max_len=65536) is Some(chunk) {
    digest.update(chunk)
    destination.write(chunk)
  }
  destination.sync()
  let etag = @crypto.bytes_to_hex_string(digest.finalize())
  let modified = destination.mtime().0
  commit_object(config, bucket, key, etag, content_type)
  send_xml(
    conn,
    200,
    "OK",
    "<CopyObjectResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><LastModified>" +
    iso_time(modified) +
    "</LastModified><ETag>\"" +
    etag +
    "\"</ETag></CopyObjectResult>",
    config.cors_origin,
  )
}
