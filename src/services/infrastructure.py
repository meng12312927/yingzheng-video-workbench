"""本地默认、PostgreSQL/Redis 可替换的基础设施边界。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional, Protocol


class IdempotencyBackend(Protocol):
    def acquire(self, key: str, ttl_seconds: int) -> bool:
        ...

    def release(self, key: str) -> None:
        ...


class LocalIdempotencyBackend:
    """单机默认实现；写入 JSON 后可跨进程重启恢复。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        now = time.time()
        with self._lock:
            values = self._load()
            values = {
                item: expires for item, expires in values.items() if expires > now
            }
            if key in values:
                self._write(values)
                return False
            values[key] = now + max(1, ttl_seconds)
            self._write(values)
            return True

    def release(self, key: str) -> None:
        with self._lock:
            values = self._load()
            values.pop(key, None)
            self._write(values)

    def _load(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        try:
            return {
                str(key): float(value)
                for key, value in json.loads(
                    self.path.read_text(encoding="utf-8")
                ).items()
            }
        except (OSError, ValueError, TypeError):
            return {}

    def _write(self, values: dict[str, float]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)


class RedisIdempotencyBackend:
    """部署模式使用 SET NX EX；只有配置 REDIS_URL 时才实例化。"""

    def __init__(self, redis_url: str, namespace: str = "yingzheng:idempotency"):
        import redis

        self.client = redis.Redis.from_url(redis_url, decode_responses=True)
        self.namespace = namespace

    def acquire(self, key: str, ttl_seconds: int) -> bool:
        return bool(
            self.client.set(
                f"{self.namespace}:{key}", "1", nx=True, ex=max(1, ttl_seconds)
            )
        )

    def release(self, key: str) -> None:
        self.client.delete(f"{self.namespace}:{key}")


def build_idempotency_backend(task_dir: Path, redis_url: str = "") -> IdempotencyBackend:
    if redis_url:
        return RedisIdempotencyBackend(redis_url)
    return LocalIdempotencyBackend(Path(task_dir) / "idempotency_keys.json")


class TaskMetadataRepository(Protocol):
    def upsert(
        self,
        *,
        task_id: str,
        state: str,
        state_version: int,
        plan_version: Optional[int],
    ) -> None:
        ...

    def get(self, task_id: str) -> Optional[dict]:
        ...


class PostgresTaskMetadataRepository:
    """只存任务索引与版本；视频、转录和大产物仍由对象/文件存储管理。"""

    def __init__(self, database_url: str):
        import psycopg

        self.psycopg = psycopg
        self.database_url = database_url

    def initialise(self) -> None:
        with self.psycopg.connect(self.database_url) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS task_metadata (
                    task_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    plan_version INTEGER,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )

    def upsert(
        self,
        *,
        task_id: str,
        state: str,
        state_version: int,
        plan_version: Optional[int],
    ) -> None:
        with self.psycopg.connect(self.database_url) as connection:
            connection.execute(
                """
                INSERT INTO task_metadata(task_id, state, state_version, plan_version)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(task_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    state_version = EXCLUDED.state_version,
                    plan_version = EXCLUDED.plan_version,
                    updated_at = NOW()
                """,
                (task_id, state, state_version, plan_version),
            )

    def get(self, task_id: str) -> Optional[dict]:
        with self.psycopg.connect(self.database_url) as connection:
            row = connection.execute(
                """
                SELECT task_id, state, state_version, plan_version, updated_at
                FROM task_metadata WHERE task_id = %s
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "task_id": row[0],
            "state": row[1],
            "state_version": row[2],
            "plan_version": row[3],
            "updated_at": row[4].isoformat(),
        }
