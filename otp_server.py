#!/usr/bin/env python3
"""
otp_server.py — 本地验证码中继（Mac 侧）

链路：
  iPhone 快捷指令(收到"验证码"短信) --POST--> 本服务 /otp {"text": "短信原文"}
  本服务抠出 4-8 位验证码 --> 写入剪贴板 + 弹桌面通知
  你在网页里 Cmd+V

零依赖：只用 Python 标准库 + macOS 自带的 pbcopy / osascript。
"""

import json
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ─── 配置 ────────────────────────────────────────────────
PORT = 8787
# 口令：留空字符串表示不校验；要用就填一串随机字符，并在快捷指令 Header 里加 X-Token
TOKEN = ""
# 命中这些关键词之一，才当作验证码短信处理（否则忽略，不动剪贴板）
KEYWORDS = ["验证码", "动态码", "校验码", "code", "verification", "otp", "one-time"]
# ─────────────────────────────────────────────────────────

# 4-8 位数字（大多数验证码是 4/6 位）。
# 用"前后都不是数字"来断界，而非 \b —— 因为中文字符在 Unicode 下算 \w，
# \b 会导致紧贴中文的数字（如"验证码是823941"）抠不出来。
_CODE_RE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def extract_code(text: str) -> str | None:
    """
    以关键词为锚抠码：找到最靠前的关键词，取它【之后】紧跟的第一个 4-8 位数字。
    这样能避开"尾号8888…验证码1234"里把手机尾号误当验证码的坑。
    找不到关键词后置数字时，退化为"优先 6 位，其次 4 位"。
    """
    low = text.lower()
    kw_pos = min(
        (low.find(k.lower()) for k in KEYWORDS if k.lower() in low),
        default=-1,
    )
    if kw_pos == -1:
        return None  # 没命中关键词，不当验证码处理

    # 关键词之后紧跟的第一个数字组（最优）
    after = _CODE_RE.search(text, kw_pos)
    if after:
        return after.group(1)

    # 退化：关键词在数字之后（如"823941 是您的验证码"），全局挑一个
    candidates = _CODE_RE.findall(text)
    for want in (6, 4):
        for c in candidates:
            if len(c) == want:
                return c
    return candidates[0] if candidates else None


def copy_to_clipboard(code: str) -> None:
    subprocess.run(["pbcopy"], input=code.encode(), check=True)


def notify(code: str) -> None:
    script = f'display notification "验证码 {code} 已复制，可直接 Cmd+V" with title "OTP"'
    subprocess.run(["osascript", "-e", script], check=False)


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(body.encode())

    def do_GET(self):
        # 健康检查：浏览器/curl 访问根路径能看到 ok
        self._send(200, "otp-relay ok\n")

    def do_POST(self):
        if self.path.rstrip("/") != "/otp":
            self._send(404, "not found\n")
            return

        if TOKEN and self.headers.get("X-Token") != TOKEN:
            self._send(401, "bad token\n")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")

        # 兼容两种请求体：JSON {"text": "..."} 或 直接纯文本
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
            print(f"[HIT] code={code}  <- {text!r}")
            self._send(200, f"ok {code}\n")
        else:
            print(f"[skip] no code  <- {text!r}")
            self._send(200, "skip\n")

    def log_message(self, *args):  # 静音默认访问日志，用我们自己的 print
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"otp-relay 已启动：监听 0.0.0.0:{PORT}，路由 POST /otp")
    print("按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        server.server_close()


if __name__ == "__main__":
    main()
