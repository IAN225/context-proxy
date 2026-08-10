#!/usr/bin/env python3
"""context-proxy 入口。

    python proxy.py            # 直接跑
    uvicorn proxy:app          # 或者交给外部 ASGI 服务器
"""

from cproxy import app as _app_module

_app_module.bootstrap()
app = _app_module.app


def main() -> None:
    import uvicorn

    from cproxy import config

    _app_module.install_sighup()
    srv = config.cfg().get("server", {})
    uvicorn.run(app, host=srv.get("host", "0.0.0.0"), port=int(srv.get("port", 8787)),
                log_config=None, access_log=True)


if __name__ == "__main__":
    main()
