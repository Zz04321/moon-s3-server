///|
fn xml_element(text : StringView, name : String) -> (String, String)? {
  let rest = match text.trim().strip_prefix("<" + name + ">") {
    Some(rest) => rest
    None => return None
  }
  match rest.split_once("</" + name + ">") {
    Some((value, rest)) => Some((value.to_owned(), rest.to_owned()))
    None => None
  }
}

///|
fn parse_completed_parts(text : StringView) -> Array[(Int32, String)]? {
  let mut source = text.trim().to_owned()
  if source.has_prefix("<?xml ") {
    source = match source.split_once("?>") {
      Some((_, rest)) => rest.trim().to_owned()
      None => return None
    }
  }
  let root = "CompleteMultipartUpload"
  source = source.replace_all(
    old="<CompleteMultipartUpload xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">",
    new="<CompleteMultipartUpload>",
  )
  let (contents, trailing) = match xml_element(source, root) {
    Some(pair) => pair
    None => return None
  }
  if !trailing.trim().is_empty() {
    return None
  }
  let mut remaining = contents
  let parts : Array[(Int32, String)] = []
  while !remaining.trim().is_empty() {
    let (part, rest) = match xml_element(remaining, "Part") {
      Some(pair) => pair
      None => return None
    }
    remaining = rest
    let mut fields = part
    let mut number : Int32? = None
    let mut etag : String? = None
    for _ in 0..<2 {
      if fields.trim().has_prefix("<PartNumber>") && number is None {
        let (value, rest) = match xml_element(fields, "PartNumber") {
          Some(pair) => pair
          None => return None
        }
        number = parse_list_number(Some(value.trim().to_owned()), 0)
        fields = rest
      } else if fields.trim().has_prefix("<ETag>") && etag is None {
        let (value, rest) = match xml_element(fields, "ETag") {
          Some(pair) => pair
          None => return None
        }
        etag = Some(value.trim().to_owned().replace_all(old="&quot;", new="\""))
        fields = rest
      } else {
        return None
      }
    }
    if !fields.trim().is_empty() {
      return None
    }
    match (number, etag) {
      (Some(number), Some(etag)) => {
        if number < 1 || number > 10000 {
          return None
        }
        let unquoted = if etag.has_prefix("\"") &&
          etag.has_suffix("\"") &&
          etag.length() >= 2 {
          etag[1:etag.length() - 1].to_owned()
        } else {
          etag
        }
        if unquoted.length() != 32 {
          return None
        }
        for digit in unquoted.to_array() {
          if !((digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f')) {
            return None
          }
        }
        parts.push((number, unquoted))
      }
      _ => return None
    }
    if parts.length() > 10000 {
      return None
    }
  }
  if parts.is_empty() {
    None
  } else {
    Some(parts)
  }
}
