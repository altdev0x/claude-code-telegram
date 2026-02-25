"""Test whether Claude CLI honors explicit session ID resume."""

import asyncio
import os

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage
from claude_agent_sdk._internal.message_parser import parse_message

os.environ.pop("CLAUDECODE", None)

CWD = "/home/clawdbot/claude-telegram-workspace/klaus"


async def run_session(resume_id=None, continue_conv=False, prompt="say hi"):
    opts = ClaudeAgentOptions(cwd=CWD, max_turns=1)
    if resume_id:
        opts.resume = resume_id
    if continue_conv:
        opts.continue_conversation = True

    client = ClaudeSDKClient(opts)
    await client.connect()
    await client.query(prompt)
    session_id = None
    result_text = None
    async for raw in client._query.receive_messages():
        try:
            msg = parse_message(raw)
        except Exception:
            continue
        if isinstance(msg, ResultMessage):
            session_id = msg.session_id
            result_text = msg.result
            break
    await client.disconnect()
    return session_id, result_text


async def main():
    # 1. Create session A
    sid_a, _ = await run_session(
        prompt="Remember: the secret word is BANANA. Respond: OK BANANA."
    )
    print(f"Session A: {sid_a}")

    # 2. Create session B (fresh, same dir)
    sid_b, _ = await run_session(
        prompt="Remember: the secret word is CHERRY. Respond: OK CHERRY."
    )
    print(f"Session B: {sid_b}")
    print()

    # Test 1: resume=A + continue_conversation=True
    print("Test 1: resume=A + continue_conversation=True")
    try:
        sid, text = await run_session(
            resume_id=sid_a,
            continue_conv=True,
            prompt="What is the secret word? Reply with JUST the word.",
        )
        print(f"  Returned session: {sid}")
        print(f"  Same as A? {sid == sid_a}  Same as B? {sid == sid_b}")
        print(f"  Response: {text}")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # Test 2: resume=A only (no continue_conversation)
    print("Test 2: resume=A only (no continue_conversation)")
    try:
        sid, text = await run_session(
            resume_id=sid_a,
            continue_conv=False,
            prompt="What is the secret word? Reply with JUST the word.",
        )
        print(f"  Returned session: {sid}")
        print(f"  Same as A? {sid == sid_a}  Same as B? {sid == sid_b}")
        print(f"  Response: {text}")
    except Exception as e:
        print(f"  FAILED: {e}")
    print()

    # Test 3: continue_conversation only (no resume — should pick latest = B)
    print("Test 3: continue_conversation only (no resume)")
    try:
        sid, text = await run_session(
            resume_id=None,
            continue_conv=True,
            prompt="What is the secret word? Reply with JUST the word.",
        )
        print(f"  Returned session: {sid}")
        print(f"  Same as A? {sid == sid_a}  Same as B? {sid == sid_b}")
        print(f"  Response: {text}")
    except Exception as e:
        print(f"  FAILED: {e}")


asyncio.run(main())
