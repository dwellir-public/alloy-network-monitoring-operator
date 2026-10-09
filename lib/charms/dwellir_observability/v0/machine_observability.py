# Copyright 2026 Erik Lönroth
# See LICENSE file for licensing details.

"""Machine-observability relation library.

This library provides a neutral relation contract for machine-subordinate
telemetry collection. Principal charms publish metrics, journald, and file-log
source declarations; subordinate consumers validate and apply those
declarations.

The canonical source of this library lives in `alloy-sub-operator` and can be
vendor-copied into other charms to keep the contract in sync.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import hmac
import io
import json
import logging
import zlib
from collections.abc import Iterable
from typing import Any, Callable, Literal, Optional

from ops.charm import CharmBase, HookEvent, RelationBrokenEvent, RelationChangedEvent
from ops.framework import EventBase, EventSource, Object, ObjectEvents
from ops.model import Relation
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

logger = logging.getLogger(__name__)

LIBID = "0b7d5c45f19b4b4b9876db265b31af48"
LIBAPI = 0
LIBPATCH = 5

DEFAULT_RELATION_NAME = "machine-observability"
MACHINE_OBSERVABILITY_SCHEMA_VERSION_V1 = 1
MACHINE_OBSERVABILITY_SCHEMA_VERSION_V2 = 2
MACHINE_OBSERVABILITY_SCHEMA_VERSION_V3 = 3
MAX_SERIALIZED_PAYLOAD_BYTES = 60 * 1024
MAX_DECODED_ARTIFACT_BYTES = 1024 * 1024
MAX_TOTAL_DECODED_ARTIFACT_BYTES = 1024 * 1024

ArtifactType = Literal["prometheus_alert_rules", "loki_alert_rules"]


class PayloadTooLargeError(ValueError):
    """Raised when a serialized relation payload exceeds the configured ceiling."""


class ObservabilityArtifact(BaseModel):
    """A compressed, checksummed observability artifact carried in relation data."""

    model_config = ConfigDict(
        extra="forbid",
        revalidate_instances="always",
        strict=True,
    )

    artifact_type: ArtifactType
    artifact_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    encoding: Literal["gzip+base64"] = "gzip+base64"
    content: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def encode_artifact(
    *, artifact_type: ArtifactType, artifact_id: str, content: bytes
) -> ObservabilityArtifact:
    """Encode bytes as a deterministic gzip/base64 artifact with a SHA-256 digest."""

    if len(content) > MAX_DECODED_ARTIFACT_BYTES:
        raise ValueError(
            f"artifact {artifact_id!r} exceeds encode size limit "
            f"of {MAX_DECODED_ARTIFACT_BYTES} bytes"
        )

    compressed = io.BytesIO()
    with gzip.GzipFile(
        fileobj=compressed,
        mode="wb",
        filename="",
        mtime=0,
    ) as gzip_file:
        gzip_file.write(content)

    return ObservabilityArtifact(
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        content=base64.b64encode(compressed.getvalue()).decode("ascii"),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def decode_artifact(artifact: ObservabilityArtifact) -> bytes:
    """Decode a bounded artifact and verify its digest against the original bytes."""

    try:
        compressed = base64.b64decode(artifact.content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"invalid base64 for artifact {artifact.artifact_id!r}") from exc

    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    content = bytearray()
    for offset in range(0, len(compressed), 8192):
        pending = compressed[offset : offset + 8192]
        while pending:
            remaining_bytes = MAX_DECODED_ARTIFACT_BYTES - len(content)
            try:
                decoded = decompressor.decompress(pending, remaining_bytes + 1)
            except zlib.error as exc:
                raise ValueError(
                    f"invalid gzip for artifact {artifact.artifact_id!r}"
                ) from exc
            pending = decompressor.unconsumed_tail
            if len(decoded) > remaining_bytes:
                raise ValueError(
                    f"artifact {artifact.artifact_id!r} exceeds decoded size limit "
                    f"of {MAX_DECODED_ARTIFACT_BYTES} bytes"
                )
            content.extend(decoded)
            if decompressor.unused_data:
                raise ValueError(
                    f"invalid trailing gzip data for artifact {artifact.artifact_id!r}"
                )

    if not decompressor.eof:
        raise ValueError(f"invalid gzip for artifact {artifact.artifact_id!r}")

    actual_sha256 = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(actual_sha256, artifact.sha256):
        raise ValueError(f"checksum mismatch for artifact {artifact.artifact_id!r}")

    return bytes(content)


def validate_artifacts(
    artifacts: Iterable[ObservabilityArtifact],
    *,
    maximum_decoded_bytes: int = MAX_TOTAL_DECODED_ARTIFACT_BYTES,
) -> int:
    """Validate artifacts sequentially while enforcing their aggregate decoded size."""

    total_decoded_bytes = 0
    for artifact in artifacts:
        validated = ObservabilityArtifact.model_validate(artifact)
        decoded_size = len(decode_artifact(validated))
        total_decoded_bytes += decoded_size
        if total_decoded_bytes > maximum_decoded_bytes:
            raise ValueError(
                "aggregate decoded artifacts exceed total size limit "
                f"of {maximum_decoded_bytes} bytes at artifact {validated.artifact_id!r}"
            )
    return total_decoded_bytes


class MetricsEndpoint(BaseModel):
    """One metrics scrape endpoint declared by a principal charm."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    targets: list[str]
    path: str = "/metrics"
    scheme: str = "http"
    interval: str = ""
    timeout: str = ""
    tls: dict[str, str | bool] = Field(default_factory=dict)


class LogFileSource(BaseModel):
    """A file log source declared by a principal charm."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)


class SourceTopology(BaseModel):
    """Explicit Juju topology for the workload that owns the declared sources."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    model: str = ""
    model_uuid: str = ""
    application: str
    unit: str
    charm_name: str = ""


class MachineObservabilityPayload(BaseModel):
    """Neutral source declarations from a principal charm."""

    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    schema_version: Literal[
        MACHINE_OBSERVABILITY_SCHEMA_VERSION_V1,
        MACHINE_OBSERVABILITY_SCHEMA_VERSION_V2,
        MACHINE_OBSERVABILITY_SCHEMA_VERSION_V3,
    ] = (
        MACHINE_OBSERVABILITY_SCHEMA_VERSION_V1
    )
    charm_name: str = ""
    source_topology: SourceTopology | None = None
    metrics_endpoints: list[MetricsEndpoint] = Field(default_factory=list)
    systemd_units: list[str] = Field(default_factory=list)
    journal_match_expressions: list[str] = Field(default_factory=list)
    log_files: list[LogFileSource] = Field(default_factory=list)
    artifacts: list[ObservabilityArtifact] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_duplicate_artifact_identities(self) -> "MachineObservabilityPayload":
        """Reject artifacts sharing the same type and identifier."""

        if (
            self.schema_version != MACHINE_OBSERVABILITY_SCHEMA_VERSION_V3
            and self.artifacts
        ):
            raise ValueError("artifacts require schema version 3")

        identities: set[tuple[ArtifactType, str]] = set()
        for artifact in self.artifacts:
            identity = (artifact.artifact_type, artifact.artifact_id)
            if identity in identities:
                raise ValueError(
                    "duplicate artifact identity: "
                    f"{artifact.artifact_type}/{artifact.artifact_id}"
                )
            identities.add(identity)
        return self


def serialize_machine_observability_payload(
    payload: MachineObservabilityPayload | dict[str, Any],
    *,
    maximum_bytes: int = MAX_SERIALIZED_PAYLOAD_BYTES,
) -> str:
    """Serialize a payload deterministically and enforce its UTF-8 byte ceiling."""

    validated = MachineObservabilityPayload.model_validate(payload)
    validate_artifacts(validated.artifacts)
    excluded_fields = (
        {"artifacts"}
        if validated.schema_version != MACHINE_OBSERVABILITY_SCHEMA_VERSION_V3
        else set()
    )
    serializable = validated.model_dump(mode="json", exclude=excluded_fields)

    serialized = json.dumps(
        serializable,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    serialized_bytes = len(serialized.encode("utf-8"))
    if serialized_bytes > maximum_bytes:
        raise PayloadTooLargeError(
            f"serialized machine-observability payload is {serialized_bytes} bytes; "
            f"maximum is {maximum_bytes} bytes (default {MAX_SERIALIZED_PAYLOAD_BYTES})"
        )
    return serialized


class MachineObservabilityProviderAppData(BaseModel):
    """Application databag model for the provider side of the relation."""

    payload: str

    def dump(self, databag: dict[str, str]) -> None:
        """Write the model into a relation databag."""

        databag["payload"] = self.payload

    @classmethod
    def load(cls, databag: dict[str, str]) -> "MachineObservabilityProviderAppData":
        """Load the provider model from a relation databag."""

        return cls(payload=databag.get("payload", "{}"))


def build_machine_observability_payload(
    *,
    service_name: str,
    charm_name: str,
    source_topology: SourceTopology | None = None,
) -> MachineObservabilityPayload:
    """Build a typed source-only observability payload for publication."""

    return MachineObservabilityPayload(
        schema_version=(
            MACHINE_OBSERVABILITY_SCHEMA_VERSION_V2
            if source_topology is not None
            else MACHINE_OBSERVABILITY_SCHEMA_VERSION_V1
        ),
        charm_name=charm_name,
        source_topology=source_topology,
        systemd_units=[service_name],
        journal_match_expressions=[],
        metrics_endpoints=[
            MetricsEndpoint(
                targets=["localhost:9615"],
                path="/metrics",
                scheme="http",
            )
        ],
        log_files=[],
    )


def load_machine_observability_payload(relation: Any) -> MachineObservabilityPayload:
    """Load and validate the remote application payload for machine-observability."""

    raw_payload = "{}"

    if hasattr(relation, "remote_app_data"):
        raw_payload = relation.remote_app_data.get("payload", "{}")
    else:
        app = getattr(relation, "app", None)
        if app is None:
            return MachineObservabilityPayload()
        raw_payload = relation.data[app].get("payload", "{}")

    return MachineObservabilityPayload.model_validate(json.loads(raw_payload))


class MachineObservabilityProvider(Object):
    """Publish machine-observability payloads to related subordinates."""

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
        *,
        payload_factory: Optional[Callable[[], MachineObservabilityPayload | dict[str, Any]]] = None,
        refresh_events: Optional[list[HookEvent]] = None,
    ):
        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name
        self._payload_factory = payload_factory
        self._refresh_events = refresh_events or [
            self._charm.on.config_changed,
            self._charm.on.upgrade_charm,
        ]

        events = self._charm.on[relation_name]
        self.framework.observe(events.relation_joined, self._on_refresh)
        self.framework.observe(events.relation_changed, self._on_refresh)
        for event in self._refresh_events:
            self.framework.observe(event, self._on_refresh)

    def _on_refresh(self, _: HookEvent) -> None:
        """Refresh relation data from the payload factory when configured."""

        if self._payload_factory is None:
            return
        self.publish(self._payload_factory())

    def publish(self, payload: MachineObservabilityPayload | dict[str, Any]) -> None:
        """Publish payload JSON into all related app databags."""

        if self.model.app is None:
            return

        provider_data = MachineObservabilityProviderAppData(
            payload=serialize_machine_observability_payload(payload)
        )
        for relation in self.model.relations.get(self._relation_name, []):
            provider_data.dump(relation.data[self.model.app])


class MachineObservabilityDataChanged(EventBase):
    """Event emitted when machine-observability data changes."""


class MachineObservabilityValidationError(EventBase):
    """Event emitted when machine-observability data fails validation."""

    def __init__(self, handle, message: str = ""):
        super().__init__(handle)
        self.message = message

    def snapshot(self) -> dict[str, str]:
        """Save validation error state."""

        return {"message": self.message}

    def restore(self, snapshot: dict[str, str]) -> None:
        """Restore validation error state."""

        self.message = snapshot["message"]


class MachineObservabilityConsumerEvents(ObjectEvents):
    """Events emitted by MachineObservabilityConsumer."""

    data_changed = EventSource(MachineObservabilityDataChanged)
    validation_error = EventSource(MachineObservabilityValidationError)


class MachineObservabilityConsumer(Object):
    """Validate and read machine-observability payloads from principal charms."""

    on = MachineObservabilityConsumerEvents()  # pyright: ignore

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name

        events = self._charm.on[relation_name]
        self.framework.observe(events.relation_changed, self._on_relation_changed)
        self.framework.observe(events.relation_broken, self._on_relation_broken)

    def _on_relation_changed(self, event: RelationChangedEvent) -> None:
        relation = event.relation
        if not self._validated_payload(relation):
            return
        self.on.data_changed.emit()  # pyright: ignore

    def _on_relation_broken(self, _: RelationBrokenEvent) -> None:
        self.on.data_changed.emit()  # pyright: ignore

    def get_payload(
        self, relation: Optional[Relation] = None
    ) -> MachineObservabilityPayload:
        """Return the validated payload for a relation or the default empty payload."""

        relation = relation or self._relation
        if relation is None:
            return MachineObservabilityPayload()

        payload = self._validated_payload(relation)
        return payload if payload is not None else MachineObservabilityPayload()

    @property
    def relations(self) -> list[Relation]:
        """All relations using the configured relation name."""

        return list(self._charm.model.relations[self._relation_name])

    @property
    def _relation(self) -> Optional[Relation]:
        """The single relation for this endpoint when present."""

        relations = self.relations
        return relations[0] if relations else None

    def _validated_payload(
        self, relation: Relation
    ) -> Optional[MachineObservabilityPayload]:
        try:
            return load_machine_observability_payload(relation)
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning(
                "Invalid machine-observability payload on relation %s: %s",
                relation.id,
                exc,
            )
            self.on.validation_error.emit(message=str(exc))  # pyright: ignore
            return None
