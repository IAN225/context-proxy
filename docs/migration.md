# 从旧版本迁移

## 会做什么

新版本第一次启动时，会打开 `summary.persist_db` 指向的同一个 SQLite 文件，
建好新表，然后把旧的 `sessions` 表导入新的 checkpoint 结构：

| 旧 `sessions` 列 | 迁移去向 |
|---|---|
| `conv_id` | `conversations.legacy_conv_id`（新的 `conv_id` 是 `legacy-<前24位>`） |
| `summary` | 一条 `kind='migrated'`、`pinned=1`、`status='sealed'` 的 checkpoint 的摘要 |
| `compressed_upto` | 该 checkpoint 的 `compressed_upto` |
| `boundary_fp` | 该 checkpoint 的 `legacy_fp` |
| `ts` | `created_at` / `updated_at` |

`summary` 为空或 `compressed_upto <= 0` 的行会跳过（没有可用状态）。

**旧的 `sessions` 表原样保留，不会被删除或改写**，随时可以回滚。
迁移只做一次，做完在 `meta` 表里打 `legacy_migrated=1` 标记，重启不会重复导入。

## 迁移后第一次请求会发生什么

1. 精确哈希（前 5 条 user 消息全文）失配——这是新算法，旧库里没有；
2. 锚点匹配失配——指纹倒排表还是空的；
3. **旧版指纹命中** `legacy_conv_id`，找到迁移过来的 checkpoint；
4. 因为迁移来的 checkpoint 没有指纹数组，用旧的 `boundary_fp` 做一次弱校验：
   重新计算 `body[compressed_upto - 1]` 的旧式哈希，对得上就采信它的 `compressed_upto` 和摘要；
5. 之后这次请求正常走增量压缩，写下第一条**带完整指纹数组**的新 checkpoint，
   同时补上 `conv_key` 和指纹倒排——从此走精确匹配的快路径。

认亲成功后会立刻给这条迁移来的 checkpoint **补写当前的指纹数组**
（日志：`旧库迁移的 checkpoint seq=1 补写指纹数组（N 条）`）。
不这么做的话，它要等到下一次真正触发压缩才有指纹，而"原文窗口回退"期间可能很久都不触发压缩，
这段时间里用户改早期消息是检测不到的。

如果第 4 步校验失败（比如该位置的消息含图片，新旧文本渲染口径不同），
会走定位兜底：保留最近若干轮原文 + 迁移过来的摘要，并告诉主模型衔接处可能对不上。
不会丢摘要，也不会从第 0 条全量重压。

## 存量会话「近期原文只剩几条」怎么办

旧版本的 bug 可能把 `compressed_upto` 推到离末尾很近的位置，导致每轮只发几条原文。
**升级后不需要做任何事**：原文窗口是每次请求现算的、不落盘，下一次请求就会自动回退补足到
`keep_recent_tokens`（日志里会打一条 `近期原文只剩 N tokens，原文窗口回退` 的 WARNING）。

- 不需要编辑消息，也不需要 `clean` 会话；
- 编辑最后一条消息不会打乱定位——分叉点远在已压缩位置之后，checkpoint 照常命中；
- 回退期间那几轮会同时出现在摘要和原文里，重叠无害（提示词里明确以原文为准）；
- 等对话继续增长、攒够新内容触发一次压缩后，压缩位置正常推进，重叠自动消失。

期间不会额外调用摘要模型，等效总量也稳定在「摘要 + keep_recent」左右，不会多花钱。

## 操作步骤

```bash
cd ~/context-proxy
cp logs/sessions.db logs/sessions.db.bak      # 保险起见先备份
sudo systemctl stop context-proxy             # 或 ./ctl.sh stop

# 部署新代码，然后按 README 更新 config.yaml（新增了 multimodal / fallback / prompts / stream 等段）

sudo systemctl start context-proxy
curl -s localhost:8787/health | python3 -m json.tool
```

`/health` 里的 `legacy_migrated` 就是本次导入的会话数。日志里也有：

```
旧 sessions 表迁移完成：导入 2 个会话为 migrated checkpoint
```

## 配置文件的变化

旧配置的字段基本都还在，位置有两处调整，另外新增了几段：

| 变化 | 说明 |
|---|---|
| `summary.system_prompt` → `summary.prompts.batch_system` | 内容可以直接搬过来 |
| 新增 `summary.prompts.recompress` | 二次重压提示词。**旧的 `recompress_chunk` / `recompress_merge` 已合并成这一条**：旧配置里如果有 `recompress_chunk`，会自动当作 `recompress` 沿用，两个旧键被忽略，不会报错 |
| 新增 `observability.capture_timeline` / `preview_chars` | 请求快照，供 `/ui` 画时间轴。默认关闭 |
| 新增 `server.ui_token` | 可视化页面的登录密钥，留空则页面不启用 |
| 新增 `summary.prompts.injection` | 注入给主模型的包装语，`{summary}` 是占位符 |
| 新增 `summary.prompts.fallback_notice` | 定位兜底时追加的警告语 |
| 新增 `summary.fallback.*` | 备用摘要模型 |
| 新增 `summary.max_batches_per_request` / `exit_gate_ratio` / `checkpoint_keep` / `min_output_tokens` / `main_max_attempts` / `summary_role` | 见 config.yaml 注释 |
| `summary.stream_smooth_chars` / `stream_smooth_delay` → `stream.smooth_chars` / `smooth_delay` | 另外新增 `stream.flush_backlog_chars` |
| 新增 `providers[].multimodal` | 纯文本模型必须设 `false` |
| 新增 `tokenizer.image_tokens` | 图片的等效 token 估值 |
| 新增 `logging.file` / `max_bytes` / `backup_count` | 进程内轮转，跨重启保留 |
| `summary.session_ttl_seconds` | 已移除（会话本来就永久保留，这个字段没有生效路径） |

缺失的字段都有默认值，不会因为少写而启动失败；但 `providers[].multimodal`
和 `summary.fallback` 建议显式配好。

## 回滚

新版本不改动旧 `sessions` 表，直接换回旧代码 + 旧 `config.yaml` 即可，
旧代码只读 `sessions` 表，看不见新表。新表会一直留在同一个 db 文件里，
下次再升级上来时 `legacy_migrated` 标记还在，不会重复导入。

想彻底重来：

```bash
./ctl.sh clean all        # 只清新表里的会话，旧 sessions 表不动
```
