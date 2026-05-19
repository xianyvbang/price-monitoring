# 余额监控

一个用于批量查询 `newApi` 和 `sub2Api` 余额的 Python Web 项目，支持 SQLite 持久化、管理员登录、自动查询、低余额邮件预警和 Docker Compose 部署。

## 快速启动

```bash
cp .env.example .env
# 修改 .env 中的 ADMIN_PASSWORD 和 APP_SECRET_KEY
docker compose up -d --build

# 重新启动
docker compose up -d --force-recreate
```

访问 `http://localhost:8000`，默认账号来自 `.env`。

首次创建 SQLite 数据库时，系统会写入一组默认账号骨架；这些默认账号只包含平台、名称和 Base URL，不包含 API Key、accessToken、SMTP 授权码或其他密钥。

## 本地开发

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

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

## 接口

- `GET /api/accounts`
- `POST /api/accounts`
- `POST /api/accounts/bulk`
- `POST /api/accounts/{id}/query`
- `POST /api/query-all`
- `POST /api/settings/general`
- `POST /api/settings/smtp`
- `POST /api/settings/smtp/test`

所有接口都需要先通过 Web 登录获取 Cookie。
