"""版本化组织配置；只保存明确录入的品牌、术语和发布规则。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from src.models.schemas import (
    OrganizationMemoryItem,
    OrganizationMemoryKind,
    OrganizationProfile,
    utc_now,
)


class OrganizationProfileService:
    """本地默认实现；文件结构可由后续 Repository 实现替换。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        name: str,
        scenario: str,
        created_by: str | None = None,
    ) -> OrganizationProfile:
        profile = OrganizationProfile(
            name=name.strip(),
            scenario=scenario,
            created_by=created_by,
        )
        self._write(profile)
        return profile

    def load(self, profile_id: str, version: int | None = None) -> OrganizationProfile:
        directory = self._profile_dir(profile_id)
        path = directory / (
            f"profile_v{version}.json" if version is not None else "current.json"
        )
        if not path.exists():
            raise ValueError("找不到组织配置或指定版本")
        return OrganizationProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def add_item(
        self,
        profile_id: str,
        *,
        kind: OrganizationMemoryKind,
        label: str,
        value: str,
        source: str,
        aliases: list[str] | None = None,
    ) -> OrganizationProfile:
        current = self._require_active(profile_id)
        item = OrganizationMemoryItem(
            kind=kind,
            label=label.strip(),
            value=value.strip(),
            source=source.strip(),
            aliases=[item.strip() for item in (aliases or []) if item.strip()],
        )
        revised = current.model_copy(update={
            "version": current.version + 1,
            "items": [*current.items, item],
            "updated_at": utc_now(),
        })
        self._write(revised)
        return revised

    def delete_item(self, profile_id: str, item_id: str) -> OrganizationProfile:
        current = self._require_active(profile_id)
        found = False
        items = []
        for item in current.items:
            if item.id == item_id:
                found = True
                items.append(item.model_copy(update={
                    "status": "deleted",
                    "deleted_at": utc_now(),
                }))
            else:
                items.append(item)
        if not found:
            raise ValueError("找不到要删除的组织规则")
        revised = current.model_copy(update={
            "version": current.version + 1,
            "items": items,
            "updated_at": utc_now(),
        })
        self._write(revised)
        return revised

    def delete_profile(self, profile_id: str) -> OrganizationProfile:
        current = self._require_active(profile_id)
        revised = current.model_copy(update={
            "version": current.version + 1,
            "status": "deleted",
            "deleted_at": utc_now(),
            "updated_at": utc_now(),
        })
        self._write(revised)
        return revised

    def active_context(self, profile_id: str) -> dict[str, list[dict[str, object]]]:
        """提供给 Harness 的可见上下文；不返回已删除项目。"""
        profile = self._require_active(profile_id)
        grouped: dict[str, list[dict[str, object]]] = {
            "brand_rules": [],
            "proper_nouns": [],
            "publishing_restrictions": [],
            "template_rules": [],
        }
        keys = {
            "brand_rule": "brand_rules",
            "proper_noun": "proper_nouns",
            "publishing_restriction": "publishing_restrictions",
            "template_rule": "template_rules",
        }
        for item in profile.items:
            if item.status != "active":
                continue
            grouped[keys[item.kind]].append({
                "id": item.id,
                "label": item.label,
                "value": item.value,
                "aliases": item.aliases,
                "source": item.source,
                "profile_version": profile.version,
            })
        return grouped

    def _require_active(self, profile_id: str) -> OrganizationProfile:
        profile = self.load(profile_id)
        if profile.status != "active":
            raise ValueError("组织配置已删除")
        return profile

    def _profile_dir(self, profile_id: str) -> Path:
        if not profile_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in profile_id):
            raise ValueError("组织配置 ID 非法")
        directory = (self.root / profile_id).resolve()
        if directory.parent != self.root.resolve():
            raise ValueError("组织配置 ID 非法")
        return directory

    def _write(self, profile: OrganizationProfile) -> None:
        directory = self._profile_dir(profile.id)
        directory.mkdir(parents=True, exist_ok=True)
        serialized = profile.model_dump_json(indent=2)
        for path in (
            directory / f"profile_v{profile.version}.json",
            directory / "current.json",
        ):
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_text(serialized, encoding="utf-8")
            temporary.replace(path)
        index_path = self.root / "index.json"
        index = {"profiles": []}
        if index_path.exists():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                index = {"profiles": []}
        entries = [
            item for item in index.get("profiles", []) if item.get("id") != profile.id
        ]
        entries.append({
            "id": profile.id,
            "name": profile.name,
            "scenario": profile.scenario,
            "version": profile.version,
            "status": profile.status,
            "updated_at": profile.updated_at.isoformat(),
        })
        temporary = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps({"profiles": entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(index_path)
