"""
工具注册系统（Tool Registry）
装饰器驱动，自动生成 OpenAI function calling schema + 自动 dispatch
"""
import inspect
import re
import typing
from typing import Dict, Any, List, Optional, Callable, get_type_hints


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}


def tool(
    name: Optional[str] = None,
    description: str = "",
    category: str = "general",
):
    """
    工具注册装饰器

    Args:
        name: 工具名称（默认使用函数名）
        description: 工具描述
        category: 工具分类（general / file / web / code / memory / agent）

    inspect.signature 会自动提取参数名、类型（通过 type hints）、默认值
    参数描述自动从 docstring 的 ``:param name: description`` 中提取
    """
    def decorator(func: Callable) -> Callable:
        nonlocal name
        if name is None:
            name = func.__name__

        # 从 docstring 提取参数描述
        param_descriptions = _extract_param_descriptions(func)

        # 从 type hints 和 inspect 提取 schema
        sig = inspect.signature(func)
        hints = get_type_hints(func) if hasattr(func, "__annotations__") else {}

        properties = {}
        required = []

        for param_name, param in sig.parameters.items():
            if param_name == "self":
                continue

            # 提取参数类型
            param_type = hints.get(param_name, str)
            json_type = _python_type_to_json_type(param_type)

            prop = {"type": json_type}

            # 注入参数描述
            if param_name in param_descriptions:
                prop["description"] = param_descriptions[param_name]

            # 如果有默认值，写入 schema
            if param.default is not inspect.Parameter.empty:
                if isinstance(param.default, str):
                    prop["default"] = param.default
                elif isinstance(param.default, (int, float, bool)):
                    prop["default"] = param.default

            properties[param_name] = prop

            # 无默认值的参数是 required
            if param.default is inspect.Parameter.empty:
                required.append(param_name)

        # 构建 OpenAI function calling schema
        schema = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                }
            }
        }
        if required:
            schema["function"]["parameters"]["required"] = required

        # 注册到全局注册表
        TOOL_REGISTRY[name] = {
            "name": name,
            "func": func,
            "description": description,
            "category": category,
            "schema": schema,
            "param_names": list(properties.keys()),
        }

        return func

    return decorator


def _python_type_to_json_type(py_type) -> str:
    """Python 类型到 JSON Schema 类型的映射"""
    type_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        list: "array",
        dict: "object",
    }
    # 处理 Optional / Union 类型（Optional[X] 是 Union[X, None]）
    origin = getattr(py_type, "__origin__", None)
    if origin is typing.Union:
        args = getattr(py_type, "__args__", [])
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            py_type = non_none[0]

    return type_map.get(py_type, "string")


def _extract_param_descriptions(func: Callable) -> Dict[str, str]:
    """从函数 docstring 中提取 ``:param name: description`` 格式的参数描述

    Supports::

        :param name: description text
        :param type name: description text (type prefix is ignored)
    """
    doc = func.__doc__
    if not doc:
        return {}

    descriptions = {}
    # Match both ":param name: desc" and ":param type name: desc"
    for m in re.finditer(r':param\s+(?:\w+\s+)?(\w+)\s*:\s*(.+)', doc):
        descriptions[m.group(1)] = m.group(2).strip()
    return descriptions


def get_tool_schemas() -> List[Dict]:
    """获取所有工具的 OpenAI function calling schema"""
    return [entry["schema"] for entry in TOOL_REGISTRY.values()]


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    """根据名称获取工具信息"""
    return TOOL_REGISTRY.get(name)


def get_tools_by_category(category: str) -> List[Dict[str, Any]]:
    """按分类获取工具列表"""
    return [e for e in TOOL_REGISTRY.values() if e["category"] == category]


def get_all_tool_names() -> List[str]:
    """获取所有工具名称列表"""
    return list(TOOL_REGISTRY.keys())
