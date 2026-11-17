import datetime
import hashlib
import hmac
import http.client
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET


BLOCK_SIZE = 1024 * 1024
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


class ClientError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def endpoint_address(endpoint):
    address = urllib.parse.urlsplit(endpoint)
    if (address.scheme not in ("http", "https") or not address.hostname
            or address.username is not None or address.password is not None
            or address.path not in ("", "/") or address.query or address.fragment
            or any(ord(character) <= 32 or ord(character) >= 127 for character in endpoint)):
        raise ValueError("endpoint must be an HTTP(S) origin without a path, credentials or query")
    if address.port is not None and not 1 <= address.port <= 65535:
        raise ValueError("endpoint port must be 1-65535")
    return address


def object_path(bucket=None, key=None, parameters=()):
    path = "/"
    if bucket is not None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise ValueError("bucket must be a valid 3-63 character bucket name")
        path += bucket
    if key is not None:
        if bucket is None or not key:
            raise ValueError("object key must not be empty and requires a bucket")
        path += "/" + urllib.parse.quote(key, safe="/-_.~")
    if parameters:
        path += "?" + urllib.parse.urlencode(parameters, quote_via=urllib.parse.quote, safe="-_.~")
    return path


def signing_headers(method, path, host, digest, headers, access_key, secret_key, region):
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    outgoing = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    outgoing.update({"host": host, "x-amz-date": timestamp, "x-amz-content-sha256": digest})
    names = sorted(outgoing)
    raw_path, _, query = path.partition("?")
    parameters = urllib.parse.parse_qsl(query, keep_blank_values=True)
    canonical_query = "&".join(name + "=" + value for name, value in sorted(
        (urllib.parse.quote(name, safe="-_.~"), urllib.parse.quote(value, safe="-_.~")) for name, value in parameters))
    canonical_headers = "".join(name + ":" + outgoing[name] + "\n" for name in names)
    canonical_request = "\n".join([method, raw_path, canonical_query, canonical_headers, ";".join(names), digest])
    scope = timestamp[:8] + "/" + region + "/s3/aws4_request"
    signing_key = ("AWS4" + secret_key).encode()
    for value in [timestamp[:8], region, "s3", "aws4_request"]:
        signing_key = hmac.new(signing_key, value.encode(), hashlib.sha256).digest()
    string_to_sign = "AWS4-HMAC-SHA256\n" + timestamp + "\n" + scope + "\n" + hashlib.sha256(canonical_request.encode()).hexdigest()
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    outgoing["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                 f"SignedHeaders={';'.join(names)}, Signature={signature}")
    return outgoing


class S3Client:
    def __init__(self, endpoint, access_key="", secret_key="", region="us-east-1", timeout=30):
        self.address = endpoint_address(endpoint)
        if bool(access_key) != bool(secret_key) or (access_key and (not access_key.strip() or not secret_key.strip())):
            raise ValueError("access and secret keys must both be nonempty, or both unset")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", region):
            raise ValueError("region must contain letters, digits, underscores or hyphens")
        if not 0 < timeout <= 3600:
            raise ValueError("timeout must be between 0 and 3600 seconds")
        self.endpoint = endpoint.rstrip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.timeout = timeout

    def request(self, method, path, body=b"", headers=None, output=None):
        headers = dict(headers or {})
        if hasattr(body, "read"):
            start = body.tell()
            digest = hashlib.sha256()
            length = 0
            while block := body.read(BLOCK_SIZE):
                digest.update(block)
                length += len(block)
            body.seek(start)
            payload_hash = digest.hexdigest()
        else:
            length = len(body)
            payload_hash = hashlib.sha256(body).hexdigest()
        headers["content-length"] = str(length)
        if self.access_key:
            headers = signing_headers(method, path, self.address.netloc, payload_hash, headers,
                                      self.access_key, self.secret_key, self.region)
        transport = http.client.HTTPSConnection if self.address.scheme == "https" else http.client.HTTPConnection
        connection = transport(self.address.hostname, self.address.port, timeout=self.timeout)
        try:
            connection.request(method, path, body=body, headers=headers, encode_chunked=False)
            response = connection.getresponse()
            metadata = {name.lower(): value for name, value in response.getheaders()}
            if not 200 <= response.status < 300:
                content = response.read(65536)
                code, message = f"HTTP{response.status}", response.reason
                if content:
                    try:
                        error = ET.fromstring(content)
                        code = error.findtext("Code") or code
                        message = error.findtext("Message") or message
                    except ET.ParseError:
                        pass
                raise ClientError(message, response.status, code)
            if output is not None:
                received = 0
                while block := response.read(BLOCK_SIZE):
                    output.write(block)
                    received += len(block)
                if "content-length" in metadata and received != int(metadata["content-length"]):
                    raise ClientError("Download ended before the declared Content-Length")
                return metadata, b""
            content = response.read(16 * BLOCK_SIZE + 1)
            if len(content) > 16 * BLOCK_SIZE:
                raise ClientError("API response exceeds 16 MiB")
            if method != "HEAD" and response.status != 204 and "content-length" in metadata and len(content) != int(metadata["content-length"]):
                raise ClientError("API response ended before the declared Content-Length")
            return metadata, content
        finally:
            connection.close()

    def xml(self, method, path, body=b"", headers=None):
        metadata, content = self.request(method, path, body, headers)
        try:
            document = ET.fromstring(content)
        except ET.ParseError as error:
            raise ClientError("Invalid XML response from server") from error
        return metadata, document

    def list_objects(self, bucket, prefix="", delimiter="", page_size=1000):
        marker = None
        seen = set()
        while True:
            parameters = [("list-type", "2"), ("encoding-type", "url"), ("max-keys", str(page_size)), ("prefix", prefix)]
            if delimiter:
                parameters.append(("delimiter", delimiter))
            if marker is not None:
                parameters.append(("continuation-token", marker))
            _, page = self.xml("GET", object_path(bucket, parameters=parameters))
            for item in page:
                if item.tag == "{" + NS["s3"] + "}Contents":
                    yield {"key": urllib.parse.unquote(item.findtext("s3:Key", namespaces=NS)),
                           "size": int(item.findtext("s3:Size", namespaces=NS)),
                           "etag": item.findtext("s3:ETag", namespaces=NS),
                           "modified": item.findtext("s3:LastModified", namespaces=NS)}
                elif item.tag == "{" + NS["s3"] + "}CommonPrefixes":
                    yield {"prefix": urllib.parse.unquote(item.findtext("s3:Prefix", namespaces=NS))}
            if page.findtext("s3:IsTruncated", namespaces=NS) != "true":
                return
            marker = page.findtext("s3:NextContinuationToken", namespaces=NS)
            if not marker or marker in seen:
                raise ClientError("Server returned a missing or repeated continuation token")
            seen.add(marker)

    def list_versions(self, bucket, prefix="", page_size=1000):
        marker = None
        seen = set()
        while True:
            parameters = [("versions", ""), ("encoding-type", "url"), ("prefix", prefix), ("max-keys", str(page_size))]
            if marker is not None:
                parameters += [("key-marker", marker[0]), ("version-id-marker", marker[1])]
            _, page = self.xml("GET", object_path(bucket, parameters=parameters))
            for item in page:
                if item.tag not in ("{" + NS["s3"] + "}Version", "{" + NS["s3"] + "}DeleteMarker"):
                    continue
                yield {"key": urllib.parse.unquote(item.findtext("s3:Key", namespaces=NS)),
                       "version_id": item.findtext("s3:VersionId", namespaces=NS),
                       "delete_marker": item.tag.endswith("}DeleteMarker"),
                       "latest": item.findtext("s3:IsLatest", namespaces=NS) == "true",
                       "size": int(item.findtext("s3:Size", default="0", namespaces=NS)),
                       "modified": item.findtext("s3:LastModified", namespaces=NS)}
            if page.findtext("s3:IsTruncated", namespaces=NS) != "true":
                return
            marker = (urllib.parse.unquote(page.findtext("s3:NextKeyMarker", default="", namespaces=NS)),
                      page.findtext("s3:NextVersionIdMarker", default="", namespaces=NS))
            if not all(marker) or marker in seen:
                raise ClientError("Server returned missing or repeated version markers")
            seen.add(marker)

    def upload(self, bucket, key, source, content_type, part_size=8 * BLOCK_SIZE):
        if not 5 * BLOCK_SIZE <= part_size <= 512 * BLOCK_SIZE:
            raise ValueError("part size must be 5-512 MiB")
        size = os.fstat(source.fileno()).st_size
        if size <= part_size:
            metadata, _ = self.request("PUT", object_path(bucket, key), source, {"content-type": content_type})
            return {"etag": metadata.get("etag"), "version_id": metadata.get("x-amz-version-id"), "size": size}
        if (size + part_size - 1) // part_size > 10000:
            raise ValueError("upload requires more than 10000 parts; increase --part-size-mib")
        _, initiated = self.xml("POST", object_path(bucket, key, [("uploads", "")]), headers={"content-type": content_type})
        upload_id = initiated.findtext("s3:UploadId", namespaces=NS)
        if not upload_id:
            raise ClientError("Server did not return an upload ID")
        try:
            completed = ET.Element("CompleteMultipartUpload")
            number = 1
            while block := source.read(part_size):
                metadata, _ = self.request("PUT", object_path(bucket, key, [("uploadId", upload_id), ("partNumber", str(number))]), block)
                if not metadata.get("etag"):
                    raise ClientError("Server did not return a part ETag")
                part = ET.SubElement(completed, "Part")
                ET.SubElement(part, "PartNumber").text = str(number)
                ET.SubElement(part, "ETag").text = metadata["etag"]
                number += 1
            metadata, document = self.xml("POST", object_path(bucket, key, [("uploadId", upload_id)]), ET.tostring(completed))
            if document.tag.split("}")[-1] != "CompleteMultipartUploadResult":
                raise ClientError("Multipart completion did not return a completion result")
            return {"etag": document.findtext("s3:ETag", namespaces=NS), "version_id": metadata.get("x-amz-version-id"), "size": size}
        except BaseException:
            try:
                self.request("DELETE", object_path(bucket, key, [("uploadId", upload_id)]))
            except (OSError, http.client.HTTPException, ClientError):
                pass
            raise
