///|
#derive(Eq, Debug)
enum DownloadRange {
  Full
  Partial(Int64, Int64)
  Unsatisfiable
}

///|
fn range_number(text : StringView) -> Int64? {
  if text.is_empty() {
    return None
  }
  let mut result = 0L
  for digit in text.to_owned().to_array() {
    if digit < '0' || digit > '9' {
      return None
    }
    let value = (digit.to_int() - '0'.to_int()).to_int64()
    result = if result > (9223372036854775807L - value) / 10L {
      9223372036854775807L
    } else {
      result * 10L + value
    }
  }
  Some(result)
}

///|
fn download_range(value : StringView, size : Int64) -> DownloadRange {
  let spec = match value.strip_prefix("bytes=") {
    Some(spec) => spec
    None => return Full
  }
  let (first, last) = match spec.split_once("-") {
    Some(pair) => pair
    None => return Full
  }
  if first.is_empty() {
    match range_number(last) {
      None => Full
      Some(suffix) =>
        if suffix == 0L || size == 0L {
          Unsatisfiable
        } else {
          Partial(if suffix >= size { 0L } else { size - suffix }, size - 1L)
        }
    }
  } else {
    let start = match range_number(first) {
      Some(start) => start
      None => return Full
    }
    let end = if last.is_empty() {
      size - 1L
    } else {
      match range_number(last) {
        Some(end) => {
          if end < start {
            return Full
          }
          if end >= size {
            size - 1L
          } else {
            end
          }
        }
        None => return Full
      }
    }
    if start >= size {
      Unsatisfiable
    } else {
      Partial(start, end)
    }
  }
}
