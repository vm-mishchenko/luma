"""E2E tests for `luma chat` using a real PTY."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pexpect


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"


def _spawn_chat(timeout: int = 8) -> pexpect.spawn:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{SRC_DIR}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else str(SRC_DIR)
    )
    child = pexpect.spawn(
        sys.executable,
        ["-m", "cli", "chat"],
        cwd=str(REPO_ROOT),
        env=env,
        encoding="utf-8",
        timeout=timeout,
    )
    child.expect(r"luma chat \(Ctrl\+D to exit\)")
    child.expect("luma> ")
    return child


def _assert_exit_zero(child: pexpect.spawn) -> None:
    child.expect(pexpect.EOF)
    child.close()
    assert child.exitstatus == 0


def test_welcome_line_printed():
    child = _spawn_chat()
    child.sendline("/exit")
    _assert_exit_zero(child)


def test_message_streams_hardcoded_response_and_reprompts():
    child = _spawn_chat()
    child.sendline("hello")
    child.expect(r"I'm Luma assistant\. I can help you find events\.")
    child.expect("luma> ")
    child.sendline("/exit")
    _assert_exit_zero(child)


def test_exit_command_exits_cleanly():
    child = _spawn_chat()
    child.sendline("/exit")
    _assert_exit_zero(child)


def test_quit_command_exits_cleanly():
    child = _spawn_chat()
    child.sendline("/quit")
    _assert_exit_zero(child)


def test_empty_input_just_reprompts():
    child = _spawn_chat()
    child.sendline("")
    child.expect("luma> ")
    assert "Luma assistant" not in child.before
    child.sendline("/exit")
    _assert_exit_zero(child)


def test_ctrl_d_at_prompt_exits_cleanly():
    child = _spawn_chat()
    child.sendeof()
    _assert_exit_zero(child)


def test_ctrl_c_during_streaming_stays_in_repl():
    child = _spawn_chat()
    child.sendline("interrupt me")
    child.expect(r"I'm ")
    child.sendintr()
    child.expect("luma> ")
    child.sendline("/exit")
    _assert_exit_zero(child)
