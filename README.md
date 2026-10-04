# phone-agent

> **本项目基于 [GELab-Zero](https://github.com/stepfun-ai/gelab-zero) 开发。**
>
> 复用了它的**视觉模型**、**`yadb` 工具**与**动作接口约定**；源代码为独立实现。
> 详细的依赖清单与审计依据见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

用**视觉模型 + ADB** 自主操控 Android 手机真机。
给它一句自然语言，它自己看屏幕、点按钮、打字。

```bash
python run.py --task "打开微信给张三发消息说我晚点到"
```

> **本skill适用于本地模型与云端模型。**
> 只要提供 OpenAI 兼容接口（`--api-base` / `--api-key` / `--model`），
> 本地 `llama.cpp` 与云端 API 都能驱动本项目。
>
> **本文档以本地模型（llama.cpp + GELab-Zero-4B）为参考配置。**
> 换用云端模型时，除接口参数外，**提示词可能也需要相应调整** —— 见「已知限制」。

## 工作原理

每一步都是同一个四步闭环：

```
截图 → 视觉模型看屏幕并决策 → 转成 adb 命令执行 → 循环
```

直到模型输出 `COMPLETE`，或达到 `--max-steps`。

**设计取舍**：

- 只依赖 `adb`，不引入 uiautomator2 / Appium 等重框架
- 中文输入走 `yadb`（推送到手机的 Java 小程序），不切换系统输入法
- 每步只把「当前帧」喂给模型，历史只保留动作摘要，控制上下文膨胀

## 特性

| 特性 | 说明 |
|---|---|
| **纯打开类任务短路** | `打开设置` 约 7 秒完成，**零模型调用**、零点击 |
| **中文输入** | `adb shell input text` 打不了中文，用 yadb 注入 |
| **绕开自绘桌面** | 用 `monkey` 启动 App，不依赖识别桌面图标 |
| **退出码语义化** | 区分「失败」和「需要人工介入」（见下表） |
| **日志留档** | `log/run_<时间戳>.log` 含每步决策，自动保留最近 20 份 |
| **截图留档** | `tmp/complete_stepN.png` —— 判断「假成功」的重要依据 |
| **上下文可控** | `--quiet` 让过程日志不进 stdout（25 步约省 95% 输出） |

## 前置条件

**三条缺一不可**：

1. **Python 3.10+**
2. **adb 在 PATH**，手机 USB 连接并已授权
   ```bash
   adb devices        # 状态必须是 device
   ```
3. **一个支持视觉的模型服务**（OpenAI 兼容接口）—— **本地或云端均可**：

   | 部署方式 | `--api-base` | `--api-key` |
   |---|---|---|
   | **本地**（llama.cpp / LM Studio / Ollama） | `http://127.0.0.1:8080/v1` | 任意占位值（如 `local`） |
   | **云端**（OpenAI / DeepSeek / 通义千问 等） | 服务商给的地址 | 你的真实 API Key |

   > ⚠️ 用云端模型时，**提示词可能需要调整**才能让模型按本项目要求的动作格式输出
   > —— 见「已知限制」。

   **本文档以下以本地部署为参考。**

## 安装

```bash
python -m venv .venv

# Windows
.venv/Scripts/pip install -r requirements.txt
# Linux / macOS
# source .venv/bin/activate && pip install -r requirements.txt
```

或者装成命令（可选）：

```bash
pip install -e .
phone-agent --task "打开设置"
```

## 用法

### 本地模型（本文档的参考配置）

```bash
# ① 直接跑（推荐）
python run.py --task "打开设置"

# ② 模块方式（等价）
cd src && python -m phone_agent --task "打开设置"

# ③ 装完之后
phone-agent --task "打开设置"
```

### 云端模型

只需换接口参数 —— 其余用法完全相同：

```bash
# 以 OpenAI 为例
python run.py --task "打开设置" \
  --api-base https://api.openai.com/v1 \
  --api-key sk-xxxxxxxxxxxxxxxx \
  --model gpt-4o

# 以 DeepSeek 为例（注意：需选支持视觉的模型）
python run.py --task "打开设置" \
  --api-base https://api.deepseek.com/v1 \
  --api-key sk-xxxxxxxxxxxxxxxx \
  --model deepseek-vl
```

> ⚠️ **云端模型不保证开箱可用。** 本项目的提示词与动作格式是为
> GELab-Zero-4B 的训练数据写的，换成通用对话模型后可能不按格式输出。
> 见「已知限制」里的说明。

**给 AI agent 调用时**，见 [SKILL.md](SKILL.md)

## 命令行参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--task` | **必填** | 自然语言任务描述 |
| `--device` | 自动 | 设备序列号（多设备时必填） |
| `--api-base` | `http://127.0.0.1:8080/v1` | OpenAI 兼容接口地址 |
| `--api-key` | 占位值 | 接口密钥 |
| `--model` | `GELab-Zero` | 模型名，须与 llama-server 的 `-a` 别名一致 |
| `--view-width` | `720` | 喂给模型的图片宽度 |
| `--max-steps` | `25` | 最多执行多少步 |
| `--step-delay` | `0.4` | 每步之后等待秒数（页面动画慢时调大） |
| `--dry-run` | — | 只让模型决策，不真的操作手机（调试提示词用） |
| `--no-reset` | — | 跳过执行前的回桌面复位 |
| `--no-auto-launch` | — | 不复位后自动启动任务里提到的 App |
| `--reset-app` | — | 复位时额外强制停止该 App（如 `com.tencent.mobileqq`） |
| `--quiet` | — | 过程日志不打屏，只留结论/错误（日志文件照常写） |
| `--log-file` | `log/run_<时间戳>.log` | 日志路径，相对路径按项目根解析 |
| `--no-log` | — | 本次不写日志文件 |

## 退出码

| 码 | 含义 | 该怎么理解 |
|---|---|---|
| **0** | 完成 | 正常成功 |
| **1** | 出错 | 连不上模型、adb 异常等 |
| **2** | 卡死 / 输入失败，被提前终止 | 说明卡在哪一步 |
| **3** | **已完成，但有可疑点** | **不是失败** —— 见下方警告 |
| **4** | **需要人工输入** | **不是失败** —— 撞上密码框，已停下等你操作 |

> ⚠️ **退出码 3 和 4 都不是失败。**
> 脚本最后一行会明确打印 `[结论] 任务【已完成】…` 或 `[结论] 任务【未完成】…`，**以那一行为准**。
> 退出码 3 的典型场景：任务只用了 1~2 步就宣告完成（可能是 App 恢复了上次页面），
> 需要人工核对 `tmp/` 里的完成截图。

## 项目结构

```
my_project/
├── run.py                      直接运行入口
├── SKILL.md                    给 AI agent 的调用规程
├── pyproject.toml / requirements.txt
├── bin/
│   └── yadb                    中文输入用的二进制（必需，随仓库分发）
├── src/phone_agent/
│   ├── config.py       255 行   所有常量 + _PROJECT_ROOT
│   ├── deps.py          15 行   第三方依赖统一导入
│   ├── output.py       128 行   输出控制 + 日志/截图清理
│   ├── adb.py          253 行   ADB 基础（命令/设备/截图/点击/滑动/按键）
│   ├── apps.py         344 行   App 操作（包名解析/启动/复位/中文输入）
│   ├── ui.py           353 行   界面树解析（dump/节点/输入框/可见文字）
│   ├── vision.py       484 行   模型交互（提示词/解析/请求重试）
│   ├── tasks.py        155 行   任务判定（纯打开/简单任务/发文字）
│   ├── actions.py      444 行   动作执行 + 卡死检测
│   ├── runner.py       289 行   主循环
│   └── phone_agent.py  134 行   命令行入口
└── log/  tmp/                  ← 运行时自动创建，已在 .gitignore（不随仓库分发）
```

## 实测性能

环境：RTX 5060 Laptop 8GB + GELab-Zero-4B-Q6_K（KV cache `q4_0`，16K 上下文）

| 场景 | 耗时 |
|---|---|
| 纯打开类任务（短路，零模型调用） | **约 7 秒** |
| 单步 | 约 5 秒（截图 1s + 模型推理 2.5s + 动作 0.5s） |
| 「打开收藏」5 步 | 36 秒 |
| 「打开收藏 + 找某本书」21 步 | 105 秒 |

**模型推理是大头**（2.5 秒/步）。llama-server 的 timing 日志显示一次请求处理
约 3000 token 用 2.5 秒，其中生成一行动作约占 1.6 秒 —— 瓶颈在生成，不在 prompt 长度。

## 已知限制

| 限制 | 说明 |
|---|---|
| **密码框需人工** | 检测到密码框会停下并返回退出码 4 —— 密码不该由脚本代输，且 Android 对密码框的 `text` 恒返回空串，回读验证必然失败 |
| **自绘桌面读不到** | 部分 ROM 的桌面是自绘 View，`uiautomator dump` 读不到图标（已用 `monkey` 绕开） |
| **小模型能力有限** | 对界面语义理解弱，复杂任务可能需多次尝试 |
| **换模型需重新验证提示词** | `vision.PROMPT_TEMPLATE` 里的坐标格式（0-1000 归一化）与动作名（`CLICK`/`SLIDE`/`TYPE`…）**是绑在 GELab-Zero-4B 训练数据上的**。换成本地其他模型或云端模型后，模型可能不按此格式输出 —— 需先改提示词并实测 |
| **纯打开任务也需模型服务** | `main()` 里的服务探测早于短路判断 |
| **安全输入场景会失效** | 密码/验证码框会强制切系统安全键盘，yadb 也注入不进去 |

## 致谢与第三方许可

- **[GELab-Zero](https://github.com/stepfun-ai/gelab-zero)**（stepfun-ai，**MIT**）——
  本项目的视觉模型、坐标约定与动作格式都基于它。
- **[YADB](https://github.com/ysbing/YADB)**（ysbing，**LGPL-3.0**）——
  `bin/yadb` 的原始项目。本仓库随附该二进制（经 GELab-Zero 仓库获得），
  以**独立进程方式调用**，未做任何修改。

> ⚠️ **`bin/yadb` 是 LGPL-3.0 组件**，与本项目自身的 MIT 许可证不同。
> 本项目以独立进程方式调用它（不链接、不修改），符合 LGPL-3.0 的要求。
> 完整声明与许可证文本见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 使用须知

> ⚠️ **这是自动化操作真机的工具，请留意三件事**：
>
> 1. **App 用户协议**：自动操作微信/QQ/抖音等第三方 App 可能违反其用户协议，
>    账号存在封禁风险。不要用于批量营销、刷量或骚扰。
> 2. **个人信息**：`log/` 和 `tmp/` 会明文留存截图与决策记录，可能含聊天记录、
>    手机号等。处理**他人设备**前请先取得授权，事后及时清理。
> 3. **密码不代输**：撞上密码框会主动停下（退出码 4），这是有意设计。

## 开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

本项目**源代码**采用 **MIT 许可证** —— 见 [LICENSE](LICENSE)。

**随仓库分发的第三方组件**：

| 组件 | 许可证 |
|---|---|
| `bin/yadb` | **LGPL-3.0** |
| `GELab-Zero-4B` 模型权重（不随仓库分发） | Apache-2.0 |

详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
