from unittest.mock import MagicMock

import pytest

import windows_mcp.__main__ as main_module


class TestModeDispatch:
    def test_remote_mode_does_not_build_local_server(self, monkeypatch):
        build_local = MagicMock(side_effect=AssertionError("local server should not be built"))
        auth_client = MagicMock()
        proxy_server = MagicMock()
        fake_transport = MagicMock()
        fake_proxy_client = MagicMock()

        monkeypatch.setattr(main_module, "_build_local_mcp", build_local)
        monkeypatch.setattr(main_module, "AuthClient", MagicMock(return_value=auth_client))
        monkeypatch.setattr(
            main_module,
            "_get_remote_proxy_types",
            MagicMock(return_value=(fake_transport, fake_proxy_client)),
        )
        monkeypatch.setattr(main_module.FastMCP, "as_proxy", MagicMock(return_value=proxy_server))

        config = main_module.Config(mode="remote", sandbox_id="sb-123", api_key="key-123")
        main_module._run_remote_mode(
            config=config,
            transport=main_module.Transport.STDIO.value,
            host="localhost",
            port=8000,
        )

        build_local.assert_not_called()
        auth_client.authenticate.assert_called_once()
        fake_transport.assert_called_once_with(url=auth_client.proxy_url, headers=auth_client.proxy_headers)
        fake_proxy_client.assert_called_once()
        proxy_server.run.assert_called_once_with(transport="stdio", show_banner=False)

    def test_remote_mode_requires_credentials(self):
        with pytest.raises(ValueError, match="SANDBOX_ID is required"):
            main_module._run_remote_mode(
                config=main_module.Config(mode="remote", sandbox_id="", api_key="key-123"),
                transport=main_module.Transport.STDIO.value,
                host="localhost",
                port=8000,
            )

        with pytest.raises(ValueError, match="API_KEY is required"):
            main_module._run_remote_mode(
                config=main_module.Config(mode="remote", sandbox_id="sb-123", api_key=""),
                transport=main_module.Transport.STDIO.value,
                host="localhost",
                port=8000,
            )
