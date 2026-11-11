#!/usr/bin/env python3
import argparse
import datetime
import hashlib
import hmac
import os
import re
import urllib.parse


def presign(endpoint, bucket, key, access_key, secret_key, method="GET", expires=900,
            region="us-east-1", date=None):
    address = urllib.parse.urlsplit(endpoint)
    if (address.scheme not in ("http", "https") or not address.hostname
            or address.username is not None or address.password is not None
            or address.path not in ("", "/") or address.query or address.fragment):
        raise ValueError("endpoint must be an HTTP(S) origin without a path, credentials or query")
    address.port
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise ValueError("bucket must be a valid 3-63 character bucket name")
    if not key:
        raise ValueError("object key must not be empty")
    if method not in ("GET", "HEAD", "PUT"):
        raise ValueError("method must be GET, HEAD or PUT")
    if not 1 <= expires <= 604800:
        raise ValueError("expires must be between 1 and 604800 seconds")
    if not access_key or not secret_key or not access_key.strip() or not secret_key.strip():
        raise ValueError("access and secret keys must be configured")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", region):
        raise ValueError("region must contain letters, digits, underscores or hyphens")
    date = date or datetime.datetime.now(datetime.timezone.utc)
    amz_date = date.strftime("%Y%m%dT%H%M%SZ")
    scope = amz_date[:8] + "/" + region + "/s3/aws4_request"
    path = "/" + bucket + "/" + urllib.parse.quote(key, safe="/-_.~")
    parameters = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": access_key + "/" + scope,
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    query = urllib.parse.urlencode(sorted(parameters.items()), quote_via=urllib.parse.quote, safe="-_.~")
    canonical_request = "\n".join([method, path, query, "host:" + address.netloc + "\n", "host", "UNSIGNED-PAYLOAD"])
    string_to_sign = "AWS4-HMAC-SHA256\n" + amz_date + "\n" + scope + "\n" + hashlib.sha256(canonical_request.encode()).hexdigest()
    signing_key = ("AWS4" + secret_key).encode()
    for component in [amz_date[:8], region, "s3", "aws4_request"]:
        signing_key = hmac.new(signing_key, component.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlunsplit((address.scheme, address.netloc, path, query + "&X-Amz-Signature=" + signature, ""))


def main():
    parser = argparse.ArgumentParser(description="Generate a temporary Moon S3 object URL using SigV4.")
    parser.add_argument("bucket")
    parser.add_argument("key")
    parser.add_argument("--endpoint", default="http://127.0.0.1:9000")
    parser.add_argument("--method", choices=["GET", "HEAD", "PUT"], default="GET")
    parser.add_argument("--expires", type=int, default=900, help="validity in seconds (1-604800)")
    parser.add_argument("--region", default=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    arguments = parser.parse_args()
    access_key = os.environ.get("MOON_S3_ACCESS_KEY", os.environ.get("AWS_ACCESS_KEY_ID", ""))
    secret_key = os.environ.get("MOON_S3_SECRET_KEY", os.environ.get("AWS_SECRET_ACCESS_KEY", ""))
    try:
        url = presign(arguments.endpoint, arguments.bucket, arguments.key, access_key, secret_key,
                      arguments.method, arguments.expires, arguments.region)
    except ValueError as error:
        parser.error(str(error))
    print(url)


if __name__ == "__main__":
    main()
