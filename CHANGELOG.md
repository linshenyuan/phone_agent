# Changelog

本项目的所有重要变更都记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
