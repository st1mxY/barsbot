import unittest
import threading
from unittest.mock import Mock, patch
import requests

import bars_bot
from configure_xray import build_config


class ProxyTests(unittest.TestCase):
    def test_vless_config_is_loopback_only_and_preserves_reality(self):
        link = ("vless://11111111-1111-4111-8111-111111111111@example.com:443?"
                "type=tcp&security=reality&pbk=" + "A" * 43
                + "&sid=aabb&sni=example.org&fp=chrome&flow=xtls-rprx-vision")
        cfg = build_config(link)
        self.assertEqual(cfg["inbounds"][0]["listen"], "127.0.0.1")
        outbound = cfg["outbounds"][0]
        self.assertEqual(outbound["settings"]["vnext"][0]["address"], "example.com")
        self.assertEqual(outbound["streamSettings"]["realitySettings"]["password"], "A" * 43)
        with self.assertRaises(ValueError):
            build_config(link.replace("type=tcp", "type=grpc"))

    def test_all_telegram_endpoints_use_proxy(self):
        reply = Mock(ok=True)
        reply.json.return_value = {"ok": True, "result": []}
        proxy = "http://127.0.0.1:10809"
        with patch.object(bars_bot, "TG_PROXY", proxy):
            tg = bars_bot.Telegram("test", "42")
        with patch("bars_bot.requests.Session.post", return_value=reply) as post, patch("bars_bot.requests.Session.get", return_value=reply) as get:
            tg.send("hello")
            tg.answer_callback("callback")
            tg.edit_message(17, "menu")
            tg.updates(0)
        for call in [*post.call_args_list, *get.call_args_list]:
            self.assertEqual(call.kwargs["proxies"], {"http": proxy, "https": proxy})

    def test_sessions_are_reused_per_thread(self):
        tg = bars_bot.Telegram("test", "42")
        first = tg._session()
        self.assertIs(first, tg._session())
        other = []
        thread = threading.Thread(target=lambda: other.append(tg._session()))
        thread.start()
        thread.join()
        self.assertIsNot(first, other[0])
        first.close()
        other[0].close()

    def test_timeout_resets_connection_without_repeating_message(self):
        tg = bars_bot.Telegram("test", "42")
        first = tg._session()
        with patch("bars_bot.requests.Session.post", side_effect=requests.ReadTimeout("test timeout")) as post:
            self.assertFalse(tg.send("test"))
            self.assertEqual(post.call_count, 1)
        self.assertIsNot(first, tg._session())
        with patch("bars_bot.requests.Session.get", side_effect=requests.ReadTimeout("poll timeout")):
            with self.assertRaises(requests.ReadTimeout):
                tg.updates(0)
        self.assertFalse(hasattr(tg._local, "session"))

    def test_token_and_proxy_password_are_removed_from_errors(self):
        error = "Failed https://api.telegram.org/bot123456:secret/sendMessage via http://user:password@localhost:10809"
        safe = bars_bot.redact_secrets(error)
        for secret in ("123456:secret", "user:password"):
            self.assertNotIn(secret, safe)

    def test_edit_api_handles_unchanged_deleted_and_network_errors(self):
        tg = bars_bot.Telegram("test", "42")
        response = Mock(status_code=400)
        response.json.return_value = {"ok": False, "description": "Bad Request: message is not modified"}
        with patch("bars_bot.requests.Session.post", return_value=response) as post:
            self.assertTrue(tg.edit_message(17, "menu"))
            self.assertTrue(post.call_args.args[0].endswith("editMessageText"))
            self.assertEqual(post.call_args.kwargs["json"]["message_id"], 17)
            response.json.return_value = {"ok": False, "description": "Bad Request: message to edit not found"}
            self.assertFalse(tg.edit_message(17, "menu"))
        with patch("bars_bot.requests.Session.post", side_effect=requests.ReadTimeout("test timeout")):
            self.assertIsNone(tg.edit_message(17, "menu"))


if __name__ == "__main__":
    unittest.main()
