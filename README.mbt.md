# moon-s3-server

用 MoonBit 写的小型 S3 兼容对象存储服务。目标不是复刻 MinIO，而是给 MoonBit 应用、CI 和本地私有化场景一个**开箱能跑、数据放在自己磁盘上**的存储底座。

> 当前版本是 `v0.1 bootstrap`。核心读写、预签名 URL 和可续传的分段上传已经可用，但还不是生产级实现。版本控制和控制台在路线图中，详见下方“当前边界”。

## 为什么做这个

MoonBit 已经有 S3 客户端，但没有可以直接部署的 S3 服务端。应用想存头像、文档、构建产物，通常还是得靠 AWS、MinIO 或自己写一套文件管理接口。

这个项目补的是服务端这一层：现有 S3 SDK、`awscli`、MinIO `mc` 等工具可以直接访问，调用方不需要是 MoonBit 程序。第一版面向**单机、本地磁盘、path-style** 场景。

## 当前能做什么

| 能力 | 状态 |
| --- | --- |
| 健康检查 `GET /healthz` | 已完成 |
| 建桶、删桶、列桶 | 已完成 |
| PUT / GET / HEAD / DELETE 对象 | 已完成 |
| ListObjectsV2 前缀、分页、delimiter / CommonPrefixes、start-after | 已完成 |
| ListObjectsV2 URL 编码输出（encoding-type=url） | 已完成 |
| CORS 预检和响应头 | 已完成 |
| 可选 SigV4 Header 认证 | 已完成 |
| SigV4 预签名 URL（临时 GET / HEAD / PUT） | 已完成 |
| botocore 预签名 GET / HEAD / PUT 互通 | 已验证（1.44.0） |
| 分段上传：创建、上传/覆盖分片、列表分页、合并、取消 | 已完成 |
| 分段上传重启续传、过期清理、分段 ETag | 已完成 |
| 对象写入/删除事务日志、启动恢复、并发读取一致性 | 已完成 |
| botocore 分段上传与 100 MiB 完整性校验 | 已验证（1.44.0） |
| MD5 ETag、Content-Type 元数据 | 已完成 |
| 单区间 Range 下载（指定区间、起点、末尾字节） | 已完成 |
| GET / HEAD 的 ETag 与日期条件请求、ETag / 日期 If-Range | 已完成 |
| 对象 Last-Modified、列表 LastModified、持久化桶创建时间 | 已完成 |
| AWS CLI / MinIO `mc` 互通 | 待补充自动测试 |

## 当前边界

现在**还不是**完整的 S3 实现：

- 支持 Multipart Upload，但每个上传请求体（普通 PUT 或单个分片）仍一次读入内存；请选择适合可用内存的分片大小，合并和下载按块处理；
- 单个数据目录只支持一个服务进程，启动时使用文件锁阻止重复打开；故障恢复已验证进程异常退出，断电和磁盘硬件故障仍需专项验证；
- 多区间 Range 请求暂按完整下载处理；HTTP 日期目前接受 IMF-fixdate 格式（例如 `Thu, 29 Feb 2024 12:34:56 GMT`）；
- 没有版本控制、生命周期、IAM/ACL、对象锁和服务端加密；
- 没有 Web 控制台、备份命令和事件通知；
- TLS 不内置，生产部署请放在 Nginx/Caddy 等反向代理后面。

不要直接把它暴露在公网。当前更适合本地开发、CI 测试和受控内网环境。

## 快速开始

需要 MoonBit CLI（本仓库使用 `moon 0.1.20261102` 验证）。

```bash
git clone https://github.com/Zz04321/moon-s3-server.git
cd moon-s3-server

moon build cmd/main --target native

MOON_S3_ROOT=./data \
MOON_S3_LISTEN=127.0.0.1:9000 \
_build/native/debug/build/cmd/main/main.exe
```

服务启动后：

```bash
# 健康检查
curl --noproxy '*' http://127.0.0.1:9000/healthz

# 建桶
curl --noproxy '*' -X PUT http://127.0.0.1:9000/avatars

# 上传
printf 'hello moon-s3-server\n' > hello.txt
curl --noproxy '*' -X PUT \
  -H 'Content-Type: text/plain' \
  --data-binary @hello.txt \
  http://127.0.0.1:9000/avatars/demo/hello.txt

# 下载
curl --noproxy '*' http://127.0.0.1:9000/avatars/demo/hello.txt

# 下载前 10 字节，返回 206 和 Content-Range；越界返回 416
curl --noproxy '*' -H 'Range: bytes=0-9' http://127.0.0.1:9000/avatars/demo/hello.txt

# 把上传或 HEAD 响应中的 ETag 原样放入 If-None-Match；未变化时返回 304
curl --noproxy '*' -H 'If-None-Match: "替换为实际ETag"' http://127.0.0.1:9000/avatars/demo/hello.txt

# 列对象
curl --noproxy '*' 'http://127.0.0.1:9000/avatars?list-type=2&prefix=demo/'

# 按文件夹浏览：子目录作为 CommonPrefixes 返回
curl --noproxy '*' 'http://127.0.0.1:9000/avatars?list-type=2&delimiter=/&prefix=demo/'

# 列桶
curl --noproxy '*' http://127.0.0.1:9000/
```

也可以直接跑完整冒烟测试：

```bash
bash scripts/smoke.sh
```

## 配置

全部通过环境变量配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `MOON_S3_ROOT` | `./data` | 数据目录 |
| `MOON_S3_LISTEN` | `127.0.0.1:9000` | 监听地址 |
| `MOON_S3_CORS_ORIGIN` | `*` | CORS 允许的来源 |
| `MOON_S3_ACCESS_KEY` | 未设置 | 设置后启用 SigV4 Header 认证 |
| `MOON_S3_SECRET_KEY` | 未设置 | 与 Access Key 成对使用 |
| `MOON_S3_MULTIPART_TTL_SECONDS` | `604800` | 未完成上传的有效期，按创建时间计算，范围 1–31536000 秒 |

开启认证后，客户端需要正确设置 endpoint 和签名，也可以使用预签名 URL 临时授权对象上传或下载。
开发时建议先不设置密钥，确认读写链路正常后再开认证。
两项密钥必须同时设置且非空，或同时不设置；缺失任意一项、空白密钥或无效监听地址会使进程以非零状态退出。

## 临时上传和下载链接

先为服务端和签名工具配置相同的 `MOON_S3_ACCESS_KEY`、`MOON_S3_SECRET_KEY`（工具也接受 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`），并通过认证请求创建桶。
链接生成工具只需要 Python 3 标准库，在本地生成签名，无需把密钥传给链接使用者：

```bash
# 为 avatars 桶生成 10 分钟有效的上传链接
UPLOAD_URL=$(python3 scripts/presign.py avatars 'demo/头像 1.png' \
  --endpoint http://127.0.0.1:9000 --method PUT --expires 600)
curl --noproxy '*' -X PUT --data-binary @avatar.png "$UPLOAD_URL"

# 生成同一对象的临时下载链接
DOWNLOAD_URL=$(python3 scripts/presign.py avatars 'demo/头像 1.png' \
  --endpoint http://127.0.0.1:9000 --expires 600)
curl --noproxy '*' "$DOWNLOAD_URL" -o downloaded.png
```

支持 GET、HEAD 和 PUT；HTTP 方法、对象路径、host 和查询参数均受签名保护。有效期为 1–604800 秒（最长 7 天，默认 15 分钟）；到期立即失效，签发时间最多允许比服务器快 15 分钟。
GET 链接支持 Range、ETag 和日期条件读取，HEAD 需单独签名。PUT 使用 `UNSIGNED-PAYLOAD`；需要约束 Content-Type 等请求头时，可通过 SDK 将它们加入 SignedHeaders，调用时必须原样提供。
浏览器可直接 `fetch(url)` 或 `fetch(url, {method: 'PUT', body: file})`；预检沿用 `MOON_S3_CORS_ORIGIN` 配置。
链接在有效期内可重复使用，PUT 可覆盖同名对象；需要提前撤销时可更换服务端密钥，所有旧链接会同时失效。
请使用客户端实际访问的 endpoint 生成链接；反向代理须保留原始路径、查询参数和 host。
当前支持静态密钥的 SigV4 对象链接，不支持 STS 安全令牌、浏览器 POST 表单上传和下载响应头覆盖参数。未启用服务端认证时，请求仍按匿名模式处理，链接不提供访问控制。
使用 AWS SDK 时选择 SigV4 和 path-style；已使用 botocore 1.44.0 验证 `get_object`、`head_object` 和带 Content-Type 签名的 `put_object` 链接。

## 分段上传与续传

分段上传使用标准 S3 API，所有请求沿用服务端认证配置：

| 操作 | 请求 |
| --- | --- |
| 创建上传，返回 UploadId | `POST /bucket/key?uploads` |
| 上传或覆盖同编号分片，返回 ETag | `PUT /bucket/key?uploadId=ID&partNumber=1` |
| 列出已上传分片 | `GET /bucket/key?uploadId=ID&max-parts=1000&part-number-marker=0` |
| 按顺序合并指定分片 | `POST /bucket/key?uploadId=ID`，XML 包含 PartNumber 和 ETag |
| 取消上传并删除分片 | `DELETE /bucket/key?uploadId=ID` |
| 列出未完成上传 | `GET /bucket?uploads&prefix=demo/&max-uploads=1000` |

保存 UploadId 后，即使服务重启，也可以先列出分片，再补传缺失分片或覆盖失败分片。上传列表使用 `key-marker` 和 `upload-id-marker` 继续翻页，分片列表使用 `part-number-marker`；每页最多 1000 项。
分片编号范围为 1–10000；合并列表必须严格递增且 ETag 匹配，除最后一片外每片至少 5 MiB。完成后的 ETag 为 `MD5(各分片原始MD5拼接)-分片数`，并非整个文件的 MD5。
合并校验失败时保留分片和原对象，允许修正后重试；成功合并或取消后删除上传目录。有未完成上传的桶不能删除。
服务启动时清理过期上传和过期损坏清单，运行期间由分段请求触发清理（最多每小时扫描一次）；直接访问过期 UploadId 会立即清理并返回 `NoSuchUpload`。有效期从创建时开始，续传不延长有效期。

使用 botocore 上传的最小示例（服务已启动并创建 `avatars` 桶）：

```python
import os
import botocore.config
import botocore.session

client = botocore.session.get_session().create_client(
    "s3", endpoint_url="http://127.0.0.1:9000", region_name="us-east-1",
    aws_access_key_id=os.environ["MOON_S3_ACCESS_KEY"],
    aws_secret_access_key=os.environ["MOON_S3_SECRET_KEY"],
    config=botocore.config.Config(
        signature_version="s3v4", s3={"addressing_style": "path"}, proxies={},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    ),
)
target = {"Bucket": "avatars", "Key": "demo/large.bin"}
upload_id = client.create_multipart_upload(**target)["UploadId"]
parts = []
with open("large.bin", "rb") as source:
    while chunk := source.read(8 * 1024 * 1024):
        number = len(parts) + 1
        result = client.upload_part(**target, UploadId=upload_id, PartNumber=number, Body=chunk)
        parts.append({"PartNumber": number, "ETag": result["ETag"]})
client.complete_multipart_upload(**target, UploadId=upload_id, MultipartUpload={"Parts": parts})
client.close()
```

示例用于非空文件；空文件可直接普通 PUT。中断后保留 UploadId，通过 `list_parts` 查询并继续上传；放弃任务时调用 `abort_multipart_upload`。SDK 需关闭可选的额外 checksum，以使用当前支持的 MD5 ETag 与 SigV4 请求体校验。
当前不支持 UploadPartCopy、额外 checksum、AWS 分块签名、上传列表的 delimiter 分组。完成请求支持普通 XML 或标准 S3 默认命名空间，以及可选 XML 声明，不支持自定义命名空间前缀、DTD 等扩展；请求体上限为 2 MiB。

## 数据布局

```text
data/
  .lock                        # 服务进程持有的数据目录锁
  avatars/                     # bucket
    .uploads/
      <upload-id>/
        manifest               # key、Content-Type、创建时间
        part.1                 # 已确认的分片；临时写入使用 .tmp
    objects/
      64656d6f2f68656c6c6f2e747874.object   # 文件内容，key 用十六进制编码
      64656d6f2f68656c6c6f2e747874.meta     # ETag、Content-Type
      <key-hex>.object.pending             # 事务提交前的完整内容
      <key-hex>.meta.pending               # 事务提交前的元数据
      <key-hex>.object.txn                 # 已提交、待完成的事务记录
```

对象 key 会被编码成安全文件名，避免路径穿越和特殊字符问题。普通 PUT 和分段合并都先写入 `.pending` 文件并同步，再持久化事务记录，最后替换内容与元数据。删除也先记录事务，再移除文件。响应成功前同步相关文件与目录。

服务启动后、监听请求前会恢复未完成事务：存在有效 `.txn` 时继续提交，只有临时文件时丢弃临时写入并保留原对象。分段合并事务还会清理对应的上传目录；重复恢复不会重复创建对象。事务记录损坏或必要数据缺失时启动失败，并保留现场供检查。已有 `.object` / `.meta` 文件无需迁移。
运行期间遇到存储错误返回 `500 InternalError`；已提交但尚未应用完成的事务会在后续访问时重试恢复。未收到成功响应的写入可能已经提交，客户端应查询对象后决定是否重试。GET 在锁内取得文件句柄及匹配元数据，随后释放锁传输，允许下载期间并发覆盖而不混用新旧内容与 ETag；HEAD 和列表与写入互斥。
当前恢复依赖本地文件系统的原子重命名、文件锁和文件/目录同步语义，已在 Linux 上验证；不支持共享数据目录的多个进程或绕过服务直接修改数据文件。备份时仍需停止服务或使用一致的文件系统快照。

## 开发

```bash
moon check --target native
moon test --target native
moon build cmd/main --target native
moon fmt
```

列表分页回归测试（需要 Python 3 标准库，不需要额外安装依赖）：

```bash
python3 scripts/list_regression.py
python3 scripts/object_regression.py
python3 scripts/auth_regression.py
python3 scripts/presigned_regression.py
python3 scripts/multipart_regression.py
python3 scripts/storage_regression.py
```

覆盖 UTF-8 对象名与排序、加号前缀、多页遍历、空页和非法分页参数。
`max-keys` 最大为 1000；非法值和溢出的 continuation token 返回 `400 InvalidArgument`。
`delimiter` 将子目录合并成 `CommonPrefixes`，每个目录和对象各占一个分页名额；`start-after` 按 UTF-8 字节顺序过滤对象和目录。
目前 continuation token 是结果偏移量，翻页需保留相同的 prefix、delimiter 和 start-after；并发增删对象时不保证分页快照一致。
对象名含控制字符时请使用 `encoding-type=url`：响应中的 Key、Prefix、Delimiter、StartAfter 和 CommonPrefixes 会进行 UTF-8 百分号编码（保留 `/`），并返回 `EncodingType=url`；客户端解码一次即可还原对象名。
对象下载回归覆盖区间、跨块读取、越界、空文件和 HEAD。Range 仅用于 GET；格式错误、未知单位和多区间请求按完整下载处理。
GET / HEAD 支持 `If-Match`（不匹配返回 412）和 `If-None-Match`（匹配返回 304），支持通配符和 ETag 列表。
`If-Match` 使用强比较，`If-None-Match` 接受弱 ETag。`If-Range` 支持匹配的强 ETag 或 Last-Modified 日期，否则回退完整下载。
GET / HEAD 也支持 `If-Modified-Since`（未更新返回 304）和 `If-Unmodified-Since`（已更新返回 412），按 UTC 整秒比较；无效日期忽略。
`If-None-Match` 优先于 `If-Modified-Since`，`If-Match` 优先于 `If-Unmodified-Since`。
`If-Range` 可使用上次响应中的 Last-Modified 日期，只有时间完全一致时返回区间，否则完整下载。写入条件暂不支持。
对象时间来自内容文件的磁盘 mtime，覆盖上传会更新，GET、HEAD 和列表读取不会更新；响应包含 Last-Modified，列表使用 UTC ISO 时间。
新桶在 `.created` 中保存创建时间，不随上传和重启变化；旧桶无此文件时使用目录 mtime 作为兼容回退，不能还原历史创建时间。
浏览器可读取 CORS 暴露的 ETag、Last-Modified、Content-Range 和 Accept-Ranges 响应头。
认证回归使用 Python 标准库生成 SigV4 签名，验证上传、下载、列表和篡改拒绝。
启用认证时，`x-amz-content-sha256` 必须与实际请求体一致；支持 `UNSIGNED-PAYLOAD`，暂不支持 AWS 分块签名上传。
签名头必须包含 host 和 x-amz-date，使用小写名称并按字典序排列，不能重复或引用缺失头；签名日期必须与凭证日期一致。
签名时间与服务器时间的偏差最多为 15 分钟，过期、未来偏差过大或无效日期的请求返回 403。部署时需保持服务器与客户端时钟同步；窗口内的重复请求仍可重放。
预签名回归覆盖 GET / HEAD / PUT、范围下载、过期、有效期边界、路径和签名篡改、重复/缺失参数、签名请求头、CORS 预检与生成工具。
如果 Python 环境已安装 botocore，可运行 `python3 scripts/presigned_regression.py --sdk`，额外验证 AWS SDK 生成的链接；默认回归只依赖 Python 标准库。
分段回归覆盖错误码、失败合并保留原对象、分片覆盖、分页、并发上传、强制停止后续传、取消和过期清理。运行 `python3 scripts/multipart_regression.py --sdk` 可额外验证 botocore 完整生命周期、100 MiB 分段上传后的 SHA-256 校验与跨分片 Range 下载。
存储回归覆盖提交前中断、内容/元数据替换的各个中间状态、删除中断、分段合并恢复、真实 PUT 过程中强制结束服务、并发覆盖时的 GET/HEAD/列表一致性、磁盘操作失败、损坏事务拒绝启动及数据目录进程锁。

测试不依赖外部数据库或 Docker。完整接口验证见 `scripts/smoke.sh`，CI 配置见 `.github/workflows/ci.yml`。

## 路线图

1. 增加断电、磁盘故障和更多异常恢复验证；
2. 补稳定的错误码；
3. 用 AWS CLI、MinIO `mc` 和 Ceph `s3-tests` 子集做互通测试；
4. 增加管理命令行、Web 控制台、备份恢复和事件通知；
5. 增加版本控制、Docker 镜像、发布流程和更多崩溃恢复测试。

## 原创说明

本项目是原创实现，不移植 MinIO，也不复制现有 S3 服务端源码。公开检索未发现 MoonBit 原生的可部署 S3 服务端；现有项目主要是客户端和存储抽象，例如 `mldong/moon-s3`、`mizchi/s3`、`kaashyapan/s3`、`mldong/moon-file-storage`、`mizchi/bit_objstore`。本项目补的是服务端这一层。

开发过程中使用 AI 辅助，所有提交仍以可运行、可测试为准。项目采用 Apache-2.0。

## 许可证

Apache-2.0，见 `LICENSE`。
