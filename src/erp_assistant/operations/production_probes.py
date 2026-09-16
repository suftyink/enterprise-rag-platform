import asyncio
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from alembic.config import Config
from alembic.script import ScriptDirectory
from pymilvus import MilvusClient
from sqlalchemy import select, text

from erp_assistant.core.config import Settings
from erp_assistant.evaluation.models import GateDecision
from erp_assistant.evaluation.profiles import ProfileStatus
from erp_assistant.infrastructure.database import SessionFactory, engine
from erp_assistant.infrastructure.health import check_readiness
from erp_assistant.models.knowledge import RetrievalProfileRecord
from erp_assistant.operations.evidence import (
    ProductionConfigurationProbe,
    _production_routes_valid,
    production_configuration_evidence,
)
from erp_assistant.operations.readiness_models import (
    EvidenceBundle,
    EvidenceCheck,
    ReleaseIdentity,
)
from erp_assistant.orchestration.providers import (
    ChatProvider,
    ProviderBoundary,
    chat_binding_fingerprint,
)
from erp_assistant.rag.index_manifest import build_manifest, capture_corpus_snapshot
from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
from erp_assistant.rag.runtime_registry import RetrievalRuntimeRegistry
from erp_assistant.rag.types import DataClassification
from erp_assistant.rag.vector_store import MilvusVectorStore
from erp_assistant.workers.celery_app import celery_app


async def collect_production_configuration(
    settings: Settings,
    identity: ReleaseIdentity,
    *,
    observed_at: datetime,
) -> EvidenceBundle:
    return await _collect_production_configuration(
        settings,
        identity_fingerprint=identity.fingerprint,
        identity=identity,
        observed_at=observed_at,
    )


async def validate_production_runtime_startup(
    settings: Settings,
    *,
    observed_at: datetime,
    worker_self: bool = False,
) -> EvidenceBundle | None:
    if settings.app_env.casefold() not in {"prod", "production"}:
        return None
    evidence = await _collect_production_configuration(
        settings,
        identity_fingerprint=settings.pilot_release_fingerprint,
        identity=None,
        observed_at=observed_at,
        worker_available_override=True if worker_self else None,
    )
    failed = tuple(
        check.name for check in evidence.checks if check.decision is not GateDecision.PASS
    )
    if failed:
        raise RuntimeError(f"Production runtime startup invalid: {','.join(failed)}")
    return evidence


async def _collect_production_configuration(
    settings: Settings,
    *,
    identity_fingerprint: str,
    identity: ReleaseIdentity | None,
    observed_at: datetime,
    worker_available_override: bool | None = None,
) -> EvidenceBundle:
    routes_available = _production_routes_valid(settings)
    worker_probe = (
        asyncio.sleep(0, result=worker_available_override)
        if worker_available_override is not None
        else asyncio.to_thread(_worker_is_available)
    )
    migration_result, worker_result, dimensions_result, identity_result = await asyncio.gather(
        _migration_is_current(),
        worker_probe,
        asyncio.to_thread(_index_dimensions_match, settings),
        _active_deployment_matches(settings, identity),
        return_exceptions=True,
    )
    return production_configuration_evidence(
        settings,
        ProductionConfigurationProbe(
            model_routes_available=routes_available,
            index_dimensions_match=_configuration_result(dimensions_result),
            database_migration_current=_configuration_result(migration_result),
            worker_available=_configuration_result(worker_result),
            release_identity_matches=_configuration_result(identity_result),
        ),
        observed_at=observed_at,
        identity_fingerprint=identity_fingerprint,
        reference="probe://production-configuration/current",
    )


async def collect_dependencies(
    settings: Settings,
    identity: ReleaseIdentity,
    *,
    observed_at: datetime,
) -> EvidenceBundle:
    health_result, worker_result = await asyncio.gather(
        check_readiness(settings),
        asyncio.to_thread(_worker_is_available),
        return_exceptions=True,
    )
    states = (
        {}
        if isinstance(health_result, BaseException)
        else {
            name: component.status == "up" for name, component in health_result.components.items()
        }
    )
    states["celery"] = worker_result is True
    states["model_routes"] = _production_routes_valid(settings)
    return EvidenceBundle(
        observed_at=observed_at,
        identity_fingerprint=identity.fingerprint,
        checks=tuple(
            EvidenceCheck(
                name=name,
                decision=GateDecision.PASS if states.get(name, False) else GateDecision.FAIL,
                reference=f"probe://dependency/{name}",
                reason_code=None if states.get(name, False) else "DEPENDENCY_UNAVAILABLE",
            )
            for name in ("postgres", "redis", "minio", "milvus", "celery", "model_routes")
        ),
    )


def _configuration_result(result: object) -> bool | None:
    """Turn an unavailable probe into explicit indeterminate evidence."""
    return result if isinstance(result, bool) else None


async def _active_deployment_matches(
    settings: Settings,
    identity: ReleaseIdentity | None,
) -> bool:
    registry = build_retrieval_runtime_registry(settings)
    bindings = registry.bindings
    if not bindings:
        return False
    async with SessionFactory() as session:
        profiles = tuple(
            (
                await session.scalars(
                    select(RetrievalProfileRecord).where(
                        RetrievalProfileRecord.status == ProfileStatus.ACTIVE.value
                    )
                )
            ).all()
        )
        ordered = _matching_active_profiles(registry, profiles)
        if not ordered:
            return False
        for profile in ordered:
            classification = DataClassification(profile.data_classification)
            snapshot = await capture_corpus_snapshot(
                session,
                classification,
            )
            if (
                not snapshot.document_count
                or profile.index_build_hash is None
                or profile.index_build_hash != build_manifest(profile, snapshot).build_hash
                or profile.index_build_corpus_hash != snapshot.corpus_hash
                or profile.index_build_document_count != snapshot.document_count
            ):
                return False
    if identity is None:
        return True
    route_fingerprints = frozenset(
        f"{profile.data_classification}:{profile.configuration_fingerprint}" for profile in ordered
    )
    answer_fingerprints = _answer_route_fingerprints(settings, list(ordered))
    build_hashes = [profile.index_build_hash for profile in ordered]
    return bool(
        route_fingerprints == identity.route_fingerprints
        and answer_fingerprints == identity.answer_route_fingerprints
        and _combined(profile.index_identity for profile in ordered) == identity.index_identity
        and _combined(value for value in build_hashes if value is not None)
        == identity.index_build_hash
        and _combined(profile.parser_version for profile in ordered) == identity.parser_version
        and _combined(profile.chunk_version for profile in ordered) == identity.chunk_version
        and identity.configuration_fingerprint
        in {profile.configuration_fingerprint for profile in ordered}
        and all(profile.dataset_hash == identity.dataset_hash for profile in ordered)
    )


def _matching_active_profiles(
    registry: RetrievalRuntimeRegistry,
    profiles: Iterable[RetrievalProfileRecord],
) -> tuple[RetrievalProfileRecord, ...]:
    profile_items = tuple(profiles)
    by_classification = {
        profile.data_classification: profile for profile in profile_items
    }
    required_classifications = {
        binding.data_classification.value for binding in registry.bindings
    }
    if (
        len(by_classification) != len(profile_items)
        or set(by_classification) != required_classifications
    ):
        return ()
    ordered = tuple(
        by_classification[classification]
        for classification in sorted(required_classifications)
    )
    if any(not registry.can_resolve(profile) for profile in ordered):
        return ()
    return ordered


def _answer_route_fingerprints(
    settings: Settings,
    profiles: list[RetrievalProfileRecord],
) -> frozenset[str]:
    fingerprints: set[str] = set()
    for profile in profiles:
        classification = DataClassification(profile.data_classification)
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        provider = SimpleNamespace(
            deployment=str(getattr(settings, f"{prefix}_chat_deployment")),
            model=(
                f"{getattr(settings, f'{prefix}_chat_model')}:"
                f"{getattr(settings, f'{prefix}_chat_version')}"
            ),
            boundary=(
                ProviderBoundary.INTERNAL
                if classification is DataClassification.RESTRICTED
                else ProviderBoundary.CLOUD
            ),
        )
        fingerprint = chat_binding_fingerprint(
            cast(ChatProvider, provider),
            classification=classification.value,
        )
        fingerprints.add(f"{classification.value}:{fingerprint}")
    return frozenset(fingerprints)


def _combined(values: Iterable[str]) -> str:
    return "|".join(dict.fromkeys(values))


async def _migration_is_current() -> bool:
    expected = await asyncio.to_thread(_expected_migration_head)
    async with engine.connect() as connection:
        actual = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    return isinstance(actual, str) and actual == expected


def _expected_migration_head() -> str | None:
    project_root = Path(__file__).resolve().parents[3]
    configuration = Config(str(project_root / "alembic.ini"))
    configuration.set_main_option("script_location", str(project_root / "migrations"))
    return ScriptDirectory.from_config(configuration).get_current_head()


def _worker_is_available() -> bool:
    replies = celery_app.control.inspect(timeout=2.0).ping()
    return bool(replies)


def _index_dimensions_match(settings: Settings) -> bool:
    try:
        registry = build_retrieval_runtime_registry(settings)
        stores = tuple(
            store
            for classification in (
                DataClassification.ORDINARY,
                DataClassification.RESTRICTED,
            )
            for store in registry.vector_stores_for(classification)
        )
        for store in stores:
            if not isinstance(store, MilvusVectorStore):
                return False
            timeout = settings.retrieval_provider_timeout_seconds
            client = MilvusClient(uri=store.uri, timeout=timeout)
            try:
                description = client.describe_collection(
                    store.collection,
                    timeout=timeout,
                )
            finally:
                client.close()
            dense = next(
                field
                for field in description.get("fields", [])
                if field.get("name") == "dense_vector"
            )
            params = dense.get("params") or dense.get("type_params") or {}
            if int(params.get("dim", 0)) != store.dimension:
                return False
        return bool(stores)
    except (AttributeError, KeyError, StopIteration, TypeError, ValueError):
        return False
