from __future__ import annotations

from types import SimpleNamespace

from bot.handlers import text as text_handlers
from bot.handlers._common import reset_dialogue
from bot.utils import db
from bot.utils.anthropic import chat
from tests.conftest import FakeClient, make_message

CHAT_ID = 555
HISTORY = [
    {"role": "user", "content": "Я Оля, у меня аллергия на орехи"},
    {"role": "assistant", "content": "Запомнила!"},
]


def facts_response(text: str):
    return (None, make_message([SimpleNamespace(type="text", text=text)]))


def test_summary_prompt_keeps_previous_facts_and_formats_cleanly():
    prompt = chat.SUMMARY_PROMPT_TEMPLATE.format(
        session_note=chat.SUMMARY_TIMEOUT_NOTE, prev_summary="Имя: Оля", dialogue="Пользователь: привет",
    )
    assert "Все факты о пользователе из предыдущего саммари перенеси" in prompt
    assert "больше 72 часов" in prompt
    assert "{" not in prompt and "}" not in prompt


async def test_reset_keeps_facts_and_clears_history(config, db_path):
    await db.init_db()
    await db.save_history(CHAT_ID, HISTORY, total_tokens=500, summary="старое саммари")
    client = FakeClient([facts_response("Факты о пользователе:\n- Оля\n- аллергия на орехи")])

    kept = await reset_dialogue(client, config, CHAT_ID, CHAT_ID)

    assert kept is True
    history = await db.get_history(CHAT_ID)
    assert history["messages_json"] == "[]"
    assert "аллергия на орехи" in history["summary"]
    # Модели передали и прошлое саммари, и свежую историю
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "старое саммари" in prompt and "аллергия на орехи" in prompt
    assert (await db.get_user_stats(CHAT_ID))["total_requests"] == 1


async def test_reset_without_facts_clears_everything(config, db_path):
    await db.init_db()
    await db.save_history(CHAT_ID, HISTORY, total_tokens=500, summary="саммари")
    client = FakeClient([facts_response(f"{chat.NO_FACTS}.")])

    assert await reset_dialogue(client, config, CHAT_ID, CHAT_ID) is False
    history = await db.get_history(CHAT_ID)
    assert history["summary"] is None and history["messages_json"] == "[]"


async def test_reset_with_empty_memory_skips_model_call(config, db_path):
    await db.init_db()
    client = FakeClient([])
    assert await reset_dialogue(client, config, CHAT_ID, CHAT_ID) is False
    assert client.messages.calls == []


async def test_reset_failure_keeps_memory(config, db_path):
    await db.init_db()
    await db.save_history(CHAT_ID, HISTORY, total_tokens=500, summary="саммари")
    client = FakeClient([(None, make_message([], stop_reason="refusal"))])

    try:
        await reset_dialogue(client, config, CHAT_ID, CHAT_ID)
    except RuntimeError:
        pass
    history = await db.get_history(CHAT_ID)
    assert history["summary"] == "саммари"
    assert "аллергия" in history["messages_json"]


class FakeCallbackMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=CHAT_ID)
        self.text = None

    async def edit_text(self, text, **kwargs):
        self.text = text


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.message = FakeCallbackMessage()

    async def answer(self, *args, **kwargs):
        pass


async def test_kill_wipes_memory_only_after_confirmation(db_path):
    await db.init_db()
    await db.save_history(CHAT_ID, HISTORY, total_tokens=500, summary="саммари")

    await text_handlers.cb_kill(FakeCallback("kill:no"))
    assert (await db.get_history(CHAT_ID))["summary"] == "саммари"

    await text_handlers.cb_kill(FakeCallback("kill:yes"))
    history = await db.get_history(CHAT_ID)
    assert history["summary"] is None and history["messages_json"] == "[]"
