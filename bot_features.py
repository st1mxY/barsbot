"""Личные команды и статистика; без сетевых запросов и настроек авторизации."""
from __future__ import annotations

import html
import json
import math
import re
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))
SUBJECTS = (("Метрология", "метрол"), ("Схемотехника", "схемот"),
            ("Базы данных", "баз"))
LESSON_TOTALS = (32, 48, 24)


def now_moscow():
    return datetime.now(MOSCOW)


def parse_date(value: str) -> str:
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError("Дата должна быть в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД.")


def semester_start(semester: str):
    match = re.search(r"(\d{4})\s*/\s*(\d{4})", semester)
    if not match:
        return None
    if "осен" in semester.lower():
        return f"{match[1]}-09-01"
    if "весен" in semester.lower():
        return f"{match[2]}-02-01"
    return None


def lesson_date(desc: str):
    match = re.match(r"(\d{2}\.\d{2}\.\d{2,4}),", desc)
    if not match:
        return None
    try:
        return datetime.strptime(match[1], "%d.%m.%y" if len(match[1]) == 8
                                 else "%d.%m.%Y").date().isoformat()
    except ValueError:
        return None


def subject_index(value: str) -> int:
    value = value.lower().strip()
    for i, (name, stem) in enumerate(SUBJECTS):
        if value == str(i + 1) or stem in value:
            return i
    raise ValueError("Предмет: 1 — Метрология, 2 — Схемотехника, 3 — Базы данных.")


def weighted_progress(rows: dict, weights: dict, target: float = 4.2) -> str:
    labels = [label for label in rows if re.match(r"\d+\.", label)]
    if not labels or any(label not in weights for label in labels):
        return "Для расчёта нужны веса КМ; /check — загрузить их из БАРС."
    total = sum(Decimal(str(weights[label])) for label in labels)
    if total != 100 or any(weights[label] <= 0 for label in labels):
        return "Веса КМ не составляют 100%; точный прогноз недоступен."
    earned = Decimal(0)
    completed = Decimal(0)
    for label in labels:
        value = rows[label].strip()
        if not value:
            continue
        match = re.match(r"^([0-5](?:[.,]\d+)?)(?:\s|$)", value)
        if not match or not 0 <= Decimal(match[1].replace(",", ".")) <= 5:
            return "Есть КМ с нечисловой оценкой; точный прогноз недоступен."
        weight = Decimal(str(weights[label]))
        earned += Decimal(match[1].replace(",", ".")) * weight
        completed += weight
    rest = total - completed
    contribution = earned / total
    prefix = f"Закрыто {completed:g}% веса КМ. Набрано {contribution:.2f} из требуемых {target:g} итогового балла. "
    if not rest:
        return prefix + ("Порог по оценкам достигнут." if contribution >= Decimal(str(target))
                         else "Для автомата нужно улучшить уже выставленные оценки.")
    needed = (Decimal(str(target)) * total - earned) / rest
    if needed > 5:
        return prefix + "Даже пятёрок за оставшиеся КМ недостаточно — нужно пересдать уже оценённые."
    needed = max(Decimal(3), needed).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    return prefix + f"На оставшихся {rest:g}% нужен взвешенный средний ≥ {needed} (при сдаче всех КМ минимум на 3)."


def required_average(rows, weights, target=4.2):
    labels = [label for label in rows if re.match(r"\d+\.", label)]
    if not labels or any(label not in weights or weights[label] <= 0 for label in labels):
        return "?"
    if sum(Decimal(str(weights[label])) for label in labels) != 100:
        return "?"
    earned = Decimal(0)
    completed = Decimal(0)
    for label in labels:
        value = rows[label].strip()
        if not value:
            continue
        match = re.match(r"^([0-5](?:[.,]\d+)?)(?:\s|$)", value)
        if not match or not 0 <= Decimal(match[1].replace(",", ".")) <= 5:
            return "?"
        weight = Decimal(str(weights[label]))
        earned += Decimal(match[1].replace(",", ".")) * weight
        completed += weight
    remaining = 100 - completed
    if not remaining:
        return "✓" if earned >= Decimal(str(target)) * 100 else "Пересдать"
    needed = (Decimal(str(target)) * 100 - earned) / remaining
    if needed > 5:
        return "Пересдать"
    return str(max(Decimal(3), needed).quantize(Decimal("0.01"), rounding=ROUND_CEILING)).replace(".", ",")


HELP = """/subjects — таблица важных предметов и пропусков
/auto — условия автомата и настройка
/pe — отдельный журнал пропусков физры
/todo — список дел и команды
/check — проверить БАРС
/status — последняя проверка"""


class PersonalFeatures:
    def __init__(self, path: Path, daily_time: str = "21:00"):
        datetime.strptime(daily_time, "%H:%M")
        self.path = path
        self.daily_time = daily_time
        self.lock = threading.RLock()
        self.data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def record_check(self, changed: bool, baseline: bool = False, now=None):
        now = now or now_moscow()
        with self.lock:
            day = self.data.setdefault("days", {}).setdefault(now.date().isoformat(), {})
            day["checks"] = day.get("checks", 0) + 1
            day["changed"] = day.get("changed", False) or changed
            day["baseline"] = day.get("baseline", False) or baseline
            self.data["last_success"] = now.isoformat()
            # Для ежедневного итога достаточно последнего месяца.
            cutoff = (now.date() - timedelta(days=31)).isoformat()
            self.data["days"] = {k: v for k, v in self.data["days"].items() if k >= cutoff}
            self.save()

    def daily_report(self, send, healthy=True, now=None):
        now = now or now_moscow()
        with self.lock:
            if now.strftime("%H:%M") < self.daily_time or not healthy:
                return
            day = self.data.get("days", {}).get(now.date().isoformat(), {})
            if not day.get("checks") or day.get("changed") or day.get("sent"):
                return
            first = " Слежение началось сегодня; сравнение — с первым снимком." if day.get("baseline") else ""
            text = (f"🌙 За {now:%d.%m.%Y} изменений оценок и пропусков не обнаружено."
                    f"\nУспешных проверок: {day['checks']}. Последняя: "
                    f"{datetime.fromisoformat(self.data['last_success']):%H:%M} МСК." + first)
            if send(text) is not False:
                day["sent"] = True
                self.save()

    def subjects(self, snapshots: dict):
        marks = snapshots.get("Оценки", {})
        disciplines = marks.get("disciplines", {})
        absences = snapshots.get("Пропуски")
        start = semester_start(marks.get("semester", ""))
        end = None
        if start:
            year = int(start[:4])
            end = f"{year + 1}-02-01" if start.endswith("09-01") else f"{year}-09-01"
        columns = []
        km_numbers = set()
        for i, (name, stem) in enumerate(SUBJECTS):
            title = next((t for t in disciplines if stem in t.lower()), None)
            rows = disciplines.get(title, {})
            values = {}
            for label, value in rows.items():
                match = re.match(r"(\d+)\.", label)
                if match:
                    number = int(match[1])
                    km_numbers.add(number)
                    grade = re.match(r"^([0-5](?:[.,]\d+)?)(?:\s|$)", value.strip())
                    values[number] = grade[1] if grade else (f"КМ{number}" if not value.strip() else "?")
            count = excused = 0
            unknown = False
            for item in (absences or {}).get("items", {}).values():
                parts = re.split(r",\s+(?![^()]*\))", item.get("desc", ""))
                if len(parts) < 4 or stem not in parts[3].lower():
                    continue
                date = lesson_date(item["desc"])
                if not date:
                    unknown = True
                elif start and start <= date < end:
                    count += 1
                    excused += int("уважит" in item.get("status", "").lower())
            known = start and absences is not None and not unknown
            target = self.data.get("targets", {}).get(str(i), {})
            weights = marks.get("weights", {}).get(title, {})
            forecast = required_average(rows, weights, target.get("score", 4.2))
            columns.append({
                "kms": values,
                "Балл": rows.get("Балл текущего контроля") or "—",
                "Для авто": forecast,
                "Без уваж.": str(count - excused) if known else "?",
                "Лимит": str(math.floor(target.get("lessons", LESSON_TOTALS[i]) * .0625)),
            })
        headers = ("", "Метрология", "Схемотех.", "БД")
        table = [headers]
        for number in sorted(km_numbers):
            table.append((f"КМ{number}", *(c["kms"].get(number, "—") for c in columns)))
        for label in ("Балл", "Для авто", "Без уваж.", "Лимит"):
            table.append((label, *(c[label] for c in columns)))
        widths = [max(len(row[i]) for row in table) for i in range(4)]
        def line(row):
            return " │ ".join(cell.ljust(widths[i]) if i == 0 else cell.center(widths[i])
                              for i, cell in enumerate(row))
        divider = "─┼─".join("─" * width for width in widths)
        lines = [line(table[0]), divider]
        for row in table[1:]:
            if row[0] == "Балл":
                lines.append(divider)
            lines.append(line(row))
        return "<pre>" + html.escape("\n".join(lines)) + "</pre>"

    def command(self, text: str, snapshots: dict):
        with self.lock:
            cmd, _, arg = text.partition(" ")
            arg = arg.strip()
            try:
                if cmd == "/subjects":
                    return self.subjects(snapshots)
                if cmd == "/auto":
                    if not arg:
                        rules = []
                        for i, (name, _) in enumerate(SUBJECTS):
                            rule = self.data.get("targets", {}).get(str(i), {})
                            total = rule.get("lessons", LESSON_TOTALS[i])
                            rules.append(f"{i+1}. {name}: ≥ {rule.get('score', 4.2):g}, занятий {total}, допустимо {math.floor(total * .0625)} неуважительных пропусков.")
                        return ("\n".join(rules) + "\n\nИзменить: /auto номер балл [всего занятий]"
                                + "\nПример: <code>/auto 2 4.2 48</code>\nЛимит — 6,25% занятий. Веса КМ читаются из БАРС.")
                    parts = arg.split()
                    if len(parts) not in (2, 3):
                        raise ValueError("Формат: /auto номер балл [всего занятий]")
                    idx = subject_index(parts[0])
                    score = float(parts[1].replace(",", "."))
                    if not 0 < score <= 5:
                        raise ValueError("Целевой средний балл должен быть больше 0 и не больше 5.")
                    rule = dict(self.data.get("targets", {}).get(str(idx), {}))
                    rule["score"] = score
                    if len(parts) == 3:
                        total = int(parts[2])
                        if not 1 <= total <= 1000:
                            raise ValueError("Количество занятий: от 1 до 1000.")
                        rule["lessons"] = total
                    self.data.setdefault("targets", {})[str(idx)] = rule
                    self.save()
                    return f"Условия для «{SUBJECTS[idx][0]}» сохранены. /subjects — сводка."
                if cmd == "/pe":
                    return self.pe(arg)
                if cmd == "/todo":
                    return self.todo(arg)
            except (ValueError, OverflowError) as exc:
                # Сообщение исключения может содержать введённый пользователем текст.
                return "❗ " + html.escape(str(exc))
        return None

    def next_id(self, key):
        value = self.data.get(key, 0) + 1
        self.data[key] = value
        return value

    def create_task(self, title, due=None):
        with self.lock:
            title = title.strip()
            if not title or len(title) > 500:
                raise ValueError("Текст дела: от 1 до 500 символов.")
            deadline = parse_date(due) if due else None
            task = {"id": self.next_id("task_id"), "title": title, "due": deadline, "done": False}
            self.data.setdefault("tasks", []).append(task)
            self.save()
            return task

    def pe(self, arg):
        entries = self.data.setdefault("pe", [])
        parts = arg.split()
        if parts and parts[0] == "add":
            if len(parts) not in (2, 3):
                raise ValueError("Формат: /pe add ДД.ММ.ГГГГ [количество пар]")
            date = parse_date(parts[1])
            count = int(parts[2]) if len(parts) == 3 else 1
            if not 1 <= count <= 100:
                raise ValueError("Количество пар: от 1 до 100.")
            entry = {"id": self.next_id("pe_id"), "date": date, "count": count}
            entries.append(entry)
            self.save()
            return f"Пропуск физры #{entry['id']} добавлен: {date}, пар: {count}."
        if parts and parts[0] == "del":
            if len(parts) != 2:
                raise ValueError("Формат: /pe del номер")
            entry = next((e for e in entries if e["id"] == int(parts[1])), None)
            if entry is None:
                raise ValueError("Запись не найдена.")
            entries.remove(entry)
            self.save()
            return "Запись физры удалена."
        if arg and arg != "list":
            raise ValueError("Команды: /pe, /pe add ДД.ММ.ГГГГ [пар], /pe del номер")
        lines = [f"🏃 <b>Физра — ручной журнал</b>\nВсего пропущенных пар: {sum(e['count'] for e in entries)}"]
        for e in sorted(entries, key=lambda e: (e["date"], e["id"])):
            lines.append(f"#{e['id']} · {e['date']} · пар: {e['count']}")
        return "\n".join(lines) + "\n\nДобавить: <code>/pe add 02.11.2026</code>\nУдалить: /pe del номер\nЖурнал ведётся отдельно от БАРС; записи не складываются с его пропусками."

    def todo(self, arg):
        tasks = self.data.setdefault("tasks", [])
        action, _, rest = arg.partition(" ")
        rest = rest.strip()
        if action == "add":
            title, sep, due = rest.partition("|")
            task = self.create_task(title, parse_date(due) if sep else None)
            deadline = task["due"]
            return f"Дело #{task['id']} добавлено" + (f", дедлайн {deadline}." if deadline else ".")
        if action in ("del", "done", "open", "deadline"):
            parts = rest.split()
            if len(parts) != (2 if action == "deadline" else 1):
                raise ValueError(f"Формат: /todo {action} номер" + (" ДД.ММ.ГГГГ (или - для снятия)" if action == "deadline" else ""))
            task = next((t for t in tasks if t["id"] == int(parts[0])), None)
            if task is None:
                raise ValueError("Дело не найдено.")
            if action == "del":
                tasks.remove(task)
            elif action == "deadline":
                task["due"] = None if parts[1] == "-" else parse_date(parts[1])
            else:
                task["done"] = action == "done"
            self.save()
            return "Дело удалено." if action == "del" else "Дело обновлено."
        if arg not in ("", "list", "all"):
            raise ValueError("Команды: /todo, /todo add текст | ДД.ММ.ГГГГ, /todo done номер, /todo del номер, /todo deadline номер дата")
        today = now_moscow().date().isoformat()
        selected = [t for t in tasks if arg == "all" or not t["done"]]
        lines = ["📝 <b>Дела</b>"]
        for t in sorted(selected, key=lambda t: (t["done"], t["due"] or "9999", t["id"])):
            due = f" · до {t['due']}" if t["due"] else ""
            late = " ⏰ просрочено" if t["due"] and t["due"] < today and not t["done"] else ""
            lines.append(f"{'✅' if t['done'] else '▫️'} #{t['id']} {html.escape(t['title'])}{due}{late}")
        if not selected:
            lines.append("Открытых дел нет." if arg != "all" else "Дел пока нет.")
        return "\n".join(lines) + ("\n\nДобавить: <code>/todo add Сдать лабораторную | 10.11.2026</code>"
            "\nДедлайн необязателен.\n/todo done номер — выполнить\n/todo open номер — вернуть"
            "\n/todo del номер — удалить\n/todo deadline номер дата — изменить дедлайн"
            "\n/todo deadline номер - — снять дедлайн\n/todo all — включая выполненные")
