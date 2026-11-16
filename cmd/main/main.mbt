///|
struct Config {
  root : String
  listen : String
  access_key : String?
  secret_key : String?
  cors_origin : String
  mutation_lock : @async.Mutex
  multipart_ttl : Int64
  mut multipart_cleanup_at : Int64
  webhook_url : String?
  webhook_secret : String?
  webhook_timeout : Int32
}

///|
fn env_or(name : String, fallback : String) -> String {
  match @env.get_env_var(name) {
    Some(value) => value
    None => fallback
  }
}

///|
fn load_config() -> Config {
  let access_key = @env.get_env_var("MOON_S3_ACCESS_KEY")
  let secret_key = @env.get_env_var("MOON_S3_SECRET_KEY")
  Config::{
    root: env_or("MOON_S3_ROOT", "./data"),
    listen: env_or("MOON_S3_LISTEN", "127.0.0.1:9000"),
    access_key,
    secret_key,
    cors_origin: env_or("MOON_S3_CORS_ORIGIN", "*"),
    mutation_lock: @async.Mutex(),
    multipart_ttl: range_number(
      env_or("MOON_S3_MULTIPART_TTL_SECONDS", "604800"),
    ).unwrap_or(0L),
    multipart_cleanup_at: 0L,
    webhook_url: @env.get_env_var("MOON_S3_WEBHOOK_URL"),
    webhook_secret: @env.get_env_var("MOON_S3_WEBHOOK_SECRET"),
    webhook_timeout: parse_list_number(
      Some(env_or("MOON_S3_WEBHOOK_TIMEOUT_MS", "5000")),
      0,
    ).unwrap_or(0),
  }
}

///|
fn header(
  headers : Map[@http.CaseInsensitiveString, String],
  name : String,
) -> String {
  match headers.get(@http.CaseInsensitiveString(name)) {
    Some(value) => value
    None => ""
  }
}

///|
fn method_name(verb : @http.RequestMethod) -> String {
  match verb {
    Get => "GET"
    Head => "HEAD"
    Post => "POST"
    Put => "PUT"
    Delete => "DELETE"
    Options => "OPTIONS"
    Patch => "PATCH"
    _ => "OTHER"
  }
}

///|
async fn send_xml(
  conn : ResponseConnection,
  status : Int32,
  reason : String,
  body : String,
  cors_origin : String,
) -> Unit {
  conn.send_response(status, reason, extra_headers={
    "Content-Type": "application/xml",
    "Access-Control-Allow-Origin": cors_origin,
  })
  conn.write(body)
  conn.end_response()
}

///|
async fn send_error(
  conn : ResponseConnection,
  status : Int32,
  code : String,
  message : String,
  cors_origin : String,
) -> Unit {
  let body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" +
    "<Error><Code>" +
    @core.xml_escape(code) +
    "</Code><Message>" +
    @core.xml_escape(message) +
    "</Message><RequestId>moon-s3-server</RequestId></Error>"
  send_xml(conn, status, code, body, cors_origin)
}

///|
fn authorize(
  config : Config,
  request : @http.Request,
  raw_path : String,
  params : Array[@core.QueryParam],
  body : Bytes,
) -> Bool {
  match config.access_key {
    None => true
    Some(access_key) =>
      match config.secret_key {
        None => false
        Some(secret_key) => {
          if has_query_auth(params) {
            return authorize_presigned(
              request,
              raw_path,
              params,
              body,
              access_key,
              secret_key,
              (@env.now() / 1000UL).reinterpret_as_int64(),
            )
          }
          if !signature_time_valid(
              header(request.headers, "x-amz-date"),
              (@env.now() / 1000UL).reinterpret_as_int64(),
            ) {
            return false
          }
          let auth_value = header(request.headers, "authorization")
          if auth_value.is_empty() {
            return false
          }
          match @core.parse_authorization(auth_value) {
            None => false
            Some(auth) => {
              let pairs : Array[(String, String)] = []
              for item in auth.signed_headers.split(";") {
                let name = item.to_owned()
                pairs.push((name, header(request.headers, name)))
              }
              let payload_hash = match
                header(request.headers, "x-amz-content-sha256") {
                "" => @core.sha256_hex(body.clamped_view())
                value => value
              }
              if payload_hash != "UNSIGNED-PAYLOAD" &&
                payload_hash != @core.sha256_hex(body.clamped_view()) {
                return false
              }
              @core.verify_sigv4(
                method_name(request.meth),
                raw_path,
                params,
                pairs,
                payload_hash,
                auth_value,
                access_key,
                secret_key,
              )
            }
          }
        }
      }
  }
}

///|
fn bucket_dir(config : Config, bucket : String) -> String {
  config.root + "/" + bucket
}

///|
fn objects_dir(config : Config, bucket : String) -> String {
  bucket_dir(config, bucket) + "/objects"
}

///|
fn object_file(config : Config, bucket : String, key : String) -> String {
  objects_dir(config, bucket) + "/" + @core.key_to_filename(key) + ".object"
}

///|
fn meta_file(config : Config, bucket : String, key : String) -> String {
  objects_dir(config, bucket) + "/" + @core.key_to_filename(key) + ".meta"
}

///|
async fn read_text(path : String) -> String {
  let data = @fs.read_file(path) catch { _ => return "" }
  @utf8.decode_lossy(data.binary().clamped_view())
}

///|
fn meta_value(text : String, name : String) -> String {
  let prefix = name + "="
  for line in text.split("\n") {
    if line.has_prefix(prefix) {
      return line[prefix.length():].to_owned()
    }
  }
  ""
}

///|
async fn object_meta(
  config : Config,
  bucket : String,
  key : String,
) -> (String, String) {
  let path = meta_file(config, bucket, key)
  if !(@fs.exists(path) catch { _ => false }) {
    return ("", "application/octet-stream")
  }
  let text = @utf8.decode_lossy(@fs.read_file(path).binary().clamped_view())
  (meta_value(text, "etag"), meta_value(text, "content-type"))
}

///|
async fn put_object(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  body : Bytes,
  conn : ResponseConnection,
) -> Unit {
  if !@fs.exists(bucket_dir(config, bucket)) {
    send_error(
      conn,
      404,
      "NoSuchBucket",
      "The bucket does not exist.",
      config.cors_origin,
    )
    return
  }
  let etag = @core.md5_hex(body.clamped_view())
  let content_type = match header(request.headers, "content-type") {
    "" => "application/octet-stream"
    value => value
  }
  try {
    prepare_object(config, bucket, key)
    @fs.write_file(
      object_file(config, bucket, key) + ".pending",
      body,
      sync=Full,
    )
    conn.set_version(commit_object(config, bucket, key, etag, content_type))
  } catch {
    error => {
      storage_error(config, conn, error)
      return
    }
  }
  conn.send_response(200, "OK", extra_headers={
    "ETag": "\"" + etag + "\"",
    "Access-Control-Allow-Origin": config.cors_origin,
  })
  conn.end_response()
}

///|
async fn check_read_precondition(
  config : Config,
  request : @http.Request,
  etag : String,
  modified : Int64,
  conn : ResponseConnection,
) -> Bool {
  let status = read_precondition(
    header(request.headers, "if-match"),
    header(request.headers, "if-none-match"),
    etag,
    if_modified_since=header(request.headers, "if-modified-since"),
    if_unmodified_since=header(request.headers, "if-unmodified-since"),
    modified~,
  )
  if status == 200 {
    return true
  }
  let headers : Map[@http.CaseInsensitiveString, String] = {
    "ETag": "\"" + etag + "\"",
    "Last-Modified": http_time(modified),
    "Access-Control-Expose-Headers": "ETag, Last-Modified, Content-Range, Accept-Ranges",
    "Access-Control-Allow-Origin": config.cors_origin,
  }
  let error_body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Error><Code>PreconditionFailed</Code><Message>At least one precondition failed.</Message><RequestId>moon-s3-server</RequestId></Error>"
  if status == 412 {
    headers["Content-Type"] = "application/xml"
    headers["Content-Length"] = @utf8.encode(error_body).length().to_string()
  }
  conn.send_response(
    status,
    if status == 304 {
      "Not Modified"
    } else {
      "Precondition Failed"
    },
    extra_headers=headers,
  )
  if status == 412 && !(request.meth is Head) {
    conn.write(error_body)
  }
  conn.end_response()
  false
}

///|
async fn get_object(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : ResponseConnection,
) -> Unit {
  let snapshot = open_versioned_read(config, request, bucket, key, conn) catch {
    error => {
      storage_error(config, conn, error)
      return
    }
  }
  let snapshot = match snapshot {
    Some(snapshot) => snapshot
    None => return
  }
  let file = snapshot.file
  let etag = snapshot.etag
  let content_type = snapshot.content_type
  defer file.close()
  let modified = snapshot.modified
  if !check_read_precondition(config, request, etag, modified, conn) {
    return
  }
  let size = file.size()
  let if_range = header(request.headers, "if-range")
  let selected_range = if request.meth is Get &&
    if_range_matches(if_range, etag, modified) {
    download_range(header(request.headers, "range"), size)
  } else {
    Full
  }
  match selected_range {
    Full => ()
    Unsatisfiable => {
      conn.send_response(416, "Requested Range Not Satisfiable", extra_headers={
        "Content-Range": "bytes */" + size.to_string(),
        "Content-Length": "0",
        "Access-Control-Allow-Origin": config.cors_origin,
      })
      conn.end_response()
      return
    }
    Partial(start, end) => {
      conn.send_response(206, "Partial Content", extra_headers={
        "Last-Modified": http_time(modified),
        "Access-Control-Expose-Headers": "ETag, Last-Modified, Content-Range, Accept-Ranges",
        "Content-Type": content_type,
        "ETag": "\"" + etag + "\"",
        "Content-Length": (end - start + 1L).to_string(),
        "Content-Range": "bytes " +
        start.to_string() +
        "-" +
        end.to_string() +
        "/" +
        size.to_string(),
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": config.cors_origin,
      })
      let mut position = start
      while position <= end {
        let remaining = end - position + 1L
        let length = if remaining > 65536L { 65536 } else { remaining.to_int() }
        conn.write(file.read_exactly_at(length, position~))
        position += length.to_int64()
      }
      conn.end_response()
      return
    }
  }
  conn.send_response(200, "OK", extra_headers={
    "Last-Modified": http_time(modified),
    "Access-Control-Expose-Headers": "ETag, Last-Modified, Content-Range, Accept-Ranges",
    "Content-Type": content_type,
    "ETag": "\"" + etag + "\"",
    "Content-Length": size.to_string(),
    "Accept-Ranges": "bytes",
    "Access-Control-Allow-Origin": config.cors_origin,
  })
  if request.meth is Get {
    conn.write_reader(file)
  }
  conn.end_response()
}

///|
async fn head_object(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : ResponseConnection,
) -> Unit {
  get_object(config, request, bucket, key, conn)
}

///|
async fn delete_object(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : ResponseConnection,
) -> Unit {
  let handled = delete_version(config, request, bucket, key, conn) catch {
    error => {
      storage_error(config, conn, error)
      return
    }
  }
  if handled {
    return
  }
  if @fs.exists(objects_dir(config, bucket)) {
    try {
      prepare_object(config, bucket, key)
      let path = object_file(config, bucket, key)
      let event = if config.webhook_url is Some(_) && @fs.exists(path) {
        let (etag, _) = object_meta(config, bucket, key)
        object_event(
          config,
          bucket,
          key,
          "ObjectRemoved:Delete",
          None,
          etag,
          @fs.file_size(path),
        )
      } else {
        None
      }
      publish_transaction(config, bucket, key, "delete\n", event?)
    } catch {
      error => {
        storage_error(config, conn, error)
        return
      }
    }
  }
  conn.send_response(204, "No Content", extra_headers={
    "Access-Control-Allow-Origin": config.cors_origin,
  })
  conn.end_response()
}

///|
async fn create_bucket(
  config : Config,
  bucket : String,
  conn : ResponseConnection,
) -> Unit {
  if !@core.valid_bucket_name(bucket) {
    send_error(
      conn,
      400,
      "InvalidBucketName",
      "The bucket name is not valid.",
      config.cors_origin,
    )
    return
  }
  let path = bucket_dir(config, bucket)
  if @fs.exists(path) {
    send_error(
      conn,
      409,
      "BucketAlreadyExists",
      "The bucket already exists.",
      config.cors_origin,
    )
    return
  }
  @fs.mkdir(path, recursive=true, allow_exist=true)
  @fs.mkdir(objects_dir(config, bucket), recursive=true, allow_exist=true)
  @fs.write_file(path + "/.created", (@env.now() / 1000UL).to_string())
  conn.send_response(200, "OK", extra_headers={
    "Access-Control-Allow-Origin": config.cors_origin,
  })
  conn.end_response()
}

///|
async fn delete_bucket(
  config : Config,
  bucket : String,
  conn : ResponseConnection,
) -> Unit {
  let dir = bucket_dir(config, bucket)
  recover_bucket(config, bucket) catch {
    error => {
      storage_error(config, conn, error)
      return
    }
  }
  if !@fs.exists(dir) {
    send_error(
      conn,
      404,
      "NoSuchBucket",
      "The bucket does not exist.",
      config.cors_origin,
    )
    return
  }
  let objects = @fs.readdir(
    objects_dir(config, bucket),
    include_hidden=false,
    sort=true,
  )
  if !objects.is_empty() {
    send_error(
      conn,
      409,
      "BucketNotEmpty",
      "The bucket is not empty.",
      config.cors_origin,
    )
    return
  }
  let uploads = bucket_dir(config, bucket) + "/.uploads"
  let history = bucket_dir(config, bucket) + "/.versions"
  if @fs.exists(history) {
    for encoded in @fs.readdir(history, include_hidden=false) {
      if !@fs.readdir(history + "/" + encoded, include_hidden=false).is_empty() {
        send_error(
          conn,
          409,
          "BucketNotEmpty",
          "The bucket contains object versions or delete markers.",
          config.cors_origin,
        )
        return
      }
    }
  }
  if @fs.exists(uploads) &&
    !@fs.readdir(uploads, include_hidden=false).is_empty() {
    send_error(
      conn,
      409,
      "BucketNotEmpty",
      "The bucket has incomplete multipart uploads.",
      config.cors_origin,
    )
    return
  }
  @fs.rmdir(dir, recursive=true, allow_missing=true)
  conn.send_response(204, "No Content", extra_headers={
    "Access-Control-Allow-Origin": config.cors_origin,
  })
  conn.end_response()
}

///|
fn parse_list_number(value : String?, fallback : Int32) -> Int32? {
  match value {
    None => Some(fallback)
    Some(text) => {
      if text.is_empty() {
        return None
      }
      let mut result = 0
      for digit in text.to_array() {
        if digit < '0' || digit > '9' {
          return None
        }
        let value = digit.to_int() - '0'.to_int()
        if result > (2147483647 - value) / 10 {
          return None
        }
        result = result * 10 + value
      }
      Some(result)
    }
  }
}

///|
async fn list_objects(
  config : Config,
  bucket : String,
  params : Array[@core.QueryParam],
  conn : ResponseConnection,
) -> Unit {
  recover_bucket(config, bucket) catch {
    error => {
      storage_error(config, conn, error)
      return
    }
  }
  let dir = objects_dir(config, bucket)
  if !@fs.exists(dir) {
    send_error(
      conn,
      404,
      "NoSuchBucket",
      "The bucket does not exist.",
      config.cors_origin,
    )
    return
  }
  let prefix = match @core.query_value(params, "prefix") {
    Some(value) => value
    None => ""
  }
  let max_keys = match
    parse_list_number(@core.query_value(params, "max-keys"), 1000) {
    Some(value) => if value > 1000 { 1000 } else { value }
    None => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "max-keys must be a non-negative integer.",
        config.cors_origin,
      )
      return
    }
  }
  let delimiter = @core.query_value(params, "delimiter").unwrap_or("")
  let start_after = @core.query_value(params, "start-after").unwrap_or("")
  let url_encoded = match @core.query_value(params, "encoding-type") {
    None => false
    Some("url") => true
    Some(_) => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "encoding-type must be url.",
        config.cors_origin,
      )
      return
    }
  }
  let offset = match
    parse_list_number(@core.query_value(params, "continuation-token"), 0) {
    Some(value) => value
    None => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "The continuation token is invalid.",
        config.cors_origin,
      )
      return
    }
  }
  let keys : Array[String] = []
  for entry in @fs.readdir(dir, include_hidden=false, sort=true) {
    if entry.has_suffix(".object") {
      match @core.filename_to_key(entry[:entry.length() - 7]) {
        Some(key) => if key.has_prefix(prefix) { keys.push(key) }
        None => ()
      }
    }
  }
  let entries = list_entries(keys, prefix, delimiter, start_after)
  let start = if offset < entries.length() { offset } else { entries.length() }
  let end = if max_keys >= entries.length() - start {
    entries.length()
  } else {
    start + max_keys
  }
  let mut body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" +
    "<ListBucketResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">" +
    "<Name>" +
    @core.xml_escape(bucket) +
    "</Name><Prefix>" +
    list_xml_value(prefix, url_encoded) +
    "</Prefix><KeyCount>" +
    (end - start).to_string() +
    "</KeyCount><MaxKeys>" +
    max_keys.to_string() +
    "</MaxKeys>"
  if @core.query_value(params, "delimiter") is Some(_) {
    body += "<Delimiter>" +
      list_xml_value(delimiter, url_encoded) +
      "</Delimiter>"
  }
  if @core.query_value(params, "start-after") is Some(_) {
    body += "<StartAfter>" +
      list_xml_value(start_after, url_encoded) +
      "</StartAfter>"
  }
  if url_encoded {
    body += "<EncodingType>url</EncodingType>"
  }
  if @core.query_value(params, "continuation-token") is Some(token) {
    body += "<ContinuationToken>" +
      @core.xml_escape(token) +
      "</ContinuationToken>"
  }
  let truncated = max_keys > 0 && end < entries.length()
  let truncated_text = if truncated { "true" } else { "false" }
  body += "<IsTruncated>" + truncated_text + "</IsTruncated>"
  if truncated {
    body += "<NextContinuationToken>" +
      end.to_string() +
      "</NextContinuationToken>"
  }
  for i in start..<end {
    let (key, common_prefix) = entries[i]
    if common_prefix {
      body += "<CommonPrefixes><Prefix>" +
        list_xml_value(key, url_encoded) +
        "</Prefix></CommonPrefixes>"
      continue
    }
    let path = object_file(config, bucket, key)
    let (etag, _) = object_meta(config, bucket, key)
    body += "<Contents><Key>" +
      list_xml_value(key, url_encoded) +
      "</Key>" +
      "<LastModified>" +
      iso_time(current_modified(config, bucket, key)) +
      "</LastModified>" +
      "<ETag>\"" +
      etag +
      "\"</ETag><Size>" +
      @fs.file_size(path).to_string() +
      "</Size><StorageClass>STANDARD</StorageClass></Contents>"
  }
  body += "</ListBucketResult>"
  send_xml(conn, 200, "OK", body, config.cors_origin)
}

///|
async fn list_buckets(config : Config, conn : ResponseConnection) -> Unit {
  let mut body = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" +
    "<ListAllMyBucketsResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Owner><ID>moonbit</ID><DisplayName>moonbit</DisplayName></Owner><Buckets>"
  for name in @fs.readdir(config.root, include_hidden=false, sort=true) {
    if @core.valid_bucket_name(name) {
      body += "<Bucket><Name>" +
        @core.xml_escape(name) +
        "</Name><CreationDate>" +
        iso_time(bucket_created(config, name)) +
        "</CreationDate></Bucket>"
    }
  }
  body += "</Buckets></ListAllMyBucketsResult>"
  send_xml(conn, 200, "OK", body, config.cors_origin)
}

///|
async fn handle_request(
  config : Config,
  request : @http.Request,
  body : &@io.Reader,
  raw_conn : @http.ServerConnection,
) -> Unit {
  let cors = request_cors(config, request) catch {
    error => {
      let conn : ResponseConnection = {
        raw: raw_conn,
        cors_headers: Map([]),
        operation_headers: Map([]),
        head: request.meth is Head,
      }
      storage_error(config, conn, error)
      return
    }
  }
  let conn : ResponseConnection = {
    raw: raw_conn,
    cors_headers: cors.headers,
    operation_headers: Map([]),
    head: request.meth is Head,
  }
  let (raw_path, query_text) = @core.split_raw_path(request.path)
  if raw_path == "/healthz" {
    conn.send_response(200, "OK", extra_headers={ "Content-Type": "text/plain" })
    conn.write("ok\n")
    conn.end_response()
    return
  }
  let params = @core.query_pairs(query_text)
  if method_name(request.meth) == "OPTIONS" {
    if cors.status == 204 {
      conn.send_response(204, "No Content")
      conn.end_response()
    } else {
      send_error(
        conn,
        cors.status,
        cors.code,
        "CORS preflight is not allowed for this request.",
        config.cors_origin,
      )
    }
    return
  }
  let request_body = body.read_all().binary()
  if !authorize(config, request, raw_path, params, request_body) &&
    !public_read_candidate(config, request, raw_path, params) {
    send_error(
      conn,
      403,
      "AccessDenied",
      "Signature verification failed.",
      config.cors_origin,
    )
    return
  }
  let (bucket, key) = @core.split_bucket_key(raw_path)
  let multipart = is_multipart_request(params)
  let locked = multipart || !(request.meth is Get && key is Some(_))
  if locked {
    config.mutation_lock.acquire()
  }
  defer (if locked { config.mutation_lock.release() })
  if bucket is None &&
    params.any(param => {
      ["cors", "versioning", "versions", "versionId", "policy"].contains(
        param.name,
      )
    }) {
    send_error(
      conn,
      400,
      "InvalidRequest",
      "The selected subresource requires a bucket.",
      config.cors_origin,
    )
    return
  }
  match bucket {
    None =>
      if request.meth is Get {
        list_buckets(config, conn)
      } else {
        send_error(
          conn,
          405,
          "MethodNotAllowed",
          "Method not allowed.",
          config.cors_origin,
        )
      }
    Some(name) => {
      if !@core.valid_bucket_name(name) {
        send_error(
          conn,
          400,
          "InvalidBucketName",
          "The bucket name is not valid.",
          config.cors_origin,
        )
        return
      }
      let selectors = params.filter(param => {
        ["cors", "versioning", "versions", "versionId", "policy"].contains(
          param.name,
        )
      })
      if selectors.length() > 1 ||
        selectors.any(param => param.name != "versionId" && param.value != "") {
        send_error(
          conn,
          400,
          "InvalidRequest",
          "Conflicting or duplicate subresource parameters.",
          config.cors_origin,
        )
        return
      }
      if @core.query_value(params, "versionId") is Some(id) {
        if key is None ||
          !valid_version_id(id) ||
          multipart ||
          !(request.meth is Get ||
          request.meth is Head ||
          request.meth is Delete) {
          send_error(
            conn,
            400,
            "InvalidArgument",
            "Invalid version identifier or operation.",
            config.cors_origin,
          )
          return
        }
      }
      if @core.query_value(params, "versioning") is Some(_) {
        if multipart {
          send_error(
            conn,
            400,
            "InvalidRequest",
            "Conflicting subresources.",
            config.cors_origin,
          )
          return
        }
        handle_versioning(config, request, name, key, request_body, conn) catch {
          error => storage_error(config, conn, error)
        }
        return
      }
      if @core.query_value(params, "policy") is Some(_) {
        handle_bucket_policy(
          config, request, name, key, params, request_body, conn,
        ) catch {
          error => storage_error(config, conn, error)
        }
        return
      }
      if @core.query_value(params, "versions") is Some(_) {
        if multipart {
          send_error(
            conn,
            400,
            "InvalidRequest",
            "Conflicting subresources.",
            config.cors_origin,
          )
          return
        }
        list_versions(config, request, name, key, params, conn) catch {
          error => storage_error(config, conn, error)
        }
        return
      }
      if @core.query_value(params, "cors") is Some(_) {
        handle_cors(config, request, name, key, params, request_body, conn) catch {
          error => storage_error(config, conn, error)
        }
        return
      }
      if multipart {
        handle_multipart(config, request, name, key, params, request_body, conn) catch {
          error => {
            println("multipart request failed: \{error}")
            send_error(
              conn,
              500,
              "InternalError",
              "The multipart operation could not be completed.",
              config.cors_origin,
            )
          }
        }
        return
      }
      match key {
        None =>
          match request.meth {
            Get => list_objects(config, name, params, conn)
            Head => {
              if @fs.exists(bucket_dir(config, name)) {
                conn.send_response(200, "OK", extra_headers={
                  "Access-Control-Allow-Origin": config.cors_origin,
                })
              } else {
                conn.send_response(404, "NotFound", extra_headers={
                  "Access-Control-Allow-Origin": config.cors_origin,
                })
              }
              conn.end_response()
            }
            Put => create_bucket(config, name, conn)
            Delete => delete_bucket(config, name, conn)
            _ =>
              send_error(
                conn,
                405,
                "MethodNotAllowed",
                "Method not allowed.",
                config.cors_origin,
              )
          }
        Some(object_key) => {
          if object_key.is_empty() {
            send_error(
              conn,
              400,
              "InvalidRequest",
              "The object key is empty.",
              config.cors_origin,
            )
            return
          }
          match request.meth {
            Put =>
              if request.headers.contains(
                  @http.CaseInsensitiveString("x-amz-copy-source"),
                ) {
                copy_object(
                  config, request, name, object_key, request_body, conn,
                ) catch {
                  error => storage_error(config, conn, error)
                }
              } else {
                put_object(
                  config, request, name, object_key, request_body, conn,
                )
              }
            Get => get_object(config, request, name, object_key, conn)
            Head => head_object(config, request, name, object_key, conn)
            Delete => delete_object(config, request, name, object_key, conn)
            _ =>
              send_error(
                conn,
                405,
                "MethodNotAllowed",
                "Method not allowed.",
                config.cors_origin,
              )
          }
        }
      }
    }
  }
}

///|
async fn main {
  let config = load_config()
  if (config.webhook_url is Some(url) && webhook_address(url) is None) ||
    (
      config.webhook_secret is Some(secret) &&
      (secret.is_empty() || config.webhook_url is None)
    ) ||
    config.webhook_timeout < 100 ||
    config.webhook_timeout > 60000 {
    println(
      "Invalid webhook configuration: expected an HTTP(S) URL, an optional nonempty secret, and timeout 100-60000 ms.",
    )
    @env.exit(1)
  }
  if config.multipart_ttl < 1L || config.multipart_ttl > 31536000L {
    println("MOON_S3_MULTIPART_TTL_SECONDS must be between 1 and 31536000.")
    @env.exit(1)
  }
  if !valid_credentials(config.access_key, config.secret_key) {
    println(
      "MOON_S3_ACCESS_KEY and MOON_S3_SECRET_KEY must both be set and non-empty, or both unset.",
    )
    @env.exit(1)
  }
  @fs.mkdir(config.root, recursive=true, allow_exist=true)
  let storage_lock = @fs.open(
    config.root + "/.lock",
    mode=ReadWrite,
    create_mode=OpenOrCreate,
  )
  defer storage_lock.close()
  if !storage_lock.try_lock(Exclusive) {
    println("Data directory is already in use: \{config.root}")
    @env.exit(1)
  }
  recover_storage(config) catch {
    error => {
      println("Storage recovery failed: \{error}")
      @env.exit(1)
    }
  }
  recover_events(config) catch {
    error => {
      println("Event recovery failed: \{error}")
      @env.exit(1)
    }
  }
  cleanup_multipart(config)
  let listen = @socket.Addr::parse(config.listen) catch {
    _ => {
      println("invalid MOON_S3_LISTEN: \{config.listen}")
      @env.exit(1)
    }
  }
  println("moon-s3-server listening on \{config.listen}, root=\{config.root}")
  let server = @http.Server(listen)
  @async.with_task_group(group => {
    if config.webhook_url is Some(_) {
      group.spawn_bg(() => dispatch_events(config), no_wait=true)
    }
    server.run_forever((request, body, conn) => {
      handle_request(config, request, body, conn) catch {
        error => println("request failed: \{error}")
      }
    })
  }) catch {
    error => println("server terminated: \{error}")
  }
}
