#!/usr/bin/env python3
"""Build a reproducible source release and optional self-contained cloud-config."""

import argparse
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import shlex
import tarfile
from urllib.parse import urlsplit


FILES = (
    "README.md", "requirements.txt", "requirements-audio.txt", "run.sh", "tools/build_release.py",
    "tools/report-status.py", "deploy/session-board.service",
    "deploy/set-password.py", "session_board/__init__.py",
    "session_board/app.py", "session_board/tmux_backend.py",
    "session_board/store.py", "session_board/mcp_health.py",
        "session_board/subscriptions_api.py", "session_board/accounts.py",
        "session_board/claude_accounts.py", "session_board/login_pty.py",
        "session_board/session_switch.py", "session_board/usage.py",
    "session_board/terminal_input.py", "session_board/terminal_images.py",
    "session_board/terminal_delivery.py",
    "session_board/chat.py", "session_board/transcripts.py", "session_board/codex_chat.py", "session_board/chat_media.py",
    "session_board/chat_actions.py", "session_board/chat_delivery.py", "session_board/questions.py",
    "session_board/session_summary.py",
    "session_board/action_details.py",
    "session_board/audio.py", "session_board/audio_worker.py", "session_board/audio_runtime.py",
    "session_board/mfa_store.py", "session_board/auth_flow.py", "deploy/setup-2fa.py",
    "session_board/files_api.py",
)
ASSETS = {"session_board/templates": {".html"}, "session_board/static": {".css", ".js", ".svg", ".webmanifest", ".png"}}


def checked_path(source, relative):
    path = source / relative
    for candidate in (path, *path.parents):
        if candidate == source:
            break
        if candidate.is_symlink():
            raise ValueError(f"refusing symlink: {candidate.relative_to(source)}")
    if not path.is_file():
        raise ValueError(f"required file missing: {relative}")
    return path


def release_bytes(source):
    entries = {name: checked_path(source, name) for name in FILES}
    for folder, extensions in ASSETS.items():
        directory = source / folder
        if directory.is_symlink():
            raise ValueError(f"refusing symlink: {folder}")
        if not directory.is_dir():
            raise ValueError(f"asset directory missing: {folder}")
        found = False
        for path in directory.rglob("*"):
            relative = path.relative_to(source)
            if any(part.startswith(".") for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"refusing symlink: {relative}")
            if path.is_file() and path.suffix in extensions:
                entries[str(relative)] = checked_path(source, relative)
                found = True
        if not found:
            raise ValueError(f"asset directory empty: {folder}")
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, path in sorted(entries.items()):
                content = path.read_bytes()
                member = tarfile.TarInfo(f"session-board/{name}")
                member.size = len(content)
                member.mode = 0o755 if name == "run.sh" else 0o644
                archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def cloud_config(archive, archive_url=None):
    if archive_url is not None:
        try:
            parsed = urlsplit(archive_url)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or "?" in archive_url or "#" in archive_url
                    or any(ord(char) <= 32 or ord(char) == 127 for char in archive_url)):
                raise ValueError
            parsed.port
        except ValueError:
            raise ValueError("--archive-url requires HTTPS without userinfo, query or fragment") from None
    digest = hashlib.sha256(archive).hexdigest()
    config = {
        "users": ["default", {"name": "board", "lock_passwd": True, "shell": "/bin/bash"}],
        "package_update": True,
        "packages": ["python3", "python3-venv", "tmux", "ca-certificates"],
        "write_files": [{
            "path": "/var/lib/session-board-release.tar.gz",
            "owner": "root:root", "permissions": "0644", "encoding": "b64",
            "content": base64.b64encode(archive).decode("ascii"),
        }],
        "runcmd": [
            ["python3", "-c", "import hashlib,pathlib,sys; "
             "actual=hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest(); "
             "sys.exit(0 if actual == sys.argv[2] else 'release checksum mismatch')",
             "/var/lib/session-board-release.tar.gz", digest],
            ["install", "-d", "-m", "0755", "/opt/session-board"],
            ["tar", "--no-same-owner", "--strip-components=1", "-xzf",
             "/var/lib/session-board-release.tar.gz", "-C", "/opt/session-board"],
            ["python3", "-m", "venv", "/opt/session-board/.venv"],
            ["/opt/session-board/.venv/bin/python", "-m", "pip", "install", "--disable-pip-version-check",
             "-r", "/opt/session-board/requirements.txt"],
            ["install", "-d", "-o", "board", "-g", "board", "-m", "0700",
             "/home/board/.config", "/home/board/.config/session-board", "/home/board/.local",
             "/home/board/.local/state", "/home/board/.local/state/session-board"],
            ["install", "-m", "0644", "/opt/session-board/deploy/session-board.service",
             "/etc/systemd/system/session-board.service"],
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "session-board.service"],
        ],
        "final_message": "Session Board installed. Set the board password and prepare 2FA via SSH, then start session-board.service. No credentials are included in user-data.",
    }
    if archive_url is not None:
        del config["write_files"]
        config["packages"].append("curl")
        config["runcmd"].insert(0, [
            "curl", "--fail", "--silent", "--show-error", "--location", "--max-redirs", "5",
            "--proto", "=https", "--proto-redir", "=https", "--max-time", "60",
            "--output", "/var/lib/session-board-release.tar.gz", "--", archive_url,
        ])
    # One fail-fast script prevents enabling a partial installation after an error.
    commands = config["runcmd"]
    config["runcmd"] = [["/bin/sh", "-ec", "\n".join(shlex.join(command) for command in commands)]]
    # JSON is a YAML subset and avoids an extra dependency in the release builder.
    return "#cloud-config\n" + json.dumps(config, indent=2) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="destination .tar.gz")
    parser.add_argument("--cloud-init", type=Path, help="optional cloud-config with embedded archive")
    parser.add_argument("--archive-url", help="optional HTTPS archive URL for small cloud-config; uploads nothing")
    args = parser.parse_args()
    if args.archive_url is not None and not args.cloud_init:
        parser.error("--archive-url requires --cloud-init")
    try:
        archive = release_bytes(args.source.resolve())
        cloud_bytes = cloud_config(archive, args.archive_url).encode("utf-8") if args.cloud_init else None
    except (OSError, ValueError) as exc:
        parser.exit(1, f"release build failed: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(archive)
    print(f"SHA256 {hashlib.sha256(archive).hexdigest()}  {args.output.name}")
    if args.cloud_init:
        args.cloud_init.parent.mkdir(parents=True, exist_ok=True)
        args.cloud_init.write_bytes(cloud_bytes)
        compressed_path = args.cloud_init.with_name(args.cloud_init.name + ".gz")
        compressed_path.write_bytes(gzip.compress(cloud_bytes, mtime=0))
        print(f"Cloud-init: {args.cloud_init.name}")
        print(f"Cloud-init gzip: {compressed_path.name} ({compressed_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
