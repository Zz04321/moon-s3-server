///|
fn parse_settings(text : String) -> Map[String, String] raise {
  let document = @json.parse(
    text,
    max_nesting_depth=2,
    reject_duplicate_keys=true,
  )
  let fields = match document {
    Object(fields) => fields
    _ => raise Failure("Configuration must be a JSON object")
  }
  let settings : Map[String, String] = Map([])
  for name, value in fields {
    if [
        "multipart_ttl_seconds", "webhook_timeout_ms", "shutdown_timeout_ms", "max_request_bytes",
        "max_buffered_bytes", "max_staged_bytes", "max_connections", "request_timeout_ms",
      ].contains(name) {
      match value {
        Number(number, ..) if number >= 0.0 &&
          number <= 536870912.0 &&
          number == number.to_int64().to_double() =>
          settings[name] = number.to_int64().to_string()
        _ => raise Failure("Invalid numeric configuration setting")
      }
    } else if [
        "root", "listen", "access_key", "secret_key", "cors_origin", "webhook_url",
        "webhook_secret",
      ].contains(name) {
      match value {
        String(value) => settings[name] = value
        _ => raise Failure("Invalid string configuration setting")
      }
    } else {
      raise Failure("Unknown configuration setting")
    }
  }
  settings
}

///|
fn bounded_setting(text : String, minimum : Int32, maximum : Int32) -> Int32 {
  match range_number(text) {
    Some(value) if value >= minimum.to_int64() && value <= maximum.to_int64() =>
      value.to_int()
    _ => 0
  }
}
