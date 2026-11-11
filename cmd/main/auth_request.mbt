///|
fn signature_timestamp(value : String) -> Int64? {
  if value.length() != 16 || value[8:9] != "T" || value[15:16] != "Z" {
    return None
  }
  let parts : Array[Int32] = []
  for (start, end) in [(0, 4), (4, 6), (6, 8), (9, 11), (11, 13), (13, 15)] {
    let mut number = 0
    for digit in value[start:end].to_owned().to_array() {
      if digit < '0' || digit > '9' {
        return None
      }
      number = number * 10 + digit.to_int() - '0'.to_int()
    }
    parts.push(number)
  }
  let date = @time.PlainDateTime::of(
    parts[0],
    parts[1],
    parts[2],
    hour=parts[3],
    minute=parts[4],
    second=parts[5],
  ) catch {
    _ => return None
  }
  Some(date.to_unix_second())
}

///|
fn signature_time_valid(value : String, now : Int64) -> Bool {
  match signature_timestamp(value) {
    Some(timestamp) => timestamp >= now - 900L && timestamp <= now + 900L
    None => false
  }
}

///|
fn presigned_time_valid(date : String, expires : String, now : Int64) -> Bool {
  let timestamp = match signature_timestamp(date) {
    Some(timestamp) => timestamp
    None => return false
  }
  let lifetime = match parse_list_number(Some(expires), 0) {
    Some(seconds) => seconds
    None => return false
  }
  lifetime >= 1 &&
  lifetime <= 604800 &&
  timestamp <= now + 900L &&
  now < timestamp + lifetime.to_int64()
}

///|
fn valid_credentials(access_key : String?, secret_key : String?) -> Bool {
  match (access_key, secret_key) {
    (None, None) => true
    (Some(access), Some(secret)) =>
      !access.trim().is_empty() && !secret.trim().is_empty()
    _ => false
  }
}
