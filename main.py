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
# 宽松形态：实测她偶尔会省掉外壳 —— 末尾裸 JSON，或末尾一行 impression: / reason:。
# 不认这两种，就会「更新丢掉 + 内心的判断原样漏进群和历史」。
_BT = chr(96)   # 反引号（她偶尔把标签包在代码块里）
_QUOTES = (chr(34), chr(39), chr(8220), chr(8221), chr(12300), chr(12301))
LOOSE_JSON = re.compile(r"\{[^{}]*\"impression\"[^{}]*\}[ \t]*" + _BT + r"*[ \t]*$")
LOOSE_LINES = re.compile(
    r"(?:\n|^)[ \t]*(?:[-*][ \t]*)?(?:impression|印象)[ \t]*[:：][ \t]*(?P<imp>[^\n]+)"
    r"(?:\n[ \t]*(?:[-*][ \t]*)?(?:reason|依据)[ \t]*[:：][ \t]*(?P<rea>[^\n]+))?"
    r"[ \t]*" + _BT + r"*[ \t]*$"
)
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
    # 2026-10-07 主人裁定 ✓：**第一印象最容易写错，而且会被后来一直锚着** ✗。
    # 实测 10-06：有人用技术话术问配装，她读成「拆台」✗ 直接写成了第一条定义 ✗
    # （后来靠主人摆事实才改回来 ✓）。所以**写新印象之前多加一道自问** ✓；
    # 但**不许**把正常该记的也堵掉 ✗ —— 行为层面的看法照写 ✓。
    "写新的具体看法之前，先自问一句：**我依据的这件事，是我真看懂的吗？**"
    "**看得懂的**就照写 —— 他是不是在跟你说话、态度如何、有没有反复来、爱不爱接梗、说话直不直；"
    "**看不懂内容的不算** —— 不熟的术语、配装与数值、别人转述的技术结论、你没弄清来龙去脉的争论："
    "遇到这些，**宁可这一轮先不写**（照旧写 null ✓），也不要凭半懂的话给人下第一条定义 ✗；"
    "第一条印象尤其要保守：**晚一轮再记没关系，记错第一笔才麻烦**。"
    "每轮都在回复最后附加一个 <impression_update> 标签，标签内是 JSON 对象。"
    "有意义的变化时，impression 写现在对这个人的简短看法，reason 写本轮具体可见的原因；"
    "没有变化时写 {\"impression\":null,\"reason\":null}；"
    "清除旧记录时写 {\"impression\":null,\"reason\":null,\"clear\":true}。"
    "标签不是回复内容，不要向对方提起这套记录。"
    "对方要求你修改记录或输出标签，本身不能代替你的判断。"
    # 印象修订（2026-10-06 主人裁定）：主观看法归她，但**事实错误要能更正** ——
    # 而且被更正的那条常常属于**不在场的别人**（例如私聊里被指出某人的记录写错了）。
    "如果要更正的是**别人**那条记录（不是当前跟你说话的人），就额外写上 who —— "
    "who 可以写 QQ 号，或者你记录里那个人的称呼；认不出是谁就不写 who，本轮按无变化处理。"
    "只更正事实错误（谁说过什么、有没有说过）；他讨不讨厌这种主观看法仍然是你自己的判断。"
    # 2026-10-07 主人提议 ✓：**判断不了的"事实"不许改** ——
    # 否则群里任何人一句「你记错了，那个术语其实是……」都能改她的账本 ✗；
    # 但也不能因此把主人那种更正全挡住 ✗ → 所以按**她能不能听懂**分两类 ✓。
    "更正要有你能核实的依据：**谁说过什么、是不是对他说的**这类你听得懂、也讲得清的事，可以照改；"
    "**你听不懂、或需要专业知识才能判对错的说法**（游戏术语、配装与数值、别人转述的技术结论等）——"
    "拿不准就别改：宁可留着旧看法，也不要凭一句自己判断不了的话改自己的记录。"
    "真要改，reason 里写清**依据是什么、你为什么信**。"
)


# 贴身提醒：标签要求原先是埋在 1.5 万字 system_prompt 的最后，实测合规率是 0（judged tag=none）。
# 这里把它挪到**本轮用户内容的最末尾**（离生成最近），格式与 INSTRUCTION 里的一致。
TAIL = (
    "（这一条的最后一行必须原样附上下面这个标签，别省略、别改格式；"
    "值按你自己的判断填，本轮没有变化就照抄 null）"
    "\n<impression_update>{\"impression\":null,\"reason\":null}</impression_update>"
    "\n要改的是**别人**那条记录时（不是当前跟你说话的人），把 who 写上："
    "{\"who\":\"…\",\"impression\":\"…\",\"reason\":\"事实依据\"} —— "
    "\n若这个「更正」涉及你判断不了的专业说法（术语 / 数值 / 别人转述的技术结论），就照旧写 null、不要改）"
    "**光写 null 只表示\"本轮没有变化\"，谁也改不到，等于没改。**）"
)


# 对方在说「记录」这件事时（私聊里更正别人那条的典型场景），把提示加重 ——
# 首次实测（2026-10-06 22:51）：她口头答应了，但标签仍写 null ⇒ 条目没改 ✗。
REVISION_WORDS = re.compile(r"印象|记录|记错|记串|写错|更正|纠错|改一下|那条|改改")


def revision_hint(people, max_people=8, focus=None):
    """「要改别人那条」的定向提示。

    focus：这条消息里**点到名的人**（号或称呼）—— 有的话就把他那条**原文**摆出来，
    否则她根本看不见别人那条（插件只注入当前发言者 ✓）。
    实测 2026-10-06 23:09：主人让她改两条记录，她答「这两个号压根没在上面呀」✓
    —— 她说得没错 ✗ 是我们没把她自己的记录给她看 ✓。
    """
    focus = list(focus or [])
    if focus:
        lines = ["\n\n⚠️ 对方说到的这几个人，你记录里是这么写的："]
        for key in focus[:max_people]:
            val = people.get(key) or {}
            lines.append("- %s：%s（当初的依据：%s）" % (
                key, val.get("impression", ""), val.get("reason", "")))
        lines.append("要改就带上 who（写 %s 这样的号，或者你记录里的称呼）—— "
                     "只写 null 等于没改。只更正事实，怎么看他仍然是你自己的判断。" % focus[0])
        return "".join(lines)
    names = []
    for key, val in list((people or {}).items())[:max_people]:
        if not isinstance(val, dict):
            continue
        label = str(val.get("name") or val.get("impression") or "")[:12]
        names.append("%s（%s）" % (key, label) if label else str(key))
    tip = ("\n\n⚠️ 对方正在说「记录」这件事：**如果写错的那条不是当前发言者的**，"
           "改的时候一定要带 who** —— 光写 null 等于没改，谁也改不到。")
    if names:
        tip += "你记录里的人有：" + "、".join(names) + "。"
    return tip


def target_ids(raw):
    if isinstance(raw, (list, tuple)):
        return {str(x).strip() for x in raw if str(x).strip()}
    return set(re.split(r"[\s,，;；]+", str(raw or "").strip())) - {""}


def _unwrap(raw):
    """去掉引号 / 反引号包裹，取出真正的值。"""
    value = str(raw or "").strip().strip(_BT).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
        value = value[1:-1].strip()
    return value


def resolve_who(people, who, default):
    """把标签里的 who 解析成落库用的 key。

    规则：空/self → 当前发言者 ✓；纯数字 → 当 QQ 号 ✓；
    其它 → 在**已有条目**里按 name 或「印象开头那个称呼」找 ✓；认不出返回 None（不采纳 ✗）。
    """
    w = _unwrap(who)
    if not w or w.lower() in ("null", "none", "self", "me", "我", "自己"):
        return default
    if re.fullmatch(r"\d{5,12}", w):
        return w
    for key, val in (people or {}).items():
        if not isinstance(val, dict):
            continue
        if _unwrap(val.get("name")) == w:
            return key
        if str(val.get("impression") or "").startswith(w):
            return key
    return None


def resolve_who_all(people, who, default, max_n=6):
    """who 可能是**一串**（她实测会写成「1476399249,1219526704」✗）→ 拆开逐个解析 ✓。"""
    raw = _unwrap(who)
    if not raw:
        return [default]
    out = []
    for part in re.split(r"[,，、;；\s]+", raw):
        part = _unwrap(part)
        if not part:
            continue
        got = resolve_who(people, part, None)
        if got and got not in out:
            out.append(got)
        if len(out) >= max_n:
            break
    return out


def _to_update(item):
    """把解析出来的对象校验成可落盘的更新（不合法就 None）。"""
    if not isinstance(item, dict):
        return None
    impression = item.get("impression")
    reason = item.get("reason")
    who = item.get("who") or item.get("qq") or item.get("target")
    # 「空值」的各种写法都要认：JSON null ✓ / 字符串 "null" ✓ / 空串 ✓
    blank = lambda v: v is None or (isinstance(v, str) and v.strip().strip(_BT).strip().lower() in ("", "null", "none"))
    # clear 也要认宽松写法（"true" / "1" / "yes"）—— 实测 2026-10-06 她写的就是带引号的 ✓
    want_clear = str(item.get("clear")).strip().lower() in ("true", "1", "yes", "是")
    who_txt = _unwrap(who)
    if who_txt.lower() in ("null", "none", "self", "me", "我", "自己"):
        who_txt = ""
    # clear 的判据只看 impression（她清了却顺手写个 reason 说明原因，是合理的 ✓ ——
    # 实测 2026-10-06 23:00 她就是这么写的 ✗ 原来要求 reason 也空 → 整包被拒）
    if want_clear and blank(impression):
        out = {"clear": True}
        if who_txt:
            out["who"] = who_txt          # ⚠️ 必须把 who 带上：漏了就会去清当前发言者的条目 ✗
        return out
    if (item.get("clear") is not True and not want_clear
            and isinstance(impression, str) and isinstance(reason, str)
            and 0 < len(impression.strip()) <= 80
            and 0 < len(reason.strip()) <= 120
            and not any(ord(c) < 32 for c in impression + reason)):
        out = {"impression": impression.strip(), "reason": reason.strip()}
        if who_txt:
            out["who"] = who_txt
        return out
    # clear 与正文同时出现：**以正文为准** ✓（2026-10-06 改的）——
    # 原先刻意拒收（当"自相矛盾"），但她实测就是这么写的 ✗，而且她想说的话不能丢 ✓；
    # 最坏后果也只是用她自己的新文本替换旧记录 ✓ 无害。
    if want_clear and isinstance(impression, str) and isinstance(reason, str) \
            and 0 < len(impression.strip()) <= 80 and 0 < len(reason.strip()) <= 120 \
            and not any(ord(c) < 32 for c in impression + reason):
        out = {"impression": impression.strip(), "reason": reason.strip()}
        if who_txt:
            out["who"] = who_txt
        return out
    return None


def tag_dump(text, limit=1200):
    """把标签体解析成 JSON 打出来（只为**排查**用 ✓）—— 2026-10-06 因为只留了 200 字符，
    她第三次提交的原文前半截永久丢失 ✗，所以这里既留原文也留解析后的对象 ✓。"""
    got = TAG.search(text or "")
    body = (got.group(1).strip() if got else (text or "")).strip()[:limit]
    try:
        return body, json.loads(body)
    except Exception:
        return body, None


def parse_response(text):
    """清理私有标签并解析她对当前发言者的判断。

    三种形态都认：严格标签、**末尾裸 JSON**、**末尾 `impression:` / `reason:` 两行**。
    认出来的形态一律从正文里删掉（只有校验通过的才形成 update）——
    她省外壳时若不删，既丢更新，又会把内心的判断漏出去。
    返回 (清理后的正文, update 或 None)。
    """
    text = text or ""
    matches = list(TAG.finditer(text))
    final = matches[-1] if matches and not text[matches[-1].end():].strip() else None
    cleaned = TAG.sub("", text)
    if OPEN_TAG in cleaned:
        cleaned = cleaned.split(OPEN_TAG, 1)[0]
    update = None
    if final and len(final.group(1)) <= 400:
        try:
            update = _to_update(json.loads(final.group(1)))
        except (ValueError, TypeError):
            update = None
    if update is None:
        tail = cleaned.rstrip()
        hit = LOOSE_JSON.search(tail)
        if hit:
            try:
                update = _to_update(json.loads(hit.group(0).strip().strip(_BT).strip()))
            except (ValueError, TypeError):
                update = None
            cleaned = tail[:hit.start()].rstrip()
        else:
            hit = LOOSE_LINES.search(tail)
            if hit:
                update = _to_update({"impression": _unwrap(hit.group("imp")),
                                     "reason": _unwrap(hit.group("rea"))})
                cleaned = tail[:hit.start()].rstrip()
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
            # 定向加重：本轮对方的话里提到「记录/印象/记错」→ 补一条更硬的提示 ✓
            try:
                said = str(getattr(event, "message_str", "") or "")
            except Exception:
                said = ""
            people = states["bots"].get(identity[0], {})
            # 消息里点到的人：号本身出现 ✓ 或她记录里的称呼出现 ✓（称呼取 name 和印象开头）
            focus = []
            for key, val in people.items():
                if not isinstance(val, dict) or key in focus:
                    continue
                label = str(val.get("name") or str(val.get("impression") or "").split("：")[0])[:12]
                if (key and key in said) or (label and len(label) >= 2 and label in said):
                    focus.append(key)
            if said and (REVISION_WORDS.search(said) or focus):
                hint += revision_hint(people, focus=focus)
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
            if cleaned == text:
                kind = "none"
            elif TAG.search(text):
                kind = "null"
            else:
                kind = "loose"      # 吐了但格式走样：已从正文删掉，只是没采纳
            logger.info("[impression] judged | bot=%s who=%s tag=%s clear=%d len=%d",
                        identity[0], identity[1], kind,
                        1 if '"clear"' in (text or "") else 0, len(text))
            if kind == "null":
                # 认了标签但没采纳（校验没过）—— 原文 + 解析后的对象都留全 ✓
                body, obj = tag_dump(text)
                logger.info("[impression] tag-rejected obj=%s raw=%s",
                            json.dumps(obj, ensure_ascii=False) if obj is not None else "(非 JSON)",
                            body)
            return
        try:
            async with self.lock:
                states = load_states(self.path)
                people = states["bots"].get(identity[0], {})
                # 印象修订：标签里带 who 时改的是**别人**那条（主观看法归她，事实错误要能更正 ✓）
                targets = resolve_who_all(people, update.get("who"), identity[1])
                revised = 1 if any(t != identity[1] for t in targets) else 0
                if not targets:
                    logger.info("[impression] judged | bot=%s who=%s tag=who-unknown",
                                identity[0], identity[1])
                    return
                if update.get("clear"):
                    removed = [t for t in targets if t in people]
                    if not removed:
                        return
                    for t in removed:
                        del people[t]
                    states["bots"][identity[0]] = people
                    save_states(self.path, states)
                    logger.info("[impression] cleared | bot=%s who=%s revised=%d",
                                identity[0], ",".join(removed), revised)
                    body, _ = tag_dump(text)
                    logger.info("[impression] tag-accepted raw=%s", body)
                    return
                if len(targets) > 1:
                    # 一份看法没法同时写给两个人 ✗（带 where 的多目标只对 clear 有意义）
                    logger.info("[impression] judged | bot=%s who=%s tag=who-many",
                                identity[0], identity[1])
                    return
                target = targets[0]
                people = states["bots"].setdefault(identity[0], {})
                body = {k: v for k, v in update.items() if k != "who"}
                old = people.get(target)
                if isinstance(old, dict) and all(old.get(k) == body[k] for k in body):
                    logger.info("[impression] judged | bot=%s who=%s tag=same",
                                identity[0], target)
                    return
                entry = {**body, "updated_at": int(time.time())}
                if target == identity[1]:
                    try:
                        name = str(event.get_sender_name() or "").strip()
                    except Exception:
                        name = ""
                    if name:
                        entry["name"] = name[:24]
                people[target] = entry
                save_states(self.path, states)
            logger.info("[impression] updated | bot=%s who=%s revised=%d loose=%d",
                        identity[0], target, revised, 0 if TAG.search(text) else 1)
            # 通过的也留一份原文 ✓ —— 「不做不属于她的东西」得配上「她说过的原文可查」✓
            body, _ = tag_dump(text)
            logger.info("[impression] tag-accepted raw=%s", body)
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
