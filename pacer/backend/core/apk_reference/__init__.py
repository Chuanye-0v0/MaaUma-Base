"""Verified access to APK-extracted reference sources."""

from .source_registry import (
    SOURCES,
    ReferenceSourceError,
    ReferenceSourceSpec,
    load_verified_json,
)
from .schema_validation import (
    ReferenceSchemaError,
    SourceValidationReport,
    validate_reference_source,
)
from .evidence import ReferenceEvidence, ReferenceEvidenceError
from .sparse_fingerprint import (
    RoiBinding,
    SparseColorPoint,
    SparseFingerprint,
    SparseFingerprintConfigError,
    SparseFingerprintMatcher,
)
from .event_title_evidence import (
    build_event_title_matcher,
    match_event_title_candidate,
)
from .factor_evidence import (
    FactorConfigurationVerification,
    FactorObservation,
    FactorSlotVerification,
    build_factor_column_matcher,
    build_horse_factor_matcher,
    match_factor_column_candidate,
    match_horse_factor_candidate,
    verify_factor_configuration,
)

__all__ = [
    "SOURCES",
    "ReferenceSourceError",
    "ReferenceSourceSpec",
    "ReferenceSchemaError",
    "ReferenceEvidence",
    "ReferenceEvidenceError",
    "FactorConfigurationVerification",
    "FactorObservation",
    "FactorSlotVerification",
    "RoiBinding",
    "SparseColorPoint",
    "SparseFingerprint",
    "SparseFingerprintConfigError",
    "SparseFingerprintMatcher",
    "SourceValidationReport",
    "build_factor_column_matcher",
    "build_horse_factor_matcher",
    "build_event_title_matcher",
    "load_verified_json",
    "match_event_title_candidate",
    "match_factor_column_candidate",
    "match_horse_factor_candidate",
    "validate_reference_source",
    "verify_factor_configuration",
]
