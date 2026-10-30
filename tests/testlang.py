"""Gleiche Umgebung für alle Tests, auch auf einem Server mit echter Konfiguration.

Die Tests prüfen die deutschen Texte im Code; die Standardsprache prüft test_i18n für sich.
Die Konfiguration des Servers (OTA führt die Tests dort aus) darf sie nicht beeinflussen.
"""

import os

from twguard.i18n import set_language

os.environ["TWGUARD_CONFIG"] = "/nonexistent/twguard-tests.toml"
os.environ.pop("TWGUARD_STATE_DIR", None)
set_language("de")
