"""移动轻量审批令牌、撤销、过期与幂等提交。"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from datetime import timedelta
from pathlib import Path

from src.models.schemas import (
    AuditableEditPlan,
    MobileReviewLink,
    MobileReviewSubmission,
    MobileReviewTokenRecord,
    utc_now,
)
from src.services.task_store import TaskStore


class MobileReviewService:
    """只允许审核指定计划版本；令牌不能迁移到新版本。"""

    def __init__(self):
        self._lock = threading.RLock()

    def issue(
        self,
        *,
        store: TaskStore,
        plan: AuditableEditPlan,
        base_url: str,
        ttl_hours: int = 24,
        created_by: str | None = None,
    ) -> MobileReviewLink:
        if plan.status != "draft":
            raise ValueError("移动审核链接只能绑定当前待审核计划")
        if not 1 <= ttl_hours <= 168:
            raise ValueError("移动审核链接有效期必须在 1–168 小时之间")
        token = secrets.token_urlsafe(32)
        record = MobileReviewTokenRecord(
            task_id=store.task_id,
            plan_id=plan.id,
            plan_version=plan.version,
            token_hash=self._hash(token),
            expires_at=utc_now() + timedelta(hours=ttl_hours),
            created_by=created_by,
        )
        with self._lock:
            records = self._load_records(store)
            records.append(record)
            store.write_payload(
                "mobile_review_tokens.json",
                {"tokens": [item.model_dump(mode="json") for item in records]},
            )
        url = f"{base_url.rstrip('/')}/review/{token}"
        qr_code_path = self._write_qr_code(store, record.id, url)
        return MobileReviewLink(
            token_record_id=record.id,
            task_id=record.task_id,
            plan_id=record.plan_id,
            plan_version=record.plan_version,
            url=url,
            expires_at=record.expires_at,
            qr_code_path=qr_code_path,
        )

    def revoke(self, store: TaskStore, token_record_id: str) -> None:
        with self._lock:
            records = self._load_records(store)
            found = False
            updated = []
            for record in records:
                if record.id == token_record_id:
                    found = True
                    updated.append(record.model_copy(update={"status": "revoked"}))
                else:
                    updated.append(record)
            if not found:
                raise ValueError("找不到移动审核链接")
            store.write_payload(
                "mobile_review_tokens.json",
                {"tokens": [item.model_dump(mode="json") for item in updated]},
            )

    def inspect(
        self,
        tasks_root: Path,
        token: str,
    ) -> tuple[TaskStore, MobileReviewTokenRecord, AuditableEditPlan]:
        store, record = self._locate_record(tasks_root, token)
        if record.status != "active":
            raise ValueError("审核链接已失效")
        if record.expires_at <= utc_now():
            self._set_status(store, record.id, "expired")
            raise ValueError("审核链接已过期")
        plan = store.read_model(
            f"edit_plan_v{record.plan_version}.json", AuditableEditPlan
        )
        if plan.id != record.plan_id or plan.status != "draft":
            raise ValueError("审核链接绑定的计划版本已失效")
        return store, record, plan

    def submit(
        self,
        *,
        tasks_root: Path,
        token: str,
        decision: str,
        comment: str,
        reviewer_name: str | None,
        idempotency_key: str,
    ) -> MobileReviewSubmission:
        if decision not in {"approve", "changes_requested"}:
            raise ValueError("未知的移动审核决定")
        with self._lock:
            store, record = self._locate_record(tasks_root, token)
            existing = self._load_submissions(store)
            duplicate = next(
                (item for item in existing if item.idempotency_key == idempotency_key),
                None,
            )
            if duplicate:
                if duplicate.token_record_id != record.id:
                    raise ValueError("幂等键已被其他审核使用")
                return duplicate
            store, record, _ = self.inspect(tasks_root, token)
            submission = MobileReviewSubmission(
                token_record_id=record.id,
                task_id=record.task_id,
                plan_id=record.plan_id,
                plan_version=record.plan_version,
                decision=decision,
                comment=comment.strip(),
                reviewer_name=(reviewer_name or "").strip() or None,
                idempotency_key=idempotency_key,
            )
            store.write_payload(
                "mobile_review_submissions.json",
                {
                    "submissions": [
                        *[item.model_dump(mode="json") for item in existing],
                        submission.model_dump(mode="json"),
                    ]
                },
            )
            self._set_status(store, record.id, "used")
            return submission

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _locate_record(
        self,
        tasks_root: Path,
        token: str,
    ) -> tuple[TaskStore, MobileReviewTokenRecord]:
        token_hash = self._hash(token)
        for token_file in Path(tasks_root).glob("*/mobile_review_tokens.json"):
            store = TaskStore(token_file.parent)
            record = next(
                (
                    item for item in self._load_records(store)
                    if secrets.compare_digest(item.token_hash, token_hash)
                ),
                None,
            )
            if record is not None:
                return store, record
        raise ValueError("审核链接不存在")

    @staticmethod
    def _load_records(store: TaskStore) -> list[MobileReviewTokenRecord]:
        path = store.task_dir / "mobile_review_tokens.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [MobileReviewTokenRecord.model_validate(item) for item in payload.get("tokens", [])]

    @staticmethod
    def _load_submissions(store: TaskStore) -> list[MobileReviewSubmission]:
        path = store.task_dir / "mobile_review_submissions.json"
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [MobileReviewSubmission.model_validate(item) for item in payload.get("submissions", [])]

    def _set_status(self, store: TaskStore, record_id: str, status: str) -> None:
        records = [
            item.model_copy(update={"status": status}) if item.id == record_id else item
            for item in self._load_records(store)
        ]
        store.write_payload(
            "mobile_review_tokens.json",
            {"tokens": [item.model_dump(mode="json") for item in records]},
        )

    @staticmethod
    def _write_qr_code(store: TaskStore, record_id: str, url: str) -> str | None:
        """二维码属于可重新生成的展示产物；生成失败不影响安全审核链接。"""
        try:
            import qrcode

            path = store.task_dir / "mobile_review" / f"{record_id}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            qrcode.make(url).save(path)
            return str(path.resolve())
        except (ImportError, OSError, ValueError):
            return None
