"""验证真实服务入口不会把 Google OAuth 回调查询串交给通用访问日志。"""

from types import SimpleNamespace

import uvicorn

import lyra.main as main


def test_server_entry_disables_access_logs_with_oauth_query_parameters(monkeypatch) -> None:
    """运行期错误日志仍保留，但 HTTP 访问日志不能记录一次性授权码与 state。"""
    settings = SimpleNamespace(
        lyra_database_path=":memory:", lyra_host="127.0.0.1", lyra_port=3141,
    )
    closed = []
    database = SimpleNamespace(close=lambda: closed.append(True))
    application = object()
    calls = []
    monkeypatch.setattr(main, "Settings", lambda: settings)
    monkeypatch.setattr(main, "open_lyra_database", lambda _: database)
    monkeypatch.setattr(main, "create_app", lambda *_: application)
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))

    main.run()

    assert calls == [(application, {
        "host": "127.0.0.1", "port": 3141, "log_level": "info", "access_log": False,
    })]
    assert closed == [True]
