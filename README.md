# xclean — 用 X 官方 API 批量清理自己的帖子

只调用 X 官方 v2 接口，遵守平台限速，不注入客户端、不抓会话、不碰非公开接口。

## 它做什么

| 动作 | 接口 | 平台限速 |
|---|---|---|
| 确认账号 | `GET /2/users/me` | — |
| 枚举自己的帖子 | `GET /2/users/{id}/tweets` | 900 / 15 分钟 |
| 删除原创 / 回复 / 引用 | `DELETE /2/tweets/{id}` | 50 / 15 分钟 |
| 撤销转推 | `DELETE /2/users/{id}/retweets/{tweet_id}` | 50 / 15 分钟 |

时间线默认包含回复和转推；转推必须走撤销接口，删原帖 ID 撤销不了转推。

## 准备：创建应用

1. 打开 <https://developer.x.com>，创建一个 Project 和其中的 App。
2. 在 App 的 **User authentication settings** 里开启 OAuth 2.0：
   - Type of App：**Web App**
   - Callback URI：`http://127.0.0.1:8721/callback`
   - Website URL：填任意你自己的地址
3. 权限（Scopes）必须包含：`tweet.read`、`tweet.write`、`users.read`、`offline.access`
4. 记下 **Client ID**（OAuth 2.0 Client ID，不是 API Key，也不是 Bearer Token）。

> 用 app-only 的 Bearer Token 删不了任何东西——删除需要用户上下文令牌。

## 用法

```bash
# 登录（会打开浏览器，授权后自动回调）
python3 xclean.py login --client-id 你的CLIENT_ID

# 确认当前账号
python3 xclean.py whoami

# 先看会删哪些，不执行
python3 xclean.py list --include-replies --include-retweets --limit 20

# 演练（默认就是演练，不会发删除请求）
python3 xclean.py delete --include-replies --include-retweets

# 真删（需输入确认词）
python3 xclean.py delete --include-replies --include-retweets --yes

# 查进度
python3 xclean.py status
```

### 常用筛选

| 参数 | 作用 |
|---|---|
| `--include-replies` / `--exclude-replies` | 是否包含回复 |
| `--include-retweets` / `--exclude-retweets` | 是否包含转推 |
| `--kind original\|reply\|quote\|repost` | 只要某一类，可重复 |
| `--before` / `--after` | 时间范围，ISO8601，如 `2024-01-01T00:00:00Z` |
| `--contains` | 正文含某关键词 |
| `--limit N` | 最多处理 N 条（先筛选再计数） |
| `--account @name` | 校验登录账号，防止误删别的号 |

## 安全设计

- **默认演练**：不加 `--yes` 只列清单、发零个删除请求。
- **二次确认**：加 `--yes` 后还要手输 `DELETE <条数>`，输错即取消。
- **先备份**：每次执行前把待删集合导出到 `~/.xclean/backups/*.jsonl`（权限 600）。
- **断点续跑**：已删 ID 记进 `~/.xclean/state.json`，重跑自动跳过，不会重复请求。
- **审计日志**：每次尝试写 `~/.xclean/audit.jsonl`，含时间、ID、类型、结果。
- **令牌保护**：`~/.xclean/token.json` 权限 600，目录 700；`logout` 可删除。
- **失败分类**：404 记为「已不存在」不重试；403 记录原因；429 按服务器 reset 时间退避重试。

## 省钱模式：用免费的数据归档当清单

读取自己的帖子按 $0.001/条计费。但 X 账号自带的数据归档是**免费下载**的，里面有完整推文 ID。
用它当待删清单，就不必再调时间线接口，读取费用直接归零，只需要为删除付费。

```bash
# 1. 在 X 设置 → 你的账号 → 下载数据存档，等邮件，拿到 zip
# 2. 直接从归档提取清单（zip / 解压目录 / tweets.js 单文件都行）
python3 archive_import.py twitter-2026-10-21-xxxx.zip --include-replies --include-reposts -o archive.jsonl

# 3. 用清单删除，跳过读取计费
python3 xclean.py delete --from-archive archive.jsonl --include-replies --include-retweets --yes
```

对比一下一万条的成本：

| 方式 | 读取 | 删除 | 合计 |
|---|---|---|---|
| 直接走 API 枚举 | $10 | $50 | **$60** |
| 用免费归档当清单 | $0 | $50 | **$50** |

能省的只有读取那部分。删除请求本身按条计费，官方没有免费通道，这点我不会含糊。

## 成本预估与预算护栏

每次执行前都会打印预估费用和耗时，不需要自己算：

```
  读取    用本地归档，不计费
  删除    $0.525（$0.005/条）
  合计    $0.525
  耗时    约 0.5 小时（限速 50 次/15 分钟）
```

想硬性控制花销，加 `--budget`：

```bash
python3 xclean.py delete --from-archive archive.jsonl --budget 1.0 --yes
```

预计花费超过预算时会直接停下并给出「按预算最多能删多少条」，一个请求都不会发出。

## 必须知道的限制

**计费**：X 自 2026 年 2 月 6 日起取消免费档，改为按量付费。官方价目表（2026-10 核对）里：
读自己的帖子 $0.001/条（Owned Reads，同一资源 24 小时内重复读不重复计费），删除按 `Content: Manage` 计 $0.005/请求。
一万条直接走 API 约 $60，改用免费归档当清单约 $50。没有让删除本身免费的官方途径。

**枚举深度**：时间线接口受平台自身返回范围限制，无法保证取回全部历史内容。工具会在执行前列出实际枚举到的最早时间，那就是本次能覆盖的边界。

**不可删的情况**：已删除、被平台隐藏、超出接口可见范围的内容，工具会记为失败并说明原因，不会谎报成功。

**不可恢复**：删除是永久的。备份文件只含文本与 ID，不构成可恢复的帖子。

**限速**：50 次 / 15 分钟意味着每小时约 200 条；一万条大约需要 50 小时。工具会按窗口自动等待，中途可以 Ctrl+C，下次接着跑。

## 文件结构

```
~/.xclean/
├── token.json      # OAuth 令牌（600）
├── state.json      # 已删 / 失败记录
├── audit.jsonl     # 审计日志
└── backups/        # 每次执行前的待删清单（700）
```

## 不做的事

不注入或篡改 X 客户端，不调用非公开接口，不绕过限速或风控，不处理他人账号，不碰点赞、书签、私信和账号本身。
