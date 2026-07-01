#!/usr/bin/env python3
"""pocket-pair — show a QR that pairs the Pocket phone app to THIS desktop.

Security model: the phone is a remote control; all agent auth (CC/Codex/Hermes)
and the bridge token live here on the desktop. This tool runs ON the desktop
(physical access), reads the bridge token straight from the LaunchAgent plist,
and renders a QR encoding:

    pocket://pair?scheme=https&host=<funnel-host>&token=<bridge-token>

The phone (設定 → 連線桌面 → 掃描 QR 配對) scans it, verifies the bridge
answers, and stores the connection. No pairing endpoint is exposed on the
network — the token never leaves this machine except inside the QR you scan.
"""
import os
import sys
import plistlib
import tempfile
import subprocess
import urllib.parse

PLIST = os.path.expanduser("~/Library/LaunchAgents/ai.studio.hermes-bridge.plist")
# Public Tailscale Funnel surface (works on any network, no Tailscale on phone).
HOST = os.environ.get("POCKET_FUNNEL_HOST", "cashcamp-1.tail905550.ts.net")
SCHEME = os.environ.get("POCKET_FUNNEL_SCHEME", "https")


def read_token() -> str:
    if os.environ.get("BRIDGE_TOKEN"):
        return os.environ["BRIDGE_TOKEN"]
    try:
        with open(PLIST, "rb") as f:
            data = plistlib.load(f)
        return (data.get("EnvironmentVariables") or {}).get("BRIDGE_TOKEN", "")
    except FileNotFoundError:
        return ""


def ensure_qrcode():
    try:
        import qrcode  # noqa: F401
        return
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "qrcode"], check=False)


def main() -> int:
    token = read_token().strip()
    if not token or token.lower().startswith("change-me"):
        print("✗ 找不到有效的 BRIDGE_TOKEN(檢查 %s)。" % PLIST, file=sys.stderr)
        return 1

    url = "pocket://pair?scheme=%s&host=%s&token=%s" % (
        urllib.parse.quote(SCHEME),
        urllib.parse.quote(HOST),
        urllib.parse.quote(token),
    )

    ensure_qrcode()
    import qrcode
    from qrcode.image.svg import SvgImage

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)

    # 1) Terminal QR (scan straight from the terminal on a light background).
    print("\n  Pocket 配對 — 用手機掃描(設定 → 連線桌面 → 掃描 QR 配對)\n")
    qr.print_ascii(invert=True)

    # 2) Also open a crisp SVG in the browser/Preview — more reliable to scan.
    try:
        svg = qr.make_image(image_factory=SvgImage)
        path = os.path.join(tempfile.gettempdir(), "pocket-pair.svg")
        svg.save(path)
        subprocess.run(["open", path], check=False)
        print("\n  (也已在瀏覽器開啟大張 QR:%s)" % path)
    except Exception:
        pass

    print("  桌面 = 金庫+執行,手機 = 遙控。CC/Codex 登入都在桌面,手機零重登。")
    print("  連線:%s://%s(token 已編進 QR,未顯示)\n" % (SCHEME, HOST))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
