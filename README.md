# context-proxy

架在 chatbox 和模型 API 之间的 **OpenAI 兼容上下文压缩网关**。
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
chatbox ──> context-proxy :8787/<provider>/v1 ──> 模型 API
                  └──> 超阈值时调摘要模型压缩较早历史
```

---

## 快速开始

```bash
cd ~/context-proxy
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
```

**1. 填 `config.yaml`**，只有这几处必填，其余都有可用默认值：

```yaml
providers:
  - name: <provider>                 # 自己起的名字，决定 URL：/<provider>/v1
    base_url: "https://your-gateway/v1"
    api_key: "sk-..."
    multimodal: true                 # 纯文本模型必须设 false，否则带图历史会报错

summary:
  base_url: "https://your-gateway/v1"
  api_key: "sk-..."
  model: "便宜的摘要模型"

server:
  auth_token: "sk-proxy-自己起一个"   # 客户端里填这个，不是供应商的 key
```

**2. 启动并自检**

```bash
chmod +x ctl.sh && ./ctl.sh start     # 本地调试用；长期运行见下面的 systemd
curl -s localhost:8787/health | python3 -m json.tool
```

**3. 在客户端里按 OpenAI 兼容接口接入**

| 字段 | 值 |
|---|---|
| API Base URL | `http://<服务器IP>:8787/<provider>/v1` |
| API Key | `server.auth_token`（**不是供应商的 key**） |

`<provider>` 换成 `config.yaml` 里配的任意 `name`，每个供应商一个独立 URL，
保存后客户端会自动从 `/<provider>/v1/models` 拉到模型列表。发一条消息能收到回复就通了。

**4.（可选）**想调思考强度这类参数、开可视化页面、改摘要提示词，见下面各节。

> 密钥都可以走环境变量，优先级高于明文：`UPSTREAM_API_KEY__<NAME大写>`、
> `SUMMARY_API_KEY`、`SUMMARY_FALLBACK_API_KEY`、`PROXY_AUTH_TOKEN`、`PROXY_UI_TOKEN`。
> 仓库里的 `config.yaml` 这几项都是空的，**别填了真实密钥再提交**。

阈值按需要调，默认值对应约 39k 触发压缩、保留约 19k 近期原文：

```yaml
summary:
  trigger_tokens: 39200              # 等效总量超过就压
  keep_recent_tokens: 19200          # 近期原文的硬性下限（自动封顶在 trigger 的 50%）
  fallback:                          # 可选：主摘要模型失败后的备用
    enabled: true
    base_url: "..."
    api_key: "..."
    model: "..."
```

---

## 参数与请求头

`messages` 之外的 body 字段**一律原样透传**——`temperature`、`top_p`、`seed`、`tools`、
`response_format`、各家的思考开关、任何厂商私有字段，代理都不认识也不改动，
所以将来出现的新参数自动就支持。

想强制某个客户端界面上表达不了的参数（比如思考强度），用 `extra_body`。
它**覆盖**客户端发来的同名字段；`messages` / `stream` 不接受改写（改了等于绕过压缩、
破坏流式处理），写了会被忽略并告警。

```yaml
providers:
  - name: <provider>
    extra_body:
      reasoning_effort: "xhigh"

summary:
  extra_body:                      # 摘要模型通常配得正好相反：不思考、低温
    temperature: 0.5
    enable_thinking: false
```

⚠️ 思考开关的字段名各家不同（`reasoning_effort` / `thinking` / `enable_thinking` …），
写错的后果还分两种：有的网关直接 400，有的默默丢掉——你以为思考开了其实没开。
所以 `config.yaml` 里几种写法都注释掉了，**别猜，直接问上游**：

```bash
python3 tools/probe_body.py <provider> --model <模型名>   # 直连上游逐个字段试
python3 tools/probe_body.py summary                       # 试主摘要模型
python3 tools/probe_body.py <provider> --model <模型名> --via-proxy   # 走代理，顺带验证透传
```

一个字段发一次最小请求（十几 token），最后打出「可用 / 被拒 / 无法判断」。
判定分三步，**任何一步证据不足都不下结论**：

| 步骤 | 请求 | 结果 |
|---|---|---|
| 1 基线 | 不带任何探针字段 | 非 2xx → 中止（退出码 2）。问题在 key / 模型名 / base_url / 余额，不在字段上 |
| 2 对照 | 一个瞎编的字段 | 400/422 **且报错文本确实在说"不认识这字段"** → 严格；2xx → 宽松；其余 → 无法判断（退出码 1） |
| 3 逐字段 | 每个候选字段 | 2xx → 可用；400/422 → 被拒；其余 → 无法判断 |

- 判为**严格**时：可用项已确认被上游识别，可按需写入 `extra_body`。
- 判为**宽松**时：可用项仅确认不会报错，是否生效要结合报告里的
  「思考 N 字」/ `reasoning_tokens` 等信号判断，别仅凭这一次结果就改配置。
- 「无法判断」的字段（429 / 5xx / 网络错误，重试一次仍失败）**别动**，
  那是这次没问出结果，不是上游不认。`--only <字段>` 过会儿单独重跑。

`--extra '{"top_k": 20}'` 可以试自己的字段。注意这些是**真实计费**的调用，只是每次都很短。

请求头默认**不透传**（无脑转发会把 cookie、`x-forwarded-for` 一起漏给上游），
需要哪个按名字白名单放行；查询串同理：

```yaml
providers:
  - name: <provider>
    forward_headers: ["anthropic-beta", "http-referer", "x-title"]
    forward_query: true            # Azure OpenAI 的 ?api-version= 需要
```

`Authorization` 永远由代理换成该 provider 的 `api_key`（客户端发来的是代理的 token），
写进白名单也不会生效。

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
./ctl.sh session <id前几位>   # 某会话的全部 checkpoint（摘要只给预览）
./ctl.sh summary <id前几位>   # 打印当前生效的摘要全文
./ctl.sh edit <id前几位>      # 用 $EDITOR 直接改摘要（见下节）
./ctl.sh clean <id前几位>     # 清除某会话（下次从头重压）
./ctl.sh clean all           # 清除全部（需二次确认）
```

## 可视化页面

想在浏览器里看和改，先设一个登录密钥（和 chatbox 用的 `auth_token` 是两把钥匙）：

```bash
./ctl.sh ui-token                 # 生成 16 位随机密钥
# 填进 config.yaml 的 server.ui_token，然后
./ctl.sh reload
```

打开 `http://<服务器IP>:8787/ui` 输入密钥即可。**按手机优先做的**，
在手机上是卡片流而不是宽表格，点击目标够大，输入框不会触发 iOS 自动放大。

三个页签：

- **会话** —— 列表与压缩进度、**存档位式的摘要编辑**（见下）、本次请求的
  「进来 → 发出去」对比和对话时间轴。
- **提示词** —— 四套提示词直接在页面上改、保存、一键恢复默认。保存**直接写回 config.yaml**：
  只替换提示词正文，文件里的注释和其余配置一字不动，写前自动备份成 `config.yaml.bak`，
  保存后立即热重载，**重启依然生效**。
- **控制台** —— 正在跑的压缩任务（进度 + 最近一批的摘要输出，可中止），
  以及供应商 / 摘要模型的增删改（见下）。

### 摘要存档位：内容和进度是分开的两件事

这是这套东西里最容易搞混的一点，所以页面上把它做成了「存档位」：

> **压缩标记（压到第几条）是进度，摘要写了什么是内容。选别的摘要 ≠ 让进度倒退。**

举例：1500 条已经压了 1400 条。你翻出一份更早的、覆盖到第 800 条的摘要，
改好之后「设为生效」——那么这 1400 条对应的摘要就是这一份，
后续到 1655 条再触发压缩时，**仍然以它为基准往后追加**，压缩标记还在 1400，不会回退重压。

代价页面会直接标出来：第 801~1400 轮既不在这份摘要里、也不在近期原文窗口里，
成了 **记忆空洞**。所以「设为生效」的按钮上就写着会留下多少轮空洞，
点之前还会再确认一次。要么把这段内容补进正文再存，要么用 `rewind` 让进度一起退回去重压（贵）。

页面上的操作：

| 操作 | 效果 |
|---|---|
| 点某个存档「载入编辑」 | 把它的正文放进编辑区，同时显示它覆盖到第几轮、会不会有空洞 |
| 选「覆盖存档 N」并保存 | 正文写回那个格子，**不改变谁生效**，链上不会多出条目 |
| 选「新建一档并设为生效」 | 新建一条 manual 存档并立刻生效 |
| 点某个存档「设为生效」 | 内容换成它的，压缩进度不动 |

**没选保存目标时，保存按钮是灰的**——避免"改了半天不知道存到哪去了"。

被设为生效的那条是**长期生效**的：固定住（裁剪时永远保留），该会话所有
「上次没压完」的半成品会被作废，重启之后仍然是它。其它存档都还在，随时能选回去。

### 控制台：模型配置与压缩任务

- **供应商 / 摘要模型**可以在页面上改、加、删，包括自定义 `extra_body`。
  **保存前会拿这份配置真调一次**：调不通就不写进文件（写进去下一秒对话就全挂了）。
  保存成功后写回 `config.yaml` 并热重载。
- API key **只回显掩码**（`sk-ab***cdef`），留空就是不改，页面永远拿不到明文。
- **「检测传参方言」**：有些中转站用 OpenAI 的 `/v1` 路径转发 Claude，内部却只认 Anthropic
  那套字段——**所以不看 URL，只看上游认不认**。点一次会逐个试
  `reasoning_effort` / `thinking` / `enable_thinking`，以及 `max_tokens` 与
  `max_completion_tokens`，探到什么就填什么。
- **看得到模型到底吐了什么**：中转站经常把自己的故障当成模型输出发回来
  （HTTP 200 + 一句「池子中没有可用账号」，或者干脆是 Cloudflare 的 HTML 页面）。
  只看状态码会把这种当成成功，所以每次测试都把**完整原始响应**折叠在下面，
  可疑的还会标出来——但**判断权在你**，页面不替你下结论。
- **压缩任务**能看到第几批 / 共几批、已压到第几轮、最近一批的摘要输出，可以**中止**。
  中止只在批与批之间生效：已落盘的批一条都不撕（钱花了，结果留着下次接着用），
  这次请求会返回一条「压缩未完成」的提示，重发消息就从断点继续。

安全上：

- **`ui_token` 留空 = 页面整个不存在**（`/ui` 返回 404），不会不小心把后台裸奔在公网上；
- 两把钥匙**双向隔离**：`ui_token` 只能访问 `/admin/*`，不能调 `/chat/completions`；
  而**一旦设了 `ui_token`，`auth_token` 就不再有后台权限**——它要填进 chatbox、
  跟着每个对话请求走，还能开后台等于把后台钥匙散出去。
  （没设 `ui_token` 时页面本来就是关的，此时 `auth_token` 仍可调 `/admin/*` 供 `./ctl.sh` 用；
  设了之后 `./ctl.sh` 会自动改用 `ui_token`，或用环境变量 `PROXY_UI_TOKEN` 指定。）
- 密钥只存在浏览器的 sessionStorage 里，关掉标签页就没了；
- 页面零外部依赖（不引 CDN），断网 / 内网机器都能打开；
- 连续输错 10 次会被限流。

> 这个页面能看到全部对话摘要。别用弱密钥，也建议只在内网、或加了 HTTPS 的反代后面开放。

### 对话时间轴（默认关闭）

想看「这次请求哪些轮次被折叠进摘要了、上游最终收到多少」，把
`observability.capture_timeline` 设为 `true` 再 `reload`。之后每次请求会存一份**结构快照**：
逐轮的 token、角色、首条消息预览，以及这一轮是折叠还是逐字发出。页面上就有一条
折叠区在上、原文区在下、中间一道分隔线的时间轴。

**只存结构，加每轮开头 `preview_chars` 字（默认 60）的预览，不存完整原文**——
存全量 payload 每次请求要写几十 MB，小机器扛不住，也没必要把对话副本再落一份盘。
注意那 60 字预览是真的原文片段：快照不是脱敏数据，`sessions.db` 该按存放对话来保护。实测开销：4000 条的会话每请求约 +18 ms（构建 13 + 落盘 5），
快照约 460 KB，且每个会话**只留最新一份**（覆盖写），不随请求数增长。不看就关掉，零开销。

## 摘要不满意？手动改

摘要模型压出来的东西不好使时，可以直接改，改完下一次请求就生效：

```bash
./ctl.sh sessions                 # 找到 conv_id
./ctl.sh summary a1b2c3d4         # 先看看现在是什么
EDITOR=nano ./ctl.sh edit a1b2c3d4   # 拉到编辑器里改，保存即写回
```

改动会写成一条新的 `manual` checkpoint，**原来那条留在链上可以回退**；位置信息
（压到第几轮、指纹数组）整套沿用，不影响定位。后续压缩会在你写的内容**之后追加**，
不会覆盖掉。

几条保护：

- 只有**此刻真有请求在跑**时才拒绝写入（判断方式是去抢同一把会话锁）。
  「上次没压完」不算——那是个可以停留很久的静止状态，见 troubleshooting；
- 带 `base_seq` 乐观锁，取回之后如果又压过一次，写回会被拒绝，不会覆盖新压出来的内容；
- 超过 `summary_total_cap_tokens` 拒绝保存——否则下次会触发二次重压把你的改动洗掉。

除了上面的[可视化页面](#可视化页面)，也可以直接调接口接自己的界面：

```
GET  /admin/session/{id}/summary                  # 当前生效的摘要，含 base_seq / 是否可编辑
GET  /admin/session/{id}/summary?format=text      # 纯文本
PUT  /admin/session/{id}/summary                  # {"summary": "...", "base_seq": N} 新建一档并生效
GET  /admin/session/{id}/checkpoint/{seq}         # 某个存档位的完整正文
PUT  /admin/session/{id}/checkpoint/{seq}         # {"summary": "...", "activate": false} 覆盖存档
POST /admin/session/{id}/checkpoint/{seq}/activate  # {"rewind": false} 设为生效（默认不动进度）
POST /admin/session/{id}/cancel                   # 中止这个会话正在跑的压缩
GET  /admin/tasks                                 # 正在跑的压缩任务
GET  /admin/models                                # 模型配置（key 掩码）
POST /admin/models/test                           # 试一份还没保存的配置，返回完整原始响应
PUT  /admin/models                                # 保存（默认先验证，force=true 跳过）
```

命令行侧：`./ctl.sh tasks` 看任务、`./ctl.sh cancel <id>` 中止、`./ctl.sh models` 看配置。

`/health` 里有几个值得盯的字段：

| 字段 | 含义 |
|---|---|
| `fallback_activations` | 定位兜底触发次数。**长期应当为 0**，非零说明定位逻辑漏了情况，见 troubleshooting |
| `compressing_now` | 此刻真的有请求在压缩的会话数。**这个才是"压缩中"**（控制台里能看到进度并中止） |
| `unfinished_compressions` | 处于 partial（上次没压完，下次请求接着压）的事件数。它是**静止状态**，可以停留很久，不代表有请求在跑；长期不降说明有会话一直没被推进 |
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
cproxy/app.py         路由、鉴权、流式转发、管理接口与控制台接口
cproxy/probe.py       向上游发最小请求探测：认哪些 body 字段、传参方言、伪成功识别
cproxy/ui.py          内嵌的可视化页面（单文件，零外部依赖）
config.yaml           配置（含四套提示词）
ctl.sh                管理脚本
tools/probe_body.py   探测上游认哪些 body 字段（填 extra_body 之前跑一遍）
tests/                单测 + 端到端测试（假上游）
logs/                 日志与 sessions.db
```

跑测试：

```bash
./venv/bin/pip install pytest
./venv/bin/python -m pytest tests/ -q
```
