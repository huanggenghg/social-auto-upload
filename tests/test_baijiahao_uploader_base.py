from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from uploader.base_video import BaseBrowserUploader, PublishStrategy
from uploader.baijiahao_uploader.main import BaiJiaHaoVideo, cookie_auth, baijiahao_setup


class BaiJiaHaoVideoInheritanceTests(unittest.TestCase):
    def test_inherits_base_browser_uploader(self):
        self.assertTrue(issubclass(BaiJiaHaoVideo, BaseBrowserUploader))

    def test_platform_name(self):
        self.assertEqual(BaiJiaHaoVideo.PLATFORM_NAME, "baijiahao")


class BaiJiaHaoLoginCompletionTests(unittest.TestCase):
    def test_login_completed_uses_baijiahao_dom_validation(self):
        import asyncio
        from uploader.baijiahao_uploader import main as bj_main

        page = object()
        with patch(
            "uploader.baijiahao_uploader.main._is_baijiahao_auth_page_valid",
            AsyncMock(return_value=True),
        ) as validate:
            result = asyncio.run(BaiJiaHaoVideo.is_login_completed(page))
        self.assertTrue(result)
        validate.assert_awaited_once_with(page)

    def test_login_not_completed_when_dom_validation_fails(self):
        import asyncio

        page = object()
        with patch(
            "uploader.baijiahao_uploader.main._is_baijiahao_auth_page_valid",
            AsyncMock(return_value=False),
        ):
            result = asyncio.run(BaiJiaHaoVideo.is_login_completed(page))
        self.assertFalse(result)


class BaiJiaHaoVideoUploadTests(unittest.TestCase):
    def test_upload_returns_unified_dict_with_url(self):
        import asyncio
        uploader = BaiJiaHaoVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json",
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def fake_session():
                class FakePage:
                    url = "https://baijiahao.baidu.com"
                yield FakePage()

            with patch.object(uploader, "_browser_session", return_value=fake_session()), \
                 patch.object(BaiJiaHaoVideo, "upload_video_content", AsyncMock(return_value="https://baijiahao.baidu.com/s?id=123")):
                result = asyncio.run(uploader.upload())
        self.assertTrue(result["success"])
        self.assertEqual(result["result_url"], "https://baijiahao.baidu.com/s?id=123")


class ModuleWrapperTests(unittest.TestCase):
    def test_setup_signature_is_5_params(self):
        import inspect
        sig = inspect.signature(baijiahao_setup)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["account_file", "handle", "return_detail", "qrcode_callback", "headless"])

    def test_cookie_auth_is_callable(self):
        self.assertTrue(callable(cookie_auth))


if __name__ == "__main__":
    unittest.main()


class BaiJiaHaoWaitTimeoutTests(unittest.TestCase):
    """封面/上传等待循环必须有超时兜底,防止状态元素永远不出现时进程挂死。"""

    def test_uploading_video_raises_on_timeout(self):
        import asyncio
        from uploader.baijiahao_uploader import main as bj_main
        from uploader.baijiahao_uploader.main import BaiJiaHaoVideo

        uploader = BaiJiaHaoVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json",
        )
        with patch.object(bj_main, "BAIJIAHAO_UPLOAD_WAIT_TIMEOUT", 0):
            with self.assertRaises(TimeoutError):
                asyncio.run(uploader.uploading_video(page=None))
