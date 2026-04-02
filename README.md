# TempMail

一个自托管、仅接收不发送的临时邮箱系统。
本项目仅供个人非商业使用，任何商业用途需获得作者书面授权。

它的定位很明确：

- 通过 API 创建临时邮箱
- 通过 token 访问收件箱
- 通过自建 SMTP/MX 接收入站邮件
- 通过 `/admin` 管理后台查看概览、日志、域名、策略和运行时配置

它不是完整企业邮箱，不提供 SMTP 发信、IMAP、POP3，也不做长期归档。更适合验证码接收、自动化流程、测试环境和私有临时邮箱服务。

## 目录

- [项目定位](#项目定位)
- [核心能力](#核心能力)
- [系统架构](#系统架构)
- [部署前准备](#部署前准备)
- [快速部署](#快速部署)
- [关键环境变量](#关键环境变量)
- [运行时配置与部署级配置](#运行时配置与部署级配置)
- [API 使用说明](#api-使用说明)
- [管理后台](#管理后台)
- [基础域名管理与自动 MX 校验](#基础域名管理与自动-mx-校验)
- [域名策略](#域名策略)
- [邮件接收与清理流程](#邮件接收与清理流程)
- [ACME 证书签发与续期](#acme-证书签发与续期)
- [运维命令](#运维命令)
- [排错指南](#排错指南)
- [目录结构](#目录结构)
- [安全建议](#安全建议)

## 项目定位

TempMail 的核心使用流程如下：

1. 客户端调用 `POST /api/v1/mailboxes` 创建邮箱。
2. 服务端返回邮箱地址与访问 token。
3. 客户端使用 token 访问 `inbox` API，拉取消息列表、查看正文、下载原始邮件和附件。
4. SMTP 侧只接受“已创建、未过期、未禁用、所属基础域名仍处于 active”的收件地址。

系统当前支持三种创建方式：

1. 空请求体直接创建随机邮箱。
2. 传 `domain` 指定基础域名，由系统随机生成前缀和子域。
3. 传 `address` 直接按完整地址创建自定义邮箱。

如果既不传 `domain` 也不传 `address`，系统会：

- 在当前所有 `active` 基础域名中随机选一个
- 在“邮箱前缀长度范围”中随机选一个长度
- 在“邮箱子域长度范围”中随机选一个长度
- 生成最终地址

例如：

```text
abc123@x8k2.example.com
```

也支持自定义完整地址，例如：

```text
123@abc.example.com
```

如果某个地址此前已过期但尚未被物理清理，系统会先清空旧消息和附件，再用新 token 重建该地址。

### 适合的场景

- 注册、测试、自动化流程中的验证码接收
- 私有化部署的临时邮箱服务
- 需要可控 TTL、可审计、可后台管理的收件系统
- 需要按域名维度控制入站邮件的场景

### 不适合的场景

- 正式企业邮箱
- 需要 SMTP 发信、IMAP、POP3 的业务
- 需要复杂反垃圾、反病毒、多租户权限体系的大型邮件平台
- 需要长期归档与全文检索全部邮件的场景

## 核心能力

- 仅收信，不提供发信能力，部署链路短，维护复杂度低
- API 创建邮箱，按 token 隔离访问权限
- 支持随机邮箱与完整地址自定义创建
- 支持多个基础域名
- 支持后台动态添加基础域名，并自动校验根域 MX 与通配子域 MX
- 支持基础域名自动启用、自动停用、自动重检
- 支持域名停用后，相关邮箱自动视为不可用
- 支持收件箱列表、详情、最新邮件、删除单封、清空邮箱、下载原始 `.eml`、下载附件
- 管理后台支持概览、邮箱管理、消息中心、基础域名管理、域名策略、监控事件、访问日志、管理员审计日志
- 支持运行时热更新业务参数，例如 TTL、邮箱长度范围、业务限流、正文截断、清理策略、域名监控间隔
- 支持收件基础域名与发件域名两类策略匹配
- 支持 `allow` / `reject` / `discard` 三种入站动作
- 原始邮件和附件落盘保存，数据库保存元数据和索引
- 自动清理过期或禁用邮箱及其关联文件
- 启动时自动补齐数据库结构，免手动迁移

## 系统架构

项目基于 Docker Compose 编排，默认包含 7 个服务：

| 服务 | 作用 |
| --- | --- |
| `db` | PostgreSQL，保存邮箱、邮件、附件元数据、访问事件、系统事件、运行时配置与审计日志 |
| `api` | FastAPI，提供邮箱创建、收件箱访问、后台 API、健康检查、管理页面 |
| `janitor` | 周期执行清理任务，标记过期邮箱并物理删除过期数据 |
| `domain-monitor` | 周期检查基础域名的根域 MX 与通配子域 MX，自动启用或停用域名 |
| `postfix` | 接收外部 SMTP 邮件，只允许有效收件地址投递，并把邮件交给 ingest CLI 持久化 |
| `edge` | Nginx，负责 HTTPS、ACME challenge、反向代理、边缘限流 |
| `acme` | `acme.sh`，自动申请和续期 Web/SMTP 证书 |

建议把系统理解为两条主链路：

- Web/API 链路：浏览器或客户端 -> `edge` -> `api` -> `db`
- SMTP 收件链路：外部发件服务器 -> `postfix` -> `tempmail.cli.ingest` -> `db` + `runtime/data`

## 部署前准备

### 域名规划

推荐将 Web/API 与 SMTP/MX 拆成两个子域：

- `mail.<ROOT_DOMAIN>`：Web、API、管理后台
- `mx.<ROOT_DOMAIN>`：SMTP、MX 收件主机

例如：

- `mail.example.com`
- `mx.example.com`

这样做的好处：

- `mail` 可以接入 Cloudflare 代理
- `mx` 必须保持“仅 DNS”，普通 Cloudflare 代理不会转发 25 端口 SMTP
- 如果 `mail` 和 `mx` 最终指向同一台服务器，只要 `mx` 暴露真实 IP，源站 IP 也可能被推断出来

### DNS 配置

至少要为每个允许创建邮箱的基础域名配置：

1. Web/API 域名记录
2. SMTP 主机记录
3. 根域名 MX
4. 通配子域 MX

以 `example.com` 为例：

```dns
mail.example.com   A    <服务器IP>
mx.example.com     A    <服务器IP>
example.com        MX   10 mx.example.com
*.example.com      MX   10 mx.example.com
```

如果启用了多个基础域名，例如：

```env
BASE_DOMAINS=example.com,example.net
```

那么每个域名都必须分别配置根域 MX 和通配子域 MX，否则虽然能创建对应邮箱，但邮件不会投递到你的服务。

### 端口要求

部署机器至少需要放行：

- `25`：SMTP 收件
- `80`：ACME HTTP-01 验证、HTTP 跳转 HTTPS
- `443`：HTTPS、后台和 API 访问

如果云厂商默认封锁 `25` 端口，需要先确认是否能解封，否则邮件无法入站。

### 基础软件要求

- Docker
- Docker Compose Plugin
- 可解析到公网的域名
- 可对外开放 `25/80/443` 的服务器

## 快速部署

### 1. 复制配置文件

```bash
cp .env.example .env
```

### 2. 生成随机密钥

```bash
sh scripts/generate-secrets.sh
```

### 3. 修改 `.env`

至少确认并修改以下关键项：

- `ROOT_DOMAIN`
- `WEB_HOSTNAME`
- `SMTP_HOSTNAME`
- `ACME_EMAIL`
- `POSTGRES_PASSWORD`
- `APP_TOKEN_HASH_SECRET`
- `ADMIN_USERNAME`
- `ADMIN_PASSWORD`
- `ADMIN_SESSION_SECRET`

`BASE_DOMAINS` 是可选的。如果填写，会在启动时作为“基础域名种子”写入数据库并标记为 `active`。后续也可以完全在 `/admin` 后台管理基础域名。

### 4. 启动服务

```bash
docker compose up -d --build
```

### 5. 验证服务

启动后可访问：

- API 根地址：`https://mail.<ROOT_DOMAIN>/`
- 健康检查：`https://mail.<ROOT_DOMAIN>/healthz`
- 就绪检查：`https://mail.<ROOT_DOMAIN>/readyz`
- 管理后台：`https://mail.<ROOT_DOMAIN>/admin`

首次启动时，如果正式证书尚未签发成功，`edge` 和 `postfix` 会先使用短期自签证书。浏览器出现证书告警属于正常现象，等待 `acme` 成功签发后会自动切换。

## 关键环境变量

更完整的注释版请直接查看 [`.env.example`](./.env.example)。

### 域名与证书

| 变量 | 说明 |
| --- | --- |
| `ROOT_DOMAIN` | 根域名，例如 `example.com` |
| `WEB_HOSTNAME` | Web/API/后台域名，默认建议 `mail.example.com` |
| `SMTP_HOSTNAME` | SMTP/MX 主机名，默认建议 `mx.example.com` |
| `ACME_EMAIL` | 证书申请邮箱 |
| `ACME_SERVER` | 证书服务商，默认 `letsencrypt` |
| `ACME_KEYLENGTH` | 证书密钥类型，推荐 `ec-256` |

### 数据库

| 变量 | 说明 |
| --- | --- |
| `POSTGRES_DB` | 数据库名 |
| `POSTGRES_USER` | 数据库用户名 |
| `POSTGRES_PASSWORD` | 数据库密码 |
| `POSTGRES_HOST` | 数据库主机，Compose 默认是 `db` |
| `POSTGRES_PORT` | 数据库端口，默认 `5432` |
| `DATABASE_DSN` | 兼容保留项，当前代码优先读取 `POSTGRES_*` |

### 基础域名与邮箱生成

| 变量 | 说明 |
| --- | --- |
| `BASE_DOMAINS` | 可选的基础域名启动种子，逗号分隔；启动时会写入数据库并标记为 `active` |
| `DEFAULT_BASE_DOMAIN` | 指定默认基础域名；若不传 `domain/address` 且系统没有随机选域需求时可作为优先域名 |
| `MAILBOX_DEFAULT_TTL_MINUTES` | 默认 TTL |
| `MAILBOX_MIN_TTL_MINUTES` | 最小 TTL |
| `MAILBOX_MAX_TTL_MINUTES` | 最大 TTL |
| `MAILBOX_LOCAL_PART_LENGTH` | 兼容旧配置的固定前缀长度；若配置了 MIN/MAX，范围配置优先 |
| `MAILBOX_SUBDOMAIN_LENGTH` | 兼容旧配置的固定子域长度；若配置了 MIN/MAX，范围配置优先 |
| `MAILBOX_LOCAL_PART_MIN_LENGTH` / `MAILBOX_LOCAL_PART_MAX_LENGTH` | 随机邮箱前缀长度范围 |
| `MAILBOX_SUBDOMAIN_MIN_LENGTH` / `MAILBOX_SUBDOMAIN_MAX_LENGTH` | 随机邮箱子域长度范围 |

### 安全与后台

| 变量 | 说明 |
| --- | --- |
| `APP_TOKEN_HASH_SECRET` | 邮箱 token 哈希密钥，必须使用长随机值 |
| `ADMIN_USERNAME` | 后台管理员用户名 |
| `ADMIN_PASSWORD` | 后台管理员密码 |
| `ADMIN_SESSION_SECRET` | 后台 Cookie 签名密钥 |
| `ADMIN_SESSION_HOURS` | 后台会话有效时长 |
| `TOKEN_PREFIX` | 返回给客户端的 token 前缀，默认 `tm_` |
| `TOKEN_BYTES` | token 随机字节数 |

### 邮件解析、清理与域名监控

| 变量 | 说明 |
| --- | --- |
| `MESSAGE_SIZE_LIMIT_BYTES` | 单封邮件最大字节数 |
| `MAX_TEXT_BODY_CHARS` | 保存的纯文本正文最大字符数 |
| `MAX_HTML_BODY_CHARS` | 保存的 HTML 正文最大字符数 |
| `MAX_ATTACHMENTS_PER_MESSAGE` | 单封邮件最大附件数 |
| `PURGE_GRACE_MINUTES` | 过期/禁用后延迟多久开始物理删除 |
| `ACCESS_EVENT_RETENTION_DAYS` | 访问日志保留天数 |
| `CLEANUP_BATCH_SIZE` | 单次清理最多处理的邮箱数 |
| `JANITOR_INTERVAL_SECONDS` | `janitor` 轮询间隔 |
| `DOMAIN_MONITOR_LOOP_SECONDS` | `domain-monitor` 循环执行间隔 |
| `DOMAIN_VERIFY_PENDING_INTERVAL_SECONDS` | 待验证基础域名检查间隔 |
| `DOMAIN_VERIFY_ACTIVE_INTERVAL_SECONDS` | 已启用基础域名检查间隔 |
| `DOMAIN_VERIFY_DISABLED_INTERVAL_SECONDS` | 已停用基础域名重试间隔 |
| `DOMAIN_VERIFY_FAILURE_THRESHOLD` | 已启用基础域名连续失败后自动停用的阈值 |
| `DOMAIN_DNS_TIMEOUT_SECONDS` | 单次 DNS 查询超时 |
| `DOMAIN_DNS_RESOLVERS` | 可选的 DNS 解析器列表，逗号分隔，例如 `1.1.1.1,8.8.8.8` |

### Postfix 连接级限制

| 变量 | 说明 |
| --- | --- |
| `POSTFIX_CLIENT_CONNECTION_RATE_LIMIT` | 单客户端连接速率限制 |
| `POSTFIX_CLIENT_MESSAGE_RATE_LIMIT` | 单客户端消息速率限制 |
| `POSTFIX_CLIENT_RECIPIENT_RATE_LIMIT` | 单客户端收件人数速率限制 |

## 运行时配置与部署级配置

`/admin` 支持热更新的是“运行时业务配置”，例如：

- 邮箱 TTL 范围
- 邮箱前缀与子域长度范围
- 创建与读取收件箱的限流
- 正文截断长度
- 附件保留数量
- 清理策略
- 基础域名监控循环与检查间隔

这些值会写入数据库中的 `admin_runtime_config`，覆盖 `.env` 里的默认值。

下面这些仍然属于“部署级配置”，通常需要改 `.env`，并视情况重建容器：

- 域名、端口、证书相关变量
- `POSTGRES_*`
- `DATA_DIR`
- `EDGE_*`
- `POSTFIX_*`

特别注意：

- `MESSAGE_SIZE_LIMIT_BYTES` 虽然出现在运行时配置中，但 `postfix` 容器启动时也会读取它生成 `main.cf`
- 如果你只在后台改了它，建议同步更新 `.env` 并重建 `postfix`
- `DOMAIN_MONITOR_LOOP_SECONDS`、`DOMAIN_VERIFY_*`、`DOMAIN_VERIFY_FAILURE_THRESHOLD` 也出现在 `/admin` 中，管理员修改后会在后续循环中热生效

## API 使用说明

### 鉴权方式

邮箱 token 支持两种传法：

- `Authorization: Bearer <token>`
- `X-Mailbox-Token: <token>`

后台管理使用 HttpOnly Cookie 会话，不使用公开 Bearer token。

### 1. 创建邮箱

#### 空请求体直接创建随机邮箱

```bash
curl -X POST "https://mail.example.com/api/v1/mailboxes"
```

#### 指定基础域名创建

```bash
curl -X POST "https://mail.example.com/api/v1/mailboxes" \
  -H "Content-Type: application/json" \
  -d '{
    "domain": "example.com",
    "ttl_minutes": 60
  }'
```

#### 指定完整地址创建

```bash
curl -X POST "https://mail.example.com/api/v1/mailboxes" \
  -H "Content-Type: application/json" \
  -d '{
    "address": "123@abc.example.com",
    "ttl_minutes": 60
  }'
```

说明：

- `address` 可选，传完整邮箱地址时按该地址创建
- `domain` 可选，指定基础域名时在该域名下随机生成前缀和子域
- 两者都不传时，系统会在所有 `active` 基础域名中随机选一个
- 使用 `address` 时不要再传 `domain`
- `ttl_minutes` 可选，最终值会被限制在最小/最大 TTL 之间
- 如果 `address` 命中一个已过期但尚未清理的旧邮箱，系统会先清空旧消息和附件，再创建新的 token

典型响应：

```json
{
  "address": "abc123@x8k2.example.com",
  "token": "tm_xxxxx",
  "created_at": "2026-03-29T01:23:45.000000Z",
  "expires_at": "2026-03-29T02:23:45.000000Z",
  "list_messages_url": "https://mail.example.com/api/v1/inbox/messages",
  "message_detail_url_template": "https://mail.example.com/api/v1/inbox/messages/{message_id}"
}
```

### 2. 获取消息列表

```bash
curl "https://mail.example.com/api/v1/inbox/messages?limit=20&offset=0" \
  -H "Authorization: Bearer <token>"
```

### 3. 获取最新一封消息

```bash
curl "https://mail.example.com/api/v1/inbox/messages/latest" \
  -H "Authorization: Bearer <token>"
```

### 4. 获取消息详情

```bash
curl "https://mail.example.com/api/v1/inbox/messages/<message_id>" \
  -H "Authorization: Bearer <token>"
```

### 5. 下载原始邮件

```bash
curl -L "https://mail.example.com/api/v1/inbox/messages/<message_id>/raw" \
  -H "Authorization: Bearer <token>" \
  -o message.eml
```

### 6. 下载附件

```bash
curl -L "https://mail.example.com/api/v1/inbox/messages/<message_id>/attachments/<attachment_id>" \
  -H "Authorization: Bearer <token>" \
  -o attachment.bin
```

### 7. 删除单封消息

```bash
curl -X DELETE "https://mail.example.com/api/v1/inbox/messages/<message_id>" \
  -H "Authorization: Bearer <token>"
```

### 8. 清空当前邮箱

```bash
curl -X DELETE "https://mail.example.com/api/v1/inbox/messages" \
  -H "Authorization: Bearer <token>"
```

### 9. 提前禁用当前邮箱

```bash
curl -X DELETE "https://mail.example.com/api/v1/mailboxes/current" \
  -H "Authorization: Bearer <token>"
```

### 常见状态码

- `200` / `201`：成功
- `400`：参数非法
- `401`：token 缺失、无效、已过期，或基础域名已停用
- `404`：邮箱或消息不存在
- `409`：自定义邮箱地址冲突
- `429`：触发业务限流或边缘限流

## 管理后台

后台地址：

```text
https://mail.<ROOT_DOMAIN>/admin
```

登录后可执行的操作包括：

- 查看概览数据，例如活跃邮箱数、停用邮箱数、最近 24 小时邮件数、基础域名状态
- 分页搜索邮箱
- 查看单个邮箱详情和最近邮件
- 查看消息中心并按邮箱、主题、发件人搜索
- 查看消息详情、下载原始 `.eml`、下载附件、从消息跳转到所属邮箱
- 手动创建随机邮箱或自定义完整地址
- 手动禁用邮箱
- 管理基础域名
- 手动触发基础域名重检
- 管理域名策略
- 在线修改运行时配置
- 查看实时系统事件、访问事件、管理员审计日志

## 基础域名管理与自动 MX 校验

系统支持在 `/admin` 中动态添加基础域名。

基础域名状态包括：

- `pending`
- `active`
- `disabled`

后台添加一个基础域名后，会先进入 `pending`，随后由 `domain-monitor` 自动检查：

1. 根域名 MX 是否指向 `SMTP_HOSTNAME`
2. 一个探测子域的 MX 是否也指向 `SMTP_HOSTNAME`

只有两项都通过，基础域名才会变成 `active`。

已启用基础域名也会定期复检；连续失败达到阈值后会自动变成 `disabled`。基础域名停用后：

- 新邮箱不会再在该域名下创建
- 该域名下已有邮箱会在鉴权与后台状态上表现为停用
- SMTP 不会再接受属于该基础域名的新收件

## 域名策略

`/admin` 提供 Domain Policies 面板，用于在“邮箱是否存在”之外，再加一层域名级别的准入控制。

当前支持两个匹配维度：

- `recipient_base_domain`：按收件邮箱所属基础域名匹配，例如 `example.com`
- `sender_domain`：按发件人地址域名匹配，例如 `gmail.com`

支持三个动作：

- `allow`
- `reject`
- `discard`

匹配规则支持：

- 精确匹配：`example.com`
- 通配所有：`*`
- 后缀匹配：`*.example.com`
- 其他 `fnmatch` 风格匹配

策略按 `priority` 从小到大执行，命中即停止。

## 邮件接收与清理流程

### 接收流程

1. Postfix 接收外部 SMTP 邮件
2. 验证收件地址所属基础域名是否仍为 `active`
3. 验证域名策略
4. 验证邮箱地址是否已创建、未过期、未禁用
5. `ingest` 解析邮件头、正文、附件
6. 原始邮件以 `.eml` 形式写入磁盘
7. 附件落盘，邮件与附件元数据入库
8. 写入系统事件与访问事件

补充说明：

- token 不会明文存库，只保存哈希
- 如果某些正文未预解析，系统会在首次查看详情时按需补解析并回填数据库
- Compose 部署中，SMTP 热路径会跳过每封邮件重复执行启动迁移

### 清理流程

1. `janitor` 先把已过期的 `active` 邮箱标记为 `expired`
2. `expired` 或 `disabled` 的邮箱，超过 `PURGE_GRACE_MINUTES` 后开始物理删除
3. 删除时一并移除原始邮件、附件及相关数据库记录
4. 访问事件与系统事件按保留周期清理

## ACME 证书签发与续期

`acme` 容器使用 `acme.sh` 管理证书。

### 首次签发

- 启动时先等待 `edge` 的 `/.well-known/acme-challenge/` 可访问
- 然后注册 ACME 账号
- 再尝试签发并安装证书

默认申请的域名：

- `WEB_HOSTNAME`
- 如果 `SMTP_HOSTNAME != WEB_HOSTNAME`，则同时把 `SMTP_HOSTNAME` 一起签到同一张证书里

### 首次失败重试

如果首次签发或安装失败，会每 `5` 分钟重试一次。

### 续期

证书安装成功后，`acme` 容器会每 `12` 小时执行一次：

```bash
acme.sh --cron
```

真正是否续期由 `acme.sh` 自己判断，通常只会在证书接近到期时实际续期。

### 续期后生效

证书安装到：

- `./runtime/certs/fullchain.pem`
- `./runtime/certs/privkey.pem`

安装完成后会触发 `touch /signals/certs.updated`，`edge` 和 `postfix` 会根据证书变化 reload。

## 运维命令

### 查看服务状态

```bash
docker compose ps
```

### 查看日志

```bash
docker compose logs -f edge acme postfix api janitor domain-monitor db
```

### 重建并重启全部服务

```bash
docker compose up -d --build
```

### 重建 API、清理任务和域名监控

如果修改了 `api/app` 下的 Python 代码、后台页面、运行时逻辑，至少重建：

```bash
docker compose up -d --build api janitor domain-monitor
```

### 重建 Postfix

如果修改了 SMTP 相关逻辑、Postfix 配置模板、运行时配置模型，重建：

```bash
docker compose up -d --build postfix
```

### 查看 API 根路径

```bash
curl https://mail.example.com/
```

### 查看 OpenAPI 文档

```bash
curl https://mail.example.com/openapi.json
```

## 排错指南

### 1. 证书迟迟未签发

先检查：

- `mail.<ROOT_DOMAIN>` 与 `mx.<ROOT_DOMAIN>` 是否正确解析到服务器
- `80` 端口是否对公网开放
- `edge` 是否能提供 `/.well-known/acme-challenge/*`
- 是否触发了 Let’s Encrypt 频率限制

查看日志：

```bash
docker compose logs -f edge acme
```

### 2. API 可访问，但收不到邮件

先检查：

- `25` 端口是否被云厂商封锁
- `mx.<ROOT_DOMAIN>` 是否保持“仅 DNS”
- 根域名和通配子域 MX 是否都已配置
- 收件地址是否真的是通过 API 创建的地址
- 邮箱是否已过期或被禁用
- 所属基础域名是否仍为 `active`
- 域名策略是否拦截了当前发件域

查看日志：

```bash
docker compose logs -f postfix api
```

### 3. 基础域名明明启用了，但随机邮箱无法收件

这种情况通常不是后台状态问题，而是某个 SMTP/ingest 组件还在运行旧镜像。尤其当你改过 `runtime_config.py`、基础域名逻辑、邮箱长度配置后，记得：

```bash
docker compose up -d --build api postfix janitor domain-monitor
```


### 4. 修改了基础域名监控间隔，但 1 到 3 分钟内没有停用

先确认：

- `DOMAIN_VERIFY_ACTIVE_INTERVAL_SECONDS` 是否真的保存成功
- `DOMAIN_VERIFY_FAILURE_THRESHOLD` 是否为你预期的值
- `domain-monitor` 是否已重建并运行新版镜像
- DNS 解析器是否还缓存着旧 MX

建议同时从服务器上验证：

```bash
dig MX example.com +short
dig MX tmprobe-wildcard-check.example.com +short
docker compose logs -f domain-monitor
```

如果宿主机 `dig` 正常、但 `domain-monitor` 仍反复报 `has no reachable nameserver`，很可能是容器内系统 DNS 解析器不稳定。此时可在 `.env` 中显式指定：

```env
DOMAIN_DNS_RESOLVERS=1.1.1.1,8.8.8.8
```

然后重建：

```bash
docker compose up -d --build api domain-monitor
```

### 5. 创建邮箱时空请求体返回 400/422

当前版本支持空请求体直接创建随机邮箱。如果仍然报“Field required”，通常是 `api` 还在跑旧镜像：

```bash
docker compose up -d --build api
```

### 6. 改了 `MESSAGE_SIZE_LIMIT_BYTES`，但 SMTP 行为不一致

如果你只在后台改了运行时配置，而没有同步更新 `.env` 并重建 `postfix`，API 层与 SMTP 层可能暂时不一致。推荐：

1. 同步修改 `.env`
2. 重建 `postfix`

### 7. 管理后台资源还是旧的

一般是浏览器缓存或容器未重建。后台资源已经使用版本化 URL，但如果容器里的文件本身还是旧版本，页面也不会更新。

建议执行：

```bash
docker compose up -d --build api
```

### 8. 消息搜索变慢

先确认：

- 数据库中是否已创建 `pg_trgm`
- 当前版本代码是否已经完成启动补齐索引

必要时重启 `api`：

```bash
docker compose up -d --build api
```

## 目录结构

```text
tempmail/
  api/                     FastAPI 应用与 CLI
  config/postfix/          Postfix 配置模板
  docker/                  edge / postfix / acme 镜像与入口脚本
  runtime/                 运行时数据、证书、信号目录
  scripts/                 辅助脚本
  sql/                     初始 SQL
  .dockerignore            Docker 构建忽略配置
  .env.example             带注释的配置示例
  docker-compose.yml       服务编排
  README.md                项目说明
```

## 安全建议

- `POSTGRES_PASSWORD`、`APP_TOKEN_HASH_SECRET`、`ADMIN_PASSWORD`、`ADMIN_SESSION_SECRET` 必须使用强随机值
- `mx` 子域不要走 Cloudflare 普通代理
- 后台建议只对可信运维人员开放，必要时在上层再加一层 IP 白名单或 VPN
- 如果 `mail` 和 `mx` 共用同一台机器，要接受 `mx` 可能暴露源站 IP 的事实
- 不要把 `.env`、数据库备份、`runtime/data` 原始邮件目录暴露到公开下载路径
- 如果要长期保留日志和邮件，请额外规划备份和访问审计
