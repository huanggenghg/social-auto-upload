from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from uploader.base_video import BaseBrowserUploader, LoginExpiredError, PublishStrategy
from uploader.tencent_uploader import main as tencent_main
from uploader.tencent_uploader.main import TencentBaseUploader, TencentVideo, cookie_auth, tencent_setup


class TencentBaseUploaderInheritanceTests(unittest.TestCase):
    def test_inherits_base_browser_uploader(self):
        self.assertTrue(issubclass(TencentBaseUploader, BaseBrowserUploader))

    def test_platform_name(self):
        self.assertEqual(TencentBaseUploader.PLATFORM_NAME, "tencent")


class TencentVideoUploadTests(unittest.TestCase):
    def test_upload_returns_unified_dict_with_empty_url(self):
        import asyncio
        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()):
            from contextlib import asynccontextmanager

            @asynccontextmanager
            async def fake_session():
                class FakePage:
                    url = "https://channels.weixin.qq.com"
                yield FakePage()

            with patch.object(uploader, "_browser_session", return_value=fake_session()), \
                 patch.object(TencentVideo, "upload_video_content", AsyncMock()):
                result = asyncio.run(uploader.upload())
        self.assertTrue(result["success"])
        # result_url only present when _fetch_published_video_short_url succeeds
        # (upload_video_content is mocked here, so no URL captured)
        self.assertNotIn("result_url", result)

    def test_upload_does_not_save_cookie_state_on_success(self):
        """微信视频号 cookie 文件不应该被 publish 流程覆盖。

        publish 流程结束后 storage_state 只剩 sessionid/wxuin 2 个 cookie
        (其他在 ~30s 上传过程中过期),覆盖会让下次 cookie_auth 必然失败。
        upload() 应该用 save_state=False 调用 _browser_session。
        """
        import asyncio
        from contextlib import asynccontextmanager

        captured_kwargs = {}

        @asynccontextmanager
        async def fake_session(*args, **kwargs):
            captured_kwargs.update(kwargs)
            class FakePage:
                url = "https://channels.weixin.qq.com"
            yield FakePage()

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", side_effect=fake_session), \
             patch.object(TencentVideo, "upload_video_content", AsyncMock()):
            asyncio.run(uploader.upload())

        self.assertFalse(
            captured_kwargs.get("save_state", True),
            f"upload() 应该传 save_state=False 防止 cookie 文件被覆盖,实际收到: {captured_kwargs}",
        )

    def test_upload_maps_pre_media_login_expiry_to_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session():
            class FakePage:
                url = "https://channels.weixin.qq.com/platform/post/create"

                async def goto(self, *args, **kwargs):
                    self.url = "https://channels.weixin.qq.com/login.html"

                def locator(self, selector):
                    raise AssertionError(f"login redirect should be detected before querying {selector}")

            yield FakePage()

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(uploader, "upload_video_file", AsyncMock()) as upload_mock:
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])
        upload_mock.assert_not_called()

    def test_login_expiry_after_file_selection_is_not_safe_to_retry(self):
        import asyncio
        from contextlib import asynccontextmanager

        class FakeFileInput:
            def __init__(self):
                self.set_input_files = AsyncMock()

        ready_file_input = FakeFileInput()
        requery_file_input = FakeFileInput()

        class FakeLocator:
            def __init__(self, count, first=None):
                self._count = count
                self.first = first if first is not None else self

            async def count(self):
                return self._count

        class FakePage:
            url = "https://channels.weixin.qq.com/platform/post/create"
            file_input_queries = 0

            async def goto(self, *args, **kwargs):
                pass

            def locator(self, selector):
                if selector == 'iframe[src*="qrconnect"]':
                    return FakeLocator(0)
                if selector == 'input[type="file"]':
                    inputs = [ready_file_input, requery_file_input]
                    file_input = inputs[min(self.file_input_queries, len(inputs) - 1)]
                    self.file_input_queries += 1
                    return FakeLocator(1, first=file_input)
                raise AssertionError(f"unexpected selector: {selector}")

        page = FakePage()

        @asynccontextmanager
        async def fake_session():
            yield page

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(
                 uploader,
                 "prepare_video_for_publish",
                 AsyncMock(side_effect=LoginExpiredError("登录在文件选择后失效")),
             ):
            result = asyncio.run(uploader.upload())

        ready_file_input.set_input_files.assert_awaited_once_with("/fake.mp4")
        self.assertEqual(page.file_input_queries, 1)
        self.assertEqual(result, {"success": False, "message": "登录在文件选择后失效"})

    def test_upload_keeps_generic_error_unstructured(self):
        import asyncio
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_session():
            class FakePage:
                url = "https://channels.weixin.qq.com/platform/post/create"
            yield FakePage()

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(TencentVideo, "upload_video_content", AsyncMock(side_effect=RuntimeError("上传失败"))):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result, {"success": False, "message": "上传失败"})


class TencentUploadInputReadinessTests(unittest.TestCase):
    @staticmethod
    def _locator(count, first=None):
        class FakeLocator:
            async def count(self):
                return count

        locator = FakeLocator()
        locator.first = first if first is not None else locator
        return locator

    def test_login_html_redirect_raises_login_expired(self):
        import asyncio

        class FakePage:
            url = "https://channels.weixin.qq.com/login.html"

            def locator(self, selector):
                raise AssertionError(f"login.html should be detected before querying {selector}")

        self.assertTrue(hasattr(tencent_main, "_wait_for_tencent_upload_input"))
        with self.assertRaisesRegex(LoginExpiredError, "cookie 已失效，请重新扫码登录"):
            asyncio.run(tencent_main._wait_for_tencent_upload_input(FakePage(), timeout_ms=0))

    def test_qrconnect_iframe_raises_login_expired(self):
        import asyncio

        class FakePage:
            url = "https://channels.weixin.qq.com/platform/post/create"

            def locator(self, selector):
                if selector == 'iframe[src*="qrconnect"]':
                    return TencentUploadInputReadinessTests._locator(1)
                raise AssertionError(f"qrconnect should be detected before querying {selector}")

        self.assertTrue(hasattr(tencent_main, "_wait_for_tencent_upload_input"))
        with self.assertRaisesRegex(LoginExpiredError, "cookie 已失效，请重新扫码登录"):
            asyncio.run(tencent_main._wait_for_tencent_upload_input(FakePage(), timeout_ms=0))

    def test_returns_first_upload_file_input(self):
        import asyncio

        file_input = object()

        class FakePage:
            url = "https://channels.weixin.qq.com/platform/post/create"

            def locator(self, selector):
                if selector == 'iframe[src*="qrconnect"]':
                    return TencentUploadInputReadinessTests._locator(0)
                if selector == 'input[type="file"]':
                    return TencentUploadInputReadinessTests._locator(1, first=file_input)
                raise AssertionError(f"unexpected selector: {selector}")

        self.assertTrue(hasattr(tencent_main, "_wait_for_tencent_upload_input"))
        result = asyncio.run(tencent_main._wait_for_tencent_upload_input(FakePage(), timeout_ms=0))
        self.assertIs(result, file_input)

    def test_neutral_timeout_is_runtime_error_not_login_expired(self):
        import asyncio

        class FakePage:
            url = "https://channels.weixin.qq.com/platform/post/create"

            def locator(self, selector):
                return TencentUploadInputReadinessTests._locator(0)

        self.assertTrue(hasattr(tencent_main, "_wait_for_tencent_upload_input"))
        with self.assertRaisesRegex(RuntimeError, "上传控件") as raised:
            asyncio.run(tencent_main._wait_for_tencent_upload_input(FakePage(), timeout_ms=0))
        self.assertNotIsInstance(raised.exception, LoginExpiredError)

    def test_poll_sleep_is_bounded_by_remaining_deadline(self):
        import asyncio

        class FakeTime:
            values = iter([0.0, 0.05, 0.1])

            @classmethod
            def monotonic(cls):
                return next(cls.values)

        class FakePage:
            url = "https://channels.weixin.qq.com/platform/post/create"

            def locator(self, selector):
                return TencentUploadInputReadinessTests._locator(0)

        with patch.object(tencent_main, "time", FakeTime), \
             patch.object(tencent_main.asyncio, "sleep", AsyncMock()) as sleep_mock:
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    tencent_main._wait_for_tencent_upload_input(
                        FakePage(),
                        timeout_ms=100,
                        poll_interval_ms=250,
                    )
                )

        sleep_mock.assert_awaited_once()
        self.assertAlmostEqual(sleep_mock.await_args.args[0], 0.05)


class TencentCookieGenTests(unittest.TestCase):
    """扫码 QR 提取失败时,tencent_cookie_gen 不应该 abort,而是继续等登录。

    场景:微信视频号登录页的 QR 在 qrconnect iframe 里,img.qrcode 的 src 是
    相对 URL(不是 data:image/),_extract_tencent_qrcode_src 会抛 RuntimeError。
    用户在 headless=False 浏览器里手动扫码后,脚本应该继续 poll 登录完成,
    而不是直接关闭浏览器报"未获取到视频号登录二维码地址"。
    """

    def test_continues_to_wait_for_login_when_qrcode_extraction_fails(self):
        import asyncio
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_playwright():
            class FakePage:
                url = "https://channels.weixin.qq.com/platform/post/create"

                async def goto(self, *args, **kwargs):
                    pass

            class FakeContext:
                async def new_page(self):
                    return FakePage()

                async def storage_state(self, *args, **kwargs):
                    pass

                async def close(self):
                    pass

            class FakeBrowser:
                async def new_context(self, *args, **kwargs):
                    return FakeContext()

                async def close(self):
                    pass

            class FakeChromium:
                async def launch(self, *args, **kwargs):
                    return FakeBrowser()

            class FakePlaywright:
                chromium = FakeChromium()

            yield FakePlaywright()

        async def run():
            success_result = {
                "success": True, "status": "success", "message": "ok",
                "account_file": "/tmp/test.json", "qrcode": None, "current_url": "",
            }
            with patch("uploader.tencent_uploader.main.async_playwright", fake_playwright), \
                 patch(
                     "uploader.tencent_uploader.main._save_tencent_qrcode",
                     AsyncMock(side_effect=RuntimeError("未获取到视频号登录二维码地址")),
                 ) as save_mock, \
                 patch(
                     "uploader.tencent_uploader.main._wait_for_tencent_login",
                     AsyncMock(return_value=success_result),
                 ) as wait_mock, \
                 patch("uploader.tencent_uploader.main.cookie_auth", AsyncMock(return_value=True)):
                from uploader.tencent_uploader.main import tencent_cookie_gen
                result = await tencent_cookie_gen("/tmp/test.json", headless=False)
            return result, save_mock, wait_mock

        result, save_mock, wait_mock = asyncio.run(run())
        self.assertTrue(
            result["success"],
            f"QR 提取失败时应继续等登录,但结果为: {result}",
        )
        save_mock.assert_called_once()
        wait_mock.assert_called_once()


class TencentQrcodeExtractionTests(unittest.TestCase):
    """真实登录页(2026-08 观察):二维码在 qrconnect iframe 内,img.qrcode 的 src 是
    相对 URL(/connect/qrcode/...),不是 data:image/。
    提取必须:1) 用 iframe[src*="qrconnect"];2) 用 element.screenshot 存 PNG。
    """

    def _make_page(self, calls):
        class FakeQrImg:
            async def wait_for(self, *args, **kwargs):
                pass

            async def get_attribute(self, name):
                return "/connect/qrcode/abc123"

            async def screenshot(self, path=None, **kwargs):
                from pathlib import Path
                calls["screenshot_path"] = str(path)
                Path(path).write_bytes(b"\x89PNG-fake")
                return b"\x89PNG-fake"

        class FakeInnerLocator:
            first = FakeQrImg()

        class FakeFrameLocator:
            def locator(self, selector):
                calls["inner_selector"] = selector
                return FakeInnerLocator()

        class FakePage:
            def frame_locator(self, selector):
                calls["iframe_selector"] = selector
                return FakeFrameLocator()

        return FakePage()

    def test_save_tencent_qrcode_uses_qrconnect_iframe_and_screenshot(self):
        import asyncio
        import tempfile
        from pathlib import Path

        from uploader.tencent_uploader.main import _save_tencent_qrcode

        calls = {}
        page = self._make_page(calls)
        with tempfile.TemporaryDirectory() as tmpdir:
            account_file = str(Path(tmpdir) / "acc.json")
            result = asyncio.run(_save_tencent_qrcode(page, account_file))

        self.assertIn("qrconnect", calls.get("iframe_selector", ""))
        self.assertEqual("img.qrcode", calls.get("inner_selector"))
        self.assertTrue(calls["screenshot_path"].endswith(".png"))
        self.assertEqual(result["image_path"], calls["screenshot_path"])


class TencentRedundantCookieAuthTests(unittest.TestCase):
    """tencent sessionid 在新浏览器上下文里 22 秒失效,任何 cookie_auth 调用都会开新浏览器,
    导致后续 cookie_auth 误判失效。所以:
    1. TencentBaseUploader.cookie_auth 不应开浏览器,只检查文件存在性(实际校验交给 _browser_session)
    2. tencent_setup(handle=True) 应该总是扫码 -- ensure_login 调到 tencent_setup 就说明
       cookie_auth 已判定失效,这时 return True 会让 upload() 用失效 cookie 进 _browser_session 失败
    3. validate_login_and_strategy 不应调 cookie_auth -- ensure_login 已验过"""

    def test_tencent_setup_scans_when_handle_true_even_if_file_exists(self):
        """handle=True + 文件存在时,tencent_setup 也应扫码,不 return True。
        因为 ensure_login 调到 tencent_setup 就说明 cookie 已失效。"""
        import asyncio
        import tempfile

        async def run():
            with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
                with patch("uploader.tencent_uploader.main.cookie_auth", AsyncMock(return_value=True)) as auth_mock, \
                     patch("uploader.tencent_uploader.main.tencent_cookie_gen", AsyncMock(return_value={"success": True})) as gen_mock:
                    from uploader.tencent_uploader.main import tencent_setup
                    return await tencent_setup(tmp.name, handle=True), auth_mock, gen_mock

        result, auth_mock, gen_mock = asyncio.run(run())
        auth_mock.assert_not_called()
        gen_mock.assert_called_once()

    def test_tencent_setup_scans_when_handle_true_and_file_missing(self):
        """handle=True + 文件不存在时,tencent_setup 直接扫码。"""
        import asyncio

        async def run():
            with patch("uploader.tencent_uploader.main.cookie_auth", AsyncMock(return_value=True)) as auth_mock, \
                 patch("uploader.tencent_uploader.main.tencent_cookie_gen", AsyncMock(return_value={"success": True})) as gen_mock:
                from uploader.tencent_uploader.main import tencent_setup
                return await tencent_setup("/nonexistent/fake.json", handle=True), auth_mock, gen_mock

        result, auth_mock, gen_mock = asyncio.run(run())
        auth_mock.assert_not_called()
        gen_mock.assert_called_once()

    def test_validate_login_and_strategy_skips_cookie_auth(self):
        """validate_login_and_strategy 不应调 cookie_auth -- ensure_login 已验过,
        再开浏览器会让 sessionid 失效。只检查文件存在性 + strategy。"""
        import asyncio
        import tempfile

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
            uploader.account_file = tmp.name
            with patch("uploader.tencent_uploader.main.cookie_auth", AsyncMock(return_value=True)) as auth_mock:
                asyncio.run(uploader.validate_login_and_strategy())
        auth_mock.assert_not_called()

    def test_cookie_auth_checks_file_existence_only(self):
        """TencentBaseUploader.cookie_auth 只检查文件存在性,不开浏览器。
        tencent sessionid 在新浏览器上下文里 22 秒失效,主动开浏览器校验会让有效 cookie 误判失效。"""
        import asyncio
        import tempfile

        async def run():
            with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
                with patch("uploader.tencent_uploader.main.async_playwright") as pw_mock:
                    exists_result = await TencentBaseUploader.cookie_auth(tmp.name)
                    not_exists_result = await TencentBaseUploader.cookie_auth("/nonexistent/fake.json")
                    return exists_result, not_exists_result, pw_mock

        exists_result, not_exists_result, pw_mock = asyncio.run(run())
        self.assertTrue(exists_result)
        self.assertFalse(not_exists_result)
        pw_mock.assert_not_called()


class TencentLoginCompletionTests(unittest.TestCase):
    @staticmethod
    def _make_page(url, visible_selectors=frozenset()):
        class FakeLocator:
            def __init__(self, visible):
                self._visible = visible

            @property
            def first(self):
                return self

            async def count(self):
                return 1 if self._visible else 0

            async def is_visible(self):
                return self._visible

        page = type("FakePage", (), {})()
        page.url = url

        def locator(selector):
            return FakeLocator(selector in visible_selectors)

        page.locator = locator
        return page

    def test_login_completed_on_publish_url_without_login_markers(self):
        import asyncio

        page = TencentLoginCompletionTests._make_page(
            url="https://channels.weixin.qq.com/platform/post/create",
            visible_selectors=set(),
        )
        self.assertTrue(asyncio.run(tencent_main._is_tencent_login_completed(page)))

    def test_login_not_completed_when_qr_iframe_is_visible(self):
        import asyncio

        page = TencentLoginCompletionTests._make_page(
            url="https://channels.weixin.qq.com/platform/post/create",
            visible_selectors={"iframe[src*=\"qrconnect\"]"},
        )
        self.assertFalse(asyncio.run(tencent_main._is_tencent_login_completed(page)))


class ModuleWrapperTests(unittest.TestCase):
    def test_setup_signature_is_5_params(self):
        import inspect
        sig = inspect.signature(tencent_setup)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["account_file", "handle", "return_detail", "qrcode_callback", "headless"])


if __name__ == "__main__":
    unittest.main()


class TencentUploadWaitTimeoutTests(unittest.TestCase):
    """wait_for_upload_complete/submit_publish 的 while 循环必须有超时兜底,
    否则发表按钮永远不激活/页面永远不跳转时进程会无限挂死。"""

    def test_wait_for_upload_complete_raises_on_timeout(self):
        import asyncio
        from uploader.tencent_uploader import main as tencent_main
        from uploader.tencent_uploader.main import TencentVideo

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(tencent_main, "TENCENT_UPLOAD_WAIT_TIMEOUT", 0):
            with self.assertRaises(TimeoutError):
                asyncio.run(uploader.wait_for_upload_complete(page=None))

    def test_submit_publish_raises_on_timeout(self):
        import asyncio
        from uploader.tencent_uploader import main as tencent_main
        from uploader.tencent_uploader.main import TencentVideo

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(tencent_main, "TENCENT_PUBLISH_WAIT_TIMEOUT", 0):
            with self.assertRaises(TimeoutError):
                asyncio.run(uploader.submit_publish(page=None))
