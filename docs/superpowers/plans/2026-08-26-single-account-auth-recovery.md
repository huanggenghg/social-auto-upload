# Single-Account Auth Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every platform publish through one canonical account, detect pre-upload Weibo/Tencent login expiry accurately, and perform at most one safe forced-login retry.

**Architecture:** Account discovery collapses to one canonical `cookies/<uploader>/account.json` path with a one-file legacy fallback. Uploaders convert only pre-media login redirects into a shared structured result, while the orchestrator owns the single forced-login retry and never retries ambiguous post-submission failures. The existing successful Weibo account is migrated only after a fresh browser diagnosis confirms the two legacy files' identities.

**Tech Stack:** Python 3.11+, `unittest`, `unittest.mock`, Patchright async browser API, editable `opub` CLI, `openpyxl` for result verification.

---

## File map

- Create `tests/test_publish_config.py`: isolated single-account discovery contract.
- Modify `publish/config.py`: canonical-account selection and legacy fallback.
- Modify `tests/test_publish_dispatch.py`: forced-login behavior.
- Modify `publish/dispatch.py`: `force=True` login setup path.
- Modify `tests/test_base_uploader.py`: structured login-expired result contract.
- Modify `uploader/base_video.py`: shared exception, result builder, and `safe_to_retry` type.
- Modify `tests/test_tencent_uploader_base.py`: Tencent redirect, QR iframe, ready input, and timeout cases.
- Modify `uploader/tencent_uploader/main.py`: pre-file-selection login-state wait and structured mapping.
- Modify `tests/test_weibo_uploader.py`: Weibo explicit-state wait cases.
- Modify `tests/test_weibo_uploader_base.py`: Weibo structured result mapping.
- Modify `uploader/weibo_uploader/main.py`: explicit auth/upload waits for video and image paths.
- Modify `tests/test_publish_engine.py`: single-result orchestration and one-shot recovery policy.
- Modify `publish/orchestrator.py`: remove account loops and own the one safe retry.
- Modify `tests/test_publish_cli.py`: documentation contract for one account per platform.
- Modify `README.md`, `AGENT.md`, and `skills/opub-cli/SKILL.md`: remove multi-account guidance.
- Runtime-only move under `cookies/weibo_uploader/`: archive `account1.json`, promote `account2.json` to `account.json`; do not commit Cookie data.

### Task 1: Collapse account discovery to one account

**Files:**
- Create: `tests/test_publish_config.py`
- Modify: `publish/config.py:31-76`

- [ ] **Step 1: Write the failing discovery tests**

```python
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publish.config import _discover_account_files


class SingleAccountDiscoveryTests(unittest.TestCase):
    def _discover(self, root: Path) -> dict[str, str]:
        with patch("publish.config.BASE_DIR", root):
            return _discover_account_files()

    def test_canonical_account_wins_over_all_legacy_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploader_dir = root / "cookies" / "weibo_uploader"
            uploader_dir.mkdir(parents=True)
            (uploader_dir / "account.json").write_text("{}", encoding="utf-8")
            (uploader_dir / "account1.json").write_text("{}", encoding="utf-8")
            (root / "cookies" / "weibo_flat.json").write_text("{}", encoding="utf-8")

            discovered = self._discover(root)

        self.assertEqual(discovered["weibo_account"], "cookies/weibo_uploader/account.json")
        self.assertNotIn(",", discovered["weibo_account"])

    def test_one_legacy_file_remains_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploader_dir = root / "cookies" / "weibo_uploader"
            uploader_dir.mkdir(parents=True)
            (uploader_dir / "old.json").write_text("{}", encoding="utf-8")

            discovered = self._discover(root)

        self.assertEqual(discovered["weibo_account"], "cookies/weibo_uploader/old.json")

    def test_multiple_legacy_files_without_canonical_choose_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploader_dir = root / "cookies" / "weibo_uploader"
            uploader_dir.mkdir(parents=True)
            (uploader_dir / "account1.json").write_text("{}", encoding="utf-8")
            (uploader_dir / "account2.json").write_text("{}", encoding="utf-8")

            discovered = self._discover(root)

        self.assertNotIn("weibo_account", discovered)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_publish_config`

Expected: the canonical and multi-legacy tests fail because `_discover_account_files()` currently joins all matching JSON paths with commas.

- [ ] **Step 3: Implement canonical-first discovery**

Replace the discovery body in `publish/config.py` with:

```python
def _discover_single_account_file(cookies_dir, platform: str, prefix: str):
    account_dir = cookies_dir / PLATFORM_ACCOUNT_SUBDIRS[platform]
    canonical = account_dir / "account.json"
    if canonical.is_file():
        return canonical

    flat_files = sorted(path for path in cookies_dir.glob(f"{prefix}*.json") if path.is_file())
    nested_legacy = sorted(
        path
        for path in account_dir.glob("*.json")
        if path.is_file() and path.name != "account.json"
    ) if account_dir.exists() else []
    legacy_files = flat_files + [path for path in nested_legacy if path not in flat_files]
    return legacy_files[0] if len(legacy_files) == 1 else None


def _discover_account_files() -> Dict[str, str]:
    cookies_dir = BASE_DIR / "cookies"
    platform_prefixes = {
        "douyin": "douyin_",
        "kuaishou": "kuaishou_",
        "xiaohongshu": "xiaohongshu_",
        "weibo": "weibo_",
        "tencent": "tencent_",
        "baijiahao": "baijiahao_",
        "bilibili": "bilibili_",
        "tk": "tk_",
    }

    platforms = {}
    for platform, prefix in platform_prefixes.items():
        account_file = _discover_single_account_file(cookies_dir, platform, prefix)
        if account_file is not None:
            platforms[f"{platform}_account"] = str(account_file.relative_to(BASE_DIR))
    return platforms
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `.venv/bin/python -m unittest -v tests.test_publish_config`

Expected: `Ran 3 tests ... OK`.

- [ ] **Step 5: Commit the discovery contract**

```bash
git add publish/config.py tests/test_publish_config.py
git commit -m "feat: discover one account per platform"
```

### Task 2: Add an explicit forced-login path

**Files:**
- Modify: `tests/test_publish_dispatch.py:28-75`
- Modify: `publish/dispatch.py:25-57`

- [ ] **Step 1: Write the failing forced-login test**

Add to `EnsureLoginTests`:

```python
    def test_force_skips_cookie_check_and_runs_setup(self):
        import asyncio
        with patch("os.path.exists", return_value=True), \
             patch("importlib.import_module") as mock_import:
            mock_module = mock_import.return_value
            mock_module.cookie_auth = AsyncMock(return_value=True)
            mock_module.douyin_setup = AsyncMock(return_value=True)

            result = asyncio.run(
                ensure_login(
                    "douyin",
                    "cookies/douyin_uploader/account.json",
                    force=True,
                )
            )

        self.assertTrue(result)
        mock_module.cookie_auth.assert_not_awaited()
        mock_module.douyin_setup.assert_awaited_once_with(
            "cookies/douyin_uploader/account.json",
            handle=True,
        )
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_publish_dispatch.EnsureLoginTests.test_force_skips_cookie_check_and_runs_setup`

Expected: `TypeError: ensure_login() got an unexpected keyword argument 'force'`.

- [ ] **Step 3: Implement `force` without changing the default path**

Update both public functions in `publish/dispatch.py`:

```python
async def ensure_login(platform: str, account_file: str, force: bool = False) -> bool:
    """确保平台已登录；force=True 时跳过 Cookie 预检并直接扫码。"""
    entry = _PLATFORM_LOGIN.get(platform)
    if entry is None:
        return False

    module_path, check_name, setup_name = entry
    module = importlib.import_module(module_path)

    if not force and os.path.exists(account_file):
        check_func = getattr(module, check_name)
        if await check_func(account_file):
            return True

    print(
        f"[opub] {platform} 未登录,即将打开浏览器等待扫码登录(最长约 5 分钟)。"
        f"若由 Agent 调用,请确保工具超时不低于 360 秒",
        file=sys.stderr,
    )
    setup_func = getattr(module, setup_name)
    return await setup_func(account_file, handle=True)


async def ensure_account_login(platform: str, account_file: str, force: bool = False) -> bool:
    resolved_account = resolve_path(account_file)
    return await ensure_login(platform, resolved_account, force=force)
```

- [ ] **Step 4: Run dispatch tests and verify GREEN**

Run: `.venv/bin/python -m unittest -v tests.test_publish_dispatch`

Expected: all dispatch tests pass, including the new forced path and existing non-forced checks.

- [ ] **Step 5: Commit the forced-login API**

```bash
git add publish/dispatch.py tests/test_publish_dispatch.py
git commit -m "feat: support forced account login"
```

### Task 3: Define the shared safe-retry result

**Files:**
- Modify: `tests/test_base_uploader.py`
- Modify: `uploader/base_video.py:15-33`

- [ ] **Step 1: Write the failing result-contract test**

Add this import and test to `tests/test_base_uploader.py`:

```python
from uploader.base_video import build_login_expired_result


class LoginExpiredResultTests(unittest.TestCase):
    def test_result_is_structured_and_safe_before_media_submission(self):
        self.assertEqual(
            build_login_expired_result(),
            {
                "success": False,
                "message": "cookie 已失效，请重新扫码登录",
                "account_issue": True,
                "issue_type": "login_expired",
                "safe_to_retry": True,
            },
        )
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_base_uploader.LoginExpiredResultTests`

Expected: import failure because `build_login_expired_result` does not exist.

- [ ] **Step 3: Add the exception, type field, and builder**

Update `uploader/base_video.py`:

```python
class PlatformResultExtras(PlatformResult, total=False):
    result_url: str
    result_id: str
    account_issue: bool
    issue_type: str
    safe_to_retry: bool


class LoginExpiredError(RuntimeError):
    """登录在选择任何素材之前失效，可由编排器安全恢复一次。"""


def build_login_expired_result(
    message: str = "cookie 已失效，请重新扫码登录",
) -> PlatformResultExtras:
    return {
        "success": False,
        "message": message,
        "account_issue": True,
        "issue_type": "login_expired",
        "safe_to_retry": True,
    }
```

- [ ] **Step 4: Run the focused base tests and verify GREEN**

Run: `.venv/bin/python -m unittest -v tests.test_base_uploader`

Expected: all base uploader tests pass.

- [ ] **Step 5: Commit the result contract**

```bash
git add uploader/base_video.py tests/test_base_uploader.py
git commit -m "feat: define safe login-expired results"
```

### Task 4: Detect Tencent login expiry before selecting the video

**Files:**
- Modify: `tests/test_tencent_uploader_base.py`
- Modify: `uploader/tencent_uploader/main.py:450-470,810-850`

- [ ] **Step 1: Write failing state-wait tests**

Import `_wait_for_tencent_upload_input` and add:

```python
class TencentUploadEntryTests(unittest.TestCase):
    class Locator:
        def __init__(self, count):
            self._count = count

        @property
        def first(self):
            return self

        async def count(self):
            return self._count

    class Page:
        def __init__(self, url, counts):
            self.url = url
            self.counts = counts

        def locator(self, selector):
            return TencentUploadEntryTests.Locator(self.counts.get(selector, 0))

        async def wait_for_timeout(self, _milliseconds):
            return None

    def test_login_redirect_is_login_expired(self):
        import asyncio
        from uploader.base_video import LoginExpiredError
        from uploader.tencent_uploader.main import _wait_for_tencent_upload_input

        page = self.Page("https://channels.weixin.qq.com/login.html", {})
        with self.assertRaises(LoginExpiredError):
            asyncio.run(_wait_for_tencent_upload_input(page, timeout_ms=0))

    def test_qrconnect_iframe_is_login_expired(self):
        import asyncio
        from uploader.base_video import LoginExpiredError
        from uploader.tencent_uploader.main import _wait_for_tencent_upload_input

        page = self.Page(
            "https://channels.weixin.qq.com/platform/post/create",
            {'iframe[src*="qrconnect"]': 1},
        )
        with self.assertRaises(LoginExpiredError):
            asyncio.run(_wait_for_tencent_upload_input(page, timeout_ms=0))

    def test_file_input_is_returned_when_upload_page_is_ready(self):
        import asyncio
        from uploader.tencent_uploader.main import _wait_for_tencent_upload_input

        page = self.Page(
            "https://channels.weixin.qq.com/platform/post/create",
            {'input[type="file"]': 1},
        )
        result = asyncio.run(_wait_for_tencent_upload_input(page, timeout_ms=0))
        self.assertEqual(asyncio.run(result.count()), 1)

    def test_unknown_timeout_is_not_classified_as_login_expired(self):
        import asyncio
        from uploader.tencent_uploader.main import _wait_for_tencent_upload_input

        page = self.Page("https://channels.weixin.qq.com/platform/post/create", {})
        with self.assertRaisesRegex(RuntimeError, "上传控件"):
            asyncio.run(_wait_for_tencent_upload_input(page, timeout_ms=0))
```

Also add a mapping test to `TencentVideoUploadTests`:

```python
    def test_pre_upload_login_expiry_returns_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager
        from uploader.base_video import LoginExpiredError

        @asynccontextmanager
        async def fake_session():
            yield object()

        uploader = TencentVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(
                 uploader,
                 "upload_video_content",
                 AsyncMock(side_effect=LoginExpiredError("cookie 已失效，请重新扫码登录")),
             ):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])
```

- [ ] **Step 2: Run Tencent tests and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_tencent_uploader_base.TencentUploadEntryTests tests.test_tencent_uploader_base.TencentVideoUploadTests.test_pre_upload_login_expiry_returns_safe_retry_result`

Expected: import failure for `_wait_for_tencent_upload_input`, and the mapping test lacks `issue_type`.

- [ ] **Step 3: Implement the pre-upload state wait**

Add to `uploader/tencent_uploader/main.py`:

```python
from uploader.base_video import (
    BaseBrowserUploader,
    LoginExpiredError,
    PlatformResultExtras,
    PublishStrategy,
    _msg,
    build_login_expired_result,
)


async def _wait_for_tencent_upload_input(
    page: Page,
    timeout_ms: int = 30_000,
    poll_interval_ms: int = 250,
):
    elapsed = 0
    while True:
        if "login.html" in (page.url or "").lower():
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        login_iframe = page.locator('iframe[src*="qrconnect"]').first
        if await login_iframe.count():
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        file_input = page.locator('input[type="file"]').first
        if await file_input.count():
            return file_input

        if elapsed >= timeout_ms:
            raise RuntimeError("视频号发布页加载超时：未找到视频上传控件")

        wait_ms = min(poll_interval_ms, timeout_ms - elapsed)
        await page.wait_for_timeout(wait_ms)
        elapsed += wait_ms
```

Replace `open_upload_page()` with an explicit readiness wait, while keeping `upload_video_file(page, file_path)` unchanged because `handle_upload_error()` also calls it:

```python
    async def open_upload_page(self, page: Page) -> None:
        await page.goto(TENCENT_UPLOAD_URL)
        await _wait_for_tencent_upload_input(page)

    async def upload_video_content(self, page: Page) -> None:
        await self.open_upload_page(page)
        tencent_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}"))
        await self.upload_video_file(page, self.file_path)
        await self.prepare_video_for_publish(page)
        await self.wait_for_upload_complete(page)
        await self.set_thumbnail(page)

        if self.publish_strategy == TENCENT_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_tencent(page, self.publish_date)

        await self.set_short_title(page, self.title, self.short_title)
        await self.submit_publish(page)

        try:
            short_url = await self._fetch_published_video_short_url(page)
            if short_url:
                self._result_url = short_url
                tencent_logger.success(_msg("🥳", f"获取视频链接: {short_url}"))
        except Exception as exc:
            tencent_logger.warning(_msg("⚠️", f"获取视频链接失败: {exc}"))
```

In `TencentVideo.upload()`, insert this exception branch before the generic branch:

```python
        except LoginExpiredError:
            result.update(build_login_expired_result())
            tencent_logger.error(_msg("❌", result["message"]))
        except Exception as exc:
            result["message"] = str(exc)
            tencent_logger.error(_msg("❌", f"上传失败: {exc}"))
```

The only `set_input_files()` call occurs after the helper returns, so neither later upload nor publish errors can receive `safe_to_retry=True`.

- [ ] **Step 4: Run Tencent tests and verify GREEN**

Run: `.venv/bin/python -m unittest -v tests.test_tencent_uploader_base`

Expected: all Tencent uploader tests pass.

- [ ] **Step 5: Commit Tencent detection**

```bash
git add uploader/tencent_uploader/main.py tests/test_tencent_uploader_base.py
git commit -m "fix: detect tencent login expiry before upload"
```

### Task 5: Detect Weibo login expiry before video or image selection

**Files:**
- Modify: `tests/test_weibo_uploader.py`
- Modify: `tests/test_weibo_uploader_base.py`
- Modify: `uploader/weibo_uploader/main.py:1-140,248-270,514-535,579-610,638-660`

- [ ] **Step 1: Extend the fake page and write failing explicit-state tests**

Add `wait_for_timeout` to `FakePage` in `tests/test_weibo_uploader.py`:

```python
    async def wait_for_timeout(self, _milliseconds):
        return None
```

Then add:

```python
class WeiboUploadEntryTests(unittest.TestCase):
    def test_newlogin_retcode_is_login_expired(self):
        from uploader.base_video import LoginExpiredError

        page = FakePage("https://weibo.com/newlogin?retcode=6102")
        with self.assertRaises(LoginExpiredError):
            asyncio.run(weibo_main._wait_for_weibo_upload_button(page, timeout_ms=0))

    def test_visible_upload_button_is_returned(self):
        page = FakePage(
            "https://weibo.com/upload/channel",
            {
                weibo_main.WEIBO_UPLOAD_BUTTON_SELECTOR: FakeLocator(
                    count=1,
                    visible=True,
                ),
            },
        )
        locator = asyncio.run(weibo_main._wait_for_weibo_upload_button(page, timeout_ms=0))
        self.assertTrue(asyncio.run(locator.is_visible()))

    def test_plain_timeout_is_not_safe_login_expiry(self):
        page = FakePage("https://weibo.com/upload/channel")
        with self.assertRaisesRegex(RuntimeError, "上传入口"):
            asyncio.run(weibo_main._wait_for_weibo_upload_button(page, timeout_ms=0))
```

Add to `WeiboVideoUploadTests` in `tests/test_weibo_uploader_base.py`:

```python
    def test_pre_upload_login_expiry_returns_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager
        from uploader.base_video import LoginExpiredError

        @asynccontextmanager
        async def fake_session():
            yield object()

        uploader = WeiboVideo(
            title="t", file_path="/fake.mp4", tags=[], publish_date=0,
            account_file="/fake.json", desc="", publish_strategy=PublishStrategy.IMMEDIATE,
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(
                 uploader,
                 "upload_video_content",
                 AsyncMock(side_effect=LoginExpiredError("cookie 已失效，请重新扫码登录")),
             ):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])
```

Add this separate test to `WeiboNoteUploadTests`:

```python
    def test_pre_upload_login_expiry_returns_safe_retry_result(self):
        import asyncio
        from contextlib import asynccontextmanager
        from uploader.base_video import LoginExpiredError

        @asynccontextmanager
        async def fake_session():
            yield object()

        uploader = WeiboNote(
            image_paths=["/fake.jpg"], note="test note", tags=[],
            publish_date=0, account_file="/fake.json",
        )
        with patch.object(uploader, "validate_upload_args", AsyncMock()), \
             patch.object(uploader, "_browser_session", return_value=fake_session()), \
             patch.object(
                 uploader,
                 "upload_note_content",
                 AsyncMock(side_effect=LoginExpiredError("cookie 已失效，请重新扫码登录")),
             ):
            result = asyncio.run(uploader.upload())

        self.assertEqual(result["issue_type"], "login_expired")
        self.assertTrue(result["safe_to_retry"])
```

- [ ] **Step 2: Run Weibo tests and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_weibo_uploader.WeiboUploadEntryTests tests.test_weibo_uploader_base.WeiboVideoUploadTests.test_pre_upload_login_expiry_returns_safe_retry_result tests.test_weibo_uploader_base.WeiboNoteUploadTests.test_pre_upload_login_expiry_returns_safe_retry_result`

Expected: missing `_wait_for_weibo_upload_button` and missing structured fields.

- [ ] **Step 3: Add shared Weibo page-state waits**

Update imports and add these helpers in `uploader/weibo_uploader/main.py`:

```python
from patchright.async_api import Page, async_playwright

from uploader.base_video import (
    BaseBrowserUploader,
    LoginExpiredError,
    PlatformResultExtras,
    PublishStrategy,
    _msg,
    build_login_expired_result,
)


async def _is_weibo_login_page(page: Page) -> bool:
    current_url = (page.url or "").lower()
    if any(marker in current_url for marker in WEIBO_LOGIN_URL_MARKERS):
        return True

    for marker in (
        page.locator('text="登录"').first,
        page.locator('text="扫码登录"').first,
        page.locator('a[href*="login"]').first,
    ):
        if await _is_visible(marker):
            return True
    return False


async def _wait_for_weibo_upload_button(
    page: Page,
    timeout_ms: int = 15_000,
    poll_interval_ms: int = 250,
):
    elapsed = 0
    while True:
        if await _is_weibo_login_page(page):
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        upload_button = page.locator(WEIBO_UPLOAD_BUTTON_SELECTOR).first
        if await _is_visible(upload_button):
            return upload_button

        if elapsed >= timeout_ms:
            raise RuntimeError("微博发布页加载超时：未找到视频上传入口")

        wait_ms = min(poll_interval_ms, timeout_ms - elapsed)
        await page.wait_for_timeout(wait_ms)
        elapsed += wait_ms


async def _wait_for_weibo_image_input(
    page: Page,
    selectors: list[str],
    timeout_ms: int = 15_000,
    poll_interval_ms: int = 250,
):
    elapsed = 0
    while True:
        if await _is_weibo_login_page(page):
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        for selector in selectors:
            upload_input = page.locator(selector).first
            if await upload_input.count():
                return upload_input

        if elapsed >= timeout_ms:
            raise RuntimeError("微博发布页加载超时：未找到图片上传入口")

        wait_ms = min(poll_interval_ms, timeout_ms - elapsed)
        await page.wait_for_timeout(wait_ms)
        elapsed += wait_ms
```

Replace `_is_weibo_auth_page_valid()` so it uses the same state rules:

```python
async def _is_weibo_auth_page_valid(page: Page) -> bool:
    """只有进入发布页并看到上传入口，才认为微博 cookie 仍然有效。"""
    if await _is_weibo_login_page(page):
        return False
    upload_button = page.locator(WEIBO_UPLOAD_BUTTON_SELECTOR).first
    return await _is_visible(upload_button)
```

Override `WeiboBaseUploader.cookie_auth()` so timeout remains distinct from a login redirect:

```python
    @classmethod
    async def cookie_auth(cls, account_file: str) -> bool:
        if not os.path.exists(account_file):
            return False
        async with async_playwright() as playwright:
            browser = await cls._launch_browser(playwright, headless=LOCAL_CHROME_HEADLESS)
            try:
                context = await cls._init_context(browser, account_file)
                page = await context.new_page()
                await page.goto(cls.UPLOAD_URL)
                try:
                    await _wait_for_weibo_upload_button(page)
                except LoginExpiredError:
                    return False
                return True
            finally:
                await browser.close()
```

Do not catch `RuntimeError` from the helper here: a neutral page timeout must surface as a page error instead of being silently treated as an expired login.

- [ ] **Step 4: Put the waits before all media submission calls**

In `WeiboVideo.upload_video_content()` replace the fixed sleep and selector wait with:

```python
        await page.goto(WEIBO_UPLOAD_CHANNEL_URL)
        weibo_logger.info(_msg("🔍", "查找上传视频按钮..."))
        upload_btn = await _wait_for_weibo_upload_button(page)

        async with page.expect_file_chooser(timeout=30000) as fc_info:
            await upload_btn.click()
            weibo_logger.info(_msg("✅", "已点击上传视频按钮"))

        file_chooser = await fc_info.value
        await file_chooser.set_files(self.file_path)
```

In `WeiboNote.upload_note_content()` replace the fixed sleep and selector loop with:

```python
        await page.goto(WEIBO_MAIN_URL)
        image_upload_selectors = [
            'input[type="file"][accept*="image"]',
            'input[type="file"][accept*=".jpg"]',
            'input[type="file"][accept*=".png"]',
        ]
        upload_input = await _wait_for_weibo_image_input(page, image_upload_selectors)
        await upload_input.set_input_files(self.image_paths)
```

Replace `WeiboVideo.upload()` with this complete method so validation is also covered by structured error handling:

```python
    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras"""
        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            weibo_logger.info(_msg("🧍", "检查 cookie 和视频文件..."))
            await self.validate_upload_args()
            weibo_logger.info(_msg("🥳", "上传前检查通过"))

            async with self._browser_session(save_on_success_only=True) as page:
                video_link = await self.upload_video_content(page)
                result["success"] = True
                if video_link:
                    result["result_url"] = video_link
                    result["message"] = f"发布成功，视频链接: {video_link}"
                else:
                    result["message"] = "发布成功，但未获取到视频链接"
            weibo_logger.success(_msg("🥳", "cookie 更新完毕"))
        except LoginExpiredError:
            result.update(build_login_expired_result())
            weibo_logger.error(_msg("❌", result["message"]))
        except Exception as exc:
            result["message"] = str(exc)
            weibo_logger.error(_msg("❌", f"上传失败: {exc}"))

        return result
```

Replace `WeiboNote.upload()` with:

```python
    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras"""
        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            weibo_logger.info(_msg("🧍", "检查 cookie 和图片文件..."))
            await self.validate_upload_args()
            weibo_logger.info(_msg("🥳", "上传前检查通过"))

            async with self._browser_session(save_on_success_only=True) as page:
                await self.upload_note_content(page)
                result["success"] = True
                result["message"] = "发布成功"
            weibo_logger.success(_msg("🥳", "cookie 更新完毕"))
        except LoginExpiredError:
            result.update(build_login_expired_result())
            weibo_logger.error(_msg("❌", result["message"]))
        except Exception as exc:
            result["message"] = str(exc)
            weibo_logger.error(_msg("❌", f"上传失败: {exc}"))

        return result
```

Keep `safe_to_retry` confined to `LoginExpiredError`; exceptions after `file_chooser.set_files()` or `set_input_files()` continue through the generic branch.

- [ ] **Step 5: Run Weibo tests and verify GREEN**

Run: `.venv/bin/python -m unittest -v tests.test_weibo_uploader tests.test_weibo_uploader_base`

Expected: all Weibo tests pass, including `newlogin?retcode=6102`, visible upload entry, neutral timeout, video mapping, and image mapping.

- [ ] **Step 6: Commit Weibo detection**

```bash
git add uploader/weibo_uploader/main.py tests/test_weibo_uploader.py tests/test_weibo_uploader_base.py
git commit -m "fix: detect weibo login expiry before upload"
```

### Task 6: Replace multi-account orchestration with one safe recovery

**Files:**
- Modify: `tests/test_publish_engine.py:395-540`
- Modify: `publish/orchestrator.py:50-120`

- [ ] **Step 1: Write failing single-account and recovery tests**

Add to `AccountLoginFlowTests`:

```python
    def _weibo_params(self):
        return {
            "enabled_platforms": ["weibo"],
            "platforms": {"weibo_account": "cookies/weibo_uploader/account.json"},
            "content_type": "video",
            "video_file": "videos/demo.mp4",
            "title": "标题",
            "desc": "描述",
            "tags": [],
            "publish_strategy": "immediate",
            "publish_time": None,
            "convert_to_video": False,
        }

    def test_legacy_comma_value_falls_back_to_one_canonical_account(self):
        import contextlib
        import io

        params = self._weibo_params()
        params["platforms"]["weibo_account"] = (
            "cookies/weibo_uploader/account1.json, "
            "cookies/weibo_uploader/account2.json"
        )
        canonical = "cookies/weibo_uploader/account.json"
        stdout = io.StringIO()
        ensure_login = AsyncMock(return_value=True)
        publish = AsyncMock(return_value={"success": True, "message": "ok"})
        with patch("publish.orchestrator.default_account_file", return_value=canonical), \
             patch("publish.orchestrator.ensure_account_login", new=ensure_login), \
             patch("publish.orchestrator.publish_to_platform", new=publish), \
             contextlib.redirect_stdout(stdout):
            results = publish_all.run_async_for_test(
                publish_all.publish_one_item(params)
            )

        self.assertEqual(list(results), ["weibo"])
        ensure_login.assert_awaited_once_with("weibo", canonical)
        self.assertEqual(publish.await_count, 1)
        self.assertNotIn("账号 1/", stdout.getvalue())

    def test_safe_login_expiry_forces_login_and_retries_once(self):
        expired = {
            "success": False,
            "message": "cookie 已失效，请重新扫码登录",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }
        ensure_login = AsyncMock(side_effect=[True, True])
        publish = AsyncMock(side_effect=[expired, {"success": True, "message": "ok"}])

        with patch("publish.orchestrator.ensure_account_login", new=ensure_login), \
             patch("publish.orchestrator.publish_to_platform", new=publish):
            results = publish_all.run_async_for_test(
                publish_all.publish_one_item(self._weibo_params())
            )

        self.assertTrue(results["weibo"]["success"])
        self.assertEqual(publish.await_count, 2)
        self.assertEqual(
            ensure_login.await_args_list[1].kwargs,
            {"force": True},
        )

    def test_forced_login_failure_returns_auth_error_without_republish(self):
        expired = {
            "success": False,
            "message": "cookie 已失效，请重新扫码登录",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }
        ensure_login = AsyncMock(side_effect=[True, False])
        publish = AsyncMock(return_value=expired)

        with patch("publish.orchestrator.ensure_account_login", new=ensure_login), \
             patch("publish.orchestrator.publish_to_platform", new=publish):
            result = publish_all.run_async_for_test(
                publish_all.publish_one_item(self._weibo_params())
            )["weibo"]

        self.assertEqual(publish.await_count, 1)
        self.assertEqual(result["error_code"], "AUTH-001")

    def test_second_login_expiry_does_not_trigger_a_third_publish(self):
        expired = {
            "success": False,
            "message": "cookie 已失效，请重新扫码登录",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": True,
        }
        ensure_login = AsyncMock(side_effect=[True, True])
        publish = AsyncMock(side_effect=[expired, expired])

        with patch("publish.orchestrator.ensure_account_login", new=ensure_login), \
             patch("publish.orchestrator.publish_to_platform", new=publish):
            result = publish_all.run_async_for_test(
                publish_all.publish_one_item(self._weibo_params())
            )["weibo"]

        self.assertEqual(publish.await_count, 2)
        self.assertEqual(ensure_login.await_count, 2)
        self.assertEqual(result["issue_type"], "login_expired")

    def test_non_safe_failure_never_forces_login(self):
        failure = {
            "success": False,
            "message": "上传失败",
            "account_issue": True,
            "issue_type": "login_expired",
            "safe_to_retry": False,
        }
        ensure_login = AsyncMock(return_value=True)
        publish = AsyncMock(return_value=failure)

        with patch("publish.orchestrator.ensure_account_login", new=ensure_login), \
             patch("publish.orchestrator.publish_to_platform", new=publish):
            publish_all.run_async_for_test(publish_all.publish_one_item(self._weibo_params()))

        self.assertEqual(ensure_login.await_count, 1)
        self.assertEqual(publish.await_count, 1)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_publish_engine.AccountLoginFlowTests`

Expected: recovery tests fail because the orchestrator never calls `force=True`; existing implementation still contains an account loop.

- [ ] **Step 3: Implement single-account orchestration and one recovery**

Add above `publish_one_item()`:

```python
def _is_safe_login_expiry(result: Dict[str, Any]) -> bool:
    return (
        not result.get("success", False)
        and result.get("account_issue") is True
        and result.get("issue_type") == "login_expired"
        and result.get("safe_to_retry") is True
    )


def _auth_failure(platform_name: str, login_error: str | None = None) -> Dict[str, Any]:
    message = f"登录失败: {platform_name}"
    if login_error:
        message += f" - {login_error}"
    return {
        "success": False,
        "message": message,
        "account_issue": True,
        "error_code": "AUTH-001",
    }
```

Replace the account list and nested loop in `publish_one_item()` with:

```python
        account_key = f"{platform}_account"
        configured_account = video_params["platforms"].get(account_key, "").strip()
        account_file = "" if "," in configured_account else configured_account
        if not account_file:
            default_file = default_account_file(platform)
            if default_file is None:
                results[platform] = {
                    "success": False,
                    "message": f"未配置 {platform} 账号",
                    "account_issue": True,
                    "error_code": "AUTH-002",
                }
                print("  ❌ 失败: 未配置账号")
                continue
            account_file = default_file
            print(f"  ℹ️ 未发现 {platform_name} 账号文件，将触发扫码登录: {default_file}")

        print(f"[{i}/{total}] 发布到 {platform_name}...")
        platform_params = {**video_params, "account_file": account_file}

        if platform_requires_account_login(platform):
            login_error = None
            try:
                login_ok = await ensure_account_login(platform, account_file)
            except Exception as exc:
                login_ok = False
                login_error = str(exc) or exc.__class__.__name__
            if not login_ok:
                result = _auth_failure(platform_name, login_error)
                results[platform] = result
                print_error(
                    "AUTH-001",
                    result["message"],
                    f"引导用户在弹出的浏览器中完成 {platform_name} 扫码登录后重试",
                )
                continue

        result = await publish_to_platform(platform, platform_params)
        if _is_safe_login_expiry(result):
            recovery_error = None
            try:
                recovered = await ensure_account_login(platform, account_file, force=True)
            except Exception as exc:
                recovered = False
                recovery_error = str(exc) or exc.__class__.__name__

            if recovered:
                result = await publish_to_platform(platform, platform_params)
            else:
                result = _auth_failure(platform_name, recovery_error)
                print_error(
                    "AUTH-001",
                    result["message"],
                    f"引导用户在弹出的浏览器中完成 {platform_name} 扫码登录后重试",
                )

        results[platform] = result
        if result["success"]:
            print("  ✅ 成功")
        else:
            print(f"  ❌ 失败: {result['message']}")
```

Do not run the recovery predicate again after the second `publish_to_platform()` call.

- [ ] **Step 4: Run orchestration tests and verify GREEN**

Run: `.venv/bin/python -m unittest -v tests.test_publish_engine.AccountLoginFlowTests tests.test_publish_engine.PublishFailurePolicyTests`

Expected: all selected tests pass; recovery performs exactly two publishes at most.

- [ ] **Step 5: Commit orchestration**

```bash
git add publish/orchestrator.py tests/test_publish_engine.py
git commit -m "feat: recover expired login once per platform"
```

### Task 7: Update the public single-account contract

**Files:**
- Modify: `tests/test_publish_cli.py:201-220`
- Modify: `README.md:18,38`
- Modify: `AGENT.md:68-72`
- Modify: `skills/opub-cli/SKILL.md:44,76,118-126`

- [ ] **Step 1: Write the failing documentation contract test**

Add to `SkillDocBlackboxTests`:

```python
    def test_documents_one_account_per_platform(self):
        paths = [
            Path("README.md"),
            Path("AGENT.md"),
            Path("skills/opub-cli/SKILL.md"),
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertIn("每个平台只自动发现一个", text, str(path))
            self.assertNotIn("微博多账号", text, str(path))
            self.assertNotIn("每个账号各发一遍", text, str(path))
```

- [ ] **Step 2: Run the test and verify RED**

Run: `.venv/bin/python -m unittest -v tests.test_publish_cli.SkillDocBlackboxTests.test_documents_one_account_per_platform`

Expected: all three documents lack the new sentence; README and skill still contain multi-account wording.

- [ ] **Step 3: Replace the account guidance**

Use this exact sentence in all three documents:

```text
每个平台只自动发现一个规范账号文件；未发现账号时，发布流程会引导扫码并写入对应上传器目录的 account.json。
```

In the README and skill platform tables, change the Weibo note to:

```text
单账号自动发现
```

Delete this skill bullet entirely:

```text
- 微博多账号发布时，同一视频会为每个账号各发一遍。
```

- [ ] **Step 4: Run documentation and CLI tests**

Run: `.venv/bin/python -m unittest -v tests.test_publish_cli`

Expected: all CLI and skill-document tests pass.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md AGENT.md skills/opub-cli/SKILL.md tests/test_publish_cli.py
git commit -m "docs: document one account per platform"
```

### Task 8: Run regression checks and migrate the Weibo Cookie safely

**Files:**
- Runtime move only: `cookies/weibo_uploader/account1.json`
- Runtime move only: `cookies/weibo_uploader/account2.json`
- Runtime create: `cookies/weibo_uploader/archive/`
- Runtime create: `cookies/weibo_uploader/account.json`

- [ ] **Step 1: Run all tests except the one known hanging Baijiahao case**

Run:

```bash
.venv/bin/python - <<'PY'
import unittest

excluded = {
    "tests.test_baijiahao_uploader_base.BaiJiaHaoWaitTimeoutTests.test_uploading_video_raises_on_timeout",
}

def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item

discovered = unittest.defaultTestLoader.discover("tests")
selected = [test for test in flatten(discovered) if test.id() not in excluded]
result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(selected))
if not result.wasSuccessful():
    raise SystemExit(1)
print(f"verified={result.testsRun} excluded={sorted(excluded)}")
PY
```

Expected: selected suite reports `OK`; output names exactly one excluded test. Do not run the excluded test without a process timeout because `@async_retry(timeout=300)` repeatedly catches its immediate `TimeoutError`.

- [ ] **Step 2: Verify the CLI surface stayed account-selector-free**

Run: `.venv/bin/opub --help`

Expected: exit code 0; help contains `--platforms`, `--video`, and `--schedule`; it does not contain `--account-file` or an account index option.

- [ ] **Step 3: Re-diagnose both Weibo files before moving either one**

Run:

```bash
.venv/bin/python - <<'PY'
import asyncio
from pathlib import Path

from patchright.async_api import async_playwright
from uploader.base_video import LoginExpiredError
from uploader.weibo_uploader.main import (
    WEIBO_UPLOAD_CHANNEL_URL,
    WeiboBaseUploader,
    _wait_for_weibo_upload_button,
)

ROOT = Path("cookies/weibo_uploader").resolve()
CANDIDATES = [ROOT / "account1.json", ROOT / "account2.json"]

async def classify(path, playwright):
    browser = await WeiboBaseUploader._launch_browser(playwright, headless=True)
    try:
        context = await WeiboBaseUploader._init_context(browser, str(path))
        page = await context.new_page()
        await page.goto(WEIBO_UPLOAD_CHANNEL_URL)
        try:
            await _wait_for_weibo_upload_button(page, timeout_ms=15_000)
        except LoginExpiredError:
            return "expired"
        return "valid"
    finally:
        await browser.close()

async def main():
    assert all(path.is_file() for path in CANDIDATES), CANDIDATES
    async with async_playwright() as playwright:
        results = {path.name: await classify(path, playwright) for path in CANDIDATES}
    print(results)
    assert results == {"account1.json": "expired", "account2.json": "valid"}, results

asyncio.run(main())
PY
```

Expected: `{'account1.json': 'expired', 'account2.json': 'valid'}`. If the assertion fails, stop without moving any file.

- [ ] **Step 4: Archive account1 and promote account2 with guarded exact paths**

Run only after Step 3 succeeds:

```bash
.venv/bin/python - <<'PY'
from datetime import datetime
from pathlib import Path

root = Path("cookies/weibo_uploader").resolve()
invalid = root / "account1.json"
valid = root / "account2.json"
canonical = root / "account.json"
archive_dir = root / "archive"

assert invalid.is_file(), invalid
assert valid.is_file(), valid
assert not canonical.exists(), canonical
archive_dir.mkdir(parents=True, exist_ok=True)
archive_target = archive_dir / f"account1.{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
assert not archive_target.exists(), archive_target

invalid.rename(archive_target)
valid.rename(canonical)
print(f"archived={archive_target}")
print(f"canonical={canonical}")
PY
```

Expected: one timestamped archive path and canonical `cookies/weibo_uploader/account.json`. The archived file remains recoverable.

- [ ] **Step 5: Verify only the canonical file is discoverable**

Run:

```bash
.venv/bin/python - <<'PY'
from publish.config import _discover_account_files

accounts = _discover_account_files()
print(accounts.get("weibo_account"))
assert accounts.get("weibo_account") == "cookies/weibo_uploader/account.json"
assert "," not in accounts["weibo_account"]
PY
```

Expected: `cookies/weibo_uploader/account.json`.

### Task 9: Reinstall and retry only Kuaishou and Tencent

**Files:**
- Verify: `/Users/hgeng/AndroidStudioProjects/opub/videos/demo.mp4`
- Verify/update by normal publisher behavior: `/Users/hgeng/AndroidStudioProjects/opub/output/75条自媒体链接-0826-黄耿.xlsx`

- [ ] **Step 1: Refresh the editable install**

Run: `.venv/bin/python -m pip install -e .`

Expected: editable `opub` installation completes successfully from `/Users/hgeng/AndroidStudioProjects/opub`.

- [ ] **Step 2: Start the bounded real retry in a persistent tool session**

Run with a tool timeout of at least 360 seconds:

```bash
.venv/bin/opub \
  --platforms kuaishou,tencent \
  --video /Users/hgeng/AndroidStudioProjects/opub/videos/demo.mp4 \
  --title '随机发货哈' \
  --desc '啊客气哦你看' \
  --tags '收集器,实践考试,谁,刷卡器'
```

Expected: only Kuaishou and Tencent appear in progress output. Douyin, Xiaohongshu, Bilibili, Baijiahao, and Weibo must not appear as publish targets.

- [ ] **Step 3: Surface QR codes while keeping the publish process alive**

When the running command reports a local QR image, render that exact absolute image path to the user immediately and keep polling the same process session. Do not terminate and relaunch the command; each login window remains alive for up to five minutes.

- [ ] **Step 4: Capture the final two-platform result**

Expected successful shape:

```text
快手: ✅ 成功
视频号: ✅ 成功
```

If either platform fails, report its exact `AUTH-xxx`/`PUB-xxx` code and message. Confirm from the output that no third platform published and that no platform attempted more than the initial publish plus one safe recovery.

- [ ] **Step 5: Verify new result links in Excel**

Run:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
from openpyxl import load_workbook

path = Path("/Users/hgeng/AndroidStudioProjects/opub/output/75条自媒体链接-0826-黄耿.xlsx")
workbook = load_workbook(path, data_only=True)
values = [
    str(cell.value)
    for sheet in workbook.worksheets
    for row in sheet.iter_rows()
    for cell in row
    if cell.value is not None
]
links = [value for value in values if value.startswith("http")]
print("\n".join(links[-10:]))
assert any("kuaishou.com" in value for value in links), "未找到快手结果链接"
assert any(
    marker in value
    for value in links
    for marker in ("channels.weixin.qq.com", "weixin.qq.com")
), "未找到视频号结果链接"
PY
```

Expected: the workbook contains a Kuaishou link and a Tencent/Weixin link. If the publisher reports success without one of those URLs, report that as a result-link capture defect instead of claiming full acceptance.

- [ ] **Step 6: Inspect the final worktree without touching pre-existing files**

Run: `git status --short`

Expected: no uncommitted tracked implementation changes. Preserve the user's pre-existing untracked `.claude/`, `probe_weibo_declare.py`, `probe_weibo_declare_verify.py`, `reports/`, `tests/Test.kt`, and `verify_weibo_e2e.py` exactly as found.

## Final acceptance checklist

- [ ] All platform account values contain one path at most.
- [ ] Weibo results use only the `weibo` key and never print account numbering.
- [ ] Tencent login URL and QR iframe are classified before `set_input_files()`.
- [ ] Weibo login URLs/markers are classified before video chooser or image input submission.
- [ ] Neutral page timeouts never set `safe_to_retry=True`.
- [ ] Forced login and republish happen once at most.
- [ ] The valid Weibo Cookie is canonical and the expired Cookie is recoverably archived.
- [ ] Related tests and the suite excluding the documented Baijiahao hanging case pass.
- [ ] The real retry targets only `kuaishou,tencent`, and the Excel result is checked.
