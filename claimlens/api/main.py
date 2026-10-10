"""Build the real app from environment variables.   Run with:

    uvicorn claimlens.api.main:build_app_from_env --factory --port 8080
or: python -m claimlens serve
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from claimlens.api.app import create_app
from claimlens.api.auth import Authenticator, OIDCAuthenticator
from claimlens.logging_safe import configure_logging
from claimlens.config import get_settings
from claimlens.llm.bedrock_client import BedrockClient, LLMError
from claimlens.llm.mock import MockLLM
from claimlens.ocr.factory import build_ocr
from claimlens.service import ProcessingService
from claimlens.storage.cache import NullCache, RedisCache
from claimlens.storage.db import make_engine, make_session_factory
from claimlens.storage.idempotency import DynamoIdempotencyStore, SqlIdempotencyStore


class _NoModel:
    """Stand-in for API servers that never call a model (uploads go to S3 and a worker does the processing)."""
    model_id = "none"

    def call_tool(self, **kwargs):
        raise LLMError("model calls are disabled on this server")


def build_service(settings=None, needs_model: bool = True):
    """needs_model=False is for API servers that only mint upload tickets (a worker does the processing)."""
    settings = settings or get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))

    llm_spec = os.getenv("CLAIMLENS_LLM", "bedrock")
    if not needs_model:
        llm = classify_llm = _NoModel()
    elif llm_spec.startswith("mock"):  # local demos only
        llm = classify_llm = MockLLM(llm_spec.split(":", 1)[1] if ":" in llm_spec else "careful")
    else:
        llm = BedrockClient(settings.model_id, settings.region)
        classify_llm = BedrockClient(settings.classify_model_id, settings.region)

    ocr_spec = os.getenv("CLAIMLENS_OCR", "none")
    idem = (DynamoIdempotencyStore(settings.dynamodb_table, settings.region, settings.dynamodb_endpoint)
            if settings.idempotency_backend == "dynamodb" else SqlIdempotencyStore(session_factory))
    cache = RedisCache(settings.redis_url, settings.cache_ttl_seconds) if settings.redis_url else NullCache()
    service = ProcessingService(session_factory, llm, idem, classify_llm=classify_llm, ocr=build_ocr(ocr_spec, settings),
                                cache=cache, settings=settings, ocr_spec=ocr_spec)
    return service, session_factory, settings


def build_app_from_env() -> FastAPI:
    configure_logging()
    settings = get_settings()
    # With synchronous upload off, this server never calls a model, so it needs no model IDs and no Bedrock access.
    service, session_factory, settings = build_service(settings, needs_model=settings.sync_upload_enabled)
    auth = (OIDCAuthenticator(settings.oidc_issuer, settings.oidc_audience, settings.oidc_jwks_url,
                              settings.oidc_roles_claim) if settings.oidc_issuer
            else Authenticator(settings.api_tokens, disabled=settings.api_auth_disabled))
    return create_app(service, session_factory, settings, auth, enable_docs=os.getenv("CLAIMLENS_API_DOCS", "1") == "1")
