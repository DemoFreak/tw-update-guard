"""Telegram-Attrappe für die Tests: zeichnet alle Aufrufe auf."""

import time

from twguard.telegram import TelegramError, gate


class FakeTelegram:
    def __init__(self, *args):
        self.sent = []  # Texte gesendeter Nachrichten
        self.documents = []  # (Dateiname, Inhalt, Beschriftung)
        self.buttons = []
        self.silent = []
        self.markups = []
        self.edited = []  # (message_id, text, buttons)
        self.raw_edits = []  # (message_id, text)
        self.markup_edits = []  # (message_id, reply_markup)
        self.deleted = []
        self.answered = []
        self.typed = []
        self.commands = None
        self.fail_delete = False
        self.fail_send = False
        self.fail_edit = False
        self.next_id = 100
        self.chat_id = -100
        self.migration = None
        # Inhalt jeder Nachricht, die es gerade gibt: message_id → (Text, Schaltflächen)
        self.current = {}
        # Wie der echte Client: nach 429 keine weiteren Aufrufe bis dahin; `limit` löst eines aus
        self.blocked_until = 0.0
        self.limit = {}  # Methode → retry_after für den nächsten Aufruf dieser Methode
        self.calls = []  # Methoden in der Reihenfolge der Aufrufe, die Telegram erreicht hätten

    def _call(self, method):
        gate(self, method)
        if method in self.limit:
            retry = self.limit.pop(method)
            self.blocked_until = time.time() + retry
            raise TelegramError(f"{method}: Too Many Requests: retry after {retry:.0f}", retry_after=retry)
        self.calls.append(method)

    def send(self, text, chat_id=None, silent=False, reply_to=None, buttons=None, markup=None):
        self._call("sendMessage")
        if self.fail_send:
            raise TelegramError("sendMessage: Too Many Requests")
        self.sent.append(text)
        self.buttons.append(buttons)
        self.silent.append(silent)
        self.markups.append(markup)
        self.next_id += 1
        self.current[self.next_id] = (text, buttons)
        return {"message_id": self.next_id, "chat": {"id": chat_id or -100}}

    def send_document(self, chat_id, filename, content, caption=""):
        self._call("sendDocument")
        self.documents.append((filename, content, caption))
        self.next_id += 1
        return {"message_id": self.next_id, "chat": {"id": chat_id}}

    def edit(self, chat_id, message_id, text, buttons=None):
        self._call("editMessageText")
        if self.fail_edit:
            raise TelegramError("editMessageText: Bad Request: message to edit not found")
        self.edited.append((message_id, text, buttons))
        self.current[message_id] = (text, buttons)
        return {}

    def edit_raw(self, chat_id, message_id, text, entities, reply_markup):
        self._call("editMessageText")
        self.raw_edits.append((message_id, text))
        return {}

    def edit_markup(self, chat_id, message_id, reply_markup):
        self._call("editMessageReplyMarkup")
        self.markup_edits.append((message_id, reply_markup))
        return {}

    def delete(self, chat_id, message_id):
        self._call("deleteMessage")
        if self.fail_delete:
            raise TelegramError("deleteMessage: Bad Request: message can't be deleted")
        self.deleted.append(message_id)
        self.current.pop(message_id, None)

    def answer_callback(self, callback_id, text="", alert=False):
        self.answered.append(text)

    def typing(self, chat_id):
        self.typed.append(chat_id)

    def set_commands(self, commands):
        self.commands = commands
