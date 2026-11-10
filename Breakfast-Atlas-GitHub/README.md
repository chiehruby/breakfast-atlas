# Breakfast Atlas｜早餐競合研究

可直接放到 GitHub Pages 的靜態網站，無須安裝套件或設定 API 金鑰。
包含最新版第一、第四部分、How this AI agent works 標題及 PESTEL 修正。

## 上傳 GitHub 並發布

1. 建立 GitHub repository（免費方案可使用公開 repository）。
2. 將本資料夾裡的所有檔案上傳到 repository 根目錄；確保首頁是根目錄的 `index.html`，不要多包一層資料夾。
3. 開啟 repository 的 Settings → Pages。
4. Source 選 Deploy from a branch；Branch 選 main，資料夾選 /(root)，按 Save。
5. 等待發布完成，Pages 設定頁會顯示可分享的網站網址。

官方說明：https://docs.github.com/en/pages/getting-started-with-github-pages/configuring-a-publishing-source-for-your-github-pages-site

## 檔案用途

- `index.html`：網站入口，載入 app.js 和 style.css。
- `app.js`：網站互動、導覽與已整合的章節內容。
- `style.css`：顏色、字體、排版及手機版樣式。
- `visual-chapters.js`：六章內容的編輯來源。
- `build-visual.py`：將章節來源同步到 app.js，並產生完整單檔 HTML。
- `Breakfast Atlas.html`：可離線開啟的單檔版本。
- `.nojekyll`：讓 GitHub Pages 直接提供靜態檔案。

## 修改內容

在 `visual-chapters.js` 修改對應函式：

| 章節 | 函式 |
| --- | --- |
| 第一部分 | backgroundVisual() |
| 第二部分 | firmVisual() |
| 第三部分 | sourcesVisual() |
| 第四部分 | agentVisual() |
| 第五部分 | outputPreview() |
| 第六部分 | conclusionsVisual() |

修改章節後，在本資料夾執行（需要 Python）：

```powershell
python build-visual.py
```

再把變更後的檔案上傳 GitHub，Pages 會依發布分支更新。
只要發布現有網站，不需要執行 Python。

## 研究原型範圍

門市點位為虛構互動示意，非真實門市研究資料。
自動擷取、每週排程、AI API 與自由問答均為提案規劃，目前未串接。
