#!/usr/bin/env bash
# start.sh — 一键启动 OTP 中继：自动探测本机 IP，打印"手机该填的 URL"，再启动服务。
set -euo pipefail

PORT=8787
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 端口已被占用 = 服务可能已在跑，避免重复启动
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "⚠️  端口 $PORT 已被占用，服务可能已经在跑了。"
  echo "    要重启就先停掉旧的：pkill -f otp_server.py"
  exit 1
fi

# 候选 IP：优先 Wi-Fi(en0)，再列出其它非回环 IPv4 作备选
primary="$(ipconfig getifaddr en0 2>/dev/null || true)"
[ -z "$primary" ] && primary="$(ipconfig getifaddr en1 2>/dev/null || true)"
others="$(ifconfig 2>/dev/null | grep 'inet ' | grep -v '127.0.0.1' | awk '{print $2}')"

echo "────────────────────────────────────────────────────"
if [ -n "$primary" ]; then
  echo "📱 在手机上把转发地址填成："
  echo ""
  echo "        http://$primary:$PORT/otp"
  echo ""
else
  echo "⚠️  没探测到 Wi-Fi 网卡 IP，从下面备选里挑一个和手机同网段的。"
fi
echo "   （手机和这台 Mac 必须连同一个 Wi-Fi，或 Mac 连着手机热点）"
echo ""
echo "   所有本机 IP 备选（换网络后地址会变，以此为准）："
for ip in $others; do
  [ "$ip" = "$primary" ] && echo "     * $ip   ← 首选" || echo "       $ip"
done
echo ""
echo "   本机状态/配置页（含 Token 开关）：http://localhost:$PORT/"
echo "   手机自测：Safari 打开 http://${primary:-<上面某个IP>}:$PORT/  应看到状态页"
echo "────────────────────────────────────────────────────"
echo ""

exec python3 "$DIR/otp_server.py"
