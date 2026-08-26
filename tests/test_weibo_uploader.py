import asyncio
import unittest

import uploader.weibo_uploader.main as weibo_main
from uploader.base_video import LoginExpiredError


class FakeLocator:
    def __init__(self, count=0, visible=False):
        self._count = count
        self._visible = visible

    @property
    def first(self):
        return self

    async def count(self):
        return self._count

    async def is_visible(self):
        return self._visible


class FakePage:
    def __init__(self, url, locators=None):
        self.url = url
        self._locators = locators or {}

    def locator(self, selector):
        return self._locators.get(selector, FakeLocator())

    async def wait_for_timeout(self, ms):
        pass


class WeiboCookieAuthTests(unittest.TestCase):
    def test_auth_page_is_invalid_when_redirected_to_login_without_visible_marker(self):
        page = FakePage("https://passport.weibo.com/sso/signin")

        valid = asyncio.run(weibo_main._is_weibo_auth_page_valid(page))

        self.assertFalse(valid)

    def test_auth_page_requires_visible_upload_entry(self):
        page = FakePage("https://weibo.com/upload/channel")

        valid = asyncio.run(weibo_main._is_weibo_auth_page_valid(page))

        self.assertFalse(valid)

    def test_auth_page_is_valid_when_upload_button_is_visible(self):
        page = FakePage(
            "https://weibo.com/upload/channel",
            {
                'button[id^="video_button_upload"], button._btn1_109u9_8': FakeLocator(
                    count=1,
                    visible=True,
                ),
            },
        )

        valid = asyncio.run(weibo_main._is_weibo_auth_page_valid(page))

        self.assertTrue(valid)


class WeiboUploadReadinessTests(unittest.TestCase):
    def test_newlogin_redirect_raises_login_expired(self):
        page = FakePage("https://weibo.com/newlogin?retcode=6102")

        with self.assertRaisesRegex(LoginExpiredError, "cookie 已失效，请重新扫码登录"):
            asyncio.run(weibo_main._wait_for_weibo_upload_button(page, timeout_ms=0))

    def test_visible_upload_button_is_returned(self):
        upload_button = FakeLocator(count=1, visible=True)
        page = FakePage(
            "https://weibo.com/upload/channel",
            {weibo_main.WEIBO_UPLOAD_BUTTON_SELECTOR: upload_button},
        )

        result = asyncio.run(weibo_main._wait_for_weibo_upload_button(page, timeout_ms=0))

        self.assertIs(result, upload_button)

    def test_neutral_upload_timeout_is_not_login_expired(self):
        page = FakePage("https://weibo.com/upload/channel")

        with self.assertRaisesRegex(RuntimeError, "视频上传入口") as raised:
            asyncio.run(weibo_main._wait_for_weibo_upload_button(page, timeout_ms=0))
        self.assertNotIsInstance(raised.exception, LoginExpiredError)

    def test_visible_image_input_is_returned(self):
        selector = 'input[type="file"][accept*="image"]'
        image_input = FakeLocator(count=1, visible=True)
        page = FakePage("https://weibo.com/", {selector: image_input})

        result = asyncio.run(
            weibo_main._wait_for_weibo_image_input(page, [selector], timeout_ms=0)
        )

        self.assertIs(result, image_input)


if __name__ == "__main__":
    unittest.main()
