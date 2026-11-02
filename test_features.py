import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import bars_bot
from bot_features import MOSCOW, PersonalFeatures, weighted_progress


class FeaturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".test-features-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "personal.json"
        self.f = PersonalFeatures(self.path)

    def at(self, day=2, hour=21):
        return datetime(2026, 11, day, hour, tzinfo=MOSCOW)

    def test_daily_once_and_restart(self):
        self.f.record_check(False, now=self.at(hour=20))
        sent = []
        self.f.daily_report(sent.append, now=self.at(hour=20))
        self.assertEqual(sent, [])
        self.f.daily_report(sent.append, now=self.at())
        PersonalFeatures(self.path).daily_report(sent.append, now=self.at(hour=22))
        self.assertEqual(len(sent), 1)

    def test_daily_suppresses_changes_errors_and_missing_checks(self):
        sent = []
        self.f.daily_report(sent.append, now=self.at())
        self.f.record_check(True, now=self.at(hour=10))
        self.f.record_check(False, now=self.at(hour=20))
        self.f.daily_report(sent.append, now=self.at())
        self.f.record_check(False, now=self.at(day=3))
        self.f.daily_report(sent.append, healthy=False, now=self.at(day=3))
        self.assertEqual(sent, [])
        self.f.daily_report(sent.append, now=self.at(day=3))
        self.assertEqual(len(sent), 1)

    def test_failed_daily_delivery_retries(self):
        self.f.record_check(False, now=self.at())
        self.f.daily_report(lambda _: False, now=self.at())
        sent = []
        self.f.daily_report(sent.append, now=self.at())
        self.assertEqual(len(sent), 1)

    def test_weighted_prediction(self):
        rows = {"1. Работа": "4 (09.10.26)", "2. Работа": ""}
        weights = {"1. Работа": 30, "2. Работа": 70}
        self.assertIn("4.29", weighted_progress(rows, weights))
        rows["1. Работа"] = "5 (09.10.26)"
        weights = {"1. Работа": 45, "2. Работа": 55}
        self.assertIn("3.55", weighted_progress(rows, weights))
        rows["1. Работа"] = "2"
        weights = {"1. Работа": 80, "2. Работа": 20}
        self.assertIn("недостаточно", weighted_progress(rows, weights))
        self.assertIn("нужны веса", weighted_progress(rows, {}))

    def test_subject_counts_semester_and_excused(self):
        names = ("Метрология", "Схемотехника", "Базы данных")
        marks = {"semester": "2026/2027, Осенний семестр", "disciplines": {n: {} for n in names}}
        items = {}
        for i, (date, status) in enumerate((("01.10.26", "пропуск"),
                                           ("02.10.26", "пропуск по уважительной причине"),
                                           ("01.03.26", "пропуск"))):
            items[str(i)] = {"desc": f"{date}, 1 пара, лекция, Метрология (экзамен), Группа", "status": status}
        report = self.f.command("/subjects", {"Оценки": marks, "Пропуски": {"items": items}})
        table = {line.split("│")[0].strip(): [cell.strip() for cell in line.split("│")[1:]]
                 for line in report.removeprefix("<pre>").removesuffix("</pre>").splitlines() if "│" in line}
        self.assertNotIn("Пропуски", table)
        self.assertEqual(table["Без уваж."], ["1", "0", "0"])
        self.assertEqual(table["Лимит"], ["2", "3", "1"])
        self.assertTrue(report.startswith("<pre>"))
        self.assertTrue(report.endswith("</pre>"))

    def test_tasks_and_pe_survive_restart(self):
        self.f.command("/todo add Сдать & защитить | 10.11.2026", {})
        self.f.command("/pe add 02.11.2026 2", {})
        f = PersonalFeatures(self.path)
        self.assertIn("Сдать &amp; защитить", f.command("/todo", {}))
        self.assertIn("2026-11-10", f.command("/todo", {}))
        self.assertIn("Всего пропущенных пар: 2", f.command("/pe", {}))
        f.command("/todo deadline 1 -", {})
        self.assertNotIn("2026-11-10", f.command("/todo", {}))
        f.command("/todo done 1", {})
        self.assertIn("Открытых дел нет", f.command("/todo", {}))
        f.command("/todo open 1", {})
        self.assertIn("Сдать", f.command("/todo", {}))
        f.command("/todo del 1", {})
        f.command("/pe del 1", {})
        self.assertEqual(f.data["tasks"], [])
        self.assertEqual(f.data["pe"], [])

    def test_invalid_input_does_not_mutate(self):
        for cmd in ("/todo add X | 31.02.2026", "/pe add 02.11.2026 -1", "/auto 1 nan", "/auto 3 4.2 0"):
            self.assertTrue(self.f.command(cmd, {}).startswith("❗"))
        self.assertFalse(self.f.data.get("tasks"))
        self.assertFalse(self.f.data.get("pe"))
        self.assertFalse(self.f.data.get("targets"))

    def test_parse_weights_and_no_migration_notifications(self):
        page = '''<select id="ddl_StudyFilterSemester"><option selected>2026/2027, Осенний семестр</option></select>
        <div data-bs-target="#s_ss_1">Базы данных</div><div id="s_ss_1"><table>
        <tr><td>1. Работа</td><td>30</td><td>4</td><td>4 (09.10.26)</td></tr>
        <tr><td>2. Работа</td><td>70</td><td>8</td><td></td></tr></table></div>'''
        snap = bars_bot.parse_marks(page)
        self.assertEqual(snap["weights"]["Базы данных"]["1. Работа"], 30)
        old = copy.deepcopy(snap)
        del old["weights"]
        self.assertEqual(bars_bot.diff_marks(old, snap), [])
        table = self.f.subjects({"Оценки": snap})
        self.assertIn("КМ2", table)
        self.assertIn("4,29", table)
        self.assertNotIn("Работа", table)
        self.assertNotIn("Осталось сдать", table)

    def test_failed_second_page_does_not_consume_first_page_changes(self):
        class FakeTelegram:
            send = lambda *a, **kw: True
        with patch.object(bars_bot, "PERSONAL_FILE", self.path), patch.object(bars_bot, "load_state", return_value={}):
            watcher = bars_bot.Watcher(FakeTelegram())
        watcher.state = {"snapshots": {"Оценки": {"old": True}}}
        before = copy.deepcopy(watcher.state)
        watcher.bars.landing_html = "ready"
        watcher.bars.discover = lambda: {"Оценки": "marks", "Пропуски": "absences"}
        watcher.bars.fetch = lambda url: url
        def fail(_):
            raise bars_bot.BarsError("test failure")
        with patch.dict(bars_bot.PARSERS, {"Оценки": (lambda _: {"new": True}, lambda *_: ["change"]), "Пропуски": (fail, None)}):
            watcher.check()
        self.assertEqual(watcher.state, before)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
