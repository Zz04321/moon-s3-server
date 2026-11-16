///|
struct QueuedEvent {
  id : String
  body : String
}

///|
fn parse_event_journal(text : String) -> (String?, String)? {
  if !text.has_prefix("event\n") {
    return Some((None, text))
  }
  match text[6:].split_once("\n") {
    Some((id, transaction)) if valid_upload_id(id.to_owned()) =>
      Some((Some(id.to_owned()), transaction.to_owned()))
    _ => None
  }
}

///|
fn webhook_address(value : String) -> (String, String)? {
  let (scheme, remaining) = if value.has_prefix("http://") {
    ("http://", value[7:].to_owned())
  } else if value.has_prefix("https://") {
    ("https://", value[8:].to_owned())
  } else {
    return None
  }
  if value
    .to_array()
    .any(character => character.to_int() <= 32 || character.to_int() >= 127) ||
    value.contains("#") ||
    value.contains("@") {
    return None
  }
  let (authority, path) = match remaining.split_once("/") {
    Some((authority, path)) => (authority.to_owned(), "/" + path.to_owned())
    None => (remaining, "/")
  }
  let host = match authority.split_once(":") {
    Some((host, port)) => {
      match version_number(port.to_owned()) {
        Some(port) if port >= 1L && port <= 65535L => ()
        _ => return None
      }
      host.to_owned()
    }
    None => authority
  }
  if host.is_empty() ||
    !host
    .to_array()
    .all(character => {
      (character >= 'a' && character <= 'z') ||
      (character >= 'A' && character <= 'Z') ||
      (character >= '0' && character <= '9') ||
      character == '.' ||
      character == '-'
    }) {
    return None
  }
  Some((scheme + authority, path))
}

///|
fn object_event(
  config : Config,
  bucket : String,
  key : String,
  name : String,
  version : String?,
  etag : String,
  size : Int64,
) -> QueuedEvent? raise {
  if config.webhook_url is None {
    return None
  }
  let id = new_version_storage()
  let object : Map[String, Json] = {
    "key": Json::string(
      @core.uri_encode(key, allow_slash=false).replace_all(old="%20", new="+"),
    ),
    "size": Json::number(size.to_double()),
  }
  if !etag.is_empty() {
    object["eTag"] = Json::string(etag)
  }
  if version is Some(version) {
    object["versionId"] = Json::string(version)
  }
  let record = Json::object({
    "eventVersion": Json::string("2.1"),
    "eventSource": Json::string("moon:s3"),
    "eventTime": Json::string(
      iso_time((@env.now() / 1000UL).reinterpret_as_int64()),
    ),
    "eventName": Json::string(name),
    "eventId": Json::string(id),
    "s3": Json::object({
      "bucket": Json::object({
        "name": Json::string(bucket),
        "arn": Json::string("arn:aws:s3:::" + bucket),
      }),
      "object": Json::object(object),
    }),
  })
  Some({
    id,
    body: Json::object({ "Records": Json::array([record]) }).stringify(),
  })
}

///|
fn valid_event_body(body : String, id : String) -> Bool {
  let document = @json.parse(
    body,
    max_nesting_depth=8,
    reject_duplicate_keys=true,
  ) catch {
    _ => return false
  }
  match document {
    { "Records": Array([record]), .. } =>
      match record {
        {
          "eventId": String(found),
          "eventSource": String("moon:s3"),
          "eventVersion": String("2.1"),
          "eventTime": String(time),
          "eventName": String(name),
          "s3": {
            "bucket": { "name": String(bucket), "arn": String(arn), .. },
            "object": Object(object),
            ..
          },
          ..
        } => {
          let (key, size) = match (object.get("key"), object.get("size")) {
            (Some(String(key)), Some(Number(size, ..))) => (key, size)
            _ => return false
          }
          if object.get("versionId") is Some(value) {
            match value {
              String(id) if valid_version_id(id) => ()
              _ => return false
            }
          }
          if object.get("eTag") is Some(value) {
            match value {
              String(etag) if valid_version_etag(etag) => ()
              _ => return false
            }
          }
          let normalized_time = time
            .replace_all(old="-", new="")
            .replace_all(old=":", new="")
            .replace_all(old=".000Z", new="Z")
          if time.length() != 24 ||
            !time.has_suffix(".000Z") ||
            signature_timestamp(normalized_time) is None {
            return false
          }
          valid_upload_id(id) &&
          found == id &&
          @core.valid_bucket_name(bucket) &&
          arn == "arn:aws:s3:::" + bucket &&
          !key.is_empty() &&
          !time.is_empty() &&
          size >= 0.0 &&
          size <= 5497558138880.0 &&
          size.to_int64().to_double() == size &&
          [
            "ObjectCreated:Put", "ObjectCreated:Copy", "ObjectCreated:CompleteMultipartUpload",
            "ObjectRemoved:Delete", "ObjectRemoved:DeleteMarkerCreated",
          ].contains(name)
        }
        _ => false
      }
    _ => false
  }
}

///|
async fn read_event(path : String, id : String) -> String {
  if @fs.file_size(path) > 65536L {
    raise Failure("Event exceeds 64 KiB: \{id}")
  }
  let body = @utf8.decode(@fs.read_file(path).binary().clamped_view())
  if !valid_event_body(body, id) {
    raise Failure("Invalid queued event: \{id}")
  }
  body
}

///|
fn events_dir(config : Config) -> String {
  config.root + "/.events"
}

///|
async fn stage_event(
  config : Config,
  bucket : String,
  key : String,
  event : QueuedEvent,
) -> Unit {
  @fs.mkdir(events_dir(config), allow_exist=true)
  sync_directory(config.root)
  @fs.write_file(
    object_file(config, bucket, key) + ".event.pending",
    event.body,
    sync=Full,
  )
}

///|
async fn preserve_event(
  config : Config,
  bucket : String,
  key : String,
  id : String,
) -> Unit {
  let pending = events_dir(config) + "/" + id + ".pending"
  let ready = events_dir(config) + "/" + id + ".json"
  if @fs.exists(ready) {
    ignore(read_event(ready, id))
    return
  }
  if !@fs.exists(pending) {
    let staged = object_file(config, bucket, key) + ".event.pending"
    ignore(read_event(staged, id))
    @fs.rename(staged, pending)
    sync_directory(objects_dir(config, bucket))
  } else {
    ignore(read_event(pending, id))
  }
  sync_directory(events_dir(config))
}

///|
async fn release_event(config : Config, id : String) -> Unit {
  let pending = events_dir(config) + "/" + id + ".pending"
  if @fs.exists(pending) {
    @fs.rename(pending, events_dir(config) + "/" + id + ".json", replace=true)
    sync_directory(events_dir(config))
  }
}

///|
async fn recover_events(config : Config) -> Unit {
  let directory = events_dir(config)
  if !@fs.exists(directory) {
    return
  }
  for entry in @fs.readdir(directory, include_hidden=true) {
    let id = match entry.split_once(".") {
      Some((id, suffix)) if valid_upload_id(id.to_owned()) &&
        (suffix == "json" || suffix == "pending") => id.to_owned()
      _ => raise Failure("Invalid event filename: \{entry}")
    }
    ignore(read_event(directory + "/" + entry, id))
    if entry.has_suffix(".pending") {
      release_event(config, id)
    }
  }
}

///|
async fn deliver_event(config : Config, id : String, body : String) -> Unit {
  let (origin, path) = webhook_address(config.webhook_url.unwrap()).unwrap()
  @async.with_timeout(config.webhook_timeout) <| () => {
    let client = @http.Client(origin)
    defer client.close()
    let headers : Map[@http.CaseInsensitiveString, String] = {
      "Content-Type": "application/json",
      "Content-Length": @utf8.encode(body).length().to_string(),
      "X-Moon-S3-Event-Id": id,
    }
    if config.webhook_secret is Some(secret) {
      headers["X-Moon-S3-Signature"] = "sha256=" +
        @core.hmac_sha256_hex(
          @utf8.encode(secret).clamped_view(),
          @utf8.encode(body).clamped_view(),
        )
    }
    let response = client.post(path, body, extra_headers=headers)
    if response.code < 200 || response.code >= 300 {
      raise Failure("Webhook returned HTTP \{response.code}")
    }
  }
}

///|
async fn dispatch_events(config : Config) -> Unit {
  let mut backoff = 1000
  while true {
    let failed = try {
      let entries = if @fs.exists(events_dir(config)) {
        @fs.readdir(events_dir(config), include_hidden=false)
      } else {
        []
      }
      entries.sort()
      let mut sent = 0
      for entry in entries {
        if !entry.has_suffix(".json") {
          continue
        }
        if sent >= 32 {
          break
        }
        let id = entry[:entry.length() - 5].to_owned()
        let body = read_event(events_dir(config) + "/" + entry, id)
        deliver_event(config, id, body)
        @fs.remove(events_dir(config) + "/" + entry)
        sync_directory(events_dir(config))
        sent += 1
      }
      false
    } catch {
      error => {
        println("event delivery failed; queued events retained: \{error}")
        true
      }
    }
    if failed {
      @async.sleep(backoff)
      backoff = if backoff >= 30000 { 60000 } else { backoff * 2 }
    } else {
      backoff = 1000
      @async.sleep(1000)
    }
  }
}
