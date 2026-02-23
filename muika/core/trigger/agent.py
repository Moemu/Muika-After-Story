from typing import Any, Dict, List, Optional

from nonebot import logger
from pydantic import AliasChoices, BaseModel, Field, TypeAdapter

from muika.core.state import MuikaState
from muika.core.trigger.intents import BaseIntent, Intent
from muika.llm import ModelRequest, load_model


class TriggerAction(BaseModel):
    intent_name: str = Field(
        ...,
        validation_alias=AliasChoices("intent_name", "intent"),
        description="The name of the intent to trigger.",
    )
    intent_args: Dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("intent_args", "args", "arguments", "params"),
        description="The arguments for the intent.",
    )


class TriggerResult(BaseModel):
    thought: str = Field("", description="Optional thought process on whether an action is needed.")
    action: Optional[TriggerAction] = Field(
        None, description="The action to trigger, if any. If no action is needed, leave this null."
    )


class TriggerAgent:
    def __init__(self):
        self.model = load_model()
        self.adapter = TypeAdapter(TriggerResult)
        logger.debug("TriggerAgent initialized.")

    def _get_available_intents(self) -> List[type[BaseIntent]]:
        intents = BaseIntent.__subclasses__()
        logger.debug(f"TriggerAgent discovered {len(intents)} intent classes.")
        return intents

    def _get_intent_descriptions(self) -> str:
        descriptions = ["Available Actions:"]
        for intent_class in self._get_available_intents():
            if "name" not in intent_class.model_fields:
                continue
            name = intent_class.model_fields["name"].default

            doc = intent_class.__doc__ or "No description."
            args = []
            for field_name, field_info in intent_class.model_fields.items():
                if field_name in ["name", "confidence", "reason", "persistence", "missed_cycles", "failure_count"]:
                    continue

                annotation = field_info.annotation
                type_name = getattr(annotation, "__name__", str(annotation))
                args.append(f"{field_name}: {type_name}")

            args_str = ", ".join(args)
            descriptions.append(f"- {name}({args_str}): {doc}")

        return "\n".join(descriptions)

    async def trigger(self, query: str, reply: str, state: MuikaState) -> Optional[Intent]:
        """
        Run the trigger agent to decide if an action is needed based on the conversation.
        """
        logger.info(f"TriggerAgent started. query_length={len(query)}, reply_length={len(reply)}")
        system_prompt = (
            "You are the Action Trigger Agent for Muika. Your job is to decide if a physical action "
            "needs to be taken based on the user's query and Muika's reply.\n"
            "You have access to several actions.\n"
            "If an action is needed, output an `action` with the intent name and arguments.\n"
            "If no action is needed, leave `action` null.\n"
            "Do NOT answer the user directly. Only output the action decision.\n"
            f"{self._get_intent_descriptions()}"
        )

        prompt = (
            f"User Query: {query}\nMuika's Reply: {reply}\n\n"
            "Does this conversation imply that an action should be taken?"
        )

        request = ModelRequest(
            prompt=prompt,
            system=system_prompt,
            format="json",
            json_schema=self.adapter,
        )

        completions = await self.model.ask(request)
        if not completions.succeed:
            logger.error(f"Trigger Agent failed: {completions.text}")
            return None

        try:
            from muika.llm.utils.json_utils import extract_json_from_text
            from muika.llm.utils.thought_processor import general_processor

            _, result_text = general_processor(completions.text)
            obj = extract_json_from_text(result_text)
            result = self.adapter.validate_python(obj)
        except Exception as e:
            logger.error(f"Trigger Agent JSON parse error: {e}")
            return None

        logger.debug(f"Trigger Agent Thought: {result.thought}")

        if not result.action:
            logger.info("TriggerAgent decided no intent should be triggered.")
            return None

        intent_name = result.action.intent_name
        logger.info(f"TriggerAgent proposed intent: {intent_name}")
        intent_class = None
        for cls in self._get_available_intents():
            if "name" in cls.model_fields and cls.model_fields["name"].default == intent_name:
                intent_class = cls
                break
        if not intent_class:
            logger.warning(f"Trigger Agent intent class not found: {intent_name}")
            return None

        try:
            intent_instance = intent_class(confidence=1.0, **result.action.intent_args)
            logger.info(f"TriggerAgent created intent instance: {intent_name}")
            return intent_instance  # type: ignore
        except Exception as e:
            logger.error(f"Trigger Agent error creating intent: {e}")
            return None
