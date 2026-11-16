///|
#derive(Eq, Debug)
struct PublicReadPolicy {
  current : Bool
  historical : Bool
}

///|
fn policy_strings(value : Json) -> Array[String]? {
  match value {
    String(value) => Some([value])
    Array(values) => {
      let strings : Array[String] = []
      for value in values {
        match value {
          String(value) => strings.push(value)
          _ => return None
        }
      }
      if strings.is_empty() {
        None
      } else {
        Some(strings)
      }
    }
    _ => None
  }
}

///|
fn parse_bucket_policy(text : String, bucket : String) -> PublicReadPolicy? {
  let document = @json.parse(
    text,
    max_nesting_depth=8,
    reject_duplicate_keys=true,
  ) catch {
    _ => return None
  }
  let fields = match document {
    Object(fields) => fields
    _ => return None
  }
  if fields.get("Version") != Some(Json::string("2012-10-17")) ||
    fields.keys().any(name => !["Version", "Statement", "Id"].contains(name)) {
    return None
  }
  if fields.get("Id") is Some(value) && !(value is String(_)) {
    return None
  }
  let statements = match fields.get("Statement") {
    Some(Array(statements)) => statements
    Some(Object(statement)) => [Json::object(statement)]
    _ => return None
  }
  if statements.length() > 20 {
    return None
  }
  let mut current = false
  let mut historical = false
  for statement in statements {
    let fields = match statement {
      Object(fields) => fields
      _ => return None
    }
    if fields
      .keys()
      .any(name => {
        !["Sid", "Effect", "Principal", "Action", "Resource"].contains(name)
      }) ||
      fields.get("Effect") != Some(Json::string("Allow")) {
      return None
    }
    if fields.get("Sid") is Some(value) && !(value is String(_)) {
      return None
    }
    match fields.get("Principal") {
      Some(String("*")) => ()
      Some(Object(principal)) if principal.length() == 1 &&
        principal.get("AWS") == Some(Json::string("*")) => ()
      _ => return None
    }
    let actions = match fields.get("Action") {
      Some(value) =>
        match policy_strings(value) {
          Some(actions) => actions
          None => return None
        }
      None => return None
    }
    let resources = match fields.get("Resource") {
      Some(value) =>
        match policy_strings(value) {
          Some(resources) => resources
          None => return None
        }
      None => return None
    }
    if resources.length() != 1 ||
      resources[0] != "arn:aws:s3:::" + bucket + "/*" {
      return None
    }
    for action in actions {
      match action {
        "s3:GetObject" => current = true
        "s3:GetObjectVersion" => historical = true
        _ => return None
      }
    }
  }
  Some({ current, historical, })
}

///|
async fn load_bucket_policy(
  config : Config,
  bucket : String,
) -> PublicReadPolicy? {
  let path = bucket_dir(config, bucket) + "/.policy"
  if !@fs.exists(path) {
    return None
  }
  if @fs.file_size(path) > 20480L {
    raise Failure("Bucket policy exceeds 20 KiB: \{bucket}")
  }
  let text = @utf8.decode(@fs.read_file(path).binary().clamped_view())
  match parse_bucket_policy(text, bucket) {
    Some(policy) => Some(policy)
    None => raise Failure("Invalid bucket policy: \{bucket}")
  }
}

///|
fn public_read_candidate(
  config : Config,
  request : @http.Request,
  raw_path : String,
  params : Array[@core.QueryParam],
) -> Bool {
  if config.access_key is None ||
    !(request.meth is Get || request.meth is Head) ||
    has_query_auth(params) ||
    params.any(param => param.name != "versionId") {
    return false
  }
  if ["authorization", "x-amz-date", "x-amz-security-token"].any(name => {
      request.headers.contains(@http.CaseInsensitiveString(name))
    }) {
    return false
  }
  match @core.split_bucket_key(raw_path) {
    (Some(bucket), Some(key)) =>
      @core.valid_bucket_name(bucket) && !key.is_empty()
    _ => false
  }
}

///|
async fn handle_bucket_policy(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String?,
  params : Array[@core.QueryParam],
  body : Bytes,
  conn : ResponseConnection,
) -> Unit {
  if key is Some(_) || is_multipart_request(params) {
    send_error(
      conn,
      400,
      "InvalidRequest",
      "Bucket policy requires a bucket without other subresources.",
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
  let path = bucket_dir(config, bucket) + "/.policy"
  match request.meth {
    Get => {
      if load_bucket_policy(config, bucket) is None {
        send_error(
          conn,
          404,
          "NoSuchBucketPolicy",
          "The bucket does not have a policy.",
          config.cors_origin,
        )
        return
      }
      let content = @fs.read_file(path).binary()
      conn.send_response(200, "OK", extra_headers={
        "Content-Type": "application/json",
      })
      conn.write(content)
      conn.end_response()
    }
    Put => {
      if body.length() > 20480 {
        send_error(
          conn,
          400,
          "MalformedPolicy",
          "Bucket policy exceeds 20 KiB.",
          config.cors_origin,
        )
        return
      }
      let text = @utf8.decode(body.clamped_view()) catch {
        _ => {
          send_error(
            conn,
            400,
            "MalformedPolicy",
            "Expected UTF-8 policy JSON.",
            config.cors_origin,
          )
          return
        }
      }
      if parse_bucket_policy(text, bucket) is None {
        send_error(
          conn,
          400,
          "MalformedPolicy",
          "Only public GetObject/GetObjectVersion grants for this bucket's objects are supported.",
          config.cors_origin,
        )
        return
      }
      @fs.write_file(path + ".tmp", body, sync=Full)
      @fs.rename(path + ".tmp", path, replace=true)
      sync_directory(bucket_dir(config, bucket))
      conn.send_response(204, "No Content")
      conn.end_response()
    }
    Delete => {
      @fs.remove(path, allow_missing=true)
      sync_directory(bucket_dir(config, bucket))
      conn.send_response(204, "No Content")
      conn.end_response()
    }
    _ =>
      send_error(
        conn,
        405,
        "MethodNotAllowed",
        "Bucket policies support GET, PUT and DELETE.",
        config.cors_origin,
      )
  }
}
