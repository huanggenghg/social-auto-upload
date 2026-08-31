# Platform Login and Publish Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Agent 环境下六个平台的登录或上传误判，同时保持现有 CLI、账号文件和用户反馈契约不变。

**Architecture:** 浏览器平台在各自模块内修正状态判断和媒体选择，不重构公共状态机。B站运行层仅在 Windows 非终端环境创建独立控制台，真实终端路径保持不变；所有行为由现有 `setup` 和统一发布编排继续调用。

**Tech Stack:** Python 3.9+、`unittest`、`unittest.mock`、Patchright、biliup CLI、Windows `subprocess`。

## Global Constraints

- 不新增 CLI 或技能参数。
- Agent 仍只向用户反馈环境状态、发布开始和发布结果，不展示过程日志。
- 不改变账号文件格式、默认路径或错误码体系。
- 不改变普通终端用户已有的交互式 B站登录行为。
- 不重构全部平台的统一登录状态机。
- 生产代码修改前必须先运行对应失败测试并确认预期失败。

---

## File Map

- `uploader/xiaohongshu_uploader/main.py`：小红书登录态保存和 Python 3.9–3.11 语法兼容。
- `uploader/ks_uploader/main.py`：快手登录态保存。
- `uploader/tencent_uploader/main.py`：视频号登录完成判断。
- `uploader/baijiahao_uploader/main.py`：百家号登录完成判断入口。
- `uploader/weibo_uploader/main.py`：微博视频文件选择策略。
- `uploader/bilibili_uploader/runtime.py`：biliup 在真实终端与 Agent 非终端环境中的启动策略。
- `tests/test_xiaohongshu_uploader.py`、`tests/test_ks_uploader_base.py`、`tests/test_tencent_uploader_base.py`、`tests/test_baijiahao_uploader_base.py`、`tests/test_weibo_uploader_base.py`、`tests/test_bilibili_runtime.py`：对应回归测试。

### Task 1: 先恢复小红书 Python 3.9–3.11 语法兼容

**Files:**
- Create: `tests/test_python_compatibility.py`
- Modify: `uploader/xiaohongshu_uploader/main.py:701-707`

**Interfaces:**
- Consumes: 小红书模块源码文本和 `all_text: str`。
- Produces: Python 3.9+ 可解析的模块源码；日志内容仍为单行预览文本。

- [ ] **Step 1: 写不导入故障模块的失败测试**

```python
from pathlib import Path
import unittest


class SupportedPythonSyntaxTests(unittest.TestCase):
    def test_xiaohongshu_module_compiles(self):
        source_path = Path(__file__).parents[1] / "uploader" / "xiaohongshu_uploader" / "main.py"
        source = source_path.read_text(encoding="utf-8")
        compile(source, str(source_path), "exec")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_python_compatibility -v`

Expected: FAIL，包含 `f-string expression part cannot include a backslash`。

- [ ] **Step 3: 修正表达式**

```python
if self.debug:
    preview_text = all_text.strip().replace("\n", " ")
    xiaohongshu_logger.debug(_msg("🧍", f"预览区域内容: {preview_text}"))
```

- [ ] **Step 4: 运行编译测试和小红书现有测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_python_compatibility tests.test_xiaohongshu_uploader tests.test_xiaohongshu_uploader_base -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add tests/test_python_compatibility.py uploader/xiaohongshu_uploader/main.py
git commit -m "fix: restore xiaohongshu python 3.11 compatibility"
```

### Task 2: 小红书和快手取消并发二次登录校验

**Files:**
- Modify: `tests/test_xiaohongshu_uploader.py`
- Modify: `tests/test_ks_uploader_base.py`
- Modify: `uploader/xiaohongshu_uploader/main.py:385-405`
- Modify: `uploader/ks_uploader/main.py:207-223`

**Interfaces:**
- Consumes: `context.storage_state(path: str)`、`_build_login_result(...)`。
- Produces: `xiaohongshu_cookie_gen(...) -> dict` 和 `get_ks_cookie(...) -> dict` 在当前页面已确认登录时保存状态并返回 `success=True`，不调用模块级 `cookie_auth`。

- [ ] **Step 1: 为小红书写失败测试**

在 `tests/test_xiaohongshu_uploader.py` 增加一个最小登录上下文，令 `XiaoHongShuBaseUploader.is_login_completed` 返回 `True`，并断言保存状态后不会调用 `cookie_auth`：

```python
def test_cookie_gen_saves_authenticated_state_without_parallel_revalidation(self):
    context = FakeLoginContext()
    with patch.object(xhs_main, "async_playwright", return_value=FakePlaywright(context)), \
         patch.object(xhs_main.XiaoHongShuBaseUploader, "is_login_completed", AsyncMock(return_value=True)), \
         patch.object(xhs_main, "cookie_auth", AsyncMock(side_effect=AssertionError("must not revalidate before closing login browser"))):
        result = asyncio.run(xhs_main.xiaohongshu_cookie_gen("account.json", poll_interval=0, max_checks=1))
    self.assertTrue(result["success"])
    self.assertEqual(context.saved_path, "account.json")
```

- [ ] **Step 2: 运行小红书测试并确认预期失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_xiaohongshu_uploader.XiaohongshuUploaderTests.test_cookie_gen_saves_authenticated_state_without_parallel_revalidation -v`

Expected: FAIL，因为当前实现调用了被设为抛错的 `cookie_auth`。

- [ ] **Step 3: 最小修改小红书登录成功分支**

把 `storage_state` 后的即时 `cookie_auth` 分支替换为直接构造成功结果：

```python
await context.storage_state(path=account_file)
result = _build_login_result(
    True, "success", "小红书扫码登录成功", account_file, qrcode_info, page.url
)
return result
```

- [ ] **Step 4: 为快手写失败测试**

在 `tests/test_ks_uploader_base.py` 增加对应假页面/上下文，断言登录完成后保存状态且不调用 `cookie_auth`：

```python
def test_cookie_gen_saves_authenticated_state_without_parallel_revalidation(self):
    with patch("uploader.ks_uploader.main.async_playwright", return_value=FakePlaywright()), \
         patch("uploader.ks_uploader.main._save_ks_qrcode", AsyncMock(return_value={"image_path": "qrcode.png"})), \
         patch("uploader.ks_uploader.main._is_ks_login_page_gone", AsyncMock(return_value=True)), \
         patch("uploader.ks_uploader.main.cookie_auth", AsyncMock(side_effect=AssertionError("must not revalidate before closing login browser"))):
        result = asyncio.run(get_ks_cookie("account.json", poll_interval=0, max_checks=1))
    self.assertTrue(result["success"])
```

- [ ] **Step 5: 运行快手测试并确认预期失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_ks_uploader_base.KSLoginPersistenceTests.test_cookie_gen_saves_authenticated_state_without_parallel_revalidation -v`

Expected: FAIL，因为当前实现仍调用 `cookie_auth`。

- [ ] **Step 6: 最小修改快手登录成功分支**

保存状态后直接返回成功结果：

```python
await context.storage_state(path=account_file)
result = _build_login_result(
    True, "success", "快手扫码登录成功", account_file, qrcode_info, page.url
)
return result
```

- [ ] **Step 7: 运行两组测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_xiaohongshu_uploader tests.test_ks_uploader_base -v`

Expected: PASS。

- [ ] **Step 8: 提交**

```bash
git add tests/test_xiaohongshu_uploader.py tests/test_ks_uploader_base.py uploader/xiaohongshu_uploader/main.py uploader/ks_uploader/main.py
git commit -m "fix: persist browser login before validation"
```

### Task 3: 修正视频号和百家号登录完成判断

**Files:**
- Modify: `tests/test_tencent_uploader_base.py`
- Modify: `tests/test_baijiahao_uploader_base.py`
- Modify: `uploader/tencent_uploader/main.py:132-161`
- Modify: `uploader/baijiahao_uploader/main.py:139-151`

**Interfaces:**
- Consumes: `_is_tencent_login_completed(page: Page) -> bool`、`_is_baijiahao_auth_page_valid(page: Page) -> bool`。
- Produces: `BaiJiaHaoVideo.is_login_completed(page: Page) -> bool`。

- [ ] **Step 1: 写视频号 URL 后备判断失败测试**

```python
def test_login_completed_on_publish_url_without_login_markers(self):
    page = TencentLoginPage(
        url="https://channels.weixin.qq.com/platform/post/create",
        visible_selectors=set(),
    )
    self.assertTrue(asyncio.run(tencent_main._is_tencent_login_completed(page)))

def test_login_not_completed_when_qr_iframe_is_visible(self):
    page = TencentLoginPage(
        url="https://channels.weixin.qq.com/platform/post/create",
        visible_selectors={'iframe[src*="qrconnect"]'},
    )
    self.assertFalse(asyncio.run(tencent_main._is_tencent_login_completed(page)))
```

- [ ] **Step 2: 运行视频号测试并确认第一项失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_tencent_uploader_base.TencentLoginCompletionTests -v`

Expected: 第一项 FAIL，当前函数在认证后 URL 上仍固定返回 `False`。

- [ ] **Step 3: 实现视频号后备判断**

把二维码 iframe 加入登录标记，并在认证后 URL 且所有登录标记不可见时返回成功：

```python
login_markers = [
    page.locator('iframe[src*="qrconnect"]').first,
    page.locator("div.login-qrcode-wrap").first,
    page.locator("div.qrcode-wrap").first,
    page.locator("img.qrcode").first,
    page.locator('span:has-text("微信扫码登录 视频号助手")').first,
]
for marker in login_markers:
    if await marker.count() and await marker.is_visible():
        return False
return page.url.startswith(TENCENT_UPLOAD_URL) or page.url.startswith(TENCENT_MANAGE_URL)
```

- [ ] **Step 4: 写百家号 DOM 委托失败测试**

```python
def test_login_completed_uses_baijiahao_dom_validation(self):
    page = object()
    with patch(
        "uploader.baijiahao_uploader.main._is_baijiahao_auth_page_valid",
        AsyncMock(return_value=True),
    ) as validate:
        result = asyncio.run(BaiJiaHaoVideo.is_login_completed(page))
    self.assertTrue(result)
    validate.assert_awaited_once_with(page)
```

- [ ] **Step 5: 运行百家号测试并确认预期失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_baijiahao_uploader_base.BaiJiaHaoLoginCompletionTests -v`

Expected: FAIL，因为当前类继承通用 URL 判断。

- [ ] **Step 6: 增加百家号类方法覆盖**

```python
@classmethod
async def is_login_completed(cls, page: Page) -> bool:
    return await _is_baijiahao_auth_page_valid(page)
```

- [ ] **Step 7: 运行两组测试并提交**

Run: `.venv/Scripts/python.exe -m unittest tests.test_tencent_uploader_base tests.test_baijiahao_uploader_base -v`

Expected: PASS。

```bash
git add tests/test_tencent_uploader_base.py tests/test_baijiahao_uploader_base.py uploader/tencent_uploader/main.py uploader/baijiahao_uploader/main.py
git commit -m "fix: detect completed creator logins"
```

### Task 4: 微博优先直接设置文件输入框

**Files:**
- Modify: `tests/test_weibo_uploader_base.py`
- Modify: `uploader/weibo_uploader/main.py:325-352`

**Interfaces:**
- Consumes: Patchright `Locator.set_input_files(path)` 和现有 `_wait_for_weibo_upload_button`。
- Produces: `_select_weibo_video_file(page: Page, file_path: str) -> None`。

- [ ] **Step 1: 写已有文件输入框的失败测试**

```python
def test_video_upload_prefers_existing_file_input(self):
    file_input = FakeFileInput(count=1)
    page = WeiboUploadPage(file_input=file_input)
    uploader = WeiboVideo(
        title="t", file_path="/fake.mp4", tags=[], publish_date=0,
        account_file="/fake.json",
    )
    with patch.object(uploader, "prepare_video_for_publish", AsyncMock()), \
         patch("uploader.weibo_uploader.main._wait_for_weibo_upload_button", AsyncMock(side_effect=AssertionError("button path must not run"))):
        asyncio.run(uploader.upload_video_content(page))
    file_input.set_input_files.assert_awaited_once_with("/fake.mp4")
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_weibo_uploader_base.WeiboVideoFileSelectionTests.test_video_upload_prefers_existing_file_input -v`

Expected: FAIL，因为当前实现总是先等待按钮和 `filechooser`。

- [ ] **Step 3: 提取最小文件选择函数并接入**

```python
async def _select_weibo_video_file(page: Page, file_path: str) -> None:
    file_input = page.locator('input[type="file"]').first
    if await file_input.count():
        await file_input.set_input_files(file_path)
        return

    upload_button = await _wait_for_weibo_upload_button(page)
    async with page.expect_file_chooser(timeout=30000) as chooser_info:
        await upload_button.click()
    chooser = await chooser_info.value
    await chooser.set_files(file_path)
```

在 `upload_video_content` 中保留登录重定向异常映射，调用该函数替换原按钮专用逻辑。

- [ ] **Step 4: 增加无输入框时按钮后备测试**

```python
def test_video_upload_uses_file_chooser_when_input_is_absent(self):
    page = WeiboUploadPage(file_input=FakeFileInput(count=0))
    asyncio.run(_select_weibo_video_file(page, "/fake.mp4"))
    page.file_chooser.set_files.assert_awaited_once_with("/fake.mp4")
```

- [ ] **Step 5: 运行微博测试并提交**

Run: `.venv/Scripts/python.exe -m unittest tests.test_weibo_uploader_base tests.test_weibo_uploader -v`

Expected: PASS。

```bash
git add tests/test_weibo_uploader_base.py uploader/weibo_uploader/main.py
git commit -m "fix: upload weibo video through file input"
```

### Task 5: B站在 Agent 非终端环境打开独立登录控制台

**Files:**
- Modify: `tests/test_bilibili_runtime.py`
- Modify: `uploader/bilibili_uploader/runtime.py:178-189`

**Interfaces:**
- Consumes: `run_biliup_command(arguments: list[str], interactive: bool = False)`。
- Produces: `_needs_detached_login_console(interactive: bool) -> bool`，仅在 Windows、`interactive=True` 且 stdin/stdout 任一不是 TTY 时为真。

- [ ] **Step 1: 写非终端 Windows 失败测试**

```python
@patch("uploader.bilibili_uploader.runtime.subprocess.run")
@patch("uploader.bilibili_uploader.runtime.ensure_biliup_binary")
def test_interactive_login_opens_new_console_when_stdio_is_redirected(self, ensure, run):
    ensure.return_value = Path("C:/mock/biliup.exe")
    run.return_value = Mock(returncode=0)
    with patch("uploader.bilibili_uploader.runtime.platform.system", return_value="Windows"), \
         patch("uploader.bilibili_uploader.runtime.sys.stdin.isatty", return_value=False), \
         patch("uploader.bilibili_uploader.runtime.sys.stdout.isatty", return_value=False):
        run_biliup_command(["-u", "account.json", "login"], interactive=True)
    _, kwargs = run.call_args
    self.assertEqual(kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)
    self.assertNotIn("capture_output", kwargs)
```

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_bilibili_runtime.BiliupRuntimeTests.test_interactive_login_opens_new_console_when_stdio_is_redirected -v`

Expected: FAIL，因为当前交互路径没有 `creationflags`。

- [ ] **Step 3: 实现环境判断和独立控制台启动**

```python
import sys

def _needs_detached_login_console(interactive: bool) -> bool:
    return (
        interactive
        and platform.system().lower() == "windows"
        and (not sys.stdin.isatty() or not sys.stdout.isatty())
    )

def run_biliup_command(arguments: list[str], interactive: bool = False):
    binary_path = ensure_biliup_binary(force_check=False)
    command = [str(binary_path), *arguments]
    if _needs_detached_login_console(interactive):
        return subprocess.run(
            command,
            check=False,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    if interactive:
        return subprocess.run(command, check=False)
    return subprocess.run(
        command, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
```

- [ ] **Step 4: 增加真实终端保持原行为测试**

```python
def test_interactive_login_keeps_inherited_stdio_in_real_terminal(self):
    with patch("uploader.bilibili_uploader.runtime.sys.stdin.isatty", return_value=True), \
         patch("uploader.bilibili_uploader.runtime.sys.stdout.isatty", return_value=True):
        run_biliup_command(["login"], interactive=True)
    _, kwargs = mock_run.call_args
    self.assertNotIn("creationflags", kwargs)
    self.assertNotIn("capture_output", kwargs)
```

- [ ] **Step 5: 运行 B站测试并提交**

Run: `.venv/Scripts/python.exe -m unittest tests.test_bilibili_runtime tests.test_bilibili_uploader_base tests.test_bilibili_uploader -v`

Expected: PASS。

```bash
git add tests/test_bilibili_runtime.py uploader/bilibili_uploader/runtime.py
git commit -m "fix: support biliup login from agent sessions"
```

### Task 6: 契约与整体回归验证

**Files:**
- Verify: `skills/opub-cli/SKILL.md`
- Verify: `tests/test_publish_cli.py`
- Verify: all modified production and test files

**Interfaces:**
- Consumes: 所有前序任务的公共函数和现有 CLI。
- Produces: 无新增接口；验证 `opub` 用户反馈和打包契约未改变。

- [ ] **Step 1: 运行六个平台专项测试**

Run:

```powershell
.venv/Scripts/python.exe -m unittest `
  tests.test_xiaohongshu_uploader `
  tests.test_xiaohongshu_uploader_base `
  tests.test_ks_uploader_base `
  tests.test_tencent_uploader_base `
  tests.test_baijiahao_uploader_base `
  tests.test_weibo_uploader_base `
  tests.test_weibo_uploader `
  tests.test_bilibili_runtime `
  tests.test_bilibili_uploader_base `
  tests.test_bilibili_uploader -v
```

Expected: 退出码 0，零失败。

- [ ] **Step 2: 验证 Agent 用户反馈契约**

Run: `.venv/Scripts/python.exe -m unittest tests.test_publish_cli -v`

Expected: 26 个测试全部通过，技能中仍无发布过程日志展示要求。

- [ ] **Step 3: 运行完整测试**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -v`

Expected: 退出码 0；如发现既有且与改动无关的问题，记录精确测试名和错误，不宣称完整通过。

- [ ] **Step 4: 构建和检查发行包**

Run: `uv build`

Run: `.venv/Scripts/python.exe -m twine check dist/*`

Expected: wheel 和 sdist 构建成功，`twine check` 均为 PASSED。

- [ ] **Step 5: 检查变更并提交最终验证调整**

Run: `git diff --check`

Run: `git status --short`

Expected: 无空白错误，只有计划内文件；如果验证未产生代码调整则不创建空提交。
