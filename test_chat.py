"""Print messages from a Twitch channel's read-only chat for 60 seconds."""

import argparse
import secrets
import socket
import ssl
import time
from datetime import datetime, timezone


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("channel", help="Twitch channel login name")
    args = parser.parse_args()
    channel = args.channel.lstrip("#").lower()

    context = ssl.create_default_context()
    with socket.create_connection(("irc.chat.twitch.tv", 6697), timeout=15) as raw_sock:
        with context.wrap_socket(raw_sock, server_hostname="irc.chat.twitch.tv") as sock:
            sock.settimeout(1)
            sock.sendall(f"NICK justinfan{secrets.randbelow(900000) + 100000}\r\n".encode())
            sock.sendall(f"JOIN #{channel}\r\n".encode())
            print(f"Listening to #{channel} for 60 seconds...")
            deadline = time.monotonic() + 60
            pending = ""
            while time.monotonic() < deadline:
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                if not data:
                    break
                pending += data.decode("utf-8", errors="replace")
                lines = pending.split("\r\n")
                pending = lines.pop()
                for line in lines:
                    if line.startswith("PING "):
                        sock.sendall(("PONG " + line[5:] + "\r\n").encode())
                    elif " PRIVMSG " in line:
                        prefix, message = line.split(" PRIVMSG ", 1)
                        sender = prefix.split("!", 1)[0].lstrip(":")
                        message = message.split(" :", 1)[-1]
                        timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
                        print(f"[{timestamp}] {sender}: {message}", flush=True)


if __name__ == "__main__":
    main()
