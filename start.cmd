@echo off
REM start.cmd — Windows 一键启动 OTP 中继
REM 双击运行，或在 cmd/PowerShell 里执行。会启动服务并打印状态页地址。
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3 并勾选 "Add to PATH"。
  echo 下载：https://www.python.org/downloads/
  pause
  exit /b 1
)

echo ────────────────────────────────────────────────────
echo  otp-relay 启动中……
echo  状态/配置页（本机）: http://localhost:8787/
echo  手机端 POST 目标   : http://^<本机IP^>:8787/otp
echo  （手机该填的具体 IP 在状态页顶部会列出）
echo  手机和这台电脑需连同一个 Wi-Fi，或电脑连着手机热点。
echo ────────────────────────────────────────────────────
echo.

python "%~dp0otp_server.py"
pause
