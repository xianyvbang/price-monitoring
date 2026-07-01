# OpenCode Go Grabber

一个 Chrome/Edge（Manifest V3）浏览器扩展，用于从 `https://opencode.ai` 抓取 OpenCode Go 账号所需的凭据，并一键推送到 [python-get-price](../../) app（本地或服务器部署均可）。

> **重要：不要直接加载本目录的源码**。源码里 manifest 用 `__APP_ORIGIN_MATCH__` 占位符代替 app 所在域名，加载会失败。请用 app 提供的「下载浏览器插件」按钮下载**已按你的部署域名烘焙好的** zip，再解包加载（见下）。

## 抓什么

1. **workspace_id** —— 当前 `opencode.ai/workspace/wrk_xxx` 页面 URL 中的 `wrk_<编码>`。
2. **登录态 Cookie** —— 发往 `https://opencode.ai/_server` 请求头里的 Cookie（必须包含名为 `auth` 的 cookie；该 cookie 是 HttpOnly，`document.cookie` 读不到，扩展用 `webRequest` + `chrome.cookies` API 读取）。
3. **API key** —— `/keys` 页加载时 `/_server` 返回的 `key.list` 响应里的可用 key（仅展示与复制，不推送：app 端 `/refresh` 会自己回填）。

## 安装（推荐：从已部署的 app 下载）

1. 部署并启动 app（本地 `uvicorn` 或 `docker compose up -d --build`）。
2. 登录 app，进入「OpenCode Go」页面，点右上角「**下载浏览器插件**」按钮 → 下载到 `opencode-go-grabber.zip`。
   - 该 zip 由 `GET /api/opencode-go-grabber/extension.zip` 在下载时按 `request.base_url` 生成：manifest 的 `content_scripts.matches` / `externally_connectable.matches` / `host_permissions` 已填入你的部署域名，选项页默认 app 地址也已填好。
3. 解压 zip，进入 `opencode-go-grabber/` 目录。
4. 打开 `chrome://extensions`（或 Edge 的 `edge://extensions`），开启「开发者模式」。
5. 「加载已解压的扩展程序」→ 选择该目录。
6. 选项页（如需）填管理员账号/密码，用于 app 会话失效时兜底重新登录。

> 若 app 在 `http://localhost:8000` 本地开发，下载的 manifest matches 即 `http://localhost:8000/*`；若部署在 `https://price.example.com`，即 `https://price.example.com/*`。一次下载只对当前域名有效，换域名需重新下载。

## 两种使用方式

### 方式 A：在 App 页面点按钮（推荐）

app 的「OpenCode Go」页面在「导入登录态」弹窗里有「**从浏览器插件抓取**」按钮，「添加账号」弹窗的 Workspace ID 输入框有「**从插件抓取**」按钮。点了之后 app 页面通过 `window.postMessage` 向扩展取值，扩展返回 workspace_id + Cookie，自动填入表单——无需打开插件弹窗。流程：

- 「导入登录态」弹窗 → 点「从浏览器插件抓取」→ workspace 与 Cookie 自动填好 → 点「导入」→（可选）刷新账号，api_key 自动回填。
- 「添加账号」弹窗 → Workspace ID 一栏点「从插件抓取」→ 填入 workspace，其余邮箱密码手填 → 保存后再到该账号「导入登录态」抓 Cookie。

> 此方式依赖扩展把 `app_bridge.js` 注入 app 所在源（下载 zip 时已按部署域名烘焙）。仓库源码默认占位 `__APP_ORIGIN_MATCH__`，必须先经 app 下载接口烘焙才能加载。

### 方式 B：点扩展图标弹窗

点「推送到 App」后，扩展依次调用 app 的接口：

1. `POST /api/opencode-go/accounts` —— 用表单里的邮箱/密码（+workspace_id）创建或更新账号；
2. `POST /api/opencode-go/accounts/{id}/session` —— 导入捕获的 Cookie 登录态；
3. `POST /api/opencode-go/accounts/{id}/refresh` —— 触发服务端刷新，自动回填 `api_key_enc`。

成功后 app 的 OpenCode Go 列表里就多出一条已配置好登录态、api_key 已自动刷出的账号。

> 方式 B 不依赖 `app_bridge.js`，只要选项页里的 App Base URL 与 host_permissions 覆盖到 app 域名即可。下载的 zip 已烘焙好，无需手改。

## 文件结构

| 文件 | 作用 |
|---|---|
| `manifest.json` | MV3 清单、权限声明（用 `__APP_ORIGIN_MATCH__` 占位，下载时烘焙） |
| `background.js` | service worker：`webRequest` 捕获 `/_server` 的 Cookie；调 app API；401 兜底登录 |
| `content.js` | 注入 opencode.ai：解析 workspace_id；`fetch`/XHR 猴补丁抓 `key.list` 响应里的 API key |
| `app_bridge.js` | 注入 app 页面：`window.postMessage` ↔ `chrome.runtime` 桥接（方式 A 用） |
| `options.html` / `options.js` | 选项页：app 地址、会话 cookie 名、管理员凭据（默认 app 地址下载时烘焙） |
| `popup.html` / `popup.js` | 弹窗：展示三值 + 邮箱密码表单 + 推送按钮 |

## 说明

- 扩展不直接写 app 的 `api_key` —— app 没有该写入接口，`api_key` 一律由 `/refresh` 在服务端跑 `key.list` 回填。
- Cookie 捕获优先用 `webRequest` 抓真实 `/_server` 请求头；5 分钟内无请求时退化用 `chrome.cookies.getAll` 拼 `opencode.ai` 域 cookie，两者对 app 等价。
- 管理员密码明文存在扩展 `chrome.storage.local`，仅用于 app 会话失效时兜底登录。
- 部署到服务器时，app 通过 `GET /api/opencode-go-grabber/extension.zip`（`require_user` 鉴权）按当前域名生成定制 zip；Dockerfile 已 `COPY extension`，镜像内自带模板。