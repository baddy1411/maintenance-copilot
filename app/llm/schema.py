from typing import Any

from pydantic import BaseModel


def strict_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema -> OpenAI "strict" form: every object closed and every property required
    (optional fields stay nullable). GPT-6 Luna rejects the default form (SPIKE §1)."""
    schema = model_cls.model_json_schema()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            node.pop("default", None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema
