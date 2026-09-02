from __future__ import annotations

import unittest
from types import SimpleNamespace
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

    def test_cookie_auth_retries_dom_validation_while_page_renders(self):
        """首页 SPA 冷加载时 marker 可能 5 秒后才渲染, 校验需轮询而不是单次判定。"""
        import asyncio
        from contextlib import asynccontextmanager

        class FakePage:
            url = "https://baijiahao.baidu.com/builder/rc/home"

            async def goto(self, *args, **kwargs):
                return None

            async def wait_for_timeout(self, timeout=0):
                return None

        class FakeContext:
            async def new_page(self):
                return FakePage()

        class FakeBrowser:
            async def new_context(self, **kwargs):
                return FakeContext()

            async def close(self):
                return None

        class FakePlaywright:
            def __init__(self):
                self.chromium = SimpleNamespace(launch=AsyncMock(return_value=FakeBrowser()))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        import os
        with patch("uploader.baijiahao_uploader.main.async_playwright", return_value=FakePlaywright()), \
             patch("uploader.baijiahao_uploader.main.set_init_script", side_effect=lambda ctx: ctx), \
             patch("uploader.baijiahao_uploader.main.os.path.exists", return_value=True), \
             patch.object(BaiJiaHaoVideo, "_launch_browser", AsyncMock(return_value=FakeBrowser())), \
             patch(
                 "uploader.baijiahao_uploader.main._is_baijiahao_auth_page_valid",
                 AsyncMock(side_effect=[False, False, True]),
             ):
            result = asyncio.run(BaiJiaHaoVideo.cookie_auth("/fake.json"))

        self.assertTrue(result)


class BaiJiaHaoCoverReadyTests(unittest.TestCase):
    def _run(self, legacy_count, cover_img_count):
        import asyncio

        class FakeLocator:
            def __init__(self, count):
                self._count = count

            async def count(self):
                return self._count

        class FakePage:
            def locator(self, selector):
                if selector == "div.cheetah-spin-container img":
                    return FakeLocator(legacy_count)
                if 'img[class*="cover"]' in selector:
                    return FakeLocator(cover_img_count)
                return FakeLocator(0)

        from uploader.baijiahao_uploader.main import _is_baijiahao_cover_ready

        return asyncio.run(_is_baijiahao_cover_ready(FakePage()))

    def test_legacy_spin_container_img_counts_as_ready(self):
        self.assertTrue(self._run(legacy_count=1, cover_img_count=0))

    def test_cover_class_img_counts_as_ready(self):
        # 新版编辑页封面图不在 cheetah-spin-container 内, class 含 cover
        self.assertTrue(self._run(legacy_count=0, cover_img_count=2))

    def test_no_cover_images_means_not_ready(self):
        self.assertFalse(self._run(legacy_count=0, cover_img_count=0))


class BaiJiaHaoVideoCoverTests(unittest.TestCase):
    """新版编辑页上传后默认不选封面, 发布依次报"请添加横版封面"/
    "请添加竖版封面"; 需逐个点击"选择封面"入口并在弹窗中点"确定"。"""

    def _make_page(self, cover_texts):
        import asyncio

        class FakeCoverEntry:
            def __init__(self, page, index):
                self._page = page
                self._index = index

            async def inner_text(self):
                return self._page.cover_texts[self._index]

            async def click(self):
                self._page.clicked_entries.append(self._index)

        class FakeCoverRoot:
            def __init__(self, page):
                self._page = page

            def nth(self, index):
                return FakeCoverEntry(self._page, index)

        class FakeConfirm:
            def __init__(self, page):
                self._page = page

            async def wait_for(self, state=None, timeout=None):
                return None

            async def click(self):
                self._page.confirm_count += 1

        class FakeFrame:
            def __init__(self, page):
                self._page = page

            @property
            def first(self):
                return self

            async def wait_for(self, state=None, timeout=None):
                self._page.frame_waited = True

        class FakePage:
            def __init__(self):
                self.cover_texts = cover_texts
                self.clicked_entries = []
                self.confirm_count = 0
                self.frame_waited = False

            def locator(self, selector):
                if "cover-container" in selector:
                    return FakeCoverRoot(self)
                if "cover-image" in selector:
                    return FakeFrame(self)
                if "确定" in selector:
                    return FakeConfirm(self)
                raise AssertionError(f"unexpected selector: {selector}")

            async def wait_for_timeout(self, timeout=0):
                return None

        return FakePage()

    def test_sets_both_covers_when_unset(self):
        import asyncio

        from uploader.baijiahao_uploader.main import _set_video_covers
        page = self._make_page(cover_texts=["选择封面", "选择封面"])
        asyncio.run(_set_video_covers(page))
        self.assertEqual(page.clicked_entries, [0, 1])
        self.assertEqual(page.confirm_count, 2)
        self.assertTrue(page.frame_waited)

    def test_skips_cover_already_set(self):
        import asyncio

        from uploader.baijiahao_uploader.main import _set_video_covers
        page = self._make_page(cover_texts=["封面不佳", "选择封面"])
        asyncio.run(_set_video_covers(page))
        self.assertEqual(page.clicked_entries, [1])
        self.assertEqual(page.confirm_count, 1)


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
