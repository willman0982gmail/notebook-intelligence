from unittest.mock import MagicMock, patch

from notebook_intelligence.llm_providers.openai_compatible_llm_provider import (
    OpenAICompatibleLLMProvider,
    format_openai_compatible_error,
    sanitize_tools_for_openai_compatible,
)


def test_sanitize_tools_for_openai_compatible_removes_function_strict_without_mutating_input():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "python",
                "description": "Run python",
                "strict": True,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
    ]

    sanitized = sanitize_tools_for_openai_compatible(tools)

    assert sanitized[0]["function"].get("strict") is None
    assert tools[0]["function"]["strict"] is True


@patch("openai.OpenAI")
def test_openai_compatible_chat_model_drops_strict_before_request(mock_openai_cls):
    provider = OpenAICompatibleLLMProvider()
    model = provider.chat_models[0]
    model.set_property_value("model_id", "test-model")
    model.set_property_value("api_key", "test-key")
    model.set_property_value("base_url", "https://example.com/v1")

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_response = MagicMock()
    mock_response.model_dump_json.return_value = '{"choices": [{"message": {"content": "ok"}}]}'
    mock_response.choices = [MagicMock(message=MagicMock(reasoning_content=None, reasoning=None))]
    mock_client.chat.completions.create.return_value = mock_response

    tools = [
        {
            "type": "function",
            "function": {
                "name": "python",
                "description": "Run python",
                "strict": True,
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
    ]

    result = model.completions(messages=[{"role": "user", "content": "hi"}], tools=tools)

    assert result["choices"][0]["message"]["content"] == "ok"
    create_kwargs = mock_client.chat.completions.create.call_args.kwargs
    assert "strict" not in create_kwargs["tools"][0]["function"]
    assert tools[0]["function"]["strict"] is True
    # LLM-S22: feature tag for quota metering
    assert mock_openai_cls.call_args.kwargs["default_headers"]["X-NBI-Feature"] == "chat"


def test_format_openai_compatible_error_quota():
    class FakeExc(Exception):
        status_code = 429
        body = {
            "error": {
                "message": "daily quota exceeded",
                "type": "quota_exceeded",
                "plan": "intern",
                "reset_at": 1893456000,
            }
        }

    msg = format_openai_compatible_error(FakeExc("boom"))
    assert "quota" in msg.lower()
    assert "daily quota exceeded" in msg
    assert "plan=intern" in msg
    assert "resets" in msg.lower()


@patch("openai.OpenAI")
def test_openai_compatible_inline_sends_inline_feature_header(mock_openai_cls):
    provider = OpenAICompatibleLLMProvider()
    model = provider.inline_completion_models[0]
    model.set_property_value("model_id", "test-model")
    model.set_property_value("api_key", "test-key")
    model.set_property_value("base_url", "https://example.com/v1")

    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="```\nx\n```"))]
    mock_client.chat.completions.create.return_value = mock_resp

    cancel = MagicMock()
    cancel.is_cancel_requested = False
    out = model.inline_completions("a", "b", "python", "f.py", MagicMock(), cancel)
    assert out.strip() == "x"
    assert mock_openai_cls.call_args.kwargs["default_headers"]["X-NBI-Feature"] == "inline"
