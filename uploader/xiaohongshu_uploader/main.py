# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import time
import os
import re
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import async_playwright

from conf import BASE_DIR, DEBUG_MODE, LOCAL_CHROME_HEADLESS
from uploader.base_video import (
    BaseBrowserUploader,
    PlatformResultExtras,
    _build_login_result,
    _emit_qrcode_callback,
    _msg,
)
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import xiaohongshu_logger
from utils.excel_writer import write_video_link

XHS_LOGIN_URL = "https://xiaohongshu.com/login"
XHS_UPLOAD_WAIT_TIMEOUT = 1800
XHS_UPLOAD_URL = "https://www.xiaohongshu.com/explore"
XHS_PUBLISH_VIDEO_URL = "https://creator.xiaohongshu.com/publish/publish?source=official&target=video"
XHS_PUBLISH_NOTE_URL = "https://creator.xiaohongshu.com/publish/publish?source=official&target=image"
XHS_PUBLISH_SUCCESS_URL_PATTERN = "**/publish/success?**"
XHS_LOGIN_BOX_SELECTOR = "div[class*='login-box']"
XHS_LOGIN_SWITCH_SELECTOR = "img.css-wemwzq"
XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"
XHS_USER_PROFILE_URL = "https://www.xiaohongshu.com/user/profile/678a98cc000000000d00891b"


class XhsPublishRestrictedError(Exception):
    """小红书账号被限制发布(如违反社区规范)时抛出。"""
    def __init__(self, toast_text: str):
        self.toast_text = toast_text
        super().__init__(f"账号被限制发布: {toast_text}")


async def _check_xhs_publish_restriction(page: Page, timeout_ms: int = 1500) -> str | None:
    """点击发布按钮后检测是否出现限制 toast。返回 toast 文本,无则 None。"""
    toast_desc = page.locator('div.d-new-toast span.d-toast-description').first
    try:
        await toast_desc.wait_for(state="visible", timeout=timeout_ms)
        text = await toast_desc.inner_text()
        return text.strip() or None
    except Exception:
        return None


def _resolve_account_file(account_file: str | Path) -> str:
    path = Path(account_file).expanduser()
    if path.is_absolute():
        return str(path)

    if len(path.parts) == 1:
        return str((Path(BASE_DIR) / "cookies" / "xiaohongshu_uploader" / path).resolve())

    return str(path.resolve())


async def _open_xhs_qrcode_panel(page: Page) -> None:
    # 主站登录页面的选择器，兼容新旧登录框结构。
    login_box = page.locator(".login-container").first
    if not await login_box.count():
        login_box = page.locator(".login-box-container").first
    if not await login_box.count():
        login_box = page.locator("div[class*='login-box']").first

    await login_box.wait_for(state="visible", timeout=30000)

    # 检查是否已经是扫码模式
    scan_text = login_box.locator("div:has-text('扫一扫')").first
    if await scan_text.count():
        return

    # 尝试切换到扫码模式
    switch_img = login_box.locator("img.css-wemwzq").first
    if await switch_img.count():
        await switch_img.wait_for(state="visible", timeout=10000)
        await switch_img.click()
        await login_box.locator("div:has-text('扫一扫')").first.wait_for(state="visible", timeout=10000)


async def _find_xhs_qrcode_locator(page: Page):
    await _open_xhs_qrcode_panel(page)

    # 主站登录页面的二维码选择器
    qrcode_img = page.locator('.login-container').locator('img[class*="qrcode"]').first

    if await qrcode_img.count():
        return qrcode_img

    # 备用选择器
    qrcode_img = page.locator('.login-container img').first
    if await qrcode_img.count():
        return qrcode_img

    # 旧版登录框：二维码在“APP扫一扫登录”文案后续兄弟节点中。
    qrcode_img = (
        page.locator(".login-box-container")
        .get_by_text("APP扫一扫登录")
        .filter(visible=True)
        .locator("xpath=..//following-sibling::div//img")
        .nth(0)
    )
    if await qrcode_img.count():
        return qrcode_img

    qrcode_img = (
        page.locator("div[class*='login-box']")
        .locator("div:has-text('扫一扫')")
        .locator("xpath=..//following-sibling::div//img")
        .nth(0)
    )
    if await qrcode_img.count():
        return qrcode_img

    raise RuntimeError("未在扫一扫登录区域找到小红书二维码图片")


async def _extract_xhs_qrcode_src(page: Page) -> str:
    qrcode_img = await _find_xhs_qrcode_locator(page)
    await qrcode_img.wait_for(state="visible", timeout=30000)
    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到小红书登录二维码地址")
    return qrcode_src


async def _save_xhs_qrcode(
    page: Page,
    account_file: str,
    previous_qrcode_path: Path | None = None,
    qrcode_callback=None,
) -> dict:
    qrcode_src = await _extract_xhs_qrcode_src(page)
    qrcode_path = build_login_qrcode_path(account_file, suffix="xhs_login_qrcode")
    qrcode_img = await _find_xhs_qrcode_locator(page)

    if qrcode_src.startswith("data:image/"):
        save_data_url_image(qrcode_src, qrcode_path)
    else:
        qrcode_path.parent.mkdir(parents=True, exist_ok=True)
        await qrcode_img.screenshot(path=str(qrcode_path))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    xiaohongshu_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "小红书APP")
    else:
        xiaohongshu_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_xhs_login_completed(page: Page) -> bool:
    # 检查是否还在登录页
    if "login" in page.url:
        return False

    # 检查登录框是否消失
    login_box = page.locator(".login-container").first
    if not await login_box.count():
        return True

    try:
        return not await login_box.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    """验证 cookie 是否有效 - 委托 XiaoHongShuBaseUploader.cookie_auth"""
    account_file = _resolve_account_file(account_file)
    return await XiaoHongShuBaseUploader.cookie_auth(account_file)


async def get_share_link(page: Page) -> dict:
    """
    获取最新发布笔记的分享链接

    流程：
    1. 打开用户首页
    2. 点击第一个笔记进入详情页
    3. 点击分享按钮
    4. 点击链接图标
    5. 从剪贴板获取链接

    Returns:
        dict: {"success": bool, "share_link": str, "note_id": str, "message": str}
    """
    try:
        # 1. 导航到用户首页
        xiaohongshu_logger.info(_msg("🧭", "正在导航到用户首页获取分享链接"))
        await page.goto(XHS_USER_PROFILE_URL, timeout=60000)
        await page.wait_for_load_state("domcontentloaded")
        await asyncio.sleep(3)

        # 2. 找到并点击第一个笔记（跳过置顶笔记，避免抓到旧链接）
        xiaohongshu_logger.info(_msg("🔍", "正在查找第一个笔记"))

        notes = page.locator("section.note-item")
        note_count = await notes.count()
        if note_count == 0:
            xiaohongshu_logger.warning(_msg("⚠️", "未找到笔记元素"))
            return {"success": False, "share_link": "", "note_id": "", "message": "未找到笔记元素"}

        # 用户置顶的笔记会排在最前，直接取 first 会一直抓到置顶那条，发布多条后拿到的链接都相同。
        # 这里遍历笔记，跳过含"置顶"标识的，取第一条非置顶笔记。
        first_note = None
        for i in range(note_count):
            note = notes.nth(i)
            try:
                note_text = await note.inner_text()
                if "置顶" in note_text:
                    xiaohongshu_logger.info(_msg("📌", f"跳过置顶笔记 (index={i})"))
                    continue
            except Exception:
                pass
            first_note = note
            break

        if first_note is None:
            xiaohongshu_logger.warning(_msg("⚠️", "未找到非置顶笔记，回退到第一条笔记"))
            first_note = notes.first

        # 点击笔记进入详情页
        await first_note.click()
        xiaohongshu_logger.info(_msg("👆", "已点击笔记进入详情页"))
        await asyncio.sleep(3)

        current_url = page.url
        xiaohongshu_logger.info(_msg("🔍", f"当前URL: {current_url}"))

        # 3. 点击分享按钮
        xiaohongshu_logger.info(_msg("🔍", "正在查找分享按钮"))

        await asyncio.sleep(2)

        share_button = page.locator('button.reds-button-new.share-icon').first

        if await share_button.count() == 0:
            xiaohongshu_logger.warning(_msg("⚠️", "未找到分享按钮"))
            return {"success": False, "share_link": "", "note_id": "", "message": "未找到分享按钮"}

        # 用 JavaScript 点击（绕过 viewport 检查）
        await share_button.evaluate('el => el.click()')
        xiaohongshu_logger.info(_msg("👆", "已点击分享按钮"))
        await asyncio.sleep(2)

        # 4. 点击链接图标
        xiaohongshu_logger.info(_msg("🔍", "正在查找链接图标"))

        link_container = page.locator('.share-icon-container').first

        if await link_container.count() == 0:
            xiaohongshu_logger.warning(_msg("⚠️", "未找到链接图标"))
            return {"success": False, "share_link": "", "note_id": "", "message": "未找到链接图标"}

        # 点击链接图标
        await link_container.click()
        xiaohongshu_logger.info(_msg("👆", "已点击链接图标"))
        await asyncio.sleep(1)

        # 5. 从剪贴板获取链接
        xiaohongshu_logger.info(_msg("📋", "正在从剪贴板获取链接"))

        try:
            clipboard_text = await page.evaluate('navigator.clipboard.readText()')
            share_link = ""
            note_id = ""

            if clipboard_text:
                # 剪贴板可能是用户残留文本而非复制结果，必须校验出 URL 才算数
                url_match = re.search(r'https?://[^\s]+', clipboard_text)
                if url_match:
                    share_link = url_match.group(0)

            if share_link:
                match = re.search(r'/(?:item|explore)/([a-f0-9]+)', share_link)
                if match:
                    note_id = match.group(1)
                xiaohongshu_logger.success(_msg("✅", f"获取到分享链接: {share_link}"))
            else:
                # 复制未生效时退回当前笔记详情页 URL（页面已在详情页，URL 含笔记 ID）
                page_match = re.search(r'/(?:explore|discovery/item|item)/([a-f0-9]+)', page.url)
                if not page_match:
                    msg = f"剪贴板与页面 URL 均未提取到链接，剪贴板内容: {clipboard_text!r}"
                    xiaohongshu_logger.warning(_msg("⚠️", msg))
                    return {"success": False, "share_link": "", "note_id": "", "message": msg}
                note_id = page_match.group(1)
                share_link = f"https://www.xiaohongshu.com/explore/{note_id}"
                xiaohongshu_logger.warning(_msg("⚠️", f"剪贴板无链接，退回详情页 URL: {share_link}"))

            return {
                "success": True,
                "share_link": share_link,
                "note_id": note_id,
                "message": "成功获取分享链接"
            }

        except Exception as e:
            xiaohongshu_logger.error(_msg("❌", f"读取剪贴板失败: {e}"))
            return {"success": False, "share_link": "", "note_id": "", "message": f"读取剪贴板失败: {e}"}

    except Exception as e:
        xiaohongshu_logger.error(_msg("❌", f"获取分享链接失败: {e}"))
        return {"success": False, "share_link": "", "note_id": "", "message": str(e)}


async def xiaohongshu_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    account_file = _resolve_account_file(account_file)
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        xiaohongshu_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await xiaohongshu_cookie_gen(
            account_file,
            qrcode_callback=qrcode_callback,
            headless=headless,
        )
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def xiaohongshu_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    if headless:
        xiaohongshu_logger.info(_msg("🖼️", "小红书登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    account_file = _resolve_account_file(account_file)
    account_path = Path(account_file)
    account_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "小红书登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(XHS_LOGIN_URL)
            qrcode_info = await _save_xhs_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            xiaohongshu_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))

            for _ in range(max_checks):
                if await XiaoHongShuBaseUploader.is_login_completed(page):
                    await asyncio.sleep(2)
                    # 访问主站，确保 cookie 能用于 www.xiaohongshu.com
                    xiaohongshu_logger.info(_msg("🧭", "正在访问主站同步登录状态"))
                    await page.goto("https://www.xiaohongshu.com/", timeout=30000)
                    await page.wait_for_load_state("domcontentloaded")
                    await asyncio.sleep(1)
                    await context.storage_state(path=account_file)
                    xiaohongshu_logger.success(_msg("🥳", "小红书扫码登录成功，小人开心收工"))
                    result = _build_login_result(
                        True,
                        "success",
                        "小红书扫码登录成功",
                        account_file,
                        qrcode_info,
                        page.url,
                    )
                    return result

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待小红书扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                xiaohongshu_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                xiaohongshu_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class XiaoHongShuBaseUploader(BaseBrowserUploader):
    """小红书上传器基类 - hook layer for BaseBrowserUploader."""

    PLATFORM_NAME = "xiaohongshu"
    UPLOAD_URL = XHS_PUBLISH_VIDEO_URL
    LOGIN_URL = XHS_LOGIN_URL
    LOGIN_MARKERS = ["手机号登录", "扫码登录"]
    PUBLISH_MARKERS = []

    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = _resolve_account_file(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.headless = headless

    @classmethod
    async def is_login_completed(cls, page):
        return await _is_xhs_login_completed(page)

    @classmethod
    async def extract_qrcode_src(cls, page):
        return await _extract_xhs_qrcode_src(page)

    @classmethod
    async def _init_context(cls, browser, account_file=None):
        """Override to add clipboard/geolocation permissions needed for share link extraction."""
        permissions = ["geolocation", "clipboard-read", "clipboard-write"]
        if account_file and os.path.exists(account_file):
            context = await browser.new_context(permissions=permissions, storage_state=account_file)
        else:
            context = await browser.new_context(permissions=permissions)
        return await set_init_script(context)

    async def validate_login_and_strategy(self):
        """Renamed from `validate_base_args(self)` to avoid collision with
        `BasePlatformUploader.validate_base_args(params)` staticmethod (called by dispatch).
        Checks cookie existence/validity + publish_strategy + publish_date."""
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成小红书登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成小红书登录: {self.account_file}")

        if self.publish_strategy not in {
            XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_xiaohongshu(self, page: Page, publish_date: datetime):
        xiaohongshu_logger.info(_msg("🕒", f"小人准备设置定时发布时间: {publish_date.strftime(self.date_format)}"))
        await page.locator('.custom-switch-card').filter(has_text="定时发布").locator('.d-switch').click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        time_input = page.locator('.d-datepicker-input-filter input.d-text')
        await time_input.fill(str(publish_date_hour))
        await asyncio.sleep(1)

    async def set_location(self, page: Page, location: str = "青岛市"):
        if not location:
            return True

        xiaohongshu_logger.info(_msg("📍", f"小人准备设置位置: {location}"))
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        await loc_ele.click()
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
        except Exception:
            xiaohongshu_logger.warning(_msg("😵", "位置下拉列表没按预期出现，小人继续按旧逻辑查找"))
        await page.wait_for_timeout(1000)
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        try:
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )

            if not location_option:
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )

            await location_option.scroll_into_view_if_needed()
            await location_option.click()
            xiaohongshu_logger.success(_msg("🥳", f"位置已经设置成 {location}"))
            return True
        except Exception as e:
            xiaohongshu_logger.error(_msg("😢", f"设置位置失败: {e}"))
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                xiaohongshu_logger.debug(_msg("🧍", f"位置下拉里一共找到 {len(all_options)} 个选项"))
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    xiaohongshu_logger.debug(_msg("🧾", f"候选位置 {i + 1}: {option_text.strip()[:50]}"))
            except Exception as inner_e:
                xiaohongshu_logger.debug(_msg("😵", f"读取位置候选列表失败: {inner_e}"))
            return False

    async def fill_title(self, page: Page) -> None:
        title_container = page.locator('input[placeholder*="填写标题"]')
        await title_container.fill(self.title[:20])

    async def fill_desc(self, page: Page) -> None:
        if not getattr(self, "desc", ""):
            return

        desc = page.locator('p[data-placeholder*="输入正文描述"]')
        await desc.click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.desc)
        await page.keyboard.press("Enter")

    async def fill_tags(self, page: Page) -> None:
        if not getattr(self, "tags", None):
            return

        if not getattr(self, "desc", ""):
            desc = page.locator('p[data-placeholder*="输入正文描述"]')
            await desc.click()

        for tag in self.tags:
            await page.keyboard.type("#" + tag, delay=30)
            await page.locator('#creator-editor-topic-container').wait_for(
                state="visible",
                timeout=3000
            )
            first_item = page.locator('#creator-editor-topic-container .item').first
            await first_item.wait_for(state="visible", timeout=2000)
            await first_item.click()
            await page.keyboard.press("Space")

    async def fill_meta(self, page: Page) -> None:
        await self.fill_title(page)
        await self.fill_desc(page)
        await self.fill_tags(page)


class XiaoHongShuVideo(XiaoHongShuBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_path=None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
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
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        xiaohongshu_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        if not thumbnail_path:
            return

        xiaohongshu_logger.info(_msg("🖼️", "小人准备设置封面"))

        cover_plugin_title = page.locator("div.cover-plugin-title").filter(has_text="设置封面")
        cover_upload_dialog = cover_plugin_title.locator(
            "xpath=ancestor::div[contains(@class, 'cover-plugin-preview')]"
        ).locator("div.cover > div.default:visible")
        await cover_upload_dialog.wait_for(state="visible", timeout=30000)

        await cover_upload_dialog.click(force=True)

        modal = page.locator("div.d-modal.cover-modal")
        await modal.wait_for(state="visible", timeout=30000)

        file_input = modal.locator('input[type="file"][accept*="image"]').first
        await file_input.wait_for(state="attached", timeout=10000)
        await file_input.set_input_files(thumbnail_path)
        await page.wait_for_timeout(2000)

        confirm_button = modal.locator("button.mojito-button").filter(has_text="确定").first
        await confirm_button.wait_for(state="visible", timeout=10000)
        await confirm_button.click()

        await modal.wait_for(state="hidden", timeout=30000)
        xiaohongshu_logger.success(_msg("🥳", "封面已经设置完成"))

    async def upload_video_content(self, page: Page) -> dict:
        """上传视频内容并获取分享链接。

        Returns:
            dict: {"share_link": str, "note_id": str, "message": str}
        """
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往视频发布页"))
        await page.goto(XHS_PUBLISH_VIDEO_URL)
        await page.wait_for_url(XHS_PUBLISH_VIDEO_URL)
        await page.locator("div[class^='upload-content'] input[class='upload-input']").set_input_files(self.file_path)

        deadline = time.monotonic() + XHS_UPLOAD_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                upload_input = await page.wait_for_selector('input.upload-input', timeout=3000)
                preview_new = await upload_input.query_selector(
                    'xpath=following-sibling::div[contains(@class, "preview-new")]')
                if preview_new:
                    # 获取整个预览区域的文本，更鲁棒地判断上传状态
                    all_text = await preview_new.inner_text()
                    upload_success = any(keyword in all_text for keyword in ['上传成功', '分辨率', '重新上传', '编辑封面', '已上传', '已选择', '100%'])

                    if not upload_success:
                        # 检查是否有特定的状态码或百分比
                        stage_elements = await preview_new.query_selector_all('div.stage')
                        for stage in stage_elements:
                            text_content = await page.evaluate('(element) => element.textContent', stage)
                            if '上传成功' in text_content or '分辨率' in text_content:
                                upload_success = True
                                break

                    if upload_success:
                        xiaohongshu_logger.success(_msg("🥳", "视频已经传完啦"))
                        break

                    if self.debug:
                        preview_text = all_text.strip().replace("\n", " ")
                        xiaohongshu_logger.debug(_msg("🧍", f"预览区域内容: {preview_text}"))
                    xiaohongshu_logger.debug(_msg("🧍", "还没看到上传成功标识，小人继续等一会"))
                else:
                    # 尝试检查标题输入框是否已经出现，如果是，说明已经进入编辑状态
                    title_container = page.locator('input[placeholder*="填写标题"]')
                    if await title_container.count() > 0 and await title_container.is_visible():
                        xiaohongshu_logger.success(_msg("🥳", "虽然没看到预览区，但标题框出来了，小人继续"))
                        break
                    xiaohongshu_logger.debug(_msg("🧍", "还没拿到预览区域，小人继续等一会"))
            except Exception as e:
                xiaohongshu_logger.debug(_msg("😵", f"上传状态还没稳定下来，小人继续观察: {e}"))
            await asyncio.sleep(2)
        else:
            raise TimeoutError(f"等待视频上传完成超时({XHS_UPLOAD_WAIT_TIMEOUT}秒)")

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        await self.set_thumbnail(page, self.thumbnail_path)

        # await self.set_location(page, "青岛市")

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        max_publish_retries = 60
        for _publish_attempt in range(max_publish_retries):
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()

                restriction_text = await _check_xhs_publish_restriction(page, timeout_ms=1500)
                if restriction_text:
                    raise XhsPublishRestrictedError(restriction_text)

                await page.wait_for_url(
                    "https://creator.xiaohongshu.com/publish/success?**",
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                break
            except XhsPublishRestrictedError:
                raise
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布视频"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError(f"视频发布重试 {max_publish_retries} 次后仍未跳转到成功页")

        # 发布成功后获取分享链接
        share_result = await get_share_link(page)
        return share_result

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras"""
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "上传前检查通过"))

        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            async with self._browser_session(save_on_success_only=True) as page:
                share_result = await self.upload_video_content(page)

                share_link = share_result.get("share_link", "") if share_result else ""
                note_id = share_result.get("note_id", "") if share_result else ""

                result["success"] = True
                result["message"] = "发布成功"

                if share_link:
                    xiaohongshu_logger.info(_msg("🔗", f"分享链接: {share_link}"))
                    result["result_url"] = share_link

                    # 写入Excel
                    try:
                        excel_result = write_video_link(video_link=share_link)
                        if excel_result["success"]:
                            xiaohongshu_logger.success(_msg("📝", f"已写入Excel: {excel_result['filepath']}"))
                        else:
                            xiaohongshu_logger.warning(_msg("⚠️", f"写入Excel失败: {excel_result['message']}"))
                    except Exception as excel_err:
                        xiaohongshu_logger.warning(_msg("⚠️", f"写入Excel异常: {excel_err}"))

                if note_id:
                    result["result_id"] = note_id

                if not share_link:
                    share_msg = share_result.get("message", "") if share_result else ""
                    result["message"] = f"发布成功，但获取分享链接失败: {share_msg}"
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        except XhsPublishRestrictedError as exc:
            result["message"] = f"账号被限制发布: {exc.toast_text}"
            result["account_issue"] = True
            result["issue_type"] = "publish_restricted"
            xiaohongshu_logger.error(_msg("😢", f"账号被限制发布: {exc.toast_text}"))
        except Exception as e:
            result["message"] = str(e)
            xiaohongshu_logger.error(_msg("❌", f"上传失败: {e}"))

        return result

    async def xiaohongshu_upload_video(self) -> PlatformResultExtras:
        return await self.upload()


class XiaoHongShuNote(XiaoHongShuBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        desc: str | None = None,
        publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
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
        self.image_paths = image_paths
        self.note = note or ""
        self.tags = tags or []
        self.desc = desc if desc is not None else self.note
        self.title = title or ((self.desc or self.note)[:20] if (self.desc or self.note) else "")

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> dict:
        """上传图文内容并获取分享链接。

        Returns:
            dict: {"share_link": str, "note_id": str, "message": str}
        """
        xiaohongshu_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        xiaohongshu_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
        await page.goto(XHS_PUBLISH_NOTE_URL)
        await page.wait_for_url(XHS_PUBLISH_NOTE_URL)

        upload_input = page.locator('input[type="file"][accept*="image"]').first
        if not await upload_input.count():
            upload_input = page.locator("div[class^='upload-content'] input[class='upload-input']").first

        await upload_input.wait_for(state="attached", timeout=30000)
        xiaohongshu_logger.info(_msg("📤", "小人正在上传图片"))
        await upload_input.set_input_files(self.image_paths)

        deadline = time.monotonic() + XHS_UPLOAD_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                title_container = page.locator('input[placeholder*="填写标题"]').first
                await title_container.wait_for(state="visible", timeout=3000)
                xiaohongshu_logger.success(_msg("🥳", "图文素材已经传完，可以开始填写内容了"))
                break
            except Exception:
                xiaohongshu_logger.debug(_msg("🧍", "图文素材还在上传，小人继续等一会"))
                await asyncio.sleep(1)
        else:
            raise TimeoutError(f"等待图文素材上传完成超时({XHS_UPLOAD_WAIT_TIMEOUT}秒)")

        xiaohongshu_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_meta(page)

        if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        max_publish_retries = 60
        for _publish_attempt in range(max_publish_retries):
            try:
                if self.publish_strategy == XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED:
                    await page.locator('button:has-text("定时发布")').click()
                else:
                    await page.locator('button:has-text("发布")').click()

                restriction_text = await _check_xhs_publish_restriction(page, timeout_ms=1500)
                if restriction_text:
                    raise XhsPublishRestrictedError(restriction_text)

                await page.wait_for_url(
                    XHS_PUBLISH_SUCCESS_URL_PATTERN,
                    timeout=3000
                )
                xiaohongshu_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except XhsPublishRestrictedError:
                raise
            except Exception:
                xiaohongshu_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(0.5)
        else:
            raise TimeoutError(f"图文发布重试 {max_publish_retries} 次后仍未跳转到成功页")

        # 发布成功后获取分享链接
        share_result = await get_share_link(page)
        return share_result

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras"""
        xiaohongshu_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        xiaohongshu_logger.info(_msg("🥳", "图文上传前检查通过"))

        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            async with self._browser_session(save_on_success_only=True) as page:
                share_result = await self.upload_note_content(page)

                share_link = share_result.get("share_link", "") if share_result else ""
                note_id = share_result.get("note_id", "") if share_result else ""

                result["success"] = True
                result["message"] = "发布成功"

                if share_link:
                    xiaohongshu_logger.info(_msg("🔗", f"分享链接: {share_link}"))
                    result["result_url"] = share_link

                    # 写入Excel
                    try:
                        excel_result = write_video_link(video_link=share_link)
                        if excel_result["success"]:
                            xiaohongshu_logger.success(_msg("📝", f"已写入Excel: {excel_result['filepath']}"))
                        else:
                            xiaohongshu_logger.warning(_msg("⚠️", f"写入Excel失败: {excel_result['message']}"))
                    except Exception as excel_err:
                        xiaohongshu_logger.warning(_msg("⚠️", f"写入Excel异常: {excel_err}"))

                if note_id:
                    result["result_id"] = note_id

                if not share_link:
                    share_msg = share_result.get("message", "") if share_result else ""
                    result["message"] = f"发布成功，但获取分享链接失败: {share_msg}"
            xiaohongshu_logger.success(_msg("🥳", "cookie 更新完毕"))
        except XhsPublishRestrictedError as exc:
            result["message"] = f"账号被限制发布: {exc.toast_text}"
            result["account_issue"] = True
            result["issue_type"] = "publish_restricted"
            xiaohongshu_logger.error(_msg("😢", f"账号被限制发布: {exc.toast_text}"))
        except Exception as e:
            result["message"] = str(e)
            xiaohongshu_logger.error(_msg("❌", f"上传失败: {e}"))

        return result

    async def xiaohongshu_upload_note(self) -> PlatformResultExtras:
        return await self.upload()
