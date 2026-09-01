"""内嵌的可视化页面（移动优先）。

单文件、零外部依赖（不引 CDN，断网和内网机器都能开），密钥只放在 sessionStorage，
关掉标签页就没了。所有数据都走已有的 /admin/* 接口，页面本身不碰数据库。

布局按手机先做：卡片流而不是宽表格、点击目标 ≥44px、输入框 16px 防 iOS 自动放大、
顶栏 sticky、留出刘海屏安全区。宽屏只是把卡片排成多列。
"""

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>context-proxy</title>
<style>
:root{
  --bg:#f4f5f7; --card:#fff; --fg:#16191d; --muted:#6b7280; --line:#e2e5ea;
  --accent:#2563eb; --warn:#b45309; --danger:#b91c1c; --ok:#15803d;
  --dim:#f0f2f5;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
  --safe-b:env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme:dark){
  :root{--bg:#131518;--card:#1c1f24;--fg:#e7e9ec;--muted:#98a1ab;--line:#2b3037;
        --accent:#60a5fa;--warn:#fbbf24;--danger:#f87171;--ok:#4ade80;--dim:#22262c;}
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:12px 12px calc(24px + var(--safe-b))}
h1{font-size:17px;margin:0}
h2{font-size:14px;margin:0;color:var(--muted);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin-bottom:12px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.spacer{flex:1}
.muted{color:var(--muted)}
.small{font-size:12.5px}
.mono{font-family:var(--mono);font-size:12.5px}
.hide{display:none!important}

button{font:inherit;min-height:42px;padding:9px 14px;border-radius:9px;border:1px solid var(--line);
       background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{color:var(--danger)}
button.ghost{background:transparent;border-color:transparent;color:var(--muted);padding:9px 10px}
button:disabled{opacity:.45}
input,textarea{font-size:16px;font-family:inherit;width:100%;padding:11px 12px;border-radius:9px;
               border:1px solid var(--line);background:var(--bg);color:var(--fg)}
textarea{font-family:var(--mono);font-size:13px;line-height:1.7;resize:vertical}

header{position:sticky;top:0;z-index:5;background:var(--bg);
       padding:10px 12px;border-bottom:1px solid var(--line)}
header .wrapin{max-width:900px;margin:0 auto;display:flex;gap:8px;align-items:center}
nav{display:flex;gap:6px;margin:12px 0}
nav button{flex:1;min-height:40px;padding:8px}
nav button.on{background:var(--accent);color:#fff;border-color:var(--accent)}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(76px,1fr));gap:10px}
.stat b{display:block;font-size:18px;font-weight:650;line-height:1.25}
.stat span{font-size:12px;color:var(--muted)}

.item{display:block;width:100%;text-align:left;padding:13px 14px;margin-bottom:10px;
      background:var(--card);border:1px solid var(--line);border-radius:12px;min-height:44px}
.item .top{display:flex;gap:8px;align-items:baseline}
.item .k{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.item .meta{font-size:12.5px;color:var(--muted);margin-top:5px}

.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11.5px;
      border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.pill.on{color:var(--accent);border-color:var(--accent)}
.pill.warn{color:var(--warn);border-color:var(--warn)}
.pill.bad{color:var(--danger);border-color:var(--danger)}

.msg{padding:10px 12px;border-radius:9px;margin-bottom:12px;font-size:13.5px;white-space:pre-wrap;
     word-break:break-word}
.msg.err{background:#fde8e8;color:#8f1d1d}
.msg.ok{background:#dcf5e3;color:#14532d}
@media (prefers-color-scheme:dark){.msg.err{background:#3a1b1b;color:#fca5a5}.msg.ok{background:#12301e;color:#86efac}}
#msg{position:fixed;left:50%;bottom:calc(12px + var(--safe-b));z-index:20;
     width:min(876px,calc(100% - 24px));transform:translateX(-50%);pointer-events:none}
#msg .msg{margin:0;box-shadow:0 10px 30px rgba(0,0,0,.18)}

.probe-list{display:grid;gap:9px;margin-top:8px}
.probe-card{border:1px solid var(--line);border-radius:10px;background:var(--dim);padding:11px}
.probe-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px}
.probe-grid{display:grid;gap:8px}
.probe-block{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:9px 10px}
.probe-label{display:block;color:var(--muted);font-size:11.5px;margin-bottom:3px}
.probe-value{white-space:pre-wrap;word-break:break-word;margin:0}
.probe-raw{margin-top:8px}
.probe-raw summary,.probe-results>summary{cursor:pointer;color:var(--muted);padding:5px 0}
.probe-raw pre{margin:6px 0 0;padding:9px;background:var(--card);border:1px solid var(--line);
               border-radius:8px;white-space:pre-wrap;word-break:break-all;max-height:240px;overflow:auto}

/* 时间轴 */
.tl{margin:0;padding:0;list-style:none}
.tl li{padding:9px 11px;border-left:3px solid var(--accent);background:var(--card);
       border-radius:0 9px 9px 0;margin-bottom:6px;border-top:1px solid var(--line);
       border-right:1px solid var(--line);border-bottom:1px solid var(--line)}
.tl li.folded{border-left-color:var(--line);background:var(--dim);color:var(--muted)}
.tl .hd{display:flex;gap:8px;align-items:baseline;font-size:12px;color:var(--muted)}
.tl .p{font-size:13px;margin-top:3px;overflow:hidden;text-overflow:ellipsis;
       display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;word-break:break-word}
.divider{display:flex;align-items:center;gap:10px;margin:12px 0;color:var(--accent);font-size:12.5px}
.divider::before,.divider::after{content:"";flex:1;height:1px;background:var(--accent);opacity:.45}

.flow{display:grid;grid-template-columns:1fr auto 1fr;gap:8px;align-items:center;text-align:center}
.flow .box{background:var(--dim);border-radius:9px;padding:10px 8px}
.flow .box b{display:block;font-size:16px}
.flow .box span{font-size:11.5px;color:var(--muted)}
.flow .arrow{color:var(--muted);font-size:18px}

#login{max-width:360px;margin:14vh auto;padding:0 16px}
@media (min-width:640px){ .stats{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))} }
</style>
</head>
<body>

<div id="login">
  <div class="card">
    <h1 style="margin-bottom:4px">context-proxy</h1>
    <p class="muted small" style="margin:0 0 14px">输入 ui_token 登录</p>
    <input id="key" type="password" autocomplete="current-password" inputmode="text" placeholder="16 位密钥">
    <div id="loginmsg" style="margin-top:10px"></div>
    <button class="primary" style="width:100%;margin-top:10px" onclick="login()">进入</button>
  </div>
</div>

<div id="app" class="hide">
  <header><div class="wrapin">
    <h1>context-proxy</h1><span class="spacer"></span>
    <button class="ghost" onclick="refresh()" aria-label="刷新">刷新</button>
    <button class="ghost" onclick="logout()" aria-label="退出">退出</button>
  </div></header>

  <div class="wrap">
    <nav>
      <button id="tab_s" class="on" onclick="tab('s')">会话</button>
      <button id="tab_p" onclick="tab('p')">提示词</button>
      <button id="tab_c" onclick="tab('c')">控制台</button>
    </nav>
    <div id="msg"></div>

    <!-- 会话列表 -->
    <div id="pane_s">
      <div class="card"><div id="health" class="stats"></div></div>
      <div id="rows"></div>
      <p id="empty" class="muted small hide">暂无会话。对话首次超过 trigger_tokens 并触发压缩后才会建立记录。</p>
    </div>

    <!-- 会话详情 -->
    <div id="pane_d" class="hide">
      <button class="ghost" style="padding-left:0" onclick="back()">← 返回会话列表</button>
      <div class="card">
        <div class="mono muted" id="d_id" style="margin-bottom:10px;word-break:break-all"></div>
        <div id="d_stats" class="stats"></div>
      </div>

      <div class="card">
        <h2 style="margin-bottom:4px">存档位</h2>
        <p class="muted small" style="margin:0 0 10px">
          每条 checkpoint 对应一个存档。选择存档后会载入下方编辑区；
          <b>「设为生效」只换摘要内容，压缩进度不会倒退</b>——压到哪条是进度，
          摘要写了什么是内容，两件事分开。
        </p>
        <div id="slots"></div>
      </div>

      <div class="card">
        <div class="row" style="margin-bottom:8px">
          <h2 id="ed_title">编辑区</h2><span class="spacer"></span>
          <span class="muted small" id="d_tok"></span>
        </div>
        <div id="ed_gap"></div>
        <textarea id="sum" rows="14" spellcheck="false" oninput="on_edit()"></textarea>
        <p class="muted small" style="margin:9px 0 6px">保存目标（<b>必须选择一项</b>）：</p>
        <div id="targets" class="row" style="gap:6px"></div>
        <div class="row" style="margin-top:12px">
          <button onclick="reload_summary()">放弃修改</button>
          <span class="spacer"></span>
          <button class="primary" id="savebtn" onclick="save()" disabled>保存</button>
        </div>
        <p class="muted small" style="margin:9px 0 0" id="sumhint"></p>
      </div>

      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <h2>本次请求：进来 → 发出去</h2><span class="spacer"></span>
          <button class="ghost small" onclick="load_timeline()">加载</button>
        </div>
        <div id="tlbox"><p class="muted small" style="margin:0">选择「加载」可查看最近一次请求的输入、输出与时间轴。</p></div>
      </div>

      <div class="card">
        <button class="danger" style="width:100%" onclick="wipe()">清除这个会话的压缩状态</button>
        <p class="muted small" style="margin:8px 0 0">
          清除后，下次对话会从第 0 条开始全量重新压缩，并产生较高调用成本。仅在压缩状态异常时使用。
        </p>
      </div>
    </div>

    <!-- 提示词 -->
    <div id="pane_p" class="hide"><div id="prompts"></div></div>

    <!-- 控制台 -->
    <div id="pane_c" class="hide">
      <div class="card">
        <div class="row" style="margin-bottom:8px">
          <h2>正在执行的压缩任务</h2><span class="spacer"></span>
          <button class="ghost small" onclick="load_tasks()">刷新</button>
        </div>
        <div id="tasks"><p class="muted small" style="margin:0">加载中…</p></div>
      </div>
      <div id="models"></div>
    </div>
  </div>
</div>

<script>
let KEY = sessionStorage.getItem("cproxy_key") || "";
let CUR = null, TL = null, TL_SHOWN = 0;
// 存档位状态：D = 会话详情，LIVE = 当前生效档，SLOT = 编辑区里载入的是哪一档，
// TARGET = 保存目标（null / {kind:"slot",seq} / {kind:"new"}）。没选 TARGET 就不让存。
let D = null, LIVE = null, SLOT = null, TARGET = null, DIRTY = false;
const PAGE_ROUNDS = 60;      // 时间轴一次渲染多少轮，几千轮的会话不能一次全画

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = n => (n == null ? "-" : String(n));

function note(text, kind, box) {
  const el = $(box || "msg");
  el.innerHTML = text ? `<div class="msg ${kind || "err"}">${esc(text)}</div>` : "";
}

async function api(path, opts) {
  const o = Object.assign({headers: {}}, opts || {});
  o.headers["Authorization"] = "Bearer " + KEY;
  if (o.body) o.headers["Content-Type"] = "application/json";
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (r.status === 401) { logout(); throw new Error("密钥无效或已失效"); }
  if (!r.ok) {
    // 校验失败时后端会把每个端点的原始响应放在 checks 里，调用方要拿它渲染
    const err = new Error((data && data.error && data.error.message) || ("HTTP " + r.status));
    err.payload = data;
    throw err;
  }
  return data;
}

async function login() {
  KEY = $("key").value.trim();
  if (!KEY) return;
  note("", "", "loginmsg");
  try {
    await api("/admin/sessions?limit=1");
    sessionStorage.setItem("cproxy_key", KEY);
    $("login").classList.add("hide"); $("app").classList.remove("hide");
    refresh();
  } catch (e) { note(e.message, "err", "loginmsg"); }
}

function logout() {
  sessionStorage.removeItem("cproxy_key"); KEY = ""; CUR = null;
  $("app").classList.add("hide"); $("login").classList.remove("hide"); $("key").value = "";
}

function tab(t) {
  for (const k of ["s", "p", "c"]) {
    $("tab_" + k).classList.toggle("on", t === k);
    if (k !== "s") $("pane_" + k).classList.toggle("hide", t !== k);
  }
  const inSession = t === "s";
  $("pane_s").classList.toggle("hide", !inSession || !!CUR);
  $("pane_d").classList.toggle("hide", !inSession || !CUR);
  note("");
  if (t === "p") load_prompts();
  if (t === "c") { load_tasks(); load_models(); }
}

function age(sec) {
  if (sec == null) return "-";
  if (sec < 60) return sec + " 秒前";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
  return Math.floor(sec / 86400) + " 天前";
}
const stat = (v, label, color) =>
  `<div class="stat"><b${color ? ` style="color:${color}"` : ""}>${esc(v)}</b><span>${esc(label)}</span></div>`;

async function refresh() {
  note("");
  try {
    const h = await (await fetch("/health")).json();
    const fb = h.fallback_activations || 0;
    const busy = h.compressing_now || 0, todo = h.unfinished_compressions || 0;
    $("health").innerHTML =
      stat(h.conversations, "会话") + stat(h.checkpoints, "checkpoint") +
      stat(busy, "正在压缩", busy ? "var(--warn)" : null) +
      stat(todo, "未完成", todo ? "var(--warn)" : null) +
      stat(fb, "定位兜底", fb ? "var(--danger)" : null) +
      stat(h.trigger_tokens, "触发阈值") +
      stat(h.keep_recent_tokens_effective, "近期原文下限");
    if (CUR) { await open_session(CUR); return; }
    const d = await api("/admin/sessions");
    $("rows").innerHTML = d.sessions.map(s => `
      <button class="item" onclick="open_session('${esc(s.conv_id)}')">
        <div class="top"><span class="k">${esc(s.conv_id_short)}</span><span class="spacer"></span>
          <span class="pill ${s.status === "partial" ? "warn" : ""}">${esc(s.status || "-")}</span></div>
        <div class="meta">已压到第 ${num(s.round_upto)} 轮 · 摘要 ${num(s.summary_chars)} 字 ·
          ${num(s.checkpoints)} 个 checkpoint${s.fallback_count ? " · 兜底 " + s.fallback_count + " 次" : ""}</div>
        <div class="meta">${esc(age(s.age_seconds))}</div>
      </button>`).join("");
    $("empty").classList.toggle("hide", d.sessions.length > 0);
  } catch (e) { note(e.message); }
}

async function open_session(cid) {
  try {
    const d = await api("/admin/session/" + cid);
    const s = await api("/admin/session/" + cid + "/summary");
    CUR = cid; D = d; LIVE = s; TL = null; TL_SHOWN = 0;
    $("pane_s").classList.add("hide"); $("pane_d").classList.remove("hide");
    $("tlbox").innerHTML = '<p class="muted small" style="margin:0">选择「加载」可查看最近一次请求的输入、输出与时间轴。</p>';
    $("d_id").textContent = s.conv_id;
    $("d_stats").innerHTML =
      stat(s.round_upto + " / " + s.total_rounds, "已压缩轮次") +
      stat(s.compressed_upto, "压缩到的下标") +
      stat(d.checkpoints.length, "存档位") +
      stat(s.base_seq, "生效档 seq") +
      (s.busy ? stat("压缩中", "暂不可保存", "var(--warn)") : "") +
      (s.unfinished_event_seq != null
        ? stat("seq " + s.unfinished_event_seq, "上次未完成", "var(--warn)") : "");
    render_slots();
    load_slot(s.base_seq, true);
  } catch (e) { note(e.message); }
}

function live_seq() { return LIVE ? LIVE.base_seq : null; }

function render_slots() {
  const cks = [...D.checkpoints].sort((a, b) => b.seq - a.seq);
  $("slots").innerHTML = cks.map(c => {
    const isLive = c.seq === live_seq(), isLoaded = c.seq === SLOT;
    const gap = isLive ? 0 : Math.max(0, (LIVE.round_upto || 0) - (c.round_upto || 0));
    return `
      <div class="item" style="cursor:default;${isLoaded ? "border-color:var(--accent)" : ""}">
        <div class="top"><b>存档 ${c.seq}</b>
          <span class="pill ${c.kind === "manual" ? "on" : c.kind === "fallback" ? "bad" : ""}">${esc(c.kind)}</span>
          ${isLive ? '<span class="pill on">生效中</span>' : ""}
          ${c.pinned === 2 ? '<span class="pill on">手工固定</span>' : c.pinned ? '<span class="pill">置顶</span>' : ""}
          ${c.status === "partial" ? '<span class="pill warn">未完成</span>' : ""}
          ${c.status === "stale" ? '<span class="pill">已作废</span>' : ""}</div>
        <div class="meta">摘要覆盖到第 ${c.round_upto} 轮 · ${c.summary_tokens} tokens ·
          ${new Date(c.updated_at * 1000).toLocaleString()}</div>
        ${gap ? `<div class="meta" style="color:var(--warn)">设为生效会留下 ${gap} 轮记忆空洞
          （第 ${c.round_upto + 1}~${LIVE.round_upto} 轮既不在这份摘要里、也不在近期原文里）</div>` : ""}
        <div class="row" style="margin-top:8px;gap:6px">
          <button class="ghost small" onclick="load_slot(${c.seq})">${isLoaded ? "已载入" : "载入编辑"}</button>
          ${isLive || !c.summary_tokens ? "" :
            `<button class="ghost small" onclick="activate(${c.seq}, ${gap})">设为生效</button>`}
        </div>
      </div>`;
  }).join("");
}

async function load_slot(seq, silent) {
  if (DIRTY && !silent && !confirm("编辑区包含未保存的修改。载入其他存档将丢弃这些修改，是否继续？")) return;
  try {
    const c = await api(`/admin/session/${CUR}/checkpoint/${seq}`);
    SLOT = seq; TARGET = null; DIRTY = false;
    $("sum").value = c.summary;
    $("ed_title").textContent = `编辑区 · 存档 ${seq}` + (seq === live_seq() ? "（生效中）" : "");
    $("d_tok").textContent = `${c.summary_tokens} / ${LIVE.summary_cap_tokens} tokens`;
    const gap = Math.max(0, (LIVE.round_upto || 0) - (c.round_upto || 0));
    $("ed_gap").innerHTML = gap
      ? `<div class="msg err" style="margin-bottom:10px">这份摘要只覆盖到第 ${c.round_upto} 轮，
         当前压缩标记在第 ${LIVE.round_upto} 轮。存成生效档的话，中间 ${gap} 轮
         既不在摘要里、也不在近期原文里——需要的话把这段内容补进正文再存。</div>`
      : "";
    render_slots(); render_targets();
  } catch (e) { note(e.message); }
}

function on_edit() { DIRTY = true; render_targets(); }

function render_targets() {
  const live = live_seq();
  const opts = [{k: "new", label: "新建一档并设为生效"}];
  if (SLOT != null) {
    const c = D.checkpoints.find(x => x.seq === SLOT);
    if (c && c.status !== "partial") opts.unshift({k: "slot", label: `覆盖存档 ${SLOT}`});
  }
  $("targets").innerHTML = opts.map(o => {
    const on = TARGET && TARGET.kind === o.k;
    return `<button class="${on ? "primary" : ""}" onclick="pick_target('${o.k}')">${esc(o.label)}</button>`;
  }).join("");
  const busy = LIVE && LIVE.busy;
  $("savebtn").disabled = !TARGET || busy;
  $("sumhint").textContent = busy
    ? "该会话当前有请求正在执行，请在请求结束后修改。"
    : (!TARGET ? "请选择保存目标。完成选择后，保存按钮将变为可用状态。"
       : TARGET.kind === "new"
         ? "会新建一条存档并设为生效；压缩进度不变，后续压缩在这份内容之后追加。"
         : `仅将正文写入存档 ${SLOT}，不更改当前生效存档`
           + (SLOT === live_seq() ? "（该存档当前已生效，因此修改会立即生效）。" : "。"));
}

function pick_target(k) { TARGET = {kind: k}; render_targets(); }

function back() {
  CUR = null; TL = null; D = null; LIVE = null; SLOT = null; TARGET = null; DIRTY = false;
  $("pane_d").classList.add("hide"); $("pane_s").classList.remove("hide"); refresh();
}
async function reload_summary() {
  if (!CUR) return;
  const keep = SLOT; DIRTY = false;
  await open_session(CUR);
  if (keep != null && D.checkpoints.some(c => c.seq === keep)) await load_slot(keep, true);
  note("已放弃未保存的修改", "ok");
}

async function save() {
  if (!TARGET) return;
  const text = $("sum").value;
  try {
    let msg;
    if (TARGET.kind === "new") {
      const r = await api("/admin/session/" + CUR + "/summary", {
        method: "PUT",
        body: JSON.stringify({summary: text, base_seq: live_seq()})
      });
      msg = `已新建存档 ${r.new_seq} 并设为生效（${r.previous_summary_tokens} → ${r.summary_tokens} tokens），下一次请求生效。`;
    } else {
      const r = await api(`/admin/session/${CUR}/checkpoint/${SLOT}`, {
        method: "PUT", body: JSON.stringify({summary: text})
      });
      msg = `已写入存档 ${SLOT}（${r.summary_tokens} tokens）。${r.note}`;
    }
    DIRTY = false; TARGET = null;
    await reopen(); note(msg, "ok");
  } catch (e) { note(e.message); }
}

async function reopen() {
  const keep = SLOT;
  await refresh();
  if (CUR && keep != null && D && D.checkpoints.some(c => c.seq === keep)) await load_slot(keep, true);
}

async function activate(seq, gap) {
  let ask = `把存档 ${seq} 的摘要设为生效？\\n\\n`
    + "压缩进度不变，只把发给模型、以及后续压缩所基于的摘要换成这一份。\\n"
    + "它会被固定住，重启也不会变回去；其它存档都还留着，可以再选回来。";
  if (gap) ask += `\\n\\n⚠️ 这份摘要只覆盖到更早的轮次，中间 ${gap} 轮会成为记忆空洞`
    + "（既不在摘要里，也不在近期原文里）。确定要这样？";
  if (!confirm(ask)) return;
  try {
    const r = await api(`/admin/session/${CUR}/checkpoint/${seq}/activate`,
                        {method: "POST", body: JSON.stringify({rewind: false})});
    DIRTY = false;
    await refresh(); await load_slot(r.new_seq, true);
    note(`存档 ${r.from_seq} 已设为生效（新存档 ${r.new_seq}）。`
         + (r.gap_rounds ? ` 注意有 ${r.gap_rounds} 轮记忆空洞，建议把这段补进摘要。` : ""), "ok");
  } catch (e) { note(e.message); }
}

async function wipe() {
  if (!confirm("确认清除该会话的全部压缩状态？\\n下次对话将从第 0 条开始全量重新压缩，并产生较高调用成本。")) return;
  try {
    await api("/admin/clean", {method: "POST", body: JSON.stringify({target: CUR})});
    back(); note("已清除", "ok");
  } catch (e) { note(e.message); }
}

/* ---- 时间轴 ---- */
async function load_timeline() {
  try {
    TL = await api("/admin/session/" + CUR + "/timeline");
    TL_SHOWN = 0; render_timeline();
  } catch (e) { $("tlbox").innerHTML = `<div class="msg err">${esc(e.message)}</div>`; }
}

function render_timeline() {
  const t = TL, all = t.rounds, folded = all.filter(r => r.c).length;
  if (!TL_SHOWN) TL_SHOWN = Math.min(all.length, PAGE_ROUNDS);
  const shown = all.slice(all.length - TL_SHOWN);
  let html = `
    <div class="flow">
      <div class="box"><b>${num(t.in.messages)}</b><span>条进来</span>
        <b style="font-size:13px;margin-top:2px">${num(t.in.tokens)}</b><span>tokens</span></div>
      <div class="arrow">→</div>
      <div class="box"><b>${num(t.out.messages)}</b><span>条发出去</span>
        <b style="font-size:13px;margin-top:2px">${num(t.out.tokens)}</b><span>tokens</span></div>
    </div>
    <p class="muted small" style="margin:10px 0 0">
      ${esc(new Date(t.at * 1000).toLocaleString())} · provider ${esc(t.provider)} · mode ${esc(t.mode)}
      · 折叠 ${folded}/${all.length} 轮 · 摘要 ${num(t.summary_tokens)} tokens
      ${t.in.tokens && t.out.tokens ? "· 省了 " + Math.max(0, Math.round((1 - t.out.tokens / t.in.tokens) * 100)) + "%" : ""}
    </p>`;
  if (TL_SHOWN < all.length)
    html += `<button style="width:100%;margin:12px 0 4px" onclick="more_timeline()">显示更早的 ${
      Math.min(PAGE_ROUNDS, all.length - TL_SHOWN)} 轮（还有 ${all.length - TL_SHOWN} 轮）</button>`;
  html += "<ul class='tl' style='margin-top:12px'>";
  let cut = false;
  for (const r of shown) {
    if (!r.c && !cut) { cut = true; if (r !== shown[0]) html += `</ul><div class="divider">以上已折叠成摘要，以下逐字发给上游</div><ul class='tl'>`; }
    html += `<li class="${r.c ? "folded" : ""}">
      <div class="hd"><b>第 ${r.r} 轮</b><span>下标 ${r.s}~${r.e}</span><span class="spacer"></span>
        <span>${r.t} tokens</span>
        <span class="pill ${r.c ? "" : "on"}">${r.c ? "已折叠" : "原文"}</span></div>
      <div class="p">${esc(r.p) || '<span class="muted">（无文本）</span>'}</div></li>`;
  }
  html += "</ul>";
  $("tlbox").innerHTML = html;
}

function more_timeline() { TL_SHOWN = Math.min(TL.rounds.length, TL_SHOWN + PAGE_ROUNDS); render_timeline(); }

/* ---- 提示词 ---- */
async function load_prompts() {
  try {
    const d = await api("/admin/prompts");
    $("prompts").innerHTML =
      `<div class="card"><p class="muted small" style="margin:0">
         保存会<b>直接写入配置文件</b>（仅替换提示词正文，并保留其他配置与注释）。
         写入前会生成备份文件，保存后立即热重载，重启后仍然生效。
       </p></div>` +
      d.prompts.map((p, i) => `
      <div class="card">
        <div class="row" style="margin-bottom:8px">
          <h2>${esc(p.label)}</h2><span class="spacer"></span>
          ${p.overridden ? '<span class="pill warn">仅内存</span>' : '<span class="pill">配置文件</span>'}
        </div>
        <div class="mono muted small" style="margin-bottom:6px">${esc(p.name)}${
          p.requires && p.requires.length ? " · 必须包含 " + esc(p.requires[0]) : ""}</div>
        <textarea id="pr_${i}" rows="10" spellcheck="false">${esc(p.effective)}</textarea>
        <div class="row" style="margin-top:10px">
          <button onclick="reset_prompt(${i})">恢复默认</button>
          <span class="spacer"></span>
          <button class="primary" onclick="save_prompt(${i})">保存到配置文件</button>
        </div>
      </div>`).join("");
    window._prompts = d.prompts;
  } catch (e) { note(e.message); }
}

async function put_prompts(body, okmsg) {
  try {
    const r = await api("/admin/prompts", {method: "PUT", body: JSON.stringify({prompts: body})});
    await load_prompts();
    note(r.warning ? r.warning : okmsg, r.warning ? "err" : "ok");
  } catch (e) { note(e.message); }
}
function save_prompt(i) {
  const p = window._prompts[i];
  put_prompts({[p.name]: $("pr_" + i).value},
              `「${p.label}」已写入配置文件，将在下一次压缩时生效。`);
}
function reset_prompt(i) {
  const p = window._prompts[i];
  if (!confirm(`确认将「${p.label}」恢复为内置默认值并写入配置文件？`)) return;
  put_prompts({[p.name]: ""}, `「${p.label}」已恢复为默认值并写入配置文件。`);
}

/* ---- 控制台：压缩任务 ---- */
async function load_tasks() {
  try {
    const d = await api("/admin/tasks");
    const rows = d.running.map(t => `
      <div class="item" style="cursor:default">
        <div class="top"><b class="mono">${esc((t.conv_id || "定位中").slice(0, 12))}</b>
          <span class="pill warn">${esc(t.phase)}</span>
          ${t.cancelled ? '<span class="pill bad">中止中</span>' : ""}</div>
        <div class="meta">第 ${t.batch}/${t.batches} 批 · 已压到第 ${t.rounds_done}/${t.rounds_total} 轮
          · 累积摘要 ${num(t.summary_tokens)} tokens · 已运行 ${t.elapsed_seconds}s
          ${t.models && t.models.length ? " · " + esc(t.models.join("+")) : ""}</div>
        ${t.last_summary ? `<div class="meta" style="white-space:pre-wrap;margin-top:6px;
          max-height:120px;overflow:auto">最近一批的输出：\\n${esc(t.last_summary)}</div>` : ""}
        ${t.cancelled
          ? '<p class="muted small" style="margin:8px 0 0">已提交中止请求，将在当前批次完成后停止。</p>'
          : t.conv_id ? `<button class="danger" style="width:100%;margin-top:8px"
              onclick="cancel_task('${esc(t.conv_id)}')">中止这个任务</button>` : ""}
      </div>`).join("");
    $("tasks").innerHTML = (rows || '<p class="muted small" style="margin:0">当前没有正在执行的压缩任务。</p>')
      + `<p class="muted small" style="margin:8px 0 0">另有 ${d.unfinished_compressions} 个会话
         处于「上次未完成」状态。该状态不会占用运行资源，下次请求会从断点继续。</p>`;
  } catch (e) { note(e.message); }
}

async function cancel_task(cid) {
  if (!confirm("确认中止本次压缩？\\n\\n任务将在当前批次完成后停止。已完成批次会保留，"
      + "下次请求将从断点继续；当前请求预计会返回「压缩未完成」提示。")) return;
  try {
    await api(`/admin/session/${cid}/cancel`, {method: "POST"});
    note("已提交中止请求，将在当前批次完成后停止", "ok");
    setTimeout(load_tasks, 1200);
  } catch (e) { note(e.message); }
}

/* ---- 控制台：模型与供应商 ---- */
let MODELS = null, LAST_CHECKS = null;

async function load_models() {
  try { MODELS = await api("/admin/models"); render_models(); }
  catch (e) { note(e.message); }
}

const jstr = o => JSON.stringify(o || {}, null, 2);

function kv(label, id, val, ph, type) {
  return `<label class="small muted" style="display:block;margin-top:8px">${esc(label)}
    <input id="${id}" type="${type || "text"}" value="${esc(val ?? "")}"
           placeholder="${esc(ph || "")}" style="margin-top:4px"></label>`;
}

function json_kv(label, id, val, ph) {
  return `<label class="small muted" style="display:block;margin-top:8px">${esc(label)}
    <textarea id="${id}" rows="3" spellcheck="false" placeholder="${esc(ph || "")}"
              style="margin-top:4px">${esc(jstr(val))}</textarea></label>`;
}

function token_field(prefix, current) {
  return `<label class="small muted" style="display:block;margin-top:8px">token 上限字段
    <select id="${prefix}_field" style="width:100%;margin-top:4px;min-height:42px;font-size:16px;
            padding:10px;border-radius:9px;border:1px solid var(--line);
            background:var(--bg);color:var(--fg)">
      ${MODELS.max_tokens_fields.map(x =>
        `<option value="${x}" ${x === current ? "selected" : ""}>${x}</option>`).join("")}
    </select></label>`;
}

function summary_model_card(which, data) {
  const prefix = which === "summary" ? "s" : "f";
  const title = which === "summary" ? "摘要模型（主）" : "摘要模型（备用）";
  const enabled = which === "fallback"
    ? `<label class="small muted"><input type="checkbox" id="f_on" ${data.enabled ? "checked" : ""}
         style="width:auto;margin-right:6px">启用</label>` : "";
  return `<div class="card">
    <div class="row"><h2>${title}</h2><span class="spacer"></span>${enabled}</div>
    ${kv("base_url", prefix + "_url", data.base_url, "https://your-gateway/v1")}
    ${kv("模型名", prefix + "_model", data.model, "摘要模型名称")}
    ${kv("api_key（留空表示保持不变）", prefix + "_key", "",
         data.key_from_env ? "由环境变量提供：" + data.api_key_masked : data.api_key_masked || "未配置", "password")}
    ${kv("单次输出 token 上限", prefix + "_maxtok", data.summary_max_tokens, "2048", "number")}
    ${kv("请求超时（秒）", prefix + "_timeout", data.timeout_seconds, "180", "number")}
    ${kv("最大尝试次数（含首次调用）", prefix + "_att", data.max_attempts, "2", "number")}
    ${kv("最小有效输出 token 数", prefix + "_mintok", data.min_output_tokens, "50", "number")}
    ${json_kv("extra_body（JSON）", prefix + "_extra", data.extra_body, '{"temperature": 0.5}')}
    ${token_field(prefix, data.max_tokens_field)}
    <p class="muted small" style="margin:6px 0 0">
      不同上游可能使用 max_tokens 或 max_completion_tokens。请通过「检测传参方言」确认，
      不要根据 URL 推断。
    </p>
    <div class="row" style="margin-top:10px">
      <button class="ghost small" onclick="test_one('${which}', 0, false)">测试连通性</button>
      <button class="ghost small" onclick="test_one('${which}', 0, true)">检测传参方言</button>
    </div>
    <div id="${prefix}_res"></div>
  </div>`;
}

function render_models() {
  const m = MODELS;
  const prov = m.providers.map((p, i) => `
    <div class="card" style="background:var(--dim)">
      <div class="row"><h2>供应商 ${esc(p.name)}</h2><span class="spacer"></span>
        <button class="ghost small danger" onclick="del_provider(${i})">删除</button></div>
      ${kv("名称（决定 URL：/<name>/v1）", "p_name_" + i, p.name)}
      ${kv("base_url", "p_url_" + i, p.base_url, "https://your-gateway/v1")}
      ${kv("api_key（留空表示保持不变）", "p_key_" + i, "",
           p.key_from_env ? "由环境变量提供：" + p.api_key_masked : p.api_key_masked || "未配置", "password")}
      ${json_kv("extra_body（JSON）", "p_extra_" + i, p.extra_body, "{}")}
      ${kv("测试模型名（保存前会发送真实请求）", "p_tm_" + i, "", "例如 gpt-4o-mini")}
      <label class="small muted" style="display:block;margin-top:8px">
        <input type="checkbox" id="p_mm_${i}" ${p.multimodal ? "checked" : ""}
               style="width:auto;margin-right:6px">支持图片（纯文本模型必须取消勾选）</label>
      <div class="row" style="margin-top:10px">
        <button class="ghost small" onclick="test_one('provider', ${i}, false)">测试连通性</button>
        <button class="ghost small" onclick="test_one('provider', ${i}, true)">检测传参方言</button>
      </div>
      <div id="p_res_${i}"></div>
    </div>`).join("");

  $("models").innerHTML = `
    <div class="card">
      <div class="row" style="margin-bottom:8px"><h2>上游供应商</h2><span class="spacer"></span>
        <button class="ghost small" onclick="add_provider()">+ 新增</button></div>
      ${prov}
    </div>

    ${summary_model_card("summary", m.summary)}
    ${summary_model_card("fallback", m.fallback)}

    <div class="card">
      <p class="muted small" style="margin:0 0 10px">
        保存前会使用当前表单中的 extra_body 和其他参数发送一次真实请求。验证失败时不会修改配置文件。
        每个端点的验证请求约使用十几 token，并按上游标准计费。
        保存成功后会写入配置文件并立即热重载，同时生成原配置的备份文件。
      </p>
      <button class="primary" style="width:100%" onclick="save_models(false)">验证并保存</button>
      <button style="width:100%;margin-top:8px" onclick="save_models(true)">跳过验证强制保存</button>
      <div id="m_res"></div>
    </div>`;
  // 重绘会替换 #m_res，因此需要恢复最近一次验证结果。
  if (LAST_CHECKS) render_checks(LAST_CHECKS);
}

function render_checks(checks) {
  LAST_CHECKS = checks;
  $("m_res").innerHTML = checks.map(c => `
    <div class="msg ${c.verdict === "ok" ? "ok" : "err"}" style="margin-top:8px">${
      esc(c.label)}：HTTP ${c.status}${c.suspect ? " ⚠️ " + esc(c.suspect) : ""}${
      c.verdict === "ok" ? "" : "\\n" + esc(c.advice || "")}</div>
    ${probe_results([c])}`).join("");
}

function add_provider() {
  MODELS.providers.push({name: "", base_url: "", api_key_masked: "", has_key: false,
                         multimodal: true, extra_body: {}, timeout_seconds: 300,
                         connect_timeout_seconds: 30, forward_headers: [], forward_query: false});
  render_models();
}

function del_provider(i) {
  if (MODELS.providers.length <= 1) return note("至少需要保留一个供应商");
  if (!confirm(`确认删除供应商 ${MODELS.providers[i].name}？保存后 /${MODELS.providers[i].name}/v1 将停止提供服务。`)) return;
  MODELS.providers.splice(i, 1); render_models();
}

function parse_json(id, label) {
  const raw = ($(id).value || "").trim();
  if (!raw) return {};
  try { const o = JSON.parse(raw); if (o && typeof o === "object" && !Array.isArray(o)) return o; }
  catch (e) { /* 落到下面统一报错 */ }
  throw new Error(`${label} 不是合法的 JSON 对象`);
}

function collect() {
  const M = MODELS, mask = M.masked_placeholder;
  const providers = M.providers.map((p, i) => ({
    ...p,
    name: $("p_name_" + i).value.trim(),
    base_url: $("p_url_" + i).value.trim(),
    api_key: $("p_key_" + i).value.trim() || mask,
    multimodal: $("p_mm_" + i).checked,
    extra_body: parse_json("p_extra_" + i, `供应商 ${$("p_name_" + i).value} 的 extra_body`),
    test_model: $("p_tm_" + i).value.trim(),
  }));
  const summaryModel = (prefix, base, enabled) => ({
    ...base,
    ...(enabled == null ? {} : {enabled}),
    base_url: $(prefix + "_url").value.trim(),
    model: $(prefix + "_model").value.trim(),
    api_key: $(prefix + "_key").value.trim() || mask,
    summary_max_tokens: Number($(prefix + "_maxtok").value) || 2048,
    timeout_seconds: Number($(prefix + "_timeout").value) || 180,
    max_attempts: Number($(prefix + "_att").value) || (prefix === "s" ? 2 : 3),
    min_output_tokens: Number($(prefix + "_mintok").value) || 50,
    max_tokens_field: $(prefix + "_field").value,
    extra_body: parse_json(prefix + "_extra", `${prefix === "s" ? "主" : "备用"}摘要模型的 extra_body`),
  });
  return {
    providers,
    summary: summaryModel("s", M.summary, null),
    fallback: summaryModel("f", M.fallback, $("f_on").checked),
  };
}

function pretty_raw(raw) {
  if (!raw) return "未收到 HTTP 响应";
  try { return JSON.stringify(JSON.parse(raw), null, 2); } catch (e) { return raw; }
}

function probe_card(r) {
  const output = r.content || (r.reasoning ? r.reasoning : "未提取到模型输出");
  const reasoningCount = r.reasoning_chars
    ? `${r.reasoning_chars} 字`
    : (r.reasoning_tokens ? `${r.reasoning_tokens} tokens` : "");
  const reasoningLabel = r.has_reasoning
    ? `检测到思考${reasoningCount ? "：" + reasoningCount : ""}`
    : "未检测到思考";
  return `<div class="probe-card">
    <div class="probe-head">
      <b>${esc(r.name || "test")}</b>
      <span class="pill ${r.ok ? "on" : "bad"}">HTTP ${r.status}</span>
      ${r.retried ? '<span class="pill warn">已重试</span>' : ""}
      <span class="pill ${r.has_reasoning ? "warn" : "on"}">${esc(reasoningLabel)}</span>
      ${r.elapsed_ms != null ? `<span class="muted small">${esc(r.elapsed_ms)} ms</span>` : ""}
      ${r.suspect ? `<span class="pill warn">${esc(r.suspect)}</span>` : ""}
    </div>
    <div class="probe-grid">
      <div class="probe-block"><span class="probe-label">发送字段</span>
        <pre class="probe-value mono small">${esc(jstr(r.request_body || {}))}</pre></div>
      <div class="probe-block"><span class="probe-label">输入</span>
        <p class="probe-value">${esc(r.input || "未记录输入")}</p></div>
      <div class="probe-block"><span class="probe-label">输出</span>
        <p class="probe-value">${esc(output)}</p></div>
      ${r.note ? `<div class="probe-block"><span class="probe-label">错误信息</span>
        <p class="probe-value">${esc(r.note)}</p></div>` : ""}
    </div>
    <details class="probe-raw">
      <summary class="small">查看原始 Payload</summary>
      <span class="probe-label">请求 Payload</span>
      <pre class="mono small">${esc(jstr(r.request_payload || r.request_body || {}))}</pre>
      <span class="probe-label" style="margin-top:8px">响应 Payload</span>
      <pre class="mono small">${esc(pretty_raw(r.raw))}</pre>
    </details>
  </div>`;
}

function probe_results(results) {
  return `<details class="probe-results">
    <summary class="small">查看测试明细（${results.length} 项）</summary>
    <div class="probe-list">${results.map(probe_card).join("")}</div>
  </details>`;
}

function render_probe(box, d) {
  const results = d.mode === "dialect" ? d.results : [d];
  const baselineOk = d.mode === "dialect" && results.length && results[0].ok;
  const cls = d.verdict === "ok" || baselineOk ? "ok" : "err";
  const head = d.mode === "dialect"
    ? `传参方言：${esc(d.dialect || "?")}｜token 字段：${esc(d.max_tokens_field || "?")}`
    : `HTTP ${d.status}｜${d.verdict === "ok" ? "正常" : d.verdict === "suspect" ? "可疑" : "失败"}`;
  const notes = (d.notes || []).map(n => esc(n)).join("\\n");
  $(box).innerHTML = `
    <div class="msg ${cls}" style="margin-top:10px">${esc(head)}${notes ? "\\n" + notes : ""}${
      d.advice ? "\\n" + esc(d.advice) : ""}${d.applied_note ? "\\n" + esc(d.applied_note) : ""}</div>
    ${probe_results(results)}`;
}

function apply_dialect(which, i, d, currentExtra) {
  const prefix = which === "summary" ? "s" : "f";
  const extraId = which === "provider" ? "p_extra_" + i : prefix + "_extra";
  const patch = d.suggested_extra_body || d.thinking_off || null;
  const changes = [];
  if (patch && Object.keys(patch).length) {
    const merged = {...currentExtra, ...patch};
    $(extraId).value = jstr(merged);
    if (which === "provider") MODELS.providers[i].extra_body = merged;
    else MODELS[which].extra_body = merged;
    changes.push("已验证参数已写入 extra_body");
  }
  if (which !== "provider" && d.max_tokens_field) {
    $(prefix + "_field").value = d.max_tokens_field;
    MODELS[which].max_tokens_field = d.max_tokens_field;
    changes.push(`token 上限字段已设为 ${d.max_tokens_field}`);
  }
  d.applied_note = changes.length
    ? changes.join("；") + "。保存前验证将使用当前表单中的 extra_body。"
    : "未检测到可自动写入的参数，当前 extra_body 保持不变。";
}

async function test_one(which, i, dialect) {
  const box = which === "provider" ? "p_res_" + i : (which === "summary" ? "s_res" : "f_res");
  try {
    const c = collect();
    const t = which === "provider" ? c.providers[i] : c[which];
    const model = which === "provider" ? t.test_model : t.model;
    if (!model) return note(which === "provider"
      ? "请填写测试模型名。供应商配置不包含默认模型，实际模型名由客户端请求提供。"
      : "请填写模型名");
    $(box).innerHTML = '<p class="muted small">正在发送测试请求…</p>';
    const d = await api("/admin/models/test", {method: "POST", body: JSON.stringify({
      target: which === "provider" ? "provider:" + t.name : which,
      base_url: t.base_url, api_key: t.api_key, model,
      extra_body: t.extra_body, max_tokens_field: t.max_tokens_field,
      timeout_seconds: t.timeout_seconds,
      detect_dialect: !!dialect})});
    if (dialect) apply_dialect(which, i, d, t.extra_body);
    render_probe(box, d);
  } catch (e) { note(e.message); }
}

async function save_models(force) {
  try {
    const body = collect();
    body.force = !!force;
    if (force && !confirm("确认跳过验证并直接保存？\\n无效配置会导致后续对话请求失败。")) return;
    $("m_res").innerHTML = '<p class="muted small">正在验证…</p>';
    const r = await api("/admin/models", {method: "PUT", body: JSON.stringify(body)});
    LAST_CHECKS = r.checks || null;
    await load_models();
    note(r.warning || `配置已保存并完成热重载（${(r.providers || []).join("、")}）`, r.warning ? "err" : "ok");
  } catch (e) {
    note(e.message);
    if (e.payload && e.payload.checks) render_checks(e.payload.checks);
  }
}

$("key").addEventListener("keydown", e => { if (e.key === "Enter") login(); });
if (KEY) { $("login").classList.add("hide"); $("app").classList.remove("hide"); refresh(); }
</script>
</body>
</html>
"""
