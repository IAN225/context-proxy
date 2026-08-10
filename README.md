# context-proxy

架在 chatbox（Open WebUI 等）和模型 API 之间的 **OpenAI 兼容上下文压缩网关**。
长对话超过阈值时，把较早的历史交给便宜的摘要模型压成摘要再转发，抑制 input token 无限累加。

面向陪伴向长对话（单会话可达数千条消息、几百万 token），首要目标是**省钱**和**不出事**：

- **滑动 checkpoint 链**：每个会话保留最近 10 个 checkpoint + 1 个永久置顶的最早 checkpoint，
  每个都存着当时的累积摘要全文、压缩位置、轮次和逐条指纹数组。
- **精确定位分支点**：用最长公共前缀比对找出用户从哪一轮改起，回退到那之前的 checkpoint，
  而不是清空状态从第 0 条重压。客户端在中间插/删几条只是错位，会被识别并修正，不算分叉。
- **逐批落盘**：每压完一批就把「摘要 + 压缩位置」在同一个事务里写进去。中途失败、进程重启，
  下次请求接着压。一个几十批的长任务被拆成多次请求渐进完成。
- **绝不把未压缩的上下文放行**：超过阈值又没压成功就报错，另有一道出口闸门在转发前实测 token。
- **可观测**：所有压缩日志都带轮次、消息下标区间、压缩前后 token、以及实际出力的模型。

详细设计与取舍见 [docs/design.md](docs/design.md)，排查手册见 [docs/troubleshooting.md](docs/troubleshooting.md)。

```
Open WebUI ──> context-proxy :8787/<provider>/v1 ──> 模型 API
                     └──> 超阈值时调摘要模型压缩较早历史
```

---

## 快速开始

```bash
cd ~/context-proxy
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp config.yaml config.yaml.bak      # 备份后按下面改
```

`config.yaml` 至少要改这几处（其余项都有可用默认值，注释在文件里）：

```yaml
providers:
  - name: run                        # 决定 URL：/run/v1
    base_url: "https://your-gateway/v1"
    api_key: "sk-..."
    multimodal: true                 # 纯文本模型必须设 false，否则带图历史会报错

summary:
  trigger_tokens: 39200              # 等效总量超过就压
  keep_recent_tokens: 19200          # 保留的近期原文下限
  base_url: "https://your-gateway/v1"
  api_key: "sk-..."
  model: "便宜的摘要模型"
  fallback:                          # 可选：主摘要模型失败后的备用
    enabled: true
    base_url: "..."
    api_key: "..."
    model: "..."

server:
  auth_token: "sk-proxy-自定义一个"   # Open WebUI 里填这个，不是供应商的 key
```

> 密钥可以走环境变量，优先级高于明文：
> `UPSTREAM_API_KEY__<NAME大写>`、`SUMMARY_API_KEY`、`SUMMARY_FALLBACK_API_KEY`、`PROXY_AUTH_TOKEN`

启动：

```bash
chmod +x ctl.sh && ./ctl.sh start     # 本地调试用；长期运行请用下面的 systemd
```

在 Open WebUI 里：设置 → 外部连接（OpenAI API）

| 字段 | 值 |
|---|---|
| API Base URL | `http://<服务器IP>:8787/run/v1` |
| API Key | `server.auth_token` |

---

## 长期运行：systemd

用 systemd 托管，开机自启 + 崩溃无限次自动拉起。启用后用 `systemctl` 启停，不再用 `./ctl.sh start/stop`。

```bash
sudo tee /etc/systemd/system/context-proxy.service >/dev/null <<'EOF'
[Unit]
Description=context-proxy - 上下文压缩网关
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/context-proxy
ExecStart=/root/context-proxy/venv/bin/python /root/context-proxy/proxy.py
ExecReload=/bin/kill -HUP $MAINPID

# 崩溃/被 OOM kill 都无条件拉起，且不因为"短时间内重启太多次"而放弃
Restart=always
RestartSec=5
StartLimitIntervalSec=0

# 2C4G 小机器上给它一个上限，超了由 systemd 重启而不是把整机拖死
MemoryMax=1G
MemoryAccounting=yes

# 密钥建议放这里而不是 config.yaml 明文
# Environment=PROXY_AUTH_TOKEN=sk-proxy-xxx
# Environment=SUMMARY_API_KEY=sk-xxx

[Install]
WantedBy=multi-user.target
EOF

cd ~/context-proxy && ./ctl.sh stop || true     # 先停掉手工启的进程
sudo systemctl daemon-reload
sudo systemctl enable --now context-proxy
systemctl status context-proxy
```

| 操作 | 命令 |
|---|---|
| 改了 `.py` 代码 | `sudo systemctl restart context-proxy` |
| 改了 `config.yaml` | `sudo systemctl reload context-proxy`（或 `./ctl.sh reload`） |
| 看日志 | `tail -f logs/proxy.log`（journald 里也有一份：`journalctl -u context-proxy -f`） |

> 日志由进程自己轮转写入 `logs/proxy.log`（append 模式，**跨重启保留**，含完整 traceback），
> 20MB × 5 份。`logs/stderr.log` 只兜住进程级崩溃输出。

---

## 日常管理

```bash
./ctl.sh status              # 运行状态 + /health
./ctl.sh reload              # 热重载 config.yaml（除 tokenizer.encoding 外全部字段都能热改）
./ctl.sh log                 # 实时日志
./ctl.sh sessions            # 所有会话的压缩进度
./ctl.sh session <id前几位>   # 某会话的全部 checkpoint 与摘要全文
./ctl.sh clean <id前几位>     # 清除某会话（下次从头重压）
./ctl.sh clean all           # 清除全部（需二次确认）
```

`/health` 里有几个值得盯的字段：

| 字段 | 含义 |
|---|---|
| `fallback_activations` | 定位兜底触发次数。**长期应当为 0**，非零说明定位逻辑漏了情况，见 troubleshooting |
| `open_compression_events` | 处于 partial 状态的压缩事件数，长期不为 0 说明有会话一直没压完 |
| `conversations` / `checkpoints` | 会话数与 checkpoint 总数 |

---

## 从旧版本升级

旧版的 `logs/sessions.db`（`sessions` 表）会在**首次启动时自动迁移**成新的 checkpoint 结构，
迁移是幂等的，不需要手工操作，已有长对话不会丢状态。细节和回滚办法见
[docs/migration.md](docs/migration.md)。

---

## 文件说明

```
proxy.py              入口
cproxy/config.py      配置加载与热重载
cproxy/messages.py    token 估算 / 逐条指纹 / 轮次切分 / 上游消息净化
cproxy/store.py       SQLite：会话、checkpoint、指纹倒排、旧库迁移
cproxy/locate.py      会话匹配与分支点检测
cproxy/summarizer.py  摘要模型调用：重试分类、fallback、二次重压
cproxy/compress.py    压缩主流程
cproxy/app.py         路由、流式转发、管理接口
config.yaml           配置（含四套提示词）
ctl.sh                管理脚本
tests/                单测 + 端到端测试（假上游）
logs/                 日志与 sessions.db
```

跑测试：

```bash
./venv/bin/pip install pytest
./venv/bin/python -m pytest tests/ -q
```
