from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from erp_assistant.core.config import Settings
from erp_assistant.evaluation.models import GateDecision
from erp_assistant.identity.composition import identity_components
from erp_assistant.operations.readiness_models import EvidenceBundle, EvidenceCheck
from erp_assistant.rag.runtime_composition import build_retrieval_runtime_registry
from erp_assistant.rag.types import DataClassification


@dataclass(frozen=True)
class ProductionConfigurationProbe:
    """Results that require contacting the deployed environment."""

    model_routes_available: bool | None
    index_dimensions_match: bool | None
    database_migration_current: bool | None
    worker_available: bool | None
    release_identity_matches: bool | None = None


def production_configuration_evidence(
    settings: Settings,
    probe: ProductionConfigurationProbe,
    *,
    observed_at: datetime,
    identity_fingerprint: str,
    reference: str,
) -> EvidenceBundle:
    checks: Mapping[str, bool | None] = {
        "oidc": (
            settings.identity_provider == "oidc"
            and bool(settings.oidc_issuer)
            and bool(settings.oidc_audience)
            and bool(settings.oidc_jwks_url)
            and bool(settings.oidc_subject_claim)
        ),
        "model_routes": probe.model_routes_available,
        "index_dimensions": probe.index_dimensions_match,
        "database_migrations": probe.database_migration_current,
        "storage": bool(
            settings.minio_endpoint
            and settings.minio_access_key
            and settings.minio_secret_key
            and settings.minio_bucket
        ),
        "worker": probe.worker_available,
        "release_identity": probe.release_identity_matches,
    }
    return EvidenceBundle(
        observed_at=observed_at,
        identity_fingerprint=identity_fingerprint,
        checks=tuple(
            EvidenceCheck(
                name=name,
                decision=_decision(result),
                reference=reference,
                reason_code=(
                    None
                    if result is True
                    else f"CONFIG_{name.upper()}_{'UNAVAILABLE' if result is None else 'INVALID'}"
                ),
            )
            for name, result in checks.items()
        ),
    )


def validate_production_startup(
    settings: Settings,
    *,
    observed_at: datetime,
) -> EvidenceBundle | None:
    if settings.app_env.casefold() not in {"prod", "production"}:
        return None
    try:
        identity_components(settings)
    except ValueError as exc:
        raise RuntimeError("Production startup configuration invalid:identity") from exc
    routes_valid = _production_routes_valid(settings)
    evidence = production_configuration_evidence(
        settings,
        ProductionConfigurationProbe(
            model_routes_available=routes_valid,
            index_dimensions_match=routes_valid,
            database_migration_current=None,
            worker_available=None,
            release_identity_matches=None,
        ),
        observed_at=observed_at,
        identity_fingerprint=settings.pilot_release_fingerprint,
        reference="startup://production-configuration",
    )
    startup_checks = {"oidc", "model_routes", "storage"}
    failed = tuple(
        check.name
        for check in evidence.checks
        if check.name in startup_checks and check.decision is not GateDecision.PASS
    )
    if not settings.pilot_release_fingerprint:
        failed += ("pilot_release_fingerprint",)
    if failed:
        raise RuntimeError(f"Production startup configuration invalid: {','.join(failed)}")
    return evidence


def _production_routes_valid(settings: Settings) -> bool:
    if not settings.retrieval_runtime_deployments or settings.chat_provider != "http":
        return False
    try:
        build_retrieval_runtime_registry(settings)
    except (TypeError, ValueError):
        return False
    classifications = {
        DataClassification(deployment.data_classification)
        for deployment in settings.retrieval_runtime_deployments
    }
    for classification in classifications:
        prefix = "restricted" if classification is DataClassification.RESTRICTED else "ordinary"
        fields = (
            str(getattr(settings, f"{prefix}_chat_base_url")),
            str(getattr(settings, f"{prefix}_chat_deployment")),
            str(getattr(settings, f"{prefix}_chat_model")),
            str(getattr(settings, f"{prefix}_chat_version")),
        )
        if not all(value.strip() for value in fields):
            return False
    return True


def _decision(result: bool | None) -> GateDecision:
    if result is None:
        return GateDecision.INDETERMINATE
    return GateDecision.PASS if result else GateDecision.FAIL
