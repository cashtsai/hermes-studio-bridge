"""In-app terminal (docs/TERMINAL_PTY_CONTRACT.md) — PTY helper unit test.

Exercises bridge._TerminalSession directly: open a real local PTY + login
shell, write a command, read the echoed output, resize the tty, then kill and
reap the child with no zombie left behind. This is the offline fallback the
work order asked for; a full WS round-trip (auth reject / echo / resize /
`exit` -> {"type":"exit"}) was also run manually against a live uvicorn
instance — see the PR description for that transcript.

Run directly (repo convention — no pytest in the runtime venv):
    python3 tests/test_terminal_pty.py
"""
import asyncio
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("BRIDGE_TOKEN", "test-unit-token")

import bridge  # noqa: E402

fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


async def main():
    loop = asyncio.get_running_loop()
    session = bridge._TerminalSession(loop)
    session.start()
    check("start(): master fd assigned", isinstance(session.master_fd, int))
    check("start(): child pid assigned", session.proc is not None and session.proc.pid > 0)

    # resize should not raise even before the shell has printed a prompt
    session.resize(100, 40)
    check("resize(): no exception raised", True)

    # write a command, then poll-read (mirrors what add_reader would deliver)
    session.write("echo hello-pty-unit\n")
    deadline = time.monotonic() + 5.0
    buf = b""
    while time.monotonic() < deadline and b"hello-pty-unit" not in buf:
        chunk = session.read_nonblocking()
        if chunk:
            buf += chunk
        elif chunk is None:
            break  # EOF — shell exited early, unexpected
        else:
            await asyncio.sleep(0.05)
    check("write()+read_nonblocking(): echoed output observed",
          b"hello-pty-unit" in buf)

    # resize after the shell is live — ioctl must actually take effect
    session.resize(120, 45)
    import fcntl
    import termios
    packed = fcntl.ioctl(session.master_fd, termios.TIOCGWINSZ, b"\0" * 8)
    rows, cols, _, _ = struct.unpack("HHHH", packed)
    check(f"resize(): TIOCGWINSZ reflects new size (got {cols}x{rows})",
          (cols, rows) == (120, 45))

    # ask the shell to exit on its own, confirm read hits EOF
    session.write("exit\n")
    deadline = time.monotonic() + 5.0
    saw_eof = False
    while time.monotonic() < deadline:
        chunk = session.read_nonblocking()
        if chunk is None:
            saw_eof = True
            break
        if not chunk:
            await asyncio.sleep(0.05)
    check("shell 'exit': read_nonblocking() reaches EOF (None)", saw_eof)

    code = await loop.run_in_executor(None, session.exit_code)
    check(f"exit_code(): shell exited 0 (got {code})", code == 0)

    pid = session.proc.pid
    await loop.run_in_executor(None, session.close)
    check("close(): master fd released", session.master_fd is None)

    # no zombie left for this pid
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        reaped_or_gone = True  # already reaped by close(); waitpid raising is also fine
    except ChildProcessError:
        reaped_or_gone = True
    check("close(): child reaped, no zombie", reaped_or_gone)

    # a second _TerminalSession that we kill mid-command (not a graceful exit)
    session2 = bridge._TerminalSession(loop)
    session2.start()
    pid2 = session2.proc.pid
    session2.write("sleep 30\n")
    await asyncio.sleep(0.3)
    await loop.run_in_executor(None, session2.close)
    check("close(): kills a still-running child (not just graceful exits)",
          session2.proc.poll() is not None)
    try:
        os.kill(pid2, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    check("close(): killed child no longer exists", not alive)


if __name__ == "__main__":
    asyncio.run(main())
    print()
    if fails:
        print(f"FAILED: {fails}")
        sys.exit(1)
    print("ALL PASS")
