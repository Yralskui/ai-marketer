"""Реестр инструментов: схема, валидация, вызов, обработка ошибок."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from app.agent.registry import EmptyArgs, ToolRegistry, default_registry, tool


class GreetArgs(BaseModel):
    name: str = Field(description="Кого приветствовать")
    times: int = Field(1, ge=1, le=3)


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()

    @tool("greet", "Поздороваться", registry=reg)
    async def greet(ctx, args: GreetArgs) -> dict:
        return {"text": " ".join([f"Привет, {args.name}!"] * args.times), "ctx": ctx}

    @tool("ping", "Без аргументов", registry=reg)
    async def ping(ctx, args: EmptyArgs) -> str:
        return "pong"

    @tool("broken", "Падает", registry=reg)
    async def broken(ctx, args: EmptyArgs) -> dict:
        raise RuntimeError("сломалось")

    @tool("soon", "Ещё не готово", registry=reg)
    async def soon(ctx, args: EmptyArgs) -> dict:
        raise NotImplementedError("Скоро будет")

    return reg


def test_schema_generated_from_pydantic(registry):
    schema = registry.get("greet").schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "greet"
    assert fn["description"] == "Поздороваться"
    params = fn["parameters"]
    assert params["type"] == "object"
    assert params["required"] == ["name"]
    assert params["properties"]["name"]["description"] == "Кого приветствовать"
    assert params["properties"]["times"]["maximum"] == 3
    assert "title" not in params


def test_empty_args_schema(registry):
    params = registry.get("ping").schema()["function"]["parameters"]
    assert params == {"type": "object", "properties": {}}


async def test_call_validates_and_runs(registry):
    result = json.loads(await registry.call("greet", {"name": "Урал", "times": 2}, ctx="CTX"))
    assert result == {"text": "Привет, Урал! Привет, Урал!", "ctx": "CTX"}
    assert await registry.call("ping", None, ctx=None) == "pong"


async def test_call_errors_are_returned_not_raised(registry):
    assert "error" in json.loads(await registry.call("greet", {"times": 5}, ctx=None))
    assert "error" in json.loads(await registry.call("nope", {}, ctx=None))
    assert "сломалось" in json.loads(await registry.call("broken", {}, ctx=None))["error"]
    soon = json.loads(await registry.call("soon", {}, ctx=None))
    assert soon == {"status": "in_development", "message": "Скоро будет"}


def test_duplicate_name_rejected(registry):
    with pytest.raises(ValueError):

        @tool("ping", "дубль", registry=registry)
        async def ping2(ctx, args: EmptyArgs) -> str:
            return ""


def test_default_registry_has_all_tools():
    import app.agent.tools  # noqa: F401

    expected = {
        "get_business_profile",
        "update_business_profile",
        "create_post_draft",
        "revise_post",
        "list_posts",
        "create_content_plan",
        "get_analytics",
        "make_report",
    }
    assert expected <= set(default_registry.names())
    # У агента нет и не должно быть инструмента публикации
    assert not any("publish" in name for name in default_registry.names())
