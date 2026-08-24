# 排查手册

日志在 `logs/proxy.log`（append + 轮转，**跨重启保留**，含完整 traceback）；
进程级崩溃输出在 `logs/stderr.log`；用 systemd 时 journald 里也有一份。

先看 `/health`：

```bash
./ctl.sh status
```

---

## 一、先看这几个指标

| 字段 | 正常值 | 异常时看哪一节 |
|---|---|---|
| `fallback_activations` | **长期 0** | [兜底频繁触发](#兜底频繁触发) |
| `open_compression_events` | 长期 0（压缩中短暂 >0） | [压缩一直压不完](#压缩一直压不完) |
| `conversations` | 和实际会话数接近 | 明显偏多说明会话匹配失败在建重复档 |
| `conversation_locks` | 几十以内 | 只增不减说明锁泄漏（不应发生） |

---

## 二、常见现象

### 401 unauthorized

chatbox 里的 API Key 要填 `server.auth_token`，**不是供应商的 key**。
`./ctl.sh reload` 报 401 说明脚本没从 `config.yaml` 解析到 token，它会自动回退 SIGHUP，配置仍然生效。

### 404 unknown provider

URL 里的 `<name>` 和 `config.yaml` 的 `providers[].name` 对不上。Base URL 末尾必须是 `/<name>/v1`。

### 502 upstream error

供应商地址 / key 错误或网络不通，日志里有原始报错。

### 503 + `compression_incomplete`

**这是设计中的行为，不是故障**：请求超过阈值但压缩没完成，代理拒绝按全量 token 转发。
错误信息里写清了压到第几轮、还剩多少、失败原因。**直接重发即可从断点继续**，已完成的批次不会重压。

如果它反复出现，看 `error.detail.reason`：

- 摘要模型报错 → 见下一条；
- `final_tokens > gate_tokens` 且 `remaining_rounds` 为 0 → **最近一轮原文本身就超过闸门**
  （用户粘了一大段东西）。调大 `trigger_tokens`，或调小 `keep_recent_tokens` 让保留区更小。

### 摘要模型一直失败

日志里 `proxy.summary` 那几行写明了错误类型：

| 类型 | 含义 | 处理 |
|---|---|---|
| `timeout` | 单批太大或摘要模型太慢 | 调大 `summary.timeout_seconds` 或调小 `summary_batch_tokens` |
| `server` | 上游 5xx | 通常是供应商抖动，会自动退避重试；持续出现就配 `summary.fallback` |
| `rate_limit` | 429 | 会读 `Retry-After` 退避；持续出现说明并发/频率超了供应商配额 |
| `auth` | 401/403/402/余额不足 | **不会重试**。检查 key 和余额 |
| `context` | 单批超出摘要模型上下文 | 会自动二分重投；如果一直触发，调小 `summary_batch_tokens` |
| `short` | 输出不足 `min_output_tokens`（不含思考部分） | 摘要模型在偷懒或被安全策略拦了，换个模型 / 调 prompt |

配一个 `summary.fallback` 备用模型能显著降低整条链路的失败率。

### 日志出现「近期原文只剩 N tokens，原文窗口回退」

不是故障，是下限保护在生效：这个会话存量的压缩位置留不够 `keep_recent_tokens` 了，
代理把原文窗口往回退补足，被回退的那几轮会同时出现在摘要和原文里（冲突以原文为准）。
日志会点明触发原因——用户删了近期消息 / 调大了 `keep_recent_tokens` / 旧库迁移的切点。

下一次真正触发压缩后切点重算，重叠自动消失。如果**每一次请求都在回退**且从不消失，
检查 `keep_recent_tokens` 是不是被调到和 `trigger_tokens` 一样大了——
那样永远攒不出可压的新内容，会一直卡在重叠状态。

### 启动日志出现「keep_recent_tokens 超过 trigger_tokens 的 50%，实际按 N 生效」

近期原文下限有效值自动封顶在 `trigger_tokens` 的一半，配置写得再大也按封顶值生效。
这是防止"硬下限"和出口闸门互相打架。`/health` 里
`keep_recent_tokens_configured` 与 `keep_recent_tokens_effective` 不一致就是这种情况，
想真正留更多近期原文请调高 `trigger_tokens`。

### 503 里写着「压缩已经压无可压」/ `cause: oversize_tail`

近期原文已经封顶了还顶穿闸门，说明**最后一轮原文自己就太大**。错误信息里会报出
最后一轮的 token 数，以及其中多少来自工具调用与工具返回。

工具调用的请求参数和返回结果会整段留在近期原文里，压缩碰不到它们，
是这条路径最常见的成因。按提示编辑或缩短最后一条消息后重发，
并避免在这一轮让模型调用工具；反复出现则说明 `trigger_tokens` 对这个对话设得太小。

### 摘要质量不行 / 想手工改

```bash
./ctl.sh summary <id前几位>        # 看当前生效的摘要全文
EDITOR=nano ./ctl.sh edit <id前几位>  # 改完保存即写回，下次请求生效
```

改动写成新的 `manual` checkpoint，原来那条留着可回退。写回被拒的三种情况：

| 提示 | 原因 |
|---|---|
| `正在压缩中（事件 seq=N 未完成）` | 有 `partial` 事件没收尾。再发一条消息把它推进完，或等它压完 |
| `摘要已被更新（你基于 seq=X，当前是 seq=Y）` | 取回之后又压过一次。重新 `edit` 一遍再改 |
| `超过 summary_total_cap_tokens` | 写太长了。超了会在下次压缩时被自动二次重压洗掉，所以直接拦住 |

想让摘要模型本身压得更好，就调 `summary.prompts.batch_system`（热重载生效），
或者换一个更强的 `summary.model`。

### 兜底频繁触发

`/health` 的 `fallback_activations` 每涨一次，日志里都有一条：

```
[a1b2c3d4] ⚠️ 定位兜底触发（第 3 次）：分叉轮次 7，所有 checkpoint 的已压缩区都被改动，…
```

**这不该频繁发生。** 排查顺序：

1. `./ctl.sh session <id>` 看这个会话的 checkpoint 链：
   `compressed_upto` 是不是一直没推进？`signature_len` 是不是 null（旧库迁移来的还没重新压过）？
2. 日志里搜 `锚点匹配` —— 如果经常出现「最高仅 1 分」，说明客户端每轮都在改写历史消息内容
   （某些 chatbox 会重写 markdown / 规范化空白），指纹全变。
3. 日志里搜 `修正 N 处错位` —— 如果经常出现且 `net_offset` 很大（远超 ±4），
   说明客户端一次性插删了很多条，`locate.WINDOW` 需要调大。
4. 都不是的话就是真 bug，把这条会话的日志片段留下。

兜底本身是安全的（会保留最近若干轮原文 + 最早的摘要，并告诉主模型衔接处可能对不上），
但它意味着一段历史的记忆精度下降了。

### 压缩一直压不完

`open_compression_events` 长期 > 0，`./ctl.sh sessions` 里某个会话一直 `partial`。

- 正常情况：几千条消息的首次压缩本来就要跨多次请求完成
  （`max_batches_per_request` 限制了单请求批数）。多聊几轮就压完了。
- 想一次压完：临时把 `max_batches_per_request` 调大到 `50`，`./ctl.sh reload`，
  在用户不使用时手动发一条消息触发，压完再调回来。
- 如果 `compressed_upto` **完全不动**，说明每次都在同一批失败，看上一节。

### 从第 0 条重新全量压缩

新版本不会再"清零重压"。如果确实看到 `新会话建档` 出现在一个老会话上：

1. 日志里搜这个会话的 `锚点匹配` 与 `分叉` 记录；
2. `./ctl.sh sessions` 看是不是产生了两条 `conversations` 记录（说明匹配失败在建重复档）；
3. 常见原因是客户端把开头几条 user 消息改了（编辑了第一条提问），
   这时精确哈希必然失配，只能靠锚点救回来——检查那几条被改的消息是否 ≥ 20 字。

### 带图片的历史发给纯文本模型报错

把那个 provider 的 `multimodal` 设成 `false`，`./ctl.sh reload`。
转发前会把所有图片换成 `[图片]` 文本。

多模态 provider 也会自动把失效的图片引用（chatbox 删除云端文件后留下的私有 id）
换成 `[图片（原文件已失效）]`，不需要额外配置。

### 流式输出一直转圈 / 输出很慢

先确认 `stream.smooth_chars` 和 `smooth_delay`：

| 偏好 | smooth_chars | smooth_delay |
|---|:---:|:---:|
| 关闭平滑（最快，推荐先试这个） | 0 | 0 |
| 日常平衡 | 24 | 0.008 |
| 明显的打字机效果 | 8 | 0.02 |

`flush_backlog_chars`（默认 600）是保险丝：待发内容积压超过它就丢弃剩余延迟一次性吐完，
所以再激进的参数也不会让总时长失控。

如果关闭平滑后仍然转圈，看日志里这次请求有没有正常走到 `转发上游：… tokens`
并在之后收到上游数据——如果代理侧早就发完了，那是 chatbox 侧的问题。

### 上游账单比日志里的 token 高

`cl100k_base` 对中文/代码会低估上游实际值约 25~30%。应对：

1. 调大 `tokenizer.per_message_overhead`（现在**支持热重载**了，不用 restart）；
2. `trigger_tokens` 往保守侧设，宁可偏早压。

注意 `keep_recent_tokens + summary_total_cap_tokens` 应明显小于 `trigger_tokens`，
否则压完没几轮又触发。经验值：`keep_recent + cap < trigger × 0.8`。

### 启动即退出

`config.yaml` 格式错误或缺字段。手动跑一次看完整报错：

```bash
./venv/bin/python proxy.py
```

`logs/stderr.log` 里也会有（不会被重启抹掉）。

---

## 三、想看内部状态

```bash
./ctl.sh sessions                 # 所有会话：压到第几轮、checkpoint 数、状态
./ctl.sh session <id前几位>        # 该会话全部 checkpoint：seq / kind / pinned / 摘要全文
```

`kind` 的含义：

| kind | 说明 |
|---|---|
| `incremental` | 正常的增量压缩事件 |
| `recompress` | 累积摘要超 cap 后的二次重压结果 |
| `fallback` | 定位兜底时写下的状态 |
| `migrated` | 从旧 `sessions` 表迁移过来的，`signature` 为 null |

`pinned=1` 的那条是永久保留的最早 checkpoint，兜底时用它的摘要。
