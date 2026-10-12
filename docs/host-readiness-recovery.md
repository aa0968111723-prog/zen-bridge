# 主持 App 連線與翻譯恢復

0.5.4 修正主持機稍晚完成配對時，收音按鈕仍停用的啟動時序問題。主工作台會自動檢查服務連線；狀態更新不重新載入社課或字幕。視窗隱藏時暫停請求，重新回到視窗或恢復網路時檢查。

Windows App 可使用已安裝的電腦版 Hermes subscription proxy，向 xAI 發送無工具的文字翻譯請求。修正先前 `tool_choice:none` 未帶 tools 導致的 HTTP 400。這個連線保留在電腦上，網站經已配對的桌面代理呼叫它。

App 開啟時，若官方 `%LOCALAPPDATA%\hermes\bin\hermes.cmd` 存在且 127.0.0.1:8645 尚無服務，就啟動獨立 proxy；App 關閉時只停止自己啟動的程序樹。既有 proxy、Hermes gateway、Telegram、Bot 排程皆不重啟。既有 OAuth 由 Hermes 自己管理，不複製登入憑證。

自訂 `ZEN_HERMES_URL` 不會自動啟動服務。需要自行管理預設埠時，在 App `.env` 設 `ZEN_HERMES_PROXY_AUTOSTART=0`。未安裝或未登入 Hermes 不會阻止本機收音；雲端翻譯仍會明確失敗，不能把健康檢查的 HTTP 200 當作推論成功。

在「代理操作台」選擇電腦本機模型、Hermes 雲端連線或混合模式。不要把雲端網站上的 loopback URL 當成已連接這台電腦；桌面翻譯模式需要 App 保持開啟且完成配對。

自動檢查不開啟麥克風、不建立測試課程，也不更動已有的人工校對字幕。
