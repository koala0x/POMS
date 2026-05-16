# 部署指南（Docker）

PomsAI 的 Docker 部署文档。覆盖前置依赖、首次部署、日常运维、故障排查。

## 1. 部署架构

```
┌─────────────────────────── Mac mini (宿主机) ───────────────────────────┐
│                                                                         │
│   ┌──────────────┐         ┌──────────────────┐                         │
│   │ Ollama       │ ◄────── │  pomsai 容器      │                         │
│   │ :11434       │         │  (Python worker) │                         │
│   │ 0.0.0.0 监听 │         │                  │                         │
│   └──────────────┘         └────────┬─────────┘                         │
│                                     │                                   │
│                          docker network: poms-net                       │
│                                     │                                   │
│                            ┌────────▼─────────┐                         │
│                            │ poms-postgres    │                         │
│                            │ (PG 容器)        │                         │
│                            └──────────────────┘                         │
└─────────────────────────────────────────────────────────────────────────┘
              │
              │  HTTPS 出站
              ▼
    ┌─────────────────┐  ┌─────────────────┐
    │  Telegram API   │  │  PushPlus API   │
    └─────────────────┘  └─────────────────┘
```

- **pomsai 容器**：本项目，常驻 worker 服务，无 HTTP 端口
- **poms-postgres 容器**：已存在，PG 数据库（同 docker network）
- **Ollama**：跑在宿主机上（不在容器里），通过 `host.docker.internal` 访问
- **Telegram / PushPlus**：外网推送服务

## 2. 前置依赖

部署前确认 Mac mini 上已就绪：

| 依赖 | 检查命令 | 备注 |
|------|---------|------|
| Docker | `docker --version` | Docker Desktop 或 OrbStack |
| Docker Compose | `docker compose version` | v2 内置在 Docker Desktop 里 |
| PG 容器 | `docker ps \| grep poms-postgres` | 必须在跑 |
| poms-net 网络 | `docker network ls \| grep poms-net` | PG 容器必须在这个网络里 |
| Ollama 服务 | `curl http://localhost:11434/api/tags` | 监听 0.0.0.0，不能只监听 127.0.0.1 |
| 业务库表 | 进 PG 检查 `\dt` 看到 normalized_messages 等表 | alembic 迁移已执行 |

### Ollama 监听 0.0.0.0 配置

容器要从 `host.docker.internal` 访问宿主机 Ollama，必须监听 0.0.0.0：

```bash
# Mac 上设置环境变量
launchctl setenv OLLAMA_HOST "0.0.0.0:11434"

# 重启 Ollama 应用（菜单栏图标 → Quit → 重新打开）
```

确认监听地址：

```bash
lsof -i :11434
# 应该看到 *:11434（LISTEN），而不是 127.0.0.1:11434
```

## 3. 首次部署

### 3.1 准备代码

把项目代码放到 Mac mini 任意目录，例如 `~/PomsAI/`：

```bash
git clone <repo-url> ~/PomsAI
cd ~/PomsAI
```

### 3.2 检查关键配置

确认以下默认值适合你的环境（不需要改 `.env`）：

| 文件 | 字段 | 默认值 | 说明 |
|------|------|--------|------|
| `config/_database.py` | `db_host` | `poms-postgres` | PG 容器名 |
| `config/_database.py` | `db_user/password/name` | `all_new/123qwe/all_new` | 业务库凭据 |
| `config/_llm.py` | `ollama_base_url` | `http://host.docker.internal:11434` | 宿主机 Ollama |
| `config/_llm.py` | `ollama_model_level5` | `qwen2.5:1.5b` | 简报模型 |
| `config/_alerts.py` | `telegram_bot_token` | (已硬编码) | Telegram 推送 |
| `config/_alerts.py` | `pushplus_token` | (已硬编码) | 微信推送 |

### 3.3 构建并启动

```bash
sudo docker compose up -d --build
```

参数说明：
- `up`：创建并启动容器
- `-d`：后台运行（detach）
- `--build`：构建镜像（首次必须，代码改动后也要带）

### 3.4 验证启动成功

**查看容器状态：**

```bash
sudo docker compose ps
```

应该看到 `pomsai` STATUS 为 `Up X seconds`。

**查看启动日志：**

```bash
sudo docker compose logs --tail 100
```

关键检查点（按顺序出现）：
- `数据库连接已初始化`
- `词典就绪：tickers=...`
- `SlidingCounter backfill 结束：ok=True`
- `HotnessService(1h) 启动`
- `AlertTriggerService(1h) 启动`
- `BriefingService 启动`
- `服务启动成功：worker 跑 N 个 service`

**实时跟踪日志：**

```bash
sudo docker compose logs -f
```

## 4. 日常运维

### 4.1 常用命令

```bash
# 查看运行状态
sudo docker compose ps

# 实时跟踪日志
sudo docker compose logs -f

# 查看最近 N 行日志
sudo docker compose logs --tail 200

# 重启服务（不重新 build）
sudo docker compose restart

# 停止服务
sudo docker compose down

# 代码更新后重新部署
sudo docker compose up -d --build
```

### 4.2 日志位置

容器挂载了宿主机的 `./logs/` 目录到容器内 `/app/logs/`：

- `logs/service.log` — 当天日志
- `logs/service.YYYY-MM-DD_HH-MM-SS_xxx.log` — 历史日志（按天滚动，保留 30 天）

直接查看宿主机日志文件：

```bash
tail -f logs/service.log
```

### 4.3 修改配置

所有配置在 `config/*.py` 文件里硬编码。修改后需要重新构建：

```bash
# 编辑配置
vim config/_alerts.py

# 重新部署
sudo docker compose up -d --build
```

### 4.4 查看 LLM 简报数据

进入 PG 容器：

```bash
sudo docker exec -it poms-postgres psql -U all_new -d all_new
```

查询最近的简报：

```sql
SELECT entity, narrative, catalyst, sentiment, confidence, window_end
FROM entity_briefings
ORDER BY window_end DESC
LIMIT 20;
```

## 5. 故障排查

### 5.1 容器启动失败

**症状：** `docker compose ps` STATUS 显示 `Restarting` 或 `Exited`

**排查：**

```bash
sudo docker compose logs --tail 200
```

常见原因：
- DB 连不上：检查 `poms-postgres` 是否在 `poms-net` 网络里
- 词典文件错误：检查 `dictionaries/*.yaml` YAML 格式
- 端口冲突：本项目无端口暴露，正常不会有

### 5.2 DB 连接失败

**症状：** 日志出现 `数据库连接异常` 或 `could not connect to server`

**检查：**

```bash
# 1. 确认 PG 容器在跑
sudo docker ps | grep poms-postgres

# 2. 确认两个容器在同一网络
sudo docker network inspect poms-net

# 应该能看到 pomsai 和 poms-postgres 两个容器

# 3. 测试容器间连通性
sudo docker exec pomsai ping poms-postgres
```

### 5.3 Ollama 连接失败

**症状：** 日志出现 `Ollama 调用失败` 或 `Connection refused`

**检查：**

```bash
# 1. 容器内能否访问宿主机
sudo docker exec pomsai curl -I http://host.docker.internal:11434

# 2. 宿主机 Ollama 是否监听 0.0.0.0
lsof -i :11434

# 3. 模型是否存在
curl http://localhost:11434/api/tags | grep qwen2.5
```

如果 Ollama 只监听 127.0.0.1，按"前置依赖"章节重新配置 `OLLAMA_HOST=0.0.0.0:11434` 并重启 Ollama。

### 5.4 Telegram / 微信推送不到

**症状：** 日志显示告警生成但没收到消息

**检查：**

```bash
# 1. 容器能否访问外网
sudo docker exec pomsai curl -I https://api.telegram.org

# 2. token 配置是否正确（看启动日志）
sudo docker compose logs | grep "AlertTriggerService"

# 3. PushPlus 配额（免费版 200/天）
# 登录 https://www.pushplus.plus 看用量
```

国内网络环境下 Telegram API 可能被墙，需要走代理或在能访问的服务器上跑。

### 5.5 LLM 简报质量差

**症状：** entity_briefings 表里 narrative / catalyst 大量为 NULL

**原因：** 当前用的 `qwen2.5:1.5b` 是小模型，对加密叙事理解有限。

**升级模型：**

```bash
# 1. Mac mini 上拉取更大模型
ollama pull qwen2.5:3b

# 2. 修改 config/_llm.py
# ollama_model_level5: str = "qwen2.5:3b"

# 3. 重新部署
sudo docker compose up -d --build
```

### 5.6 架构警告（Mac Apple Silicon）

**症状：** Docker Desktop 显示 "AMD64 Image may have poor performance" 警告

**解决：** 删掉 `docker-compose.yml` 里的 `platform: linux/amd64`（如果有），重新 build：

```bash
sudo docker compose up -d --build
```

镜像会构建为原生 ARM64，性能更好。

## 6. 卸载 / 清理

完全停止并清理（保留日志和数据库）：

```bash
sudo docker compose down
```

同时删除镜像：

```bash
sudo docker compose down --rmi local
```

清理日志（在项目目录执行）：

```bash
rm -rf logs/*.log
```

数据库数据（`entity_briefings` / `hotness_snapshots` 等表）独立于容器存在，PG 容器不删除即可保留。

## 7. 附录

### 7.1 文件清单

| 文件 | 作用 |
|------|------|
| `Dockerfile` | 构建 Python 运行镜像 |
| `docker-compose.yml` | 容器编排配置 |
| `.dockerignore` | 排除不需要打包进镜像的文件 |
| `requirements.txt` | Python 依赖 |
| `main.py` | 服务入口 |
| `config/*.py` | 全部配置（硬编码） |

### 7.2 环境变量（可选）

虽然默认配置已硬编码，但部分参数支持环境变量覆盖：

| 变量 | 默认值 | 用途 |
|------|--------|------|
| `DB_HOST` | `poms-postgres` | DB 主机（容器名或 IP） |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama 地址 |

需要覆盖时在项目根目录创建 `.env` 文件，并在 `docker-compose.yml` 的 `app` 服务下加 `env_file: - .env`。
