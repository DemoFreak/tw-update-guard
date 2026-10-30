# SPDX-License-Identifier: GPL-3.0-or-later
"""Nachrichten je Thema: ersetzen statt neu senden, bearbeiten, kurzlebige wieder löschen.

Ein Thema ist z. B. "status", "briefing" oder "issue:reboot". Je Thema merkt sich der
Zustand die zuletzt gesendete Nachricht ({"chat_id", "message_id", "at"}).
"""

from __future__ import annotations

import logging
import time

from .i18n import tr
from .state import State
from .telegram import Buttons, Telegram, TelegramError

log = logging.getLogger(__name__)

# Telegram erlaubt Löschen nur in den ersten 48 Stunden; ältere Nachrichten werden gekürzt
DELETE_LIMIT = 48 * 3600
REPLACED = "↓ ersetzt durch eine neuere Nachricht"


def ref(sent: dict) -> dict:
    return {"chat_id": sent["chat"]["id"], "message_id": sent["message_id"], "at": time.time()}


def release(st: State, message_id: int) -> None:
    """Die Nachricht gehört danach keinem Thema mehr (z. B. weil sie umgewidmet wird)."""
    for topic in [t for t, r in st.messages.items() if r.get("message_id") == message_id]:
        del st.messages[topic]


def claim(st: State, topic: str, message: dict) -> None:
    release(st, message["message_id"])
    st.messages[topic] = message


def edit(tg: Telegram, message: dict, text: str, buttons: Buttons | None = None) -> bool:
    try:
        tg.edit(message["chat_id"], message["message_id"], text, buttons=buttons)
        return True
    except TelegramError as e:
        if "not modified" in str(e):
            return True
        log.info(tr("Nachricht %s nicht bearbeitet: %s"), message.get("message_id"), e)
        return False


def delete(tg: Telegram, message: dict) -> bool:
    try:
        tg.delete(message["chat_id"], message["message_id"])
        return True
    except TelegramError as e:
        log.info(tr("Nachricht %s nicht gelöscht: %s"), message.get("message_id"), e)
        return False


def retire(tg: Telegram, message: dict, short: str = "") -> None:
    """Löschen; geht das nicht mehr (älter als 48 h), auf eine Zeile ohne Schaltflächen kürzen."""
    if time.time() - message.get("at", 0) < DELETE_LIMIT and delete(tg, message):
        return
    edit(tg, message, short or tr(REPLACED))


def replace(tg: Telegram, st: State, topic: str, text: str, buttons: Buttons | None = None,
            silent: bool = False, chat_id: int | None = None, markup: dict | None = None) -> dict:
    """Neue Nachricht zum Thema senden und danach die vorige entfernen. Wirft TelegramError."""
    old = st.messages.get(topic)
    sent = tg.send(text, chat_id=chat_id, silent=silent, buttons=buttons, markup=markup)
    claim(st, topic, ref(sent))
    if old and old["message_id"] != sent["message_id"]:
        retire(tg, old)
    return sent


def edit_or_send(tg: Telegram, message: dict, text: str, buttons: Buttons | None = None) -> dict | None:
    """Ergebnis in eine vorhandene (Warte-)Nachricht schreiben, sonst neu senden."""
    if message.get("message_id") and edit(tg, message, text, buttons):
        return message
    try:
        return ref(tg.send(text, chat_id=message.get("chat_id"), buttons=buttons))
    except TelegramError as e:
        log.warning(tr("Nachricht nicht gesendet: %s"), e)
        return None


def expire(st: State, sent: dict, seconds: float) -> None:
    """Kurzlebige Antwort: der Bot löscht sie nach `seconds`."""
    st.ephemeral.append({**ref(sent), "delete_at": time.time() + seconds})


def cleanup(tg: Telegram, st: State, now: float | None = None) -> None:
    now = now or time.time()
    due = [m for m in st.ephemeral if m["delete_at"] <= now]
    st.ephemeral = [m for m in st.ephemeral if m["delete_at"] > now]
    for message in due:
        delete(tg, message)
    # Aufgeklappte Details lassen sich nach 48 h ohnehin nicht mehr sinnvoll zurückklappen
    st.collapsed = {k: v for k, v in st.collapsed.items() if now - v.get("at", 0) < DELETE_LIMIT}
