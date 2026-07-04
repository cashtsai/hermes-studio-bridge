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
import json
import plistlib
import tempfile
import subprocess
import urllib.parse
import urllib.request

PLIST = os.path.expanduser("~/Library/LaunchAgents/ai.studio.hermes-bridge.plist")
# Public Tailscale Funnel surface (works on any network, no Tailscale on phone).
HOST = os.environ.get("POCKET_FUNNEL_HOST", "cashcamp-1.tail905550.ts.net")
SCHEME = os.environ.get("POCKET_FUNNEL_SCHEME", "https")
BRIDGE_PORT = int(os.environ.get("POCKET_BRIDGE_PORT", "8081"))


def _lan_host() -> str | None:
    """這台 Mac 的私網 IP(en0 優先)——在家同網段直連用,零繞路。"""
    for dev in ("en0", "en1"):
        try:
            out = subprocess.run(["ipconfig", "getifaddr", dev],
                                 capture_output=True, text=True, timeout=3)
            ip = out.stdout.strip()
            if ip:
                return ip
        except Exception:
            continue
    return None


def _tailscale_host() -> str | None:
    """Tailscale MagicDNS 名(私有 tailnet,外出但兩端都在 tailnet 時最短路)。
    與 Funnel host 相同就不重複列。"""
    for ts in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale", "tailscale"):
        try:
            out = subprocess.run([ts, "status", "--json"],
                                 capture_output=True, text=True, timeout=3)
            if out.returncode != 0:
                continue
            dns = (json.loads(out.stdout).get("Self") or {}).get("DNSName") or ""
            dns = dns.rstrip(".")
            if dns and dns != HOST:
                return dns
        except Exception:
            continue
    return None


def candidate_hosts() -> list:
    """QR payload v2:依優先序的連線候選(自動選路名單動態化的來源)。
    1) 在家私網直連 2) tailnet MagicDNS 3) 公網 Funnel(保底,恆在)。"""
    hosts = []
    lan = _lan_host()
    if lan:
        hosts.append("http://%s:%d" % (lan, BRIDGE_PORT))
    ts = _tailscale_host()
    if ts:
        hosts.append("https://%s" % ts)
    hosts.append("%s://%s" % (SCHEME, HOST))
    return hosts


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


def mint_code(token: str) -> str | None:
    """Ask the LOCAL bridge for a one-time pairing code (master token → code).
    Returns None if the bridge isn't reachable or lacks /pair/new."""
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8081/pair/new", method="POST",
            headers={"Authorization": "Bearer %s" % token},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            return (json.load(r) or {}).get("code")
    except Exception:
        return None


def main() -> int:
    token = read_token().strip()
    if not token or token.lower().startswith("change-me"):
        print("✗ 找不到有效的 BRIDGE_TOKEN(檢查 %s)。" % PLIST, file=sys.stderr)
        return 1

    # QR payload v2:`hosts=` 帶依優先序的候選清單(app 自動選路名單動態化
    # 的來源);v1 的 scheme/host 鍵保留 → 舊 app 掃 v2 QR 照樣可配對。
    hosts = candidate_hosts()
    hosts_q = urllib.parse.quote(",".join(hosts))

    # Prefer a one-time pairing code (the master token never leaves this Mac).
    code = mint_code(token)
    if code:
        payload = "pocket://pair?scheme=%s&host=%s&hosts=%s&code=%s" % (
            urllib.parse.quote(SCHEME), urllib.parse.quote(HOST), hosts_q,
            urllib.parse.quote(code))
        note = "一次性配對碼(5 分鐘有效),master token 不外流。"
    else:
        # Fallback: bridge not running / older bridge → embed the token directly.
        payload = "pocket://pair?scheme=%s&host=%s&hosts=%s&token=%s" % (
            urllib.parse.quote(SCHEME), urllib.parse.quote(HOST), hosts_q,
            urllib.parse.quote(token))
        note = "⚠ bridge 未回應,改用直接 token(請確認 bridge 執行中以取得一次性碼)。"
    url = payload

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

    print("  %s" % note)
    print("  桌面 = 金庫+執行,手機 = 遙控。CC/Codex 登入都在桌面,手機零重登。")
    print("  連線候選(依優先序):")
    for h in hosts:
        print("    · %s" % h)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
