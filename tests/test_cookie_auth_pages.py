import asyncio
import sys
import types
import unittest

playwright_module = types.ModuleType("playwright")
playwright_async_api = types.ModuleType("playwright.async_api")
playwright_async_api.Page = object
playwright_async_api.Playwright = object
playwright_async_api.async_playwright = None
playwright_module.async_api = playwright_async_api
sys.modules.setdefault("playwright", playwright_module)
sys.modules.setdefault("playwright.async_api", playwright_async_api)

import uploader.baijiahao_uploader.main as baijiahao_main
import uploader.douyin_uploader.main as douyin_main
import uploader.ks_uploader.main as ks_main
import uploader.tencent_uploader.main as tencent_main
import uploader.tk_uploader.main as tk_main
import uploader.xiaohongshu_uploader.main as xhs_main


class FakeLocator:
    def __init__(self, count=0, visible=False, text="", wait_raises=None):
        self._count = count
        self._visible = visible
        self._text = text
        self._wait_raises = wait_raises

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible

    async def wait_for(self, state="visible", timeout=30000):
        if self._wait_raises is not None:
            raise self._wait_raises
        if not self._visible:
            raise TimeoutError(f"Timeout {timeout}ms exceeded waiting for state={state}")

    async def inner_text(self):
        return self._text


class FakePage:
    def __init__(self, url, locators=None):
        self.url = url
        self._locators = locators or {}

    def locator(self, selector):
        return self._locators.get(selector, FakeLocator())

    def get_by_text(self, text, exact=False):
        return self._locators.get(f"text:{text}", FakeLocator())

    def get_by_role(self, role, name=None, exact=False):
        return self._locators.get(f"role:{role}:{name}", FakeLocator())


class CookieAuthPageTests(unittest.TestCase):
    def test_douyin_auth_page_requires_publish_entry(self):
        page = FakePage("https://creator.douyin.com/creator-micro/content/upload")

        valid = asyncio.run(douyin_main._is_douyin_auth_page_valid(page))

        self.assertFalse(valid)

    def test_douyin_auth_page_accepts_visible_publish_entry(self):
        page = FakePage(
            "https://creator.douyin.com/creator-micro/content/upload",
            {"text:发布视频": FakeLocator(count=1, visible=True)},
        )

        valid = asyncio.run(douyin_main._is_douyin_auth_page_valid(page))

        self.assertTrue(valid)

    def test_kuaishou_auth_page_requires_upload_button(self):
        page = FakePage(ks_main.KUAISHOU_UPLOAD_URL)

        valid = asyncio.run(ks_main._is_ks_auth_page_valid(page))

        self.assertFalse(valid)

    def test_kuaishou_auth_page_accepts_visible_upload_button(self):
        page = FakePage(
            ks_main.KUAISHOU_UPLOAD_URL,
            {'button[class^="_upload-btn"]': FakeLocator(count=1, visible=True)},
        )

        valid = asyncio.run(ks_main._is_ks_auth_page_valid(page))

        self.assertTrue(valid)

    def test_tencent_auth_page_accepts_url_fallback_without_login_markers(self):
        # 2026-08 plan:认证后 URL 且无任何登录标记可见时判定登录完成,
        # 不再强制要求可见的发布标记(Agent 环境下发布标记常未及时渲染)。
        page = FakePage(tencent_main.TENCENT_UPLOAD_URL)

        valid = asyncio.run(tencent_main._is_tencent_login_completed(page))

        self.assertTrue(valid)

    def test_tencent_auth_page_accepts_visible_publish_marker(self):
        page = FakePage(
            tencent_main.TENCENT_UPLOAD_URL,
            {'button:has-text("发表")': FakeLocator(count=1, visible=True)},
        )

        valid = asyncio.run(tencent_main._is_tencent_login_completed(page))

        self.assertTrue(valid)

    def test_baijiahao_auth_page_requires_workspace_marker(self):
        page = FakePage("https://baijiahao.baidu.com/builder/rc/home")

        valid = asyncio.run(baijiahao_main._is_baijiahao_auth_page_valid(page))

        self.assertFalse(valid)

    def test_baijiahao_auth_page_accepts_visible_workspace_marker(self):
        page = FakePage(
            "https://baijiahao.baidu.com/builder/rc/home",
            {'input[type=file]': FakeLocator(count=1, visible=True)},
        )

        valid = asyncio.run(baijiahao_main._is_baijiahao_auth_page_valid(page))

        self.assertTrue(valid)

    def test_tiktok_auth_page_requires_upload_marker(self):
        page = FakePage("https://www.tiktok.com/tiktokstudio/upload?lang=en")

        valid = asyncio.run(tk_main._is_tiktok_auth_page_valid(page))

        self.assertFalse(valid)

    def test_tiktok_auth_page_accepts_visible_upload_marker(self):
        page = FakePage(
            "https://www.tiktok.com/tiktokstudio/upload?lang=en",
            {'button:has-text("Select video")': FakeLocator(count=1, visible=True)},
        )

        valid = asyncio.run(tk_main._is_tiktok_auth_page_valid(page))

        self.assertTrue(valid)


class DouyinRestrictionDetectorTests(unittest.TestCase):
    def test_detector_returns_text_when_toast_visible(self):
        page = FakePage(
            "https://creator.douyin.com/creator-micro/content/upload",
            {
                ".semi-toast-error": FakeLocator(
                    count=1, visible=True, text="作品发布失败，健康分不足投稿功能受限"
                )
            },
        )

        result = asyncio.run(douyin_main._check_douyin_publish_restriction(page, timeout_ms=500))

        self.assertEqual(result, "作品发布失败，健康分不足投稿功能受限")

    def test_detector_returns_none_when_toast_not_visible(self):
        page = FakePage(
            "https://creator.douyin.com/creator-micro/content/upload",
            {".semi-toast-error": FakeLocator(count=0, visible=False)},
        )

        result = asyncio.run(douyin_main._check_douyin_publish_restriction(page, timeout_ms=500))

        self.assertIsNone(result)

    def test_detector_returns_none_on_unexpected_error(self):
        page = FakePage(
            "https://creator.douyin.com/creator-micro/content/upload",
            {".semi-toast-error": FakeLocator(wait_raises=RuntimeError("oops"))},
        )

        result = asyncio.run(douyin_main._check_douyin_publish_restriction(page, timeout_ms=500))

        self.assertIsNone(result)


class XhsRestrictionDetectorTests(unittest.TestCase):
    def test_detector_returns_text_when_toast_visible(self):
        page = FakePage(
            "https://creator.xiaohongshu.com/publish/publish?source=official&target=image",
            {
                "div.d-new-toast span.d-toast-description": FakeLocator(
                    count=1, visible=True, text="因违反社区规范禁止发笔记"
                )
            },
        )

        result = asyncio.run(xhs_main._check_xhs_publish_restriction(page, timeout_ms=500))

        self.assertEqual(result, "因违反社区规范禁止发笔记")

    def test_detector_returns_none_when_toast_not_visible(self):
        page = FakePage(
            "https://creator.xiaohongshu.com/publish/publish?source=official&target=image",
            {"div.d-new-toast span.d-toast-description": FakeLocator(count=0, visible=False)},
        )

        result = asyncio.run(xhs_main._check_xhs_publish_restriction(page, timeout_ms=500))

        self.assertIsNone(result)

    def test_detector_returns_none_on_unexpected_error(self):
        page = FakePage(
            "https://creator.xiaohongshu.com/publish/publish?source=official&target=image",
            {"div.d-new-toast span.d-toast-description": FakeLocator(wait_raises=RuntimeError("oops"))},
        )

        result = asyncio.run(xhs_main._check_xhs_publish_restriction(page, timeout_ms=500))

        self.assertIsNone(result)


class DouyinCookieAuthWaitTests(unittest.TestCase):
    def test_wait_for_publish_marker_returns_silently_when_visible(self):
        page = FakePage(
            "https://creator.douyin.com/creator-micro/content/upload",
            {"text:发布视频": FakeLocator(count=1, visible=True)},
        )

        # 不应抛异常
        asyncio.run(douyin_main._wait_for_douyin_publish_marker(page, timeout_ms=500))

    def test_wait_for_publish_marker_silent_on_timeout(self):
        page = FakePage(
            "https://creator.douyin.com/creator-micro/content/upload",
            {"text:发布视频": FakeLocator(count=0, visible=False)},
        )

        # 超时也不应抛异常(静默返回,由 _is_douyin_auth_page_valid 兜底)
        asyncio.run(douyin_main._wait_for_douyin_publish_marker(page, timeout_ms=500))


if __name__ == "__main__":
    unittest.main()
