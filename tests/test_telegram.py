import socket
import unittest
from unittest import mock

import testlang  # noqa: F401
from twguard import telegram
from twguard.telegram import ErrorMeter, Telegram, TelegramError, connect, format_phases, multipart


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.addCleanup(self.server.close)

    def addrinfo(self, *hosts):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (h, self.port)) for h in hosts]

    def test_falls_back_to_next_address(self):
        # 192.0.2.1 (TEST-NET) ist nicht erreichbar, wie ein hängender IPv6-Weg
        phases = {}
        with mock.patch.object(socket, "getaddrinfo", return_value=self.addrinfo("192.0.2.1", "127.0.0.1")):
            sock = connect("api.example", self.port, 0.3, phases)
        sock.close()
        self.assertEqual(phases["addr"], "127.0.0.1")
        self.assertIn("192.0.2.1", phases["failed"][0])
        self.assertIn("connect", phases)

    def test_preferred_address_first(self):
        phases = {}
        with mock.patch.object(socket, "getaddrinfo", return_value=self.addrinfo("192.0.2.1", "127.0.0.1")):
            connect("api.example", self.port, 0.3, phases, prefer="127.0.0.1").close()
        self.assertEqual(phases["addr"], "127.0.0.1")
        self.assertNotIn("failed", phases)

    def test_client_remembers_working_address(self):
        tg = Telegram("x", 0)
        attempts = []

        def fake_connect(host, port, timeout, phases, prefer=""):
            attempts.append(prefer)
            phases["addr"] = "149.154.166.110"
            return mock.Mock()

        # Verbindung steht, danach scheitert TLS: die Adresse gilt trotzdem als erreichbar
        tg._ssl = mock.Mock(**{"wrap_socket.side_effect": OSError("TLS abgebrochen")})
        with mock.patch.object(telegram, "connect", side_effect=fake_connect):
            for _ in range(2):
                with self.assertRaises(TelegramError):
                    tg.call("getMe")
        self.assertEqual(attempts, ["", "149.154.166.110"])

    def test_no_address_reachable(self):
        with mock.patch.object(socket, "getaddrinfo", return_value=self.addrinfo("192.0.2.1")):
            with self.assertRaises(OSError):
                connect("api.example", self.port, 0.2, {})

    def test_format_phases(self):
        text = format_phases({"dns": 0.01, "connect": 7.2, "tls": 0.1, "antwort": 20.3, "addr": "149.154.167.220"})
        self.assertEqual(text, "dns 0.0 s, connect 7.2 s, tls 0.1 s, antwort 20.3 s, über 149.154.167.220")


class CallTest(unittest.TestCase):
    def test_send_follows_supergroup_migration(self):
        tg = Telegram("x", -100)
        calls = []

        def call(method, **params):
            calls.append(params["chat_id"])
            if params["chat_id"] == -100:
                raise TelegramError("sendMessage: Bad Request: group chat was upgraded to a supergroup chat",
                                    migrate_to=-1009)
            return {"message_id": 1, "chat": {"id": -1009}}

        with mock.patch.object(tg, "call", side_effect=call):
            tg.send("hallo")
        self.assertEqual(calls, [-100, -1009])
        self.assertEqual(tg.migration, (-100, -1009))
        self.assertEqual(tg.chat_id, -1009)

    def test_migration_error_for_other_chat_not_followed(self):
        tg = Telegram("x", -100)
        error = TelegramError("upgraded", migrate_to=-1009)
        with mock.patch.object(tg, "call", side_effect=error), self.assertRaises(TelegramError):
            tg.send("hallo", chat_id=42)
        self.assertIsNone(tg.migration)

    def test_network_error_becomes_telegram_error(self):
        with mock.patch.object(telegram, "connect", side_effect=OSError("keine Adresse erreichbar")):
            with self.assertRaises(TelegramError) as ctx:
                Telegram("x", 0).call("getMe")
        self.assertIn("getMe", str(ctx.exception))

    def test_slow_call_logged_with_phases(self):
        def slow(host, port, timeout, phases, prefer=""):
            phases.update(dns=0.0, connect=6.0, addr="2001:db8::1")
            raise OSError("timed out")

        with mock.patch.object(telegram, "connect", side_effect=slow), \
             mock.patch.object(telegram, "SLOW_SECONDS", -1), \
             self.assertLogs("twguard.telegram", "WARNING") as logs:
            with self.assertRaises(TelegramError):
                Telegram("x", 0).call("getUpdates", timeout=0)
        self.assertIn("connect 6.0 s", logs.output[0])
        self.assertIn("2001:db8::1", logs.output[0])


class DocumentTest(unittest.TestCase):
    def test_upload_body_contains_fields_and_file(self):
        body, content_type = multipart({"chat_id": -5}, "document", 'a".html', b"<p>inhalt</p>", "text/html")
        boundary = content_type.split("boundary=")[1]
        self.assertTrue(body.endswith(f"--{boundary}--\r\n".encode()))
        self.assertIn(b'name="chat_id"\r\n\r\n-5\r\n', body)
        self.assertIn(b'filename="a_.html"', body)
        self.assertIn(b"<p>inhalt</p>", body)

    def test_send_document_counts_as_send_error(self):
        tg = Telegram("x", 0, max_errors_per_hour=5)
        with mock.patch.object(tg, "_call", side_effect=TelegramError("sendDocument: reset")):
            with self.assertRaises(TelegramError):
                tg.send_document(-5, "a.html", b"x")
        self.assertEqual(tg.errors.counts, {"Senden": 1})


class ErrorMeterTest(unittest.TestCase):
    def test_warns_once_above_limit(self):
        meter = ErrorMeter(limit=2)
        with self.assertLogs("twguard.telegram", "WARNING") as logs:
            for now in (0, 1, 2, 3):
                meter.add("getUpdates" if now else "sendMessage", OSError("reset"), now=now)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("Abruf 2, Senden 1", logs.output[0])
        self.assertIn("reset", logs.output[0])

    def test_silent_up_to_limit(self):
        meter = ErrorMeter(limit=2)
        with self.assertNoLogs("twguard.telegram", "WARNING"):
            meter.add("getUpdates", OSError(), now=0)
            meter.add("getUpdates", OSError(), now=1)

    def test_new_hour_counts_afresh(self):
        meter = ErrorMeter(limit=1)
        with self.assertLogs("twguard.telegram", "WARNING") as logs:
            for now in (0, 1, 3600, 3601):
                meter.add("getUpdates", OSError(), now=now)
        self.assertEqual(len(logs.output), 2)

    def test_zero_never_warns_and_other_methods_do_not_count(self):
        with self.assertNoLogs("twguard.telegram", "WARNING"):
            ErrorMeter(limit=0).add("getUpdates", OSError(), now=0)
            meter = ErrorMeter(limit=1)
            for now in range(3):
                meter.add("deleteMessage", OSError(), now=now)
        self.assertEqual(meter.counts, {})

    def test_call_counts_errors_but_not_migration(self):
        tg = Telegram("x", 0, max_errors_per_hour=5)
        for error in (TelegramError("sendMessage: reset"), TelegramError("umgezogen", migrate_to=-5)):
            with mock.patch.object(tg, "_call", side_effect=error), self.assertRaises(TelegramError):
                tg.call("sendMessage")
        self.assertEqual(tg.errors.counts, {"Senden": 1})


if __name__ == "__main__":
    unittest.main()
