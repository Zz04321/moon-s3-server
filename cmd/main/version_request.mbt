///|
struct ObjectSnapshot {
  file : @fs.File
  etag : String
  content_type : String
  modified : Int64
}

///|
async fn open_versioned_read(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : ResponseConnection,
) -> ObjectSnapshot? {
  let locked = request.meth is Get
  if locked {
    config.mutation_lock.acquire()
  }
  defer (if locked { config.mutation_lock.release() })
  let (raw_path, query_text) = @core.split_raw_path(request.path)
  let params = @core.query_pairs(query_text)
  if public_read_candidate(config, request, raw_path, params) {
    let allowed = match load_bucket_policy(config, bucket) {
      Some(policy) =>
        if @core.query_value(params, "versionId") is Some(_) {
          policy.historical
        } else {
          policy.current
        }
      None => false
    }
    if !allowed {
      send_error(
        conn,
        403,
        "AccessDenied",
        "Anonymous access is not permitted for this object.",
        config.cors_origin,
      )
      return None
    }
  }
  recover_object(config, bucket, key)
  let selected = @core.query_value(params, "versionId")
  let versions = all_key_versions(config, bucket, key)
  let candidates = match selected {
    None => versions
    Some(id) => versions.filter(version => version.id == id)
  }
  if candidates.is_empty() {
    send_error(
      conn,
      404,
      if selected is Some(_) {
        "NoSuchVersion"
      } else {
        "NoSuchKey"
      },
      "The requested object version does not exist.",
      config.cors_origin,
    )
    return None
  }
  let version = candidates[0]
  if selected is Some(_) ||
    !version.storage.is_empty() ||
    versioning_status(config, bucket) != "" {
    conn.set_version(Some(version.id))
  }
  if version.marker {
    conn.operation_headers[@http.CaseInsensitiveString("x-amz-delete-marker")] = "true"
    if selected is Some(_) {
      conn.operation_headers[@http.CaseInsensitiveString("Last-Modified")] = http_time(
        version.modified,
      )
    }
    send_error(
      conn,
      if selected is Some(_) {
        405
      } else {
        404
      },
      if selected is Some(_) {
        "MethodNotAllowed"
      } else {
        "NoSuchKey"
      },
      "The requested version is a delete marker.",
      config.cors_origin,
    )
    return None
  }
  let file = @fs.open(
    version_content(config, bucket, key, version),
    mode=ReadOnly,
  )
  Some({
    file,
    etag: version.etag,
    content_type: version.content_type,
    modified: version.modified,
  })
}

///|
async fn delete_version(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String,
  conn : ResponseConnection,
) -> Bool {
  let (_, query) = @core.split_raw_path(request.path)
  let selected = @core.query_value(@core.query_pairs(query), "versionId")
  if selected is None && versioning_status(config, bucket) == "" {
    return false
  }
  recover_object(config, bucket, key)
  if selected is Some(id) {
    let versions = all_key_versions(config, bucket, key)
    let matches = versions.filter(version => version.id == id)
    conn.set_version(Some(id))
    if !matches.is_empty() {
      let version = matches[0]
      if version.marker {
        conn.operation_headers[@http.CaseInsensitiveString(
          "x-amz-delete-marker",
        )] = "true"
      }
      if version.storage.is_empty() {
        prepare_object(config, bucket, key)
        publish_transaction(config, bucket, key, "delete\n")
      } else {
        publish_transaction(
          config,
          bucket,
          key,
          "remove-version\n" + version.storage + "\n",
        )
      }
    }
  } else {
    prepare_object(config, bucket, key)
    let id = commit_version(config, bucket, key, "", "", marker=true)
    conn.set_version(Some(id))
    conn.operation_headers[@http.CaseInsensitiveString("x-amz-delete-marker")] = "true"
  }
  conn.send_response(204, "No Content")
  conn.end_response()
  true
}

///|
async fn handle_versioning(
  config : Config,
  request : @http.Request,
  bucket : String,
  key : String?,
  body : Bytes,
  conn : ResponseConnection,
) -> Unit {
  if key is Some(_) {
    send_error(
      conn,
      400,
      "InvalidRequest",
      "Versioning configuration requires a bucket.",
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
  match request.meth {
    Get => {
      let status = versioning_status(config, bucket)
      send_xml(
        conn,
        200,
        "OK",
        "<VersioningConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">" +
        (if status.is_empty() { "" } else { "<Status>" + status + "</Status>" }) +
        "</VersioningConfiguration>",
        config.cors_origin,
      )
    }
    Put => {
      if body.length() > 4096 {
        send_error(
          conn,
          400,
          "InvalidRequest",
          "Versioning configuration exceeds 4 KiB.",
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
            "Expected UTF-8 versioning XML.",
            config.cors_origin,
          )
          return
        }
      }
      let mut text = text.trim().to_owned()
      if text.has_prefix("<?xml ") {
        text = match text.split_once("?>") {
          Some((_, rest)) => rest.trim().to_owned()
          None => {
            send_error(
              conn,
              400,
              "MalformedXML",
              "Invalid XML declaration.",
              config.cors_origin,
            )
            return
          }
        }
      }
      let text = text
        .replace_all(
          old="<VersioningConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">",
          new="<VersioningConfiguration>",
        )
        .replace_all(
          old="<VersioningConfiguration xmlns='http://s3.amazonaws.com/doc/2006-03-01/'>",
          new="<VersioningConfiguration>",
        )
      let (contents, rest) = match
        xml_element(text, "VersioningConfiguration") {
        Some(pair) => pair
        None => {
          send_error(
            conn,
            400,
            "MalformedXML",
            "Invalid versioning configuration.",
            config.cors_origin,
          )
          return
        }
      }
      let (status, trailing) = match xml_element(contents, "Status") {
        Some(pair) => pair
        None => {
          send_error(
            conn,
            400,
            "MalformedXML",
            "A versioning status is required.",
            config.cors_origin,
          )
          return
        }
      }
      let status = status.trim().to_owned()
      if !rest.trim().is_empty() ||
        !trailing.trim().is_empty() ||
        !["Enabled", "Suspended"].contains(status) {
        send_error(
          conn,
          400,
          "InvalidRequest",
          "Only Enabled or Suspended versioning without MFA Delete is supported.",
          config.cors_origin,
        )
        return
      }
      let path = bucket_dir(config, bucket) + "/.versioning"
      @fs.write_file(path + ".tmp", status, sync=Full)
      @fs.rename(path + ".tmp", path, replace=true)
      sync_directory(bucket_dir(config, bucket))
      conn.send_response(200, "OK")
      conn.end_response()
    }
    _ =>
      send_error(
        conn,
        405,
        "MethodNotAllowed",
        "Versioning supports GET and PUT.",
        config.cors_origin,
      )
  }
}
