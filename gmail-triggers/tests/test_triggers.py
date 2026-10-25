from gmail_triggers.config import MAX_LISTED_MESSAGES, MAX_RULE_PROMPT
from gmail_triggers.store import Held, Rule
from gmail_triggers.triggers import BLOCK_END, MAX_REQUEST_PROMPT, UNTRUSTED_NOTE, request_prompt

from .conftest import AGENT

LONG_SENDER = "a" * 64 + "@" + "b" * 250 + ".example"


def rule(prompt: str) -> Rule:
    return Rule(name='Bills "quoted"\nname', label_ids=["INBOX"], agent_id=AGENT, prompt=prompt)


def test_the_block_end_survives_a_long_prompt_and_hostile_subjects():
    items = [Held(f"18c{i:013x}", LONG_SENDER, '"' * 120) for i in range(MAX_LISTED_MESSAGES)]
    text = request_prompt(rule("x" * MAX_RULE_PROMPT), items)
    assert len(text) <= MAX_REQUEST_PROMPT
    assert text.endswith(UNTRUSTED_NOTE + "\n" + BLOCK_END)
    assert text.startswith("x" * MAX_RULE_PROMPT + "\n\n")
    listed = [line for line in text.splitlines() if line.startswith("- Gmail message id")]
    assert 0 < len(listed) < MAX_LISTED_MESSAGES
    assert f"- and {MAX_LISTED_MESSAGES - len(listed)} more; search Gmail to find them." in text
    # Senders are cut for display; quotes stay escaped inside their JSON string.
    assert LONG_SENDER not in text and all(len(line) < 500 for line in listed)


def test_the_block_end_survives_even_an_oversized_prompt():
    items = [Held("18c0000000000001", LONG_SENDER, "hi")] * 3
    text = request_prompt(rule("y" * 20000), items)
    assert len(text) <= MAX_REQUEST_PROMPT and text.endswith(BLOCK_END)
    assert "- and 3 more" in text


def test_short_prompts_list_every_message():
    items = [Held(f"18c{i:013x}", "hal@example.com", "Invoice") for i in range(3)]
    text = request_prompt(rule("Note it."), items)
    assert text.count("- Gmail message id") == 3 and "more;" not in text


def test_test_runs_carry_no_note_about_emails():
    text = request_prompt(rule("Note it."), [], test=True)
    assert "(test run, no email)" in text and text.endswith(BLOCK_END) and UNTRUSTED_NOTE not in text
