"""A scripted chat model: replays prepared AI messages (text or tool calls) so the real graph, tools and gates run end to end in tests
without a network or a live LLM. Also records every prompt it was given."""

from __future__ import annotations

import itertools
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

_ids = itertools.count(1)


def say(text: str) -> AIMessage:
    return AIMessage(content=text)


def call(name: str, **args: Any) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}", "type": "tool_call"}]
    )


class ScriptedChatModel(BaseChatModel):
    script: list[Any] = Field(default_factory=list)
    prompts: list[Any] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self, messages: list[Any], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self.prompts.append(messages)
        if not self.script:
            raise RuntimeError("the scripted model has no more messages")
        return ChatResult(generations=[ChatGeneration(message=self.script.pop(0))])
