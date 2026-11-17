///|
fn storage_filename(key : String) -> String {
  if @utf8.encode(key).length() <= 117 {
    @core.key_to_filename(key)
  } else {
    "k-" + @core.sha256_hex(@utf8.encode(key).clamped_view())
  }
}

///|
async fn storage_key(
  config : Config,
  bucket : String,
  filename : String,
) -> String {
  let key = if filename.has_prefix("k-") {
    let path = bucket_dir(config, bucket) + "/.keys/" + filename
    if filename.length() != 66 || @fs.file_size(path) > 1024L {
      raise Failure("Invalid long key mapping")
    }
    @utf8.decode(@fs.read_file(path).binary().clamped_view())
  } else {
    match @core.filename_to_key(filename) {
      Some(key) => key
      None => raise Failure("Invalid storage key")
    }
  }
  if !valid_object_key(key) || storage_filename(key) != filename {
    raise Failure("Noncanonical storage key")
  }
  key
}

///|
async fn ensure_key_mapping(
  config : Config,
  bucket : String,
  key : String,
) -> Unit {
  if !valid_object_key(key) {
    raise Failure("Invalid object key length")
  }
  let filename = storage_filename(key)
  if !filename.has_prefix("k-") {
    return
  }
  let directory = bucket_dir(config, bucket) + "/.keys"
  @fs.mkdir(directory, allow_exist=true)
  let path = directory + "/" + filename
  if @fs.exists(path) {
    if storage_key(config, bucket, filename) != key {
      raise Failure("Storage key collision")
    }
    return
  }
  @fs.write_file(path + ".tmp", key, sync=Full)
  @fs.rename(path + ".tmp", path)
  sync_directory(directory)
  sync_directory(bucket_dir(config, bucket))
}
