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

Docker 容器启动时会默认检查 Playwright；会先执行 `pip install -r /app/requirements.txt`，如果仍缺少 Python 包会显式补装 `playwright==1.60.0`，再执行 `python -m playwright install --with-deps chromium` 自动补齐 Chromium 浏览器和运行依赖。如需关闭启动时检查，可设置环境变量 `PLAYWRIGHT_INSTALL_ON_START=0`。

`APP_SECRET_KEY` 不能继续使用默认值，否则 session 和加密数据的安全性都会下降。

首次创建 SQLite 数据库时，系统会写入一组默认账号骨架；这些默认账号只包含平台、名称和 Base URL，不包含 API Key、accessToken、SMTP 授权码或其他密钥。

## 本地开发

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
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

## OpenCode Go

页面顶部的 `OpenCode Go` 用于监控 OpenCode Go 订阅的 5h、7d、30d 用量和 API key。新增账号时填写 Google 邮箱和密码；服务端会用 Playwright 走 OpenCode 的 Google OAuth 登录，保存加密后的网页登录态，并在自动刷新时复用该登录态。

如果 Google 触发验证码、2FA 或风控，登录会失败并在页面显示错误，需要先人工处理账号验证。OpenCode Go 的自动刷新沿用通用查询间隔和暂停开关。

## 接口

- `GET /api/accounts`
- `GET /api/dashboard`
- `GET /api/settings`
- `POST /api/accounts`
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
- `POST /api/opencode-go/accounts`
- `PUT /api/opencode-go/accounts/{id}`
- `DELETE /api/opencode-go/accounts/{id}`
- `POST /api/opencode-go/accounts/{id}/enabled`
- `POST /api/opencode-go/accounts/{id}/login`
- `POST /api/opencode-go/accounts/{id}/refresh`
- `POST /api/opencode-go/query-all`
- `GET /api/opencode-go/accounts/{id}/history`
- `GET /api/opencode-go/accounts/{id}/api-key`

所有接口都需要先通过 Web 登录获取 Cookie。
