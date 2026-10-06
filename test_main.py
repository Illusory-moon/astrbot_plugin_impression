import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def load_plugin(data_dir):
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    root = types.ModuleType("astrbot")
    core = types.ModuleType("astrbot.core")
    agent = types.ModuleType("astrbot.core.agent")
    message = types.ModuleType("astrbot.core.agent.message")

    class Star:
        def __init__(self, context):
            self.context = context

    class Logger:
        def info(self, *args):
            pass

        def warning(self, *args):
            pass

    class TextPart:
        def __init__(self, text):
            self.text = text
            self.temporary = False

        def mark_as_temp(self):
            self.temporary = True
            return self

    def hook(*args, **kwargs):
        return lambda fn: fn

    api.AstrBotConfig = dict
    api.logger = Logger()
    api.star = star_api
    star_api.Star = Star
    star_api.Context = object
    star_api.StarTools = types.SimpleNamespace(get_data_dir=lambda name: data_dir)
    event_api.AstrMessageEvent = object
    event_api.filter = types.SimpleNamespace(
        on_llm_request=hook, on_llm_response=hook, on_agent_done=hook)
    message.TextPart = TextPart
    modules = {
        "astrbot": root,
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
        "astrbot.core": core,
        "astrbot.core.agent": agent,
        "astrbot.core.agent.message": message,
    }
    spec = importlib.util.spec_from_file_location("impression_under_test", Path(__file__).with_name("main.py"))
    plugin = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(plugin)
    return plugin


class Event:
    def __init__(self, bot, sender, cron=False, text=""):
        self.bot = bot
        self.sender = sender
        self.cron = cron
        self.message_str = text

    def get_self_id(self):
        return self.bot

    def get_sender_id(self):
        return self.sender

    def get_extra(self, key):
        return self.cron if key == "cron_job" else None


class ImpressionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.module = load_plugin(self.temp.name)

    def test_off_by_default_and_explicit_bot_scope(self):
        plugin = self.module.Main(None, {})
        self.assertIsNone(plugin.identity(Event("fire", "person")))
        plugin.config = {"enabled": True, "enabled_self_ids": ""}
        self.assertIsNone(plugin.identity(Event("fire", "person")))
        plugin.config["enabled_self_ids"] = "fire"
        self.assertEqual(plugin.identity(Event("fire", "person")), ("fire", "person"))
        self.assertIsNone(plugin.identity(Event("water", "person")))
        self.assertIsNone(plugin.identity(Event("fire", "fire")))
        self.assertIsNone(plugin.identity(Event("fire", "person", cron=True)))

    def test_updates_only_the_visible_sender_and_strips_private_tag(self):
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        request = types.SimpleNamespace(system_prompt="persona", prompt="hello")
        asyncio.run(plugin.inject(Event("fire", "person"), request))
        self.assertIn("可建立首条记录，照常回复", request.prompt)
        self.assertIn("不主动谈论印象、好感", request.system_prompt)
        response = types.SimpleNamespace(completion_text=(
            '回复~\n<impression_update>{"impression":"有点爱挑衅",'
            '"reason":"这轮反复拿同一件事逗我"}</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "回复~")
        states = self.module.load_states(plugin.path)["bots"]
        self.assertEqual(set(states), {"fire"})
        self.assertEqual(set(states["fire"]), {"person"})
        self.assertEqual(states["fire"]["person"]["impression"], "有点爱挑衅")
        history_part = self.module.TextPart(
            '回复~\n<impression_update>{"impression":"有点爱挑衅",'
            '"reason":"这轮反复拿同一件事逗我"}</impression_update>')
        run_context = types.SimpleNamespace(messages=[
            types.SimpleNamespace(role="assistant", content=[history_part])])
        asyncio.run(plugin.scrub_history(Event("fire", "person"), run_context, response))
        self.assertEqual(history_part.text, "回复~")
        request2 = types.SimpleNamespace(system_prompt="persona", prompt="hello")
        asyncio.run(plugin.inject(Event("fire", "person"), request2))
        self.assertIn("有点爱挑衅", request2.prompt)
        water_request = types.SimpleNamespace(system_prompt="persona", prompt="hello")
        asyncio.run(plugin.inject(Event("water", "person"), water_request))
        self.assertEqual(water_request.prompt, "hello")
        self.assertEqual(water_request.system_prompt, "persona")

    def test_revision_hint_only_when_the_topic_is_records(self):
        """对方提到「记录/记错」时加重提示（首次实测她口头答应却没改 ✗ 之后的补救 ✓）。"""
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        self.module.save_states(plugin.path, {"version": 1, "bots": {"fire": {
            "100002": {"impression": "爱拆台的人", "reason": "老拿配置说事", "updated_at": 1},
        }}})
        plain = types.SimpleNamespace(system_prompt="persona", prompt="hello")
        asyncio.run(plugin.inject(Event("fire", "person", text="晚上好呀"), plain))
        self.assertNotIn("对方正在说", plain.prompt)      # 尾巴里本来就有「等于没改」，判据用定向那句 ✓
        asked = types.SimpleNamespace(system_prompt="persona", prompt="hello")
        asyncio.run(plugin.inject(
            Event("fire", "person", text="你把他那条记录记错了，改一下"), asked))
        self.assertIn("对方正在说", asked.prompt)
        self.assertIn("100002", asked.prompt)          # 把可选对象列出来，省得她猜 who ✓
        self.assertIn("爱拆台", asked.prompt)

    def test_named_revision_updates_the_named_person(self):
        """印象修订：标签里带 who 时改的是**别人**那条（私聊里更正事实错误用 ✓）。"""
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        first = types.SimpleNamespace(completion_text=(
            '略\n<impression_update>{"impression":"爱拆台的人",'
            '"reason":"老拿配置说事"}</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "100002"), first))
        second = types.SimpleNamespace(completion_text=(
            '略\n<impression_update>{"who":"100002","impression":"其实挺憨厚",'
            '"reason":"他是在提建议，而且先被开玩笑的是他家的 AI"}</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "100003"), second))
        st = self.module.load_states(plugin.path)["bots"]["fire"]
        self.assertEqual(st["100002"]["impression"], "其实挺憨厚")
        self.assertNotIn("100003", st)   # 指名改别人时，不许顺手给当前发言者写一条 ✗

    def test_resolve_who_rules(self):
        people = {"100001": {"name": "鲸鱼", "impression": "Agoni：爱接梗"},
                  "100002": {"impression": "神秘二维码—爱琢磨配置"}}
        f = self.module.resolve_who
        self.assertEqual(f(people, "", "self"), "self")
        self.assertEqual(f(people, "null", "self"), "self")
        self.assertEqual(f(people, "2166832487", "self"), "2166832487")   # 纯数字 = QQ 号
        self.assertEqual(f(people, "鲸鱼", "self"), "100001")             # 按存下的 name
        self.assertEqual(f(people, "Agoni", "self"), "100001")            # 按印象开头的称呼
        self.assertEqual(f(people, "神秘二维码", "self"), "100002")
        self.assertIsNone(f(people, "查无此人", "self"))                  # 认不出 → 拒收 ✓

    def test_no_change_and_malformed_tag(self):
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        response = types.SimpleNamespace(completion_text="普通回复")
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertFalse(os.path.exists(plugin.path))
        response.completion_text = ('普通回复<impression_update>'
            '{"impression":null,"reason":null}</impression_update>')
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "普通回复")
        self.assertFalse(os.path.exists(plugin.path))
        response.completion_text = "回复<impression_update>{bad}</impression_update>"
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "回复")
        self.assertFalse(os.path.exists(plugin.path))
        response.completion_text = "回复<impression_update>{bad}"
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "回复")

    def test_clear_old_impression_without_affecting_other_people(self):
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        self.module.save_states(plugin.path, {"version": 1, "bots": {
            "fire": {
                "person": {"impression": "旧评价", "reason": "旧事", "updated_at": 1},
                "other": {"impression": "另一个人", "reason": "别的事", "updated_at": 1},
            },
            "water": {"person": {"impression": "另一位 bot 的记录", "reason": "独立", "updated_at": 1}},
        }})
        response = types.SimpleNamespace(completion_text=(
            '照常回复<impression_update>{"impression":null,"reason":null,'
            '"clear":true}</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "照常回复")
        states = self.module.load_states(plugin.path)["bots"]
        self.assertNotIn("person", states["fire"])
        self.assertIn("other", states["fire"])
        self.assertIn("person", states["water"])
        request = types.SimpleNamespace(system_prompt="persona", prompt="hi")
        asyncio.run(plugin.inject(Event("fire", "person"), request))
        self.assertIn("暂无", request.prompt)

        response.completion_text = (
            '回复<impression_update>{"impression":"新看法","reason":"新的可见互动"}'
            '</impression_update>')
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(self.module.load_states(plugin.path)["bots"]["fire"]["person"]["impression"],
                         "新看法")
        # clear 与正文同时出现：改成**以正文为准** ✓（她实测就这么写 ✗；
        # 原先拒收会让她的更新白白丢掉 —— 2026-10-06 改）
        response.completion_text = (
            '回复<impression_update>{"impression":"改后的看法","reason":"新的事实",'
            '"clear":true}</impression_update>')
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(self.module.load_states(plugin.path)["bots"]["fire"]["person"]["impression"],
                         "改后的看法")

    def test_named_clear_targets_the_named_person(self):
        """指名清除：clear 必须带 who，否则会去清**当前发言者**的条目 ✗（2026-10-06 修的 bug）。"""
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        self.module.save_states(plugin.path, {"version": 1, "bots": {"fire": {
            "100002": {"impression": "爱拆台的人", "reason": "x", "updated_at": 1},
            "100009": {"impression": "说话的人", "reason": "y", "updated_at": 1},
        }}})
        response = types.SimpleNamespace(completion_text=(
            '好\n<impression_update>{"who":"100002","clear":"true",'
            '"impression":"null","reason":null}</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "100009"), response))
        st = self.module.load_states(plugin.path)["bots"]["fire"]
        self.assertNotIn("100002", st)      # 被指名的那条清了 ✓
        self.assertIn("100009", st)         # 当前发言者**不许**被误清 ✓

    def test_clear_accepts_multiple_ids_and_a_reason(self):
        """她实测的写法：who 里塞两个号 + clear + 带 reason 说明原因 ✓ 必须收下。"""
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        self.module.save_states(plugin.path, {"version": 1, "bots": {"fire": {
            "100002": {"impression": "a", "reason": "x", "updated_at": 1},
            "100003": {"impression": "b", "reason": "y", "updated_at": 1},
            "100009": {"impression": "当前发言者", "reason": "z", "updated_at": 1},
        }}})
        response = types.SimpleNamespace(completion_text=(
            '好\n<impression_update>{"who":"100002,100003","clear":true,'
            '"impression":null,"reason":"水梦当场更正事实，那两笔不成立了"}</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "100009"), response))
        st = self.module.load_states(plugin.path)["bots"]["fire"]
        self.assertNotIn("100002", st)
        self.assertNotIn("100003", st)
        self.assertIn("100009", st)          # 当前发言者仍不许被误清 ✓

    def test_loose_tag_forms_are_stripped_and_parsed(self):
        """她偶尔省掉外壳：末尾裸 JSON / impression: 两行 —— 都要删掉并认出来。"""
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        # ① 末尾裸 JSON
        response = types.SimpleNamespace(completion_text=(
            '回复~\n{"impression":"爱较真","reason":"这轮反复纠正同一个说法"}'))
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "回复~")
        self.assertEqual(
            self.module.load_states(plugin.path)["bots"]["fire"]["person"]["impression"], "爱较真")
        # ② 末尾 impression: / reason: 两行（线上真出现过这个形态）
        response.completion_text = ('嗯，建：\n\nimpression: "爱熬夜"\nreason: "凌晨两点还在问配队"')
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "嗯，建：")
        self.assertEqual(
            self.module.load_states(plugin.path)["bots"]["fire"]["person"]["impression"], "爱熬夜")
        # ③ 只有 impression 没有 reason → 正文里删掉，但不采纳（不覆盖上一条）
        response.completion_text = '回复\nimpression: "半条"'
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(response.completion_text, "回复")
        self.assertEqual(
            self.module.load_states(plugin.path)["bots"]["fire"]["person"]["impression"], "爱熬夜")
        # ④ 历史清洗同样认宽松形态
        part = self.module.TextPart('回复\nimpression: "爱熬夜"\nreason: "凌晨两点还在问配队"')
        run_context = types.SimpleNamespace(messages=[
            types.SimpleNamespace(role="assistant", content=[part])])
        asyncio.run(plugin.scrub_history(Event("fire", "person"), run_context, response))
        self.assertEqual(part.text, "回复")

    def test_current_impression_is_temporary_context(self):
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        request = types.SimpleNamespace(
            system_prompt="persona", prompt="hello", extra_user_content_parts=[])
        asyncio.run(plugin.inject(Event("fire", "person"), request))
        self.assertEqual(request.prompt, "hello")
        self.assertEqual(len(request.extra_user_content_parts), 1)
        self.assertTrue(request.extra_user_content_parts[0].temporary)
        self.assertFalse(os.path.exists(plugin.path))

    def test_bad_state_file_is_preserved(self):
        plugin = self.module.Main(None, {"enabled": True, "enabled_self_ids": "fire"})
        Path(plugin.path).write_text("{bad", encoding="utf-8")
        response = types.SimpleNamespace(completion_text=(
            '回复<impression_update>{"impression":"新印象","reason":"新事件"}'
            '</impression_update>'))
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(Path(plugin.path).read_text(encoding="utf-8"), "{bad")
        self.assertEqual(response.completion_text, "回复")


if __name__ == "__main__":
    unittest.main()
