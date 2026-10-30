# SPDX-License-Identifier: GPL-3.0-or-later
"""tw-update-guard: Routine-Updates einspielen, Kritisches melden und nachhaken."""

__version__ = "0.7.1"

# install.sh schreibt beim Bauen den Git-Stand nach _build.py
try:
    from ._build import COMMIT
except ImportError:
    COMMIT = "unknown"
