"""手机 GUI Agent —— 命令行入口。

用法：
    python run.py --task "打开微信给张三发消息说我晚点到"
    python run.py --task "打开设置" --dry-run
"""

from __future__ import annotations

import argparse
import sys

from .adb import pick_device
from .config import DEFAULT_API_BASE, DEFAULT_API_KEY, DEFAULT_MAX_STEPS, DEFAULT_MODEL, DEFAULT_STEP_DELAY, DEFAULT_VIEW_WIDTH, EXIT_NEED_HUMAN, LOG_DIR
from . import output
from .output import info
from .runner import run
from .tasks import _strip_wrapping_quotes
from .vision import (build_http_client, is_loopback_endpoint,
                     model_name_matches, served_model_ids)
from .deps import OpenAI




def main() -> int:
    """解析参数并启动。"""
    # ---- 输出编码兜底（2026-10-01 加）----
    # ★ 为什么需要：Windows 的 cmd 默认代码页是 936(GBK)，Python 在这种环境下
    #   输出到管道时用的是 GBK 字节；而调用方（Pi / Node）按 UTF-8 解码
    #   → 中文全变乱码。实测 Pi 拿到的日志是「[��־] ������־��<盘符>:...」。
    # ★ 只在「输出被捕获」（非终端）时强制 UTF-8：
    #   输出到终端时保持系统编码，否则会在 cmd 窗口里反而显示乱码。
    if not sys.stdout.isatty():
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8")
            except (AttributeError, OSError):
                pass

    parser = argparse.ArgumentParser(
        description="用视觉模型 + ADB 自主操控 Android 手机",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               '  python run.py --task "打开微信给张三发消息说我晚点到"\n'
               '  python run.py --task "打开设置" --dry-run\n',
    )
    parser.add_argument("--task", required=True, help="自然语言任务描述")
    parser.add_argument("--device", default=None, help="设备序列号（多设备时必填）")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="OpenAI 兼容接口地址")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="接口密钥")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="模型名，必须与 llama-server 的 -a 别名一致")
    parser.add_argument("--view-width", type=int, default=DEFAULT_VIEW_WIDTH,
                        help="喂给模型的图片宽度，默认 720")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help="最多执行多少步，默认 25")
    parser.add_argument("--step-delay", type=float, default=DEFAULT_STEP_DELAY,
                        help=f"每步之后等待秒数，默认 {DEFAULT_STEP_DELAY:g}。"
                             f"页面动画慢导致截到过渡帧时调大")
    parser.add_argument("--dry-run", action="store_true",
                        help="只让模型决策，不真的操作手机（调试提示词用）")
    parser.add_argument("--no-reset", action="store_true",
                        help="跳过执行前的回桌面复位（默认会复位，保证起点可复现）")
    parser.add_argument("--no-auto-launch", action="store_true",
                        help="不复位后自动启动任务里提到的 App。默认会启动，"
                             "但**只对「纯打开」类任务生效**（如「打开设置」）—— "
                             "本机桌面是自绘界面读不到图标，从桌面起步基本走不通；"
                             "带别的要求的任务（如「打开微信发消息」）不自动启动，"
                             "交给模型自己输出 OPEN")
    parser.add_argument("--reset-app", default=None, metavar="包名",
                        help="复位时额外强制停止这个 App（如 com.tencent.mobileqq）。"
                             "默认只按 Home 不强杀，因为强杀会中断后台收消息")
    parser.add_argument("--quiet", action="store_true",
                        help="过程日志不打屏，只留结论/错误 —— 省调用方的上下文。"
                             "日志文件照常写")
    parser.add_argument("--log-file", default=None, metavar="路径",
                        help=f"日志路径。默认写 {LOG_DIR} 下带时间戳的文件；"
                             f"相对路径按脚本目录解析")
    parser.add_argument("--no-log", action="store_true", help="本次不写日志文件")
    args = parser.parse_args()
    # 兼容各种调用方传进来的多余引号（见 _strip_wrapping_quotes 的说明）
    args.task = _strip_wrapping_quotes(args.task)

    # ---- 输出控制（2026-10-01 加）----
    # ★ 必须尽早装上 Tee：后面所有 print（含结论、含报错）都要同步落盘。
    # 输出控制：设静默开关 + 装日志（含 stdout/stderr 的 Tee 重定向）
    # ★ 实现已挪到 output.setup_log() —— QUIET/_log_handle 是那边的模块级状态，
    #   在这里写 `global QUIET` 改不到它。
    output.setup_log(args.log_file, args.no_log, args.quiet)

    # ★ 日志装配后必须保证收尾（2026-10-05）：无论正常返回、抛异常还是 Ctrl+C，
    #   finally 都会把 stdout/stderr 还原、把日志文件关掉 —— 尤其是当本函数被
    #   当作 library 调用时，不能把调用方的 stdout 一直换着。
    try:
        device = pick_device(args.device)
        if device is None:
            print("没有检测到在线设备。请：\n"
                  "  1. 用 USB 线连接手机，USB 用途选「文件传输」\n"
                  "  2. 手机设置里打开「开发者选项」->「USB 调试」\n"
                  "  3. 手机上弹出授权提示时点「允许」\n"
                  "  4. 执行 adb devices 确认状态是 device\n")
            return 1

        info(f"使用设备：{device}")
        info(f"任务：{args.task}")
        info(f"模型：{args.model} @ {args.api_base}")
        info(f"退出码约定：0=完成  1=出错  2=卡死/输入失败被提前终止  "
             f"3=完成但可疑（可能是假成功，需人工核对）  "
             f"{EXIT_NEED_HUMAN}=需要人工输入（撞上密码框，已停下等你操作，不是失败）")

        client = OpenAI(base_url=args.api_base, api_key=args.api_key,
                        http_client=build_http_client())

        # ★ 下面这些「本地 llama-server 专属」的兜底**只在回环端点启用**（2026-10-05 修）。
        #   云端场景下它们全是错的（Claude 审查 #5）：
        #     · `/models` 失败即致命 —— 可不少 OpenAI 兼容服务并不实现 /models
        #     · 静默切到 available[0] —— 在云端那是「任意一个模型」（可能是
        #       embedding 模型、或更贵的模型），而且**会收到你的截图**
        #     · 宽松的名字匹配 —— 云端模型名是精确的
        local = is_loopback_endpoint(args.api_base)

        # 先探一次模型服务，避免跑到一半才发现 llama-server 没起
        models = None
        try:
            models = client.models.list()
        except Exception as exc:
            if local:
                print(f"\n连不上模型服务：{exc}\n"
                      f"请确认 llama-server 已在 {args.api_base} 启动。")
                return 1
            print(f"[警告] 探测 {args.api_base} 的模型列表失败：{exc}")
            print(f"        继续用配置的模型名「{args.model}」"
                  f"—— 云端不一定实现 /models。")

        # ★ 模型名兜底：服务端实际提供的 id 未必等于我们配置的名字。
        #   典型场景：llama-server 没带 -Alias 启动，别名是从文件名推导的
        #   （如 `stepfun-ai_GELab-Zero-4B-preview`），而我们默认发 `GELab-Zero`。
        #   与其让每步请求都失败，不如自动改用服务端真实提供的那个名字。
        model = args.model
        if models is not None:
            available = served_model_ids(models)
            if available and not model_name_matches(model, available, loose=local):
                if local:
                    fallback = available[0]
                    print(f"[模型名兜底] 服务端没有「{model}」，"
                          f"实际提供的是：{', '.join(available)}")
                    print(f"[模型名兜底] 自动改用「{fallback}」")
                    print(f"            （要固定用「{model}」，请让 llama-server 带 "
                          f"-Alias {model} 启动）")
                    model = fallback
                else:
                    print(f"[错误] 服务端没有「{model}」，可用的是："
                          f"{', '.join(available)}")
                    print("       云端模型名必须精确匹配，**不会**自动改用别的模型 ——"
                          " 那可能是 embedding 模型或更贵的模型，还会收到你的截图。")
                    return 1

        return run(args.task, device, client, model, args.view_width,
                   args.max_steps, args.step_delay, args.dry_run,
                   reset_app=args.reset_app, no_reset=args.no_reset,
                   auto_launch=not args.no_auto_launch)
    finally:
        output.teardown_log()

if __name__ == "__main__":
    sys.exit(main())