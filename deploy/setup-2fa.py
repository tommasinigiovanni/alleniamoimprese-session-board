#!/usr/bin/env python3
"""Authorize the first authenticator enrollment through a local terminal."""

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_board.mfa_store import AlreadyEnrolled, MFAStore, MFAStoreError


def main():
    default_state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_state / "session-board/auth")
    parser.add_argument("--username", default=os.environ.get("BOARD_USERNAME", "admin"))
    args = parser.parse_args()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.exit(1, "Serve un terminale interattivo per mostrare il bootstrap in modo riservato.\n")
    try:
        bootstrap = MFAStore(args.state_dir).prepare_enrollment(args.username)
    except AlreadyEnrolled:
        parser.exit(1, "Il secondo fattore è già attivo: questa procedura non esegue reset.\n")
    except (MFAStoreError, ValueError):
        parser.exit(1, "Impossibile preparare il bootstrap: verificare utente e integrità dello stato MFA.\n")
    print("Bootstrap iniziale valido 15 minuti. Apri /login, inserisci le credenziali e completa il setup.")
    print(f"Bootstrap: {bootstrap}")
    print("Il codice autorizza una sola registrazione. Non salvarlo nei log o nella configurazione.")


if __name__ == "__main__":
    main()
