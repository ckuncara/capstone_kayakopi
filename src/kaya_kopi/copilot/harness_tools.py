"""Tool-calling loop for the OCR harness."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool


def run_tool_chat(
    model,
    tools: Sequence[BaseTool],
    messages: list[BaseMessage],
    before_call: Callable[[], object],
    on_response: Callable[[AIMessage, list[BaseMessage]], None],
    on_tool: Callable[[str, str], None],
    max_model_calls: int = 8,
    shared_price_scope: bool = False,
    tool_config: RunnableConfig | None = None,
) -> tuple[AIMessage, list[BaseMessage]]:
    """Run a bounded model/tool loop and return the final answer and turn messages."""
    bound_model = model.bind_tools(tools)
    tools_by_name = {item.name: item for item in tools}
    conversation = list(messages)
    turn: list[BaseMessage] = []
    for _ in range(max_model_calls):
        before_call()
        response = bound_model.invoke(conversation)
        on_response(response, conversation)
        if response.invalid_tool_calls:
            raise RuntimeError("The model returned an invalid tool call.")
        conversation.append(response)
        turn.append(response)
        if not response.tool_calls:
            return response, turn
        for call in response.tool_calls:
            tool = tools_by_name.get(call["name"])
            if tool is None:
                raise RuntimeError(f"Unknown tool requested: {call['name']}")
            args = dict(call["args"])
            price_tool = (
                call["name"] == "assess_kopi_price_change_sql"
                or (call["name"] == "analyze_pos_sql" and args.get("analysis") == "observed_prices")
            )
            removed_filter = shared_price_scope and price_tool and bool(args.get("outlet_codes"))
            if shared_price_scope and price_tool:
                args["outlet_codes"] = []
            result = tool.invoke(args, config=tool_config)
            content = result if isinstance(result, str) else json.dumps(result, default=str)
            if removed_filter:
                content = "Outlet filter ignored: price analysis covers all outlets.\n" + content
            tool_message = ToolMessage(content=content, tool_call_id=call["id"], name=tool.name)
            conversation.append(tool_message)
            turn.append(tool_message)
            on_tool(tool.name, content)
    raise RuntimeError("The tool-calling limit was reached before an answer was produced.")
