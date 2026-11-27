# -*- coding: utf-8 -*-
"""SuperStar 本地 / Docker Web 控制面板启动入口。"""

import os

from web.server import app


def main() -> None:
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_PORT", "5000"))
    debug = os.environ.get("WEB_DEBUG", "false").lower() in {"1", "true", "yes"}
    auth_mode = "已开启 WEB_PASSWORD 访问鉴权" if os.environ.get("WEB_PASSWORD") else "未设置 WEB_PASSWORD（仅建议本地访问）"
    print(f"SuperStar Web 控制台已启动: http://{host}:{port} ({auth_mode})")
    app.run(host=host, port=port, debug=debug, threaded=True)


if __name__ == "__main__":
    main()
