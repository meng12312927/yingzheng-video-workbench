"""活动资料解析与专有名词人工确认队列。"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from src.models.schemas import (
    ContentAnalysis,
    EntityReviewItem,
    EntityReviewQueue,
    ReferenceDocument,
    ReferenceDocumentCategory,
    ReferenceDocumentEvidence,
    ReferenceLibrary,
    utc_now,
)


class ReferenceDocumentService:
    """MVP 支持可可靠定位到行/单元格的轻量文本资料。"""

    ALLOWED_SUFFIXES = {".txt", ".md", ".csv", ".json"}
    MAX_BYTES = 10 * 1024 * 1024
    ENTITY_TYPE_BY_CATEGORY = {
        "people": "person",
        "awards": "award",
        "products": "product",
        "organizations": "organization",
        "terminology": "term",
    }

    def ingest(
        self,
        *,
        task_id: str,
        path: str,
        category: ReferenceDocumentCategory,
        library: ReferenceLibrary | None = None,
    ) -> ReferenceLibrary:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ValueError("活动资料文件不存在")
        if source.suffix.lower() not in self.ALLOWED_SUFFIXES:
            raise ValueError("当前活动资料支持 TXT、Markdown、CSV 和 JSON")
        if source.stat().st_size > self.MAX_BYTES:
            raise ValueError("活动资料不能超过 10MB")
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        current = library or ReferenceLibrary(task_id=task_id)
        duplicate = next(
            (item for item in current.documents if item.content_hash == digest), None
        )
        if duplicate:
            return current
        document = ReferenceDocument(
            task_id=task_id,
            category=category,
            filename=source.name,
            source_path=str(source),
            content_hash=digest,
        )
        evidence = [
            ReferenceDocumentEvidence(
                id=f"document_evidence_{hashlib.sha256(f'{document.id}|{location}|{content}'.encode('utf-8')).hexdigest()[:16]}",
                document_id=document.id,
                document_version=document.version,
                category=category,
                location=location,
                content=content[:1000],
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            )
            for location, content in self._read_entries(source)
            if content.strip()
        ]
        if not evidence:
            raise ValueError("活动资料中没有可读取的文字")
        return current.model_copy(update={
            "version": current.version + (1 if current.documents else 0),
            "documents": [*current.documents, document],
            "evidence": [*current.evidence, *evidence],
            "updated_at": utc_now(),
        })

    def build_entity_queue(
        self,
        *,
        task_id: str,
        analysis: ContentAnalysis,
        library: ReferenceLibrary,
        previous: EntityReviewQueue | None = None,
    ) -> EntityReviewQueue:
        previous_by_key = {
            (
                item.transcript_segment_id,
                item.suggested_text,
                item.entity_type,
            ): item
            for item in (previous.items if previous else [])
        }
        canonical_terms: list[tuple[str, str, str]] = []
        for evidence in library.evidence:
            entity_type = self.ENTITY_TYPE_BY_CATEGORY.get(evidence.category)
            if not entity_type:
                continue
            for term in self._extract_terms(evidence.content):
                canonical_terms.append((term, entity_type, evidence.id))
        items: list[EntityReviewItem] = []
        seen: set[tuple[str, str, str]] = set()
        for segment in analysis.transcript:
            compact = self._compact(segment.text)
            for term, entity_type, evidence_id in canonical_terms:
                compact_term = self._compact(term)
                observed, similarity = self._best_observed(compact, compact_term)
                threshold = 0.65 if len(compact_term) <= 4 else 0.72
                if similarity < threshold:
                    continue
                key = (segment.id, term, entity_type)
                if key in seen:
                    continue
                seen.add(key)
                old = previous_by_key.get(key)
                items.append(
                    old or EntityReviewItem(
                        entity_type=entity_type,
                        source_asset_id=segment.source_asset_id,
                        transcript_segment_id=segment.id,
                        source_start=segment.start,
                        source_end=segment.end,
                        observed_text=observed or segment.text,
                        suggested_text=term,
                        reference_evidence_ids=[evidence_id],
                        similarity=round(similarity, 4),
                    )
                )
        return EntityReviewQueue(
            task_id=task_id,
            version=(previous.version + 1 if previous else 1),
            items=items,
            updated_at=utc_now(),
        )

    @staticmethod
    def resolve(
        queue: EntityReviewQueue,
        *,
        item_id: str,
        action: str,
        actor_id: str | None = None,
        edited_value: str | None = None,
    ) -> EntityReviewQueue:
        if action not in {"confirm", "reject", "edit"}:
            raise ValueError("专有名词处理动作必须是 confirm、reject 或 edit")
        found = False
        items = []
        for item in queue.items:
            if item.id != item_id:
                items.append(item)
                continue
            found = True
            if action == "edit" and not str(edited_value or "").strip():
                raise ValueError("修改专有名词时必须填写最终文字")
            value = (
                str(edited_value).strip()
                if action == "edit"
                else item.suggested_text if action == "confirm" else None
            )
            items.append(item.model_copy(update={
                "status": {"confirm": "confirmed", "reject": "rejected", "edit": "edited"}[action],
                "confirmed_value": value,
                "actor_id": actor_id,
                "resolved_at": utc_now(),
            }))
        if not found:
            raise ValueError("找不到待确认的专有名词")
        return queue.model_copy(update={
            "version": queue.version + 1,
            "items": items,
            "updated_at": utc_now(),
        })

    @classmethod
    def _read_entries(cls, path: Path) -> list[tuple[str, str]]:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            entries = []
            for row_index, row in enumerate(rows, start=1):
                for column_index, value in enumerate(row, start=1):
                    if value.strip():
                        entries.append((f"第 {row_index} 行第 {column_index} 列", value.strip()))
            return entries
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            return cls._flatten_json(data)
        text = path.read_text(encoding="utf-8-sig")
        return [
            (f"第 {index} 行", line.strip())
            for index, line in enumerate(text.splitlines(), start=1)
            if line.strip() and not line.lstrip().startswith("#")
        ]

    @classmethod
    def _flatten_json(cls, value: Any, prefix: str = "$") -> list[tuple[str, str]]:
        if isinstance(value, dict):
            return [
                item
                for key, child in value.items()
                for item in cls._flatten_json(child, f"{prefix}.{key}")
            ]
        if isinstance(value, list):
            return [
                item
                for index, child in enumerate(value)
                for item in cls._flatten_json(child, f"{prefix}[{index}]")
            ]
        text = str(value).strip()
        return [(prefix, text)] if text else []

    @staticmethod
    def _extract_terms(text: str) -> list[str]:
        return [
            item.strip()
            for item in re.split(r"[,，、;；\t|/：:]", text)
            if 2 <= len(item.strip()) <= 24
        ][:20]

    @staticmethod
    def _compact(text: str) -> str:
        return "".join(re.findall(r"[\u4e00-\u9fffa-zA-Z0-9]", text)).lower()

    @staticmethod
    def _best_observed(text: str, term: str) -> tuple[str, float]:
        if not text or not term:
            return "", 0.0
        if term in text:
            return term, 1.0
        best_text, best_score = "", 0.0
        for width in range(max(2, len(term) - 2), min(len(text), len(term) + 2) + 1):
            for start in range(0, len(text) - width + 1):
                candidate = text[start:start + width]
                score = SequenceMatcher(None, candidate, term).ratio()
                if score > best_score:
                    best_text, best_score = candidate, score
        return best_text, best_score
