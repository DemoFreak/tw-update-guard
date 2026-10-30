"""Telegram-Attrappe für die Tests: zeichnet alle Aufrufe auf."""

from twguard.telegram import TelegramError


class FakeTelegram:
    def __init__(self, *args):
        self.sent = []  # Texte gesendeter Nachrichten
        self.documents = []  # (Dateiname, Inhalt, Beschriftung)
        self.buttons = []
        self.silent = []
        self.markups = []
        self.edited = []  # (message_id, text, buttons)
        self.raw_edits = []  # (message_id, text)
        self.deleted = []
        self.answered = []
        self.typed = []
        self.commands = None
        self.fail_delete = False
        self.next_id = 100
        self.chat_id = -100
        self.migration = None

    def send(self, text, chat_id=None, silent=False, reply_to=None, buttons=None, markup=None):
        self.sent.append(text)
        self.buttons.append(buttons)
        self.silent.append(silent)
        self.markups.append(markup)
        self.next_id += 1
        return {"message_id": self.next_id, "chat": {"id": chat_id or -100}}

    def send_document(self, chat_id, filename, content, caption=""):
        self.documents.append((filename, content, caption))
        self.next_id += 1
        return {"message_id": self.next_id, "chat": {"id": chat_id}}

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edited.append((message_id, text, buttons))
        return {}

    def edit_raw(self, chat_id, message_id, text, entities, reply_markup):
        self.raw_edits.append((message_id, text))
        return {}

    def delete(self, chat_id, message_id):
        if self.fail_delete:
            raise TelegramError("deleteMessage: Bad Request: message can't be deleted")
        self.deleted.append(message_id)

    def answer_callback(self, callback_id, text="", alert=False):
        self.answered.append(text)

    def typing(self, chat_id):
        self.typed.append(chat_id)

    def set_commands(self, commands):
        self.commands = commands
