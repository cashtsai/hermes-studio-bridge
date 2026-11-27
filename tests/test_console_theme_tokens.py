"""Console L2 design tokens stay aligned with the shared PocketThemeSpec.

正本:pocket-cis repo `tokens/themes/{pocket,white,dark}.json`(53-role spec,
design.pocketone.app 體系;schema `tokens/pocket-theme.schema.json`)。
console 的 style.css L2 token 區塊是對齊該 spec 的鏡像 —— 這條測試把兩邊
逐 role 比對(含 border 的 alpha-壓平配方),飄移即紅,取代「手抄值沒人看守」。
pocket-cis 不在本機(CI、別台機器)時跳過,不擋套件;`POCKET_CIS_DIR` 可覆寫。
"""
import _isolation  # noqa: F401
import json
import os
import re
import unittest
from pathlib import Path

STYLE = Path(__file__).resolve().parent.parent / "static" / "console" / "style.css"
CIS = Path(os.environ.get("POCKET_CIS_DIR", str(Path.home() / "apps" / "pocket-cis")))
THEMES_DIR = CIS / "tokens" / "themes"

# console 變數 ← spec role。只列 console 真的在用、且兩邊語意一對一的 role:
#   · warning 在 console 用 oklch() 表示(SDS 狀態色),無法無損轉 hex → 不比;
#   · surface-sunken / surface-hover 是 console 自己的層次 token,53-role 裡
#     沒有一對一對應 → 不比;
#   · border 在 console 是 alpha 配方,spec 存的是壓平到 canvas 的實色
#     (tokens/README.md 的文件化配方)→ 壓平後比。
ROLE_TO_VAR = {
    "canvas": "--color-canvas",
    "surface": "--color-surface",
    "accent": "--color-accent",
    "accentForeground": "--color-accent-foreground",
    "accentSurface": "--color-accent-surface",
    "text": "--color-text",
    "textMuted": "--color-text-muted",
    "iconMuted": "--color-text-faint",
    "error": "--color-danger",
    "update": "--color-info",
    "border": "--color-border",
}
TOLERANCE = 2  # 每通道 ±2:alpha 壓平/十六進位捨入的誤差界


def _blocks(css):
    """selector → {var: value}。只掃檔頭 token 區(第一個元件選擇器之前)。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)  # 註解裡的 {} 會騙過塊解析
    head = css.split("*{box-sizing", 1)[0]
    out = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", head):
        selector = " ".join(m.group(1).split())
        decls = dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", m.group(2)))
        merged = out.setdefault(selector, {})
        merged.update({k: v.strip() for k, v in decls.items()})
    return out


def _theme_vars(blocks, theme):
    """該主題生效的變數表:L1(:root)+ 預設主題塊(帶裸 :root)+ 主題覆蓋。"""
    merged = {}
    for selector, decls in blocks.items():  # dict 保序 = 檔案順序 = cascade 順序
        if selector == ":root" or '[data-theme="pocket"]' in selector \
                or f'[data-theme="{theme}"]' in selector:
            merged.update(decls)
    return merged


_VAR_RE = re.compile(r"var\((--[\w-]+)\)")


def _resolve(value, vars_):
    for _ in range(10):
        m = _VAR_RE.fullmatch(value.strip())
        if not m:
            return value.strip()
        value = vars_[m.group(1)]
    raise AssertionError("var() 鏈太深:" + value)


def _hex_rgb(s):
    s = s.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _css_rgb(value, vars_, canvas_rgb):
    """hex / rgba(壓平到 canvas)→ (r,g,b)。"""
    value = _resolve(value, vars_)
    if value.startswith("#"):
        return _hex_rgb(value)
    m = re.fullmatch(
        r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", value)
    if m:
        a = float(m.group(4)) if m.group(4) else 1.0
        fg = tuple(int(m.group(i)) for i in (1, 2, 3))
        return tuple(round(c * a + bg * (1 - a)) for c, bg in zip(fg, canvas_rgb))
    raise AssertionError("看不懂的色值:" + value)


@unittest.skipUnless(THEMES_DIR.is_dir(),
                     "pocket-cis 不在本機(POCKET_CIS_DIR 可指定正本位置)")
class ConsoleThemeTokenTests(unittest.TestCase):
    def test_l2_tokens_match_shared_theme_spec(self):
        blocks = _blocks(STYLE.read_text(encoding="utf-8"))
        for theme in ("pocket", "white", "dark"):
            spec = json.loads((THEMES_DIR / f"{theme}.json").read_text(encoding="utf-8"))
            colors = spec["colors"]
            vars_ = _theme_vars(blocks, theme)
            canvas_rgb = _hex_rgb(colors["canvas"])
            for role, var in ROLE_TO_VAR.items():
                with self.subTest(theme=theme, role=role):
                    self.assertIn(var, vars_, f"{theme}: console 缺 {var}")
                    got = _css_rgb(vars_[var], vars_, canvas_rgb)
                    want = _hex_rgb(colors[role])
                    self.assertTrue(
                        all(abs(g - w) <= TOLERANCE for g, w in zip(got, want)),
                        f"{theme}.{role} 飄移:console {var}={vars_[var]!r} → "
                        f"rgb{got},spec={colors[role]}(容差 ±{TOLERANCE}/通道)")

    def test_spec_files_are_complete_53_roles(self):
        for theme in ("pocket", "white", "dark"):
            spec = json.loads((THEMES_DIR / f"{theme}.json").read_text(encoding="utf-8"))
            self.assertEqual(
                len(spec["colors"]), 53,
                f"{theme}.json 不是完整 53-role spec —— 正本結構變了,"
                "請同步更新本測試與 style.css 的同步說明")


if __name__ == "__main__":
    unittest.main()
