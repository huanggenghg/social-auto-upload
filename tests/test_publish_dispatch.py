import unittest
from unittest.mock import AsyncMock, patch

from publish.dispatch import (
    _PLATFORM_LOGIN,
    ensure_login,
    platform_requires_account_login,
)


class PlatformLoginRegistryTests(unittest.TestCase):
    def test_registry_covers_all_platforms(self):
        expected = {"douyin", "xiaohongshu", "kuaishou", "tencent", "baijiahao", "bilibili", "weibo", "tk"}
        self.assertEqual(set(_PLATFORM_LOGIN.keys()), expected)

    def test_registry_entries_are_three_tuples(self):
        for platform, entry in _PLATFORM_LOGIN.items():
            self.assertEqual(len(entry), 3, f"{platform} entry must be (module_path, check_name, setup_name)")
            module_path, check_name, setup_name = entry
            self.assertTrue(module_path.startswith("uploader."), f"{platform} module_path wrong: {module_path}")
            self.assertEqual(check_name, "cookie_auth", f"{platform} check_name should be cookie_auth")
            self.assertTrue(setup_name.endswith("_setup"), f"{platform} setup_name wrong: {setup_name}")

    def test_platform_requires_account_login(self):
        self.assertTrue(platform_requires_account_login("douyin"))
        self.assertTrue(platform_requires_account_login("weibo"))
        self.assertTrue(platform_requires_account_login("tk"))
        self.assertFalse(platform_requires_account_login("unknown_platform"))


class EnsureLoginTests(unittest.TestCase):
    def test_returns_false_for_unknown_platform(self):
        import asyncio
        result = asyncio.run(ensure_login("unknown", "cookies/x.json"))
        self.assertFalse(result)

    def test_triggers_setup_when_account_file_missing(self):
        import asyncio
        with patch("os.path.exists", return_value=False), \
             patch("importlib.import_module") as mock_import:
            mock_module = mock_import.return_value
            mock_module.douyin_setup = AsyncMock(return_value=True)
            result = asyncio.run(
                ensure_login("douyin", "cookies/douyin_uploader/account.json")
            )
        self.assertTrue(result)
        mock_module.douyin_setup.assert_awaited_once()

    def test_checks_cookie_auth_when_file_exists(self):
        import asyncio
        with patch("os.path.exists", return_value=True), \
             patch("importlib.import_module") as mock_import:
            mock_module = mock_import.return_value
            mock_module.cookie_auth = AsyncMock(return_value=True)
            mock_module.douyin_setup = AsyncMock(return_value=True)
            result = asyncio.run(
                ensure_login("douyin", "cookies/douyin_uploader/account.json")
            )
        self.assertTrue(result)
        mock_module.cookie_auth.assert_awaited_once()
        mock_module.douyin_setup.assert_not_awaited()

    def test_force_login_skips_cookie_auth_and_runs_setup(self):
        import asyncio
        with patch("os.path.exists", return_value=True), \
             patch("importlib.import_module") as mock_import:
            mock_module = mock_import.return_value
            mock_module.cookie_auth = AsyncMock(return_value=True)
            mock_module.douyin_setup = AsyncMock(return_value=True)
            result = asyncio.run(
                ensure_login(
                    "douyin", "cookies/douyin_uploader/account.json", force=True
                )
            )
        self.assertTrue(result)
        mock_module.cookie_auth.assert_not_awaited()
        mock_module.douyin_setup.assert_awaited_once_with(
            "cookies/douyin_uploader/account.json", handle=True
        )

    def test_falls_through_to_setup_when_cookie_invalid(self):
        import asyncio
        with patch("os.path.exists", return_value=True), \
             patch("importlib.import_module") as mock_import:
            mock_module = mock_import.return_value
            mock_module.cookie_auth = AsyncMock(return_value=False)
            mock_module.douyin_setup = AsyncMock(return_value=True)
            result = asyncio.run(
                ensure_login("douyin", "cookies/douyin_uploader/account.json")
            )
        self.assertTrue(result)
        mock_module.cookie_auth.assert_awaited_once()
        mock_module.douyin_setup.assert_awaited_once()


class PublishDispatchRegistryTests(unittest.TestCase):
    def test_registry_covers_all_platforms(self):
        from publish.dispatch import _PUBLISH_DISPATCH
        expected = {"douyin", "xiaohongshu", "kuaishou", "tencent", "baijiahao", "bilibili", "weibo", "tk"}
        self.assertEqual(set(_PUBLISH_DISPATCH.keys()), expected)

    def test_registry_values_are_callable(self):
        from publish.dispatch import _PUBLISH_DISPATCH
        for platform, handler in _PUBLISH_DISPATCH.items():
            self.assertTrue(callable(handler), f"{platform} handler not callable")

    def test_publish_to_platform_dispatches_to_handler(self):
        import asyncio
        from publish.dispatch import publish_to_platform
        with patch("publish.dispatch._PUBLISH_DISPATCH") as mock_reg:
            mock_handler = AsyncMock(return_value={"success": True, "message": "ok"})
            mock_reg.get.return_value = mock_handler
            result = asyncio.run(
                publish_to_platform("douyin", {"key": "val"})
            )
        self.assertEqual(result, {"success": True, "message": "ok"})
        mock_handler.assert_awaited_once_with({"key": "val"})

    def test_publish_to_platform_returns_error_for_unknown(self):
        import asyncio
        from publish.dispatch import publish_to_platform
        with patch("publish.dispatch._PUBLISH_DISPATCH") as mock_reg:
            mock_reg.get.return_value = None
            result = asyncio.run(
                publish_to_platform("unknown_plat", {})
            )
        self.assertFalse(result["success"])
        self.assertIn("未知平台", result["message"])


if __name__ == "__main__":
    unittest.main()
