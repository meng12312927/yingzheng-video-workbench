"""MVP2 任务状态机：显式转移、乐观版本和幂等事件。"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from src.models.schemas import TaskEvent, TaskEventRecord, TaskSnapshot, TaskState, utc_now


TRANSITIONS: dict[tuple[TaskState, TaskEvent], TaskState] = {
    ("created", "preflight_passed"): "preflight_ok",
    ("preflight_ok", "requirement_drafted"): "requirement_draft",
    ("requirement_draft", "clarification_required"): "awaiting_requirement_clarification",
    ("awaiting_requirement_clarification", "clarification_applied"): "requirement_draft",
    ("requirement_draft", "requirement_confirmed"): "requirement_confirmed",
    ("requirement_confirmed", "style_recommended"): "style_recommended",
    ("requirement_confirmed", "style_skipped"): "style_skipped",
    ("style_recommended", "analysis_started"): "analyzing",
    ("style_skipped", "analysis_started"): "analyzing",
    ("analyzing", "review_ready"): "awaiting_review",
    ("awaiting_review", "plan_approved"): "plan_approved",
    ("plan_approved", "review_invalidated"): "awaiting_review",
    ("plan_approved", "render_started"): "rendering",
    ("rendering", "render_completed"): "verifying",
    ("verifying", "verification_passed"): "succeeded",
    ("verifying", "delivery_resolution_required"): "awaiting_delivery_resolution",
    ("awaiting_delivery_resolution", "delivery_approved"): "succeeded",
    ("awaiting_delivery_resolution", "delivery_revision_requested"): "awaiting_review",
}


class InvalidTransitionError(ValueError):
    """事件不允许从当前状态执行。"""


class StateVersionConflictError(ValueError):
    """调用者读取的状态版本已经过期。"""


class IdempotencyConflictError(ValueError):
    """同一幂等键被用于不同事件。"""


class TaskStateMachine:
    """以 task_state.json + task_events.jsonl 为唯一持久化状态语义。"""

    def __init__(self, task_dir: Path):
        self.task_dir = Path(task_dir)
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = self.task_dir / "task_state.json"
        self.events_path = self.task_dir / "task_events.jsonl"
        self._lock = threading.RLock()

    @property
    def task_id(self) -> str:
        return self.task_dir.name

    def initialise(self) -> TaskSnapshot:
        if self.snapshot_path.exists() or self.events_path.exists():
            return self.load()
        snapshot = TaskSnapshot(task_id=self.task_id)
        self._write_snapshot(snapshot)
        return snapshot

    def load(self) -> TaskSnapshot:
        events = self.events()
        if self.snapshot_path.exists():
            snapshot = TaskSnapshot.model_validate_json(
                self.snapshot_path.read_text(encoding="utf-8")
            )
            if events:
                last = events[-1]
                if (
                    snapshot.version != last.resulting_state_version
                    or snapshot.state != last.to_state
                    or snapshot.last_event_id != last.id
                ):
                    snapshot = self._replay(events)
                    self._write_snapshot(snapshot)
            return snapshot
        if not events:
            return self.initialise()
        snapshot = self._replay(events)
        self._write_snapshot(snapshot)
        return snapshot

    def _replay(self, events: list[TaskEventRecord]) -> TaskSnapshot:
        """以追加事件为事实源重建快照，并拒绝断裂或被篡改的事件链。"""
        expected_state: TaskState = "created"
        expected_version = 1
        for event in events:
            replay_target = (
                "failed"
                if event.event == "task_failed" and expected_state not in {"succeeded", "failed"}
                else TRANSITIONS.get((expected_state, event.event))
            )
            if event.task_id != self.task_id:
                raise ValueError("任务事件属于其他任务")
            if (
                event.from_state != expected_state
                or event.expected_state_version != expected_version
                or event.resulting_state_version != expected_version + 1
                or event.to_state != replay_target
            ):
                raise ValueError("任务事件链不连续，无法安全恢复")
            expected_state = event.to_state
            expected_version = event.resulting_state_version
        last = events[-1]
        return TaskSnapshot(
            task_id=self.task_id,
            state=last.to_state,
            version=last.resulting_state_version,
            last_event_id=last.id,
            error_code=last.payload.get("error_code") if last.to_state == "failed" else None,
            error_message=last.payload.get("error_message") if last.to_state == "failed" else None,
            updated_at=last.created_at,
        )

    def events(self) -> list[TaskEventRecord]:
        if not self.events_path.exists():
            return []
        records: list[TaskEventRecord] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(TaskEventRecord.model_validate_json(line))
        return records

    def apply(
        self,
        event: TaskEvent,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> tuple[TaskSnapshot, TaskEventRecord, bool]:
        """提交一个状态事件，返回（新快照、事件、是否为重复提交）。"""
        with self._lock:
            return self._apply_locked(
                event,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
                actor_id=actor_id,
                payload=payload,
            )

    def _apply_locked(
        self,
        event: TaskEvent,
        *,
        expected_version: int,
        idempotency_key: str,
        actor_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> tuple[TaskSnapshot, TaskEventRecord, bool]:
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency_key 不能为空")
        snapshot = self.initialise()
        existing = next((record for record in self.events() if record.idempotency_key == key), None)
        if existing:
            if existing.event != event:
                raise IdempotencyConflictError("同一幂等键不能用于不同事件")
            return snapshot, existing, True
        if snapshot.version != expected_version:
            raise StateVersionConflictError(
                f"状态版本冲突：期望 {expected_version}，当前 {snapshot.version}"
            )
        if event == "task_failed":
            if snapshot.state in {"succeeded", "failed"}:
                raise InvalidTransitionError(f"终态 {snapshot.state} 不能再执行 task_failed")
            target: TaskState = "failed"
        else:
            target = TRANSITIONS.get((snapshot.state, event))  # type: ignore[assignment]
            if target is None:
                raise InvalidTransitionError(
                    f"非法状态迁移：{snapshot.state} --{event}--> ?"
                )
        record = TaskEventRecord(
            task_id=self.task_id,
            event=event,
            from_state=snapshot.state,
            to_state=target,
            expected_state_version=snapshot.version,
            resulting_state_version=snapshot.version + 1,
            idempotency_key=key,
            actor_id=actor_id,
            payload=payload or {},
        )
        self._append_event(record)
        updated = snapshot.model_copy(
            update={
                "state": target,
                "version": snapshot.version + 1,
                "last_event_id": record.id,
                "error_code": (payload or {}).get("error_code") if target == "failed" else None,
                "error_message": (payload or {}).get("error_message") if target == "failed" else None,
                "updated_at": utc_now(),
            }
        )
        self._write_snapshot(updated)
        return updated, record, False

    def _write_snapshot(self, snapshot: TaskSnapshot) -> None:
        temporary = self.snapshot_path.with_name(
            f".{self.snapshot_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(self.snapshot_path)

    def _append_event(self, event: TaskEventRecord) -> None:
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()
