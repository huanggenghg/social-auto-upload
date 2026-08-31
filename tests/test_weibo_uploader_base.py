from __future__ import annotations

import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

from uploader.base_video import BaseBrowserUploader, LoginExpiredError, PublishStrategy
from uploader.weibo_uploader.main import (
    WEIBO_UPLOAD_BUTTON_SELECTOR,
    WeiboBaseUploader,
    WeiboNote,
    WeiboVideo,
    cookie_auth,
    weibo_setup,
)


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
    def test_upload_maps_validation_cookie_auth_failure_to_safe_retry_result(self):
        import asyncio

        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch("uploader.weibo_uploader.main.os.path.exists", return_value=True), \
             patch("uploader.weibo_uploader.main.cookie_auth", AsyncMock(return_value=False)):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["account_issue"])
        self.assertTrue(result["safe_to_retry"])

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

    def test_upload_maps_pre_selection_click_login_redirect_to_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager

        class UploadButton:
            @property
            def first(self):
                return self

            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def click(self):
                page.url = "https://weibo.com/newlogin?retcode=6102"
                raise RuntimeError("file chooser was interrupted")

        class HiddenLocator:
            @property
            def first(self):
                return self

            async def count(self):
                return 0

            async def is_visible(self):
                return False

        class FakePage:
            url = "https://weibo.com/upload/channel"

            async def goto(self, *args, **kwargs):
                pass

            def locator(self, selector):
                if selector == "button[id^=\"video_button_upload\"], button._btn1_109u9_8":
                    return UploadButton()
                return HiddenLocator()

            def expect_file_chooser(self, **kwargs):
                return chooser_context()

        @asynccontextmanager
        async def chooser_context():
            yield object()

        page = FakePage()

        @asynccontextmanager
        async def fake_session():
            yield page

        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])


class FakeFileInput:
    def __init__(self, count=0):
        self.set_input_files = AsyncMock()
        self._count = count

    @property
    def first(self):
        return self

    async def count(self):
        return self._count


class FakeFileChooser:
    def __init__(self):
        self.set_files = AsyncMock()


class FakeFileChooserInfo:
    def __init__(self, chooser):
        self._chooser = chooser

    @property
    def value(self):
        async def resolve():
            return self._chooser

        return resolve()


class WeiboUploadPage:
    url = "https://weibo.com/upload/channel"

    def __init__(self, file_input):
        self.file_input = file_input
        self.file_chooser = FakeFileChooser()

    async def goto(self, *args, **kwargs):
        pass

    async def evaluate(self, script):
        # 让上传流程走"秒传完成"分支立即返回
        return "auto"

    def expect_file_chooser(self, **kwargs):
        page = self

        @asynccontextmanager
        async def chooser_context():
            yield FakeFileChooserInfo(page.file_chooser)

        return chooser_context()

    def locator(self, selector):
        if selector == 'input[type="file"]':
            return self.file_input
        if selector == WEIBO_UPLOAD_BUTTON_SELECTOR:
            return _VisibleLocator()
        return _HiddenLocator()


class _HiddenLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return False


class _VisibleLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def click(self):
        return None


class WeiboVideoFileSelectionTests(unittest.TestCase):
    def test_video_upload_prefers_existing_file_input(self):
        file_input = FakeFileInput(count=1)
        page = WeiboUploadPage(file_input=file_input)
        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch(
            "uploader.weibo_uploader.main._wait_for_weibo_upload_button",
            AsyncMock(side_effect=AssertionError("button path must not run")),
        ):
            asyncio.run(uploader.upload_video_content(page))
        file_input.set_input_files.assert_awaited_once_with("/fake.mp4")

    def test_video_upload_uses_file_chooser_when_input_is_absent(self):
        from uploader.weibo_uploader.main import _select_weibo_video_file

        page = WeiboUploadPage(file_input=FakeFileInput(count=0))
        asyncio.run(_select_weibo_video_file(page, "/fake.mp4"))
        page.file_chooser.set_files.assert_awaited_once_with("/fake.mp4")


class WeiboNoteUploadTests(unittest.TestCase):
    def test_upload_maps_validation_cookie_auth_failure_to_safe_retry_result(self):
        import asyncio

        uploader = WeiboNote(
            image_paths=["/fake.jpg"], note="test note", tags=[],
            publish_date=0, account_file="/fake.json",
        )
        with patch("uploader.weibo_uploader.main.os.path.exists", return_value=True), \
             patch("uploader.weibo_uploader.main.cookie_auth", AsyncMock(return_value=False)):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["account_issue"])
        self.assertTrue(result["safe_to_retry"])

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
