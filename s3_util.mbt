///|
pub(all) struct QueryParam {
  name : String
  value : String
}

///|
pub(all) struct ParsedAuth {
  access_key : String
  date : String
  region : String
  service : String
  signed_headers : String
  signature : String
}

///|
pub fn percent_decode(input : StringView) -> String {
  let chars = @utf8.encode(input.to_owned())
  let out : Array[Byte] = []
  let mut i = 0
  while i < chars.length() {
    let c = chars[i]
    if c == b'%' && i + 2 < chars.length() {
      match
        (hex_digit(chars[i + 1].to_char()), hex_digit(chars[i + 2].to_char())) {
        (Some(high), Some(low)) => {
          out.push((high * 16 + low).to_byte())
          i += 3
        }
        _ => {
          out.push(c)
          i += 1
        }
      }
    } else {
      out.push(c)
      i += 1
    }
  }
  @utf8.decode_lossy(Bytes::from_array(out))
}

///|
fn hex_digit(c : Char) -> Int32? {
  let value = c.to_int()
  if value >= '0'.to_int() && value <= '9'.to_int() {
    Some(value - '0'.to_int())
  } else if value >= 'a'.to_int() && value <= 'f'.to_int() {
    Some(value - 'a'.to_int() + 10)
  } else if value >= 'A'.to_int() && value <= 'F'.to_int() {
    Some(value - 'A'.to_int() + 10)
  } else {
    None
  }
}

///|
pub fn uri_encode(input : StringView, allow_slash~ : Bool = false) -> String {
  let bytes = @utf8.encode(input.to_owned())
  let mut out = ""
  for i in 0..<bytes.length() {
    let b = bytes[i].to_int()
    let unreserved = (b >= 65 && b <= 90) ||
      (b >= 97 && b <= 122) ||
      (b >= 48 && b <= 57) ||
      b == 45 ||
      b == 46 ||
      b == 95 ||
      b == 126
    if unreserved || (allow_slash && b == 47) {
      out += b.to_byte().to_char().to_string()
    } else {
      out += "%" + bytes[i].to_hex().to_upper()
    }
  }
  out
}

///|
pub fn query_pairs(query : StringView) -> Array[QueryParam] {
  let result : Array[QueryParam] = []
  for part in query.split("&") {
    if !part.is_empty() {
      match part.split_once("=") {
        Some(pair) => {
          let (name, value) = pair
          result.push(QueryParam::{
            name: percent_decode(name),
            value: percent_decode(value),
          })
        }
        None =>
          result.push(QueryParam::{ name: percent_decode(part), value: "", })
      }
    }
  }
  result
}

///|
pub fn query_value(params : Array[QueryParam], name : StringView) -> String? {
  for param in params {
    if param.name == name.to_owned() {
      return Some(param.value)
    }
  }
  None
}

///|
pub fn canonical_query(params : Array[QueryParam]) -> String {
  let encoded : Array[(String, String)] = []
  for param in params {
    encoded.push((uri_encode(param.name), uri_encode(param.value)))
  }
  encoded.sort_by((left, right) => {
    let order = left.0[:].lexical_compare(right.0[:])
    if order == 0 {
      left.1[:].lexical_compare(right.1[:])
    } else {
      order
    }
  })
  encoded.map(pair => pair.0 + "=" + pair.1).join("&")
}

///|
pub fn split_raw_path(path : StringView) -> (String, String) {
  match path.split_once("?") {
    Some(pair) => (pair.0.to_owned(), pair.1.to_owned())
    None => (path.to_owned(), "")
  }
}

///|
pub fn split_bucket_key(path : StringView) -> (String?, String?) {
  let decoded = percent_decode(path)
  let without_slash = if decoded.has_prefix("/") {
    decoded[1:]
  } else {
    decoded
  }
  if without_slash.is_empty() {
    return (None, None)
  }
  match without_slash.split_once("/") {
    Some(pair) => {
      let (bucket, key) = pair
      if key.is_empty() {
        (Some(bucket.to_owned()), None)
      } else {
        (Some(bucket.to_owned()), Some(key.to_owned()))
      }
    }
    None => (Some(without_slash.to_owned()), None)
  }
}

///|
pub fn valid_bucket_name(name : StringView) -> Bool {
  if name.length() < 3 || name.length() > 63 {
    return false
  }
  let chars = name.to_owned().to_array()
  for i in 0..<chars.length() {
    let c = chars[i]
    let ok = (c >= 'a' && c <= 'z') ||
      (c >= '0' && c <= '9') ||
      c == '-' ||
      c == '.'
    if !ok {
      return false
    }
  }
  let first = chars[0]
  let last = chars[chars.length() - 1]
  first != '-' && first != '.' && last != '-' && last != '.'
}

///|
pub fn key_to_filename(key : StringView) -> String {
  @crypto.bytes_to_hex_string(@utf8.encode(key.to_owned()))
}

///|
pub fn filename_to_key(name : StringView) -> String? {
  let chars = name.to_owned().to_array()
  if chars.length() == 0 || chars.length() % 2 != 0 {
    return None
  }
  let bytes : Array[Byte] = []
  let mut i = 0
  while i < chars.length() {
    match (hex_digit(chars[i]), hex_digit(chars[i + 1])) {
      (Some(high), Some(low)) => bytes.push((high * 16 + low).to_byte())
      _ => return None
    }
    i += 2
  }
  Some(@utf8.decode_lossy(Bytes::from_array(bytes)))
}

///|
pub fn xml_escape(value : StringView) -> String {
  value
  .to_owned()
  .replace_all(old="&", new="&amp;")
  .replace_all(old="<", new="&lt;")
  .replace_all(old=">", new="&gt;")
  .replace_all(old="\"", new="&quot;")
  .replace_all(old="'", new="&apos;")
}

///|
pub fn sha256_hex(data : BytesView) -> String {
  @crypto.bytes_to_hex_string(@crypto.sha256(data))
}

///|
pub fn md5_hex(data : BytesView) -> String {
  @crypto.bytes_to_hex_string(@crypto.md5(data))
}

///|
pub fn hmac_sha256(key : BytesView, data : BytesView) -> FixedArray[Byte] {
  @crypto.hmac(@crypto.SHA256::new(), key, data)
}

///|
pub fn hmac_sha256_hex(key : BytesView, data : BytesView) -> String {
  @crypto.bytes_to_hex_string(hmac_sha256(key, data))
}

///|
pub fn signing_key(
  secret : StringView,
  date : StringView,
  region : StringView,
  service : StringView,
) -> FixedArray[Byte] {
  let k_date = hmac_sha256(
    @utf8.encode("AWS4" + secret.to_owned()).clamped_view(),
    @utf8.encode(date.to_owned()).clamped_view(),
  )
  let k_region = hmac_sha256(
    Bytes::from_array(k_date[:]).clamped_view(),
    @utf8.encode(region.to_owned()).clamped_view(),
  )
  let k_service = hmac_sha256(
    Bytes::from_array(k_region[:]).clamped_view(),
    @utf8.encode(service.to_owned()).clamped_view(),
  )
  hmac_sha256(
    Bytes::from_array(k_service[:]).clamped_view(),
    @utf8.encode("aws4_request").clamped_view(),
  )
}

///|
pub fn parse_authorization(value : StringView) -> ParsedAuth? {
  match value.strip_prefix("AWS4-HMAC-SHA256 ") {
    None => None
    Some(rest) => {
      let mut credential : String = ""
      let mut signed_headers : String = ""
      let mut signature : String = ""
      for raw_part in rest.split(",") {
        let part = raw_part.trim()
        if part.has_prefix("Credential=") {
          credential = part[11:].to_owned()
        } else if part.has_prefix("SignedHeaders=") {
          signed_headers = part[14:].to_owned()
        } else if part.has_prefix("Signature=") {
          signature = part[10:].to_owned()
        }
      }
      if credential.is_empty() ||
        signed_headers.is_empty() ||
        signature.is_empty() {
        return None
      }
      let pieces : Array[String] = []
      for piece in credential.split("/") {
        pieces.push(piece.to_owned())
      }
      if pieces.length() != 5 || pieces[4] != "aws4_request" {
        return None
      }
      Some(ParsedAuth::{
        access_key: pieces[0],
        date: pieces[1],
        region: pieces[2],
        service: pieces[3],
        signed_headers,
        signature,
      })
    }
  }
}

///|
pub fn canonical_headers(
  signed_headers : StringView,
  headers : Array[(String, String)],
) -> String {
  let mut out = ""
  for name in signed_headers.split(";") {
    let wanted = name.to_owned()
    let mut value = ""
    for pair in headers {
      if pair.0 == wanted {
        value = pair.1
      }
    }
    let words : Array[String] = []
    for word in value.replace_all(old="\t", new=" ").split(" ") {
      if !word.is_empty() {
        words.push(word.to_owned())
      }
    }
    out += wanted + ":" + words.join(" ") + "\n"
  }
  out
}

///|
pub fn verify_sigv4(
  http_method : StringView,
  canonical_uri : StringView,
  query : Array[QueryParam],
  signed_header_pairs : Array[(String, String)],
  payload_hash : StringView,
  authorization : StringView,
  access_key : StringView,
  secret_key : StringView,
) -> Bool {
  match parse_authorization(authorization) {
    None => false
    Some(auth) => {
      if auth.access_key != access_key.to_owned() || auth.service != "s3" {
        return false
      }
      if auth.region.is_empty() || auth.signature.length() != 64 {
        return false
      }
      for digit in auth.signature.to_array() {
        if !((digit >= '0' && digit <= '9') || (digit >= 'a' && digit <= 'f')) {
          return false
        }
      }
      let mut previous = ""
      let mut has_host = false
      let mut has_date = false
      for name in auth.signed_headers.split(";") {
        if name.is_empty() || previous[:].lexical_compare(name) >= 0 {
          return false
        }
        for character in name.to_owned().to_array() {
          if !((character >= 'a' && character <= 'z') ||
            (character >= '0' && character <= '9') ||
            character == '-') {
            return false
          }
        }
        let mut present = false
        for (header_name, value) in signed_header_pairs {
          if header_name[:] == name && !value.trim().is_empty() {
            present = true
          }
        }
        if !present {
          return false
        }
        if name == "host" {
          has_host = true
        }
        if name == "x-amz-date" {
          has_date = true
        }
        previous = name.to_owned()
      }
      if !has_host || !has_date {
        return false
      }
      let mut amz_date : String = ""
      for pair in signed_header_pairs {
        if pair.0 == "x-amz-date" {
          amz_date = pair.1
        }
      }
      if amz_date.length() != 16 ||
        amz_date[8:9] != "T" ||
        amz_date[15:16] != "Z" {
        return false
      }
      for index in 0..<amz_date.length() {
        if index != 8 &&
          index != 15 &&
          (amz_date[index] < '0' || amz_date[index] > '9') {
          return false
        }
      }
      if amz_date[:8] != auth.date {
        return false
      }
      let canonical_header_text = canonical_headers(
        auth.signed_headers,
        signed_header_pairs,
      )
      let canonical_request : String = http_method.to_owned() +
        "\n" +
        canonical_uri.to_owned() +
        "\n" +
        canonical_query(query) +
        "\n" +
        canonical_header_text +
        "\n" +
        auth.signed_headers +
        "\n" +
        payload_hash.to_owned()
      let scope = auth.date +
        "/" +
        auth.region +
        "/" +
        auth.service +
        "/aws4_request"
      let string_to_sign : String = "AWS4-HMAC-SHA256\n" +
        amz_date +
        "\n" +
        scope +
        "\n" +
        sha256_hex(@utf8.encode(canonical_request).clamped_view())
      let key : FixedArray[Byte] = signing_key(
        secret_key,
        auth.date,
        auth.region,
        auth.service,
      )
      let expected : String = hmac_sha256_hex(
        Bytes::from_array(key[:]).clamped_view(),
        @utf8.encode(string_to_sign).clamped_view(),
      )
      let mut difference = 0
      for index in 0..<expected.length() {
        difference = difference |
          (expected[index].to_int() ^ auth.signature[index].to_int())
      }
      difference == 0
    }
  }
}
