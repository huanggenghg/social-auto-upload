from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from uploader.base_video import BaseBrowserUploader, LoginExpiredError, PublishStrategy
from uploader.weibo_uploader.main import WeiboBaseUploader, WeiboVideo, WeiboNote, cookie_auth, weibo_setup


class WeiboBaseUploaderInheritanceTests(unittest.TestCase):
    def test_inherits_base_browser_uploader(self):
        self.assertTrue(issubclass(WeiboBaseUploader, BaseBrowserUploader))

    def test_platform_name(self):
        self.assertEqual(WeiboBaseUploader.PLATFORM_NAME, "weibo")

    def test_upload_url(self):
        self.assertTrue(WeiboBaseUploader.UPLOAD_URL.startswith("https://"))

    def test_login_url(self):
        self.assertTrue(WeiboBaseUploader.LOGIN_URL.startswith("https://"))

    def test_login_markers_nonempty(self):
        self.assertGreater(len(WeiboBaseUploader.LOGIN_MARKERS), 0)


class WeiboVideoUploadTests(unittest.TestCase):
    def test_upload_returns_platform_result_extras(self):
        import asyncio
        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "_browser_session") as mock_session, \
             patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(WeiboVideo, "upload_video_content", AsyncMock(return_value="https://weibo.com/v/123")):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def fake_session():
                class FakePage:
                    url = "https://weibo.com/upload/channel"
                yield FakePage()

            mock_session.return_value = fake_session()
            result = asyncio.run(uploader.upload())
        self.assertTrue(result["success"])
        self.assertEqual(result["result_url"], "https://weibo.com/v/123")

    def test_upload_maps_pre_media_login_expiry_to_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager

        class FakePage:
            url = "https://weibo.com/upload/channel"

            async def goto(self, *args, **kwargs):
                self.url = "https://weibo.com/newlogin?retcode=6102"

            def locator(self, selector):
                raise AssertionError(f"login redirect should be detected before querying {selector}")

            async def wait_for_timeout(self, ms):
                pass

        @asynccontextmanager
        async def fake_session():
            yield FakePage()

        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])

    def test_login_expiry_after_media_selection_is_not_safe_to_retry(self):
        import asyncio
        from contextlib import asynccontextmanager

        class FakeFileChooser:
            set_files = AsyncMock()

        file_chooser = FakeFileChooser()

        async def fail_after_media_selection(page):
            await file_chooser.set_files("/fake.mp4")
            raise LoginExpiredError("登录在文件选择后失效")

        @asynccontextmanager
        async def fake_session():
            yield object()

        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(uploader, "upload_video_content", side_effect=fail_after_media_selection):
            result = asyncio.run(uploader.upload())

        file_chooser.set_files.assert_awaited_once_with("/fake.mp4")
        self.assertEqual(result, {"success": False, "message": "登录在文件选择后失效"})


class WeiboNoteUploadTests(unittest.TestCase):
    def test_upload_returns_platform_result_extras(self):
        import asyncio
        uploader = WeiboNote(
            image_paths=["/fake.jpg"], note="test note", tags=[],
            publish_date=0, account_file="/fake.json",
        )
        with patch.object(uploader, "_browser_session") as mock_session, \
             patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(WeiboNote, "upload_note_content", AsyncMock()):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def fake_session():
                class FakePage:
                    url = "https://weibo.com/upload/channel"
                yield FakePage()

            mock_session.return_value = fake_session()
            result = asyncio.run(uploader.upload())
        self.assertTrue(result["success"])
        self.assertEqual(result["message"], "发布成功")

    def test_upload_maps_pre_media_login_expiry_to_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager

        class FakePage:
            url = "https://weibo.com/"

            async def goto(self, *args, **kwargs):
                self.url = "https://weibo.com/newlogin?retcode=6102"

            def locator(self, selector):
                raise AssertionError(f"login redirect should be detected before querying {selector}")

            async def wait_for_timeout(self, ms):
                pass

        @asynccontextmanager
        async def fake_session():
            yield FakePage()

        uploader = WeiboNote(
            image_paths=["/fake.jpg"], note="test note", tags=[],
            publish_date=0, account_file="/fake.json",
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])


class WeiboLogTimingTests(unittest.TestCase):
    def test_log_not_printed_when_upload_raises_exception(self):
        """When upload() raises an exception, 'cookie 更新完毕' log must NOT fire.
        Log is now after the async with block (after storage_state save),
        so exceptions skip it."""
        import asyncio
        uploader = WeiboVideo(
            title="test", file_path="/fake.mp4", tags=[],
            publish_date=0, account_file="/fake.json",
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session") as mock_session, \
             patch.object(uploader, "upload_video_content", AsyncMock(side_effect=RuntimeError("upload failed"))), \
             patch("uploader.weibo_uploader.main.weibo_logger") as mock_logger:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def fake_session():
                class FakePage:
                    url = "https://weibo.com/upload"
                yield FakePage()

            mock_session.return_value = fake_session()
            result = asyncio.run(uploader.upload())
        self.assertFalse(result["success"])
        # "cookie 更新完毕" log must NOT be called when upload fails
        for call in mock_logger.success.call_args_list:
            args, kwargs = call
            if args and "cookie 更新完毕" in str(args[0]):
                self.fail("cookie 更新完毕 log was printed on failure - should only print on success")

    def test_log_printed_on_success(self):
        """On success, 'cookie 更新完毕' log fires. Complements the failure-path
        test (verifies log does NOT fire on failure) and the ordering test in
        test_base_uploader_session.py (verifies storage_state saves before code
        after async with runs)."""
        import asyncio
        uploader = WeiboVideo(
            title="test", file_path="/fake.mp4", tags=[],
            publish_date=0, account_file="/fake.json",
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session") as mock_session, \
             patch.object(WeiboVideo, "upload_video_content", AsyncMock(return_value="https://weibo.com/v/123")), \
             patch("uploader.weibo_uploader.main.weibo_logger") as mock_logger:
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def fake_session():
                class FakePage:
                    url = "https://weibo.com/upload"
                yield FakePage()

            mock_session.return_value = fake_session()
            result = asyncio.run(uploader.upload())
        self.assertTrue(result["success"])
        # "cookie 更新完毕" log must be called on success
        success_calls = []
        for call in mock_logger.success.call_args_list:
            args, kwargs = call
            if args and "cookie 更新完毕" in str(args[0]):
                success_calls.append(call)
        self.assertEqual(len(success_calls), 1)


class ModuleWrapperTests(unittest.TestCase):
    def test_cookie_auth_delegates_to_classmethod(self):
        import asyncio
        with patch.object(WeiboBaseUploader, "cookie_auth", AsyncMock(return_value=True)):
            result = asyncio.run(cookie_auth("/fake.json"))
        self.assertTrue(result)

    def test_weibo_setup_signature_is_5_params(self):
        import inspect
        sig = inspect.signature(weibo_setup)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["account_file", "handle", "return_detail", "qrcode_callback", "headless"])


if __name__ == "__main__":
    unittest.main()
