"""任务文本判定 —— 是不是发文字任务、是不是简单任务、是不是纯打开。"""

from __future__ import annotations

import re

from .apps import load_app_aliases



# ============================================================
# 命令行入口
# ============================================================

def _strip_wrapping_quotes(text: str) -> str:
    """剥掉任务文本首尾「成对」的多余引号，只剥一层。

    为什么需要：任务文本从「模型输出 toolCall」到「shell 执行」要穿过好几层，
    不同层对引号的处理不一致 ——

      * Git Bash 收到 --task '打开设置'  -> 会剥掉单引号，拿到 打开设置
      * Windows 的 .cmd 用 %* 转发时    -> 单引号是普通字符，拿到 '打开设置'
      * 模型自作主张套一层 bash -c '..' -> 又多一层引号

    带着引号的任务文本会被原样喂给视觉模型，它会去搜一个「带引号的目标」，
    结果就是找不到、任务失败。

    只剥首尾成对且相同的引号，避免误伤任务本身内含的引号
    （例如 --task '搜索 "蓝牙" 设置' 里的内层双引号不受影响）。
    """
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1].strip()
    return s




def _looks_like_text_task(task: str) -> bool:
    """
    这个任务是不是「需要打出文字」的那类。

    用途：模型声称 COMPLETE、但全程没 TYPE 过任何文字时，这个判断决定
    要不要把它标成「可疑完成」。

    只是关键词启发式，宁可少报（漏判只是少一条警告，误判会打扰用户）。
    ★ 单字关键词（尤其「说」）极易被复合词误命中，必须先剔除再判断。
    """
    t = task
    for w in _SAY_FALSE_POSITIVES:
        t = t.replace(w, "")

    keys = ("发消息", "发送", "说", "告诉", "回复", "输入", "打字", "搜索",
            "评论", "留言", "短信", "邮件")
    if any(k in t for k in keys):
        return True

    # 提到聊天类 App 时，光有 App 名不算「发文字任务」—— 还要有「给某人 / 消息 /
    # 聊天 / 回复」这类线索。否则「打开微信」会被误判成发消息任务，
    # 配合下面「步数太少」的规则一起把正常完成标成可疑（2026-09-30 实测误报）。
    chat_apps = ("微信", "qq", "企微", "钉钉")
    if any(a in t.lower() for a in chat_apps):
        hints = ("给", "消息", "聊天", "发送", "发一", "发个", "问", "回复", "联系")
        return any(h in t for h in hints)
    return False




def _is_pure_open_task(task: str, target_pkg: str) -> bool:
    """
    任务是不是「纯粹打开某个 App」—— 除了打开它，没有任何别的要求。

    ★ 为什么不用 _is_trivial_task 当护栏（2026-10-01）：
      那个函数用「长度 ≤ 8」当快捷判据，「打开微信发消息」(7 字) 会被误判成
      纯打开任务 → 短路 → 消息没发就宣布完成。误判代价太大，所以这里
      只认「剥掉动词和 App 名后什么都不剩」，宁可放过不可错杀。

    :param target_pkg: 任务里识别出的目标 App 包名
    :return: True 表示可以跳过模型、直接判定任务完成
    """
    t = _strip_wrapping_quotes(task).strip()

    # ① 剥掉打开类动词和修饰词
    for v in ("帮我", "请", "麻烦", "打开", "启动", "进入", "开一下", "一下",
              "手机上的", "手机的", "手机里的", "手机里", "我要", "我想"):
        t = t.replace(v, "")

    # ② 剥掉这个 App 的名字（包名 + 中文别名）
    t = t.replace(target_pkg, "")
    for alias, pkg in load_app_aliases().items():
        if pkg == target_pkg:
            t = t.replace(alias, "")

    # ③ 剥掉标点和空白
    t = re.sub(r"[\s,，。、;；!！?？~～]", "", t)

    return not t          # 什么都不剩 = 纯打开




def _is_trivial_task(task: str) -> bool:
    """
    这个任务是不是「一两步就该做完」的简单任务。

    用途：步数太少的可疑判定要排除掉这类任务 ——
    「打开设置」可能 2 步就完成了，不该被标成可疑。
    """
    t = task.strip()
    if not t:
        return False

    # ① 很短的、且不含发送/输入类动词的，视为简单任务
    if len(t) <= 8 and not _looks_like_text_task(t):
        return True

    # ② 纯「打开某个 App」：把打开类动词 + 修饰词 + App 名都剥掉后**什么都不剩**，
    #    说明整句就是一个启动动作 —— 这时 App 名多长都算简单任务。
    #    ★ 为什么要单独加这条（2026-09-30 实测）：
    #      长度闸门（≤8）对长 App 名会误判 ——「打开手机的轻小说文库」是 10 个字，
    #      配上自动启动后 1~2 步就能完成，于是被「步数太少」规则标成可疑完成 → 退出码 3
    #      → 调用方当成失败。这类「纯打开」任务不该受 App 名长度影响。
    if not _looks_like_text_task(t):
        residual = t
        for v in ("帮我", "请", "麻烦", "打开", "启动", "进入", "开一下",
                  "手机上的", "手机的", "手机里的", "手机里", "一下"):
            residual = residual.replace(v, "")
        for alias in sorted(load_app_aliases(), key=len, reverse=True):
            if alias:
                residual = residual.replace(alias, "")
        if not residual.strip():
            return True

    trivial_keys = ("回桌面", "回到桌面", "按home", "home", "锁屏", "息屏", "截屏")
    return any(k in t.lower() for k in trivial_keys)




# ============================================================
# 动作执行
# ============================================================

# 「说」在这些复合词里是名词或别的意思，**不代表「要打字」** ——
# 判断前先把它们抠掉，再看剩下的「说」是不是真作动词。
# ★ 实测踩坑（2026-09-30，代价很大）：
#   任务「打开轻小说文库」被「小**说**」误命中 → 判成发文字任务 → 全程没有 TYPE
#   → 标成「可疑完成」→ 退出码 3 → 调用方（Pi）把退出码 3 当成失败，
#   回给用户「我无法打开轻小说文库，因为该文件夹不存在」。
#   **一次成功的任务，被一连串误报说成了失败，还编了个假原因。**
_SAY_FALSE_POSITIVES = (
    "小说", "学说", "演说", "说明", "说服", "说法", "传说", "听说",
    "据说", "解说", "述说", "劝说", "诉说",
)
