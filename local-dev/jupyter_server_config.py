# Jupyter Server config for local-dev stack (JupyterLab 4.5.9 + NBI).
# Loaded when JUPYTER_CONFIG_DIR points at this directory's parent... actually
# this file is copied/symlinked into the runtime config dir by start-local-stack.sh.

c = get_config()  # noqa: F821

# Hide SaaS providers so local testing always uses the sidecar path.
c.NotebookIntelligence.disabled_providers = [
    "github-copilot",
    "ollama",
    "litellm-compatible",
]

# Optional locks (uncomment if you want Settings to be read-only for models):
# import os
# os.environ.setdefault("NBI_CHAT_MODEL_PROVIDER", "openai-compatible")
# os.environ.setdefault("NBI_CHAT_MODEL_ID", "openai-compatible-chat-model")
# os.environ.setdefault("NBI_INLINE_COMPLETION_MODEL_PROVIDER", "openai-compatible")
# os.environ.setdefault("NBI_INLINE_COMPLETION_MODEL_ID", "openai-compatible-inline-completion-model")
