from __future__ import annotations

import importlib
from pathlib import Path

from graphblocks import ContentPart


ROOT = Path(__file__).parents[1]


def test_conversation_package_exposes_turn_transaction_contract(monkeypatch) -> None:
    graphblocks_conversation = importlib.import_module("graphblocks.conversation")

    store = graphblocks_conversation.InMemoryConversationStore()
    store.create(graphblocks_conversation.Conversation(conversation_id="conv-1"))
    turn = store.begin_turn("conv-1", expected_revision=0, turn_id="turn-1")

    draft = store.append_turn_message(
        turn.turn_id,
        graphblocks_conversation.Message(
            message_id="msg-assistant",
            role="assistant",
            parts=(ContentPart(kind="text", text="draft answer"),),
        ),
    )
    completed = store.commit_turn(turn.turn_id)

    assert draft.messages[0].status == "draft"
    assert completed.status == "completed"
    assert completed.committed_revision == 1
    assert store.get("conv-1").conversation.messages[0].status == "committed"
