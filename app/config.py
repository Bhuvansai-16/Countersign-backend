import json
import os
from dataclasses import dataclass, field
from typing import Mapping

REQUIRED = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "DATABASE_URL",
    "MERCHANT_SIGNING_KEY",
    "ADMIN_PASSWORD",
    "SESSION_SECRET",
)


@dataclass(frozen=True)
class Settings:
    paypal_client_id: str
    paypal_client_secret: str
    database_url: str
    merchant_signing_key: str
    admin_password: str
    session_secret: str
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    paypal_webhook_id: str = ""
    paypal_sandbox_buyers: list = field(default_factory=list)
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_fast_model: str = ""
    llm_max_tokens: int = 8192  # reasoning models need room for hidden reasoning plus the answer
    mcp_agent_keys: dict = field(default_factory=dict)  # api key -> agent id
    frontend_origin: str = "http://localhost:3000"
    public_base_url: str = "http://localhost:8000"
    simulator_enabled: bool = False
    allow_simulated_webhooks: bool = False
    demo_enabled: bool = False
    demo_dispute_pool: tuple = ()
    key_id: str = "k1"
    merchant_name: str = "Maya's Loom (sandbox)"


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() == "true"


def _agent_keys(raw: str) -> dict:
    keys = {}
    for pair in filter(None, (p.strip() for p in raw.split(","))):
        agent_id, _, key = pair.partition(":")
        keys[key] = agent_id
    return keys


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise RuntimeError("Missing required env vars: " + ", ".join(missing))
    g = env.get
    return Settings(
        paypal_client_id=env["PAYPAL_CLIENT_ID"],
        paypal_client_secret=env["PAYPAL_CLIENT_SECRET"],
        database_url=env["DATABASE_URL"],
        merchant_signing_key=env["MERCHANT_SIGNING_KEY"],
        admin_password=env["ADMIN_PASSWORD"],
        session_secret=env["SESSION_SECRET"],
        paypal_base_url=g("PAYPAL_BASE_URL") or Settings.paypal_base_url,
        paypal_webhook_id=g("PAYPAL_WEBHOOK_ID", ""),
        paypal_sandbox_buyers=json.loads(g("PAYPAL_SANDBOX_BUYERS") or "[]"),
        llm_base_url=g("LLM_BASE_URL", ""),
        llm_api_key=g("LLM_API_KEY", ""),
        llm_model=g("LLM_MODEL", ""),
        llm_fast_model=g("LLM_FAST_MODEL") or g("LLM_MODEL", ""),
        llm_max_tokens=int(g("LLM_MAX_TOKENS") or Settings.llm_max_tokens),
        mcp_agent_keys=_agent_keys(g("MCP_AGENT_KEYS", "")),
        frontend_origin=g("FRONTEND_ORIGIN") or Settings.frontend_origin,
        public_base_url=(g("PUBLIC_BASE_URL") or Settings.public_base_url).rstrip("/"),
        simulator_enabled=_flag(env, "SIMULATOR_ENABLED"),
        allow_simulated_webhooks=_flag(env, "ALLOW_SIMULATED_WEBHOOKS"),
        demo_enabled=_flag(env, "DEMO_ENABLED"),
        demo_dispute_pool=tuple(filter(None, (p.strip() for p in g("DEMO_DISPUTE_POOL", "").split(",")))),
    )
