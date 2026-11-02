#!/usr/bin/env python3
"""
БАРС МЭИ → Telegram.

Раз в N минут входит в bars.mpei.ru, читает страницы «Оценки» и «Пропуски
занятий», сравнивает с прошлой проверкой и присылает в Telegram только то,
что изменилось: новые/изменённые оценки и новые пропуски.

Запуск:
    python bars_bot.py           # обычный режим (проверки по расписанию + команды в Telegram)
    python bars_bot.py --once    # одна проверка и выход
    python bars_bot.py --dump    # войти и сохранить HTML страниц в dumps/ (для отладки парсера)
"""

from __future__ import annotations

import argparse
import copy
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import pickle
import queue
import re
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from bot_features import PersonalFeatures, now_moscow
from bot_ui import BotUI, MAIN_MENU

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BARS_BASE = os.getenv("BARS_BASE", "https://bars.mpei.ru/bars_web/")
BARS_LOGIN = os.getenv("BARS_LOGIN", "")
BARS_PASSWORD = os.getenv("BARS_PASSWORD", "")
BARS_TOTP_SECRET = os.getenv("BARS_TOTP_SECRET", "").strip()
STUDENT_ID = os.getenv("STUDENT_ID", "").strip()
MARKS_URL = os.getenv("MARKS_URL", "").strip()
ABSENCES_URL = os.getenv("ABSENCES_URL", "").strip()
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
CHECK_INTERVAL_MIN = float(os.getenv("CHECK_INTERVAL_MIN", "20"))
QUIET_HOURS = os.getenv("QUIET_HOURS", "").strip()  # например "1-7": ночью не проверять
STATE_FILE = BASE_DIR / os.getenv("STATE_FILE", "state.json")
COOKIES_FILE = BASE_DIR / os.getenv("COOKIES_FILE", "cookies.pkl")
PERSONAL_FILE = BASE_DIR / os.getenv("PERSONAL_FILE", "personal.json")
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "21:00").strip()

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

log = logging.getLogger("bars-bot")


# --------------------------------------------------------------------------- #
#  Вход в БАРС и загрузка страниц
# --------------------------------------------------------------------------- #

class BarsError(Exception):
    pass


def _has_password_field(soup: BeautifulSoup) -> bool:
    return soup.find("input", {"type": "password"}) is not None


REMEMBER_RE = re.compile(r"remember|запомн|trust|довер", re.I)
AF2_SEND_RE = re.compile(r"af2_code_send\(\s*'[^']*'\s*,\s*'([^']+)'\s*,\s*'(\d+)'")
TOTP_RE = re.compile(r"totp|одноразов|аутентификатор|authenticator", re.I)
CODE_NAME_RE = re.compile(r"code|код|otp|token|pin|totp", re.I)
TWOFA_TEXT_RE = re.compile(r"(введите|отправлен|получен\w*|одноразов\w*|подтвержд\w*)\s.{0,40}код|двухфактор|2fa|второ\w* фактор", re.I)


def _find_code_input(soup: BeautifulSoup):
    candidates = [
        i for i in soup.find_all("input")
        if i.get("name") and (i.get("type") or "text").lower() in ("text", "number", "tel")
    ]
    for i in candidates:
        if CODE_NAME_RE.search(i["name"] + " " + (i.get("id") or "") + " " + (i.get("placeholder") or "")):
            return i
    return candidates[0] if len(candidates) == 1 else None


def _is_2fa_page(soup: BeautifulSoup) -> bool:
    text = soup.get_text(" ", strip=True)
    if not TWOFA_TEXT_RE.search(text):
        return False
    # В кабинете есть кнопка «Выйти» — значит, мы уже внутри, а не на проверке кода
    logged_in = any(_clean(a.get_text()).lower() == "выйти" for a in soup.find_all(["a", "button"]))
    return not logged_in


def totp_now(secret: str, at: float | None = None, digits: int = 6, period: int = 30) -> str:
    """Код TOTP (RFC 6238, SHA1) — то же, что показывает приложение-аутентификатор."""
    key = secret.replace(" ", "").upper()
    key = base64.b32decode(key + "=" * (-len(key) % 8))
    counter = int((time.time() if at is None else at) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def _save_debug_page(name: str, content: str) -> None:
    out = BASE_DIR / "dumps"
    out.mkdir(exist_ok=True)
    (out / name).write_text(content, encoding="utf-8")


class BarsClient:
    def __init__(self, login: str, password: str, base: str = BARS_BASE):
        self.login_name = login
        self.password = password
        self.base = base
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ru-RU,ru;q=0.9"})
        self.landing_html: str | None = None
        self.landing_url: str | None = None
        self.code_provider = None  # функция, которая спрашивает код у человека (если нет TOTP)
        # Восстанавливаем сессию после перезапуска, чтобы лишний раз не проходить 2FA
        if COOKIES_FILE.exists():
            try:
                with COOKIES_FILE.open("rb") as f:
                    self.s.cookies.update(pickle.load(f))
            except Exception:
                COOKIES_FILE.unlink(missing_ok=True)

    def _save_cookies(self) -> None:
        with COOKIES_FILE.open("wb") as f:
            pickle.dump(self.s.cookies, f)
        COOKIES_FILE.chmod(0o600)

    def _get(self, url: str) -> requests.Response:
        r = self.s.get(url, timeout=30)
        r.raise_for_status()
        return r

    def _submit(self, form, page_url: str, data: list[tuple[str, str]]) -> requests.Response:
        action = urljoin(page_url, form.get("action") or page_url)
        if (form.get("method") or "post").lower() == "get":
            r = self.s.get(action, params=data, timeout=30)
        else:
            r = self.s.post(action, data=data, timeout=30, headers={"Referer": page_url})
        r.raise_for_status()
        return r

    @staticmethod
    def _base_fields(form) -> list[tuple[str, str]]:
        """Скрытые поля и значения по умолчанию; галочки «запомнить» включаем."""
        data: list[tuple[str, str]] = []
        for inp in form.find_all(["input", "select"]):
            name = inp.get("name")
            if not name:
                continue
            if inp.name == "select":
                opt = inp.find("option", selected=True) or inp.find("option")
                if opt is not None:
                    data.append((name, opt.get("value", opt.get_text(strip=True))))
                continue
            itype = (inp.get("type") or "text").lower()
            if itype == "checkbox":
                if REMEMBER_RE.search(name) or inp.has_attr("checked"):
                    data.append((name, inp.get("value", "true")))
            elif itype == "radio":
                if inp.has_attr("checked"):
                    data.append((name, inp.get("value", "on")))
            elif itype not in ("submit", "button", "image", "reset", "password"):
                data.append((name, inp.get("value", "")))
        return data

    def login(self) -> None:
        r = self._get(self.base)
        soup = BeautifulSoup(r.text, "html.parser")
        pwd = soup.find("input", {"type": "password"})
        if pwd is None:
            # уже залогинены (живая сессия)
            self.landing_html, self.landing_url = r.text, r.url
            return

        form = pwd.find_parent("form")
        if form is None:
            raise BarsError("На странице входа не найдена форма логина")

        user_field = None
        for inp in form.find_all("input"):
            if (inp.get("type") or "text").lower() in ("text", "email") and inp.get("name"):
                user_field = inp["name"]
                break
        if user_field is None:
            raise BarsError("Не нашёл поле логина в форме входа")

        data = [(k, v) for k, v in self._base_fields(form) if k != user_field]
        data += [(user_field, self.login_name), (pwd["name"], self.password)]

        log.info("Вход в БАРС как %s", self.login_name)
        r2 = self._submit(form, r.url, data)

        # После пароля БАРС может спросить второй фактор (иногда — сначала выбор способа)
        for _ in range(3):
            soup2 = BeautifulSoup(r2.text, "html.parser")
            if _has_password_field(soup2):
                err = soup2.select_one(".validation-summary-errors, .field-validation-error, .alert-danger, .text-danger")
                msg = err.get_text(" ", strip=True) if err else "неверный логин/пароль или код?"
                raise BarsError(f"Не удалось войти в БАРС: {msg}")
            if not _is_2fa_page(soup2):
                break
            r2 = self._pass_2fa(soup2, r2.url)
        else:
            raise BarsError("Не удалось пройти двухфакторную проверку")

        self.landing_html, self.landing_url = r2.text, r2.url
        self._save_cookies()

    def _pass_2fa(self, soup: BeautifulSoup, page_url: str) -> requests.Response:
        code_input = _find_code_input(soup)

        if code_input is None:
            # Возможно, сначала нужно выбрать способ: ищем вариант TOTP
            for el in soup.find_all(["a", "button", "input"]):
                label = _clean(el.get_text(" ") + " " + (el.get("value") or "") + " " + (el.get("title") or ""))
                if not TOTP_RE.search(label) or not BARS_TOTP_SECRET:
                    continue
                if el.name == "a" and el.get("href") and not el["href"].startswith(("#", "javascript")):
                    return self._get(urljoin(page_url, el["href"]))
                form = el.find_parent("form")
                if form is not None:
                    data = self._base_fields(form)
                    if el.get("name"):
                        data.append((el["name"], el.get("value", "")))
                    return self._submit(form, page_url, data)
            _save_debug_page("2fa_unrecognized.html", str(soup))
            raise BarsError("Не понял страницу двухфакторной проверки (сохранил её в dumps/2fa_unrecognized.html)")

        form = code_input.find_parent("form")
        if form is None:
            _save_debug_page("2fa_unrecognized.html", str(soup))
            raise BarsError("Поле для кода есть, но формы нет (сохранил страницу в dumps/2fa_unrecognized.html)")

        data = [(k, v) for k, v in self._base_fields(form) if k != code_input["name"]]

        # Если в форме есть выбор способа (select/radio) — выбираем TOTP
        if BARS_TOTP_SECRET:
            for sel in form.find_all("select"):
                for opt in sel.find_all("option"):
                    if TOTP_RE.search(opt.get_text(" ")):
                        data = [(k, v) for k, v in data if k != sel.get("name")]
                        data.append((sel.get("name"), opt.get("value", opt.get_text(strip=True))))
            for rb in form.find_all("input", {"type": "radio"}):
                lbl = soup.find("label", {"for": rb.get("id")}) if rb.get("id") else rb.find_parent("label")
                text = (lbl.get_text(" ") if lbl else "") + " " + (rb.get("value") or "")
                if TOTP_RE.search(text):
                    data = [(k, v) for k, v in data if k != rb.get("name")]
                    data.append((rb.get("name"), rb.get("value", "on")))

        # БАРС МЭИ: способ кода переключается JS-ссылкой
        #   af2_code_send('btnSend', '/bars_web/Auth/JSON_SendAF2_Code', '5', '6')  — «Войти по одноразовому коду»
        # Без этого вызова сервер ждёт 4-значный код из MAX/ВК.
        self._choose_af2_mode(soup, page_url)

        code = self._get_code()
        data.append((code_input["name"], code))
        submit = form.find(["button", "input"], {"type": "submit", "name": True})
        if submit is not None:
            data.append((submit["name"], submit.get("value", "")))
        log.info("Отправляю код второго фактора")
        return self._submit(form, page_url, data)

    def _choose_af2_mode(self, soup: BeautifulSoup, page_url: str) -> None:
        calls = []
        for el in soup.find_all(onclick=AF2_SEND_RE):
            m = AF2_SEND_RE.search(el["onclick"])
            label = _clean(el.get("data-text") or el.get_text(" "))
            calls.append((label, m.group(1), m.group(2)))
        if not calls:
            return
        if BARS_TOTP_SECRET:
            chosen = next((c for c in calls if TOTP_RE.search(c[0])), None)
        else:
            # Без ключа просим прислать код в мессенджер (MAX, иначе первый доступный)
            chosen = next((c for c in calls if re.search(r"макс|max", c[0], re.I)), calls[0])
        if chosen is None:
            return
        label, url, tid = chosen
        log.info("2FA: выбираю «%s»", label)
        r = self.s.get(urljoin(page_url, url), params={"tid": tid}, timeout=30)
        r.raise_for_status()
        try:
            res = r.json()
        except ValueError:
            return
        if isinstance(res, dict) and res.get("success") is False:
            raise BarsError(f"БАРС не дал выбрать «{label}»: {res.get('error') or 'ошибка'}")

    def _get_code(self) -> str:
        if BARS_TOTP_SECRET:
            # Если код вот-вот сменится — подождём новый, чтобы не отправить просроченный
            if 30 - time.time() % 30 < 3:
                time.sleep(4)
            return totp_now(BARS_TOTP_SECRET)
        if self.code_provider is not None:
            code = self.code_provider()
            if code:
                return code
        raise BarsError("БАРС просит код второго фактора, а BARS_TOTP_SECRET не задан и код не прислали")

    def fetch(self, url: str) -> str:
        r = self._get(url)
        if _has_password_field(BeautifulSoup(r.text, "html.parser")):
            log.info("Сессия истекла, вхожу заново")
            self.login()
            r = self._get(url)
            if _has_password_field(BeautifulSoup(r.text, "html.parser")):
                raise BarsError(f"После входа страница всё ещё просит пароль: {url}")
        return r.text

    def _find_own_student_id(self) -> str | None:
        page = self.fetch(urljoin(self.base, "ST/Student/ListStudent"))
        soup = BeautifulSoup(page, "html.parser")
        found: dict[str, str] = {}
        for a in soup.find_all("a", href=True):
            m = re.search(r"studentID=([0-9a-fA-F-]{36})", a["href"])
            if m:
                row = a.find_parent("tr") or a.parent
                found.setdefault(m.group(1), _clean(row.get_text(" ")).lower())
        if len(found) == 1:
            return next(iter(found))
        login = self.login_name.split("\\")[-1].lower()
        for sid, text in found.items():
            if login and login in text:
                return sid
        _save_debug_page("students.html", page)
        return None

    def discover(self) -> dict[str, str]:
        """Адреса страниц оценок и пропусков (по ссылкам из меню кабинета или из .env)."""
        if self.landing_html is None:
            self.login()
        soup = BeautifulSoup(self.landing_html or "", "html.parser")
        base = self.landing_url or self.base

        marks = urljoin(self.base, MARKS_URL) if MARKS_URL else None
        student_id = None
        for a in soup.find_all("a", href=True):
            text = _clean(a.get_text(" ")).lower()
            m = re.search(r"studentID=([0-9a-fA-F-]{36})", a["href"])
            if m and not student_id:
                student_id = m.group(1)
            if marks is None and text == "оценки барс":
                marks = urljoin(base, a["href"])
        if student_id is None and marks:
            m = re.search(r"studentID=([0-9a-fA-F-]{36})", marks)
            student_id = m.group(1) if m else None
        if STUDENT_ID:
            student_id = STUDENT_ID
        if student_id is None:
            # После входа открылось главное меню (например, у старост) — ищем свой кабинет в «Студентах»
            student_id = self._find_own_student_id()
        if marks is None and student_id:
            marks = urljoin(self.base, f"ST_Study/Main/Main?studentID={student_id}")
        if marks is None:
            _save_debug_page("landing.html", self.landing_html or "")
            raise BarsError("Не нашёл свой кабинет студента после входа. Впиши STUDENT_ID в .env "
                            "(число из адреса страницы «Оценки БАРС» после studentID=)")

        pages = {"Оценки": marks}
        if ABSENCES_URL:
            pages["Пропуски"] = urljoin(self.base, ABSENCES_URL)
        elif student_id:
            # Тот же запрос, что делает страница «Посещение занятий», но сразу все пропуски одним списком
            query = json.dumps({
                "ID": student_id, "State": "0", "SortOrder": "CustomSort1 desc",
                "Page": "1", "PageSize": "500", "SearchText": "",
                "FilterStartDate": "", "FilterEndDate": "",
                "FilterSemester": {"Value": ""},
                "FilterLessonSkipType": {"Value": "1"},      # только пропущенные
                "FilterLessonSkipReason": {"Value": ""},
            }, ensure_ascii=False)
            pages["Пропуски"] = urljoin(
                self.base,
                f"ST_Study/Lesson__Student/_PartialListStudent__Lesson__Student?studentID={student_id}&"
                + urlencode({"query": query}),
            )
        return pages


# --------------------------------------------------------------------------- #
#  Разбор страниц БАРС МЭИ
# --------------------------------------------------------------------------- #

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


TEACHER_RE = re.compile(r",\s*[А-ЯЁ][а-яё\-]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ]\.\s?[А-ЯЁ]?\.?")


def short_discipline(title: str) -> str:
    """«Базы данных, Зейн А.Н., экзамен, 4ЗЕ» → «Базы данных»."""
    m = TEACHER_RE.search(title)
    return title[:m.start()] if m else title.split(",")[0]


def parse_marks(page_html: str) -> dict:
    """Страница «Оценки БАРС»: {семестр, {дисциплина: {строка: значение}}}."""
    soup = BeautifulSoup(page_html, "html.parser")
    sem_select = soup.find("select", id="ddl_StudyFilterSemester")
    if sem_select is None:
        _save_debug_page("marks_unrecognized.html", page_html)
        raise BarsError("Страница оценок выглядит не так, как ожидалось (сохранил её в dumps/marks_unrecognized.html)")
    opt = sem_select.find("option", selected=True) or sem_select.find("option")
    semester = _clean(opt.get_text()) if opt else ""

    disciplines: dict[str, dict[str, str]] = {}
    weights: dict[str, dict[str, float]] = {}
    for head in soup.select('div[data-bs-target^="#s_ss_"]'):
        for badge in head.select(".badge"):
            badge.decompose()
        title = _clean(head.get_text(" "))
        box = soup.find(id=head["data-bs-target"].lstrip("#"))
        rows: dict[str, str] = {}
        subject_weights = {}
        if box is not None:
            for tr in box.find_all("tr"):
                if (tr.get("id") or "").startswith("divMERes"):   # раскрывающиеся «критерии»
                    continue
                cells = tr.find_all("td")
                if len(cells) < 2:          # заголовок таблицы и «Текущий контроль:»
                    continue
                for a in cells[0].find_all("a"):
                    if "критерии" in a.get_text():
                        a.decompose()
                label = _clean(cells[0].get_text(" ")).rstrip(":")
                value = _clean(cells[-1].get_text(" "))
                if label:
                    rows[label] = value
                    if re.match(r"\d+\.", label) and len(cells) == 4:
                        raw_weight = _clean(cells[1].get_text()).replace(",", ".").rstrip("%")
                        if re.fullmatch(r"\d+(?:\.\d+)?", raw_weight):
                            subject_weights[label] = float(raw_weight)
        disciplines[title] = rows
        weights[title] = subject_weights
    return {"kind": "marks", "semester": semester, "disciplines": disciplines, "weights": weights}


def parse_absences(page_html: str) -> dict:
    """Список пропусков: {id: {описание, статус}}."""
    soup = BeautifulSoup(page_html, "html.parser")
    rows = soup.select("tr[data-ls-id]")
    if not rows and soup.find("table") is None:
        _save_debug_page("absences_unrecognized.html", page_html)
        raise BarsError("Страница пропусков выглядит не так, как ожидалось (сохранил её в dumps/absences_unrecognized.html)")
    items: dict[str, dict[str, str]] = {}
    for tr in rows:
        cell = tr.find("td")
        link = cell.find("a") if cell else None
        desc = _clean(link.get_text(" ")) if link else _clean(cell.get_text(" ") if cell else "")
        status = " ".join(_clean(b.get_text()) for b in (cell.select(".badge") if cell else []))
        items[tr["data-ls-id"]] = {"desc": desc, "status": status}
    return {"kind": "absences", "items": items}


def short_lesson(desc: str) -> str:
    """«22.10.26, 1 пара (09:20-10:55), практическое занятие (Степанов Г.А.), Спортивные секции (зачёт…), Аэ-21-24»
    → «22.10.26, 1 пара, практическое занятие — Спортивные секции»."""
    parts = [p.strip() for p in re.split(r",\s+(?![^()]*\))", desc)]
    if len(parts) >= 4:
        date, pair, kind, subj = parts[0], parts[1], parts[2], parts[3]
        pair = re.sub(r"\s*\(.*?\)", "", pair)
        kind = re.sub(r"\s*\(.*?\)", "", kind)
        subj = re.sub(r"\s*\((?:[^()]|\([^()]*\))*\)\s*$", "", subj)
        return f"{date}, {pair}, {kind} — {subj}"
    return desc


# --------------------------------------------------------------------------- #
#  Сравнение снимков
# --------------------------------------------------------------------------- #

def _e(s: str) -> str:
    return html.escape(s or "—")


def diff_marks(old: dict | None, new: dict) -> list[str]:
    if not old or old.get("kind") != "marks":
        return []
    if old.get("semester") != new["semester"]:
        return [f"🗓 Начался новый семестр: <b>{_e(new['semester'])}</b>. Дальше слежу за ним."]

    out: list[str] = []
    for title, rows in new["disciplines"].items():
        prev = old["disciplines"].get(title)
        if prev is None:
            out.append(f"\n📚 <b>{_e(short_discipline(title))}</b> — новая дисциплина в БАРС")
            continue
        lines = []
        score_line = None
        for label in dict.fromkeys([*rows, *prev]):
            value = rows.get(label, "")
            before = prev.get(label, "")
            if before == value:
                continue
            if label.startswith("Балл текущего контроля"):
                score_line = f"   текущий балл: {_e(before)} → <b>{_e(value)}</b>"
                continue
            icon = "🗑" if not value else ("✏️" if before else "🆕")
            lines.append(f"{icon} {_e(label)}: {_e(before) + ' → ' if before else ''}<b>{_e(value)}</b>")
        if lines or score_line:
            out.append(f"\n📚 <b>{_e(short_discipline(title))}</b>")
            out.extend(lines)
            if score_line:
                out.append(score_line)
    for title in old["disciplines"]:
        if title not in new["disciplines"]:
            out.append(f"🗑 Дисциплина убрана из БАРС: {_e(short_discipline(title))}")
    return out


def diff_absences(old: dict | None, new: dict) -> list[str]:
    if not old or old.get("kind") != "absences":
        return []
    out: list[str] = []
    for lid, it in new["items"].items():
        before = old["items"].get(lid)
        if before is None:
            reason = " (по уважительной)" if "уважит" in it["status"] else ""
            out.append(f"🆕 {_e(short_lesson(it['desc']))}{reason}")
        elif before["status"] != it["status"]:
            out.append(f"✏️ {_e(short_lesson(it['desc']))}: {_e(before['status'])} → <b>{_e(it['status'])}</b>")
    for lid, it in old["items"].items():
        if lid not in new["items"]:
            out.append(f"🗑 Пропуск убран: {_e(short_lesson(it['desc']))}")
    return out


PARSERS = {"Оценки": (parse_marks, diff_marks), "Пропуски": (parse_absences, diff_absences)}


# --------------------------------------------------------------------------- #
#  Telegram
# --------------------------------------------------------------------------- #

class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.api = f"https://api.telegram.org/bot{token}/"
        self.chat_id = chat_id

    def send(self, text: str, chat_id: str | None = None, reply_markup=MAIN_MENU) -> bool:
        chat = chat_id or self.chat_id
        if not chat:
            log.warning("TG_CHAT_ID не задан, сообщение не отправлено:\n%s", text)
            return False
        # Telegram ограничивает сообщение 4096 символами — режем по строкам
        chunk = ""
        for line in text.split("\n"):
            if len(chunk) + len(line) + 1 > 3900:
                if not self._send_raw(chat, chunk):
                    return False
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            if not self._send_raw(chat, chunk, reply_markup):
                return False
        return True

    def _send_raw(self, chat: str, text: str, reply_markup=None) -> bool:
        try:
            payload = {
                "chat_id": chat, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            r = requests.post(self.api + "sendMessage", json=payload, timeout=30)
            if not r.ok:
                log.error("Telegram ответил %s: %s", r.status_code, r.text[:300])
                return False
            return bool(r.json().get("ok"))
        except requests.RequestException as e:
            log.error("Не удалось отправить в Telegram: %s", e)
            return False

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        if not callback_id:
            return
        try:
            r = requests.post(self.api + "answerCallbackQuery", json={
                "callback_query_id": callback_id, "text": text,
            }, timeout=10)
            r.raise_for_status()
        except requests.RequestException:
            log.warning("Не удалось подтвердить нажатие кнопки Telegram")

    def updates(self, offset: int) -> list[dict]:
        r = requests.get(self.api + "getUpdates",
                         params={"offset": offset, "timeout": 50}, timeout=60)
        r.raise_for_status()
        return r.json().get("result", [])


# --------------------------------------------------------------------------- #
#  Основная логика
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("state.json повреждён, начинаю заново")
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


class Watcher:
    def __init__(self, tg: Telegram):
        self.tg = tg
        self.codes: queue.Queue[str] = queue.Queue()
        self.awaiting_code = threading.Event()
        self.bars = self._new_client()
        self.state = load_state()
        self.lock = threading.Lock()
        self.fail_streak = 0
        self.features = PersonalFeatures(PERSONAL_FILE, DAILY_REPORT_TIME)
        self.last_check: str = self.state.get("last_check", "ещё не было")
        self.state_lock = threading.Lock()

    def _new_client(self) -> "BarsClient":
        c = BarsClient(BARS_LOGIN, BARS_PASSWORD)
        c.code_provider = self.ask_code
        return c

    def ask_code(self) -> str | None:
        """Без TOTP-ключа: просим код (из MAX/ВК) в Telegram и ждём ответ до 10 минут."""
        while not self.codes.empty():
            self.codes.get_nowait()
        self.awaiting_code.set()
        try:
            self.tg.send("🔐 БАРС просит код подтверждения. Пришли его сюда одним сообщением (только цифры).", reply_markup=None)
            return self.codes.get(timeout=600)
        except queue.Empty:
            self.tg.send("⌛ Код так и не пришёл, попробую войти позже.")
            return None
        finally:
            self.awaiting_code.clear()

    def check(self, manual: bool = False) -> str:
        with self.lock:
            try:
                if self.bars.landing_html is None:
                    self.bars.login()
                pages = self.bars.discover()
                if not pages:
                    raise BarsError("Не нашёл страницы оценок/пропусков — укажи MARKS_URL и ABSENCES_URL в .env")

                report: list[str] = []
                first_run_pages = []
                with self.state_lock:
                    next_state = copy.deepcopy(self.state)
                for name, url in pages.items():
                    parse, differ = PARSERS[name]
                    snap = parse(self.bars.fetch(url))
                    snaps = next_state.setdefault("snapshots", {})
                    old = snaps.get(name)
                    if old is None:
                        first_run_pages.append(name)
                    else:
                        changes = differ(old, snap)
                        if changes:
                            icon = "📌" if name == "Оценки" else "⚠️"
                            report.append(f"{icon} <b>{name}</b>")
                            report.extend(changes)
                            report.append("")
                    snaps[name] = snap
                self.last_check = now_moscow().strftime("%d.%m %H:%M МСК")
                next_state["last_check"] = self.last_check
                # Отмечаем изменения до сохранения снимка, чтобы перезапуск
                # между записями не привёл к ложному итогу «изменений нет».
                self.features.record_check(bool(report), baseline=bool(first_run_pages))
                save_state(next_state)
                with self.state_lock:
                    self.state = next_state
                if self.fail_streak >= 3:
                    self.tg.send("✅ Снова получается заходить в БАРС.")
                self.fail_streak = 0

                if first_run_pages:
                    self.tg.send("👀 Начал следить: " + ", ".join(first_run_pages)
                                 + ".\nТекущее состояние запомнил — дальше буду писать только об изменениях.")
                if report:
                    self.tg.send("\n".join(report).strip())
                    return "есть изменения"
                if manual and not first_run_pages:
                    self.tg.send("Новых оценок и пропусков нет.")
                return "без изменений"

            except (BarsError, requests.RequestException) as e:
                self.fail_streak += 1
                self.bars = self._new_client()  # чистая сессия на следующую попытку
                log.error("Проверка не удалась (%d подряд): %s", self.fail_streak, e)
                if manual or self.fail_streak == 3:
                    self.tg.send(f"❗ Не получается проверить БАРС: {html.escape(str(e))}")
                return f"ошибка: {e}"


def in_quiet_hours() -> bool:
    if not QUIET_HOURS:
        return False
    try:
        start, end = (int(x) for x in QUIET_HOURS.split("-"))
    except ValueError:
        return False
    h = now_moscow().hour
    return start <= h < end if start < end else (h >= start or h < end)


def scheduler_loop(w: Watcher, stop: threading.Event) -> None:
    next_check = 0.0
    while not stop.is_set():
        if time.monotonic() >= next_check and not in_quiet_hours():
            log.info("Проверка: %s", w.check())
            next_check = time.monotonic() + CHECK_INTERVAL_MIN * 60
        # Тик раз в 15 секунд: вечерний итог не привязан к интервалу проверок.
        with w.lock:
            w.features.daily_report(w.tg.send, healthy=w.fail_streak == 0)
        stop.wait(15)


def commands_loop(w: Watcher, tg: Telegram, stop: threading.Event) -> None:
    offset = 0
    ui = BotUI(w, tg, lambda: (
        f"Последняя проверка: {w.last_check}\nИнтервал: {CHECK_INTERVAL_MIN:g} мин"
        f"\nВечерний итог: {DAILY_REPORT_TIME} МСК (если изменений не было)"
        + (f"\nТихие часы: {QUIET_HOURS}" if QUIET_HOURS else "")))
    while not stop.is_set():
        try:
            for upd in tg.updates(offset):
                offset = upd["update_id"] + 1
                if upd.get("callback_query"):
                    ui.handle_callback(upd["callback_query"])
                    continue
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id", ""))
                text = (msg.get("text") or "").strip()
                if text.startswith("/"):
                    command, sep, argument = text.partition(" ")
                    text = command.split("@")[0] + (sep + argument if sep else "")
                if not chat_id:
                    continue
                if chat_id != tg.chat_id:
                    if not tg.chat_id and text == "/start":
                        # Первичная настройка: владелец ещё не вписал свой chat id
                        tg.send(f"Привет! Твой chat id: <code>{chat_id}</code>\n"
                                "Впиши его в TG_CHAT_ID в файле .env и перезапусти бота.", chat_id, reply_markup=None)
                    elif text == "/start":
                        tg.send("Это личный бот: он присылает уведомления из БАРС только своему владельцу.", chat_id, reply_markup=None)
                    continue  # остальным ничего не отвечаем
                if w.awaiting_code.is_set() and re.fullmatch(r"\d{4,8}", text.replace(" ", "")):
                    w.codes.put(text.replace(" ", ""))
                    tg.send("Принял код, вхожу…", reply_markup=None)
                    continue
                if ui.handle_text(text):
                    continue
                if text.split(" ", 1)[0] in ("/subjects", "/auto", "/pe", "/todo"):
                    with w.state_lock:
                        snapshots = copy.deepcopy(w.state.get("snapshots", {}))
                    tg.send(w.features.command(text, snapshots))
                    continue
                ui.menu()
        except requests.RequestException as e:
            log.warning("getUpdates: %s", e)
            stop.wait(10)


def dump_pages() -> None:
    out = BASE_DIR / "dumps"
    out.mkdir(exist_ok=True)
    c = BarsClient(BARS_LOGIN, BARS_PASSWORD)
    c.login()
    (out / "landing.html").write_text(c.landing_html or "", encoding="utf-8")
    print(f"Страница после входа: {c.landing_url}")
    for name, url in c.discover().items():
        page = c.fetch(url)
        fname = out / ("marks.html" if name == "Оценки" else "absences.html")
        fname.write_text(page, encoding="utf-8")
        snap = PARSERS[name][0](page)
        print(f"{name}: сохранено в dumps/{fname.name}")
        if name == "Оценки":
            graded = sum(1 for rows in snap["disciplines"].values() for v in rows.values() if v)
            print(f"  {snap['semester']}: {len(snap['disciplines'])} дисциплин, заполненных строк: {graded}")
            for title in snap["disciplines"]:
                print(f"   • {short_discipline(title)}")
        else:
            print(f"  пропусков всего: {len(snap['items'])}")
            for it in list(snap["items"].values())[:3]:
                print(f"   • {short_lesson(it['desc'])} [{it['status']}]")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="одна проверка и выход")
    ap.add_argument("--dump", action="store_true", help="сохранить HTML страниц в dumps/")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    missing = [k for k, v in {"BARS_LOGIN": BARS_LOGIN, "BARS_PASSWORD": BARS_PASSWORD}.items() if not v]
    if not args.dump and not TG_TOKEN:
        missing.append("TG_TOKEN")
    if missing:
        sys.exit(f"Заполни в .env: {', '.join(missing)}")

    if args.dump:
        dump_pages()
        return

    tg = Telegram(TG_TOKEN, TG_CHAT_ID)
    w = Watcher(tg)

    if args.once:
        print(w.check(manual=True))
        return

    stop = threading.Event()
    threading.Thread(target=commands_loop, args=(w, tg, stop), daemon=True).start()
    if not TG_CHAT_ID:
        log.warning("TG_CHAT_ID пуст. Напиши боту /start в Telegram, он пришлёт твой chat id.")
        stop.wait()  # ждём, пока пользователь настроит chat id и перезапустит
    try:
        scheduler_loop(w, stop)
    except KeyboardInterrupt:
        stop.set()


if __name__ == "__main__":
    main()
