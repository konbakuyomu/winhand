"""A deliberately awkward interactive program used by the tests.

Commands at the `fake> ` prompt:
  echo <text>     print text
  secret          ask for a password (no echo)
  auth            print a browser sign-in request and wait for Enter
  confirm         ask "Continue? [y/N]"
  more            show a pager prompt and wait for a key
  sleep <s>       stay silent, then print "woke"
  spam <n>        print n numbered lines quickly
  tui             full-screen menu driven by arrow keys, Enter selects
  exit [code]     quit
"""

from __future__ import annotations

import getpass
import sys
import time

WIN = sys.platform == "win32"


def out(text: str = "", end: str = "\n") -> None:
    sys.stdout.write(text + end)
    sys.stdout.flush()


def read_key() -> str:
    if WIN:
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down"}.get(msvcrt.getwch(), "?")
        return "enter" if ch == "\r" else ch
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        seq = sys.stdin.read(2)
        return {"[A": "up", "[B": "down"}.get(seq, "?")
    return "enter" if ch in ("\r", "\n") else ch


def tui() -> None:
    items = ["Alpha", "Bravo", "Charlie", "Delta"]
    sel = 0
    raw_state = None
    if not WIN:
        import termios
        import tty

        fd = sys.stdin.fileno()
        raw_state = termios.tcgetattr(fd)
        tty.setraw(fd)
    try:
        while True:
            sys.stdout.write("\x1b[2J\x1b[H")
            sys.stdout.write("  Fake Config Menu\r\n\r\n")
            for i, item in enumerate(items):
                line = f"   [{'*' if i == sel else ' '}] {item}".ljust(30)
                sys.stdout.write(("\x1b[7m" + line + "\x1b[0m" if i == sel else line) + "\r\n")
            sys.stdout.write("\r\n  <Enter> select   arrows move")
            sys.stdout.flush()
            key = read_key()
            if key == "up":
                sel = max(0, sel - 1)
            elif key == "down":
                sel = min(len(items) - 1, sel + 1)
            elif key == "enter":
                break
    finally:
        if raw_state is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, raw_state)
    sys.stdout.write("\x1b[2J\x1b[H")
    out(f"selected {items[sel]}")


def main() -> None:
    out("fake app ready")
    while True:
        try:
            line = input("fake> ")
        except EOFError:
            return
        except KeyboardInterrupt:
            out("^C")
            continue
        cmd, _, arg = line.strip().partition(" ")
        if cmd == "echo":
            out(arg)
        elif cmd == "secret":
            value = getpass.getpass("Password: ")
            out(f"got {len(value)} chars")
        elif cmd == "auth":
            out("Authenticate your account at:")
            out("https://example.com/auth/cli/abc123")
            input("Press ENTER to open in the browser...")
            out("authenticated")
        elif cmd == "confirm":
            answer = input("Continue? [y/N] ")
            out("confirmed" if answer.strip().lower() == "y" else "declined")
        elif cmd == "more":
            out("line 1\nline 2")
            input("--More--")
            out("line 3")
        elif cmd == "sleep":
            time.sleep(float(arg or 1))
            out("woke")
        elif cmd == "spam":
            for i in range(int(arg or 100)):
                out(f"row {i}")
        elif cmd == "tui":
            tui()
        elif cmd == "exit":
            sys.exit(int(arg or 0))
        elif cmd:
            out(f"unknown: {cmd}")


if __name__ == "__main__":
    main()
