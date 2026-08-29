# Agent Publish Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Agent-only `opub-cli` skill hide publishing process logs and expose only environment status, publish start, and final publish results.

**Architecture:** Keep the `opub` CLI unchanged. Strengthen the Agent skill contract so the Agent redirects command stdout/stderr to an internal temporary log, parses it privately, and emits only three stable categories of user-facing feedback; protect the contract with the existing skill black-box tests.

**Tech Stack:** Markdown skill instructions, Python `unittest`, existing `tests/test_publish_cli.py` black-box contract tests.

## Global Constraints

- 只修改 `skills/opub-cli/SKILL.md` 及其契约测试。
- 不修改 `opub` CLI，不增加命令行参数，也不改变直接执行 `opub` 时的输出。
- 发布前收集并确认素材、标题、描述、标签和平台的现有要求保持不变。
- 发布过程中不展示上传进度、依赖安装、登录过程、浏览器操作、调试输出、异常堆栈或原始 stdout/stderr。
- 发布过程中浏览器自行展示登录或扫码交互，Agent 不再追加提醒。
- 最终反馈保留稳定错误码、各平台结果、结果链接和总体计数。

---

### Task 1: Enforce the concise Agent feedback contract

**Files:**
- Modify: `tests/test_publish_cli.py`
- Modify: `skills/opub-cli/SKILL.md`

**Interfaces:**
- Consumes: The existing `SkillDocBlackboxTests.SKILL_PATH` contract test fixture and the existing `opub ...` invocation documented by the skill.
- Produces: A documented Agent behavior contract with no new Python or CLI interfaces.

- [ ] **Step 1: Write the failing black-box contract test**

Add this method to `SkillDocBlackboxTests` in `tests/test_publish_cli.py`:

```python
def test_agent_hides_publish_process_logs_and_reports_only_milestones(self):
    text = self.SKILL_PATH.read_text(encoding="utf-8")

    self.assertIn("用户可见反馈仅限以下三类", text)
    for milestone in ["发布环境状态", "发布开始", "发布结果"]:
        self.assertIn(milestone, text)
    self.assertIn("stdout 和 stderr 重定向到 Agent 内部临时日志", text)
    self.assertIn("禁止向用户展示或转述", text)
    self.assertIn("不发送发布进度", text)
    for retained_result in ["错误码", "结果链接", "总体计数"]:
        self.assertIn(retained_result, text)
    self.assertNotIn("--agent-mode", text)
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
python -m unittest tests.test_publish_cli.SkillDocBlackboxTests.test_agent_hides_publish_process_logs_and_reports_only_milestones -v
```

Expected: `FAIL` because the current skill does not contain the concise-feedback contract or stdout/stderr redirection requirement.

- [ ] **Step 3: Add the minimal skill instructions**

Insert the following section before `## 读取结果` in `skills/opub-cli/SKILL.md`:

```markdown
## Agent 用户反馈

本技能只供 Agent 使用。Agent 执行发布时，用户可见反馈仅限以下三类：

1. **发布环境状态**：正在检查、已就绪，或环境异常的简短结论。
2. **发布开始**：发布已启动，可包含目标平台和素材数量。
3. **发布结果**：各平台成功或失败、错误码、结果链接和总体计数。

执行 `opub ...` 时，必须将 stdout 和 stderr 重定向到 Agent 内部临时日志，禁止向用户展示或转述原始命令输出。Agent 只可在内部读取日志以判断退出码并提取最终结果，不得把内部日志转化为过程消息。

发布运行期间不发送发布进度，不转述依赖安装、登录过程、浏览器操作、调试信息或异常堆栈。浏览器登录交互由浏览器界面自行提示，Agent 不追加扫码或操作提醒。失败时只反馈稳定错误码、简短原因和最终建议。
```

In `## Agent 注意事项`, replace the existing QR-code and Bilibili interaction bullets with this single non-conflicting rule:

```markdown
- 发布运行期间不要额外展示二维码、转述登录过程或提示扫码；登录交互由弹出的浏览器界面负责。
```

Do not add or document an Agent-only CLI flag.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```bash
python -m unittest tests.test_publish_cli.SkillDocBlackboxTests.test_agent_hides_publish_process_logs_and_reports_only_milestones -v
```

Expected: `OK` with one passing test.

- [ ] **Step 5: Run all skill document contract tests**

Run:

```bash
python -m unittest tests.test_publish_cli.SkillDocBlackboxTests -v
```

Expected: all `SkillDocBlackboxTests` pass.

- [ ] **Step 6: Run the complete publish CLI test module**

Run:

```bash
python -m unittest tests.test_publish_cli -v
```

Expected: all tests pass with no failures or errors.

- [ ] **Step 7: Verify the diff and commit**

Run:

```bash
git diff --check
git diff -- tests/test_publish_cli.py skills/opub-cli/SKILL.md
```

Expected: no whitespace errors; the diff contains only the new contract test and concise Agent feedback instructions.

Commit:

```bash
git add tests/test_publish_cli.py skills/opub-cli/SKILL.md
git commit -m "fix: hide agent publish process logs"
```
