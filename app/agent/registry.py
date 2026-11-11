"""Реестр инструментов агента.

Объявление инструмента:

    class MyArgs(BaseModel):
        topic: str = Field(description="Тема")

    @tool("my_tool", "Что делает инструмент — это читает LLM")
    async def my_tool(ctx: AgentContext, args: MyArgs) -> dict:
        ...

JSON-схема параметров генерируется из pydantic-модели. Модель аргументов берётся
из аннотации второго параметра функции (или передаётся явно: `@tool(..., args=MyArgs)`).
Инструмент возвращает dict / list / str — реестр сериализует результат в JSON.
"""

from __future__ import annotations

import json
import logging
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

ToolFunc = Callable[[Any, Any], Awaitable[Any]]


class EmptyArgs(BaseModel):
    """Для инструментов без параметров."""


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    func: ToolFunc

    def schema(self) -> dict[str, Any]:
        """Описание инструмента в формате OpenAI function tools."""
        params = _clean_schema(self.args_model.model_json_schema())
        params.setdefault("properties", {})
        params["type"] = "object"
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Инструмент {spec.name!r} уже зарегистрирован")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [self._tools[name].schema() for name in self.names()]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    async def call(self, name: str, raw_args: dict[str, Any] | None, ctx: Any) -> str:
        """Валидирует аргументы, вызывает инструмент, возвращает JSON-строку для LLM.

        Ошибки не пробрасываются, а возвращаются LLM как {"error": ...}, чтобы агент
        мог исправиться или объяснить пользователю.
        """
        spec = self._tools.get(name)
        if spec is None:
            return _dump({"error": f"Неизвестный инструмент: {name}"})
        try:
            args = spec.args_model.model_validate(raw_args or {})
        except ValidationError as exc:
            return _dump({"error": f"Неверные аргументы: {exc.errors(include_url=False)}"})
        try:
            result = await spec.func(ctx, args)
        except NotImplementedError as exc:
            return _dump(
                {
                    "status": "in_development",
                    "message": str(exc) or f"Функция «{name}» пока в разработке.",
                }
            )
        except Exception as exc:
            log.exception("Ошибка инструмента %s", name)
            return _dump({"error": f"{type(exc).__name__}: {exc}"})
        return result if isinstance(result, str) else _dump(result)


default_registry = ToolRegistry()


def tool(
    name: str,
    description: str,
    *,
    args: type[BaseModel] | None = None,
    registry: ToolRegistry | None = None,
) -> Callable[[ToolFunc], ToolFunc]:
    """Декоратор: регистрирует функцию как инструмент агента."""

    def decorator(func: ToolFunc) -> ToolFunc:
        args_model = args or _infer_args_model(func)
        (registry or default_registry).register(
            ToolSpec(name=name, description=description, args_model=args_model, func=func)
        )
        return func

    return decorator


def _infer_args_model(func: ToolFunc) -> type[BaseModel]:
    hints = typing.get_type_hints(func)
    hints.pop("return", None)
    params = list(hints.values())
    if len(params) >= 2 and isinstance(params[1], type) and issubclass(params[1], BaseModel):
        return params[1]
    return EmptyArgs


def _clean_schema(schema: Any) -> Any:
    """Убирает служебные "title" — они только тратят токены."""
    if isinstance(schema, dict):
        return {
            k: _clean_schema(v)
            for k, v in schema.items()
            if not (k == "title" and isinstance(v, str))
        }
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)
