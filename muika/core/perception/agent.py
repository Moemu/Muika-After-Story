import json
from typing import List, Optional

from nonebot import logger
from pydantic import BaseModel, Field, TypeAdapter

from muika.core.perception.registry import get_tool_handler, invoke_tool
from muika.core.perception.tools import BaseTool, PerceptionTool
from muika.core.state import MuikaState
from muika.llm import ModelRequest, load_model
from muika.llm.utils.json_utils import extract_json_from_text
from muika.llm.utils.thought_processor import general_processor


class PerceptionResult(BaseModel):
    thought: str = Field("", description="Optional thought process on what to do next.")
    action: Optional[PerceptionTool] = Field(
        None,
        description="The typed tool call, if needed. If you have enough information, leave this null.",
    )
    conclusion: Optional[str] = Field(
        None,
        description="If you have gathered enough information, summarize the facts here. Otherwise, leave this null.",
    )


class PerceptionAgent:
    def __init__(self):
        self.model = load_model()
        self.adapter = TypeAdapter(PerceptionResult)
        logger.debug("PerceptionAgent initialized.")

    def _get_available_tools(self) -> List[type[BaseTool]]:
        tools = BaseTool.__subclasses__()
        logger.debug(f"PerceptionAgent discovered {len(tools)} tool classes.")
        return tools

    async def _execute_tool(self, action: PerceptionTool, state: MuikaState) -> str:
        tool_name = action.name
        logger.info(f"PerceptionAgent executing tool: {tool_name}")

        handler = get_tool_handler(tool_name)
        if not handler:
            logger.warning(f"PerceptionAgent tool handler not found: {tool_name}")
            return f"[{tool_name} result]: Handler not found."

        try:
            action_output = await invoke_tool(handler, action, state)

            if hasattr(action_output, "content"):
                logger.debug(f"PerceptionAgent tool completed: {tool_name}")
                return f"[{tool_name} result]: {action_output.content}"
            logger.debug(f"PerceptionAgent tool completed with non-standard output: {tool_name}")
            return f"[{tool_name} result]: {action_output}"
        except Exception as e:
            logger.error(f"PerceptionAgent tool execution failed ({tool_name}): {e}")
            return f"[{tool_name} error]: {e}"

    async def perceive(self, query: str, state: MuikaState, max_loops: int = 3) -> str:
        """
        Run the perception loop to gather facts.
        """
        logger.info(f"PerceptionAgent started. max_loops={max_loops}, query_length={len(query)}")
        schema = self.adapter.json_schema()
        system_prompt = (
            "You are the Perception Agent for Muika. Your job is to gather factual information "
            "from the system to help Muika answer the user's query.\n"
            "You have access to several tools. You can call one tool at a time.\n"
            "If you need information, output an `action` with the tool name and arguments.\n"
            "If you have gathered enough information, or if no tools are needed, "
            "output a `conclusion` summarizing the facts.\n"
            "Do NOT answer the user directly. Only output facts.\n"
            "Output JSON matching the schema:\n"
            f"{json.dumps(schema, indent=1)}"  # 有些 SDK 实现可能不支持 TypeAdapter 的 json_schema 方法，直接输出字符串以防万一
        )

        facts_gathered: list[str] = []

        for index in range(max_loops):
            logger.debug(f"PerceptionAgent loop {index + 1}/{max_loops}, facts_count={len(facts_gathered)}")
            prompt = f"User Query: {query}\n\n"
            if facts_gathered:
                prompt += "Facts gathered so far:\n" + "\n".join(facts_gathered) + "\n\n"
            prompt += "What should we do next?"

            request = ModelRequest(
                prompt=prompt,
                system=system_prompt,
                format="json",
                json_schema=self.adapter,
            )

            completions = await self.model.ask(request)
            if not completions.succeed:
                logger.error(f"Perception Agent failed: {completions.text}")
                break

            try:
                _, result_text = general_processor(completions.text)
                obj = extract_json_from_text(result_text)
                result = self.adapter.validate_python(obj)
            except Exception as e:
                logger.error(f"Perception Agent JSON parse error: {e}")
                break

            logger.debug(f"Perception Agent Thought: {result.thought}")

            if result.conclusion:
                logger.debug(f"Perception Agent Conclusion: {result.conclusion}")
                logger.info("PerceptionAgent finished with model conclusion.")
                return result.conclusion

            if not result.action:
                logger.debug("PerceptionAgent received no action and no conclusion, ending loop.")
                break

            tool_result = await self._execute_tool(result.action, state)
            facts_gathered.append(tool_result)

        if facts_gathered:
            logger.info(f"PerceptionAgent finished with gathered facts. facts_count={len(facts_gathered)}")
            return "\n".join(facts_gathered)
        logger.info("PerceptionAgent finished with no additional facts.")
        return "No additional facts gathered."
