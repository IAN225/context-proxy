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
    </nav>
    <div id="msg"></div>

    <!-- 会话列表 -->
    <div id="pane_s">
      <div class="card"><div id="health" class="stats"></div></div>
      <div id="rows"></div>
      <p id="empty" class="muted small hide">还没有会话——对话超过 trigger_tokens 触发第一次压缩后才会建档。</p>
    </div>

    <!-- 会话详情 -->
    <div id="pane_d" class="hide">
      <button class="ghost" style="padding-left:0" onclick="back()">← 返回会话列表</button>
      <div class="card">
        <div class="mono muted" id="d_id" style="margin-bottom:10px;word-break:break-all"></div>
        <div id="d_stats" class="stats"></div>
      </div>

      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <h2>当前生效的摘要</h2><span class="spacer"></span>
          <span class="muted small" id="d_tok"></span>
        </div>
        <textarea id="sum" rows="14" spellcheck="false"></textarea>
        <div class="row" style="margin-top:10px">
          <button onclick="reload_summary()">放弃修改</button>
          <span class="spacer"></span>
          <button class="primary" id="savebtn" onclick="save()">保存摘要</button>
        </div>
        <p class="muted small" style="margin:9px 0 0" id="sumhint"></p>
        <p class="muted small" style="margin:6px 0 0">
          保存写成一条新的 manual checkpoint 并固定为当前，原来那条留在链上可回退；
          下一次请求生效，后续压缩在你写的内容之后追加。
        </p>
      </div>

      <div class="card">
        <div class="row" style="margin-bottom:10px">
          <h2>本次请求：进来 → 发出去</h2><span class="spacer"></span>
          <button class="ghost small" onclick="load_timeline()">加载</button>
        </div>
        <div id="tlbox"><p class="muted small" style="margin:0">点「加载」看最近一次请求的对比与时间轴。</p></div>
      </div>

      <div class="card">
        <h2 style="margin-bottom:10px">checkpoint 链</h2>
        <div id="cks"></div>
        <button class="danger" style="width:100%;margin-top:6px" onclick="wipe()">清除这个会话</button>
      </div>
    </div>

    <!-- 提示词 -->
    <div id="pane_p" class="hide"><div id="prompts"></div></div>
  </div>
</div>

<script>
let KEY = sessionStorage.getItem("cproxy_key") || "";
let CUR = null, TL = null, TL_SHOWN = 0;
const PAGE_ROUNDS = 60;      // 时间轴一次渲染多少轮，几千轮的会话不能一次全画

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = n => (n == null ? "-" : String(n));

function note(text, kind, box) {
  const el = $(box || "msg");
  el.innerHTML = text ? `<div class="msg ${kind || "err"}">${esc(text)}</div>` : "";
  if (text && !box) el.scrollIntoView({block: "nearest", behavior: "smooth"});
}

async function api(path, opts) {
  const o = Object.assign({headers: {}}, opts || {});
  o.headers["Authorization"] = "Bearer " + KEY;
  if (o.body) o.headers["Content-Type"] = "application/json";
  const r = await fetch(path, o);
  let data = null;
  try { data = await r.json(); } catch (e) {}
  if (r.status === 401) { logout(); throw new Error("密钥无效或已失效"); }
  if (!r.ok) throw new Error((data && data.error && data.error.message) || ("HTTP " + r.status));
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
  $("tab_s").classList.toggle("on", t === "s");
  $("tab_p").classList.toggle("on", t === "p");
  $("pane_p").classList.toggle("hide", t !== "p");
  const inSession = t === "s";
  $("pane_s").classList.toggle("hide", !inSession || !!CUR);
  $("pane_d").classList.toggle("hide", !inSession || !CUR);
  note("");
  if (t === "p") load_prompts();
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
      stat(todo, "没压完", todo ? "var(--warn)" : null) +
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
    CUR = cid; TL = null; TL_SHOWN = 0;
    $("pane_s").classList.add("hide"); $("pane_d").classList.remove("hide");
    $("tlbox").innerHTML = '<p class="muted small" style="margin:0">点「加载」看最近一次请求的对比与时间轴。</p>';
    $("d_id").textContent = s.conv_id;
    $("d_stats").innerHTML =
      stat(s.round_upto + " / " + s.total_rounds, "已压缩轮次") +
      stat(s.compressed_upto, "压缩到的下标") +
      stat(d.checkpoints.length, "checkpoint") +
      stat(s.base_seq, "当前 seq") +
      (s.busy ? stat("压缩中", "暂不可保存", "var(--warn)") : "") +
      (s.unfinished_event_seq != null
        ? stat("seq " + s.unfinished_event_seq, "上次没压完", "var(--warn)") : "");
    $("sum").value = s.summary;
    $("sum").dataset.base = s.base_seq;
    $("savebtn").disabled = !s.editable;
    $("sumhint").textContent = s.busy
      ? "这个会话此刻有请求在跑，等它结束再改。"
      : (s.unfinished_event_seq != null
          ? `上次压缩没压完（事件 seq ${s.unfinished_event_seq}），这是静止状态、不影响编辑；`
            + "保存后那个半成品会作废，下次请求从你这条继续压。"
          : "保存即固定为当前 checkpoint，下一次请求就用它，重启后依然是它。");
    $("d_tok").textContent = `${s.summary_tokens} / ${s.summary_cap_tokens} tokens`;
    const cur = d.checkpoints.length ? Math.max(...d.checkpoints.map(c => c.seq)) : null;
    $("cks").innerHTML = d.checkpoints.map(c => `
      <div class="item" style="cursor:default">
        <div class="top"><b>seq ${c.seq}</b>
          <span class="pill ${c.kind === "manual" ? "on" : c.kind === "fallback" ? "bad" : ""}">${esc(c.kind)}</span>
          ${c.seq === cur ? '<span class="pill on">当前</span>' : ""}
          ${c.pinned === 2 ? '<span class="pill on">手工固定</span>' : c.pinned ? '<span class="pill">置顶</span>' : ""}
          ${c.status === "partial" ? '<span class="pill warn">没压完</span>' : ""}
          ${c.status === "stale" ? '<span class="pill">已作废</span>' : ""}</div>
        <div class="meta">第 ${c.round_upto} 轮 / 下标 ${c.compressed_upto} ·
          ${c.summary_tokens} tokens · 指纹 ${c.signature_len ?? "无"}</div>
        <div class="meta">${new Date(c.updated_at * 1000).toLocaleString()}</div>
        ${c.seq === cur || !c.summary_tokens ? "" :
          `<button class="ghost" style="margin-top:8px;width:100%"
             onclick="activate(${c.seq}, ${c.round_upto})">设为当前摘要</button>`}
      </div>`).join("");
  } catch (e) { note(e.message); }
}

function back() { CUR = null; TL = null; $("pane_d").classList.add("hide"); $("pane_s").classList.remove("hide"); refresh(); }
async function reload_summary() { if (CUR) { await open_session(CUR); note("已放弃未保存的修改", "ok"); } }

async function save() {
  const box = $("sum");
  try {
    const r = await api("/admin/session/" + CUR + "/summary", {
      method: "PUT",
      body: JSON.stringify({summary: box.value, base_seq: Number(box.dataset.base)})
    });
    const ok = `已保存：seq ${r.base_seq} → ${r.new_seq}，${r.previous_summary_tokens} → ${r.summary_tokens} tokens。下一次请求生效。`;
    await refresh(); note(ok, "ok");
  } catch (e) { note(e.message); }
}

async function activate(seq, round_upto) {
  if (!confirm(`把 seq ${seq} 的摘要设为当前摘要？\n\n`
      + `之后发给模型的、以及继续压缩所基于的都是它（已压到第 ${round_upto} 轮）。\n`
      + "这条会被固定住，重启也不会变回去；更晚的 checkpoint 仍然留着，可以再选回去。")) return;
  try {
    const r = await api(`/admin/session/${CUR}/checkpoint/${seq}/activate`, {method: "POST"});
    await refresh();
    note(`已固定 seq ${r.from_seq} 为当前摘要（新 seq ${r.new_seq}，第 ${r.round_upto} 轮）`, "ok");
  } catch (e) { note(e.message); }
}

async function wipe() {
  if (!confirm("清除这个会话的全部压缩状态？\\n下次对话会从第 0 条开始全量重压，很贵。")) return;
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
         保存会<b>直接写回 config.yaml</b>（只替换正文，文件里的注释原样保留，
         原文件备份成 config.yaml.bak），保存后立即热重载，重启依然生效。
       </p></div>` +
      d.prompts.map((p, i) => `
      <div class="card">
        <div class="row" style="margin-bottom:8px">
          <h2>${esc(p.label)}</h2><span class="spacer"></span>
          ${p.overridden ? '<span class="pill warn">仅内存</span>' : '<span class="pill">config.yaml</span>'}
        </div>
        <div class="mono muted small" style="margin-bottom:6px">${esc(p.name)}${
          p.requires && p.requires.length ? " · 必须包含 " + esc(p.requires[0]) : ""}</div>
        <textarea id="pr_${i}" rows="10" spellcheck="false">${esc(p.effective)}</textarea>
        <div class="row" style="margin-top:10px">
          <button onclick="reset_prompt(${i})">恢复默认</button>
          <span class="spacer"></span>
          <button class="primary" onclick="save_prompt(${i})">保存到 config.yaml</button>
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
              `已把「${p.label}」写回 config.yaml，下一次压缩生效。`);
}
function reset_prompt(i) {
  const p = window._prompts[i];
  if (!confirm(`把「${p.label}」恢复成内置默认值并写回 config.yaml？`)) return;
  put_prompts({[p.name]: ""}, `「${p.label}」已恢复成默认值并写回 config.yaml。`);
}

$("key").addEventListener("keydown", e => { if (e.key === "Enter") login(); });
if (KEY) { $("login").classList.add("hide"); $("app").classList.remove("hide"); refresh(); }
</script>
</body>
</html>
"""
