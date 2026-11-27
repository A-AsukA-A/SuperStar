# SuperStar

超星学习通轻量化控制台（低内存、按课程划分自建 SQLite 题库、支持 `systemd` 原生后台常驻与 Docker）。

## 核心功能

- **扫码一体选课 & 多账号切换**：新建任务时直接展示登录二维码，扫码确认后自动消码并拉取课程，支持多账号 Cookie 独立切换与单独更新登录。
- **预检未完成章节**：开课前自动跳过已完成章节，仅调度未完成章节并计算初始百分比；倍速上限 `1.0~2.0`、并发上限 `1~4`（默认 `4`）。
- **按课程划分的自建题库**：本地 SQLite（WAL 模式，亚毫秒检索）优先查库，命中则零延迟直接作答并跳过 AI 请求；未命中再调用 AI API 作答并自动按课程入库，支持在「自建题库」页查看、搜索、改答案与清理。
- **极简低内存架构**：直连 `requests` 调用 OpenAI/DeepSeek 接口（已移除 `openai`/`httpx` SDK），字库与 `fontTools` 按需懒加载。
- **低开销实时监控（SSE）**：后台 2 秒采样 CPU / 内存并通过 SSE 实时推送任务百分比、当前课程/章节/任务点及关键日志（默认过滤 `TRACE` 心跳，可选开启）。

## VPS 部署方案 A：宿主机 venv + systemd（最省内存，推荐 1G 以下小鸡）

```bash
git clone https://github.com/A-AsukA-A/SuperStar.git /opt/superstar
cd /opt/superstar

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cat > .env << 'EOF'
WEB_USERNAME=july
WEB_PASSWORD=请改成你的强密码
WEB_PORT=5000
EOF

cp deploy/superstar.service /etc/systemd/system/superstar.service
systemctl daemon-reload
systemctl enable --now superstar
```

## VPS 部署方案 B：单容器 Docker 部署

```bash
export WEB_PASSWORD="你的强密码"
docker compose up -d --build
```

## 本地运行（Windows / macOS / Linux）

```powershell
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe run_web.py
```

访问 `http://127.0.0.1:5000`。

## 安全说明

`cookies.txt`、`config.ini`、`data/`（含 API Key、多账号会话与自建题库 `question_bank.sqlite3`）、`instances/`、`.env`、`chaoxing.log` 均已加入 `.gitignore` 与 `.dockerignore`。
