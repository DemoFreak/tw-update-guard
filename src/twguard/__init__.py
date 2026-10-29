# SPDX-License-Identifier: GPL-3.0-or-later
"""tw-update-guard: Routine-Updates einspielen, Kritisches melden und nachhaken."""

__version__ = "0.5.2"

# install.sh schreibt beim Bauen den Git-Stand nach _build.py
try:
    from ._build import COMMIT
except ImportError:
    COMMIT = "unbekannt"
