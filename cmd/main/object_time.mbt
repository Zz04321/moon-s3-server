///|
fn pad_time(number : Int32, width : Int32) -> String {
  let mut text = number.to_string()
  while text.length() < width {
    text = "0" + text
  }
  text
}

///|
fn iso_time(timestamp : Int64) -> String raise {
  let date = @time.unix(timestamp, zone=@time.utc_zone)
  pad_time(date.year(), 4) +
  "-" +
  pad_time(date.month(), 2) +
  "-" +
  pad_time(date.day(), 2) +
  "T" +
  pad_time(date.hour(), 2) +
  ":" +
  pad_time(date.minute(), 2) +
  ":" +
  pad_time(date.second(), 2) +
  ".000Z"
}

///|
fn http_time(timestamp : Int64) -> String raise {
  let date = @time.unix(timestamp, zone=@time.utc_zone)
  let weekday = match date.weekday() {
    Monday => "Mon"
    Tuesday => "Tue"
    Wednesday => "Wed"
    Thursday => "Thu"
    Friday => "Fri"
    Saturday => "Sat"
    Sunday => "Sun"
  }
  let months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
    "Dec",
  ]
  weekday +
  ", " +
  pad_time(date.day(), 2) +
  " " +
  months[date.month() - 1] +
  " " +
  pad_time(date.year(), 4) +
  " " +
  pad_time(date.hour(), 2) +
  ":" +
  pad_time(date.minute(), 2) +
  ":" +
  pad_time(date.second(), 2) +
  " GMT"
}

///|
fn parse_http_time(value : StringView) -> Int64? {
  let text = value.trim().to_owned()
  if text.length() != 29 ||
    text[3:5] != ", " ||
    text[7:8] != " " ||
    text[11:12] != " " ||
    text[16:17] != " " ||
    text[19:20] != ":" ||
    text[22:23] != ":" ||
    text[25:] != " GMT" {
    return None
  }
  let months = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
    "Dec",
  ]
  let mut month = 0
  for index in 0..<months.length() {
    if months[index][:] == text[8:11] {
      month = index + 1
    }
  }
  if month == 0 {
    return None
  }
  let compact = text[12:16].to_owned() +
    pad_time(month, 2) +
    text[5:7].to_owned() +
    "T" +
    text[17:19].to_owned() +
    text[20:22].to_owned() +
    text[23:25].to_owned() +
    "Z"
  let timestamp = match signature_timestamp(compact) {
    Some(timestamp) => timestamp
    None => return None
  }
  let canonical = http_time(timestamp) catch { _ => return None }
  if canonical != text {
    return None
  }
  Some(timestamp)
}

///|
async fn file_modified(path : String) -> Int64 {
  let file = @fs.open(path, mode=ReadOnly)
  defer file.close()
  file.mtime().0
}

///|
async fn bucket_created(config : Config, bucket : String) -> Int64 {
  let directory = bucket_dir(config, bucket)
  let recorded = read_text(directory + "/.created")
  match range_number(recorded.trim()) {
    Some(timestamp) => timestamp
    None => file_modified(directory)
  }
}

///|
fn if_range_matches(value : String, etag : String, modified : Int64) -> Bool {
  value.is_empty() ||
  value == "\"" + etag + "\"" ||
  parse_http_time(value) == Some(modified)
}
