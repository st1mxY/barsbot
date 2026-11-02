"""Кнопки Telegram и пошаговый ввод для личного бота."""
from __future__ import annotations

import copy
import html
import threading
import time

from bot_features import SUBJECTS, now_moscow, parse_date


def button(text, data):
    return {"text": text, "callback_data": data}


def keyboard(*rows):
    return {"inline_keyboard": list(rows)}


HOME = [button("🏠 Главное меню", "home")]
CANCEL = keyboard([button("Отмена", "cancel")])
MAIN_MENU = keyboard(
    [button("📚 Важные предметы", "subjects"), button("🚫 Пропуски БАРС", "absences")],
    [button("🏃 Пропуски физры", "pe"), button("📝 Список дел", "tasks")],
    [button("🔄 Проверить БАРС", "check"), button("ℹ️ Статус", "status")],
)


class BotUI:
    PAGE_SIZE = 6
    DIALOG_TTL = 15 * 60

    def __init__(self, watcher, tg, status_text):
        self.w = watcher
        self.tg = tg
        self.features = watcher.features
        self.status_text = status_text
        self.pending = None
        self.message_id = None

    def send(self, text, reply_markup=MAIN_MENU):
        # Переходы, диалоги и подтверждения занимают одно сообщение.
        if self.message_id:
            edited = self.tg.edit_message(self.message_id, text, reply_markup=reply_markup)
            if edited is not False:
                return edited
        sent = self.tg.send(text, reply_markup=reply_markup)
        if sent:
            self.message_id = self.tg.last_message_id
        return sent

    def menu(self):
        self.send("Выбери раздел 👇", reply_markup=MAIN_MENU)

    def snapshots(self):
        with self.w.state_lock:
            return copy.deepcopy(self.w.state.get("snapshots", {}))

    def prompt(self, kind, text, extra=None, markup=CANCEL):
        self.pending = {"kind": kind, "time": time.monotonic(), **(extra or {})}
        self.send(text, reply_markup=markup)

    def data_list(self, key):
        with self.features.lock:
            return copy.deepcopy(self.features.data.get(key, []))

    def show_pe(self, page=0, deleting=False):
        entries = sorted(self.data_list("pe"), key=lambda e: (e["date"], e["id"]), reverse=True)
        page = max(0, min(page, max(0, (len(entries) - 1) // self.PAGE_SIZE)))
        text = ("🏃 <b>Пропуски физры — ручной журнал</b>"
                f"\nВсего пропущенных пар: <b>{sum(e['count'] for e in entries)}</b>"
                "\nУчитываются только записи, которые ты добавил сюда.")
        rows = []
        visible = entries[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]
        if not entries:
            text += "\n\nЗаписей пока нет."
        for e in visible:
            label = f"{e['date']} · пар: {e['count']} · #{e['id']}"
            text += "\n" + label
            if deleting:
                rows.append([button("🗑 " + label, f"pe:confirm:{e['id']}")])
        if deleting and entries:
            text += "\n\nВыбери запись для удаления."
        prefix = "pe:deletepage" if deleting else "pe:page"
        rows += self.page_buttons(prefix, page, len(entries))
        rows.append([button("➕ Добавить пропуск", "pe:add"), button("🗑 Удалить запись", "pe:delete")])
        rows.append([button("🔄 Обновить журнал", "pe")])
        rows.append(HOME)
        self.send(text, reply_markup=keyboard(*rows))

    def page_buttons(self, prefix, page, total):
        row = []
        if page > 0:
            row.append(button("◀️ Назад", f"{prefix}:{page-1}"))
        if (page + 1) * self.PAGE_SIZE < total:
            row.append(button("Далее ▶️", f"{prefix}:{page+1}"))
        return [row] if row else []

    def show_tasks(self, page=0, all_tasks=False):
        tasks = [t for t in self.data_list("tasks") if all_tasks or not t["done"]]
        tasks.sort(key=lambda t: (t["done"], t["due"] or "9999", t["id"]))
        page = max(0, min(page, max(0, (len(tasks) - 1) // self.PAGE_SIZE)))
        text = "📝 <b>Все дела</b>" if all_tasks else "📝 <b>Открытые дела</b>"
        text += f"\nВсего: {len(tasks)}. Нажми на дело, чтобы изменить его."
        rows = []
        today = now_moscow().date().isoformat()
        for t in tasks[page * self.PAGE_SIZE:(page + 1) * self.PAGE_SIZE]:
            due = f" · до {t['due']}" if t["due"] else ""
            late = " ⏰ просрочено" if t["due"] and t["due"] < today and not t["done"] else ""
            title = t["title"][:120] + ("…" if len(t["title"]) > 120 else "")
            text += f"\n\n{'✅' if t['done'] else '▫️'} #{t['id']} {html.escape(title)}{due}{late}"
            rows.append([button(f"#{t['id']} {t['title'][:45]}", f"task:view:{t['id']}")])
        if not tasks:
            text += "\n\nДел пока нет."
        rows += self.page_buttons("tasks:allpage" if all_tasks else "tasks:page", page, len(tasks))
        rows.append([button("➕ Добавить дело", "task:add"), button("📋 Все дела" if not all_tasks else "▫️ Открытые", "tasks:all" if not all_tasks else "tasks")])
        rows.append(HOME)
        self.send(text, reply_markup=keyboard(*rows))

    def task(self, ident):
        task = next((t for t in self.data_list("tasks") if t["id"] == ident), None)
        if task is None:
            raise ValueError("Дело уже удалено. Открой список заново.")
        return task

    def show_task(self, ident):
        task = self.task(ident)
        text = f"📝 <b>Дело #{ident}</b>\n{html.escape(task['title'])}"
        text += f"\nДедлайн: {task['due'] or 'не задан'}.\nСтатус: {'выполнено' if task['done'] else 'открыто'}."
        self.send(text, reply_markup=keyboard(
            [button("↩️ Вернуть в открытые" if task["done"] else "✅ Выполнено", f"task:{'open' if task['done'] else 'done'}:{ident}")],
            [button("📅 Изменить дедлайн", f"task:deadline:{ident}"), button("🗑 Удалить", f"task:confirm:{ident}")],
            [button("📝 К списку дел", "tasks")], HOME))

    def show_auto(self):
        text = self.features.command("/auto", {}).split("\n\nИзменить:")[0]
        self.send("🎯 <b>Условия автомата</b>\n" + text + "\n\nВыбери предмет для изменения условий.",
                     reply_markup=keyboard(*[[button(name, f"auto:edit:{i}")] for i, (name, _) in enumerate(SUBJECTS)], HOME))

    def handle_callback(self, callback):
        chat = str(callback.get("message", {}).get("chat", {}).get("id", ""))
        sender = str(callback.get("from", {}).get("id", ""))
        if not self.tg.chat_id or chat != self.tg.chat_id or sender != self.tg.chat_id:
            self.tg.answer_callback(callback.get("id", ""), "Это личный бот.")
            return
        self.tg.answer_callback(callback.get("id", ""))
        self.message_id = callback.get("message", {}).get("message_id")
        action = callback.get("data", "")
        try:
            self.action(action)
        except (ValueError, OverflowError) as exc:
            self.send("❗ " + html.escape(str(exc)), reply_markup=keyboard(HOME))

    def action(self, action):
        # Кнопки внутри диалога действуют только для текущего шага.
        if action in ("pe:today", "pe:count:1", "pe:count:2", "task:nodue", "task:cleardue"):
            expected = {"pe:today": "pe_date", "pe:count:1": "pe_count", "pe:count:2": "pe_count",
                        "task:nodue": "task_due", "task:cleardue": "task_deadline"}[action]
            if not self.pending or self.pending["kind"] != expected or self.expired():
                self.send("Этот шаг уже завершён. Открой раздел заново.")
                return
            values = {"pe:today": now_moscow().date().isoformat(), "pe:count:1": "1",
                      "pe:count:2": "2", "task:nodue": "-", "task:cleardue": "-"}
            self.handle_text(values[action])
            return
        self.pending = None
        if action in ("home", "cancel"):
            self.menu()
        elif action == "subjects":
            text = self.features.command("/subjects", self.snapshots())
            self.send(text, reply_markup=keyboard([button("🔄 Обновить сводку", "subjects"), button("🔄 Проверить БАРС", "check")], HOME))
        elif action == "absences":
            self.send(self.features.command("/absences", self.snapshots()), reply_markup=keyboard(
                [button("🔄 Обновить таблицу", "absences"), button("🔄 Проверить БАРС", "check")], HOME))
        elif action == "status":
            self.send(self.status_text(), reply_markup=MAIN_MENU)
        elif action == "check":
            if self.w.lock.locked():
                self.send("Проверка БАРС уже идёт. Результат придёт сюда.")
            else:
                self.send("Проверяю БАРС… Результат придёт сюда.")
                threading.Thread(target=self.w.check, kwargs={"manual": True}, daemon=True).start()
        elif action == "pe" or action == "pe:delete":
            self.show_pe(deleting=action == "pe:delete")
        elif action.startswith(("pe:page:", "pe:deletepage:")):
            self.show_pe(int(action.rsplit(":", 1)[1]), deleting=action.startswith("pe:deletepage:"))
        elif action == "pe:add":
            self.prompt("pe_date", "📅 За какую дату добавить пропуск физры?\nНапиши дату, например 02.11.2026, или нажми «Сегодня».",
                        markup=keyboard([button("Сегодня", "pe:today")], [button("Отмена", "cancel")]))
        elif action.startswith("pe:confirm:"):
            ident = int(action.rsplit(":", 1)[1])
            entry = next((e for e in self.data_list("pe") if e["id"] == ident), None)
            if entry is None:
                raise ValueError("Запись уже удалена.")
            self.send(f"Удалить запись #{ident} от {entry['date']} (пар: {entry['count']})?",
                         reply_markup=keyboard([button("🗑 Да, удалить", f"pe:remove:{ident}"), button("Отмена", "pe")]))
        elif action.startswith("pe:remove:"):
            result = self.features.command("/pe del " + action.rsplit(":", 1)[1], {})
            if result.startswith("❗"):
                raise ValueError("Запись уже удалена.")
            self.show_pe()
        elif action == "tasks" or action == "tasks:all":
            self.show_tasks(all_tasks=action == "tasks:all")
        elif action.startswith(("tasks:page:", "tasks:allpage:")):
            self.show_tasks(int(action.rsplit(":", 1)[1]), all_tasks=action.startswith("tasks:allpage:"))
        elif action == "task:add":
            self.prompt("task_title", "📝 Напиши, что нужно сделать (до 500 символов).")
        elif action.startswith("task:"):
            _, verb, raw_id = action.split(":")
            ident = int(raw_id)
            self.task(ident)
            if verb == "view":
                self.show_task(ident)
            elif verb in ("done", "open", "del"):
                self.features.command(f"/todo {verb} {ident}", {})
                self.show_tasks() if verb == "del" else self.show_task(ident)
            elif verb == "deadline":
                self.prompt("task_deadline", "📅 Напиши новый дедлайн, например 10.11.2026, или нажми «Снять дедлайн».",
                            {"id": ident}, keyboard([button("Снять дедлайн", "task:cleardue")], [button("Отмена", "cancel")]))
            elif verb == "confirm":
                self.send(f"Удалить дело #{ident}?", reply_markup=keyboard([button("🗑 Да, удалить", f"task:del:{ident}"), button("Отмена", f"task:view:{ident}")]))
            else:
                raise ValueError("Неизвестное действие. Открой меню заново.")
        elif action == "auto":
            self.show_auto()
        elif action.startswith("auto:edit:"):
            idx = int(action.rsplit(":", 1)[1])
            if idx not in range(len(SUBJECTS)):
                raise ValueError("Предмет не найден.")
            self.prompt("auto_score", f"🎯 {SUBJECTS[idx][0]}: какой средний балл нужен для автомата? Например, 4.2.", {"idx": idx})
        else:
            self.menu()

    def expired(self):
        return self.pending and time.monotonic() - self.pending["time"] > self.DIALOG_TTL

    def handle_text(self, text):
        routes = {"/start": "home", "/help": "home", "/menu": "home", "/cancel": "cancel",
                  "/subjects": "subjects", "/absences": "absences", "/pe": "pe", "/todo": "tasks", "/auto": "auto",
                  "/status": "status", "/check": "check"}
        if text in routes:
            self.action(routes[text])
            return True
        if text.startswith("/"):
            self.pending = None
            return False  # Старые команды с аргументами тоже поддерживаются.
        if not self.pending:
            return False
        if self.expired():
            self.pending = None
            self.send("Время ввода истекло. Открой нужный раздел заново.")
            return True
        state = self.pending
        try:
            kind = state["kind"]
            if kind == "pe_date":
                date = parse_date(text)
                self.prompt("pe_count", "Сколько пар пропущено? Нажми кнопку или напиши число от 1 до 100.",
                            {"date": date}, keyboard([button("1 пара", "pe:count:1"), button("2 пары", "pe:count:2")], [button("Отмена", "cancel")]))
            elif kind == "pe_count":
                if not text.isascii() or not text.isdigit():
                    raise ValueError("Напиши количество пар числом от 1 до 100.")
                count = int(text)
                if not 1 <= count <= 100:
                    raise ValueError("Количество пар: от 1 до 100.")
                result = self.features.command(f"/pe add {state['date']} {count}", {})
                self.pending = None
                if result.startswith("❗"):
                    raise ValueError("Не удалось добавить запись физры.")
                self.show_pe()
            elif kind == "task_title":
                title = text.strip()
                if not title or len(title) > 500:
                    raise ValueError("Текст дела: от 1 до 500 символов.")
                self.prompt("task_due", "📅 Напиши дедлайн, например 10.11.2026, или выбери «Без дедлайна».",
                            {"title": title}, keyboard([button("Без дедлайна", "task:nodue")], [button("Отмена", "cancel")]))
            elif kind == "task_due":
                due = None if text == "-" else parse_date(text)
                task = self.features.create_task(state["title"], due)
                self.pending = None
                self.show_task(task["id"])
            elif kind == "task_deadline":
                due = "-" if text == "-" else parse_date(text)
                result = self.features.command(f"/todo deadline {state['id']} {due}", {})
                self.pending = None
                if result.startswith("❗"):
                    raise ValueError("Дело уже удалено.")
                self.show_task(state["id"])
            elif kind == "auto_score":
                try:
                    score = float(text.replace(",", "."))
                except ValueError:
                    raise ValueError("Напиши средний балл числом, например 4.2.") from None
                if not 0 < score <= 5:
                    raise ValueError("Средний балл должен быть больше 0 и не больше 5.")
                self.prompt("auto_lessons", "Сколько всего занятий по этому предмету за семестр?", {"idx": state["idx"], "score": score})
            elif kind == "auto_lessons":
                if not text.isascii() or not text.isdigit():
                    raise ValueError("Напиши количество занятий целым числом от 1 до 1000.")
                total = int(text)
                if not 1 <= total <= 1000:
                    raise ValueError("Количество занятий: от 1 до 1000.")
                self.features.command(f"/auto {state['idx']+1} {state['score']} {total}", {})
                self.pending = None
                self.show_auto()
        except (ValueError, OverflowError) as exc:
            self.send("❗ " + html.escape(str(exc)) + "\nПопробуй ещё раз.", reply_markup=CANCEL)
        return True
