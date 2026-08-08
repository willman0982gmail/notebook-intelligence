# Snippet for jupyterhub_config.py (LLM-S06 / S07 / S11)
# Copy the relevant lines into your Hub config; paths assume the local-dev
# tree is installed at /opt/nbi-local-dev inside the user image (or adjust).

c = get_config()  # noqa: F821

import sys

sys.path.insert(0, "/opt/nbi-local-dev/hub")
from pre_spawn_hook import make_pre_spawn_hook  # noqa: E402

c.KubeSpawner.pre_spawn_hook = make_pre_spawn_hook(
    quota_service_url="http://nbi-quota.jhub.svc.cluster.local:8090",
)

# Sidecar then jupyterhub-singleuser (see deploy/entrypoint.sh / Dockerfile.singleuser)
c.KubeSpawner.cmd = ["/opt/nbi-local-dev/deploy/entrypoint.sh"]

# Bake NBI openai-compatible defaults in the *user image* at:
#   $CONDA_PREFIX/share/jupyter/nbi/config.json
# (see local-dev/nbi-config.local.json)

c.KubeSpawner.environment.update(
    {
        "MODE": "proxy",
        "TOKEN_PROVIDER": "jar",
        "TOKEN_JAR": "/var/run/nbi-llm-auth/token-tool.jar",
        "KEYSTORE_PATH": "/var/run/nbi-llm-auth/keystore.jks",
        "TRUSTSTORE_PATH": "/var/run/nbi-llm-auth/truststore.jks",
        "HOST": "127.0.0.1",
        "PORT": "8089",
        "QUOTA_BACKEND": "http",
        "QUOTA_SERVICE_URL": "http://nbi-quota.jhub.svc.cluster.local:8090",
        "NBI_LLM_SIDECAR_URL": "http://127.0.0.1:8089",
        "UPSTREAM_BASE_URL": "https://llm-gateway.example.com/v1",
        "NBI_CHAT_MODEL_PROVIDER": "openai-compatible",
        "NBI_CHAT_MODEL_ID": "openai-compatible-chat-model",
        "NBI_INLINE_COMPLETION_MODEL_PROVIDER": "openai-compatible",
        "NBI_INLINE_COMPLETION_MODEL_ID": "openai-compatible-inline-completion-model",
    }
)

# JAR / JKS / passwords from Secret (LLM-S06) — do not put secrets in git
c.KubeSpawner.volumes = [
    {
        "name": "nbi-llm-auth",
        "secret": {"secretName": "nbi-llm-auth", "defaultMode": 0o400},
    }
]
c.KubeSpawner.volume_mounts = [
    {
        "name": "nbi-llm-auth",
        "mountPath": "/var/run/nbi-llm-auth",
        "readOnly": True,
    }
]
# Prefer secretKeyRef via chart values; example if your chart supports envFrom:
# c.KubeSpawner.extra_container_config = {
#   "envFrom": [{"secretRef": {"name": "nbi-llm-auth"}}],
# }
