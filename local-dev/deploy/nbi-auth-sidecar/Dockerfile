# syntax=docker/dockerfile:1.6
# -----------------------------------------------------------------------------
# Dockerfile — nbi-auth-sidecar dedicated image.
#
# Design goals
# ------------
#  * Pure stdlib Python 3.11 — ZERO pip packages.  No FastAPI, no urllib3.
#  * Ships OpenJDK 17 JRE (headless) because the corporate token-tool.jar
#    requires the JVM.  A compiler JDK is not needed; -jre-headless keeps
#    image size small (~140 MB vs ~280 MB for full JDK).
#  * Runs as non-root UID 1000 / GID 1000 (matches the Z2JK default
#    "jovyan" user's UID so writes to the user's PVC home directory
#    succeed without chmod games).
#  * Uses tini(1) as PID 1 so zombie java subprocesses (leftover from
#    killed mint attempts) are reaped.
#  * RUN python -m py_compile on every shipped .py so syntax errors fail
#    the BUILD phase instead of runtime CrashLoopBackOff.
#  * HEALTHCHECK queries 127.0.0.1:18090/healthz so pod restarts happen
#    when the Python main thread wedges.
#  * Image size target: ≤ 500 MB uncompressed.  With slim-bookworm +
#    openjdk-17-jre-headless the actual size is ~330 MB, well under budget.
#
# Build via:  build-images.sh (preferred, runs py_compile pre-check)
#         or:  docker build -f Dockerfile -t nbi-auth-sidecar:dev .
# -----------------------------------------------------------------------------

# -- Base image selection ----------------------------------------------------
# python:3.11-slim-bookworm is the Python Software Foundation's official
# image on Debian 12 "bookworm" slim variant.  Slim removes documentaion,
# man pages, and non-essential locales — ~3x smaller than the default
# "bookworm" base.  We pin a minor-version SHA-less tag; use digest pinning
# in regulated environments via ARG BASE_DIGEST.
ARG BASE_IMAGE=python:3.11-slim-bookworm
FROM ${BASE_IMAGE}

# -- Layer 1: System packages (apt) ------------------------------------------
# All apt install lines in ONE RUN block so a single apt cache layer is
# produced and we can apt-get clean + rm lists *inside* the same layer to
# avoid blowing up the image with package list metadata.
#
# Packages:
#   openjdk-17-jre-headless   JVM for token-tool.jar (IdP OIDC minting)
#   tini                     PID 1 wrapper — zombie reap + signal forward
#   ca-certificates          Trust store for corporate IdP TLS
#   curl                     HEALTHCHECK probe client (python urllib.request
#                            works too, but curl is ~2 lines in HEALTHCHECK)
RUN set -eux; \
    apt-get update; \
    apt-get upgrade --no-install-recommends -y; \
    DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        openjdk-17-jre-headless \
        tini \
        ca-certificates \
        curl \
    ; \
    update-ca-certificates; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# -- Layer 2: Runtime user ---------------------------------------------------
# Match Jupyter's historical "jovyan" UID 1000:1000 so the sidecar can
# read+write the shared PVC mounted at /home/jovyan.  We use --no-log-init
# so no lastlog bloat, create HOME with mode 0755 explicitly.
ARG SIDECAR_UID=1000
ARG SIDECAR_GID=1000
RUN set -eux; \
    groupadd --system --gid ${SIDECAR_GID} jovyan; \
    useradd  --system --uid ${SIDECAR_UID} --gid ${SIDECAR_GID} \
             --home-dir /home/jovyan --create-home --no-log-init \
             --shell /usr/sbin/nologin jovyan; \
    chmod 0755 /home/jovyan

# -- Layer 3: Install Python package and byte-compile -----------------------
# Copy ONLY the nbi_auth_sidecar/ directory — no Dockerfile, no README, no
# __pycache__ (that is excluded by .dockerignore).  Use mode 0555 root:root
# so it's non-writable at runtime (defense-in-depth — a token leak through
# the sidecar HTTP surface cannot overwrite our own code).
WORKDIR /opt/nbi-auth-sidecar
COPY --chown=root:root --chmod=0555 nbi_auth_sidecar /opt/nbi-auth-sidecar/nbi_auth_sidecar

# FAIL-FAST byte-compile every module so any syntax error aborts the build
# immediately.  python -m py_compile <dir> works because Python 3.11 treats
# a directory as "compile all .py recursively".
RUN set -eux; \
    find /opt/nbi-auth-sidecar/nbi_auth_sidecar -name '*.py' -print0 \
    | xargs -0 -n1 python3 -m py_compile; \
    echo "PY_COMPILE_ALL_OK"

# -- Layer 4: Environment defaults ------------------------------------------
# NOTE: ANTHROPIC_API_KEY and JKS passwords are intentionally UNSET here.
# They arrive at runtime via K8s envFrom on Secret nbi-llm-auth.  Baking
# secrets into the image would leak them into the registry.
ENV \
    # Disable bytecode writing at runtime — image is read-only.
    PYTHONDONTWRITEBYTECODE=1 \
    # Force unbuffered I/O so sidecar log lines arrive at kubectl logs
    # immediately even when no TTY is attached.
    PYTHONUNBUFFERED=1 \
    # Home dir matches our jovyan user; the PVC is mounted here too.
    HOME=/home/jovyan \
    # Default Python module path so ``python -m nbi_auth_sidecar`` works.
    PYTHONPATH=/opt/nbi-auth-sidecar \
    # Hard default: use JAR minter (the real one).  CI overrides to mock.
    NBI_SIDECAR_MINTER=jar \
    # Loopback-only HTTP bind.  K8s probes MUST use host: 127.0.0.1.
    NBI_SIDECAR_HTTP_HOST=127.0.0.1 \
    NBI_SIDECAR_HTTP_PORT=18090 \
    # Default STRING_OVERRIDE provider; overridden by helm values.yaml.
    NBI_CHAT_MODEL_PROVIDER=openai_compatible \
    # Default T-5m rule — override via helm for IdPs with shorter TTL.
    NBI_REFRESH_BEFORE_EXP_SEC=300 \
    # Zero-config Jupyter runtime discovery — matches Z2JK default.
    JUPYTER_RUNTIME_DIR=/home/jovyan/.local/share/jupyter/runtime \
    # Token-tool JKS file paths as mounted from Secret nbi-llm-auth.
    NBI_TOKEN_TOOL_JAR=/var/run/nbi-llm-auth/token-tool.jar \
    NBI_KEYSTORE_JKS=/var/run/nbi-llm-auth/keystore.jks \
    NBI_TRUSTSTORE_JKS=/var/run/nbi-llm-auth/aitruststore.jks \
    # Java subprocess stderr redactor level — keep secret strings out of logs.
    NBI_LOG_LEVEL=INFO

# -- Layer 5: Runtime security posture --------------------------------------
# Switch to non-root NOW (after the ROOT-owned COPY above so COPY chowns
# cleanly at build time).  All instructions below run as UID 1000.
USER 1000:1000
WORKDIR /home/jovyan

# HEALTHCHECK queries /healthz using curl (std-lib would require more lines).
# Interval=20s is K8s-friendly — not too chatty but fast enough to notice
# a wedged main thread.  start-period=120s lets bootstrap run its full
# 90s budget without pre-failure HC counts against restart count.
HEALTHCHECK --interval=20s --timeout=5s --start-period=120s --retries=4 \
    CMD curl --fail --silent --max-time 3 \
        http://127.0.0.1:${NBI_SIDECAR_HTTP_PORT}/healthz > /dev/null || exit 1

# ENTRYPOINT uses tini as PID 1 so signals reach Python cleanly and
# defunct java subprocesses are reaped.  CMD invokes the package as
# ``python3 -m nbi_auth_sidecar`` — any runtime args (none today) can be
# appended by overriding `command` in K8s container spec.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "nbi_auth_sidecar"]

# ---------------------------------------------------------------------------
# Labels (optional; informative for `docker inspect` output)
# ---------------------------------------------------------------------------
LABEL \
    org.opencontainers.image.title="nbi-auth-sidecar" \
    org.opencontainers.image.description="AI Factory token mint + NBI config writer + local HTTP probes" \
    org.opencontainers.image.licenses="Proprietary" \
    org.opencontainers.image.vendor="SCB GDP Platform Team" \
    org.opencontainers.image.source="https://dev.azure.com/scbdev/notebook-intelligence"
