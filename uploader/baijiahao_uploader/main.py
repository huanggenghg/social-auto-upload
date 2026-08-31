# -*- coding: utf-8 -*-
from __future__ import annotations

import random
from datetime import datetime

from patchright.async_api import Playwright, async_playwright, Page
import os
import time
import asyncio
import re

from conf import LOCAL_CHROME_PATH, LOCAL_CHROME_HEADLESS
from uploader.base_video import (
    BaseBrowserUploader,
    PlatformResultExtras,
    PublishStrategy,
    _msg,
)
from utils.base_social_media import set_init_script
from utils.log import baijiahao_logger
from utils.network import async_retry

BAIJIAHAO_HOME_URL = "https://baijiahao.baidu.com/builder/rc/home"
BAIJIAHAO_COVER_WAIT_TIMEOUT = 300
BAIJIAHAO_UPLOAD_WAIT_TIMEOUT = 1800
BAIJIAHAO_LOGIN_URL = "https://baijiahao.baidu.com/builder/theme/bjh/login"
BAIJIAHAO_UPLOAD_EDIT_URL = "https://baijiahao.baidu.com/builder/rc/edit?type=videoV2&is_from_cms=1"
BAIJIAHAO_LOGIN_URL_MARKERS = ("/login", "login")
BAIJIAHAO_PUBLISH_MARKERS = [
    "发布作品",
    "发布",
    "上传",
]


def _extract_bjh_public_url_from_preview_href(href: str | None) -> str | None:
    """从 builder/preview/s?id={ID} href 提取 id,拼公开链接 https://baijiahao.baidu.com/s?id={ID}。"""
    if not href:
        return None
    m = re.search(r"[?&]id=(\d+)", href)
    return f"https://baijiahao.baidu.com/s?id={m.group(1)}" if m else None


async def _is_baijiahao_locator_visible(locator) -> bool:
    try:
        if not await locator.count():
            return False
        return await locator.is_visible()
    except Exception:
        return False


async def _is_baijiahao_locator_present(locator) -> bool:
    try:
        return bool(await locator.count())
    except Exception:
        return False


async def _is_baijiahao_auth_page_valid(page: Page) -> bool:
    current_url = (page.url or "").lower()
    if any(marker in current_url for marker in BAIJIAHAO_LOGIN_URL_MARKERS):
        return False

    login_markers = [
        page.get_by_text("登录/注册百家号").first,
        page.get_by_text("扫码登录").first,
    ]
    for marker in login_markers:
        if await _is_baijiahao_locator_visible(marker):
            return False

    publish_markers = [
        page.locator("input[type=file]").first,
        page.locator('button:has-text("发布")').first,
        page.locator('button:has-text("上传")').first,
        page.locator("div#formMain").first,
        page.locator('[id^="asideMenuItem-"]').first,
        page.get_by_text("发布作品").first,
    ]
    return any([await _is_baijiahao_locator_present(marker) for marker in publish_markers])


async def cookie_auth(account_file):
    """验证 cookie 是否有效 - 委托 BaiJiaHaoVideo.cookie_auth"""
    return await BaiJiaHaoVideo.cookie_auth(account_file)


async def baijiahao_setup(
    account_file,
    handle=False,
    return_detail=False,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    """百家号登录设置 - 委托 BaiJiaHaoVideo.setup"""
    return await BaiJiaHaoVideo.setup(account_file, handle, return_detail, qrcode_callback, headless)


class BaiJiaHaoVideo(BaseBrowserUploader):
    """百家号视频发布器 (1-tier, 直接继承 BaseBrowserUploader)。
    保留 @async_retry 装饰器(uploading_video / publish_video)和 ai2video 辅助方法。"""

    PLATFORM_NAME = "baijiahao"
    UPLOAD_URL = BAIJIAHAO_HOME_URL
    LOGIN_URL = BAIJIAHAO_LOGIN_URL
    LOGIN_MARKERS = list(BAIJIAHAO_LOGIN_URL_MARKERS)
    PUBLISH_MARKERS = list(BAIJIAHAO_PUBLISH_MARKERS)

    BAIJIAHAO_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.4324.150 Safari/537.36"
    )

    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime,
        account_file,
        proxy_setting=None,
        publish_strategy: str = PublishStrategy.IMMEDIATE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless
        self.proxy_setting = proxy_setting
        self.publish_strategy = publish_strategy

    @classmethod
    async def _init_context(cls, browser, account_file: str | None = None):
        """Override: 百家号需要自定义 user_agent + geolocation 权限。"""
        context_kwargs = {
            "user_agent": cls.BAIJIAHAO_USER_AGENT,
            "permissions": ["geolocation"],
        }
        if account_file and os.path.exists(account_file):
            context_kwargs["storage_state"] = account_file
        context = await browser.new_context(**context_kwargs)
        return await set_init_script(context)

    @classmethod
    async def is_login_completed(cls, page: Page) -> bool:
        """Override: 百家号登录完成需要 DOM marker 校验,不能只看 URL。"""
        return await _is_baijiahao_auth_page_valid(page)

    @classmethod
    async def cookie_auth(cls, account_file: str) -> bool:
        """Override: 百家号 cookie 校验需要 DOM marker 检查(_is_baijiahao_auth_page_valid)。"""
        if not os.path.exists(account_file):
            return False
        async with async_playwright() as playwright:
            browser = await cls._launch_browser(playwright, headless=LOCAL_CHROME_HEADLESS)
            try:
                context = await cls._init_context(browser, account_file)
                page = await context.new_page()
                try:
                    await page.goto(cls.UPLOAD_URL, timeout=60000, wait_until="domcontentloaded")
                except Exception as exc:
                    baijiahao_logger.warning(f"home 页 goto 异常(继续检测): {exc}")
                await page.wait_for_timeout(timeout=5000)

                if await _is_baijiahao_auth_page_valid(page):
                    baijiahao_logger.success(_msg("🥳", "cookie 有效"))
                    return True

                baijiahao_logger.error("等待5秒 cookie 失效")
                return False
            except Exception:
                return False
            finally:
                await browser.close()

    async def validate_login_and_strategy(self):
        """检查 cookie 存在/有效 + publish_strategy + publish_date。
        Renamed from validate_base_args(self) to avoid collision with
        BasePlatformUploader.validate_base_args(params) staticmethod (called by dispatch)."""
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成百家号登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成百家号登录: {self.account_file}")
        if self.publish_strategy not in {PublishStrategy.IMMEDIATE, PublishStrategy.SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == PublishStrategy.SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def validate_upload_args(self):
        await self.validate_login_and_strategy()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")
        self.file_path = str(self.validate_video_file(self.file_path))

    async def set_schedule_time(self, page, publish_date):
        """选择定时发布的日期/时/分,然后点确认。"""
        publish_date_day = f"{publish_date.month}月{publish_date.day:02d}日"
        publish_date_hour = f"{publish_date.hour}点"
        publish_date_min = f"{publish_date.minute}分"
        await page.wait_for_selector('div.select-wrap', timeout=5000)

        async def open_dropdown_and_pick(idx: int, option_text: str, label: str):
            """打开第 idx 个 select-wrap,在虚拟列表里滚动查找文本匹配的选项并点击。"""
            await page.locator('div.select-wrap').nth(idx).click()
            await page.wait_for_selector('div.rc-virtual-list:visible', timeout=5000)
            await page.wait_for_timeout(300)

            option_locator = page.locator(
                f'div.rc-virtual-list:visible div.cheetah-select-item-option:has-text("{option_text}"),'
                f'div.rc-virtual-list:visible div.cheetah-select-item:has-text("{option_text}")'
            ).first

            # 虚拟列表只渲染可见区域,需要滚动查找
            for scroll_attempt in range(40):
                if await option_locator.count() > 0:
                    await option_locator.click()
                    await page.wait_for_timeout(300)
                    return
                # 滚动 rc-virtual-list-holder
                scrolled = await page.evaluate("""
                    () => {
                        const lists = Array.from(document.querySelectorAll('div.rc-virtual-list'));
                        const visible = lists.filter(l => {
                            const r = l.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                        if (!visible.length) return false;
                        const list = visible[visible.length - 1];
                        const holder = list.querySelector('div.rc-virtual-list-holder');
                        if (!holder) return false;
                        const before = holder.scrollTop;
                        holder.scrollTop += 80;
                        return holder.scrollTop !== before;
                    }
                """)
                if not scrolled:
                    break
                await page.wait_for_timeout(150)

            # 滚完还没找到 -> dump 可见选项辅助排查
            visible_options = await page.evaluate("""
                () => {
                    const lists = Array.from(document.querySelectorAll('div.rc-virtual-list'));
                    const visible = lists.filter(l => {
                        const r = l.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    });
                    if (!visible.length) return [];
                    const list = visible[visible.length - 1];
                    const items = list.querySelectorAll('div.cheetah-select-item, div.cheetah-select-item-option');
                    return Array.from(items).slice(0, 30).map(it => (it.innerText || '').trim());
                }
            """)
            baijiahao_logger.error(f"{label} 选项未找到 '{option_text}',可见选项: {visible_options}")
            raise ValueError(f"{label} 选项未找到 '{option_text}',可见选项: {visible_options}")

        # 1. 日期
        await open_dropdown_and_pick(0, publish_date_day, "日期")
        # 2. 时
        await open_dropdown_and_pick(1, publish_date_hour, "时")
        # 3. 分
        await open_dropdown_and_pick(2, publish_date_min, "分")

        # 4. 点弹窗内确认按钮(用 cheetah-modal-confirm-btns 限定,避免匹配到底层触发按钮)
        await page.locator('div.cheetah-modal-confirm-btns button:has-text("定时发布")').click()

    async def handle_upload_error(self, page):
        # 日后实现，目前没遇到
        return
        print("视频出错了，重新上传中")

    async def upload_video_content(self, page: Page) -> str | None:
        """上传视频内容(页面已通过 _browser_session 打开)。返回视频公开链接或 None。"""
        # 直接访问带 is_from_cms=1 参数的 URL
        await page.goto(BAIJIAHAO_UPLOAD_EDIT_URL, timeout=60000)
        baijiahao_logger.info(f"正在上传-------{self.title}.mp4")
        baijiahao_logger.info(f"已打开页面: {page.url}")

        # 等待上传区域加载
        baijiahao_logger.info("等待上传区域加载...")
        await page.wait_for_timeout(3000)

        # 检查 input 元素状态
        input_count = await page.locator("input[type=file]").count()
        baijiahao_logger.info(f"找到 {input_count} 个 file input 元素")

        # 打印每个 input 的详细信息
        for i in range(input_count):
            input_elem = page.locator("input[type=file]").nth(i)
            accept = await input_elem.get_attribute("accept")
            multiple = await input_elem.get_attribute("multiple")
            visible = await input_elem.is_visible()
            baijiahao_logger.info(f"Input {i}: accept={accept}, multiple={multiple}, visible={visible}")

        # 设置文件
        await page.locator("input[type=file]").set_input_files(self.file_path)
        baijiahao_logger.info("视频文件已选择")

        # 等待上传开始（页面会显示上传进度）
        baijiahao_logger.info("等待上传开始...")
        await asyncio.sleep(3)

        # 检查上传是否已经开始
        uploading = await page.locator('div .cover-overlay:has-text("上传中")').count()
        upload_progress = await page.locator('div[class*="progress"]').count()
        baijiahao_logger.info(f"上传中状态: {uploading}, 进度条: {upload_progress}")

        # 等待页面跳转到视频发布页面（上传过程中会自动跳转）
        max_wait_time = 120  # 最多等待120秒
        start_time = time.time()
        while time.time() - start_time < max_wait_time:
            try:
                # 检查 formMain 是否存在
                form_main = await page.locator("div#formMain").count()
                if form_main > 0:
                    baijiahao_logger.info("已进入视频发布页面")
                    break

                # 检查上传状态
                uploading = await page.locator('div .cover-overlay:has-text("上传中")').count()
                upload_failed = await page.locator('div .cover-overlay:has-text("上传失败")').count()

                if upload_failed:
                    baijiahao_logger.error("上传失败")
                    raise Exception("视频上传失败")

                if uploading:
                    baijiahao_logger.info("正在上传视频中...")
                else:
                    # 打印页面上的一些信息用于调试
                    page_url = page.url
                    baijiahao_logger.info(f"当前页面URL: {page_url}")

                    # 检查是否有其他表单元素
                    title_input = await page.locator('input[placeholder*="标题"]').count()
                    desc_input = await page.locator('textarea').count()
                    baijiahao_logger.info(f"标题输入框: {title_input}, 描述输入框: {desc_input}")

                await asyncio.sleep(2)
            except Exception as e:
                baijiahao_logger.info(f"检查页面状态: {e}")
                await asyncio.sleep(2)
        else:
            # 超时，打印页面HTML帮助调试
            baijiahao_logger.error("等待页面跳转超时")
            html_content = await page.content()
            baijiahao_logger.info(f"页面内容长度: {len(html_content)}")
            raise Exception("等待视频发布页面超时")

        # 填充标题和话题
        # 这里为了避免页面变化，故使用相对位置定位：作品标题父级右侧第一个元素的input子元素
        await asyncio.sleep(1)
        baijiahao_logger.info("正在填充标题和话题...")
        await self.add_title_tags(page)

        upload_status = await self.uploading_video(page)
        if not upload_status:
            baijiahao_logger.error(f"发现上传出错了... 文件:{self.file_path}")
            raise

        # 判断视频封面图是否生成成功
        cover_deadline = time.monotonic() + BAIJIAHAO_COVER_WAIT_TIMEOUT
        while time.monotonic() < cover_deadline:
            baijiahao_logger.info("正在确认封面完成, 准备去点击定时/发布...")
            if await page.locator("div.cheetah-spin-container img").count():
                baijiahao_logger.info("封面已完成，点击定时/发布...")
                break
            else:
                baijiahao_logger.info("等待封面生成...")
                await asyncio.sleep(3)
        else:
            raise TimeoutError(f"等待封面生成超时({BAIJIAHAO_COVER_WAIT_TIMEOUT}秒)")

        await self.select_creation_declaration(page)
        await self.publish_video(page, self.publish_date)
        await page.wait_for_timeout(2000)
        if await page.locator('div.passMod_dialog-container >> text=百度安全验证:visible').count():
            baijiahao_logger.error("出现验证，退出")
            raise Exception("出现验证，退出")
        video_link = None
        try:
            await page.wait_for_url("https://baijiahao.baidu.com/builder/rc/clue**", timeout=30000)
            baijiahao_logger.success("视频发布成功")
        except Exception:
            current_url = page.url
            baijiahao_logger.warning(f"未跳转到 clue 页, 当前 URL: {current_url}")
            body_text = await page.evaluate(
                "() => (document.body && document.body.innerText) ? document.body.innerText.slice(0, 1000) : ''"
            )
            if "发布成功" in body_text or "成功" in body_text:
                baijiahao_logger.success(f"检测到发布成功标志, URL: {current_url}")
            else:
                baijiahao_logger.error(f"发布可能失败, body 文本前 500 字: {body_text[:500]}")
                raise Exception(f"发布后未跳转 clue 页, 当前 URL: {current_url}")

        if self.publish_date == 0:
            try:
                video_link = await self._capture_content_url(page)
            except Exception as e:
                baijiahao_logger.warning(f"内容链接抓取异常(不算发布失败): {e}")
                video_link = None
        else:
            baijiahao_logger.info("定时发布,跳过内容链接抓取")

        return video_link

    async def upload(self) -> PlatformResultExtras:
        """主入口，返回 PlatformResultExtras。"""
        baijiahao_logger.info(_msg("🧍", "小人先检查 cookie 和视频文件"))
        await self.validate_upload_args()
        baijiahao_logger.info(_msg("🥳", "上传前检查通过"))

        result: PlatformResultExtras = {"success": False, "message": ""}

        try:
            async with self._browser_session(save_on_success_only=True) as page:
                video_link = await self.upload_video_content(page)
                result["success"] = True
                if video_link:
                    result["result_url"] = video_link
                    result["message"] = f"发布成功，视频链接: {video_link}"
                else:
                    result["message"] = "发布成功"
            baijiahao_logger.success(_msg("🥳", "cookie 更新完毕"))
        except Exception as e:
            result["message"] = str(e)
            baijiahao_logger.error(_msg("❌", f"上传失败: {e}"))

        return result

    async def _capture_content_url(self, page: Page) -> str | None:
        """跳转内容管理页,取第一条列表项的公开链接。抓不到返回 None。"""
        content_url = "https://baijiahao.baidu.com/builder/rc/content"
        try:
            await page.goto(content_url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            baijiahao_logger.warning(f"goto 内容管理页异常(继续): {e}")

        try:
            await page.wait_for_function(
                "() => { const r = document.querySelector('#root'); return r && r.children.length > 0; }",
                timeout=30000,
            )
        except Exception:
            pass

        item_locator = page.locator("div.client_pages_content_v2_components_articleItem")
        for _ in range(6):
            if await item_locator.count() > 0:
                break
            await asyncio.sleep(5)
        else:
            baijiahao_logger.warning("内容管理页未出现列表项,跳过链接抓取")
            return None

        try:
            href = await item_locator.first.locator('a[href*="builder/preview/s?id="]').first.get_attribute("href")
        except Exception as e:
            baijiahao_logger.warning(f"未找到预览链接(继续): {e}")
            return None
        public_url = _extract_bjh_public_url_from_preview_href(href)
        if public_url:
            baijiahao_logger.success(f"已抓取内容公开链接: {public_url}")
        else:
            baijiahao_logger.warning(f"无法从 href 提取 id: {href}")
        return public_url


    @async_retry(timeout=300)  # 例如，最多重试3次，超时时间为180秒
    async def uploading_video(self, page):
        # async_retry 只在异常时计时,这里必须自己兜底防止"上传中"永远不消失
        upload_deadline = time.monotonic() + BAIJIAHAO_UPLOAD_WAIT_TIMEOUT
        while time.monotonic() < upload_deadline:
            upload_failed = await page.locator('div .cover-overlay:has-text("上传失败")').count()
            if upload_failed:
                baijiahao_logger.error("发现上传出错了...")
                # await self.handle_upload_error(page)  # 假设这是处理上传错误的函数
                return False

            uploading = await page.locator('div .cover-overlay:has-text("上传中")').count()
            if uploading:
                baijiahao_logger.info("正在上传视频中...")
                await asyncio.sleep(2)  # 等待2秒再次检查
                continue

            # 检查上传是否成功
            if not uploading and not upload_failed:
                baijiahao_logger.success("视频上传完毕")
                return True
        raise TimeoutError(f"等待视频上传完成超时({BAIJIAHAO_UPLOAD_WAIT_TIMEOUT}秒)")

    async def set_schedule_publish(self, page, publish_date):
        while True:
            schedule_element = page.locator("div.op-btn-outter-content", has_text="定时发布").locator("button")
            try:
                await schedule_element.click()
                await page.wait_for_selector('div.select-wrap:visible', timeout=3000)
                await page.wait_for_timeout(timeout=2000)
                baijiahao_logger.info("开始点击发布定时...")
                await self.set_schedule_time(page, publish_date)
                break
            except Exception as e:
                baijiahao_logger.error(f"定时发布失败: {e}")
                # 关闭可能残留的定时弹窗,避免遮挡按钮导致 retry 点击失败
                try:
                    await page.keyboard.press('Escape')
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
                raise  # 重新抛出异常，让重试装饰器捕获

    @async_retry(timeout=300)  # 例如，最多重试3次，超时时间为180秒
    async def publish_video(self, page: Page, publish_date):
        if publish_date != 0:
            # 定时发布
            await self.set_schedule_publish(page, publish_date)
        else:
            # 立即发布
            await self.direct_publish(page)

    async def direct_publish(self, page):
        try:
            # 关闭可能存在的"我知道了"引导弹窗, 避免遮挡发布按钮
            know_button = page.locator('button:has-text("我知道了")')
            if await know_button.count():
                try:
                    await know_button.first.click(timeout=2000)
                    baijiahao_logger.info("已关闭引导弹窗")
                    await asyncio.sleep(1)
                except Exception:
                    pass

            publish_button = page.get_by_test_id("publish-btn")
            if await publish_button.count():
                disabled = await publish_button.get_attribute("disabled")
                baijiahao_logger.info(f"发布按钮 disabled={disabled}, 即将点击")
                await publish_button.click(force=True)
                baijiahao_logger.info(f"发布按钮已点击, 当前 URL: {page.url}")
                # 等待 2 秒后抓取页面状态, 看是否有错误提示或弹窗
                await asyncio.sleep(2)
                screenshot_path = "output/baijiahao_after_publish_click.png"
                from pathlib import Path as _Path
                _Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=screenshot_path, full_page=True)
                baijiahao_logger.info(f"点击后截图: {screenshot_path}")
                # 检查是否有错误提示
                error_toast = await page.locator('.cheetah-message-error, .cheetah-notification-error, [class*="error"][class*="toast"]').count()
                if error_toast:
                    error_text = await page.locator('.cheetah-message-error, .cheetah-notification-error, [class*="error"][class*="toast"]').first.text_content()
                    baijiahao_logger.error(f"检测到错误提示: {error_text}")
                # 检查是否有确认弹窗
                confirm_btn = await page.locator('button:has-text("确认"), button:has-text("确定"), button:has-text("继续发布")').count()
                if confirm_btn:
                    baijiahao_logger.info(f"检测到确认弹窗, 按钮数量: {confirm_btn}")
                    try:
                        await page.locator('button:has-text("确认"), button:has-text("确定"), button:has-text("继续发布")').first.click(timeout=2000)
                        baijiahao_logger.info("已点击确认按钮")
                    except Exception:
                        pass
            else:
                baijiahao_logger.error("未找到发布按钮")
        except Exception as e:
            baijiahao_logger.error(f"直接发布视频失败: {e}")
            raise  # 重新抛出异常，让重试装饰器捕获

    async def add_title_tags(self, page):
        # 百家号 videoV2 编辑页只有"作品描述"字段(contenteditable div), 无独立标题输入框
        # 用 title 填充作品描述
        if len(self.title) <= 8:
            self.title += " 你不知道的"
        editor = page.locator('div[contenteditable="true"][role="textbox"]').first
        await editor.click()
        await page.keyboard.press("ControlOrMeta+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(self.title[:30])

    async def select_creation_declaration(self, page) -> None:
        """点击创作声明 input, 在弹出的 modal 中选择'无需声明'并确定。"""
        declaration_input = page.locator('input[placeholder="请选择创作声明"]').first
        if not await declaration_input.count():
            baijiahao_logger.warning("未找到创作声明 input, 跳过")
            return
        baijiahao_logger.info("点击创作声明 input...")
        await declaration_input.click()
        try:
            await page.wait_for_selector('.cheetah-modal-title:has-text("创作声明")', timeout=5000)
        except Exception as e:
            baijiahao_logger.error(f"等待创作声明 modal 超时: {e}")
            raise
        option = page.locator('div.flex.items-center.cursor-pointer').filter(has_text="无需声明").first
        baijiahao_logger.info("选择'无需声明'...")
        await option.click()
        await asyncio.sleep(1)
        confirm_btn = page.locator('.cheetah-modal-footer button.cheetah-btn-primary').first
        baijiahao_logger.info("点击确定...")
        await confirm_btn.click()
        try:
            await page.wait_for_selector('.cheetah-modal-title:has-text("创作声明")', state='hidden', timeout=5000)
        except Exception:
            pass
        baijiahao_logger.success("创作声明已选为'无需声明'")



    # 使用 AI成片 功能
    async def ai2video(self, playwright: Playwright) -> None:
        # 使用 Chromium 浏览器启动一个浏览器实例
        browser = await playwright.chromium.launch(headless=self.headless, executable_path=self.local_executable_path, proxy=self.proxy_setting)
        # 创建一个浏览器上下文，使用指定的 cookie 文件
        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            storage_state=f"{self.account_file}",
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.4324.150 Safari/537.36'
        )
        # context = await set_init_script(context)
        await context.grant_permissions(['geolocation'])

        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://aigc.baidu.com/make", timeout=60000)
        # 等待页面跳转到指定的 URL，没进入，则自动等待到超时
        baijiahao_logger.info('正在打开主页...')
        await page.wait_for_url("https://aigc.baidu.com/make", timeout=60000)

        # 点击"全网"标签
        await page.locator('div.rounded-lg.border:has-text("全网")').click()
        await asyncio.sleep(1)  # 这里延迟是为了方便眼睛直观的观看

        # 点击 "上传视频" 按钮
        # await page.locator("div[class^='video-main-container'] input").set_input_files(self.file_path)

        # region 操作处

        # 生成日期时间键名（格式：ai2video_YYYYMMDDHHMM）
        now = datetime.now()
        datetime_str = now.strftime("%Y%m%d%H%M")
        processed_key = "ai2video_processed_titles"
        batch_key = f"ai2video_{datetime_str}"

        # 初始化LocalStorage
        await page.evaluate(f"""
                   if (!localStorage.getItem("{processed_key}")) {{
                       localStorage.setItem("{processed_key}", JSON.stringify([]));
                   }}
                   if (!localStorage.getItem("{batch_key}")) {{
                       localStorage.setItem("{batch_key}", JSON.stringify([]));
                   }}
               """)

        # 定位新闻列表容器（转义特殊CSS字符）
        container_selector = r'.overflow-auto.flex-grow.h-0.saas-scrollbar.mt\-\[-4px\].pl\-\[24px\].pr\-\[10px\].pb\-\[18px\]'
        news_items = await page.locator(container_selector).locator(r'div.py\-\[6px\].group.cursor-pointer').all()

        for item in news_items:
            try:
                # 获取新闻标题
                title_elem = item.locator(r'div.flex.text-gray-darker.items-center.relative.pr\-\[56px\] > span')
                title = await title_elem.text_content()
                if not title:
                    continue

                # 检查是否已处理过
                is_processed = await page.evaluate(
                    f"""title => {{
                               const processedList = JSON.parse(localStorage.getItem("{processed_key}") || "[]");
                               return processedList.includes(title);
                           }}""",
                    title
                )

                if is_processed:
                    print(f"[跳过] {title}")
                    continue

                # 悬停显示按钮（根据HTML结构，按钮在悬停时显示）
                await item.hover()

                # 点击生成文案按钮
                button = item.locator('button:has-text("生成文案")')
                await button.click()
                print(f"[点击] {title}")

                # 等待30秒
                # await page.wait_for_timeout(30000)
                print(f"[等待完成] {title}")

                # 监听"一键成片"按钮
                print(f"[开始监听] 一键成片按钮")
                should_exit_while_loop = False  # 添加标志变量
                while True:
                    # 定位"一键成片"按钮
                    one_key_button = page.locator("button:has-text('一键成片')")

                    # 检查按钮是否存在
                    if await one_key_button.count() > 0:
                        # 检查按钮是否有disabled属性
                        is_disabled = await one_key_button.get_attribute("disabled")

                        if is_disabled is None:
                            # 按钮不再被禁用，点击它
                            print(f"[发现可点击按钮] 一键成片")
                            await one_key_button.click()  # 先点击一键成片按钮

                            # 等待可能出现的"温馨提示"窗口
                            print(f"[检查] 是否出现温馨提示窗口")
                            await page.wait_for_timeout(2000)  # 等待2秒，让窗口有时间显示

                            try:
                                # 检查是否存在"温馨提示"窗口，设置较短的超时时间
                                tip_window = page.locator("div:has-text('温馨提示') >> visible=true")
                                if await tip_window.count() > 0:
                                    print(f"[发现] 温馨提示窗口")

                                    # 定位并点击"知道了"按钮，设置较短的超时时间
                                    know_button = page.locator("button:has-text('知道了')")
                                    if await know_button.count() > 0:
                                        try:
                                            # 设置较短的超时时间进行点击
                                            await know_button.click(timeout=5000)
                                            print(f"[已点击] 知道了按钮")
                                        except Exception as e:
                                            print(f"[警告] 点击知道了按钮时出错: {str(e)}")
                                    else:
                                        print(f"[警告] 未找到知道了按钮")
                                else:
                                    print(f"[信息] 未出现温馨提示窗口，继续执行")
                            except Exception as e:
                                print(f"[警告] 处理温馨提示窗口时出错: {str(e)}")
                                # 继续执行，不要因为这个错误中断流程

                            # 记录到LocalStorage前打印日志
                            print(f"[开始记录] 准备将标题 '{title}' 记录到LocalStorage")

                            # 记录到LocalStorage
                            await page.evaluate(
                                f"""
                                        (title, processedKey, batchKey) => {{
                                            // 更新已处理列表
                                            const processedList = JSON.parse(localStorage.getItem(processedKey) || "[]");
                                            if (!processedList.includes(title)) {{
                                                processedList.push(title);
                                                localStorage.setItem(processedKey, JSON.stringify(processedList));
                                            }}

                                            // 更新当前批次记录
                                            const batchList = JSON.parse(localStorage.getItem(batchKey) || "[]");
                                            if (!batchList.includes(title)) {{
                                                batchList.push(title);
                                                localStorage.setItem(batchKey, JSON.stringify(batchList));
                                            }}
                                        }}
                                        """,
                                title, processed_key, batch_key
                            )

                            # 记录完成后打印日志
                            print(f"[记录完成] 标题 '{title}' 已成功记录到LocalStorage")

                            print(f"[记录完成] {title}")

                            # 监听新打开的标签页
                            print(f"[监听] 等待新标签页打开")
                            # 获取当前所有页面
                            current_pages = context.pages
                            current_page_count = len(current_pages)

                            # 等待新标签页打开（最多等待10秒）
                            new_page = None
                            max_wait_time = 10  # 最大等待时间（秒）
                            start_time = time.time()

                            while time.time() - start_time < max_wait_time:
                                # 获取最新的页面列表
                                pages = context.pages
                                # 如果页面数量增加，说明新标签页已打开
                                if len(pages) > current_page_count:
                                    # 获取最新打开的页面（通常是列表中的最后一个）
                                    new_page = pages[-1]
                                    print(f"[发现] 新标签页已打开")
                                    break
                                # 短暂等待后再次检查
                                await asyncio.sleep(0.5)

                            # 如果找到新标签页，获取其标题和URL并保存
                            if new_page:
                                # 等待页面加载完成
                                try:
                                    await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
                                    # 获取页面标题和URL
                                    page_title = await new_page.title()
                                    page_url = new_page.url

                                    print(f"[获取] 标题: {page_title}")
                                    print(f"[获取] URL: {page_url}")

                                    # 将标题和URL保存到url.txt文件
                                    with open("url.txt", "a", encoding="utf-8") as f:
                                        f.write(f"{page_title}\n{page_url}\n\n")

                                    print(f"[保存] 标题和URL已保存到url.txt")

                                    # 等待5秒后关闭新标签页
                                    print(f"[等待] 5秒后将关闭新标签页")
                                    await asyncio.sleep(5)
                                    await new_page.close()
                                    print(f"[关闭] 新标签页已关闭")
                                except Exception as e:
                                    print(f"[错误] 处理新标签页时出错: {str(e)}")
                                    try:
                                        # 尝试关闭页面，即使出错
                                        await new_page.close()
                                        print(f"[关闭] 新标签页已关闭（出错后）")
                                    except:
                                        pass
                            else:
                                print(f"[警告] 未检测到新标签页打开")

                            # 跳出整个while循环
                            print(f"[操作] 跳出所有循环，不再处理其他新闻")
                            should_exit_while_loop = True  # 设置标志变量
                            break  # 跳出while循环

                    # 检查是否需要跳出while循环
                    if should_exit_while_loop:
                        break

                    # 每秒检查一次按钮状态
                    await page.wait_for_timeout(1000)

                # 检查是否需要跳出for循环
                if should_exit_while_loop:
                    print(f"[操作] 跳出for循环，完全结束处理")
                    break  # 跳出for循环
            except Exception as e:
                print(f"处理新闻时出错: {str(e)}")
                continue


        # endregion 操作处

        print(f"[循环完成] 准备关闭浏览器")

        # 暂停 1000s
        await asyncio.sleep(1000)  # 这里延迟是为了方便眼睛直观的观看

        # 退出前保存 storage 信息
        await context.storage_state(path=self.account_file)  # 保存cookie
        baijiahao_logger.info('cookie更新完毕！')
        await asyncio.sleep(2)  # 这里延迟是为了方便眼睛直观的观看
        # 关闭浏览器上下文和浏览器实例
        await context.close()
        await browser.close()


    async def mainAi(self):
        async with async_playwright() as playwright:
            await self.ai2video(playwright)
