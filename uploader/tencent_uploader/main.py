# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import async_playwright

from conf import BASE_DIR, DEBUG_MODE, LOCAL_CHROME_HEADLESS
from uploader.base_video import (
    BaseBrowserUploader,
    LoginExpiredError,
    PlatformResultExtras,
    _build_launch_kwargs,
    _build_login_result,
    build_login_expired_result,
    _emit_qrcode_callback,
    _get_qrcode_utils,
    _msg,
)
from utils.log import tencent_logger

TENCENT_LOGIN_URL = "https://channels.weixin.qq.com"
TENCENT_UPLOAD_URL = "https://channels.weixin.qq.com/platform/post/create"
TENCENT_MANAGE_URL = "https://channels.weixin.qq.com/platform/post/list"
TENCENT_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
TENCENT_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
TENCENT_UPLOAD_WAIT_TIMEOUT = 1800
TENCENT_PUBLISH_WAIT_TIMEOUT = 600


class _TencentPreMediaLoginExpired(LoginExpiredError):
    """Login expiry proven before the initial media selection."""


async def _wait_for_tencent_upload_input(
    page: Page,
    timeout_ms: int = 30_000,
    poll_interval_ms: int = 250,
):
    """Wait for the upload input while detecting a redirected login page first."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if "login.html" in (page.url or "").lower():
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        if await page.locator('iframe[src*="qrconnect"]').count():
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        file_input = page.locator('input[type="file"]')
        if await file_input.count():
            return file_input.first

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise RuntimeError("未找到腾讯视频号上传控件")

        await asyncio.sleep(min(poll_interval_ms / 1000, remaining_seconds))


def _resolve_account_file(account_file: str | Path) -> str:
    path = Path(account_file).expanduser()
    if path.is_absolute():
        return str(path)

    if len(path.parts) == 1:
        return str((Path(BASE_DIR) / "cookies" / "tencent_uploader" / path).resolve())

    return str(path.resolve())


def format_str_for_short_title(origin_title: str) -> str:
    allowed_special_chars = "《》“”:+?%°"
    filtered_chars = [char if char.isalnum() or char in allowed_special_chars else " " if char == "," else "" for char in origin_title]
    formatted_string = "".join(filtered_chars)

    if len(formatted_string) > 16:
        formatted_string = formatted_string[:16]
    elif len(formatted_string) < 6:
        formatted_string += " " * (6 - len(formatted_string))

    return formatted_string


async def _find_tencent_qrcode_element(page: Page):
    """真实登录页(2026-08 微信改版后)的二维码在 qrconnect iframe 内,img.qrcode
    的 src 是相对 URL,不能走 data:image 解析,调用方须用 element.screenshot 存图。"""
    if not hasattr(page, "frame_locator"):
        raise RuntimeError("未获取到视频号登录二维码地址")
    iframe_locator = page.frame_locator('[src*="qrconnect"]')
    qr_code_img = iframe_locator.locator("img.qrcode").first
    await qr_code_img.wait_for(state="visible", timeout=30000)
    return qr_code_img


async def _save_tencent_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    qrcode_utils = _get_qrcode_utils()
    qr_code_img = await _find_tencent_qrcode_element(page)
    qrcode_path = qrcode_utils["build_login_qrcode_path"](account_file, suffix="tencent_login_qrcode")
    qrcode_path.parent.mkdir(parents=True, exist_ok=True)
    await qr_code_img.screenshot(path=qrcode_path)
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if qrcode_utils["remove_qrcode_file"](previous_qrcode_path):
            tencent_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    tencent_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = qrcode_utils["decode_qrcode_from_path"](qrcode_path)
    if qrcode_content:
        qrcode_utils["print_terminal_qrcode"](qrcode_content, qrcode_path, "微信")
    else:
        tencent_logger.warning(
            _msg(
                "😵",
                f"没能从二维码图片里解析出可打印内容，所以这次没法在终端重绘二维码；请直接打开 {qrcode_path} 扫码",
            )
        )

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": None,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_tencent_login_completed(page: Page) -> bool:
    publish_markers = [
        page.locator('div:has-text("发表视频")').first,
        page.locator('button:has-text("发表")').first,
        page.locator('button:has-text("保存草稿")').first,
    ]
    for marker in publish_markers:
        try:
            if await marker.count() and await marker.is_visible():
                return True
        except Exception:
            continue

    if not (page.url.startswith(TENCENT_UPLOAD_URL) or page.url.startswith(TENCENT_MANAGE_URL)):
        return False

    login_markers = [
        page.locator('iframe[src*="qrconnect"]').first,
        page.locator("div.login-qrcode-wrap").first,
        page.locator("div.qrcode-wrap").first,
        page.locator("img.qrcode").first,
        page.locator('span:has-text("微信扫码登录 视频号助手")').first,
    ]
    for marker in login_markers:
        try:
            if await marker.count() and await marker.is_visible():
                return False
        except Exception:
            continue

    # 已在认证后 URL 上且没有任何登录标记可见，视为登录完成
    return True


async def _is_tencent_qrcode_expired(page: Page) -> bool:
    tip_selectors = [
        'div.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'div.mask.show p.refresh-tip:has-text("网络不可用，点击刷新")',
        'p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'p.refresh-tip:has-text("网络不可用，点击刷新")',
    ]
    for selector in tip_selectors:
        tip = page.locator(selector).first
        try:
            if await tip.count() and await tip.is_visible():
                return True
        except Exception:
            continue
    return False


async def _is_tencent_qrcode_scanned(page: Page) -> bool:
    scanned_tips = [
        'div.qr-tip div:has-text("已扫码")',
        'div.qr-tip div:has-text("需在手机上进行确认")',
    ]
    for selector in scanned_tips:
        tip = page.locator(selector).first
        try:
            if await tip.count() and await tip.is_visible():
                return True
        except Exception:
            continue
    return False


async def _refresh_tencent_qrcode(page: Page) -> None:
    visible_refresh_selectors = [
        "div.login-qrcode-wrap div.mask.show div.refresh-wrap",
        "div.login-qrcode-wrap div.mask.show .refresh-wrap",
    ]
    for selector in visible_refresh_selectors:
        refresh_wrap = page.locator(selector).first
        try:
            if not await refresh_wrap.count() or not await refresh_wrap.is_visible():
                continue
            await refresh_wrap.click()
            return
        except Exception:
            continue

    tip_selectors = [
        'div.mask.show p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'div.mask.show p.refresh-tip:has-text("网络不可用，点击刷新")',
        'p.refresh-tip:has-text("二维码已过期，点击刷新")',
        'p.refresh-tip:has-text("网络不可用，点击刷新")',
    ]
    for selector in tip_selectors:
        tip = page.locator(selector).first
        try:
            if not await tip.count() or not await tip.is_visible():
                continue
            refresh_wrap = tip.locator("xpath=ancestor::div[contains(@class, 'refresh-wrap')]").first
            if await refresh_wrap.count():
                await refresh_wrap.click()
            else:
                await tip.click()
            return
        except Exception:
            continue

    fallback_refresh = page.locator("div.login-qrcode-wrap div.refresh-wrap").first
    if await fallback_refresh.count():
        await fallback_refresh.click()
        return

    raise RuntimeError("未找到可点击的视频号二维码刷新区域")


async def _wait_for_tencent_login(
    page: Page,
    account_file: str,
    qrcode_info: dict,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
) -> dict:
    qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
    scanned_logged = False
    for _ in range(max_checks):
        if await _is_tencent_login_completed(page):
            tencent_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "视频号扫码登录成功", account_file, qrcode_info, page.url)

        if not scanned_logged and await _is_tencent_qrcode_scanned(page):
            tencent_logger.info(_msg("📱", "已经扫码啦，还差手机端确认一下"))
            scanned_logged = True

        if await _is_tencent_qrcode_expired(page):
            tencent_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
            await _refresh_tencent_qrcode(page)
            await asyncio.sleep(1)
            try:
                qrcode_info = await _save_tencent_qrcode(
                    page,
                    account_file,
                    previous_qrcode_path=qrcode_path,
                    qrcode_callback=qrcode_callback,
                )
                qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            except Exception as refresh_exc:
                tencent_logger.warning(_msg("⚠️", f"二维码刷新失败,继续用浏览器内可见二维码: {refresh_exc}"))

        await asyncio.sleep(poll_interval)

    return _build_login_result(False, "timeout", "等待视频号扫码登录超时", account_file, qrcode_info, page.url)


async def cookie_auth(account_file):
    """验证 cookie 是否有效 - 委托 TencentBaseUploader.cookie_auth"""
    account_file = _resolve_account_file(account_file)
    return await TencentBaseUploader.cookie_auth(account_file)


async def tencent_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    account_file = _resolve_account_file(account_file)
    Path(account_file).parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**_build_launch_kwargs(headless=headless))
        context = await browser.new_context()
        qrcode_path = None
        result = _build_login_result(False, "failed", "视频号登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(TENCENT_LOGIN_URL)
            try:
                qrcode_info = await _save_tencent_qrcode(page, account_file, qrcode_callback=qrcode_callback)
                qrcode_path = Path(qrcode_info["image_path"]) if qrcode_info.get("image_path") else None
            except Exception as extract_exc:
                tencent_logger.warning(
                    _msg("⚠️", f"二维码提取失败,请在打开的浏览器里直接扫码: {extract_exc}")
                )
                qrcode_info = {"image_path": "", "image_data_url": ""}
                qrcode_path = None
            tencent_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_tencent_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
            )
            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                if not await cookie_auth(account_file):
                    result = _build_login_result(
                        False,
                        "cookie_invalid",
                        "视频号扫码流程结束，但 cookie 校验失败",
                        account_file,
                        qrcode_info,
                        page.url,
                    )
            return result
        except Exception as exc:
            result = _build_login_result(
                False,
                "failed",
                str(exc),
                account_file,
                current_url=page.url if "page" in locals() else "",
            )
            return result
        finally:
            qrcode_utils = _get_qrcode_utils()
            if qrcode_utils["remove_qrcode_file"](qrcode_path):
                tencent_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                tencent_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()


async def tencent_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    """微信视频号登录设置。

    handle=False:只校验文件存在性(不调 cookie_auth,会开浏览器让 sessionid 失效)。
    handle=True:总是扫码 -- ensure_login 调到这里说明 cookie 已失效(或文件不存在),
    不能 return True,否则 upload() 会用失效 cookie 进 _browser_session 失败。

    Why: tencent sessionid 在新浏览器上下文里 22 秒失效,cookie_auth 开新浏览器校验
    反而让有效 cookie 误判失效。所以 cookie_auth 只检查文件存在性,实际校验交给
    _browser_session 导航时暴露。
    """
    account_file = _resolve_account_file(account_file)
    if not handle:
        if not os.path.exists(account_file):
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在", account_file)
            return result if return_detail else False
        result = _build_login_result(True, "cookie_valid", "cookie文件存在", account_file)
        return result if return_detail else True

    tencent_logger.info(_msg("🥹", "准备打开浏览器扫码登录"))
    result = await tencent_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
    return result if return_detail else result["success"]


async def get_tencent_cookie(account_file, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS):
    return await tencent_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)


async def weixin_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    return await tencent_setup(
        account_file,
        handle=handle,
        return_detail=return_detail,
        qrcode_callback=qrcode_callback,
        headless=headless,
    )


class TencentBaseUploader(BaseBrowserUploader):
    """微信视频号上传器基类 - hook layer for BaseBrowserUploader."""

    PLATFORM_NAME = "tencent"
    UPLOAD_URL = TENCENT_UPLOAD_URL
    LOGIN_URL = TENCENT_LOGIN_URL
    LOGIN_MARKERS = ["login.html"]
    PUBLISH_MARKERS = []

    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = _resolve_account_file(account_file)
        self.publish_strategy = publish_strategy
        self._result_url: str | None = None
        self.debug = debug
        self.headless = headless

    @classmethod
    async def cookie_auth(cls, account_file: str) -> bool:
        """只检查文件存在性,不开浏览器。

        Why: tencent sessionid 在新浏览器上下文里 22 秒失效,主动开浏览器校验会让
        有效 cookie 误判失效(实测:ensure_login cookie_auth 通过后 48 秒,upload 前的
        cookie_auth 在新浏览器里失效)。实际校验交给 _browser_session 导航时暴露 --
        如果 cookie 真失效,page.goto 会被重定向到 login.html,set_input_files 找不到
        input,upload() 失败,用户重扫码即可。

        历史背景:之前版本会开浏览器等 "发表视频" marker,但这是 sessionid 失效的
        根因之一。headless bug 修复(project_tencent_cookie_auth_headless_bug.md)
        让单次校验能通过,但多次校验之间 sessionid 还是会失效,所以最终方案是不校验。
        """
        return os.path.exists(account_file)

    @classmethod
    async def is_login_completed(cls, page: Page) -> bool:
        """Override hook: 视频号登录完成判断(基于 DOM marker,不是 URL)。"""
        return await _is_tencent_login_completed(page)

    async def validate_login_and_strategy(self):
        """Renamed from `validate_base_args(self)` to avoid collision with
        `BasePlatformUploader.validate_base_args(params)` staticmethod (called by dispatch).
        Checks cookie existence + publish_strategy + publish_date.

        不主动调 cookie_auth:tencent sessionid 在新浏览器上下文里 22 秒失效,
        ensure_login 已在 upload() 前验过,再开浏览器会让 sessionid 误判失效。
        文件存在即认为有效,失效会在 _browser_session 导航时暴露。
        """
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成视频号登录: {self.account_file}")
        if self.publish_strategy not in {TENCENT_PUBLISH_STRATEGY_IMMEDIATE, TENCENT_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == TENCENT_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_tencent(self, page: Page, publish_date: datetime):
        label_element = page.locator("label").filter(has_text="定时").nth(1)
        await label_element.click()
        await page.click('input[placeholder="请选择发表时间"]')

        current_month = publish_date.strftime("%m月")
        page_month = await page.inner_text('span.weui-desktop-picker__panel__label:has-text("月")')
        if page_month != current_month:
            await page.click("button.weui-desktop-btn__icon__right")

        elements = await page.query_selector_all("table.weui-desktop-picker__table a")
        for element in elements:
            if "weui-desktop-picker__disabled" in await element.evaluate("el => el.className"):
                continue
            text = await element.inner_text()
            if text.strip() == str(publish_date.day):
                await element.click()
                break

        await page.click('input[placeholder="请选择时间"]')
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(publish_date.strftime("%H"))
        await page.locator("div.input-editor").click()

    async def open_upload_page(self, page: Page):
        await page.goto(TENCENT_UPLOAD_URL)
        try:
            return await _wait_for_tencent_upload_input(page)
        except LoginExpiredError as exc:
            raise _TencentPreMediaLoginExpired(str(exc)) from exc

    async def upload_video_file(self, page: Page, file_path: str, file_input=None) -> None:
        if file_input is None:
            file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(file_path)

    async def set_short_title(self, page: Page, title: str, short_title: str | None = None) -> None:
        short_title_element = (
            page.get_by_text("短标题", exact=True)
            .locator("..")
            .locator("xpath=following-sibling::div")
            .locator('span input[type="text"]')
        )
        if await short_title_element.count():
            await short_title_element.fill(short_title or format_str_for_short_title(title))

    async def fill_title_and_tags(self, page: Page) -> None:
        await page.locator("div.input-editor").click()
        await page.keyboard.type(self.title)
        tencent_logger.info(_msg("🏷️", f"成功添加 title: {len(self.title)}"))

    async def fill_description(self, page: Page) -> None:
        await page.keyboard.press("Enter")
        await page.keyboard.type(self.desc)
        tencent_logger.info(_msg("🏷️", f"成功添加 desc: {len(self.desc)}"))
        # 在描述后面追加标签
        for tag in self.tags:
            await page.keyboard.type(" #" + tag)
        tencent_logger.info(_msg("🏷️", f"成功添加 hashtag: {len(self.tags)}"))

    async def apply_collection(self, page: Page) -> None:
        collection_elements = (
            page.get_by_text("添加到合集")
            .locator("xpath=following-sibling::div")
            .locator(".option-list-wrap > div")
        )
        if await collection_elements.count() > 1:
            await page.get_by_text("添加到合集").locator("xpath=following-sibling::div").click()
            await collection_elements.first.click()

    async def apply_original_statement(self, page: Page) -> None:
        if await page.get_by_label("视频为原创").count():
            await page.get_by_label("视频为原创").check()

        try:
            label_locator = await page.locator('label:has-text("我已阅读并同意 《视频号原创声明使用条款》")').is_visible()
        except Exception:
            label_locator = False

        if label_locator:
            await page.get_by_label("我已阅读并同意 《视频号原创声明使用条款》").check()
            await page.get_by_role("button", name="声明原创").click()

        if await page.locator('div.label span:has-text("声明原创")').count() and getattr(self, "category", None):
            if not await page.locator("div.declare-original-checkbox input.ant-checkbox-input").is_disabled():
                await page.locator("div.declare-original-checkbox input.ant-checkbox-input").click()
                checked_locator = page.locator(
                    "div.declare-original-dialog "
                    "label.ant-checkbox-wrapper.ant-checkbox-wrapper-checked:visible"
                )
                if not await checked_locator.count():
                    await page.locator("div.declare-original-dialog input.ant-checkbox-input:visible").click()

            original_type_form = page.locator('div.original-type-form > div.form-label:has-text("原创类型"):visible')
            if await original_type_form.count():
                await page.locator("div.form-content:visible").click()
                await page.locator(
                    "div.form-content:visible "
                    "ul.weui-desktop-dropdown__list "
                    f'li.weui-desktop-dropdown__list-ele:has-text("{self.category}")'
                ).first.click()
                await page.wait_for_timeout(1000)

            declare_button = page.locator('button:has-text("声明原创"):visible')
            if await declare_button.count():
                await declare_button.click()

    async def wait_for_upload_complete(self, page: Page) -> None:
        deadline = time.monotonic() + TENCENT_UPLOAD_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                publish_button = page.get_by_role("button", name="发表")
                button_class = await publish_button.get_attribute("class")
                if button_class and "weui-desktop-btn_disabled" not in button_class:
                    tencent_logger.info(_msg("🥳", "视频上传完毕"))
                    break

                tencent_logger.info(_msg("🏃", "正在上传视频中..."))
                await asyncio.sleep(2)

                upload_failed = await page.locator("div.status-msg.error").count()
                delete_button = await page.locator('div.media-status-content div.tag-inner:has-text("删除")').count()
                if upload_failed and delete_button:
                    tencent_logger.error(_msg("😵", "发现上传出错了，准备重试"))
                    await self.handle_upload_error(page)
            except Exception:
                tencent_logger.info(_msg("🏃", "正在上传视频中..."))
                await asyncio.sleep(2)
        else:
            raise TimeoutError(f"等待视频上传完成超时({TENCENT_UPLOAD_WAIT_TIMEOUT}秒)，发表按钮一直未激活")

    async def submit_publish(self, page: Page) -> None:
        deadline = time.monotonic() + TENCENT_PUBLISH_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                if getattr(self, "is_draft", False):
                    draft_button = page.locator('div.form-btns button:has-text("保存草稿")')
                    if await draft_button.count():
                        await draft_button.click()
                    await page.wait_for_url("**/post/list**", timeout=5000)
                    tencent_logger.success(_msg("🥳", "视频草稿保存成功"))
                else:
                    publish_button = page.locator('div.form-btns button:has-text("发表")')
                    if await publish_button.count():
                        await publish_button.click()
                    await page.wait_for_url(TENCENT_MANAGE_URL, timeout=5000)
                    tencent_logger.success(_msg("🥳", "视频发布成功"))
                break
            except Exception as exc:
                current_url = page.url
                if getattr(self, "is_draft", False):
                    if "post/list" in current_url or "draft" in current_url:
                        tencent_logger.success(_msg("🥳", "视频草稿保存成功"))
                        break
                else:
                    if TENCENT_MANAGE_URL in current_url:
                        tencent_logger.success(_msg("🥳", "视频发布成功"))
                        break
                tencent_logger.exception(f"  [-] Exception: {exc}")
                tencent_logger.info(_msg("🏃", "视频正在发布中..."))
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError(f"发布/保存草稿超时({TENCENT_PUBLISH_WAIT_TIMEOUT}秒)，页面一直未跳转")

    async def _fetch_published_video_short_url(self, page: Page) -> str | None:
        """发布成功后,从管理页抓取刚发布视频的分享短链。

        流程:
        1. 重新加载管理页,拦截 post_list 和 auth_data 响应
        2. 从 post_list 取第一个视频(刚发布的)的 exportId + objectNonce
        3. 从 auth_data 取 finderUsername(_log_finder_id)
        4. 调 get_object_short_link API 拿 shortUrl
        """
        captured = {"post_list_body": None, "finder_id": None, "aid": None}

        async def on_response(response):
            try:
                url = response.url
                if "post/post_list" in url and response.ok:
                    captured["post_list_body"] = await response.text()
                    m = re.search(r"_aid=([^&]+)", url)
                    if m:
                        captured["aid"] = m.group(1)
                elif "auth/auth_data" in url and response.ok:
                    body = await response.text()
                    data = json.loads(body)
                    finder_user = data.get("data", {}).get("finderUser", {})
                    captured["finder_id"] = finder_user.get("finderUsername")
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(TENCENT_MANAGE_URL, wait_until="domcontentloaded", timeout=30000)
            for _ in range(40):
                if captured["post_list_body"] and captured["finder_id"]:
                    break
                await asyncio.sleep(0.5)

            if not captured["post_list_body"]:
                tencent_logger.warning(_msg("⚠️", "未抓到 post_list 响应,无法获取视频链接"))
                return None

            post_list_data = json.loads(captured["post_list_body"])
            video_list = post_list_data.get("data", {}).get("list", [])
            if not video_list:
                tencent_logger.warning(_msg("⚠️", "post_list 返回空列表"))
                return None

            # 取 createTime 最大的(刚发布的),不依赖列表排序
            latest_video = max(video_list, key=lambda v: v.get("createTime", 0))
            export_id = latest_video.get("exportId") or latest_video.get("objectId")
            object_nonce = latest_video.get("objectNonce")
            if not export_id or not object_nonce:
                tencent_logger.warning(_msg("⚠️", "视频缺少 exportId 或 objectNonce"))
                return None

            short_link_url = "https://channels.weixin.qq.com/micro/content/cgi-bin/mmfinderassistant-bin/post/get_object_short_link"
            if captured["aid"]:
                short_link_url += f"?_aid={captured['aid']}"

            result_text = await page.evaluate(
                """
                async (params) => {
                    try {
                        const resp = await fetch(params.url, {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({
                                exportId: params.exportId,
                                nonceId: String(params.nonceId),
                                scene: 40,
                                timestamp: String(Date.now()),
                                _log_finder_uin: '',
                                _log_finder_id: params.finderId || '',
                                rawKeyBuff: '',
                                pluginSessionId: null,
                                reqScene: 7
                            })
                        });
                        return await resp.text();
                    } catch (e) {
                        return JSON.stringify({error: e.message});
                    }
                }
                """,
                {"url": short_link_url, "exportId": export_id, "nonceId": object_nonce, "finderId": captured["finder_id"] or ""},
            )

            result_data = json.loads(result_text)
            short_url = result_data.get("data", {}).get("shortUrl")
            if short_url:
                return short_url
            tencent_logger.warning(_msg("⚠️", f"get_object_short_link 返回无 shortUrl: {result_text[:200]}"))
            return None
        finally:
            page.remove_listener("response", on_response)


class TencentVideo(TencentBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        category=None,
        is_draft=False,
        desc: str | None = None,
        thumbnail_path: str | None = None,
        short_title: str | None = None,
        publish_strategy: str = TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.category = category
        self.is_draft = is_draft
        self.desc = desc or ""
        self.thumbnail_path = thumbnail_path
        self.short_title = short_title

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page) -> None:
        tencent_logger.info(_msg("😵", "视频出错了，重新上传中"))
        await page.locator('div.media-status-content div.tag-inner:has-text("删除")').click()
        await page.get_by_role("button", name="删除", exact=True).click()
        await self.upload_video_file(page, self.file_path)

    async def set_thumbnail(self, page: Page) -> None:
        if not self.thumbnail_path:
            return

        tencent_logger.info(_msg("🖼️", "小人准备设置封面"))

        cover_entry_selectors = [
            'div.vertical-cover-wrap:has-text("个人主页卡片"):has-text("3:4")',
            'div.vertical-cover-wrap:has-text("3:4")',
            'div.vertical-cover-wrap:has-text("个人主页卡片")',
        ]
        for selector in cover_entry_selectors:
            cover_entry = page.locator(selector).first
            try:
                if not await cover_entry.count():
                    continue
                await cover_entry.wait_for(state="visible", timeout=3000)
                await cover_entry.click()
                await page.wait_for_timeout(500)
                break
            except Exception:
                continue

        cover_dialog = page.locator("div.weui-desktop-dialog").filter(has_text="编辑个人主页卡片").first
        if not await cover_dialog.count():
            tencent_logger.info(_msg("🧍", "当前页面没有出现封面编辑弹窗，小人先跳过自定义封面"))
            return

        try:
            await cover_dialog.wait_for(state="visible", timeout=5000)
        except Exception:
            tencent_logger.warning(_msg("😵", "封面编辑弹窗暂时不可见，这次先跳过自定义封面"))
            return

        file_input = cover_dialog.locator('.single-cover-uploader-wrap input[type="file"]').first
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(self.thumbnail_path)
        await page.wait_for_timeout(1000)

        crop_dialog = page.locator("div.weui-desktop-dialog").filter(has_text="裁剪封面图").first
        if await crop_dialog.count():
            try:
                await crop_dialog.wait_for(state="visible", timeout=10000)
                crop_confirm_button = crop_dialog.locator(
                    'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("确定")'
                ).first
                if await crop_confirm_button.count():
                    await crop_confirm_button.wait_for(state="visible", timeout=5000)
                    await crop_confirm_button.click()
                    await page.wait_for_timeout(1000)
            except Exception as exc:
                tencent_logger.warning(_msg("😵", f"封面裁剪确认时出错，小人继续尝试保存主弹窗: {exc}"))

        confirm_button = cover_dialog.locator(
            'div.weui-desktop-dialog__ft button.weui-desktop-btn_primary:has-text("确认")'
        ).first
        await confirm_button.wait_for(state="visible", timeout=10000)
        await confirm_button.click()
        tencent_logger.success(_msg("🥳", "封面已经设置完成"))

    async def prepare_video_for_publish(self, page: Page) -> None:
        await self.fill_title_and_tags(page)
        await self.fill_description(page)
        await self.apply_collection(page)
        await self.apply_original_statement(page)

    async def upload_video_content(self, page: Page) -> None:
        """上传视频内容(页面已通过 _browser_session 打开)。"""
        file_input = await self.open_upload_page(page)
        tencent_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}"))

        await self.upload_video_file(page, self.file_path, file_input)
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
        except Exception as e:
            tencent_logger.warning(_msg("⚠️", f"获取视频链接失败: {e}"))

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras。"""
        tencent_logger.info(_msg("🧍", "小人先检查 cookie、视频文件和发布时间"))
        await self.validate_upload_args()
        tencent_logger.info(_msg("🥳", "上传前检查通过"))

        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            async with self._browser_session(save_on_success_only=True, save_state=False) as page:
                await self.upload_video_content(page)
                result["success"] = True
                result["message"] = "发布成功"
                if getattr(self, "_result_url", None):
                    result["result_url"] = self._result_url
            tencent_logger.success(_msg("🥳", "cookie 更新完毕"))
        except _TencentPreMediaLoginExpired as e:
            result.update(build_login_expired_result(str(e) or "cookie 已失效，请重新扫码登录"))
            tencent_logger.error(_msg("❌", f"上传失败: {e}"))
        except Exception as e:
            result["message"] = str(e)
            tencent_logger.error(_msg("❌", f"上传失败: {e}"))

        return result


class TencentNote(TencentBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        is_draft: bool = False,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []
        self.is_draft = is_draft

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def switch_to_note_mode(self, page: Page) -> None:
        raise NotImplementedError("请在 TencentNote.switch_to_note_mode 中补充视频号切换到图文发布模式的逻辑")

    async def upload_note_images(self, page: Page) -> None:
        raise NotImplementedError("请在 TencentNote.upload_note_images 中补充视频号图文图片上传逻辑")

    async def fill_note_title_and_tags(self, page: Page) -> None:
        raise NotImplementedError("请在 TencentNote.fill_note_title_and_tags 中补充视频号图文标题/话题填写逻辑")

    async def fill_note_body(self, page: Page) -> None:
        return None

    async def prepare_note_for_publish(self, page: Page) -> None:
        await self.fill_note_title_and_tags(page)
        await self.fill_note_body(page)
        await self.apply_collection(page)
        await self.apply_original_statement(page)

    async def upload_note_content(self, page: Page) -> None:
        await self.switch_to_note_mode(page)
        await self.upload_note_images(page)
        await self.prepare_note_for_publish(page)

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras。
        TencentNote 是 stub(多数方法 NotImplementedError)，upload() 会捕获异常返回失败。"""
        tencent_logger.info(_msg("🧍", "小人先检查 cookie、图文图片和发布时间"))
        await self.validate_upload_args()
        tencent_logger.info(_msg("🥳", "图文上传前检查通过"))

        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            async with self._browser_session(save_on_success_only=True, save_state=False) as page:
                await self.open_upload_page(page)
                tencent_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))

                await self.upload_note_content(page)

                if self.publish_strategy == TENCENT_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
                    await self.set_schedule_time_tencent(page, self.publish_date)

                await self.submit_publish(page)

                result["success"] = True
                result["message"] = "发布成功"
            tencent_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as e:
            result["message"] = str(e)
            tencent_logger.error(_msg("❌", f"上传失败: {e}"))

        return result
