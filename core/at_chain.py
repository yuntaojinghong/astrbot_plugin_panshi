"""把带 CQ 码的文本转成真正的消息组件。

问题
----

入群欢迎的模板里 ``{at}`` 会被替换成 ``[CQ:at,qq=123]`` 这样的 CQ 码字符串，
然后整段用 ``event.plain_result()`` 发出去。``plain_result`` 把内容包成
**纯文本**组件，于是 CQ 码不会被协议端解释，用户看到的是字面量：

    [CQ:at,qq=3823105457] 你好呀，欢迎来到「机器人调试群」

（线上截图确认了这一点。）

修法
----

把这段文本解析成 ``[Plain("…"), At(qq=123), Plain("…")]`` 这样的组件链，
用 ``event.chain_result()`` 发出。``At`` 是 AstrBot 的一等组件，
适配器会转成协议端认识的 at 段。

为什么在插件侧解析，而不是直接构造组件
--------------------------------------

欢迎语来自用户可编辑的模板（面板里能改），里面可能混着 CQ 码、
纯文本、甚至多个 at。保持"模板是字符串"这一现状、在发送前统一转换，
比要求用户去构造组件对象现实得多。

纯函数，无 IO，便于单测。
"""

from __future__ import annotations

import re

#: CQ 码：``[CQ:at,qq=123]`` 或 ``[CQ:at,qq=123,name=某某]``
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")

#: 支持的 CQ 类型 → astrbot 组件名。只映射确定存在的几个，
#: 其余原样保留为文本，避免拼出插件环境里不存在的类。
_SUPPORTED = {"at": "At", "face": "Face", "image": "Image"}


def _parse_params(raw: str) -> dict:
    """解析 ``,qq=123,name=某`` 这种参数串。"""
    out: dict[str, str] = {}
    for part in str(raw or "").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def at_chain(text: str):
    """把文本转成消息组件列表；没有可识别的 CQ 码时返回 ``None``。

    返回 ``None`` 表示"不需要特殊处理，照常用纯文本发"，
    这样调用方可以保持原有路径不变，减少改动面。
    """
    raw = str(text or "")
    if not raw or "[CQ:" not in raw:
        return None

    try:
        from astrbot.api.message_components import At
    except Exception:
        # 拿不到 At（老版本/极简环境）→ 退回纯文本，至少不崩
        return None

    # Plain 只用来包文字；拿不到就退化成"整段文本 + at 组件"，
    # 也比把 CQ 码当字面量发出去好。
    Plain = None
    try:
        from astrbot.api.message_components import Plain as _Plain
        Plain = _Plain
    except Exception:
        Plain = None

    parts: list = []

    def push_text(s: str) -> None:
        if not s:
            return
        if Plain is not None:
            try:
                parts.append(Plain(s))
                return
            except Exception:
                pass
        parts.append(s)

    pos = 0
    for m in _CQ_RE.finditer(raw):
        push_text(raw[pos:m.start()])
        pos = m.end()
        kind = m.group(1).lower()
        params = _parse_params(m.group(2))

        if kind == "at":
            qq = params.get("qq", "").strip()
            if not qq or qq == "all":
                # @全体 需要 AtAll；这里不擅自扩大权限，按原文保留
                push_text(m.group(0))
                continue
            try:
                parts.append(At(qq=qq))
            except Exception:
                try:
                    parts.append(At(qq=int(qq)))
                except Exception:
                    push_text(m.group(0))
            continue

        # 其余类型不在白名单里 → 原样保留，避免构造出不存在的类
        push_text(m.group(0))

    push_text(raw[pos:])

    # 全是纯文本等于没转换，交给调用方走原路径
    if not any(type(p).__name__ == "At" for p in parts):
        return None
    return parts
