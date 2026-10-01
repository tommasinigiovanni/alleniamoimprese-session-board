#!/usr/bin/env python3
"""Ask for the login password without echo and store only a private scrypt hash."""

import argparse
import getpass
import os
from pathlib import Path
import tempfile
import warnings

from werkzeug.security import generate_password_hash


def main():
    default_config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=Path, default=default_config / "session-board/password.hash")
    args = parser.parse_args()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Nuova password della board (almeno 12 caratteri): ")
            if len(password) < 12:
                parser.exit(1, "Password troppo corta; nessun file modificato.\n")
            if password != getpass.getpass("Conferma password: "):
                parser.exit(1, "Le password non coincidono; nessun file modificato.\n")
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        parser.exit(1, "Serve un terminale interattivo con input nascosto; nessun file modificato.\n")
    password_hash = generate_password_hash(password, method="scrypt")
    args.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=args.path.parent, prefix=".password-", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(password_hash + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f"Hash salvato in {args.path}. Riavvia la tua istanza della board per applicarlo.")


if __name__ == "__main__":
    main()
