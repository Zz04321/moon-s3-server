///|
fn signature_time_valid(value : String, now : Int64) -> Bool {
  if value.length() != 16 || value[8:9] != "T" || value[15:16] != "Z" {
    return false
  }
  let parts : Array[Int32] = []
  for (start, end) in [(0, 4), (4, 6), (6, 8), (9, 11), (11, 13), (13, 15)] {
    let mut number = 0
    for digit in value[start:end].to_owned().to_array() {
      if digit < '0' || digit > '9' {
        return false
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
    _ => return false
  }
  let timestamp = date.to_unix_second()
  timestamp >= now - 900L && timestamp <= now + 900L
}
