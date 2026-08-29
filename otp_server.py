#!/usr/bin/env python3
"""
otp_server.py — 本地验证码中继（Mac 侧）

链路：
  iPhone 快捷指令 / Android 短信转发器（收到"验证码"短信）
    --POST--> 本服务 /otp {"text": "短信原文"}
  本服务抠出 4-8 位验证码 --> 写入剪贴板 + 弹桌面通知
  你在网页里 Cmd+V

内置一个状态/配置页（GET /）：显示手机该填的 URL、token 开关、最近活动。
  - 看状态：同一局域网可访问（方便手机确认连通）
  - 改 token：仅限本机 localhost（同网段的人改不了你的口令）
  - 最近验证码在页面上脱敏显示（同网段的人读不到你的真实码）

零依赖：只用 Python 标准库 + macOS 自带的 pbcopy / osascript。
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

# ─── 配置 ────────────────────────────────────────────────
PORT = 8787
CONFIG_PATH = Path(__file__).with_name("config.json")  # 存 token，已 gitignore
# 命中这些关键词之一，才当作验证码短信处理（否则忽略，不动剪贴板）
KEYWORDS = ["验证码", "动态码", "校验码", "code", "verification", "otp", "one-time"]

# 登录自启（LaunchAgent）
LAUNCH_LABEL = "com.otp-relay.local"
LAUNCH_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_LABEL}.plist"
SCRIPT_PATH = Path(__file__).resolve()
# ─────────────────────────────────────────────────────────


def autostart_enabled() -> bool:
    return LAUNCH_PLIST.exists()


def enable_autostart() -> None:
    """写入并加载 LaunchAgent，登录时自动后台启动本服务。"""
    log = SCRIPT_PATH.with_name("otp-relay.log")
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCH_LABEL}</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>{SCRIPT_PATH}</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""
    LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_PLIST.write_text(plist)
    # 尽量加载（已在跑时 bootstrap 可能报"已存在"，忽略即可）
    subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCH_PLIST)],
        capture_output=True,
    )


def disable_autostart() -> None:
    """卸载并删除 LaunchAgent。"""
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_LABEL}"],
        capture_output=True,
    )
    LAUNCH_PLIST.unlink(missing_ok=True)
# ─────────────────────────────────────────────────────────

# 4-8 位数字。用"前后都不是数字"断界，而非 \b（中文在 Unicode 下算 \w，\b 会漏）。
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")

# 最近活动（内存，重启清空）
RECENT = deque(maxlen=12)


def load_token() -> str:
    try:
        return str(json.loads(CONFIG_PATH.read_text()).get("token", ""))
    except (FileNotFoundError, json.JSONDecodeError):
        return ""


def save_token(token: str) -> None:
    CONFIG_PATH.write_text(json.dumps({"token": token}, ensure_ascii=False, indent=2))


TOKEN = load_token()


def extract_code(text: str) -> str | None:
    """以关键词为锚：取关键词【之后】紧跟的第一个 4-8 位数字，避开手机尾号误抓。"""
    low = text.lower()
    kw_pos = min((low.find(k.lower()) for k in KEYWORDS if k.lower() in low), default=-1)
    if kw_pos == -1:
        return None
    after = _CODE_RE.search(text, kw_pos)
    if after:
        return after.group(1)
    candidates = _CODE_RE.findall(text)  # 退化：码在关键词之前
    for want in (6, 4):
        for c in candidates:
            if len(c) == want:
                return c
    return candidates[0] if candidates else None


def mask(code: str) -> str:
    """脱敏：8****1，只露首尾，防止局域网访问者从状态页读到真实码。"""
    if len(code) <= 2:
        return "•" * len(code)
    return code[0] + "•" * (len(code) - 2) + code[-1]


def copy_to_clipboard(code: str) -> None:
    subprocess.run(["pbcopy"], input=code.encode(), check=True)


def notify(code: str) -> None:
    script = f'display notification "验证码 {code} 已复制，可直接 Cmd+V" with title "OTP"'
    subprocess.run(["osascript", "-e", script], check=False)


def local_ips() -> list[str]:
    """所有非回环 IPv4，首选 en0 排最前。"""
    ips: list[str] = []
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet ") and "127.0.0.1" not in line:
                ip = line.split()[1]
                if not ip.startswith(("169.254.", "fe80")):
                    ips.append(ip)
    except Exception:
        pass
    try:
        en0 = subprocess.run(
            ["ipconfig", "getifaddr", "en0"], capture_output=True, text=True
        ).stdout.strip()
        if en0 in ips:
            ips.remove(en0)
            ips.insert(0, en0)
    except Exception:
        pass
    return ips


def record(kind: str, detail: str) -> None:
    RECENT.appendleft((datetime.now().strftime("%H:%M:%S"), kind, detail))


# ─── 状态页 HTML ─────────────────────────────────────────
def render_status_page(is_local: bool) -> str:
    ips = local_ips()
    url_rows = "".join(
        f'<div class="url {"primary" if i == 0 else ""}">'
        f'http://{ip}:{PORT}/otp{" ← 首选" if i == 0 else ""}</div>'
        for i, ip in enumerate(ips)
    ) or '<div class="url">未探测到局域网 IP</div>'

    token_on = bool(TOKEN)
    token_badge = (
        '<span class="badge on">已开启</span>'
        if token_on
        else '<span class="badge off">未开启</span>'
    )

    if is_local:
        token_form = f"""
        <form method="POST" action="/config" class="tokenform">
          <input type="text" name="token" placeholder="留空=关闭校验"
                 value="{TOKEN}" autocomplete="off">
          <button type="submit">保存 token</button>
        </form>
        <p class="hint">开启后，手机端请求需加头部 <code>X-Token: 你填的值</code>。</p>
        """
    else:
        token_form = (
            '<p class="hint">🔒 修改 token 只能在这台 Mac 上（localhost）操作。</p>'
        )

    auto_on = autostart_enabled()
    auto_badge = (
        '<span class="badge on">已开启</span>'
        if auto_on
        else '<span class="badge off">未开启</span>'
    )
    if is_local:
        auto_action = "/autostart/off" if auto_on else "/autostart/on"
        auto_label = "关闭登录自启" if auto_on else "开启登录自启"
        server_controls = f"""
        <form method="POST" action="{auto_action}" style="display:inline">
          <button type="submit">{auto_label}</button>
        </form>
        <form method="POST" action="/shutdown" style="display:inline"
              onsubmit="return confirm('停止后需在 Mac 上重新运行 ./start.sh（或靠登录自启）才能再开。确定停止？')">
          <button type="submit" class="danger">停止服务器</button>
        </form>
        <p class="hint">停止后本页会失效——服务器是它自己托管的，只能从 Mac 命令行或登录自启再开。</p>
        """
    else:
        server_controls = (
            '<p class="hint">🔒 开关服务器 / 登录自启只能在这台 Mac 上操作。</p>'
        )

    rows = "".join(
        f"<tr><td>{t}</td><td>{'✅ 命中' if k == 'hit' else '➖ 忽略'}</td><td>{d}</td></tr>"
        for t, k, d in RECENT
    ) or '<tr><td colspan="3" class="empty">还没收到任何请求</td></tr>'

    return f"""<!doctype html><html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>otp-relay 状态</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 640px;
    margin: 0 auto; padding: 20px; line-height: 1.5; }}
  h1 {{ font-size: 20px; }} h2 {{ font-size: 15px; color: #888; margin-top: 28px; }}
  .card {{ background: rgba(127,127,127,.12); border-radius: 12px; padding: 16px; }}
  .url {{ font-family: ui-monospace, monospace; font-size: 15px; padding: 6px 0;
    word-break: break-all; }}
  .url.primary {{ font-weight: 700; }}
  .badge {{ padding: 2px 10px; border-radius: 999px; font-size: 13px; }}
  .badge.on {{ background: #1a7f37; color: #fff; }}
  .badge.off {{ background: #9a6700; color: #fff; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  td, th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid rgba(127,127,127,.2); }}
  .empty {{ color: #999; text-align: center; }}
  .tokenform {{ display: flex; gap: 8px; margin-top: 8px; }}
  .tokenform input {{ flex: 1; padding: 8px; border-radius: 8px;
    border: 1px solid rgba(127,127,127,.4); background: transparent; color: inherit; }}
  .tokenform button, .card button {{ padding: 8px 14px; border: 0; border-radius: 8px;
    background: #0a84ff; color: #fff; font-weight: 600; margin-right: 8px; }}
  .card button.danger {{ background: #d1242f; }}
  .hint {{ color: #888; font-size: 13px; }} code {{ font-family: ui-monospace, monospace; }}
  .status-ok {{ color: #1a7f37; font-weight: 600; }}
</style></head><body>
  <h1>otp-relay <span class="status-ok">● 运行中</span></h1>
  <p class="hint">页面每 5 秒自动刷新。手机和这台 Mac 需在同一 Wi-Fi，或 Mac 连着手机热点。</p>

  <h2>📱 手机端把转发地址填成</h2>
  <div class="card">{url_rows}</div>

  <h2>🔐 安全（Token）&nbsp;{token_badge}</h2>
  <div class="card">{token_form}</div>

  <h2>⚙️ 服务控制&nbsp;（登录自启 {auto_badge}）</h2>
  <div class="card">{server_controls}</div>

  <h2>🕑 最近活动（验证码已脱敏）</h2>
  <div class="card"><table>
    <tr><th>时间</th><th>结果</th><th>详情</th></tr>{rows}
  </table></div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _is_local(self) -> bool:
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _send(self, status: int, body: str = "", ctype: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body.encode())

    def do_GET(self):
        path = self.path.rstrip("/") or "/"
        if path == "/":
            self._send(200, render_status_page(self._is_local()), "text/html")
        elif path == "/status":
            self._send(
                200,
                json.dumps(
                    {"ok": True, "ips": local_ips(), "token_enabled": bool(TOKEN)}
                ),
                "application/json",
            )
        else:
            self._send(404, "not found\n")

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/otp":
            self._handle_otp()
        elif path == "/config":
            self._handle_config()
        elif path == "/autostart/on":
            self._handle_autostart(True)
        elif path == "/autostart/off":
            self._handle_autostart(False)
        elif path == "/shutdown":
            self._handle_shutdown()
        else:
            self._send(404, "not found\n")

    def _redirect_home(self):
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_autostart(self, enable: bool):
        if not self._is_local():
            self._send(403, "only allowed from localhost\n")
            return
        try:
            enable_autostart() if enable else disable_autostart()
        except Exception as e:
            print(f"[autostart] 失败: {e}")
        self._redirect_home()

    def _handle_shutdown(self):
        if not self._is_local():
            self._send(403, "only allowed from localhost\n")
            return
        self._send(200, "服务器正在停止……本页已失效。重开请在 Mac 上运行 ./start.sh\n")
        print("[shutdown] 收到本机停止请求，退出。")
        # 另起线程延迟退出，先把响应发完
        threading.Thread(target=lambda: (time.sleep(0.3), os._exit(0)), daemon=True).start()

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length).decode("utf-8", errors="replace")

    def _handle_config(self):
        # 改 token 只允许本机，防止同网段的人改你口令
        if not self._is_local():
            self._send(403, "config only allowed from localhost\n")
            return
        global TOKEN
        params = parse_qs(self._read_body())
        TOKEN = params.get("token", [""])[0].strip()
        save_token(TOKEN)
        # 表单提交后重定向回状态页
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def _handle_otp(self):
        if TOKEN and self.headers.get("X-Token") != TOKEN:
            record("skip", "token 不匹配，已拒绝")
            self._send(401, "bad token\n")
            return

        raw = self._read_body()
        text = raw
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "text" in data:
                text = str(data["text"])
        except json.JSONDecodeError:
            pass

        code = extract_code(text)
        if code:
            copy_to_clipboard(code)
            notify(code)
            record("hit", f"验证码 {mask(code)} 已复制")
            print(f"[HIT] code={code}  <- {text!r}")
            self._send(200, f"ok {code}\n")
        else:
            record("skip", "未识别到验证码")
            print(f"[skip] no code  <- {text!r}")
            self._send(200, "skip\n")

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"otp-relay 已启动：http://localhost:{PORT}/  （状态/配置页）")
    print(f"手机端 POST 目标：http://<本机IP>:{PORT}/otp")
    print(f"Token 校验：{'开启' if TOKEN else '关闭'}。按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
