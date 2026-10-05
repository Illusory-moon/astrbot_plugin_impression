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
    def __init__(self, bot, sender, cron=False):
        self.bot = bot
        self.sender = sender
        self.cron = cron

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
        response.completion_text = (
            '回复<impression_update>{"impression":"不该写入","reason":"不该写入",'
            '"clear":true}</impression_update>')
        asyncio.run(plugin.record(Event("fire", "person"), response))
        self.assertEqual(self.module.load_states(plugin.path)["bots"]["fire"]["person"]["impression"],
                         "新看法")

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
