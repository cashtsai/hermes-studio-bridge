# Console × Shan Design(Studio Design System v0.2)對賬

> 2026-11-13。對象:static/console/style.css(498 行,26 個變數)vs design.shan.house。
> SDS 核心主張:「一套底層,品牌只換外觀不換結構」—— 所以 Pocket 紅可以留,**結構與紀律要照規範**。

## 一、修正清單(違反規範,該改)

| # | 問題 | 現狀 | SDS 規範 | 嚴重度 |
|---|------|------|---------|--------|
| 1 | **CJK 行高過低** | body 14px/1.5 | 中文正文最低 1.7(建議 1.75–1.8),標題最低 1.15 | 高(可讀性) |
| 2 | **正文字級偏小** | 14px | text-body 16px;caption 才 13px | 高 |
| 3 | **accent 用量違紀** | Pocket Red 用於 user bubble(大面積) | 「強調色只用於操作/選取/焦點,禁大面積」 | 高 |
| 4 | **faint 文字對比不足** | #B3AEA4 on #FFF2D6 ≈ 2:1 | 次要文字 ≥4.5:1、輔助 ≥3:1 | 高(無障礙) |
| 5 | **狀態色不對位** | 自家 good/warn/bad | 狀態色跨品牌固定:success #1d7a4a、danger #b42318、info #1f6aa1、warning oklch(48% .1 85) | 中 |
| 6 | **surface 只有兩層半** | bg/panel/panel-2 混用 | 三層語意:surface(頁底)/raised(卡片浮層)/sunken(側欄、代碼框) —— sidebar 現在用 panel(raised),按規範該用 sunken | 中 |
| 7 | **圓角不成系統** | sess 8px、st 4px、各處散落 | btn 9999(膠囊)/field 10/dialog 20 三檔 | 中 |
| 8 | **無 focus ring** | icon-btn 等鍵盤焦點無樣式 | focus ring 用 accent,必備 | 中(可達性) |
| 9 | **字族未對位** | 系統字(-apple-system/PingFang) | Inter + Noto Sans TC + system-ui;mono 用 JetBrains Mono 優先 | 低(載字體有成本,可只改宣告鏈) |
| 10 | **動效未 token 化** | transition 散落各處 | --ease-standard cubic-bezier(.2,0,0,1) + .2s/.25s 兩檔 | 低 |
| 11 | **陰影缺體系** | 幾乎無陰影 | shadow-1(卡片)/shadow-2(浮層)兩級柔影 | 低 |
| 12 | hover/selected 不統一 | hover=panel-2、active=inset 線 | 統一 surface-hover / surface-selected 一組 token | 低 |

## 二、優化建議(超出對位,值得做)

1. **三層 token 制導入**:L1 palette → L2 alias(--color-*)→ 元件,照 SDS;現有 --bg/--panel 保留為 L2 別名過渡,不用全檔重寫
2. **與 app 主題 spec 合流**:E4 的 PocketThemeSpec(53-role JSON)已是跨端單一真相 —— 寫一個小生成器把同一份 JSON 吐成 console 的 CSS vars,三端(iOS/Android/web)從此一份主題。SDS 的「品牌只換外觀」正是這個架構
3. **訊息動作列/時間戳**用 caption 13px/1.6 規格統一(剛做的七件套動作列字級目前隨手寫)
4. **膠囊化主要按鈕**(送出、繼續):btn-radius 9999 是 SDS 最鮮明的簽名,一改整個氣質就對了
5. **pocket 主題底色靠向 SDS 暖紙**:#FFF2D6(偏黃)→ 可考慮 #FDF9F4 系(SDS surface),跟 design site 同紙感;或保留 App 奶油底不動 —— 這是品牌選擇,列出讓你拍
6. dark 主題灰階對位 SDS gray-850/900/950(現 #131317 族接近,微調即可)

## 三、建議執行切法
- **第一刀(高嚴重度 1–4)**:行高/字級/對比/accent 紀律 —— 純 CSS,半天,體感最大
- **第二刀(5–8)**:狀態色、三層 surface、圓角三檔、focus ring —— token 重整
- **第三刀(優化 1–2)**:token 三層制 + 與 PocketThemeSpec 合流(含生成器)
