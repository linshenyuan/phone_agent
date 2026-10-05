# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

- **模型输出解析（`vision.parse_action`）三处静默错误**（2026-10-05 代码审计）：
  - 正文里出现「空格 + 已知键」（如 `value:set value: 5 now`）会被**砍半截**，
    且截断后仍能通过回读校验 → 半截消息发出还判成功。现在已知键只在行首 /
    制表符 / 换行后（或非自由文本字段里的空格后）才算分隔符。
  - 模型给出「两个候选动作」时旧实现 last-wins、**静默执行后一个**；现在保留
    第一次出现的值，并把被忽略的那个写进日志。
  - `max_tokens` 截断（`finish_reason == "length"`）完全未检测 —— 半截坐标仍是
    合法坐标，会点到屏幕别处且不报错。现在检出即判失败并重新采样。
- **异常路径日志脱敏失效**：yadb / `input text` 失败时，报错信息把输入正文原样
  带进 `log/run_*.log` 长期留存。现在在 `input_text` 处把正文替换为脱敏版本。
- **截图损坏会以未捕获 traceback 崩溃**：`Image.open` 抛的是 `OSError`（非
  `AdbError`），会逃出 `run()` 的 `except AdbError`，且没有 `[结论]` 行。现在统一
  包成 `AdbError`，runner 侧也一并捕获。
- **越界坐标被静默夹回屏幕边**：`to_real` 会把 1040 拉到最右一列，SLIDE 两端都
  越界时退化成原地长按却报「滑动成功」。现在越界**拒绝执行**，与 `vision` 对齐。
- **多输入框歧义被误报成「需要人工输入（密码）」**：`report_need_human` 缺自定义
  抬头/结论，脚本自己给出与事实相反的诊断。现在 `ExecResult` 携带抬头/结论。
- **`current_package` 吞掉 `AdbError`**：使 runner 的 `_adb_fail("读取当前前台应用")`
  成死代码，且掉线时「全程没离开起点」这条假成功判据被静默跳过。现在 adb 失败即
  抛错（信息性调用点自行降级）。
- **「回读为空即放行」在带占位文字的输入框上不生效**：Android 对空 EditText 会把
  hint 当 text 返回，旧判据 `== ""` 在主场景（聊天框）不触发。现在「不含上一条输入」
  也算已发送。
- **密码提示可能漏检**：`visible_texts` 默认只取 15 条，密码提示排在其后会被确定性
  漏检。密码检测改为不限条数。
- **旧机型（Android 7/8）RAW 截图静默退 PNG**：其 `screencap` header 只有 12 字节
  （无色彩空间），旧实现只认 16 字节 → 老机型恒定静默降级。现在两条都认，且降级路径
  打日志。
- 死代码清理：`StuckDetector._count_same_spot`（无生产调用）、`_blocked_type`（只写不读）。

### Changed

- **CI 现在会跑单元测试**（`python -m unittest discover -s tests -v`）—— 此前 89 个用例
  从未在 CI 执行，等于没有回归网。
- CI 的第三方 action 固定到 commit SHA（供应链最佳实践）。
- `README` 项目结构表的行数与代码同步。

### Security

- `adb` 的 `shlex.quote` 注释收敛：它挡得住 host → 设备端 shell 的注入，**挡不住**
  设备端 `input` 自身的 `%s` → 空格替换（当前被上层白名单挡住，属潜伏坑）。

## [0.1.0] - 2026-10-02

首个版本。核心能力在 2026-09-29 ~ 10-02 的迭代中成型，本版将其整理为可发布的项目结构。

### Added

**来源**

- **基于 [GELab-Zero](https://github.com/stepfun-ai/gelab-zero) 开发**（stepfun-ai，MIT）——
  复用其视觉模型（GELab-Zero-4B，Apache-2.0）、`yadb` 工具与动作接口约定。
  源代码为独立实现，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- **随仓库分发 `bin/yadb`**（原始项目 [ysbing/YADB](https://github.com/ysbing/YADB)，
  **LGPL-3.0**）—— 以独立进程方式调用，未修改。已按其要求保留版权与许可证声明

**核心能力**

- **主循环**：截图 → 视觉模型决策 → adb 执行，直到模型输出 `COMPLETE` 或达到步数上限
- **中文输入**：走 yadb 注入（`adb shell input text` 不支持非 ASCII 字符）
- **绕开自绘桌面**：用 `monkey -p <包名>` 启动 App，不依赖识别桌面图标
  （部分 ROM 的桌面是自绘 View，`uiautomator dump` 读不到图标）
- **纯打开类任务短路**：`打开设置` 这类任务在 App 启动后直接判定完成，
  零模型调用、零点击（约 7 秒返回）
- **卡死检测**：识别原地打转 —— 重复点击、重复滑动、连续 TYPE 相同内容

**安全与可靠性**

- **密码框检测**：撞上密码框立即停止并返回退出码 4，密码留给人工输入
- **退出码语义化**：0 完成 / 1 出错 / 2 卡死 / 3 可疑完成 / 4 需人工输入，
  并在 stdout 末尾打印无歧义的 `[结论]` 行
- **输出控制**：`--quiet` 静默过程日志（25 步约省 95% 输出），
  结论/错误仍打屏；日志写文件并保留最近 20 份
- **截图留档**：完成时和需人工介入时各存一张截图，保留最近 20 张
  —— 这是判断「假成功」的唯一依据

**行为约束（提示词管不住的部分用代码兜底）**

- **滑动次数上限** `MAX_SLIDE_TOTAL = 3`：提示词写了「最多 1 次」但 4B 模型不遵守，
  实测连滑 3 次而目标其实一开始就在屏幕上
- **连续 TYPE 拦截** `MAX_CONSECUTIVE_TYPE = 1`：模型点不中发送按钮时会反复追加文字，
  最后退化成 200 字乱码
- **SLIDE 前查屏**：滑动前 dump 一次界面文字并写进历史，让模型知道当前在哪一页，
  减少无意义滑动

### Changed

- 单文件 2334 行拆分为 13 个模块，最大 446 行（`vision.py`）
- `src/phone_agent/phone_agent.py` 只保留命令行入口 `main()`
- 新增 `run.py` 作为直接运行入口（包内模块用相对导入，无法直接 `python xxx.py`）
- 路径常量统一基于 `config._PROJECT_ROOT`，不再依赖 `__file__` 的相对位置

### Fixed

- **Windows cmd 下输出中文乱码**：cmd 默认代码页 936，Python 输出到管道用 GBK 字节，
  而调用方按 UTF-8 解码 —— 现在非终端时强制 UTF-8
- **`global QUIET` 跨模块失效**：拆分为多模块后，`main()` 里的 `global` 语句
  改不到 `output.py` 的状态，导致 `--quiet` 静默失效 —— 已封装为 `output.setup_log()`
- **拆分时 `@dataclass` 装饰器丢失**：基于 `ast` 行号搬运代码时，
  `FunctionDef.lineno` 不含装饰器行，导致 `ExecResult` 退化成普通类
- **拆分后截图路径漂移**：`Path(__file__).parent` 的基准从「脚本目录」变成
  「模块所在目录」，截图被写到包内 —— 已统一到项目根的 `tmp/`
- **`tmp/` 只进不出**：截图永不删除，跑 100 次累积 30 MB —— 已加清理逻辑
