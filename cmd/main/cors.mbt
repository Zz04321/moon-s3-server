///|
struct ResponseConnection {
  raw : @http.ServerConnection
  cors_headers : Map[@http.CaseInsensitiveString, String]
  operation_headers : Map[@http.CaseInsensitiveString, String]
  head : Bool
  trace : RequestTrace
}

///|
async fn ResponseConnection::send_response(
  self : ResponseConnection,
  status : Int32,
  reason : String,
  extra_headers~ : Map[@http.CaseInsensitiveString, String] = Map([]),
) -> Unit {
  let headers : Map[@http.CaseInsensitiveString, String] = Map([])
  for name, value in extra_headers {
    if !name.to_string().to_lower().has_prefix("access-control-") {
      headers[name] = value
    }
  }
  for name, value in self.cors_headers {
    if name == @http.CaseInsensitiveString("Vary") &&
      headers.get(name) is Some(existing) {
      headers[name] = existing + ", " + value
    } else {
      headers[name] = value
    }
  }
  for name, value in self.operation_headers {
    headers[name] = value
  }
  headers[@http.CaseInsensitiveString("x-moon-request-id")] = self.trace.id.to_string()
  if self.trace.close_after {
    headers[@http.CaseInsensitiveString("Connection")] = "close"
  }
  self.raw.send_response(status, reason, extra_headers=headers)
  self.trace.status = status
}

///|
fn ResponseConnection::set_version(
  self : ResponseConnection,
  version : String?,
) -> Unit {
  if version is Some(id) {
    self.operation_headers[@http.CaseInsensitiveString("x-amz-version-id")] = id
  }
}

///|
async fn ResponseConnection::write(
  self : ResponseConnection,
  data : &@io.Data,
) -> Unit {
  if !self.head {
    self.raw.write(data)
  }
}

///|
async fn ResponseConnection::write_reader(
  self : ResponseConnection,
  reader : &@io.Reader,
) -> Unit {
  if !self.head {
    self.raw.write_reader(reader)
  }
}

///|
async fn ResponseConnection::end_response(self : ResponseConnection) -> Unit {
  self.raw.end_response()
  self.trace.ended = true
}

///|
fn cors_pattern(pattern : String, value : String) -> Bool {
  match pattern.split_once("*") {
    Some((prefix, suffix)) =>
      value.length() >= prefix.length() + suffix.length() &&
      value.has_prefix(prefix) &&
      value.has_suffix(suffix)
    None => pattern == value
  }
}

///|
fn match_cors(
  rules : Array[CorsRule],
  origin : String,
  verb : String,
  headers : Array[String],
) -> CorsRule? {
  if origin.is_empty() ||
    origin
    .to_array()
    .any(character => character.to_int() <= 32 || character.to_int() >= 127) {
    return None
  }
  for rule in rules {
    if rule.methods.contains(verb) &&
      rule.origins.any(pattern => cors_pattern(pattern, origin)) &&
      headers.all(name => {
        rule.headers.any(pattern => {
          cors_pattern(pattern.to_lower(), name.to_lower())
        })
      }) {
      return Some(rule)
    }
  }
  None
}

///|
async fn load_cors(config : Config, bucket : String) -> Array[CorsRule]? {
  let path = bucket_dir(config, bucket) + "/.cors"
  if !@fs.exists(path) {
    return None
  }
  if @fs.file_size(path) > 65536L {
    raise Failure("Bucket CORS configuration exceeds 64 KiB")
  }
  let text = @utf8.decode(@fs.read_file(path).binary().clamped_view())
  match parse_cors_xml(text) {
    Some(rules) if valid_cors_rules(rules) => Some(rules)
    _ => raise Failure("Invalid bucket CORS configuration: \{bucket}")
  }
}

///|
struct CorsResponse {
  headers : Map[@http.CaseInsensitiveString, String]
  status : Int32
  code : String
}

///|
async fn request_cors(config : Config, request : @http.Request) -> CorsResponse {
  let headers : Map[@http.CaseInsensitiveString, String] = {
    "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
  }
  let preflight = request.meth is Options
  let (path, query) = @core.split_raw_path(request.path)
  let repair = @core.query_value(@core.query_pairs(query), "cors") is Some(_) &&
    (request.meth is Put || request.meth is Delete)
  let (bucket, _) = @core.split_bucket_key(path)
  let rules = match bucket {
    Some(bucket) if @core.valid_bucket_name(bucket) => {
      config.mutation_lock.acquire()
      defer config.mutation_lock.release()
      if preflight && !@fs.exists(objects_dir(config, bucket)) {
        return { headers, status: 404, code: "NoSuchBucket", }
      }
      load_cors(config, bucket) catch {
        error => if repair { Some([]) } else { raise error }
      }
    }
    _ => None
  }
  let origin = header(request.headers, "origin")
  let verb = if preflight {
    header(request.headers, "access-control-request-method")
  } else {
    method_name(request.meth)
  }
  let names : Array[String] = []
  if preflight {
    for
      item in header(request.headers, "access-control-request-headers").split(
        ",",
      ) {
      let name = item.trim().to_owned()
      if !name.is_empty() {
        if !cors_token(name, wildcard=false) {
          return { headers, status: 400, code: "InvalidRequest", }
        }
        names.push(name)
      }
    }
    if origin.is_empty() ||
      !["GET", "HEAD", "PUT", "POST", "DELETE"].contains(verb) {
      return { headers, status: 400, code: "InvalidRequest", }
    }
  }
  match rules {
    None => {
      headers[@http.CaseInsensitiveString("Access-Control-Allow-Origin")] = config.cors_origin
      headers[@http.CaseInsensitiveString("Access-Control-Expose-Headers")] = "ETag, Last-Modified, Content-Range, Accept-Ranges"
      if preflight {
        headers[@http.CaseInsensitiveString("Access-Control-Allow-Methods")] = "GET,PUT,POST,HEAD,DELETE,OPTIONS"
        headers[@http.CaseInsensitiveString("Access-Control-Allow-Headers")] = if names.is_empty() {
          "*"
        } else {
          names.join(", ")
        }
        headers[@http.CaseInsensitiveString("Access-Control-Max-Age")] = "600"
      }
    }
    Some(rules) =>
      match match_cors(rules, origin, verb, names) {
        None =>
          if preflight {
            return { headers, status: 403, code: "AccessForbidden", }
          }
        Some(rule) => {
          let allow_origin = if rule.origins.contains("*") {
            "*"
          } else {
            origin
          }
          headers[@http.CaseInsensitiveString("Access-Control-Allow-Origin")] = allow_origin
          if allow_origin != "*" {
            headers[@http.CaseInsensitiveString(
              "Access-Control-Allow-Credentials",
            )] = "true"
          }
          if preflight {
            headers[@http.CaseInsensitiveString("Access-Control-Allow-Methods")] = rule.methods.join(
              ", ",
            )
            if !names.is_empty() {
              headers[@http.CaseInsensitiveString(
                "Access-Control-Allow-Headers",
              )] = names.join(", ")
            }
            if rule.max_age is Some(max_age) {
              headers[@http.CaseInsensitiveString("Access-Control-Max-Age")] = max_age.to_string()
            }
          } else if !rule.expose.is_empty() {
            headers[@http.CaseInsensitiveString("Access-Control-Expose-Headers")] = rule.expose.join(
              ", ",
            )
          }
        }
      }
  }
  { headers, status: 204, code: "No Content", }
}

///|
async fn handle_cors(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String?,
  params : Array[@core.QueryParam],
  body : Bytes,
  conn : ResponseConnection,
) -> Unit {
  if key is Some(_) ||
    params.filter(param => param.name == "cors").length() != 1 ||
    @core.query_value(params, "cors") != Some("") ||
    params.any(param => {
      param.name == "uploads" ||
      param.name == "uploadId" ||
      param.name == "partNumber"
    }) {
    send_error(
      conn,
      400,
      "InvalidRequest",
      "CORS configuration requires a bucket and a single cors parameter.",
      config.cors_origin,
    )
    return
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
  let path = bucket_dir(config, bucket) + "/.cors"
  match request.meth {
    Get =>
      match load_cors(config, bucket) {
        None =>
          send_error(
            conn,
            404,
            "NoSuchCORSConfiguration",
            "The bucket has no CORS configuration.",
            config.cors_origin,
          )
        Some(rules) =>
          send_xml(conn, 200, "OK", cors_xml(rules), config.cors_origin)
      }
    Put => {
      if body.length() > 65536 {
        send_error(
          conn,
          400,
          "MalformedXML",
          "CORS configuration exceeds 64 KiB.",
          config.cors_origin,
        )
        return
      }
      let md5 = header(request.headers, "content-md5")
      if !md5.is_empty() &&
        md5 !=
        @base64.encode(
          Bytes::from_array(@crypto.md5(body.clamped_view()).clamped_view()).clamped_view(),
        ) {
        send_error(
          conn,
          400,
          "BadDigest",
          "Content-MD5 does not match the CORS request body.",
          config.cors_origin,
        )
        return
      }
      let text = @utf8.decode(body.clamped_view()) catch {
        _ => {
          send_error(
            conn,
            400,
            "MalformedXML",
            "CORS configuration must be UTF-8 XML.",
            config.cors_origin,
          )
          return
        }
      }
      let rules = match parse_cors_xml(text) {
        Some(rules) => rules
        None => {
          send_error(
            conn,
            400,
            "MalformedXML",
            "Invalid CORS XML configuration.",
            config.cors_origin,
          )
          return
        }
      }
      if !valid_cors_rules(rules) {
        send_error(
          conn,
          400,
          "InvalidRequest",
          "Expected 1-100 valid CORS rules with origins and supported methods.",
          config.cors_origin,
        )
        return
      }
      let persisted = cors_xml(rules)
      if @utf8.encode(persisted).length() > 65536 {
        send_error(
          conn,
          400,
          "MalformedXML",
          "Normalized CORS configuration exceeds 64 KiB.",
          config.cors_origin,
        )
        return
      }
      @fs.write_file(path + ".tmp", persisted, sync=Full)
      @fs.rename(path + ".tmp", path, replace=true)
      sync_directory(bucket_dir(config, bucket))
      conn.send_response(200, "OK")
      conn.end_response()
    }
    Delete => {
      @fs.remove(path, allow_missing=true)
      @fs.remove(path + ".tmp", allow_missing=true)
      sync_directory(bucket_dir(config, bucket))
      conn.send_response(204, "No Content")
      conn.end_response()
    }
    _ =>
      send_error(
        conn,
        405,
        "MethodNotAllowed",
        "CORS supports GET, PUT and DELETE.",
        config.cors_origin,
      )
  }
}
