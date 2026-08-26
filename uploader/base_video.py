from __future__ import annotations

import inspect
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional, TypedDict

from patchright.async_api import Page, Playwright, async_playwright

from conf import LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from utils.base_social_media import set_init_script


class PublishStrategy(str, Enum):
    IMMEDIATE = "immediate"
    SCHEDULED = "scheduled"


class PlatformResult(TypedDict):
    success: bool
    message: str


class PlatformResultExtras(PlatformResult, total=False):
    result_url: str
    result_id: str
    account_issue: bool
    issue_type: str
    safe_to_retry: bool


class AccountRestrictedError(Exception):
    """平台限制发布(风控/限流/封禁)。upload() 捕获后映射为 account_issue=True。"""


class LoginExpiredError(RuntimeError):
    """登录在选择或提交媒体前失效，可由编排器安全恢复一次。"""


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


class BasePlatformUploader:
    SUPPORTED_VIDEO_EXTENSIONS = {
        ".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".flv", ".wmv",
    }
    SUPPORTED_IMAGE_EXTENSIONS = {
        ".jpg", ".jpeg", ".png", ".webp", ".bmp",
    }
    MIN_SCHEDULE_LEAD_TIME = timedelta(hours=2)

    @classmethod
    def validate_video_file(cls, file_path: str | Path) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"视频文件不存在: {path}")
        if not path.is_file():
            raise ValueError(f"视频路径不是文件: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_VIDEO_EXTENSIONS:
            raise ValueError(
                f"不支持的视频格式: {path.suffix}，当前支持: {', '.join(sorted(cls.SUPPORTED_VIDEO_EXTENSIONS))}"
            )
        return path

    @classmethod
    def validate_image_file(cls, file_path: str | Path) -> Path:
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"图片文件不存在: {path}")
        if not path.is_file():
            raise ValueError(f"图片路径不是文件: {path}")
        if path.suffix.lower() not in cls.SUPPORTED_IMAGE_EXTENSIONS:
            raise ValueError(
                f"不支持的图片格式: {path.suffix}，当前支持: {', '.join(sorted(cls.SUPPORTED_IMAGE_EXTENSIONS))}"
            )
        return path

    @classmethod
    def validate_publish_date(cls, publish_date: datetime | int | None) -> datetime | int:
        if publish_date in (None, 0):
            return 0
        if not isinstance(publish_date, datetime):
            raise TypeError("publish_date 必须是 datetime 类型或 0")
        now = datetime.now(tz=publish_date.tzinfo) if publish_date.tzinfo else datetime.now()
        if publish_date <= now:
            raise ValueError("定时发布时间必须晚于当前时间")
        min_publish_time = now + cls.MIN_SCHEDULE_LEAD_TIME
        if publish_date <= min_publish_time:
            raise ValueError("定时发布时间必须大于当前时间 2 小时")
        return publish_date

    @staticmethod
    def validate_base_args(params: dict) -> Optional[PlatformResultExtras]:
        """Returns error dict if invalid, None if OK.
        Expects paths already resolved by dispatch (resolve_path applied).
        Called by dispatch before construction."""
        if params.get("content_type") == "video":
            video_file = params.get("video_file")
            if not video_file or not os.path.exists(video_file):
                return {"success": False, "message": f"视频文件不存在: {video_file}"}
        elif params.get("content_type") == "note":
            images = params.get("images") or []
            if not images:
                return {"success": False, "message": "图文模式需要提供图片"}
            for img_path in images:
                if not os.path.exists(img_path):
                    return {"success": False, "message": f"图片文件不存在: {img_path}"}
        return None


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _build_launch_kwargs(headless: bool) -> dict:
    launch_kwargs: dict = {"headless": headless}
    if LOCAL_CHROME_PATH:
        launch_kwargs["executable_path"] = LOCAL_CHROME_PATH
    else:
        launch_kwargs["channel"] = "chrome"
    return launch_kwargs


async def _emit_qrcode_callback(qrcode_callback, payload: dict) -> None:
    if not qrcode_callback:
        return
    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


def _get_qrcode_utils() -> dict:
    from utils.login_qrcode import (
        build_login_qrcode_path,
        decode_qrcode_from_path,
        print_terminal_qrcode,
        remove_qrcode_file,
        save_data_url_image,
    )
    return {
        "build_login_qrcode_path": build_login_qrcode_path,
        "decode_qrcode_from_path": decode_qrcode_from_path,
        "print_terminal_qrcode": print_terminal_qrcode,
        "remove_qrcode_file": remove_qrcode_file,
        "save_data_url_image": save_data_url_image,
    }


class BaseBrowserUploader(BasePlatformUploader):
    """浏览器平台基类:提供 cookie_auth/setup/cookie_gen 模板方法和 _browser_session context manager。
    子类必须定义:PLATFORM_NAME / UPLOAD_URL / LOGIN_URL / LOGIN_MARKERS / PUBLISH_MARKERS
    子类可 override:extract_qrcode_src / is_login_completed / cookie_gen / _launch_browser"""

    PLATFORM_NAME: str = ""
    UPLOAD_URL: str = ""
    LOGIN_URL: str = ""
    LOGIN_MARKERS: list = []
    PUBLISH_MARKERS: list = []

    @classmethod
    async def _launch_browser(cls, playwright: Playwright, headless: bool):
        return await playwright.chromium.launch(**_build_launch_kwargs(headless))

    @classmethod
    async def _init_context(cls, browser, account_file: Optional[str]):
        if account_file and os.path.exists(account_file):
            context = await browser.new_context(storage_state=account_file)
        else:
            context = await browser.new_context()
        return await set_init_script(context)

    @classmethod
    async def is_login_completed(cls, page: Page) -> bool:
        """Override hook:轮询登录是否完成。默认检查 URL 不在 LOGIN_MARKERS。"""
        current_url = (page.url or "").lower()
        if any(marker.lower() in current_url for marker in cls.LOGIN_MARKERS):
            return False
        return True

    @classmethod
    async def extract_qrcode_src(cls, page: Page) -> Optional[str]:
        """Override hook:从登录页提取 QR 图片 src。默认返回 None(子类实现)。"""
        return None

    @classmethod
    async def cookie_auth(cls, account_file: str) -> bool:
        """Navigate to upload page, check if still logged in."""
        if not os.path.exists(account_file):
            return False
        async with async_playwright() as playwright:
            browser = await cls._launch_browser(playwright, headless=LOCAL_CHROME_HEADLESS)
            try:
                context = await cls._init_context(browser, account_file)
                page = await context.new_page()
                await page.goto(cls.UPLOAD_URL)
                await page.wait_for_timeout(3000)
                current_url = (page.url or "").lower()
                if any(marker.lower() in current_url for marker in cls.LOGIN_MARKERS):
                    return False
                if await cls.is_login_completed(page):
                    return True
                return False
            except Exception:
                return False
            finally:
                await browser.close()

    @classmethod
    async def setup(
        cls,
        account_file: str,
        handle: bool = False,
        return_detail: bool = False,
        qrcode_callback=None,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        """Resolve path -> cookie_auth -> if invalid and handle: cookie_gen."""
        if not os.path.exists(account_file) or not await cls.cookie_auth(account_file):
            if not handle:
                result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
                return result if return_detail else False
            result = await cls.cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
            return result if return_detail else (result["success"] if isinstance(result, dict) else result)
        result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
        return result if return_detail else True

    @classmethod
    async def _save_state_and_validate(cls, context, account_file, page):
        """Save storage state and validate cookie. Returns login result dict."""
        await page.wait_for_timeout(2000)
        await context.storage_state(path=account_file)
        if await cls.cookie_auth(account_file):
            return _build_login_result(True, "success", f"{cls.PLATFORM_NAME}扫码登录成功", account_file, None, page.url)
        return _build_login_result(False, "cookie_invalid", f"{cls.PLATFORM_NAME}扫码完成但 cookie 校验失败", account_file, None, page.url)

    @classmethod
    async def cookie_gen(
        cls,
        account_file: str,
        qrcode_callback=None,
        headless: bool = LOCAL_CHROME_HEADLESS,
        return_detail: bool = False,
    ):
        """QR login: goto login URL -> extract QR -> poll until complete -> save state.
        If the page is already at a logged-in state (non-blank URL without login
        markers, e.g. valid context cookies caused an immediate redirect), skip
        the QR flow and save state directly."""
        Path(account_file).parent.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser = await cls._launch_browser(playwright, headless)
            context = await cls._init_context(browser, None)
            result = _build_login_result(False, "failed", f"{cls.PLATFORM_NAME}登录失败", account_file)
            page = None
            try:
                page = await context.new_page()
                # Pre-check: if page already reflects a logged-in state (non-blank
                # URL without login markers), save state without navigating to
                # the login page. In real usage new_page() starts at about:blank
                # so this branch is skipped and the QR flow runs normally.
                pre_url = (page.url or "").strip()
                if pre_url and pre_url != "about:blank" and await cls.is_login_completed(page):
                    result = await cls._save_state_and_validate(context, account_file, page)
                else:
                    await page.goto(cls.LOGIN_URL)
                    await page.wait_for_timeout(3000)
                    qrcode_src = await cls.extract_qrcode_src(page)
                    if qrcode_src:
                        await _emit_qrcode_callback(qrcode_callback, {"qrcode": qrcode_src, "account_file": account_file})
                    for _ in range(100):
                        if await cls.is_login_completed(page):
                            result = await cls._save_state_and_validate(context, account_file, page)
                            break
                        await page.wait_for_timeout(3000)
                    else:
                        result = _build_login_result(False, "timeout", f"{cls.PLATFORM_NAME}扫码登录超时", account_file, None, page.url if page else "")
            except Exception as exc:
                result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if page else "")
            finally:
                await context.close()
                await browser.close()
            return result

    @asynccontextmanager
    async def _browser_session(self, headless: Optional[bool] = None, save_on_success_only: bool = False, save_state: bool = True):
        """Launch browser + context with stored cookies, yield page.
        Saves storage_state on exit. If save_on_success_only=True, skips save
        when the yielded block raised an exception. If save_state=False, skips
        save entirely (for platforms whose cookies expire mid-session, e.g.
        tencent video channel where storage_state at end of upload would
        overwrite the complete cookie file with only sessionid/wxuin)."""
        async with async_playwright() as playwright:
            browser = await self._launch_browser(playwright, headless if headless is not None else self.headless)
            context = await self._init_context(browser, self.account_file)
            page = await context.new_page()
            success = False
            try:
                yield page
                success = True
            finally:
                if save_state and (not save_on_success_only or success):
                    try:
                        await context.storage_state(path=self.account_file)
                    except Exception:
                        pass
                await context.close()
                await browser.close()


class BaseCliUploader(BasePlatformUploader):
    """CLI 平台基类(如 bilibili 走 biliup subprocess)。子类实现 cookie_auth/setup/upload。"""

    @classmethod
    async def cookie_auth(cls, account_file: str) -> bool:
        raise NotImplementedError

    @classmethod
    async def setup(cls, account_file, handle=False, return_detail=False, qrcode_callback=None, headless=LOCAL_CHROME_HEADLESS):
        raise NotImplementedError

    async def upload(self) -> PlatformResultExtras:
        raise NotImplementedError
