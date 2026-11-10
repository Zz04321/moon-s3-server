# moon-s3-server

用 MoonBit 写的小型 S3 兼容对象存储服务。目标不是复刻 MinIO，而是给 MoonBit 应用、CI 和本地私有化场景一个**开箱能跑、数据放在自己磁盘上**的存储底座。

> 当前版本是 `v0.1 bootstrap`。核心读写链路已经可用，但还不是生产级实现。分段上传、预签名 URL、版本控制和控制台在路线图中，详见下方“当前边界”。

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
| MD5 ETag、Content-Type 元数据 | 已完成 |
| 单区间 Range 下载（指定区间、起点、末尾字节） | 已完成 |
| GET / HEAD 的 ETag 条件请求、ETag If-Range | 已完成 |
| AWS CLI / MinIO `mc` 互通 | 待补充自动测试 |

## 当前边界

现在**还不是**完整的 S3 实现：

- 没有 Multipart Upload；大文件目前一次请求读完，适合中小文件和开发测试；
- 没有 Presigned URL 和按日期判断的条件请求；多区间 Range 请求暂按完整下载处理；
- `LastModified` 暂时是固定占位值；
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

开启认证后，客户端需要正确设置 endpoint 和签名；当前不支持浏览器预签名 URL。
开发时建议先不设置密钥，确认读写链路正常后再开认证。

## 数据布局

```text
data/
  avatars/                     # bucket
    objects/
      64656d6f2f68656c6c6f2e747874.object   # 文件内容，key 用十六进制编码
      64656d6f2f68656c6c6f2e747874.meta     # ETag、Content-Type
```

对象 key 会被编码成安全文件名，避免路径穿越和特殊字符问题。写入先落到 `.tmp`，再原子替换目标文件。

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
```

覆盖 UTF-8 对象名与排序、加号前缀、多页遍历、空页和非法分页参数。
`max-keys` 最大为 1000；非法值和溢出的 continuation token 返回 `400 InvalidArgument`。
`delimiter` 将子目录合并成 `CommonPrefixes`，每个目录和对象各占一个分页名额；`start-after` 按 UTF-8 字节顺序过滤对象和目录。
目前 continuation token 是结果偏移量，翻页需保留相同的 prefix、delimiter 和 start-after；并发增删对象时不保证分页快照一致。
对象名含控制字符时请使用 `encoding-type=url`：响应中的 Key、Prefix、Delimiter、StartAfter 和 CommonPrefixes 会进行 UTF-8 百分号编码（保留 `/`），并返回 `EncodingType=url`；客户端解码一次即可还原对象名。
对象下载回归覆盖区间、跨块读取、越界、空文件和 HEAD。Range 仅用于 GET；格式错误、未知单位和多区间请求按完整下载处理。
GET / HEAD 支持 `If-Match`（不匹配返回 412）和 `If-None-Match`（匹配返回 304），支持通配符和 ETag 列表。
`If-Match` 使用强比较，`If-None-Match` 接受弱 ETag。`If-Range` 仅接受匹配的强 ETag，否则回退完整下载。
日期条件和写入条件暂不支持。

测试不依赖外部数据库或 Docker。完整接口验证见 `scripts/smoke.sh`，CI 配置见 `.github/workflows/ci.yml`。

## 路线图

1. 补 Multipart Upload、日期条件请求和真实 `LastModified`；
2. 补 Presigned URL 和稳定的错误码；
3. 用 AWS CLI、MinIO `mc` 和 Ceph `s3-tests` 子集做互通测试；
4. 增加管理命令行、Web 控制台、备份恢复和事件通知；
5. 增加版本控制、Docker 镜像、发布流程和更多崩溃恢复测试。

## 原创说明

本项目是原创实现，不移植 MinIO，也不复制现有 S3 服务端源码。公开检索未发现 MoonBit 原生的可部署 S3 服务端；现有项目主要是客户端和存储抽象，例如 `mldong/moon-s3`、`mizchi/s3`、`kaashyapan/s3`、`mldong/moon-file-storage`、`mizchi/bit_objstore`。本项目补的是服务端这一层。

开发过程中使用 AI 辅助，所有提交仍以可运行、可测试为准。项目采用 Apache-2.0。

## 许可证

Apache-2.0，见 `LICENSE`。
