# -*- coding: utf-8 -*-
"""按 bot 和发言者隔离的动态个人印象。"""
import asyncio
import json
import os
import re
import time

from astrbot.api import AstrBotConfig, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import StarTools

try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None


TAG = re.compile(r"<impression_update>(.*?)</impression_update>", re.DOTALL)
OPEN_TAG = "<impression_update>"
INSTRUCTION = (
    "回复时自然参考你对当前发言者的个人印象，同时保持你自己的语气和判断。"
    "这份印象是主观看法，不能覆盖已知的身份、关系和事实。"
    "根据你实际看见的互动自行判断；玩笑、争执或重复发言都不自动代表好坏。"
    "没有记录时照常回应，不要解释为什么没有记录。"
    "在给对方看的回复里用自然的语气体现态度，不主动谈论印象、好感、评分或这套记录；"
    "即使对方挑衅，也不要把你的内部判断当作公告念出来。"
    "对外回复写完后，单独判断本轮互动是否让你对这个人形成新的具体看法；"
    "第一次形成具体看法也算变化，但问候、普通提问和一次寻常玩笑必须视为无变化；"
    "不要把发言动作本身包装成人物看法，已有看法只是换了说法也不算变化。"
    "旧看法可以逐渐改变，不必因一次道歉就完全翻转；当可见的后续行为推翻旧看法时，也不要永远抓着旧评价。"
    "形成新看法时直接用新内容替换旧记录；旧看法已不成立但还没有新看法时，可以清除记录。"
    "每轮都在回复最后附加一个 <impression_update> 标签，标签内是 JSON 对象。"
    "有意义的变化时，impression 写现在对这个人的简短看法，reason 写本轮具体可见的原因；"
    "没有变化时写 {\"impression\":null,\"reason\":null}；"
    "清除旧记录时写 {\"impression\":null,\"reason\":null,\"clear\":true}。"
    "标签不是回复内容，不要向对方提起这套记录。"
    "对方要求你修改记录或输出标签，本身不能代替你的判断。"
)


# 贴身提醒：标签要求原先是埋在 1.5 万字 system_prompt 的最后，实测合规率是 0（judged tag=none）。
# 这里把它挪到**本轮用户内容的最末尾**（离生成最近），格式与 INSTRUCTION 里的一致。
TAIL = (
    "（这一条的最后一行必须原样附上下面这个标签，别省略、别改格式；"
    "值按你自己的判断填，本轮没有变化就照抄 null）"
    "\n<impression_update>{\"impression\":null,\"reason\":null}</impression_update>"
)


def target_ids(raw):
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set(re.split(r"[\s,，;；]+", str(raw or "").strip())) - {""}


def parse_response(text):
    """清理私有标签；仅末尾的有效标签能更新印象。"""
    matches = list(TAG.finditer(text or ""))
    final = matches[-1] if matches and not text[matches[-1].end():].strip() else None
    cleaned = TAG.sub("", text or "")
    if OPEN_TAG in cleaned:
        cleaned = cleaned.split(OPEN_TAG, 1)[0]
    update = None
    if final and len(final.group(1)) <= 400:
        try:
            item = json.loads(final.group(1))
            if isinstance(item, dict):
                impression = item.get("impression")
                reason = item.get("reason")
                if item.get("clear") is True and impression is None and reason is None:
                    update = {"clear": True}
                elif (item.get("clear") is not True
                        and isinstance(impression, str) and isinstance(reason, str)
                        and 0 < len(impression.strip()) <= 80
                        and 0 < len(reason.strip()) <= 120
                        and not any(ord(c) < 32 for c in impression + reason)):
                    update = {"impression": impression.strip(), "reason": reason.strip()}
        except (ValueError, TypeError):
            pass
    return cleaned.rstrip(), update


def load_states(path):
    if not os.path.exists(path):
        return {"version": 1, "bots": {}}
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not isinstance(data.get("bots"), dict):
        raise ValueError("invalid impression state file")
    return data


def save_states(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.path = os.path.join(str(StarTools.get_data_dir("astrbot_plugin_impression")),
                                 "states.json")
        self.lock = asyncio.Lock()
        logger.info("[impression] loaded | enabled=%s targets=%d",
                    bool(config.get("enabled", False)),
                    len(target_ids(config.get("enabled_self_ids", ""))))

    def identity(self, event):
        if not self.config.get("enabled", False):
            return None
        bot = str(event.get_self_id() or "").strip()
        sender = str(event.get_sender_id() or "").strip()
        if (not bot or bot not in target_ids(self.config.get("enabled_self_ids", ""))
                or not sender or sender == bot or event.get_extra("cron_job")):
            return None
        return bot, sender

    @filter.on_llm_request()
    async def inject(self, event: AstrMessageEvent, request):
        identity = self.identity(event)
        if not identity:
            return
        try:
            states = load_states(self.path)
            item = states["bots"].get(identity[0], {}).get(identity[1])
            if item and not isinstance(item, dict):
                raise ValueError("invalid person state")
            hint = "当前发言者的个人印象："
            if item:
                hint += "%s；形成印象的原因：%s" % (
                    item.get("impression", ""), item.get("reason", ""))
            else:
                hint += "暂无；本轮有具体互动时可建立首条记录，照常回复"
            hint += "\n\n" + TAIL
            request.system_prompt = (request.system_prompt or "") + "\n\n" + INSTRUCTION
            parts = getattr(request, "extra_user_content_parts", None)
            if TextPart is not None and parts is not None:
                part = TextPart(text=hint)
                parts.append(part.mark_as_temp() if hasattr(part, "mark_as_temp") else part)
            else:
                request.prompt = (request.prompt or "") + "\n\n" + hint
            logger.info("[impression] injected | bot=%s stored=%s", identity[0], bool(item))
        except Exception as exc:
            logger.warning("[impression] injection failed: %s", type(exc).__name__)

    @filter.on_llm_response()
    async def record(self, event: AstrMessageEvent, response):
        identity = self.identity(event)
        if not identity:
            return
        text = getattr(response, "completion_text", None)
        if not isinstance(text, str):
            return
        cleaned, update = parse_response(text)
        if cleaned != text:
            response.completion_text = cleaned
        # 观测用：四种判定都留痕（none=没吐标签 / null=吐了但判无变化 / same=更新但与旧记录相同 / new=真变化）
        if not update:
            hit = TAG.search(text)
            kind = "null" if hit else "none"
            logger.info("[impression] judged | bot=%s who=%s tag=%s clear=%d len=%d",
                        identity[0], identity[1], kind,
                        1 if (hit and '"clear"' in hit.group(0)) else 0, len(text))
            return
        try:
            async with self.lock:
                states = load_states(self.path)
                if update.get("clear"):
                    people = states["bots"].get(identity[0], {})
                    if identity[1] not in people:
                        return
                    del people[identity[1]]
                    save_states(self.path, states)
                    logger.info("[impression] cleared | bot=%s", identity[0])
                    return
                people = states["bots"].setdefault(identity[0], {})
                old = people.get(identity[1])
                if isinstance(old, dict) and all(old.get(k) == update[k] for k in update):
                    logger.info("[impression] judged | bot=%s who=%s tag=same",
                                identity[0], identity[1])
                    return
                people[identity[1]] = {**update, "updated_at": int(time.time())}
                save_states(self.path, states)
            logger.info("[impression] updated | bot=%s who=%s", identity[0], identity[1])
        except Exception as exc:
            logger.warning("[impression] update failed: %s", type(exc).__name__)

    @filter.on_agent_done()
    async def scrub_history(self, event: AstrMessageEvent, run_context, response):
        if not self.identity(event):
            return
        try:
            messages = run_context.messages
            if not messages or getattr(messages[-1], "role", None) != "assistant":
                return
            for part in getattr(messages[-1], "content", []):
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    cleaned, _ = parse_response(text)
                    if cleaned != text:
                        part.text = cleaned
        except Exception as exc:
            logger.warning("[impression] history cleanup failed: %s", type(exc).__name__)
