///|
fn list_entries(
  keys : Array[String],
  prefix : String,
  delimiter : String,
  start_after : String,
) -> Array[(String, Bool)] {
  let entries : Array[(String, Bool)] = []
  let groups : Map[String, Bool] = Map([])
  for key in keys {
    if !key.has_prefix(prefix) {
      continue
    }
    let group = if delimiter.is_empty() {
      None
    } else {
      match key[prefix.length():].split_once(delimiter) {
        Some((before, _)) => Some(prefix + before.to_owned() + delimiter)
        None => None
      }
    }
    let name = group.unwrap_or(key)
    if @utf8.encode(name)
      .clamped_view()
      .lexical_compare(@utf8.encode(start_after).clamped_view()) <=
      0 {
      continue
    }
    match group {
      Some(_) =>
        if !groups.contains(name) {
          groups[name] = true
          entries.push((name, true))
        }
      None => entries.push((name, false))
    }
  }
  entries.sort_by((left, right) => {
    @utf8.encode(left.0)
    .clamped_view()
    .lexical_compare(@utf8.encode(right.0).clamped_view())
  })
  entries
}
