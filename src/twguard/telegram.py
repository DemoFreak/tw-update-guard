# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimaler Client für die Telegram-Bot-API (nur Standardbibliothek)."""

from __future__ import annotations

import http.client
import json
import logging
import socket
import ssl
import time

log = logging.getLogger(__name__)

HOST = "api.telegram.org"
PORT = 443
# Pro Adresse; hängt eine (oft IPv6), kommt nach kurzer Zeit die nächste dran
CONNECT_TIMEOUT = 5
# Aufrufe, die länger dauern, landen als Warnung im Journal (getUpdates: zusätzlich zur Wartezeit)
SLOW_SECONDS = 3

# Schaltflächen: Zeilen aus (Beschriftung, Rückgabewert); der Rückgabewert hat höchstens 64 Bytes
Buttons = list[list[tuple[str, str]]]


def keyboard(buttons: Buttons | None) -> dict:
    rows = [[{"text": text, "callback_data": data} for text, data in row] for row in buttons or []]
    return {"inline_keyboard": rows}


class TelegramError(Exception):
    def __init__(self, message: str, migrate_to: int = 0):
        super().__init__(message)
        # Gruppe wurde zur Supergruppe: deren neue ID (Telegram nennt sie im Fehler)
        self.migrate_to = migrate_to


def connect(host: str, port: int, timeout: float, phases: dict, prefer: str = "") -> socket.socket:
    """Adressen der Reihe nach mit kurzem Zeitlimit versuchen; Dauer je Phase in `phases`.

    urllib wartet pro Adresse das volle Zeitlimit ab. Hängt der IPv6-Weg gelegentlich,
    kostet das jeden Aufruf viele Sekunden, während curl per Happy Eyeballs ausweicht.
    `prefer` (die zuletzt erfolgreiche Adresse) kommt zuerst dran.
    """
    start = time.monotonic()
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    phases["dns"] = time.monotonic() - start
    infos.sort(key=lambda info: info[4][0] != prefer)
    failed = []
    start = time.monotonic()
    for family, type_, proto, _, addr in infos:
        sock = socket.socket(family, type_, proto)
        sock.settimeout(timeout)
        t = time.monotonic()
        try:
            sock.connect(addr)
        except OSError as e:
            sock.close()
            failed.append(f"{addr[0]} nach {time.monotonic() - t:.1f} s: {e}")
            continue
        phases["connect"] = time.monotonic() - start
        phases["addr"] = addr[0]
        if failed:
            phases["failed"] = failed
            log.warning("Verbindung zu %s über %s, künftig zuerst; vorher: %s", host, addr[0], "; ".join(failed))
        return sock
    raise OSError("keine Adresse erreichbar: " + "; ".join(failed))


def format_phases(phases: dict) -> str:
    parts = [f"{name} {phases[name]:.1f} s" for name in ("dns", "connect", "tls", "antwort") if name in phases]
    if "addr" in phases:
        parts.append(f"über {phases['addr']}")
    return ", ".join(parts)


class Telegram:
    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = chat_id
        self._ssl = ssl.create_default_context()
        # Zuletzt erfolgreiche Adresse; so kostet ein hängender Weg nur einmal Zeit
        self._prefer = ""
        # (alte, neue) Chat-ID, falls die Gruppe beim Senden umgezogen ist; der Besitzer übernimmt sie
        self.migration: tuple[int, int] | None = None

    def call(self, method: str, http_timeout: float = 20, **params):
        start = time.monotonic()
        phases: dict = {}
        try:
            return self._call(method, http_timeout, phases, **params)
        finally:
            elapsed = time.monotonic() - start
            if elapsed > SLOW_SECONDS + params.get("timeout", 0):
                log.warning("langsam: %s brauchte %.1f s (%s)", method, elapsed, format_phases(phases))

    def _call(self, method: str, http_timeout: float, phases: dict, **params):
        conn = None
        try:
            sock = connect(HOST, PORT, CONNECT_TIMEOUT, phases, self._prefer)
            self._prefer = phases["addr"]
            t = time.monotonic()
            sock = self._ssl.wrap_socket(sock, server_hostname=HOST)
            phases["tls"] = time.monotonic() - t
            sock.settimeout(http_timeout)
            conn = http.client.HTTPSConnection(HOST, PORT, timeout=http_timeout, context=self._ssl)
            conn.sock = sock
            t = time.monotonic()
            conn.request("POST", f"/bot{self.token}/{method}", body=json.dumps(params).encode(),
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
            phases["antwort"] = time.monotonic() - t
        except (OSError, http.client.HTTPException) as e:
            raise TelegramError(f"{method}: {e}") from e
        finally:
            if conn is not None:
                conn.close()
        # Die API liefert auch bei Fehlern JSON mit "description"
        try:
            data = json.loads(body)
        except ValueError:
            raise TelegramError(f"{method}: HTTP {resp.status}") from None
        if not data.get("ok"):
            parameters = data.get("parameters") or {}
            raise TelegramError(f"{method}: {data.get('description')}",
                                migrate_to=int(parameters.get("migrate_to_chat_id", 0)))
        return data["result"]

    def send(self, text: str, chat_id: int | None = None, silent: bool = False,
             reply_to: int | None = None, buttons: Buttons | None = None, markup: dict | None = None) -> dict:
        params = {
            "chat_id": chat_id or self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
            "link_preview_options": {"is_disabled": True},
        }
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if buttons:
            params["reply_markup"] = keyboard(buttons)
        if markup:
            params["reply_markup"] = markup
        try:
            return self.call("sendMessage", **params)
        except TelegramError as e:
            if not e.migrate_to or params["chat_id"] != self.chat_id:
                raise
            # Die Gruppe ist zur Supergruppe geworden: an die neue ID senden und den Umzug vormerken
            self.migration = (self.chat_id, e.migrate_to)
            self.chat_id = params["chat_id"] = e.migrate_to
            return self.call("sendMessage", **params)

    def edit(self, chat_id: int, message_id: int, text: str, buttons: Buttons | None = None) -> dict:
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML", link_preview_options={"is_disabled": True},
                         reply_markup=keyboard(buttons))

    def edit_raw(self, chat_id: int, message_id: int, text: str, entities: list, reply_markup: dict) -> dict:
        """Inhalt so wiederherstellen, wie ihn Telegram geliefert hat (Text mit Entities statt HTML)."""
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         entities=entities, link_preview_options={"is_disabled": True},
                         reply_markup=reply_markup)

    def delete(self, chat_id: int, message_id: int) -> None:
        self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    def typing(self, chat_id: int) -> None:
        """Zeigt „schreibt …“ an, bis die Antwort kommt (höchstens 5 Sekunden)."""
        self.call("sendChatAction", http_timeout=5, chat_id=chat_id, action="typing")

    def answer_callback(self, callback_id: str, text: str = "", alert: bool = False) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200], show_alert=alert)

    def remove_buttons(self, chat_id: int, message_id: int) -> None:
        self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id, reply_markup=keyboard(None))

    def get_updates(self, offset: int, timeout: int = 10, slack: float = 5) -> list[dict]:
        # Kurze Wartezeit und knappes Zeitlimit: eine unterwegs verlorene Antwort kostet höchstens 15 s
        return self.call("getUpdates", http_timeout=timeout + slack, offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"])

    def get_me(self) -> dict:
        return self.call("getMe")
