"""Stable secret identifiers and their retired desktop setting names.

Legacy field names remain part of the migration boundary even though current
clients never persist their values. Keeping this registry framework-neutral
lets composition roots and maintenance utilities sanitize old documents
without importing Flask or a platform credential adapter.
"""
from __future__ import annotations

from types import MappingProxyType


LEGACY_SECRET_IDS = MappingProxyType({
    "aiKey": "provider:ai:api-key",
    "embedKey": "provider:embedding:api-key",
    "imgGenKey": "provider:image-generation:api-key",
    "mistralKey": "provider:mistral:api-key",
    "ocrClaudeKey": "provider:anthropic:api-key",
    "ocrAzureKey": "provider:azure-ocr:api-key",
    "ocrAwsKey": "provider:aws-ocr:access-key-id",
    "ocrAwsSecret": "provider:aws-ocr:secret-access-key",
    "supabaseKey": "cloud:supabase:service-role-key",
    "supabaseAnonKey": "cloud:supabase:anon-key",
    "r2KeyId": "storage:r2:access-key-id",
    "r2Secret": "storage:r2:secret-access-key",
    "gsKeyFile": "provider:google-sheets:service-account-file",
})
LEGACY_SECRET_KEYS = frozenset(LEGACY_SECRET_IDS)
