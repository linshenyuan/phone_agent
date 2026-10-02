# 第三方组件声明

> **本项目基于 [GELab-Zero](https://github.com/stepfun-ai/gelab-zero) 开发。**
>
> 复用了它的视觉模型（`GELab-Zero-4B`）、`yadb` 工具与动作接口约定。
> 源代码为独立实现 —— 审计依据见文末「代码原创性说明」。

本项目包含以下第三方组件。按各自许可证的要求，在此保留其版权声明。

---

## ⚠️ 许可证总览（重要）

| 组件 | 许可证 | 是否随本仓库分发 |
|---|---|---|
| 本项目源代码 | **MIT** | 是 |
| **GELab-Zero**（上游参考） | **MIT** | 否（仅参考视觉模型与动作接口约定） |
| **`bin/yadb`** | **LGPL-3.0** | **是** ← 需特别留意 |
| `GELab-Zero-4B` 模型权重 | **Apache-2.0** | **否**（使用者自行下载） |
| `Qwen3-VL-4B-Instruct`（模型基座） | **Apache-2.0** | 否 |
| `pillow`（依赖） | MIT-CMU | 否（pip 安装） |
| `openai`（依赖） | Apache-2.0 | 否（pip 安装） |

---

## yadb

- **文件**：`bin/yadb`
- **大小**：69906 字节
- **用途**：向 Android 设备注入中文文本（`adb shell input text` 不支持非 ASCII）
- **原始项目**：https://github.com/ysbing/YADB
- **本项目中的副本来源**：https://github.com/stepfun-ai/gelab-zero 仓库根目录的 `yadb`
  - 来源 commit：`7b619f6f67d2`（2026-05-11，"update execution tools"）
  - ⚠️ **这是 gelab-zero 转发的版本，不是 ysbing/YADB 的官方 release**
    —— 本文件为 APK 构建（YADB 1.0 时期，AGP 7.4.2），69,906 字节；
    而 ysbing/YADB 官方 release 的 `yadb` 是脚本形态（v1.1.3 仅 14,523 字节）。
    两者形态与用途不同，不要混用。
- **版权**：Copyright (c) ysbing
- **许可证**：**GNU Lesser General Public License v3.0（LGPL-3.0）**

### 本项目的使用方式与合规说明

本项目**以独立进程方式调用** `yadb`，而非链接到它：

```
adb shell app_process -Djava.class.path=/data/local/tmp/yadb \
    /data/local/tmp com.ysbing.yadb.Main -keyboard "文本"
```

即：由 Android 系统启动一个独立的 Java 进程执行 yadb，
本项目的 Python 代码**不导入、不链接、不修改** yadb 的任何代码。

**LGPL-3.0 允许这样使用**：LGPL 的核心目的是让非 LGPL 的软件能够使用 LGPL 库，
只要（a）不修改该库，（b）保留其许可证与版权声明，（c）提供获取其源码的途径。

本项目满足以上三点：
1. **未修改** `yadb`（`bin/yadb` 与上游二进制一致，大小 69906 字节）
2. **保留**其版权与许可证声明（即本节）
3. **提供**源码获取途径：https://github.com/ysbing/YADB

### LGPL-3.0 全文

本仓库随附完整文本：**[`LICENSES/LGPL-3.0.txt`](LICENSES/LGPL-3.0.txt)**
（另附 [`LICENSES/GPL-3.0.txt`](LICENSES/GPL-3.0.txt) —— LGPL-3.0 以 GPL-3.0 为基础）

官方地址：https://www.gnu.org/licenses/lgpl-3.0.txt

```
GNU LESSER GENERAL PUBLIC LICENSE
Version 3, 29 June 2007

Copyright (C) 2007 Free Software Foundation, Inc. <https://fsf.org/>
Everyone is permitted to copy and distribute verbatim copies
of this license document, but changing it is not allowed.

This version of the GNU Lesser General Public License incorporates
the terms and conditions of version 3 of the GNU General Public
License, supplemented by the additional permissions listed below.
...
（完整文本见上方链接；LGPL-3.0 由 GPL-3.0 附加条款构成）
```

> **注意**：若你**修改**了 `bin/yadb`，修改后的版本必须以 LGPL-3.0 发布，
> 并提供修改后的源码。本项目未做任何修改。

---

## GELab-Zero-4B 模型权重（不随本仓库分发）

- **来源**：https://huggingface.co/stepfun-ai/GELab-Zero-4B-preview
- **许可证**：**Apache-2.0**（依据 gelab-zero 仓库的 `Notice.txt`）
- **基座模型**：`Qwen3-VL-4B-Instruct`（同样 Apache-2.0）

**本项目不包含、不分发模型权重**。使用者需自行获取，并遵守 Apache-2.0 的要求
（保留版权与许可声明、标注修改等）。

---

## 关于 gelab-zero

以下内容**不是**对 gelab-zero 源代码的复制，而是为适配其模型接口所做的实现：

| 位置 | 内容 | 说明 |
|---|---|---|
| `actions.py` | 坐标换算 `real = (norm / 1000) * 屏幕尺寸` | 归一化坐标是模型的输入约定，换算公式是数学定义 |
| `vision.py` | 动作输出格式 `action:CLICK point:<x>,<y>` | 模型按此格式训练，必须匹配才能被解析 |
| `vision.py` | `temperature = 1.0` | 遵循 GELab-Zero 官方指南的建议值 |
| `config.py` | `DEFAULT_MODEL = "GELab-Zero"` | 默认连接的模型名 |
