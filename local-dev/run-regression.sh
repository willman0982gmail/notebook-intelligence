#!/usr/bin/env bash
# Aggregate local regression for LLM gateway spike (LLM-S24).
# Requires Python >= 3.12 (conda env nbi-jl45 recommended).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCAL_DEV="${ROOT}/local-dev"
cd "$ROOT"

# shellcheck disable=SC1091
source "${LOCAL_DEV}/python.sh"
export PYTHON="$NBI_PYTHON"
echo "using Python: $PYTHON ($("$PYTHON" --version 2>&1))"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 12), sys.version'

echo "== unit: local-dev =="
"$PYTHON" local-dev/tests/test_local_stack.py

echo "== smoke: sidecar =="
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true
./local-dev/start-local-stack.sh --smoke-only
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true

echo "== smoke: quota deny =="
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true
QUOTA_TOKENS_DAY=50 ./local-dev/start-sidecar.sh
SMOKE_QUOTA_DENY=1 ./local-dev/smoke-test.sh
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true

echo "== probe: local mock feature matrix =="
./local-dev/start-sidecar.sh
UPSTREAM_BASE_URL=http://127.0.0.1:8089/v1 UPSTREAM_API_KEY=x \
  PROBE_LABEL=local-mock ./local-dev/probe-gateway.sh >/dev/null
"$PYTHON" - <<'PY'
import json
from pathlib import Path
p = Path("local-dev/.runtime/probe-results.json")
r = json.loads(p.read_text())
caps = r["capabilities"]
assert caps["chat_non_stream"]["status"] == "Supported"
assert caps["chat_sse_stream"]["status"] == "Supported"
assert caps["inline_fim_style"]["status"] == "Supported"
print("PASS local mock probe", p)
PY
./local-dev/merge-probe-into-matrix.sh
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true

echo "== smoke: quota HTTP backend + CSV export =="
./local-dev/stop-quota-service.sh >/dev/null 2>&1 || true
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true
rm -f local-dev/.runtime/quota-service-store.json
./local-dev/start-quota-service.sh
QUOTA_BACKEND=http QUOTA_SERVICE_URL=http://127.0.0.1:8090 \
  NBI_LLM_USER=alice NBI_LLM_GROUPS=interns ./local-dev/start-sidecar.sh
curl -fsS http://127.0.0.1:8089/v1/chat/completions \
  -H 'Content-Type: application/json' -H 'X-NBI-Feature: chat' \
  -d '{"model":"databricks/gdp-gpt4o","messages":[{"role":"user","content":"hi"}],"stream":false}' \
  >/dev/null
./local-dev/export-usage.sh --summary | tee local-dev/.runtime/usage-summary.csv
grep -q alice local-dev/.runtime/usage-summary.csv
echo "PASS quota http + usage summary"
./local-dev/stop-sidecar.sh >/dev/null 2>&1 || true
./local-dev/stop-quota-service.sh >/dev/null 2>&1 || true

echo "== tabletop chaos (S03/S18) =="
./local-dev/tabletop-chaos.sh

echo "== pytest: NBI LLM changes =="
if "$PYTHON" -c 'import pytest' 2>/dev/null; then
  "$PYTHON" -m pytest \
    tests/test_openai_compatible_llm_provider.py \
    tests/test_llm_quota_handler.py \
    -q --tb=short
else
  echo "pytest not installed in this env — installing..."
  "$PYTHON" -m pip install -q pytest
  "$PYTHON" -m pytest \
    tests/test_openai_compatible_llm_provider.py \
    tests/test_llm_quota_handler.py \
    -q --tb=short
fi

echo "== provider feature header (3.12) =="
"$PYTHON" - <<'PY'
import importlib.util, sys, types
from pathlib import Path
from unittest.mock import MagicMock, patch
root = Path('.').resolve()
api = types.ModuleType('notebook_intelligence.api')
class ChatModel:
    def __init__(self, provider): self._provider=provider; self._properties=[]
    def get_property(self, id):
        for p in self._properties:
            if p.id==id: return p
    def set_property_value(self, id, value):
        for p in self._properties:
            if p.id==id: p.value=value
class InlineCompletionModel(ChatModel): pass
class EmbeddingModel: pass
class LLMProvider:
    def __init__(self): pass
class CancelToken: pass
class ChatResponse: pass
class CompletionContext: pass
class LLMProviderProperty:
    def __init__(self, id, name, description, value='', optional=True):
        self.id=id; self.value=value
class MarkdownData:
    def __init__(self, content='', detail=None, reasoning_content=''): self.content=content
for k,v in dict(ChatModel=ChatModel,InlineCompletionModel=InlineCompletionModel,EmbeddingModel=EmbeddingModel,LLMProvider=LLMProvider,CancelToken=CancelToken,ChatResponse=ChatResponse,CompletionContext=CompletionContext,LLMProviderProperty=LLMProviderProperty,MarkdownData=MarkdownData).items():
    setattr(api,k,v)
sys.modules['notebook_intelligence']=types.ModuleType('notebook_intelligence'); sys.modules['notebook_intelligence'].__path__=[str(root/'notebook_intelligence')]
sys.modules['notebook_intelligence.api']=api
sys.modules['notebook_intelligence.llm_providers']=types.ModuleType('notebook_intelligence.llm_providers'); sys.modules['notebook_intelligence.llm_providers'].__path__=[str(root/'notebook_intelligence/llm_providers')]
spec=importlib.util.spec_from_file_location('notebook_intelligence.llm_providers.openai_compatible_llm_provider', root/'notebook_intelligence/llm_providers/openai_compatible_llm_provider.py')
mod=importlib.util.module_from_spec(spec); sys.modules[spec.name]=mod; spec.loader.exec_module(mod)
with patch('openai.OpenAI') as O:
    p=mod.OpenAICompatibleLLMProvider(); m=p.chat_models[0]
    m.set_property_value('model_id','m'); m.set_property_value('api_key','k'); m.set_property_value('base_url','http://x/v1')
    c=MagicMock(); O.return_value=c
    r=MagicMock(); r.model_dump_json.return_value='{"choices":[{"message":{"content":"ok"}}]}'
    r.choices=[MagicMock(message=MagicMock(reasoning_content=None, reasoning=None))]
    c.chat.completions.create.return_value=r
    m.completions([{'role':'user','content':'hi'}])
    assert O.call_args.kwargs['default_headers']['X-NBI-Feature']=='chat'
print('PASS provider feature header on', sys.version.split()[0])
PY

echo "ALL REGRESSION PASSED (Python $($PYTHON -c 'import sys; print("%d.%d.%d"%sys.version_info[:3])'))"
