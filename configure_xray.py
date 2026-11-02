"""Создать конфигурацию локального Xray из VLESS TCP Reality ссылки."""
import argparse
import getpass
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID


def build_config(link):
    try:
        url = urlsplit(link.strip())
        params = parse_qs(url.query, keep_blank_values=True)
        def value(name, default=""):
            return params.get(name, [default])[0]
        if url.scheme != "vless" or value("security") != "reality" or value("type", "tcp") not in ("tcp", "raw"):
            raise ValueError
        user_id = str(UUID(unquote(url.username or "")))
        address, port = url.hostname, url.port or 443
        if not address or not value("pbk") or not value("sni"):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value("pbk")):
            raise ValueError
        sid = value("sid")
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", sid):
            raise ValueError
        flow = value("flow")
        if flow not in ("", "xtls-rprx-vision"):
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Нужна корректная ссылка vless:// с TCP и Reality (pbk, sni, sid).") from None
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{"listen": "127.0.0.1", "port": 10809, "protocol": "http", "settings": {}}],
        "outbounds": [{
            "protocol": "vless",
            "settings": {"vnext": [{"address": address, "port": port, "users": [
                {"id": user_id, "encryption": "none", "flow": flow}
            ]}]},
            "streamSettings": {
                "network": "raw", "security": "reality",
                "realitySettings": {"fingerprint": value("fp", "chrome"),
                                    "serverName": value("sni"), "password": value("pbk"), "shortId": sid},
            },
        }],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    link = getpass.getpass("Вставь VLESS-ссылку (ввод скрыт) и нажми Enter: ")
    try:
        config = build_config(link)
    except ValueError as exc:
        parser.exit(1, str(exc) + "\n")
    path = args.output
    if path.exists() and path.read_text(encoding="utf-8").strip() not in ("", "{}"):
        parser.exit(1, "Файл уже содержит настройки. Сначала сохрани его резервную копию и выбери другой путь.\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    path.chmod(0o600)
    print("Конфигурация сохранена. HTTP-прокси: http://127.0.0.1:10809")


if __name__ == "__main__":
    main()
