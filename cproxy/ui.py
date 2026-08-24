"""内嵌的可视化页面。

单文件、零外部依赖（不引 CDN，断网和内网机器都能开），密钥只放在 sessionStorage，
关掉标签页就没了。所有数据都走已有的 /admin/* 接口，页面本身不碰数据库。
"""

PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>context-proxy</title>
<style>
:root {
  --bg:#f6f7f9; --card:#fff; --fg:#1c1f23; --muted:#6b7280; --line:#e3e6ea;
  --accent:#2563eb; --warn:#b45309; --danger:#b91c1c; --ok:#15803d;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
}
@media (prefers-color-scheme:dark){
  :root{ --bg:#15171a; --card:#1d2024; --fg:#e6e8ea; --muted:#9aa3ad; --line:#2c3138;
         --accent:#60a5fa; --warn:#fbbf24; --danger:#f87171; --ok:#4ade80; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:16px}
h1{font-size:18px;margin:0}
h2{font-size:15px;margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.spacer{flex:1}
button{font:inherit;padding:7px 13px;border-radius:7px;border:1px solid var(--line);
       background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{color:var(--danger)}
button:disabled{opacity:.5;cursor:not-allowed}
input,textarea{font:inherit;width:100%;padding:9px 11px;border-radius:7px;
               border:1px solid var(--line);background:var(--bg);color:var(--fg)}
textarea{font-family:var(--mono);font-size:13px;line-height:1.65;min-height:56vh;resize:vertical}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:500}
tbody tr{cursor:pointer}
tbody tr:hover{background:var(--bg)}
code,.mono{font-family:var(--mono);font-size:12.5px}
.muted{color:var(--muted)}
.pill{display:inline-block;padding:1px 7px;border-radius:99px;font-size:12px;border:1px solid var(--line)}
.pill.manual{color:var(--accent);border-color:var(--accent)}
.pill.partial{color:var(--warn);border-color:var(--warn)}
.pill.fallback{color:var(--danger);border-color:var(--danger)}
.stat{display:inline-block;margin-right:18px;font-size:13px}
.stat b{font-size:17px;font-weight:600;display:block}
.msg{padding:9px 12px;border-radius:7px;margin-bottom:12px;font-size:13.5px;white-space:pre-wrap}
.msg.err{background:#fee2e2;color:#991b1b}
.msg.ok{background:#dcfce7;color:#14532d}
@media (prefers-color-scheme:dark){.msg.err{background:#3b1a1a;color:#fca5a5}.msg.ok{background:#14301f;color:#86efac}}
.hide{display:none}
#login{max-width:380px;margin:12vh auto}
.scroll{overflow-x:auto}
</style>
</head>
<body>

<div id="login" class="card">
  <h1 style="margin-bottom:4px">context-proxy</h1>
  <p class="muted" style="margin:0 0 14px;font-size:13px">输入 ui_token 登录</p>
  <input id="key" type="password" autocomplete="current-password" placeholder="16 位密钥">
  <div id="loginmsg"></div>
  <button class="primary" style="width:100%;margin-top:10px" onclick="login()">进入</button>
</div>

<div id="app" class="wrap hide">
  <div class="row" style="margin-bottom:14px">
    <h1>context-proxy</h1><span class="spacer"></span>
    <button onclick="refresh()">刷新</button>
    <button onclick="logout()">退出</button>
  </div>
  <div id="health" class="card"></div>
  <div id="msg"></div>

  <div id="listview" class="card">
    <h2>会话</h2>
    <div class="scroll"><table>
      <thead><tr><th>conv_id</th><th>压缩进度</th><th>checkpoint</th><th>状态</th>
                 <th>摘要</th><th>兜底</th><th>最后活动</th></tr></thead>
      <tbody id="rows"></tbody>
    </table></div>
    <p id="empty" class="muted hide" style="font-size:13px">还没有会话——对话超过 trigger_tokens 触发第一次压缩后才会建档。</p>
  </div>

  <div id="detail" class="hide">
    <div class="card">
      <div class="row">
        <button onclick="back()">← 返回</button>
        <span class="spacer"></span>
        <span class="mono muted" id="d_id"></span>
      </div>
      <div id="d_stats" style="margin-top:12px"></div>
    </div>

    <div class="card">
      <div class="row" style="margin-bottom:10px">
        <h2 style="margin:0">当前生效的摘要</h2>
        <span class="spacer"></span>
        <span class="muted" id="d_tok" style="font-size:13px"></span>
        <button onclick="reload_summary()">放弃修改</button>
        <button class="primary" id="savebtn" onclick="save()">保存</button>
      </div>
      <textarea id="sum" spellcheck="false"></textarea>
      <p class="muted" style="font-size:12.5px;margin:8px 0 0">
        保存会写成一条新的 manual checkpoint，原来那条留在链上可回退；下一次请求生效，
        后续压缩在你写的内容之后追加。压缩进行中、或期间又压过一次，保存会被拒绝。
      </p>
    </div>

    <div class="card">
      <h2>checkpoint 链</h2>
      <div class="scroll"><table>
        <thead><tr><th>seq</th><th>类型</th><th>状态</th><th>压到</th><th>指纹</th>
                   <th>摘要</th><th>更新时间</th></tr></thead>
        <tbody id="cks"></tbody>
      </table></div>
      <div class="row" style="margin-top:12px">
        <span class="spacer"></span>
        <button class="danger" onclick="wipe()">清除这个会话</button>
      </div>
    </div>
  </div>
</div>

<script>
let KEY = sessionStorage.getItem("cproxy_key") || "";
let CUR = null;      // 当前打开的会话详情

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

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
  sessionStorage.removeItem("cproxy_key"); KEY = "";
  $("app").classList.add("hide"); $("login").classList.remove("hide");
  $("key").value = "";
}

function age(sec) {
  if (sec == null) return "-";
  if (sec < 60) return sec + " 秒前";
  if (sec < 3600) return Math.floor(sec / 60) + " 分钟前";
  if (sec < 86400) return Math.floor(sec / 3600) + " 小时前";
  return Math.floor(sec / 86400) + " 天前";
}

async function refresh() {
  note("");
  try {
    const h = await (await fetch("/health")).json();
    const fb = h.fallback_activations || 0, open = h.open_compression_events || 0;
    $("health").innerHTML =
      `<span class="stat"><b>${h.conversations}</b>会话</span>` +
      `<span class="stat"><b>${h.checkpoints}</b>checkpoint</span>` +
      `<span class="stat" style="color:${open ? "var(--warn)" : "inherit"}"><b>${open}</b>压缩中</span>` +
      `<span class="stat" style="color:${fb ? "var(--danger)" : "inherit"}"><b>${fb}</b>定位兜底</span>` +
      `<span class="stat"><b>${h.trigger_tokens}</b>触发阈值</span>` +
      `<span class="stat"><b>${h.keep_recent_tokens_effective}</b>近期原文下限` +
      (h.keep_recent_tokens_effective !== h.keep_recent_tokens_configured
        ? `<span class="muted">（配置 ${h.keep_recent_tokens_configured}，已按 50% 封顶）</span>` : "") +
      `</span>`;
    if (CUR) { await open_session(CUR); return; }
    const d = await api("/admin/sessions");
    $("rows").innerHTML = d.sessions.map(s => `
      <tr onclick="open_session('${esc(s.conv_id)}')">
        <td class="mono">${esc(s.conv_id_short)}</td>
        <td>第 ${s.round_upto ?? "-"} 轮 / 下标 ${s.compressed_upto ?? "-"}</td>
        <td>${s.checkpoints}</td>
        <td><span class="pill ${s.status === "partial" ? "partial" : ""}">${esc(s.status || "-")}</span></td>
        <td>${s.summary_chars ?? 0} 字</td>
        <td style="color:${s.fallback_count ? "var(--danger)" : "inherit"}">${s.fallback_count}</td>
        <td class="muted">${age(s.age_seconds)}</td>
      </tr>`).join("");
    $("empty").classList.toggle("hide", d.sessions.length > 0);
  } catch (e) { note(e.message); }
}

async function open_session(cid) {
  try {
    const [d, s] = [await api("/admin/session/" + cid),
                    await api("/admin/session/" + cid + "/summary")];
    CUR = cid;
    $("listview").classList.add("hide"); $("detail").classList.remove("hide");
    $("d_id").textContent = s.conv_id;
    $("d_stats").innerHTML =
      `<span class="stat"><b>${s.round_upto} / ${s.total_rounds}</b>已压缩轮次</span>` +
      `<span class="stat"><b>${s.compressed_upto}</b>压缩到的消息下标</span>` +
      `<span class="stat"><b>${d.checkpoints.length}</b>checkpoint</span>` +
      `<span class="stat"><b>${s.base_seq}</b>当前 seq</span>` +
      (s.editable ? "" : `<span class="stat" style="color:var(--warn)"><b>压缩中</b>暂不可保存</span>`);
    $("sum").value = s.summary;
    $("sum").dataset.base = s.base_seq;
    $("savebtn").disabled = !s.editable;
    $("d_tok").textContent = `${s.summary_tokens} / ${s.summary_cap_tokens} tokens`;
    $("cks").innerHTML = d.checkpoints.map(c => `
      <tr style="cursor:default">
        <td>${c.seq}${c.pinned ? ' <span class="pill">置顶</span>' : ""}</td>
        <td><span class="pill ${c.kind === "manual" ? "manual" : c.kind === "fallback" ? "fallback" : ""}">${esc(c.kind)}</span></td>
        <td>${c.status === "partial" ? '<span class="pill partial">partial</span>' : "sealed"}</td>
        <td>第 ${c.round_upto} 轮 / ${c.compressed_upto}</td>
        <td>${c.signature_len ?? '<span class="muted">无</span>'}</td>
        <td>${c.summary_tokens} tokens</td>
        <td class="muted">${new Date(c.updated_at * 1000).toLocaleString()}</td>
      </tr>`).join("");
  } catch (e) { note(e.message); }
}

function back() { CUR = null; $("detail").classList.add("hide"); $("listview").classList.remove("hide"); refresh(); }
async function reload_summary() { if (CUR) await open_session(CUR); note("已放弃未保存的修改", "ok"); }

async function save() {
  const box = $("sum");
  try {
    const r = await api("/admin/session/" + CUR + "/summary", {
      method: "PUT",
      body: JSON.stringify({summary: box.value, base_seq: Number(box.dataset.base)})
    });
    const ok = `已保存：seq ${r.base_seq} → ${r.new_seq}，${r.previous_summary_tokens} → ${r.summary_tokens} tokens。下一次请求生效。`;
    await refresh();          // 走 refresh 而不是 open_session，顶部统计也一起更新
    note(ok, "ok");
  } catch (e) { note(e.message); }
}

async function wipe() {
  if (!confirm("清除这个会话的全部压缩状态？\\n下次对话会从第 0 条开始全量重压，很贵。")) return;
  try {
    await api("/admin/clean", {method: "POST", body: JSON.stringify({target: CUR})});
    back(); note("已清除", "ok");
  } catch (e) { note(e.message); }
}

$("key").addEventListener("keydown", e => { if (e.key === "Enter") login(); });
if (KEY) { $("login").classList.add("hide"); $("app").classList.remove("hide"); refresh(); }
</script>
</body>
</html>
"""
