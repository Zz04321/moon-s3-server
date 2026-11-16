///|
struct ObjectVersion {
  id : String
  storage : String
  order : Int64
  marker : Bool
  etag : String
  content_type : String
  modified : Int64
  size : Int64
}

///|
fn valid_version_id(id : String) -> Bool {
  id == "null" || valid_upload_id(id)
}

///|
fn version_dir(config : Config, bucket : String, key : String) -> String {
  bucket_dir(config, bucket) + "/.versions/" + @core.key_to_filename(key)
}

///|
async fn versioning_status(config : Config, bucket : String) -> String {
  let path = bucket_dir(config, bucket) + "/.versioning"
  if !@fs.exists(path) {
    return ""
  }
  if @fs.file_size(path) > 9L {
    raise Failure("Invalid bucket versioning status: \{bucket}")
  }
  let text = @utf8.decode(@fs.read_file(path).binary().clamped_view())
  if text != "Enabled" && text != "Suspended" {
    raise Failure("Invalid bucket versioning status: \{bucket}")
  }
  text
}

///|
fn version_number(text : String) -> Int64? {
  match range_number(text) {
    Some(value) if value.to_string() == text => Some(value)
    _ => None
  }
}

///|
fn valid_version_etag(etag : String) -> Bool {
  let (digest, count) = match etag.split_once("-") {
    Some((digest, count)) => (digest.to_owned(), Some(count.to_owned()))
    None => (etag, None)
  }
  if digest.length() != 32 ||
    !digest
    .to_array()
    .all(digit => {
      (digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f')
    }) {
    return false
  }
  match count {
    None => true
    Some(count) =>
      match version_number(count) {
        Some(value) => value >= 1L && value <= 10000L
        None => false
      }
  }
}

///|
async fn object_versions(
  config : Config,
  bucket : String,
  key : String,
) -> Array[ObjectVersion] {
  let base = version_dir(config, bucket, key)
  let versions : Array[ObjectVersion] = []
  if @fs.exists(base) {
    for storage in @fs.readdir(base, include_hidden=false) {
      if !valid_upload_id(storage) {
        raise Failure("Invalid version directory: \{storage}")
      }
      let record = base + "/" + storage + "/record"
      if @fs.file_size(record) > 65536L {
        raise Failure("Version record exceeds 64 KiB")
      }
      let text = @utf8.decode(@fs.read_file(record).binary().clamped_view())
      let fields : Map[String, Bool] = Map([])
      for line in text.split("\n") {
        if line.is_empty() {
          continue
        }
        let name = match line.split_once("=") {
          Some((name, _)) => name.to_owned()
          None => raise Failure("Invalid version field: \{record}")
        }
        if ![
            "version-id", "order", "delete-marker", "etag", "content-type", "modified",
          ].contains(name) ||
          fields.contains(name) {
          raise Failure("Invalid or duplicate version field: \{record}")
        }
        fields[name] = true
      }
      if fields.length() != 6 {
        raise Failure("Incomplete version record: \{record}")
      }
      let id = meta_value(text, "version-id")
      let marker = meta_value(text, "delete-marker")
      let order = version_number(meta_value(text, "order"))
      let modified = version_number(meta_value(text, "modified"))
      if !(id == "null" || id == storage) ||
        !(marker == "true" || marker == "false") ||
        order is None ||
        modified is None {
        raise Failure("Invalid version record: \{base}/\{storage}")
      }
      let path = base + "/" + storage + "/content"
      let etag = meta_value(text, "etag")
      let content_type = meta_value(text, "content-type")
      if (
          marker == "true" &&
          (@fs.exists(path) || !etag.is_empty() || !content_type.is_empty())
        ) ||
        (marker == "false" && !valid_version_etag(etag)) {
        raise Failure("Invalid version content metadata: \{record}")
      }
      if versions.any(version => version.order == order.unwrap()) {
        raise Failure("Duplicate version order: \{record}")
      }
      let size = if marker == "true" { 0L } else { @fs.file_size(path) }
      versions.push({
        id,
        storage,
        order: order.unwrap(),
        modified: modified.unwrap(),
        marker: marker == "true",
        etag,
        content_type,
        size,
      })
    }
  }
  versions.sort_by((left, right) => right.order.compare(left.order))
  versions
}

///|
async fn legacy_version(
  config : Config,
  bucket : String,
  key : String,
) -> ObjectVersion? {
  let path = object_file(config, bucket, key)
  if !@fs.exists(path) {
    return None
  }
  let (etag, content_type) = object_meta(config, bucket, key)
  Some({
    id: "null",
    storage: "",
    order: 0L,
    marker: false,
    etag,
    content_type,
    modified: file_modified(path),
    size: @fs.file_size(path),
  })
}

///|
async fn all_key_versions(
  config : Config,
  bucket : String,
  key : String,
) -> Array[ObjectVersion] {
  let versions = object_versions(config, bucket, key)
  if versions.is_empty() && legacy_version(config, bucket, key) is Some(legacy) {
    versions.push(legacy)
  }
  versions
}

///|
fn version_content(
  config : Config,
  bucket : String,
  key : String,
  version : ObjectVersion,
) -> String {
  if version.storage.is_empty() {
    object_file(config, bucket, key)
  } else {
    version_dir(config, bucket, key) + "/" + version.storage + "/content"
  }
}

///|
async fn copy_storage_file(source : String, destination : String) -> Unit {
  let input = @fs.open(source, mode=ReadOnly)
  defer input.close()
  let output = @fs.create(destination, allow_existing=true)
  defer output.close()
  output.write_reader(input)
  output.sync()
}

///|
fn new_version_storage() -> String raise {
  match @env.rand(32) {
    Some(bytes) => @crypto.bytes_to_hex_string(bytes)
    None => raise Failure("Could not generate version identifier")
  }
}

///|
async fn ensure_version_directory(
  config : Config,
  bucket : String,
  key : String,
) -> String {
  let parent = bucket_dir(config, bucket) + "/.versions"
  @fs.mkdir(parent, allow_exist=true)
  let base = version_dir(config, bucket, key)
  @fs.mkdir(base, allow_exist=true)
  sync_directory(parent)
  sync_directory(bucket_dir(config, bucket))
  return base
}

///|
fn version_record(version : ObjectVersion) -> String {
  "version-id=" +
  version.id +
  "\norder=" +
  version.order.to_string() +
  "\ndelete-marker=" +
  version.marker.to_string() +
  "\netag=" +
  version.etag +
  "\ncontent-type=" +
  version.content_type +
  "\nmodified=" +
  version.modified.to_string() +
  "\n"
}

///|
async fn import_legacy_version(
  config : Config,
  bucket : String,
  key : String,
) -> Unit {
  if !object_versions(config, bucket, key).is_empty() {
    return
  }
  let legacy = match legacy_version(config, bucket, key) {
    Some(version) => version
    None => return
  }
  let base = ensure_version_directory(config, bucket, key)
  let stage = base + "/.pending"
  if @fs.exists(stage) {
    @fs.rmdir(stage, recursive=true)
  }
  @fs.mkdir(stage)
  copy_storage_file(object_file(config, bucket, key), stage + "/content")
  @fs.write_file(stage + "/record", version_record(legacy), sync=Full)
  sync_directory(stage)
  @fs.rename(stage, base + "/" + new_version_storage())
  sync_directory(base)
}

///|
async fn refresh_current_version(
  config : Config,
  bucket : String,
  key : String,
) -> Unit {
  let versions = object_versions(config, bucket, key)
  let content = object_file(config, bucket, key)
  let metadata = meta_file(config, bucket, key)
  if versions.is_empty() || versions[0].marker {
    @fs.remove(content, allow_missing=true)
    @fs.remove(metadata, allow_missing=true)
  } else {
    let version = versions[0]
    copy_storage_file(
      version_content(config, bucket, key, version),
      content + ".pending",
    )
    @fs.write_file(
      metadata + ".pending",
      "etag=" +
      version.etag +
      "\ncontent-type=" +
      version.content_type +
      "\nmodified=" +
      version.modified.to_string() +
      "\nversion-id=" +
      version.id +
      "\n",
      sync=Full,
    )
    @fs.rename(content + ".pending", content, replace=true)
    @fs.rename(metadata + ".pending", metadata, replace=true)
  }
  sync_directory(objects_dir(config, bucket))
}

///|
async fn apply_version_transaction(
  config : Config,
  bucket : String,
  key : String,
  storage : String,
  remove~ : Bool,
) -> Unit {
  let base = version_dir(config, bucket, key)
  let path = base + "/" + storage
  if remove {
    if @fs.exists(path) {
      @fs.rmdir(path, recursive=true)
    }
  } else {
    if !@fs.exists(path) {
      @fs.rename(base + "/.pending", path)
      sync_directory(base)
    }
    let versions = object_versions(config, bucket, key)
    let added = versions.filter(version => version.storage == storage)[0]
    if added.id == "null" {
      for version in versions {
        if version.id == "null" && version.storage != storage {
          @fs.rmdir(base + "/" + version.storage, recursive=true)
        }
      }
    }
  }
  sync_directory(base)
  refresh_current_version(config, bucket, key)
}

///|
async fn commit_version(
  config : Config,
  bucket : String,
  key : String,
  etag : String,
  content_type : String,
  marker~ : Bool = false,
  upload_id? : String,
) -> String {
  import_legacy_version(config, bucket, key)
  let versions = object_versions(config, bucket, key)
  let order = if versions.is_empty() { 1L } else { versions[0].order + 1L }
  if order < 1L {
    raise Failure("Version sequence overflow")
  }
  let storage = new_version_storage()
  let id = if versioning_status(config, bucket) == "Enabled" {
    storage
  } else {
    "null"
  }
  let base = ensure_version_directory(config, bucket, key)
  let stage = base + "/.pending"
  if @fs.exists(stage) {
    @fs.rmdir(stage, recursive=true)
  }
  @fs.mkdir(stage)
  let modified = if marker {
    (@env.now() / 1000UL).reinterpret_as_int64()
  } else {
    file_modified(object_file(config, bucket, key) + ".pending")
  }
  if !marker {
    @fs.rename(
      object_file(config, bucket, key) + ".pending",
      stage + "/content",
    )
  }
  let version : ObjectVersion = {
    id,
    storage,
    order,
    marker,
    etag,
    content_type,
    modified,
    size: 0L,
  }
  @fs.write_file(stage + "/record", version_record(version), sync=Full)
  sync_directory(stage)
  sync_directory(base)
  publish_transaction(
    config,
    bucket,
    key,
    "version\n" + storage + "\n" + upload_id.unwrap_or("-") + "\n",
  )
  id
}

///|
async fn current_modified(
  config : Config,
  bucket : String,
  key : String,
) -> Int64 {
  let text = read_text(meta_file(config, bucket, key))
  match range_number(meta_value(text, "modified")) {
    Some(value) => value
    None => file_modified(object_file(config, bucket, key))
  }
}
