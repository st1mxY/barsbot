import queue
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bars_bot import Telegram, commands_loop
from bot_features import PersonalFeatures
from bot_ui import BotUI, MAIN_MENU


class FakeTelegram:
    chat_id = "42"

    def __init__(self):
        self.messages = []
        self.answers = []
        self.edits = []
        self.last_message_id = None
        self.send_count = 0

    def send(self, text, chat_id=None, reply_markup=MAIN_MENU):
        self.messages.append((text, reply_markup))
        self.send_count += 1
        self.last_message_id = self.send_count
        return True

    def edit_message(self, ident, text, reply_markup=MAIN_MENU):
        self.edits.append((ident, text, reply_markup))
        self.messages.append((text, reply_markup))
        return True

    def answer_callback(self, ident, text=""):
        self.answers.append((ident, text))


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-ui-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.f = PersonalFeatures(Path(self.tmp.name) / "personal.json")
        self.tg = FakeTelegram()
        self.w = SimpleNamespace(features=self.f, state_lock=threading.Lock(), state={},
                                 lock=threading.Lock(), awaiting_code=threading.Event(),
                                 codes=queue.Queue(), last_check="ещё не было", check=Mock())
        self.ui = BotUI(self.w, self.tg, lambda: "Статус проверки")

    def click(self, data, sender=42):
        self.ui.handle_callback({"id": "callback", "from": {"id": sender},
                                 "message": {"chat": {"id": 42}, "message_id": 17}, "data": data})

    def test_pe_add_total_delete_and_repeat_click(self):
        self.click("pe:add")
        self.ui.handle_text("не дата")
        self.assertEqual(self.ui.pending["kind"], "pe_date")
        self.ui.handle_text("02.11.2026")
        self.click("pe:count:2")
        self.assertIn("Всего пропущенных пар: <b>2</b>", self.tg.messages[-1][0])
        self.click("pe:count:2")
        self.assertEqual(len(self.f.data["pe"]), 1)
        self.click("pe:confirm:1")
        self.assertEqual(len(self.f.data["pe"]), 1)
        self.click("pe:remove:1")
        self.assertEqual(self.f.data["pe"], [])

    def test_task_flow_and_literal_text(self):
        self.click("task:add")
        self.ui.handle_text("Разобрать A | B & C")
        self.ui.handle_text("31.02.2026")
        self.assertFalse(self.f.data.get("tasks"))
        self.ui.handle_text("10.11.2026")
        self.assertEqual(self.f.data["tasks"][0]["title"], "Разобрать A | B & C")
        self.assertIn("A | B &amp; C", self.tg.messages[-1][0])
        self.click("task:done:1")
        self.assertTrue(self.f.data["tasks"][0]["done"])
        self.click("task:open:1")
        self.assertFalse(self.f.data["tasks"][0]["done"])
        self.click("task:deadline:1")
        self.click("task:cleardue")
        self.assertIsNone(self.f.data["tasks"][0]["due"])
        self.click("task:confirm:1")
        self.assertEqual(len(self.f.data["tasks"]), 1)
        self.click("task:del:1")
        self.assertEqual(self.f.data["tasks"], [])

    def test_no_deadline_cancel_and_expiry(self):
        self.click("task:add")
        self.ui.handle_text("Дело")
        self.click("task:nodue")
        self.assertIsNone(self.f.data["tasks"][0]["due"])
        self.click("pe:add")
        self.click("cancel")
        self.assertIsNone(self.ui.pending)
        self.click("pe:add")
        self.ui.pending["time"] -= 901
        self.click("pe:today")
        self.assertFalse(self.f.data.get("pe"))

    def test_callback_owner_and_stale_items(self):
        self.click("pe:add", sender=99)
        self.assertIsNone(self.ui.pending)
        self.assertEqual(self.tg.messages, [])
        self.assertIn("личный", self.tg.answers[-1][1])
        self.click("task:view:999")
        self.assertIn("уже удалено", self.tg.messages[-1][0])

    def test_navigation_and_auto_settings(self):
        for route in ("home", "subjects", "absences", "auto", "pe", "tasks", "status"):
            self.click(route)
            self.assertIn("inline_keyboard", self.tg.messages[-1][1])
        self.click("auto:edit:1")
        self.ui.handle_text("nan")
        self.assertEqual(self.ui.pending["kind"], "auto_score")
        self.ui.handle_text("4,2")
        self.ui.handle_text("48")
        self.assertEqual(self.f.data["targets"]["1"], {"score": 4.2, "lessons": 48})

    def test_navigation_replaces_clicked_menu_instead_of_sending(self):
        self.click("home")
        self.click("absences")
        self.click("tasks")
        self.click("home")
        self.assertEqual(self.tg.send_count, 0)
        self.assertTrue(all(edit[0] == 17 for edit in self.tg.edits))
        self.assertIn("Пропуски БАРС", str(self.tg.edits[-1][2]))

    def test_edit_timeout_does_not_create_duplicate_but_deleted_menu_is_replaced(self):
        with patch.object(self.tg, "edit_message", return_value=None):
            self.click("home")
        self.assertEqual(self.tg.send_count, 0)
        with patch.object(self.tg, "edit_message", return_value=False):
            self.click("home")
        self.assertEqual(self.tg.send_count, 1)

    def test_pagination_and_callback_payload_limits(self):
        for i in range(8):
            self.f.create_task(f"Дело {i}")
        self.click("tasks")
        first = self.tg.messages[-1]
        self.assertIn("Дело 0", first[0])
        self.assertNotIn("Дело 6", first[0])
        self.click("tasks:page:1")
        self.assertIn("Дело 6", self.tg.messages[-1][0])
        for _, markup in self.tg.messages:
            for row in (markup or {}).get("inline_keyboard", []):
                for btn in row:
                    self.assertLessEqual(len(btn["callback_data"].encode()), 64)

    def test_keyboard_only_on_last_chunk(self):
        response = Mock(ok=True)
        response.json.return_value = {"ok": True}
        with patch("bars_bot.requests.Session.post", return_value=response) as post:
            self.assertTrue(Telegram("test", "42").send("a" * 2000 + "\n" + "b" * 2000))
        payloads = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual(len(payloads), 2)
        self.assertNotIn("reply_markup", payloads[0])
        self.assertEqual(payloads[1]["reply_markup"], MAIN_MENU)

    def test_polling_routes_callbacks_and_keeps_2fa_separate(self):
        stop = threading.Event()
        def updates(_):
            stop.set()
            return [
                {"update_id": 1, "callback_query": {"id": "x", "from": {"id": 42}, "message": {"chat": {"id": 42}}, "data": "pe:add"}},
                {"update_id": 2, "message": {"chat": {"id": 42}, "text": "123456"}},
            ]
        self.tg.updates = updates
        self.w.awaiting_code.set()
        commands_loop(self.w, self.tg, stop)
        self.assertEqual(self.w.codes.get_nowait(), "123456")
        self.assertFalse(self.f.data.get("pe"))
        self.assertEqual(self.tg.answers[0][0], "x")


if __name__ == "__main__":
    unittest.main()
