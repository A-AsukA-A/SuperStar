# SuperStar

基于超星学习通核心刷课引擎的单容器 Web 控制台（适配 1C1G / 1C2G 小内存服务器）。

## 核心功能

- **扫码一体选课 & 多账号切换**：新建任务时直接展示登录二维码，扫码确认后自动消码并拉取课程，支持多账号 Cookie 独立切换与单独更新登录。
- **预检未完成章节**：开课前自动跳过已完成章节，按真实剩余章节调度并计算初始百分比；支持倍速（`1.0~2.0`）与并发（`1~4`，默认 `4`）安全上限。
- **AI 大模型答题**：仅保留 OpenAI 兼容接口（如 DeepSeek），支持在前端管理多条 API、连通性测试与密钥脱敏。
- **低开销实时监控**：后台 2 秒采样 CPU / 内存并通过 SSE 实时推送任务百分比、当前课程/章节/任务点及日志，按服务器硬件自动限制并发上限。

## 本地运行（Python 3.13+）

```powershell
uv venv --python 3.13 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe run_web.py
```

访问 `http://127.0.0.1:5000`。

## VPS 单容器后台部署

整个服务仅运行 **1 个 Docker 容器**，关闭 SSH 和浏览器后任务仍在服务器后台持续执行，随时通过浏览器访问控制面板。

```bash
git clone https://github.com/A-AsukA-A/SuperStar.git /opt/superstar
cd /opt/superstar

# 公网访问建议设置密码保护
export WEB_PASSWORD="你的强密码"
docker compose up -d --build
```

- 控制面板端口：`5000`（可通过 `WEB_PORT` 修改）。
- 持久化目录：`./data`（AI API 与多账号会话）和 `./instances`（各任务独立 `cookies.txt`、`config.ini` 与日志）。

## 关于后续超星接口维护

本项目的协议核心（[api/](file:///c:/Users/ruben/Desktop/文件/superstar/api) 与 [main.py](file:///c:/Users/ruben/Desktop/文件/superstar/main.py)）与 Web 控制台（[web/](file:///c:/Users/ruben/Desktop/文件/superstar/web)）完全解耦。若未来学习通接口调整，只需更新 `api/` 下的请求/解析文件即可，无需改动前端与调度逻辑。

## 安全说明

`cookies.txt`、`config.ini`、`data/`、`instances/`、`.env`、`cache.json`、`chaoxing.log` 均已加入 `.gitignore` 与 `.dockerignore`，推送代码前请确保工作区未包含个人凭证。
