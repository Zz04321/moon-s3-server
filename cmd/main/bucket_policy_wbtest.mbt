///|
test "public policies distinguish current and historical permissions" {
  let statement = "{\"Effect\":\"Allow\",\"Principal\":\"*\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::policy-bucket/*\"}"
  let policy = "{\"Version\":\"2012-10-17\",\"Statement\":[" + statement + "]}"
  assert_eq(
    parse_bucket_policy(policy, "policy-bucket"),
    Some({ current: true, historical: false, }),
  )
  assert_eq(
    parse_bucket_policy(
      policy.replace_all(old="s3:GetObject", new="s3:GetObjectVersion"),
      "policy-bucket",
    ),
    Some({ current: false, historical: true, }),
  )
  assert_eq(
    parse_bucket_policy(
      "{\"Version\":\"2012-10-17\",\"Statement\":[]}", "policy-bucket",
    ),
    Some({ current: false, historical: false, }),
  )
  for
    text in [
      policy.replace_all(old="Allow", new="Deny"),
      policy.replace_all(old="s3:GetObject", new="s3:*"),
      policy.replace_all(
        old="arn:aws:s3:::policy-bucket/*",
        new="arn:aws:s3:::other-bucket/*",
      ),
      policy.replace_all(old="\"Effect\":", new="\"Condition\":{},\"Effect\":"),
      policy.replace_all(
        old="\"Effect\":",
        new="\"Effect\":\"Deny\",\"Effect\":",
      ),
      policy.replace_all(
        old="\"Principal\":\"*\"",
        new="\"NotPrincipal\":\"*\"",
      ),
      policy.replace_all(
        old="\"Statement\":",
        new="\"Unknown\":true,\"Statement\":",
      ),
      "{",
      "null",
      "[]",
    ] {
    assert_eq(parse_bucket_policy(text, "policy-bucket"), None)
  }
}
