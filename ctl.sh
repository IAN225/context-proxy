#!/usr/bin/env bash
# 前台/后台管理脚本。长期运行推荐用 systemd（见 readme），这里主要用于本地调试。
set -euo pipefail
cd "$(dirname "$0")"

PIDFILE="proxy.pid"
STDERR_LOG="logs/stderr.log"      # 只兜住进程级崩溃输出；正常日志由 proxy 自己轮转写 logs/proxy.log
LOG="logs/proxy.log"
PORT="${PROXY_PORT:-8787}"

# 本脚本只调 /admin/*：配了 ui_token 就必须用 ui_token（服务端此时不再认 auth_token），
# 没配才退回 auth_token。环境变量优先于 config.yaml。
_cfg_val() { # _cfg_val KEY
  [ -f config.yaml ] || return 0
  grep -E "^\s*$1:" config.yaml | head -1 | sed -E "s/.*$1:\s*\"?([^\"#]*)\"?.*/\1/" | xargs || true
}
TOKEN="${PROXY_UI_TOKEN:-}"
[ -n "$TOKEN" ] || TOKEN=$(_cfg_val ui_token)
if [ -z "$TOKEN" ]; then
  TOKEN="${PROXY_AUTH_TOKEN:-}"
  [ -n "$TOKEN" ] || TOKEN=$(_cfg_val auth_token)
fi

if [ -x "venv/bin/python" ] && venv/bin/python -c "import httpx" 2>/dev/null; then
  PY="venv/bin/python"
else
  PY="python3"
fi

_pid() {
  pgrep -f "proxy.py" 2>/dev/null | while read -r p; do
    c=$(tr '\0' ' ' </proc/"$p"/cmdline 2>/dev/null || true)
    case "$c" in
      *python*\ proxy.py*|*python*/proxy.py*) echo "$p";;
    esac
  done | head -1
}

_api() { # _api METHOD PATH [DATA]
  local m="$1" p="$2" d="${3:-}"
  if [ -n "$d" ]; then
    curl -s -X "$m" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d "$d" "http://127.0.0.1:$PORT$p"
  else
    curl -s -X "$m" -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT$p"
  fi | python3 -m json.tool 2>/dev/null || echo "(请求失败或服务未运行)"
}

case "${1:-status}" in
  start)
    if [ -n "$(_pid)" ]; then echo "已在运行 PID=$(_pid)"; exit 0; fi
    mkdir -p logs
    # >> 而不是 >：每次重启都保留上一次的崩溃现场
    setsid nohup "$PY" proxy.py >> "$STDERR_LOG" 2>&1 < /dev/null &
    sleep 3
    p=$(_pid)
    if [ -z "$p" ]; then
      echo "启动失败，最后 20 行 $STDERR_LOG："
      tail -20 "$STDERR_LOG" 2>/dev/null
      exit 1
    fi
    echo "$p" > "$PIDFILE"
    echo "已启动 PID=$p"
    _api GET /health
    ;;
  stop)
    p=$(_pid)
    if [ -n "$p" ]; then kill "$p"; echo "已停止 PID=$p"; else echo "未在运行"; fi
    rm -f "$PIDFILE"
    ;;
  restart) "$0" stop || true; sleep 1; "$0" start ;;
  reload)
    if curl -fsS -X POST -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:$PORT/admin/reload" >/dev/null; then
      echo "已热重载"; _api GET /health
    else
      p=$(_pid)
      if [ -n "$p" ]; then kill -HUP "$p"; echo "已发 SIGHUP 重载 PID=$p"; else echo "未在运行"; fi
    fi
    ;;
  status)
    p=$(_pid)
    if [ -n "$p" ]; then echo "运行中 PID=$p"; _api GET /health; else echo "未运行"; fi
    ;;
  sessions) _api GET /admin/sessions ;;
  session)
    [ -z "${2:-}" ] && { echo "用法: $0 session <conv_id 前几位>"; exit 1; }
    _api GET "/admin/session/$2"
    ;;
  ui-token)
    python3 -c "import secrets,string; a=string.ascii_letters+string.digits; print(''.join(secrets.choice(a) for _ in range(16)))"
    echo "把它填进 config.yaml 的 server.ui_token，然后 $0 reload；页面地址 http://<服务器IP>:$PORT/ui" >&2
    ;;
  summary)
    [ -z "${2:-}" ] && { echo "用法: $0 summary <conv_id 前几位>"; exit 1; }
    curl -s -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:$PORT/admin/session/$2/summary?format=text"
    echo
    ;;
  edit)
    # 取回摘要 -> 打开编辑器 -> 存回。带 base_seq 乐观锁，期间发生过压缩会拒绝写入。
    [ -z "${2:-}" ] && { echo "用法: $0 edit <conv_id 前几位>   （编辑器取 \$EDITOR，默认 vi）"; exit 1; }
    tmp=$(mktemp -t cproxy-summary-XXXXXX.md)
    trap 'rm -f "$tmp" "$tmp.orig" "$tmp.meta" "$tmp.json"' EXIT
    curl -s -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:$PORT/admin/session/$2/summary" > "$tmp.meta"
    python3 - "$tmp.meta" "$tmp" <<'PY' || exit 1
import json, sys
raw = open(sys.argv[1], encoding="utf-8").read()
try:
    d = json.loads(raw)
except Exception:
    print("服务返回的不是 JSON：" + raw[:500], file=sys.stderr); sys.exit(1)
if "summary" not in d:
    print("取摘要失败：" + json.dumps(d, ensure_ascii=False, indent=2), file=sys.stderr); sys.exit(1)
open(sys.argv[2], "w", encoding="utf-8").write(d["summary"])
print("会话 {}｜base_seq={}｜{} tokens（上限 {}）｜已压到第 {}/{} 轮".format(
    d["conv_id"][:16], d["base_seq"], d["summary_tokens"], d["summary_cap_tokens"],
    d["round_upto"], d["total_rounds"]), file=sys.stderr)
if d.get("busy"):
    print("⚠️  该会话此刻有请求在跑，现在存回可能被拒绝，等它结束再改", file=sys.stderr)
elif d.get("unfinished_event_seq") is not None:
    print("提示：上次压缩没压完（事件 seq={}），不影响编辑；"
          "存回后那个半成品会作废，下次请求从你这条继续压".format(
              d.get("unfinished_event_seq")), file=sys.stderr)
PY
    cp "$tmp" "$tmp.orig"
    "${EDITOR:-vi}" "$tmp"
    if cmp -s "$tmp" "$tmp.orig"; then echo "内容没有变化，未提交"; exit 0; fi
    python3 - "$tmp.meta" "$tmp" > "$tmp.json" <<'PY' || exit 1
import json, sys
d = json.loads(open(sys.argv[1], encoding="utf-8").read())
print(json.dumps({"summary": open(sys.argv[2], encoding="utf-8").read(),
                  "base_seq": d["base_seq"]}, ensure_ascii=False))
PY
    curl -s -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      --data-binary @"$tmp.json" \
      "http://127.0.0.1:$PORT/admin/session/$2/summary" | python3 -m json.tool
    ;;
  clean)
    [ -z "${2:-}" ] && { echo "用法: $0 clean all | $0 clean <conv_id 前几位>"; exit 1; }
    if [ "$2" = "all" ]; then
      printf '确认清空【全部】会话？这会让所有对话下次触发全量重压 [y/N] '
      read -r ans
      case "$ans" in y|Y) ;; *) echo "已取消"; exit 0;; esac
    fi
    _api POST /admin/clean "{\"target\":\"$2\"}"
    ;;
  tasks) _api GET /admin/tasks ;;
  cancel)
    [ -z "${2:-}" ] && { echo "用法: $0 cancel <conv_id 前几位>"; exit 1; }
    _api POST "/admin/session/$2/cancel"
    ;;
  models) _api GET /admin/models ;;
  log) tail -f "$LOG" ;;
  errlog) tail -f "$STDERR_LOG" ;;
  *)
    echo "用法: $0 {start|stop|restart|reload|status|log|errlog|sessions|session <id>|summary <id>|edit <id>|tasks|cancel <id>|models|ui-token|clean all|clean <id>}"
    exit 1
    ;;
esac
