# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from conf import BASE_DIR, DEBUG_MODE, LOCAL_CHROME_HEADLESS
from uploader.base_video import (
    BaseBrowserUploader,
    LoginExpiredError,
    PlatformResultExtras,
    PublishStrategy,
    build_login_expired_result,
    _msg,
)
from utils.log import weibo_logger

WEIBO_MAIN_URL = "https://weibo.com/"  # 微博主站，发布入口在首页
WEIBO_LOGIN_URL = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&disp=popup&url=https%3A%2F%2Fweibo.com%2Fu%2F6569482075&from=weibopro"  # 登录页面
WEIBO_UPLOAD_CHANNEL_URL = "https://weibo.com/upload/channel"  # 视频上传页面
WEIBO_LOGIN_URL_MARKERS = ("newlogin", "passport", "login.sina", "/login", "/sso/")
WEIBO_UPLOAD_BUTTON_SELECTOR = 'button[id^="video_button_upload"], button._btn1_109u9_8'
WEIBO_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
WEIBO_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _resolve_account_file(account_file: str | Path) -> str:
    path = Path(account_file).expanduser()
    if path.is_absolute():
        return str(path)

    if len(path.parts) == 1:
        return str((Path(BASE_DIR) / "cookies" / "weibo_uploader" / path).resolve())

    return str(path.resolve())


async def _is_visible(locator) -> bool:
    try:
        if not await locator.count():
            return False
        return await locator.is_visible()
    except Exception:
        return False


class _WeiboPreMediaLoginExpired(LoginExpiredError):
    """Login expiry proven before the initial media selection."""


async def _is_weibo_login_page(page: Page) -> bool:
    current_url = (page.url or "").lower()
    if any(marker in current_url for marker in WEIBO_LOGIN_URL_MARKERS):
        return True

    for selector in ('text="登录"', 'text="扫码登录"', 'a[href*="login"]'):
        if await _is_visible(page.locator(selector).first):
            return True

    return False


async def _wait_for_weibo_upload_button(
    page: Page,
    timeout_ms: int = 15_000,
    poll_interval_ms: int = 250,
):
    """Wait for the video entry while explicitly detecting a login redirect."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if await _is_weibo_login_page(page):
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        upload_button = page.locator(WEIBO_UPLOAD_BUTTON_SELECTOR).first
        if await _is_visible(upload_button):
            return upload_button

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise RuntimeError("未找到视频上传入口")
        await page.wait_for_timeout(
            max(1, int(min(poll_interval_ms / 1000, remaining_seconds) * 1000))
        )


async def _select_weibo_video_file(page: Page, file_path: str) -> None:
    """选择视频文件：点击"上传视频"按钮触发 file chooser。

    页面只有在该路径下才会从上传区切换到编辑表单；直接对隐藏 input
    set_input_files 只会发起网络上传，UI 不会进入编辑态。
    按钮缺失（selector 漂移）时才回退到直接设置隐藏 input。
    """
    try:
        upload_button = await _wait_for_weibo_upload_button(page)
    except RuntimeError:
        file_input = page.locator('input[type="file"]').first
        if await file_input.count():
            await file_input.set_input_files(file_path)
            return
        raise

    # chooser 未弹出多为页面未就绪/点击竞态（handler 尚未挂载），
    # 重试一次即可恢复，避免整次发布失败。
    for _attempt in range(2):
        try:
            async with page.expect_file_chooser(timeout=15000) as chooser_info:
                await upload_button.click()
            chooser = await chooser_info.value
            await chooser.set_files(file_path)
            return
        except PlaywrightTimeoutError:
            continue

    raise RuntimeError("上传视频按钮点击后未弹出文件选择框")


async def _open_first_video_link(page: Page) -> str | None:
    """点击视频管理页第一个视频封面，返回新页签的视频链接。

    封面图被 woo-picture-cover 覆盖层拦截，直接点击 img 过不了命中检测，
    因此优先点击封面上的播放图标；旧哈希 class 失效时回退到对封面图
    force 点击。均失败时返回 None（链接仅是附加信息，不应让发布失败）。
    """
    first_video = page.locator('.vue-recycle-scroller__item-view').first
    candidates = [
        ('i.woo-font--play', False),
        ('img.woo-picture-img', True),
    ]
    for selector, force in candidates:
        cover = first_video.locator(selector)
        if not await cover.count():
            continue
        try:
            async with page.expect_popup(timeout=10000) as popup_info:
                await cover.click(timeout=5000, force=force)
            new_page = await popup_info.value
            await new_page.wait_for_load_state()
            link = new_page.url.split('?')[0]
            weibo_logger.success(_msg("🔗", f"视频链接: {link}"))
            await new_page.close()
            return link
        except Exception:
            continue
    return None


async def _wait_for_weibo_image_input(
    page: Page,
    selectors,
    timeout_ms: int = 15_000,
    poll_interval_ms: int = 250,
):
    """Wait for an image input while explicitly detecting a login redirect."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        if await _is_weibo_login_page(page):
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        for selector in selectors:
            image_input = page.locator(selector)
            if await image_input.count():
                return image_input.first

        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise RuntimeError("未找到图片上传入口")
        await page.wait_for_timeout(
            max(1, int(min(poll_interval_ms / 1000, remaining_seconds) * 1000))
        )


async def _is_weibo_auth_page_valid(page: Page) -> bool:
    """只有进入发布页并看到上传入口，才认为微博 cookie 仍然有效。"""
    if await _is_weibo_login_page(page):
        return False

    upload_button = page.locator(WEIBO_UPLOAD_BUTTON_SELECTOR).first
    return await _is_visible(upload_button)


async def _is_weibo_login_completed(page: Page) -> bool:
    """检查微博登录是否完成"""
    # 检查 URL 是否跳转到微博主页（非登录页）
    if "passport.weibo.com" in page.url:
        return False

    # 检查是否还有登录相关元素
    login_markers = [
        page.locator('text="登录"').first,
        page.locator('text="扫码登录"').first,
    ]

    for marker in login_markers:
        if await marker.count():
            try:
                if await marker.is_visible():
                    return False
            except Exception:
                continue

    return True


async def cookie_auth(account_file):
    """验证 cookie 是否有效 - 委托 WeiboBaseUploader.cookie_auth"""
    account_file = _resolve_account_file(account_file)
    return await WeiboBaseUploader.cookie_auth(account_file)


async def weibo_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    """生成微博登录 cookie - 委托 WeiboBaseUploader.cookie_gen"""
    account_file = _resolve_account_file(account_file)
    return await WeiboBaseUploader.cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)


async def weibo_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    """微博登录设置 - 委托 WeiboBaseUploader.setup"""
    account_file = _resolve_account_file(account_file)
    return await WeiboBaseUploader.setup(account_file, handle, return_detail, qrcode_callback, headless)


class WeiboBaseUploader(BaseBrowserUploader):
    """微博上传器基类 - hook layer for BaseBrowserUploader."""

    PLATFORM_NAME = "weibo"
    UPLOAD_URL = WEIBO_UPLOAD_CHANNEL_URL
    LOGIN_URL = WEIBO_LOGIN_URL
    LOGIN_MARKERS = list(WEIBO_LOGIN_URL_MARKERS)
    PUBLISH_MARKERS = []

    def __init__(
        self,
        publish_date,
        account_file,
        publish_strategy=PublishStrategy.IMMEDIATE,
        debug=DEBUG_MODE,
        headless=LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = _resolve_account_file(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.headless = headless

    @classmethod
    async def is_login_completed(cls, page):
        return await _is_weibo_login_completed(page)

    @classmethod
    async def cookie_auth(cls, account_file: str) -> bool:
        """Validate persisted state against Weibo's actual video upload entry."""
        if not os.path.exists(account_file):
            return False

        async with async_playwright() as playwright:
            browser = await cls._launch_browser(playwright, headless=LOCAL_CHROME_HEADLESS)
            try:
                context = await cls._init_context(browser, account_file)
                page = await context.new_page()
                await page.goto(WEIBO_UPLOAD_CHANNEL_URL)
                try:
                    await _wait_for_weibo_upload_button(page)
                except LoginExpiredError:
                    return False
                return True
            finally:
                await browser.close()

    async def validate_login_and_strategy(self):
        """Renamed from `validate_base_args(self)` to avoid collision with
        `BasePlatformUploader.validate_base_args(params)` staticmethod (called by dispatch).
        Checks cookie existence/validity + publish_strategy + publish_date."""
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成微博登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise LoginExpiredError("cookie 已失效，请重新扫码登录")

        if self.publish_strategy not in {PublishStrategy.IMMEDIATE, PublishStrategy.SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == PublishStrategy.SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def fill_content(self, page: Page, content: str, tags: list = None):
        """填写微博正文和话题"""
        weibo_logger.info(_msg("✍️", "正在填写微博内容..."))

        # 找到正文输入框
        content_input = page.locator('textarea[placeholder*="有什么新鲜事"], textarea[placeholder*="分享"], div[contenteditable="true"]').first
        await content_input.wait_for(state="visible", timeout=10000)
        await content_input.click()

        # 构建完整内容
        full_content = content
        if tags:
            tags_text = " ".join([f"#{tag}#" for tag in tags])
            full_content = f"{content}\n{tags_text}"

        # 输入内容
        await page.keyboard.type(full_content, delay=30)
        weibo_logger.success(_msg("✅", "内容填写完成"))

    async def set_schedule_time(self, page: Page, publish_date: datetime):
        """设置定时发布"""
        weibo_logger.info(_msg("🕒", f"设置定时发布时间: {publish_date.strftime(self.date_format)}"))

        # 查找定时发布按钮
        schedule_btn = page.locator('text="定时发布", button:has-text("定时")').first
        if await schedule_btn.count():
            await schedule_btn.click()
            await page.wait_for_timeout(1000)

            # 设置日期时间
            # 具体选择器需要根据实际页面调整
            date_input = page.locator('input[type="date"], input[placeholder*="日期"]').first
            if await date_input.count():
                await date_input.fill(publish_date.strftime("%Y-%m-%d"))

            time_input = page.locator('input[type="time"], input[placeholder*="时间"]').first
            if await time_input.count():
                await time_input.fill(publish_date.strftime("%H:%M"))

            # 确认
            confirm_btn = page.locator('button:has-text("确定"), button:has-text("确认")').first
            if await confirm_btn.count():
                await confirm_btn.click()

            weibo_logger.success(_msg("✅", "定时发布设置完成"))
        else:
            weibo_logger.warning(_msg("⚠️", "未找到定时发布按钮，可能不支持定时发布"))


class WeiboVideo(WeiboBaseUploader):
    """微博视频发布器"""

    def __init__(
        self,
        title: str,
        file_path: str,
        tags: list = None,
        publish_date: datetime | int = 0,
        account_file: str = "",
        desc: str | None = None,
        publish_strategy: str = WEIBO_PUBLISH_STRATEGY_IMMEDIATE,
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
        self.desc = desc or ""

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频发布需要提供标题")

        self.file_path = str(self.validate_video_file(self.file_path))

    async def upload_video_content(self, page: Page) -> str | None:
        """上传视频内容"""
        weibo_logger.info(_msg("🏃", f"开始上传视频: {self.title}"))
        weibo_logger.info(_msg("🧭", "正在访问微博视频上传页面..."))

        await page.goto(WEIBO_UPLOAD_CHANNEL_URL)

        # 选择视频文件：优先直接使用页面已有的文件输入框，
        # 点击"上传视频"按钮触发 file chooser 仅作为后备。
        weibo_logger.info(_msg("🔍", "查找视频文件输入入口..."))
        try:
            await _select_weibo_video_file(page, self.file_path)
        except LoginExpiredError as exc:
            raise _WeiboPreMediaLoginExpired(str(exc)) from exc
        except Exception as exc:
            # 在首次选择媒体前，页面跳转到登录页可安全判定为 cookie 失效。
            if await _is_weibo_login_page(page):
                raise _WeiboPreMediaLoginExpired("cookie 已失效，请重新扫码登录") from exc
            raise
        weibo_logger.info(_msg("📤", "视频文件已选择，开始上传..."))

        # 等待编辑表单出现（上传开始后会显示标题/类型等表单）
        # 先等待上传进度条出现，确认上传真正开始
        weibo_logger.info(_msg("⏳", "等待视频上传开始..."))
        for i in range(30):
            upload_started = await page.evaluate("""() => {
                const bar = document.querySelector('._pro_109u9_49');
                if (bar) {
                    const match = (bar.style.transform || '').match(/scaleX\\(([\\d.]+)\\)/);
                    if (match && parseFloat(match[1]) > 0) return true;
                }
                // 也要检查是否秒传完成
                const bodyText = document.body.innerText || '';
                if (bodyText.includes('上传完成')) return true;
                if (bodyText.includes('视频已上传成功')) return 'auto';
                return false;
            }""")
            if upload_started == 'auto':
                weibo_logger.success(_msg("🥳", "视频秒传成功，已自动发布"))
                return
            if upload_started:
                weibo_logger.info(_msg("✅", "视频上传已开始"))
                break
            await page.wait_for_timeout(2000)
        else:
            raise RuntimeError("视频上传未开始，可能上传请求被拒绝")

        # 填写表单
        title_input = page.locator('input[placeholder*="填写标题"]').first
        if not await title_input.is_visible():
            raise RuntimeError("上传开始后未找到标题输入框")

        # 填写标题
        weibo_logger.info(_msg("✍️", "正在填写视频标题..."))
        await title_input.fill(self.title)
        weibo_logger.success(_msg("✅", f"标题已填写: {self.title}"))

        # 填写描述（如有）
        if self.desc:
            desc_input = page.locator('textarea[placeholder*="新鲜事"], textarea[placeholder*="描述"]').first
            if await desc_input.count() and await desc_input.is_visible():
                await desc_input.fill(self.desc)
                weibo_logger.success(_msg("✅", "描述已填写"))

        # 填写话题标签
        if self.tags:
            tags_text = " ".join([f"#{tag}#" for tag in self.tags])
            desc_input = page.locator('textarea[placeholder*="新鲜事"], textarea[placeholder*="描述"]').first
            if await desc_input.count() and await desc_input.is_visible():
                current_desc = await desc_input.input_value()
                await desc_input.fill(f"{current_desc}\n{tags_text}" if current_desc else tags_text)
            weibo_logger.success(_msg("✅", f"标签已填写: {tags_text}"))

        # 选择视频类型（必选：原创/二创/转载，默认选"原创"）
        # 必须模拟真人点击，不能用 JS 直接操作 DOM，否则会被检测为自动化脚本
        weibo_logger.info(_msg("📝", "正在选择视频类型..."))

        # 先关闭可能存在的 toast 弹窗（遮挡点击）
        toast = page.locator('[class*="woo-toast"]').first
        if await toast.count() and await toast.is_visible():
            weibo_logger.info(_msg("🔴", "检测到 toast 弹窗，尝试关闭"))
            try:
                close_btn = toast.locator('[class*="close"], [class*="Close"], button').first
                if await close_btn.count():
                    await close_btn.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

        # 用 Playwright 原生点击模拟真人操作
        # 点击包含"原创"文字的 label 元素
        try:
            original_label = page.locator('label.woo-radio-main:has-text("原创")').first
            await original_label.wait_for(state="visible", timeout=5000)
            await original_label.click()
            await page.wait_for_timeout(500)
            weibo_logger.success(_msg("✅", "已选择类型: 原创"))
        except Exception as e:
            weibo_logger.warning(_msg("⚠️", f"点击原创 label 失败: {e}，尝试点击文字区域..."))
            try:
                # 备选：点击"原创"文字所在的 span
                original_text = page.locator('span.woo-radio-text:has-text("原创")').first
                await original_text.click()
                await page.wait_for_timeout(500)
                weibo_logger.success(_msg("✅", "已选择类型: 原创（通过文字点击）"))
            except Exception as e2:
                weibo_logger.error(_msg("❌", f"选择类型失败: {e2}"))

        # 选择内容声明（必选）
        # 2026-08 起微博改版:触发按钮文案改为"请进行内容声明（必填）"放在 title 属性,
        # 选项文案从"内容无需标注"改为"我的内容无需声明"。保留旧文案做回退。
        weibo_logger.info(_msg("📝", "正在选择内容声明..."))
        try:
            trigger = page.locator('div[title*="内容声明"]').first
            if not await trigger.count():
                trigger = page.locator('text=内容声明').locator('xpath=following-sibling::*').first
            await trigger.wait_for(state="visible", timeout=5000)
            await trigger.click()
            await page.wait_for_timeout(800)

            option = None
            selected_text = None
            for option_text in ["我的内容无需声明", "内容无需标注"]:
                loc = page.locator(f'button:has-text("{option_text}")').first
                if await loc.count() and await loc.is_visible():
                    option = loc
                    selected_text = option_text
                    break

            if option is None:
                raise RuntimeError("未找到内容声明选项")

            await option.click()
            await page.wait_for_timeout(800)

            confirm_btn = page.locator('button:has-text("确定")').first
            if await confirm_btn.count() and await confirm_btn.is_visible():
                await confirm_btn.click()
                await page.wait_for_timeout(800)

            weibo_logger.success(_msg("✅", f"已选择内容声明: {selected_text}"))
        except Exception as e:
            weibo_logger.error(_msg("❌", f"选择内容声明失败: {e}"))

        # 设置定时发布
        if self.publish_strategy == WEIBO_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time(page, self.publish_date)

        # 等待上传完成（如果还在上传中）
        weibo_logger.info(_msg("⏳", "等待视频上传完成..."))
        for i in range(120):
            # 检查是否已上传完成或已自动发布
            upload_done = await page.evaluate("""() => {
                const bodyText = document.body.innerText || '';
                if (bodyText.includes('上传完成')) return 'done';
                if (bodyText.includes('视频已上传成功')) return 'auto_published';
                const bar = document.querySelector('._pro_109u9_49');
                if (bar) {
                    const match = (bar.style.transform || '').match(/scaleX\\(([\\d.]+)\\)/);
                    if (match) return Math.round(parseFloat(match[1]) * 100);
                }
                return null;
            }""")
            if upload_done == 'auto_published':
                weibo_logger.success(_msg("🥳", "视频已上传成功，自动发布"))
                return None  # 自动发布时无法获取视频链接
            if upload_done == 'done':
                weibo_logger.success(_msg("🥳", "视频上传完成"))
                break
            if upload_done is not None:
                weibo_logger.info(_msg("⏳", f"视频上传中... {upload_done}%"))
            await page.wait_for_timeout(5000)

        # 点击发布按钮
        weibo_logger.info(_msg("🚀", "正在发布..."))
        publish_btn = page.locator('button:has-text("发布"):not([disabled])').first
        try:
            await publish_btn.wait_for(state="visible", timeout=10000)
            await publish_btn.click()
        except Exception as e:
            weibo_logger.warning(_msg("⚠️", f"查找发布按钮超时: {e}"))

        # 等待发布成功提示出现：页面会显示"视频已上传成功"
        weibo_logger.info(_msg("⏳", "等待发布结果..."))
        for i in range(60):  # 等待最多60秒
            body_text = await page.evaluate("() => document.body.innerText || ''")
            if "视频已上传成功" in body_text:
                weibo_logger.success(_msg("🥳", "视频发布成功"))

                # === 跳转到视频管理页，轮询检查审核状态并获取视频链接 ===
                weibo_logger.info(_msg("🧭", "正在跳转到视频管理页..."))
                video_manage_url = "https://me.weibo.com/content/video"
                await page.goto(video_manage_url)
                await page.wait_for_timeout(3000)

                weibo_logger.info(_msg("📍", f"当前 URL: {page.url}"))

                # 等待视频列表加载
                weibo_logger.info(_msg("⏳", "等待视频列表加载..."))
                for i in range(10):
                    video_count = await page.locator('.vue-recycle-scroller__item-view').count()
                    if video_count > 0:
                        weibo_logger.info(_msg("✅", f"视频列表已加载，共 {video_count} 个视频"))
                        break
                    await page.wait_for_timeout(2000)

                # 轮询检查第一个视频是否有编辑按钮（有编辑按钮 = 审核通过）
                weibo_logger.info(_msg("🔍", "开始检查第一个视频的审核状态..."))
                max_retries = 30  # 最多检查30次，每次5秒，共150秒
                video_link = None

                for i in range(max_retries):
                    # 刷新页面获取最新状态
                    await page.reload()
                    await page.wait_for_timeout(2000)

                    # 等待视频列表加载
                    for j in range(5):
                        video_count = await page.locator('.vue-recycle-scroller__item-view').count()
                        if video_count > 0:
                            break
                        await page.wait_for_timeout(1000)

                    first_video = page.locator('.vue-recycle-scroller__item-view').first
                    edit_btn = first_video.locator('button:has-text("编辑")')
                    has_edit_btn = await edit_btn.count() > 0

                    if has_edit_btn:
                        weibo_logger.success(_msg("✅", f"第 {i+1} 次检查: 发现编辑按钮，审核通过！"))

                        # 点击第一个视频封面，会打开新页签
                        weibo_logger.info(_msg("👆", "点击第一个视频获取链接..."))
                        video_link = await _open_first_video_link(page)
                        break
                    else:
                        weibo_logger.info(_msg("⏳", f"第 {i+1} 次检查: 未发现编辑按钮，审核中..."))
                        await page.wait_for_timeout(5000)
                else:
                    weibo_logger.warning(_msg("⚠️", "审核超时，请稍后手动检查"))

                return video_link
            # 同时检查失败标志
            if "上传失败" in body_text or "发布失败" in body_text:
                raise RuntimeError("微博提示发布失败")
            await page.wait_for_timeout(1000)

        # 超时未检测到成功提示，需要手动确认
        weibo_logger.warning(_msg("⚠️", "未检测到明确的发布成功提示，请手动确认发布状态"))
        return None

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras"""
        weibo_logger.info(_msg("🧍", "检查 cookie 和视频文件..."))
        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            try:
                await self.validate_upload_args()
            except LoginExpiredError as exc:
                raise _WeiboPreMediaLoginExpired(str(exc)) from exc
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
        except _WeiboPreMediaLoginExpired as e:
            result.update(build_login_expired_result(str(e) or "cookie 已失效，请重新扫码登录"))
            weibo_logger.error(_msg("❌", f"上传失败: {e}"))
        except Exception as e:
            result["message"] = str(e)
            weibo_logger.error(_msg("❌", f"上传失败: {e}"))

        return result


class WeiboNote(WeiboBaseUploader):
    """微博图文发布器"""

    def __init__(
        self,
        image_paths: list,
        note: str,
        tags: list = None,
        publish_date: datetime | int = 0,
        account_file: str = "",
        title: str | None = None,
        publish_strategy: str = WEIBO_PUBLISH_STRATEGY_IMMEDIATE,
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
        self.note = note
        self.tags = tags or []
        self.title = title or note[:50] if note else ""

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.image_paths:
            raise ValueError("图文发布需要提供图片")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_paths = []
        for img_path in self.image_paths:
            normalized_paths.append(str(self.validate_image_file(img_path)))
        self.image_paths = normalized_paths[:9]  # 微博最多9张图

    async def upload_note_content(self, page: Page) -> None:
        """上传图文内容"""
        weibo_logger.info(_msg("🏃", f"开始上传图文，共 {len(self.image_paths)} 张图片"))
        weibo_logger.info(_msg("🧭", "正在访问微博创作者中心..."))

        await page.goto(WEIBO_MAIN_URL)

        # 查找图片上传入口
        image_upload_selectors = [
            'input[type="file"][accept*="image"]',
            'input[type="file"][accept*=".jpg"]',
            'input[type="file"][accept*=".png"]',
        ]

        try:
            upload_input = await _wait_for_weibo_image_input(page, image_upload_selectors)
        except LoginExpiredError as exc:
            raise _WeiboPreMediaLoginExpired(str(exc)) from exc

        # 上传图片
        await upload_input.set_input_files(self.image_paths)
        weibo_logger.info(_msg("📤", f"已选择 {len(self.image_paths)} 张图片，等待上传..."))

        # 等待图片上传完成
        max_wait = 120
        for _ in range(max_wait // 3):
            await page.wait_for_timeout(3000)

            # 检查是否有上传成功的图片预览
            if await page.locator('img[src*="blob"], img[src*="http"]').count() >= len(self.image_paths):
                weibo_logger.success(_msg("🥳", "图片上传完成"))
                break

            weibo_logger.info(_msg("⏳", "图片上传中..."))

        # 填写内容
        await self.fill_content(page, self.note, self.tags)

        # 设置定时发布
        if self.publish_strategy == WEIBO_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time(page, self.publish_date)

        # 点击发布
        weibo_logger.info(_msg("🚀", "正在发布..."))
        publish_btn = page.locator('button:has-text("发布"), button:has-text("发送")').first
        await publish_btn.click()
        await page.wait_for_timeout(3000)

        # 检查发布结果
        if await page.locator('text="发布成功", text="已发布"').count():
            weibo_logger.success(_msg("🥳", "图文发布成功"))
        else:
            weibo_logger.info(_msg("✅", "发布请求已提交"))

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras"""
        weibo_logger.info(_msg("🧍", "检查 cookie 和图片文件..."))
        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            try:
                await self.validate_upload_args()
            except LoginExpiredError as exc:
                raise _WeiboPreMediaLoginExpired(str(exc)) from exc
            weibo_logger.info(_msg("🥳", "上传前检查通过"))
            async with self._browser_session(save_on_success_only=True) as page:
                await self.upload_note_content(page)
                result["success"] = True
                result["message"] = "发布成功"
            weibo_logger.success(_msg("🥳", "cookie 更新完毕"))
        except _WeiboPreMediaLoginExpired as e:
            result.update(build_login_expired_result(str(e) or "cookie 已失效，请重新扫码登录"))
            weibo_logger.error(_msg("❌", f"上传失败: {e}"))
        except Exception as e:
            result["message"] = str(e)
            weibo_logger.error(_msg("❌", f"上传失败: {e}"))

        return result
