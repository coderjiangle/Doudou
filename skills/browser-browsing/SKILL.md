---
name: browser-browsing
description: 使用 Google Chrome 上网、搜索信息与控制音视频播放（播放/暂停/调音量/全屏/关闭标签页）时加载。
---

# Chrome 浏览器上网与影音控制指南

## 通用浏览流程

1. 先用 `browser_open(url)` 打开目标网站；若已知搜索地址可直接拼 URL 打开，省去先找首页搜索框的步骤。
2. 用 `browser_read_page(filter_text=...)` 查看页面标题、播放器状态与带 `[编号]` 的可交互元素：
   - 页面内容很多时（如视频站、电商、资讯首页），优先传 `filter_text` 过滤关键词（例如 `"总决赛"`、`"搜索"`），减少干扰。
3. 使用 `browser_type(target, text, submit=True)` 在搜索框输入并回车，或 `browser_click(target)` 点击元素编号。
   - 若点击后弹出新标签页，工具会自动切换焦点到新页面；不确定当前在哪个标签时调用 `browser_tabs()` 查看并切换。

## 音视频控制（B 站 / YouTube / 各类网页播放器通用）

- **播放 / 暂停 / 静音 / 调音量 / 进度 / 倍速 / 全屏**：优先直接调用 `browser_media_control(action, value)`，不需要去页面里找按钮或发空格键。
  - `action="pause"` 暂停
  - `action="play"` 继续播放
  - `action="unmute"` 取消静音、`action="set_volume", value=0.5` 调到 50% 音量
  - `action="forward"` / `action="backward"` 快进/快退（默认 15 秒，可传 `value`）
  - `action="fullscreen"` / `action="exit_fullscreen"` 切换**视频播放器**全屏
- **浏览器窗口最大化 / 还原 / F11 全屏**：
  - 用户说「把浏览器最大化 / 放大 / 右上角全屏」时，调用 `browser_window(state="maximized")`（保留标签栏、地址栏和系统任务栏）；还原窗口用 `browser_window(state="normal")`。
  - 仅在用户明确要求「F11 全屏 / 隐藏地址栏覆盖整个屏幕」时，才用 `browser_window(state="fullscreen")`。
- **退出视频 / 关掉页面**：调用 `browser_close_tab()` 关闭当前标签页，或先用 `browser_tabs()` 确认编号后再关闭对应标签页。

## 常见视频站快捷技巧

- **哔哩哔哩（Bilibili）**：
  - 搜索直达：`https://search.bilibili.com/all?keyword=<URL编码后的关键词>`
  - 打开搜索结果页后，用 `browser_read_page(filter_text="...")` 列出视频标题链接，再用 `browser_click(编号)` 打开播放（新标签页会自动接管焦点）。
  - 视频打开后调用 `browser_media_control(action="status")` 确认已在播放；若处于暂停或静音状态，补调 `play` / `unmute`。
- **YouTube**：
  - 搜索直达：`https://www.youtube.com/results?search_query=<关键词>`
