///|
fn delete_xml_field(source : String, name : String) -> (String, String)? {
  let rest = match source.trim().strip_prefix("<" + name + ">") {
    Some(value) => value
    None => return None
  }
  let (value, remaining) = match rest.split_once("</" + name + ">") {
    Some(value) => value
    None => return None
  }
  let decoded = match cors_xml_text(value.to_owned()) {
    Some(value) => value
    None => return None
  }
  Some((decoded, remaining.trim().to_owned()))
}

///|
fn parse_delete_objects(text : String) -> (Array[(String, String?)], Bool)? {
  let mut source = text.trim().to_owned()
  if source.has_prefix("<?xml ") {
    source = match source.split_once("?>") {
      Some((_, rest)) => rest.trim().to_owned()
      None => return None
    }
  }
  let (opening, contents) = match source.split_once(">") {
    Some(value) => value
    None => return None
  }
  let attributes = match opening.strip_prefix("<Delete") {
    Some(value) => value
    None => return None
  }
  if !attributes.is_empty() &&
    !attributes.has_prefix(" ") &&
    !attributes.has_prefix("\n") &&
    !attributes.has_prefix("\r") &&
    !attributes.has_prefix("\t") {
    return None
  }
  let attributes = attributes.trim()
  if attributes != "" &&
    attributes != "xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"" &&
    attributes != "xmlns='http://s3.amazonaws.com/doc/2006-03-01/'" {
    return None
  }
  let (contents, trailing) = match contents.split_once("</Delete>") {
    Some(value) => value
    None => return None
  }
  if !trailing.trim().is_empty() {
    return None
  }
  let objects : Array[(String, String?)] = []
  let mut quiet : Bool? = None
  source = contents.trim().to_owned()
  while !source.is_empty() {
    if source.has_prefix("<Quiet>") {
      if quiet is Some(_) {
        return None
      }
      let (value, rest) = match delete_xml_field(source, "Quiet") {
        Some(value) => value
        None => return None
      }
      quiet = match value {
        "true" => Some(true)
        "false" => Some(false)
        _ => return None
      }
      source = rest
    } else {
      let rest = match source.strip_prefix("<Object>") {
        Some(value) => value
        None => return None
      }
      let (contents, after) = match rest.split_once("</Object>") {
        Some(value) => value
        None => return None
      }
      let mut fields = contents.trim().to_owned()
      let mut key : String? = None
      let mut version : String? = None
      while !fields.is_empty() {
        if fields.has_prefix("<Key>") && key is None {
          let (value, rest) = match delete_xml_field(fields, "Key") {
            Some(value) => value
            None => return None
          }
          key = Some(value)
          fields = rest
        } else if fields.has_prefix("<VersionId>") && version is None {
          let (value, rest) = match delete_xml_field(fields, "VersionId") {
            Some(value) => value
            None => return None
          }
          if !valid_version_id(value) {
            return None
          }
          version = Some(value)
          fields = rest
        } else {
          return None
        }
      }
      let key = match key {
        Some(value) => value
        None => return None
      }
      if !valid_object_key(key) || objects.length() >= 1000 {
        return None
      }
      objects.push((key, version))
      source = after.trim().to_owned()
    }
  }
  if objects.is_empty() {
    None
  } else {
    Some((objects, quiet.unwrap_or(false)))
  }
}

///|
async fn delete_objects(
  config : Config,
  bucket : String,
  body : Bytes,
  conn : ResponseConnection,
) -> Unit {
  if !@fs.exists(objects_dir(config, bucket)) {
    send_error(
      conn,
      404,
      "NoSuchBucket",
      "Bucket not found.",
      config.cors_origin,
    )
    return
  }
  let parsed = if body.length() <= 1048576 {
    let text = @utf8.decode(body.clamped_view()) catch { _ => "" }
    parse_delete_objects(text)
  } else {
    None
  }
  let (objects, quiet) = match parsed {
    Some(value) => value
    None => {
      send_error(
        conn,
        400,
        "MalformedXML",
        "Expected 1-1000 objects in a Delete document of at most 1 MiB.",
        config.cors_origin,
      )
      return
    }
  }
  let mut result = "<DeleteResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
  for (key, selected) in objects {
    let identity = "<Key>" +
      @core.xml_escape(key) +
      "</Key>" +
      (match selected {
        Some(id) => "<VersionId>" + @core.xml_escape(id) + "</VersionId>"
        None => ""
      })
    try {
      let (handled, removed, marker) = remove_version(
        config, bucket, key, selected,
      )
      if !handled {
        remove_unversioned_object(config, bucket, key)
      }
      if !quiet {
        result += "<Deleted>" + identity
        if marker {
          result += "<DeleteMarker>true</DeleteMarker><DeleteMarkerVersionId>" +
            @core.xml_escape(removed.unwrap()) +
            "</DeleteMarkerVersionId>"
        }
        result += "</Deleted>"
      }
    } catch {
      _ => {
        log_event("error", "batch_delete_failed", "An object deletion failed.")
        result += "<Error>" +
          identity +
          "<Code>InternalError</Code><Message>Object deletion failed.</Message></Error>"
      }
    }
  }
  send_xml(conn, 200, "OK", result + "</DeleteResult>", config.cors_origin)
}
