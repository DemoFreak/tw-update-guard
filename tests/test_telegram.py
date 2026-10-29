import socket
import unittest
from unittest import mock

from twguard import telegram
from twguard.telegram import Telegram, TelegramError, connect, format_phases


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


if __name__ == "__main__":
    unittest.main()
