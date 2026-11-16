///|
#derive(Eq, Debug)
struct CorsRule {
  id : String?
  origins : Array[String]
  methods : Array[String]
  headers : Array[String]
  expose : Array[String]
  max_age : Int32?
}

///|
fn cors_xml_text(value : String) -> String? {
  if value.contains("<") || value.contains("]]>") {
    return None
  }
  let mut result = ""
  let mut remaining = value
  while remaining.split_once("&") is Some((prefix, rest)) {
    result += prefix.to_owned()
    let (entity, after) = match rest.split_once(";") {
      Some(pair) => pair
      None => return None
    }
    result += match entity {
      "amp" => "&"
      "lt" => "<"
      "gt" => ">"
      "quot" => "\""
      "apos" => "'"
      _ => {
        let (digits, base) = if entity.has_prefix("#x") {
          (entity[2:], 16)
        } else if entity.has_prefix("#") {
          (entity[1:], 10)
        } else {
          return None
        }
        if digits.is_empty() {
          return None
        }
        let mut number = 0
        for digit in digits.to_owned().to_array() {
          let value = if digit >= '0' && digit <= '9' {
            digit.to_int() - '0'.to_int()
          } else if base == 16 && digit >= 'a' && digit <= 'f' {
            digit.to_int() - 'a'.to_int() + 10
          } else if base == 16 && digit >= 'A' && digit <= 'F' {
            digit.to_int() - 'A'.to_int() + 10
          } else {
            return None
          }
          if number > (1114111 - value) / base {
            return None
          }
          number = number * base + value
        }
        if !(number == 9 ||
          number == 10 ||
          number == 13 ||
          (number >= 32 && number <= 55295) ||
          (number >= 57344 && number <= 65533) ||
          (number >= 65536 && number <= 1114111)) {
          return None
        }
        number.to_char().unwrap().to_string()
      }
    }
    remaining = after.to_owned()
  }
  result += remaining
  if result
    .to_array()
    .any(character => {
      let number = character.to_int()
      !(number == 9 ||
      number == 10 ||
      number == 13 ||
      (number >= 32 && number <= 55295) ||
      (number >= 57344 && number <= 65533) ||
      (number >= 65536 && number <= 1114111))
    }) {
    None
  } else {
    Some(result)
  }
}

///|
fn parse_cors_xml(text : String) -> Array[CorsRule]? {
  let mut source = text.trim().to_owned()
  if source.has_prefix("<?xml ") {
    source = match source.split_once("?>") {
      Some((_, rest)) => rest.trim().to_owned()
      None => return None
    }
  }
  let (opening, rest) = match source.split_once(">") {
    Some(pair) => pair
    None => return None
  }
  let attributes = match opening.strip_prefix("<CORSConfiguration") {
    Some(attributes) => {
      if !attributes.is_empty() &&
        !attributes.has_prefix(" ") &&
        !attributes.has_prefix("\n") &&
        !attributes.has_prefix("\t") &&
        !attributes.has_prefix("\r") {
        return None
      }
      attributes.trim()
    }
    None => return None
  }
  if attributes != "" &&
    attributes != "xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\"" &&
    attributes != "xmlns='http://s3.amazonaws.com/doc/2006-03-01/'" {
    return None
  }
  let (contents, trailing) = match rest.split_once("</CORSConfiguration>") {
    Some(pair) => pair
    None => return None
  }
  if !trailing.trim().is_empty() {
    return None
  }
  let rules : Array[CorsRule] = []
  let mut remaining = contents.to_owned()
  while !remaining.trim().is_empty() {
    let (fields, rest) = match xml_element(remaining, "CORSRule") {
      Some(pair) => pair
      None => return None
    }
    remaining = rest
    let mut fields = fields
    let mut id : String? = None
    let mut max_age : Int32? = None
    let origins : Array[String] = []
    let methods : Array[String] = []
    let headers : Array[String] = []
    let expose : Array[String] = []
    while !fields.trim().is_empty() {
      let (opening, _) = match fields.trim().split_once(">") {
        Some(pair) => pair
        None => return None
      }
      let name = match opening.strip_prefix("<") {
        Some(name) => name.to_owned()
        None => return None
      }
      let (value, rest) = match xml_element(fields, name) {
        Some(pair) => pair
        None => return None
      }
      let value = match cors_xml_text(value) {
        Some(value) => value
        None => return None
      }
      fields = rest
      match name {
        "AllowedOrigin" => origins.push(value)
        "AllowedMethod" => methods.push(value)
        "AllowedHeader" => headers.push(value)
        "ExposeHeader" => expose.push(value)
        "ID" if id is None => id = Some(value)
        "MaxAgeSeconds" if max_age is None =>
          max_age = match parse_list_number(Some(value), 0) {
            Some(value) => Some(value)
            None => return None
          }
        _ => return None
      }
    }
    rules.push({ id, origins, methods, headers, expose, max_age, })
    if rules.length() > 100 {
      return None
    }
  }
  Some(rules)
}

///|
fn cors_token(value : String, wildcard~ : Bool) -> Bool {
  if value.is_empty() || value.length() > 1024 {
    return false
  }
  let mut stars = 0
  for character in value.to_array() {
    if character == '*' {
      stars += 1
    }
    if !((character >= 'a' && character <= 'z') ||
      (character >= 'A' && character <= 'Z') ||
      (character >= '0' && character <= '9') ||
      "!#$%&'*+-.^_`|~".contains(character.to_string())) {
      return false
    }
  }
  stars <= (if wildcard { 1 } else { 0 })
}

///|
fn valid_cors_rules(rules : Array[CorsRule]) -> Bool {
  if rules.is_empty() || rules.length() > 100 {
    return false
  }
  let ids : Map[String, Bool] = Map([])
  for rule in rules {
    if rule.origins.is_empty() || rule.methods.is_empty() {
      return false
    }
    if rule.id is Some(id) {
      if id.is_empty() || id.length() > 255 || ids.contains(id) {
        return false
      }
      ids[id] = true
    }
    for origin in rule.origins {
      if origin.is_empty() ||
        origin.length() > 2048 ||
        origin
        .to_array()
        .any(character => character.to_int() <= 32 || character.to_int() >= 127) ||
        origin.split("*").count() > 2 {
        return false
      }
    }
    for verb in rule.methods {
      if !["GET", "PUT", "POST", "DELETE", "HEAD"].contains(verb) {
        return false
      }
    }
    if rule.headers.any(value => !cors_token(value, wildcard=true)) ||
      rule.expose.any(value => !cors_token(value, wildcard=false)) {
      return false
    }
  }
  true
}

///|
fn cors_xml(rules : Array[CorsRule]) -> String {
  let mut xml = "<CORSConfiguration xmlns=\"http://s3.amazonaws.com/doc/2006-03-01/\">"
  for rule in rules {
    xml += "<CORSRule>"
    if rule.id is Some(id) {
      xml += "<ID>" + @core.xml_escape(id) + "</ID>"
    }
    for
      (name, values) in [
        ("AllowedOrigin", rule.origins),
        ("AllowedMethod", rule.methods),
        ("AllowedHeader", rule.headers),
        ("ExposeHeader", rule.expose),
      ] {
      for value in values {
        xml += "<" + name + ">" + @core.xml_escape(value) + "</" + name + ">"
      }
    }
    if rule.max_age is Some(max_age) {
      xml += "<MaxAgeSeconds>" + max_age.to_string() + "</MaxAgeSeconds>"
    }
    xml += "</CORSRule>"
  }
  xml + "</CORSConfiguration>"
}
