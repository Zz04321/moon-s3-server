///|
#derive(Eq, Debug)
enum StorageTransaction {
  ReplaceObject(String?)
  RemoveObject
  PublishVersion(String, String?)
  RemoveVersion(String)
}

///|
async fn storage_error(
  config : Config,
  conn : ResponseConnection,
  error : Error,
) -> Unit {
  println("storage operation failed: \{error}")
  send_error(
    conn,
    500,
    "InternalError",
    "The storage operation could not be completed.",
    config.cors_origin,
  )
}

///|
fn parse_transaction(text : String) -> StorageTransaction? {
  if text == "put\n" {
    return Some(ReplaceObject(None))
  }
  if text == "delete\n" {
    return Some(RemoveObject)
  }
  let lines = text.split("\n").to_array()
  if lines.length() == 3 &&
    lines[0] == "remove-version" &&
    lines[2] == "" &&
    valid_upload_id(lines[1].to_owned()) {
    return Some(RemoveVersion(lines[1].to_owned()))
  }
  if lines.length() == 4 &&
    lines[0] == "version" &&
    lines[3] == "" &&
    valid_upload_id(lines[1].to_owned()) &&
    (lines[2] == "-" || valid_upload_id(lines[2].to_owned())) {
    return Some(
      PublishVersion(
        lines[1].to_owned(),
        if lines[2] == "-" {
          None
        } else {
          Some(lines[2].to_owned())
        },
      ),
    )
  }
  if lines.length() == 3 &&
    lines[0] == "put" &&
    lines[2] == "" &&
    valid_upload_id(lines[1].to_owned()) {
    Some(ReplaceObject(Some(lines[1].to_owned())))
  } else {
    None
  }
}

///|
async fn sync_directory(path : String) -> Unit {
  let directory = @fs.open(path, mode=ReadOnly)
  defer directory.close()
  directory.sync()
}

///|
async fn recover_object(config : Config, bucket : String, key : String) -> Unit {
  let content = object_file(config, bucket, key)
  let metadata = meta_file(config, bucket, key)
  let journal = content + ".txn"
  if !@fs.exists(journal) {
    return
  }
  let transaction = match parse_transaction(read_text(journal)) {
    Some(transaction) => transaction
    None => raise Failure("Invalid storage transaction: \{journal}")
  }
  match transaction {
    PublishVersion(storage, _) =>
      apply_version_transaction(config, bucket, key, storage, remove=false)
    RemoveVersion(storage) =>
      apply_version_transaction(config, bucket, key, storage, remove=true)
    ReplaceObject(_) => {
      for path in [content, metadata] {
        if !@fs.exists(path + ".pending") && !@fs.exists(path) {
          raise Failure("Missing transaction data: \{path}")
        }
      }
      for path in [content, metadata] {
        if @fs.exists(path + ".pending") {
          @fs.rename(path + ".pending", path, replace=true)
        }
      }
    }
    RemoveObject => {
      @fs.remove(content, allow_missing=true)
      @fs.remove(metadata, allow_missing=true)
    }
  }
  sync_directory(objects_dir(config, bucket))
  let upload = match transaction {
    ReplaceObject(upload) | PublishVersion(_, upload) => upload
    _ => None
  }
  if upload is Some(id) {
    let uploads = bucket_dir(config, bucket) + "/.uploads"
    if @fs.exists(upload_dir(config, bucket, id)) {
      @fs.rmdir(upload_dir(config, bucket, id), recursive=true)
    }
    if @fs.exists(uploads) {
      sync_directory(uploads)
    }
  }
  @fs.remove(journal)
  sync_directory(objects_dir(config, bucket))
}

///|
async fn prepare_object(config : Config, bucket : String, key : String) -> Unit {
  recover_object(config, bucket, key)
  for
    path in [
      object_file(config, bucket, key) + ".pending",
      meta_file(config, bucket, key) + ".pending",
      object_file(config, bucket, key) + ".txn.tmp",
    ] {
    @fs.remove(path, allow_missing=true)
  }
}

///|
async fn publish_transaction(
  config : Config,
  bucket : String,
  key : String,
  text : String,
) -> Unit {
  let journal = object_file(config, bucket, key) + ".txn"
  sync_directory(objects_dir(config, bucket))
  @fs.write_file(journal + ".tmp", text, sync=Full)
  @fs.rename(journal + ".tmp", journal, replace=true)
  sync_directory(objects_dir(config, bucket))
  recover_object(config, bucket, key)
}

///|
async fn commit_object(
  config : Config,
  bucket : String,
  key : String,
  etag : String,
  content_type : String,
  upload_id? : String,
) -> String? {
  if versioning_status(config, bucket) != "" {
    return Some(
      commit_version(config, bucket, key, etag, content_type, upload_id?),
    )
  }
  @fs.write_file(
    meta_file(config, bucket, key) + ".pending",
    "etag=" + etag + "\ncontent-type=" + content_type + "\n",
    sync=Full,
  )
  let journal = match upload_id {
    Some(id) => "put\n" + id + "\n"
    None => "put\n"
  }
  publish_transaction(config, bucket, key, journal)
  None
}

///|
async fn recover_bucket(config : Config, bucket : String) -> Unit {
  let directory = objects_dir(config, bucket)
  if !@fs.exists(directory) {
    return
  }
  @fs.remove(bucket_dir(config, bucket) + "/.cors.tmp", allow_missing=true)
  @fs.remove(
    bucket_dir(config, bucket) + "/.versioning.tmp",
    allow_missing=true,
  )
  let abandoned : Array[String] = []
  for entry in @fs.readdir(directory, include_hidden=false) {
    if entry.has_suffix(".object.txn") {
      let encoded = entry[:entry.length() - 11].to_owned()
      let key = match @core.filename_to_key(encoded) {
        Some(key) => key
        None => raise Failure("Invalid transaction filename: \{entry}")
      }
      if @core.key_to_filename(key) != encoded {
        raise Failure("Noncanonical transaction filename: \{entry}")
      }
      recover_object(config, bucket, key)
    } else if entry.has_suffix(".object.pending") ||
      entry.has_suffix(".meta.pending") ||
      entry.has_suffix(".object.txn.tmp") ||
      entry.has_suffix(".object.tmp") ||
      entry.has_suffix(".meta.tmp") {
      abandoned.push(directory + "/" + entry)
    }
  }
  for path in abandoned {
    @fs.remove(path, allow_missing=true)
  }
  if !abandoned.is_empty() {
    sync_directory(directory)
  }
  let history = bucket_dir(config, bucket) + "/.versions"
  if @fs.exists(history) {
    if versioning_status(config, bucket).is_empty() {
      raise Failure("Version history without bucket configuration")
    }
    for encoded in @fs.readdir(history, include_hidden=false) {
      let key = match @core.filename_to_key(encoded) {
        Some(key) if @core.key_to_filename(key) == encoded => key
        _ => raise Failure("Invalid history key: \{encoded}")
      }
      let pending = version_dir(config, bucket, key) + "/.pending"
      if @fs.exists(pending) {
        @fs.rmdir(pending, recursive=true)
      }
      let versions = object_versions(config, bucket, key)
      if versions.filter(version => version.id == "null").length() > 1 {
        raise Failure("Duplicate null version without a recovery journal")
      }
    }
  }
}

///|
async fn recover_storage(config : Config) -> Unit {
  for bucket in @fs.readdir(config.root, include_hidden=false) {
    if @core.valid_bucket_name(bucket) {
      recover_bucket(config, bucket)
    }
  }
}
