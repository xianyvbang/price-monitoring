# 余额监控

一个用于批量查询 `newApi` 和 `sub2Api` 余额的 Web 项目。后端使用 FastAPI + SQLite，前端使用 Vue 3 + Element Plus，支持管理员登录、自动查询、低余额邮件预警和 Docker Compose 部署。

## 快速启动

```bash
cp .env.example .env
# 修改 .env 中的 ADMIN_PASSWORD 和 APP_SECRET_KEY
# 可选：调整 SESSION_MAX_AGE_SECONDS 控制登录保持时间
docker compose up -d --build

# 重新启动
docker compose up -d --force-recreate
```

访问 `http://localhost:8000`，默认账号来自 `.env`。

`APP_SECRET_KEY` 不能继续使用默认值，否则 session 和加密数据的安全性都会下降。

首次创建 SQLite 数据库时，系统会写入一组默认账号骨架；这些默认账号只包含平台、名称和 Base URL，不包含 API Key、accessToken、SMTP 授权码或其他密钥。

## 本地开发

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

前端开发：

```bash
cd frontend
npm install
npm run dev
```

生产构建前端：

```bash
cd frontend
npm run build
```

构建产物会写入 `app/frontend`，FastAPI 会托管这个 Vue 单页应用。Docker 镜像直接使用仓库中的 `app/frontend` 构建产物；如果修改了 `frontend` 源码，请先运行 `npm run build` 再重新构建镜像。

## 批量导入格式

支持 JSON 数组，也支持逐行 CSV。

`sub2Api` CSV：

```text
name,baseUrl,apiKey,threshold
```

如果要手动查询账号分组倍率，也可以补充登录邮箱和登录密码：

```text
name,baseUrl,email,password,apiKey,threshold
```

`groupId` 不是必填项。只想筛选展示某个分组时再使用：

```text
name,baseUrl,groupId,email,password,apiKey,threshold
```

`newApi` CSV：

```text
name,baseUrl,accessToken,userId,threshold
```

JSON 示例：

```json
[
  {
    "name": "main",
    "base_url": "https://example.com",
    "key_id": "group-id",
    "email": "user@example.com",
    "password": "login-password",
    "api_key": "sk-xxx",
    "access_token": "token",
    "user_id": "1",
    "threshold": 5
  }
]
```

`sub2Api` 账号自动查询时使用 `GET {baseUrl}/v1/usage`，并使用 `apiKey` 作为 `Authorization: Bearer ...`。手动点击“查组”时会先用当前 `apiKey` 查询激活分组，再用 `email/password` 登录 `POST {baseUrl}/api/v1/auth/login` 获取 JWT，调用 `GET {baseUrl}/api/v1/groups/available` 和 `GET {baseUrl}/api/v1/groups/rates` 获取该激活分组的名称和倍率。登录 JWT 会按过期时间缓存在服务进程内，未过期时复用；如果分组接口请求失败，会清除缓存并重新登录后重试一次。倍率优先显示 `user_rate_multiplier`，为空时显示 `default_rate_multiplier`。

如果 `sub2Api` 站点开启了 Turnstile 人机验证（例如 `https://2chat.cc`），服务端无法只用 `email/password` 自动登录，登录接口可能返回 `400`。这时可以在账号里填写网页登录后的 `auth_token` 到 `accessToken`，并可选填写 `refresh_token` 到 `refreshToken`；查组会优先使用 `refreshToken` 刷新 `accessToken`，并在 `accessToken` 失效前自动刷新。JSON 批量导入时字段名为 `access_token` / `accessToken` 和 `refresh_token` / `refreshToken`。

平台配置页面提供“下载账号导入插件”按钮，可下载独立的 `NewAPI/Sub2API Account Grabber` Chrome/Edge 扩展。它与 OpenCode Go 插件分开，源码模板位于 `extension/account-grabber`。安装后，扩展会在识别到 newApi 或 sub2Api 类网站时显示右侧悬浮按钮；悬浮窗可自动填入名称、Base URL、备注 `1:1`、预警阈值 `5`、仪表盘显示和自动查询开关，并从页面请求或浏览器存储中抓取 newApi 的 `accessToken/userId`、sub2Api 的第一个 `apiKey` 与已登录 `accessToken/refreshToken`。推送到 app 时会按平台和 Base URL 判重：Base URL 相同则更新账号，不同则新增账号；保存后不会立即触发余额或分组查询。

## 平台调度

“平台调度”页面使用 SQLite 中最后一次成功同步的账号集合。手动同步按 Sub2API 账号 ID 增量合并：已有账号保留历史字段，只补充新发现的分组归属；新账号加入集合，本次未返回的历史账号不会删除。每个已知分组都可单独同步，全部同步仍可按平台、类型、状态及是否包含未分组账号筛选本次拉取范围。远端请求失败时保留旧集合；账号手动启停会立即写入 Sub2API 并更新本地快照。

自动评分默认开启；关闭后，后台不再自动读取证据、执行探活或重算健康分，但手动证据刷新和立即执行仍可使用。自动调度总开关和健康回池、智能扩容、负载因子、价格保护四项策略默认关闭；开启自动调度会同时开启自动评分，关闭自动评分会同时关闭自动调度。仅开启自动评分时，后台会增量读取请求证据、按最近一次探活时间判断到期探活并计算健康评分，但不写远端；开启自动调度后，异常账号停用和至少保留 1 个可用账号始终生效，即使四项子策略均关闭。调度器默认每 60 秒运行一轮，可在页面的规则参数中调整（最短 5 秒）；健康异常策略每轮每个分组最多自动关闭 1 个账号的调度，所有分组合计最多自动开启 1 个。多分组账号关闭后会占用其所属全部分组的当轮关闭名额。账号健康证据每个账号保留最近 60 条，探活证据有效期默认 180 秒，账号状态、目标并发、目标负载因子和操作审计均保存在 SQLite。

页面可手动重新获取短期和长期健康证据：系统会重新拉取参与自动调度账号的最近使用/错误历史、逐个执行探活并重算健康分。账号标题旁保留最近 15 条探活结果的迷你时间轴；账号下方展示参与短期分计算的综合最新 10 条证据中的使用成功和错误记录。健康计算仍只使用按时间排序的综合最近 60 条证据。规则参数可设置默认探活模型，分组标题和账号卡片可分别设置覆盖模型；探活按“账号模型、分组模型、默认模型”依次回退。账号属于多个已设置模型的分组时，使用分组 ID 最小的配置；全部留空时由 Sub2API 选择模型。账号卡片还可直接排除账号，被排除账号不会参与自动评分或调度，也不会被删除或远端停用；可在页面的“已排除账号”区域恢复。

每个正式分组可独立关闭“参与自动调度”，设置按 Sub2API 站点隔离。关闭后分组和账号仍显示，历史证据与最后健康分继续保留，但该分组的独占账号不会参与定时自动轮次、手动立即执行或批量健康证据刷新，也不会被自动启停或调整并发和负载；单账号手动探活和人工启停仍可使用。多分组账号只要属于一个仍开启的分组就继续参与，策略统计只计入开启分组；未分组账号不受此开关影响。

每个调度账号可绑定一个余额监控分组，绑定按 Sub2API 站点隔离。系统只使用余额监控最近取得的有效分组倍率计算上游成本，不在调度轮次中额外登录上游：`上游成本倍率 = 有效分组倍率 × 充值实付金额 ÷ 充值到账金额`。最后有效倍率可沿用两个“自动查询分组倍率”周期；超过宽限期后成本视为未知。原 Sub2API 账号返回的 `rate_multiplier` 不再展示，也不参与平台调度。

负载因子和智能扩容按 `健康分 ÷ 上游成本倍率 ^ 成本权重指数` 分配，所以上游成本越低，取得的负载和扩容量越多。未绑定、成本缺失或成本过期的账号保持现有负载因子与并发，不参与成本调权，但仍可参与健康评分、最低账号保障和正常请求调度。

价格保护使用账号所属本地平台分组中的最小正数倍率作为销售倍率，最低安全销售倍率为 `上游成本倍率 × (1 + 最低利润率 / 100)`，最低利润率默认 10%。低于安全线的全部账号会在同一轮立即关闭；成本超过宽限期时只标记倍率过期并停止成本调权，不会关闭调度。未绑定或本地销售倍率未知时只告警，不推断为 1.0。价格重新安全且健康证据有效时，系统每轮最多恢复一个 active 账号，包括价格保护、健康策略或人工关闭的账号；远端 inactive/error 账号不会自动恢复。关闭价格保护开关不会主动恢复此前关闭的账号。

## OpenCode Go

页面顶部的 `OpenCode Go` 用于监控 OpenCode Go 订阅的 5h、7d、30d 用量和 API key。新增账号时填写 Google 邮箱和密码用于账号归档；服务端不会启动内置浏览器，也不会自动操作 Google 登录。

OpenCode Go 的自动刷新沿用通用查询间隔和暂停开关。刷新时服务端使用已导入并加密保存的 OpenCode 登录态调用 OpenCode 前端 `_server` 接口。页面里的“配置 OpenCode Go JS 文件”可以分别配置用量 JS 和 API key JS：用量 JS 会解析 `queryLiteSubscription_query`，请求使用 `X-Server-Instance: server-fn:3`；API key JS 默认是 `https://opencode.ai/_build/assets/index-PbCOrg8_.js`，会解析 `listKeys_query`，请求使用 `X-Server-Instance: server-fn:2`。两个请求都会把解析到的 server id 写入 `_server?id=...` 和 `X-Server-Id`。

当 5h、7d 或 30d 用量达到 `99%` 时，系统会通过 CPA management API 对同名 OpenAI provider 的全部 API key 执行连通性测试，任一 key 连通就保持 provider 可用；全部测试报错时才自动停用。30d 达到 `99%` 时可通过列表顶部开关改为删除 provider，该开关默认关闭。远端删除成功后本地账号保留并显示“已删除”，OpenCode 用量仍会继续刷新；再次导入 CPA 会重建并启用 provider，同时清除删除状态。

OpenCode Go 页面提供“下载浏览器插件”按钮，可下载按当前部署域名定制的 `OpenCode Go Grabber` Chrome/Edge 扩展。下载接口会把 app 的访问域名写入扩展 manifest 的 `content_scripts.matches`、`externally_connectable.matches` 和 `host_permissions`；如果换了部署域名，需要重新下载插件，不要直接加载仓库里的 `extension/opencode-go-grabber` 源码目录。

安装插件后，在 `opencode.ai` 登录并打开对应 workspace，插件会抓取当前 `workspace_id`、包含 `auth` 的登录态 Cookie，以及 `/keys` 页面返回的 API key 信息。推荐在 app 的“导入登录态”弹窗点击“从浏览器插件抓取”，插件会把 workspace 和 Cookie 自动填入表单；也可以在“添加 OpenCode Go 账号”的 Workspace ID 输入框点击“从插件抓取”。扩展弹窗里的“推送到 App”会创建或更新账号、导入登录态，并触发服务端刷新，API key 仍由 app 的刷新流程回填。

导入登录态时，在 OpenCode Go 页面点击“导入登录态”，再点击“打开本地浏览器登录页”。系统会在你当前设备的默认浏览器打开 OpenCode 登录页；登录完成后，从浏览器开发者工具或 Cookie 导出工具复制 OpenCode 的登录态，粘贴到弹窗中保存。

弹窗支持两种内容：

```json
{ "cookies": [{ "name": "session", "value": "...", "domain": ".opencode.ai", "path": "/" }], "origins": [] }
```

也可以直接粘贴浏览器请求里的 Cookie 头：

```text
Cookie: name=value; name2=value2
```

## 接口

- `GET /api/accounts`
- `GET /api/dashboard`
- `GET /api/settings`
- `GET /api/platform-dispatch`（只读取本地缓存）
- `GET /api/platform-dispatch/cost-source-options`
- `PUT /api/platform-dispatch/accounts/{id}/cost-binding`
- `DELETE /api/platform-dispatch/accounts/{id}/cost-binding`
- `POST /api/platform-dispatch/refresh`（手动拉取并覆盖缓存）
- `POST /api/platform-dispatch/accounts/{id}/enabled`
- `GET /api/platform-dispatch/policy`
- `PUT /api/platform-dispatch/policy`
- `POST /api/platform-dispatch/policy/run`
- `PUT /api/platform-dispatch/groups/{id}/auto-dispatch`
- `GET /api/platform-dispatch/actions`
- `POST /api/accounts`
- `POST /api/accounts/import-by-base-url`
- `POST /api/accounts/bulk`
- `POST /api/accounts/{id}/query`
- `DELETE /api/accounts/{id}`
- `POST /api/query-all`
- `POST /api/settings/general`
- `POST /api/settings/smtp`
- `POST /api/settings/smtp/test`
- `POST /api/settings/password`
- `GET /api/logs`
- `DELETE /api/logs`
- `GET /api/opencode-go/accounts`
- `GET /api/opencode-go/settings`
- `POST /api/opencode-go/settings`
- `POST /api/opencode-go/settings/cpa-auto-delete`
- `POST /api/opencode-go/accounts`
- `PUT /api/opencode-go/accounts/{id}`
- `DELETE /api/opencode-go/accounts/{id}`
- `POST /api/opencode-go/accounts/{id}/enabled`
- `POST /api/opencode-go/accounts/{id}/login`
- `POST /api/opencode-go/accounts/{id}/session`
- `POST /api/opencode-go/accounts/{id}/refresh`
- `POST /api/opencode-go/query-all`
- `GET /api/opencode-go/accounts/{id}/history`
- `GET /api/opencode-go/accounts/{id}/api-key`
- `GET /api/opencode-go-grabber/extension.zip`
- `GET /api/account-grabber/extension.zip`

所有接口都需要先通过 Web 登录获取 Cookie。
