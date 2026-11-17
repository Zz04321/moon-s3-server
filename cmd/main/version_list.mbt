///|
struct ListedVersion {
  key : String
  version : ObjectVersion?
  latest : Bool
}

///|
async fn list_versions(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String?,
  params : Array[@core.QueryParam],
  conn : ResponseConnection,
) -> Unit {
  if key is Some(_) || !(request.meth is Get) {
    send_error(
      conn,
      405,
      "MethodNotAllowed",
      "Version listing requires GET on a bucket.",
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
  recover_bucket(config, bucket)
  let seen : Map[String, Bool] = Map([])
  for param in params {
    if seen.contains(param.name) {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Duplicate version listing parameter.",
        config.cors_origin,
      )
      return
    }
    seen[param.name] = true
  }
  let maximum = match
    parse_list_number(@core.query_value(params, "max-keys"), 1000) {
    Some(maximum) => if maximum > 1000 { 1000 } else { maximum }
    None => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Invalid max-keys.",
        config.cors_origin,
      )
      return
    }
  }
  let prefix = @core.query_value(params, "prefix").unwrap_or("")
  let delimiter = @core.query_value(params, "delimiter").unwrap_or("")
  let marker = @core.query_value(params, "key-marker").unwrap_or("")
  let version_marker = @core.query_value(params, "version-id-marker")
  let encoded = match @core.query_value(params, "encoding-type") {
    None => false
    Some("url") => true
    _ => {
      send_error(
        conn,
        400,
        "InvalidArgument",
        "Invalid encoding-type.",
        config.cors_origin,
      )
      return
    }
  }
  if version_marker is Some(id) && (marker.is_empty() || !valid_version_id(id)) {
    send_error(
      conn,
      400,
      "InvalidArgument",
      "A valid version marker requires a key marker.",
      config.cors_origin,
    )
    return
  }
  let keys : Map[String, Bool] = Map([])
  for entry in @fs.readdir(objects_dir(config, bucket), include_hidden=false) {
    if entry.has_suffix(".object") {
      keys[storage_key(config, bucket, entry[:entry.length() - 7].to_owned())] = true
    }
  }
  let history = bucket_dir(config, bucket) + "/.versions"
  if @fs.exists(history) {
    for entry in @fs.readdir(history, include_hidden=false) {
      keys[storage_key(config, bucket, entry)] = true
    }
  }
  let ordered = keys.keys().to_array()
  ordered.sort_by((left, right) => {
    @utf8.encode(left)
    .clamped_view()
    .lexical_compare(@utf8.encode(right).clamped_view())
  })
  let entries : Array[ListedVersion] = []
  let prefixes : Map[String, Bool] = Map([])
  let mut marker_found = version_marker is None
  for key in ordered {
    if !key.has_prefix(prefix) {
      continue
    }
    let versions = all_key_versions(config, bucket, key)
    if versions.is_empty() {
      continue
    }
    if !delimiter.is_empty() &&
      key[prefix.length():].split_once(delimiter) is Some((before, _)) {
      let common = prefix + before.to_owned() + delimiter
      if !prefixes.contains(common) {
        prefixes[common] = true
        if @utf8.encode(common)
          .clamped_view()
          .lexical_compare(@utf8.encode(marker).clamped_view()) >
          0 {
          entries.push({ key: common, version: None, latest: false, })
        }
      }
      continue
    }
    let order = @utf8.encode(key)
      .clamped_view()
      .lexical_compare(@utf8.encode(marker).clamped_view())
    if order < 0 || (order == 0 && version_marker is None) {
      continue
    }
    let mut skip = order == 0 && version_marker is Some(_)
    for index in 0..<versions.length() {
      let version = versions[index]
      if skip {
        if version_marker == Some(version.id) {
          skip = false
          marker_found = true
        }
        continue
      }
      entries.push({ key, version: Some(version), latest: index == 0, })
    }
  }
  if !marker_found {
    send_error(
      conn,
      400,
      "InvalidArgument",
      "Version marker does not identify a listed version.",
      config.cors_origin,
    )
    return
  }
  let count = if maximum < entries.length() {
    maximum
  } else {
    entries.length()
  }
  let truncated = maximum > 0 && count < entries.length()
  let mut xml = "<ListVersionsResult xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"><Name>" +
    @core.xml_escape(bucket) +
    "</Name><Prefix>" +
    list_xml_value(prefix, encoded) +
    "</Prefix><KeyMarker>" +
    list_xml_value(marker, encoded) +
    "</KeyMarker><MaxKeys>" +
    maximum.to_string() +
    "</MaxKeys><IsTruncated>" +
    truncated.to_string() +
    "</IsTruncated>"
  if encoded {
    xml += "<EncodingType>url</EncodingType>"
  }
  if !delimiter.is_empty() {
    xml += "<Delimiter>" + list_xml_value(delimiter, encoded) + "</Delimiter>"
  }
  if version_marker is Some(id) {
    xml += "<VersionIdMarker>" + id + "</VersionIdMarker>"
  }
  if truncated {
    let last = entries[count - 1]
    xml += "<NextKeyMarker>" +
      list_xml_value(last.key, encoded) +
      "</NextKeyMarker>"
    if last.version is Some(version) {
      xml += "<NextVersionIdMarker>" + version.id + "</NextVersionIdMarker>"
    }
  }
  for index in 0..<count {
    let item = entries[index]
    match item.version {
      None =>
        xml += "<CommonPrefixes><Prefix>" +
          list_xml_value(item.key, encoded) +
          "</Prefix></CommonPrefixes>"
      Some(version) => {
        let tag = if version.marker { "DeleteMarker" } else { "Version" }
        xml += "<" +
          tag +
          "><Key>" +
          list_xml_value(item.key, encoded) +
          "</Key><VersionId>" +
          version.id +
          "</VersionId><IsLatest>" +
          item.latest.to_string() +
          "</IsLatest><LastModified>" +
          iso_time(version.modified) +
          "</LastModified>"
        if !version.marker {
          xml += "<ETag>\"" +
            version.etag +
            "\"</ETag><Size>" +
            version.size.to_string() +
            "</Size><StorageClass>STANDARD</StorageClass>"
        }
        xml += "</" + tag + ">"
      }
    }
  }
  send_xml(conn, 200, "OK", xml + "</ListVersionsResult>", config.cors_origin)
}
