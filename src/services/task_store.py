"""本地任务产物、审核闸门和业务审计的持久化服务。"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from src.models.schemas import (
    AuditableEditPlan,
    AuditEvent,
    CandidateDecision,
    ClarificationTurn,
    ExecutionBrief,
    RequirementBrief,
    RequirementSlot,
    RequirementSpec,
    ReviewGate,
    utc_now,
)


ModelT = TypeVar("ModelT", bound=BaseModel)


class TaskStore:
    """每个任务对应一个目录；写入采用原子替换，审计日志只追加。"""

    def __init__(self, task_dir: Path):
        self.task_dir = task_dir
        self.task_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @property
    def task_id(self) -> str:
        return self.task_dir.name

    def write_model(self, filename: str, model: BaseModel) -> Path:
        return self.write_payload(filename, model.model_dump(mode="json"))

    def write_payload(self, filename: str, payload: dict) -> Path:
        destination = self.task_dir / filename
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
        return destination

    def read_model(self, filename: str, model_type: type[ModelT]) -> ModelT:
        payload = json.loads((self.task_dir / filename).read_text(encoding="utf-8"))
        return model_type.model_validate(payload)

    def save_requirement_draft(
        self,
        brief: RequirementBrief,
        spec: RequirementSpec,
        execution_brief: ExecutionBrief,
        actor_id: str | None = None,
        generation_mode: str = "llm_generated",
        slots: list[RequirementSlot] | None = None,
    ) -> ReviewGate:
        """写入一个需求草稿，并创建唯一可批准的 requirement Gate。"""
        if generation_mode not in {"llm_generated", "manual_required"}:
            raise ValueError("未知的需求编译模式")
        if spec.brief_id != brief.id:
            raise ValueError("RequirementSpec 必须引用同一个 RequirementBrief")
        if execution_brief.requirement_spec_id != spec.id:
            raise ValueError("ExecutionBrief 必须引用同一个 RequirementSpec")
        if execution_brief.requirement_spec_version != spec.version:
            raise ValueError("ExecutionBrief 与 RequirementSpec 版本不一致")
        if spec.status != "draft" or execution_brief.status != "draft":
            raise ValueError("只能创建 draft 状态的需求审核门禁")

        self.write_model("requirement_brief.json", brief)
        self.write_model(f"requirement_spec_v{spec.version}.json", spec)
        self.write_model(f"execution_brief_v{execution_brief.version}.json", execution_brief)
        if slots is not None:
            self.write_payload(
                f"requirement_slots_v{spec.version}.json",
                {"slots": [slot.model_dump(mode="json") for slot in slots]},
            )
        gate = ReviewGate(
            task_id=self.task_id,
            stage="requirement",
            target_type="RequirementSpec",
            target_id=spec.id,
            target_version=spec.version,
        )
        self.write_payload("review_gates.json", self._replace_gate(gate))
        manual_required = generation_mode == "manual_required"
        self.append_audit(
            AuditEvent(
                task_id=self.task_id,
                action="requirement_draft_created",
                subject_type="RequirementSpec",
                subject_id=spec.id,
                subject_version=spec.version,
                actor_type="system",
                actor_id=actor_id,
                summary=(
                    "AI 需求解析失败；系统保留原始需求并创建了等待人工核对的草稿。"
                    if manual_required
                    else "系统生成了可编辑的需求任务书和 AI 执行说明。"
                ),
                metadata={"generation_mode": generation_mode},
            )
        )
        return gate

    def save_requirement_revision(
        self,
        brief: RequirementBrief,
        previous_spec: RequirementSpec,
        spec: RequirementSpec,
        execution_brief: ExecutionBrief,
        actor_id: str | None = None,
        slots: list[RequirementSlot] | None = None,
    ) -> ReviewGate:
        """保存修改后的新版本，并让旧版本的审核结果失效。"""
        if spec.id != previous_spec.id:
            raise ValueError("需求修订必须保留 RequirementSpec ID")
        if spec.version != previous_spec.version + 1:
            raise ValueError("需求修订版本必须恰好加 1")
        if spec.status != "draft":
            raise ValueError("修订后的需求必须先以 draft 状态重新审核")
        self.write_model(
            f"requirement_spec_v{previous_spec.version}.json",
            previous_spec.model_copy(update={"status": "superseded"}),
        )
        self.supersede_requirement_gate(previous_spec.id, previous_spec.version)
        gate = self.save_requirement_draft(
            brief,
            spec,
            execution_brief,
            actor_id,
            slots=slots,
        )
        self.append_audit(
            AuditEvent(
                task_id=self.task_id,
                action="requirement_revised",
                subject_type="RequirementSpec",
                subject_id=spec.id,
                subject_version=spec.version,
                actor_type="user",
                actor_id=actor_id,
                summary=f"用户创建了需求任务书第 {spec.version} 版，旧版本审核已失效。",
            )
        )
        return gate

    def read_requirement_slots(self, version: int) -> list[RequirementSlot]:
        payload = json.loads(
            (self.task_dir / f"requirement_slots_v{version}.json").read_text(encoding="utf-8")
        )
        return [RequirementSlot.model_validate(item) for item in payload.get("slots", [])]

    def append_clarification(self, turn: ClarificationTurn) -> None:
        destination = self.task_dir / "clarification_turns.jsonl"
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(turn.model_dump_json() + "\n")
            handle.flush()

    def save_plan_draft(
        self,
        plan: AuditableEditPlan,
        actor_id: str | None = None,
        previous_plan: AuditableEditPlan | None = None,
    ) -> ReviewGate:
        """保存计划版本并创建唯一的合并方案审核 Gate。"""
        if plan.status != "draft":
            raise ValueError("只有 draft 计划可以进入审核")
        if previous_plan is not None:
            if plan.id != previous_plan.id or plan.version != previous_plan.version + 1:
                raise ValueError("计划修订必须保留 ID 且版本恰好加 1")
            self.write_model(
                f"edit_plan_v{previous_plan.version}.json",
                previous_plan.model_copy(update={"status": "superseded"}),
            )
            self.supersede_gate("edit_plan", previous_plan.id, previous_plan.version)
        self.write_model(f"edit_plan_v{plan.version}.json", plan)
        self.write_model(f"delivery_spec_v{plan.delivery_spec.version}.json", plan.delivery_spec)
        gate = ReviewGate(
            task_id=self.task_id,
            stage="edit_plan",
            target_type="AuditableEditPlan",
            target_id=plan.id,
            target_version=plan.version,
        )
        self.write_payload("review_gates.json", self._replace_gate(gate))
        self.append_audit(
            AuditEvent(
                task_id=self.task_id,
                action="edit_plan_draft_created" if previous_plan is None else "edit_plan_revised",
                subject_type="AuditableEditPlan",
                subject_id=plan.id,
                subject_version=plan.version,
                actor_type="system" if previous_plan is None else "user",
                actor_id=actor_id,
                summary=(
                    f"系统生成了可审核剪辑计划第 {plan.version} 版。"
                    if previous_plan is None
                    else f"用户修改了剪辑计划，新版本为第 {plan.version} 版，旧批准已失效。"
                ),
            )
        )
        return gate

    def approve_plan(
        self,
        gate_id: str,
        actor_id: str | None,
    ) -> tuple[AuditableEditPlan, ReviewGate]:
        with self._lock:
            return self._approve_plan_locked(gate_id, actor_id)

    def _approve_plan_locked(
        self,
        gate_id: str,
        actor_id: str | None,
    ) -> tuple[AuditableEditPlan, ReviewGate]:
        gate = next((item for item in self._load_gates() if item.id == gate_id), None)
        if gate is None or gate.stage != "edit_plan":
            raise ValueError("未找到剪辑计划审核门禁")
        if gate.status == "approved":
            return self.read_model(
                f"edit_plan_v{gate.target_version}.json", AuditableEditPlan
            ), gate
        if gate.status != "pending":
            raise ValueError("当前剪辑计划 Gate 不能批准")
        plan = self.read_model(f"edit_plan_v{gate.target_version}.json", AuditableEditPlan)
        if plan.id != gate.target_id or plan.status != "draft":
            raise ValueError("Gate 与当前计划版本不匹配")
        approved = plan.model_copy(
            update={"status": "approved", "approved_at": utc_now()}
        )
        self.write_model(f"edit_plan_v{approved.version}.json", approved)
        resolved = self.resolve_gate(gate.id, "approved", actor_id)
        self.append_audit(
            AuditEvent(
                task_id=self.task_id,
                action="edit_plan_approved",
                subject_type="AuditableEditPlan",
                subject_id=approved.id,
                subject_version=approved.version,
                actor_type="user",
                actor_id=actor_id,
                summary=f"用户批准了剪辑计划第 {approved.version} 版。",
            )
        )
        return approved, resolved

    def append_decisions(self, decisions: list[CandidateDecision]) -> None:
        destination = self.task_dir / "decisions.json"
        existing = {"decisions": []}
        if destination.exists():
            existing = json.loads(destination.read_text(encoding="utf-8"))
        known_ids = {item.get("id") for item in existing.get("decisions", [])}
        new_items = [
            decision.model_dump(mode="json")
            for decision in decisions if decision.id not in known_ids
        ]
        if not new_items:
            return
        self.write_payload(
            "decisions.json",
            {"decisions": [*existing.get("decisions", []), *new_items]},
        )
        for decision in decisions:
            if decision.id in known_ids:
                continue
            self.append_audit(
                AuditEvent(
                    task_id=self.task_id,
                    action=f"candidate_{decision.action}",
                    subject_type="CandidateClip",
                    subject_id=decision.candidate_id,
                    subject_version=decision.plan_version,
                    actor_type="user",
                    actor_id=decision.actor_id,
                    summary=f"用户对候选执行了 {decision.action} 操作。",
                )
            )

    def confirm_requirement(self, gate_id: str, actor_id: str | None) -> tuple[RequirementSpec, ExecutionBrief, ReviewGate]:
        with self._lock:
            return self._confirm_requirement_locked(gate_id, actor_id)

    def _confirm_requirement_locked(self, gate_id: str, actor_id: str | None) -> tuple[RequirementSpec, ExecutionBrief, ReviewGate]:
        """确认没有待答问题的任务书，并同步确认其用户可见执行说明。"""
        gate = next((item for item in self._load_gates() if item.id == gate_id), None)
        if gate is None or gate.stage != "requirement":
            raise ValueError("未找到需求阶段的审核门禁")
        if gate.status != "pending":
            raise ValueError("只有 pending 状态的需求门禁可以确认")
        spec = self.read_model(f"requirement_spec_v{gate.target_version}.json", RequirementSpec)
        if spec.id != gate.target_id:
            raise ValueError("审核门禁与需求任务书不匹配")
        if spec.open_questions:
            raise ValueError("请先回答或删除所有待确认问题，再确认需求")
        execution = self.read_model(f"execution_brief_v{gate.target_version}.json", ExecutionBrief)
        now = utc_now()
        confirmed_spec = spec.model_copy(update={"status": "confirmed", "confirmed_at": now})
        confirmed_execution = execution.model_copy(update={"status": "confirmed", "confirmed_at": now})
        self.write_model(f"requirement_spec_v{gate.target_version}.json", confirmed_spec)
        self.write_model(f"execution_brief_v{gate.target_version}.json", confirmed_execution)
        resolved = self.resolve_gate(gate_id, "approved", actor_id)
        self.append_audit(
            AuditEvent(
                task_id=self.task_id,
                action="requirement_confirmed",
                subject_type="ExecutionBrief",
                subject_id=confirmed_execution.id,
                subject_version=confirmed_execution.version,
                actor_type="user",
                actor_id=actor_id,
                summary=f"用户确认了需求任务书第 {confirmed_spec.version} 版和 AI 执行说明。",
            )
        )
        return confirmed_spec, confirmed_execution, resolved

    def resolve_gate(
        self,
        gate_id: str,
        status: str,
        actor_id: str | None,
        comment: str | None = None,
    ) -> ReviewGate:
        with self._lock:
            return self._resolve_gate_locked(gate_id, status, actor_id, comment)

    def _resolve_gate_locked(
        self,
        gate_id: str,
        status: str,
        actor_id: str | None,
        comment: str | None = None,
    ) -> ReviewGate:
        """审核、退回或拒绝指定 Gate；退回必须留下说明。"""
        if status not in {"approved", "changes_requested", "rejected"}:
            raise ValueError("审核状态只能是 approved、changes_requested 或 rejected")
        if status == "changes_requested" and not (comment or "").strip():
            raise ValueError("请求修改时必须说明修改内容")
        gates = self._load_gates()
        matched = next((gate for gate in gates if gate.id == gate_id), None)
        if matched is None:
            raise ValueError("未找到审核门禁")
        if matched.status == status:
            return matched
        if matched.status != "pending":
            raise ValueError("只有 pending 状态的审核门禁可以处理")
        resolved = matched.model_copy(
            update={"status": status, "actor_id": actor_id, "comment": comment, "resolved_at": utc_now()}
        )
        self.write_payload("review_gates.json", self._replace_gate(resolved))
        self.append_audit(
            AuditEvent(
                task_id=self.task_id,
                action=f"review_{status}",
                subject_type=resolved.target_type,
                subject_id=resolved.target_id,
                subject_version=resolved.target_version,
                actor_type="user",
                actor_id=actor_id,
                summary=f"用户将 {resolved.stage} 阶段目标标记为 {status}。",
                metadata={"comment": comment},
            )
        )
        return resolved

    def is_approved(self, stage: str, target_id: str, target_version: int) -> bool:
        return any(
            gate.stage == stage
            and gate.target_id == target_id
            and gate.target_version == target_version
            and gate.status == "approved"
            for gate in self._load_gates()
        )

    def supersede_requirement_gate(self, target_id: str, target_version: int) -> None:
        """任务书有新版本时，使旧版本批准或待审状态永久失效。"""
        updated = []
        for gate in self._load_gates():
            if (
                gate.stage == "requirement"
                and gate.target_id == target_id
                and gate.target_version == target_version
                and gate.status in {"pending", "approved", "changes_requested"}
            ):
                updated.append(gate.model_copy(update={"status": "superseded", "resolved_at": utc_now()}))
            else:
                updated.append(gate)
        self.write_payload("review_gates.json", {"gates": [gate.model_dump(mode="json") for gate in updated]})

    def supersede_gate(self, stage: str, target_id: str, target_version: int) -> None:
        updated = []
        for gate in self._load_gates():
            if (
                gate.stage == stage
                and gate.target_id == target_id
                and gate.target_version == target_version
                and gate.status in {"pending", "approved", "changes_requested"}
            ):
                updated.append(
                    gate.model_copy(update={"status": "superseded", "resolved_at": utc_now()})
                )
            else:
                updated.append(gate)
        self.write_payload(
            "review_gates.json",
            {"gates": [gate.model_dump(mode="json") for gate in updated]},
        )

    def load_gates(self) -> list[ReviewGate]:
        return self._load_gates()

    def append_audit(self, event: AuditEvent) -> None:
        destination = self.task_dir / "audit_events.jsonl"
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
            handle.flush()

    def _load_gates(self) -> list[ReviewGate]:
        destination = self.task_dir / "review_gates.json"
        if not destination.exists():
            return []
        payload = json.loads(destination.read_text(encoding="utf-8"))
        return [ReviewGate.model_validate(item) for item in payload.get("gates", [])]

    def _replace_gate(self, replacement: ReviewGate) -> dict:
        gates = [gate for gate in self._load_gates() if gate.id != replacement.id]
        gates.append(replacement)
        return {"gates": [gate.model_dump(mode="json") for gate in gates]}
