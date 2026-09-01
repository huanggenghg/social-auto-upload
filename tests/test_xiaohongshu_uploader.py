import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from patchright.async_api import TimeoutError as PlaywrightTimeoutError

import uploader.xiaohongshu_uploader.main as xhs_main


class FakeLocator:
    def __init__(self, name, count=0, src=None, children=None):
        self.name = name
        self._count = count
        self._src = src
        self._children = children or {}

    @property
    def first(self):
        return self

    def locator(self, selector):
        return self._children.get(selector, FakeLocator(selector))

    def get_by_text(self, text, exact=False):
        return self._children.get(f"text:{text}", FakeLocator(text))

    def filter(self, **kwargs):
        return self

    def nth(self, index):
        return self

    async def count(self):
        return self._count

    async def wait_for(self, **kwargs):
        return None

    async def get_attribute(self, name):
        if name == "src":
            return self._src
        return None

    async def fill(self, value):
        return None

    async def click(self):
        return None


class RecordingKeyboard:
    def __init__(self):
        self.actions = []

    async def press(self, key):
        self.actions.append(("press", key))

    async def type(self, text, delay=None):
        self.actions.append(("type", text, delay))


class RecordingLocator(FakeLocator):
    def __init__(self, name):
        super().__init__(name, count=1)
        self.actions = []

    async def fill(self, value):
        self.actions.append(("fill", value))

    async def click(self):
        self.actions.append(("click",))

    async def wait_for(self, **kwargs):
        self.actions.append(("wait_for", kwargs))


class RecordingPage:
    def __init__(self):
        self.keyboard = RecordingKeyboard()
        self.locators = {
            'input[placeholder*="填写标题"]': RecordingLocator("title"),
            'p[data-placeholder*="输入正文描述"]': RecordingLocator("desc"),
            '#creator-editor-topic-container': RecordingLocator("topic-container"),
            '#creator-editor-topic-container .item': RecordingLocator("topic-item"),
        }

    def locator(self, selector):
        return self.locators[selector]


class AlwaysTimeoutTopicContainer(RecordingLocator):
    """话题面板永远超时的 locator，模拟服务端建议未加载/无匹配。"""

    def __init__(self):
        super().__init__("topic-container-timeout")
        self.calls = 0

    async def wait_for(self, **kwargs):
        self.calls += 1
        raise PlaywrightTimeoutError("topic container not visible")


class FakeLoginPage:
    url = "https://www.xiaohongshu.com/"

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_load_state(self, state=None, **kwargs):
        return None


class FakeLoginContext:
    def __init__(self):
        self.saved_path = None

    async def new_page(self):
        return FakeLoginPage()

    async def add_init_script(self, script):
        return None

    async def storage_state(self, path=None):
        self.saved_path = path

    async def close(self):
        return None


class FakeBrowser:
    def __init__(self, context):
        self._context = context

    async def new_context(self):
        return self._context

    async def close(self):
        return None


class FakeChromium:
    def __init__(self, context):
        self._browser = FakeBrowser(context)

    async def launch(self, **kwargs):
        return self._browser


class FakePlaywright:
    def __init__(self, context):
        self.chromium = FakeChromium(context)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class XiaohongshuUploaderTests(unittest.TestCase):
    def test_find_xhs_qrcode_locator_prefers_scan_sibling_inside_login_box(self):
        qrcode_locator = FakeLocator("qrcode", count=1, src="data:image/png;base64,abc")
        scan_text_locator = FakeLocator(
            "scan-text",
            count=1,
            children={
                "xpath=..//following-sibling::div//img": qrcode_locator,
            },
        )
        login_box_locator = FakeLocator(
            "login-box",
            count=1,
            children={
                "div:has-text('扫一扫')": scan_text_locator,
                "text:APP扫一扫登录": scan_text_locator,
            },
        )
        page = FakeLocator(
            "page",
            children={
                "div[class*='login-box']": login_box_locator,
                ".login-box-container": login_box_locator,
            },
        )

        locator = asyncio.run(xhs_main._find_xhs_qrcode_locator(page))
        self.assertIs(locator, qrcode_locator)

    def test_cookie_gen_saves_authenticated_state_without_parallel_revalidation(self):
        context = FakeLoginContext()
        account_file = xhs_main._resolve_account_file("account.json")
        with patch.object(xhs_main, "async_playwright", return_value=FakePlaywright(context)), \
             patch.object(xhs_main, "_save_xhs_qrcode", AsyncMock(return_value={"image_path": "qrcode.png"})), \
             patch.object(xhs_main, "remove_qrcode_file", return_value=False), \
             patch.object(xhs_main.XiaoHongShuBaseUploader, "is_login_completed", AsyncMock(return_value=True)), \
             patch.object(xhs_main, "cookie_auth", AsyncMock(side_effect=AssertionError("must not revalidate before closing login browser"))):
            result = asyncio.run(xhs_main.xiaohongshu_cookie_gen("account.json", poll_interval=0, max_checks=1))
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(context.saved_path, account_file)

    def test_setup_returns_detail_when_cookie_invalid_without_handle(self):
        with patch("uploader.xiaohongshu_uploader.main.os.path.exists", return_value=False):
            result = asyncio.run(
                xhs_main.xiaohongshu_setup(
                    "missing.json",
                    handle=False,
                    return_detail=True,
                )
            )
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "cookie_invalid")

    def test_setup_uses_login_flow_when_handle_is_true(self):
        login_result = {
            "success": True,
            "status": "success",
            "message": "ok",
            "account_file": "account.json",
            "qrcode": {"image_path": "qrcode.png"},
            "current_url": "https://creator.xiaohongshu.com/",
        }
        with patch("uploader.xiaohongshu_uploader.main.os.path.exists", return_value=False):
            with patch(
                "uploader.xiaohongshu_uploader.main.xiaohongshu_cookie_gen",
                new=AsyncMock(return_value=login_result),
            ) as mock_login:
                result = asyncio.run(
                    xhs_main.xiaohongshu_setup(
                        "account.json",
                        handle=True,
                        return_detail=True,
                    )
                )
        self.assertTrue(result["success"])
        mock_login.assert_awaited_once()

    def test_video_validate_upload_args_normalizes_video_and_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            video_path = Path(tmp_dir) / "demo.mp4"
            thumbnail_path = Path(tmp_dir) / "demo.png"
            cookie_path = Path(tmp_dir) / "account.json"
            video_path.write_bytes(b"video")
            thumbnail_path.write_bytes(b"image")
            cookie_path.write_text("{}")

            app = xhs_main.XiaoHongShuVideo(
                title="demo",
                file_path=str(video_path),
                tags=["xhs"],
                publish_date=0,
                account_file=str(cookie_path),
                thumbnail_path=str(thumbnail_path),
            )

            with patch(
                "uploader.xiaohongshu_uploader.main.cookie_auth",
                new=AsyncMock(return_value=True),
            ):
                asyncio.run(app.validate_upload_args())

        self.assertTrue(app.file_path.endswith("demo.mp4"))
        self.assertTrue(app.thumbnail_path.endswith("demo.png"))

    def test_note_uploader_exists_and_validates_required_fields(self):
        note_cls = getattr(xhs_main, "XiaoHongShuNote")
        app = note_cls(
            image_paths=[],
            note="",
            tags=[],
            publish_date=0,
            account_file="account.json",
        )

        with patch.object(app, "validate_login_and_strategy", new=AsyncMock(return_value=None)):
            with self.assertRaises(ValueError):
                asyncio.run(app.validate_upload_args())

    def test_video_fill_meta_uses_desc_then_first_tag(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
            desc="描述内容",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['input[placeholder*="填写标题"]'].actions,
            [("fill", "标题内容")],
        )
        self.assertEqual(
            page.locators['p[data-placeholder*="输入正文描述"]'].actions,
            [("click",)],
        )
        self.assertIn(("type", "描述内容", None), page.keyboard.actions)
        self.assertIn(("type", "#话题1", 30), page.keyboard.actions)
        self.assertEqual(
            page.locators['#creator-editor-topic-container .item'].actions,
            [("wait_for", {"state": "visible", "timeout": 5000}), ("click",)],
        )

    def test_video_fill_meta_can_fill_first_tag_without_desc(self):
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['p[data-placeholder*="输入正文描述"]'].actions,
            [("click",)],
        )
        self.assertNotIn(("type", "", None), page.keyboard.actions)
        self.assertIn(("type", "#话题1", 30), page.keyboard.actions)

    def test_video_fill_tags_skips_tag_when_topic_container_times_out(self):
        # 话题面板超时不应让整个发布失败：跳过该话题（保留纯文本），继续处理后续话题
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["超时话题", "正常话题"],
            publish_date=0,
            account_file="account.json",
            desc="描述内容",
        )
        page = RecordingPage()
        page.locators['#creator-editor-topic-container'] = AlwaysTimeoutTopicContainer()

        asyncio.run(app.fill_meta(page))  # 不应抛异常

        self.assertIn(("type", "#超时话题", 30), page.keyboard.actions)
        self.assertIn(("type", "#正常话题", 30), page.keyboard.actions)
        # 超时话题被跳过后，仍应有 Space 分隔，避免两个话题连在一起
        self.assertIn(("press", "Space"), page.keyboard.actions)

    def test_video_fill_tags_waits_longer_for_topic_suggestions(self):
        # 话题建议由服务端接口驱动，偶发加载慢；超时从 3s 提高到 8s
        app = xhs_main.XiaoHongShuVideo(
            title="标题内容",
            file_path="demo.mp4",
            tags=["话题1"],
            publish_date=0,
            account_file="account.json",
            desc="描述内容",
        )
        page = RecordingPage()

        asyncio.run(app.fill_meta(page))

        self.assertEqual(
            page.locators['#creator-editor-topic-container'].actions,
            [("wait_for", {"state": "visible", "timeout": 8000})],
        )
        self.assertEqual(
            page.locators['#creator-editor-topic-container .item'].actions,
            [("wait_for", {"state": "visible", "timeout": 5000}), ("click",)],
        )

    def test_note_title_defaults_do_not_override_explicit_title(self):
        app = xhs_main.XiaoHongShuNote(
            image_paths=["a.png"],
            note="正文",
            tags=[],
            publish_date=0,
            account_file="account.json",
            title="显式标题",
            desc="图文正文",
        )

        self.assertEqual(app.title, "显式标题")
        self.assertEqual(app.desc, "图文正文")


if __name__ == "__main__":
    unittest.main()
