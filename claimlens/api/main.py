"""Build the real app from environment variables.   Run with:

    uvicorn claimlens.api.main:build_app_from_env --factory --port 8080
or: python -m claimlens serve
"""
from __future__ import annotations

import os

from fastapi import FastAPI

from claimlens.api.app import create_app
from claimlens.api.auth import Authenticator
from claimlens.config import get_settings
from claimlens.llm.bedrock_client import BedrockClient
from claimlens.llm.mock import MockLLM
from claimlens.ocr.factory import build_ocr
from claimlens.service import ProcessingService
from claimlens.storage.cache import NullCache, RedisCache
from claimlens.storage.db import make_engine, make_session_factory
from claimlens.storage.idempotency import DynamoIdempotencyStore, SqlIdempotencyStore


def build_service(settings=None):
    settings = settings or get_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))

    llm_spec = os.getenv("CLAIMLENS_LLM", "bedrock")
    if llm_spec.startswith("mock"):  # local demos only
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
    service, session_factory, settings = build_service()
    auth = Authenticator(settings.api_tokens, disabled=settings.api_auth_disabled)  # refuses to start if unconfigured
    return create_app(service, session_factory, settings, auth, enable_docs=os.getenv("CLAIMLENS_API_DOCS", "1") == "1")
