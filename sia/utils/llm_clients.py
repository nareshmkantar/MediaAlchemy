"""
LLM Client Wrappers - Unified interface for multiple LLM providers.
Provides Gemini-compatible generate_content interface.
"""
import logging
import json


try:
    from openai import OpenAI, AzureOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

logger = logging.getLogger(__name__)


def _is_temperature_restricted_model(model_name: str) -> bool:
    """GPT-5 chat deployments reject temperature and should omit it upfront."""
    model_lower = (model_name or "").lower()
    return (
        "gpt-5" in model_lower
        or "gpt5" in model_lower
    )


def _extract_response_text(response: object) -> str:
    """Return assistant text when present, otherwise serialize first choice for debugging."""
    try:
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text_parts = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                    text_parts.append(str(part.get("text")))
                elif hasattr(part, "type") and getattr(part, "type", None) == "text" and getattr(part, "text", None):
                    text_parts.append(str(getattr(part, "text")))
            if text_parts:
                return "\n".join(text_parts)

        finish_reason = getattr(choice, "finish_reason", None)
        try:
            if hasattr(choice, "model_dump"):
                payload = choice.model_dump()
            elif hasattr(choice, "dict"):
                payload = choice.dict()
            else:
                payload = {
                    "finish_reason": finish_reason,
                    "message": getattr(message, "model_dump", lambda: None)() if hasattr(message, "model_dump") else str(message),
                }
        except Exception:
            payload = {
                "finish_reason": finish_reason,
                "message": str(message),
            }

        return json.dumps({
            "note": "message.content was empty; showing first choice payload for debugging",
            "choice": payload,
        }, indent=2, ensure_ascii=False, default=str)
    except Exception:
        return ""

class OpenAILLMWrapper:
    """Wrapper for OpenAI API."""
    def __init__(self, api_key: str, model_name: str):
        if not OPENAI_AVAILABLE:
            raise ImportError("openai package not installed")
        self.client = OpenAI(api_key=api_key)
        self.model_name = model_name
    
    def generate_content(self, prompt: str, **kwargs):
        model_lower = self.model_name.lower()
        # Reasoning models or newer GPT-4o deployments might be strict
        is_reasoning_model = "o1-" in model_lower or model_lower == "o1" or "o3-" in model_lower
        is_newer_gpt = "gpt-5" in model_lower or "gpt5" in model_lower or "gpt-4o" in model_lower
        temperature_restricted = _is_temperature_restricted_model(self.model_name)
        
        call_params = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        # Initial mapping
        tokens = kwargs.get("max_completion_tokens", kwargs.get("max_tokens", 4096))
        # Initial guess on whether to use max_completion_tokens
        use_completion = is_reasoning_model or is_newer_gpt
        
        if use_completion:
            call_params["max_completion_tokens"] = tokens
        else:
            call_params["max_tokens"] = tokens
            
        if "temperature" in kwargs:
            # Only include temperature if not a known restricted model
            if not is_reasoning_model and not temperature_restricted:
                call_params["temperature"] = kwargs["temperature"]
        elif not is_reasoning_model and not temperature_restricted:
            # Default to 0.0 for deterministic output unless reasoning model
            call_params["temperature"] = 0.0
            
        for k, v in kwargs.items():
            if k not in ["max_tokens", "max_completion_tokens", "temperature", "generation_config", "model", "messages"]:
                call_params[k] = v

        # Parameter Auto-Healing Loop
        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(**call_params)
                content = _extract_response_text(response)
                return type('Response', (), {'text': content or ""})
            except Exception as e:
                err_msg = str(e).lower()
                
                # Case 1: max_tokens rejection
                if "max_completion_tokens" in err_msg and "max_tokens" in call_params:
                    logger.warning(f"Parameter Auto-Healing: Switching max_tokens -> max_completion_tokens for {self.model_name}")
                    call_params["max_completion_tokens"] = call_params.pop("max_tokens")
                    continue
                
                # Case 2: temperature rejection
                if "temperature" in err_msg and "temperature" in call_params:
                    logger.warning(f"Parameter Auto-Healing: Removing temperature for {self.model_name}")
                    call_params.pop("temperature")
                    continue

                # If no more auto-healing options or unknown error
                if attempt == 1:
                    logger.error(f"OpenAI API Error after healing attempt: {e}")
                raise e

class AzureOpenAILLMWrapper:
    """Wrapper for Azure OpenAI API with SSL fallback support."""
    def __init__(self, api_key: str, deployment_name: str, endpoint: str, api_version: str, verify_ssl: bool = True):
        if not OPENAI_AVAILABLE:
            raise ImportError("openai package not installed")
        self.api_key = api_key
        self.deployment_name = deployment_name
        self.endpoint = endpoint
        self.api_version = api_version
        self.verify_ssl = verify_ssl
        self.model_name = deployment_name
        
        if verify_ssl:
            self.client = AzureOpenAI(
                api_key=api_key,
                api_version=api_version,
                azure_endpoint=endpoint
            )
        else:
            self.client = self._get_insecure_client()

    def _get_insecure_client(self):
        import httpx
        return AzureOpenAI(
            api_key=self.api_key,
            api_version=self.api_version,
            azure_endpoint=self.endpoint,
            http_client=httpx.Client(verify=False)
        )

    def generate_content(self, prompt: str, **kwargs):
        deployment_lower = self.deployment_name.lower()
        is_reasoning_model = "o1-" in deployment_lower or deployment_lower == "o1" or "o3-" in deployment_lower
        is_newer_gpt = "gpt-5" in deployment_lower or "gpt5" in deployment_lower or "gpt-4o" in deployment_lower
        temperature_restricted = _is_temperature_restricted_model(self.deployment_name)
        
        call_params = {
            "model": self.deployment_name,
            "messages": [{"role": "user", "content": prompt}]
        }
        
        tokens = kwargs.get("max_completion_tokens", kwargs.get("max_tokens", 4096))
        use_completion = is_reasoning_model or is_newer_gpt
        
        if use_completion:
            call_params["max_completion_tokens"] = tokens
        else:
            call_params["max_tokens"] = tokens
            
        if "temperature" in kwargs:
            if not is_reasoning_model and not temperature_restricted:
                call_params["temperature"] = kwargs["temperature"]
        elif not is_reasoning_model and not temperature_restricted:
            call_params["temperature"] = 0.0

        for k, v in kwargs.items():
            if k not in ["max_tokens", "max_completion_tokens", "temperature", "generation_config", "model", "messages"]:
                call_params[k] = v

        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(**call_params)
                content = _extract_response_text(response)
                return type('Response', (), {'text': content or ""})
            except Exception as e:
                err_msg = str(e).lower()
                if "max_completion_tokens" in err_msg and "max_tokens" in call_params:
                    logger.warning(f"Parameter Auto-Healing (Azure): Switching max_tokens -> max_completion_tokens for {self.deployment_name}")
                    call_params["max_completion_tokens"] = call_params.pop("max_tokens")
                    continue
                if "temperature" in err_msg and "temperature" in call_params:
                    logger.warning(f"Parameter Auto-Healing (Azure): Removing temperature for {self.deployment_name}")
                    call_params.pop("temperature")
                    continue
                raise e
