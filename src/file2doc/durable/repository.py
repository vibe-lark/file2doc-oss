from __future__ import annotations

import hashlib
import mimetypes
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from file2doc.durable.schema import SCHEMA_SQL, SCHEMA_VERSION


@dataclass(frozen=True)
class ClaimedWorkItem:
    work_item_id: str
    job_id: str
    kind: str
    payload: dict[str, Any]
    attempt_count: int
    lease_owner: str
    lease_token: str
    lease_expires_at: datetime


class LeaseLostError(RuntimeError):
    pass


class PostgresJobRepository:
    def __init__(self, database_url: str) -> None:
        if not database_url:
            raise ValueError("database_url must not be empty")
        self.database_url = database_url

    def migrate(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(SCHEMA_SQL)
            cursor.execute(
                """
                    insert into file2doc_schema_migrations (version)
                    values (%s)
                    on conflict (version) do nothing
                    """,
                (SCHEMA_VERSION,),
            )

    def check_readiness(self) -> None:
        with self._connect() as connection:
            row = connection.execute("select 1 as ready").fetchone()
        if row is None or row["ready"] != 1:
            raise RuntimeError("PostgreSQL readiness query failed")

    def create_text_job(
        self,
        *,
        job_id: str,
        filename: str,
        content_type: str | None,
        source_bytes: bytes,
        source_object_key: str,
        parser_profile: str,
        retention: str,
    ) -> dict[str, Any]:
        return self.create_parse_job(
            job_id=job_id,
            filename=filename,
            content_type=content_type,
            source_bytes=source_bytes,
            source_object_key=source_object_key,
            parser_profile=parser_profile,
            retention=retention,
            work_kinds=("text_parse",),
        )

    def create_parse_job(
        self,
        *,
        job_id: str,
        filename: str,
        content_type: str | None,
        source_bytes: bytes,
        source_object_key: str,
        parser_profile: str,
        retention: str,
        work_kinds: tuple[str, ...],
    ) -> dict[str, Any]:
        return self.create_parse_job_from_metadata(
            job_id=job_id,
            filename=filename,
            content_type=content_type,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            source_size_bytes=len(source_bytes),
            source_object_key=source_object_key,
            parser_profile=parser_profile,
            retention=retention,
            work_kinds=work_kinds,
        )

    def create_parse_job_from_metadata(
        self,
        *,
        job_id: str,
        filename: str,
        content_type: str | None,
        source_sha256: str,
        source_size_bytes: int,
        source_object_key: str,
        parser_profile: str,
        retention: str,
        work_kinds: tuple[str, ...],
    ) -> dict[str, Any]:
        if not work_kinds:
            raise ValueError("work_kinds must not be empty")
        now = _now()
        work_item_ids = [_id("work") for _ in work_kinds]
        assembly_id = _id("work")
        expires_at = now + _retention_delta(retention)
        source_media_type = (
            content_type
            or mimetypes.guess_type(filename)[0]
            or "application/octet-stream"
        )
        job = {
            "job_id": job_id,
            "status": "queued",
            "stage": "queued",
            "percent": 0,
            "poll_url": f"/parse-jobs/{job_id}",
            "created_at": _iso(now),
            "expires_at": _iso(expires_at),
            "source": {
                "filename": filename,
                "content_type": source_media_type,
                "sha256": source_sha256,
                "size_bytes": source_size_bytes,
                "path": f"source/{filename}",
            },
            "parser_profile": parser_profile,
            "retention": retention,
            "latest_progress": _progress(
                "queued", 0, "Parse job accepted", created_at=now
            ),
            "warnings_count": 0,
            "error": None,
            "result": None,
        }
        payload = {
            "source_object_key": source_object_key,
            "filename": filename,
            "content_type": source_media_type,
            "parser_profile": parser_profile,
        }
        with self._connect() as connection:
            connection.execute(
                """
                insert into parse_jobs (
                    job_id, status, stage, percent, document,
                    created_at, updated_at, expires_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    job_id,
                    job["status"],
                    job["stage"],
                    job["percent"],
                    Jsonb(job),
                    now,
                    now,
                    expires_at,
                ),
            )
            self._append_event(
                connection, job_id, "queued", 0, "Parse job accepted", now
            )
            self._append_event(
                connection, job_id, "intaking", 10, "Source file stored", now
            )
            for work_item_id, kind in zip(work_item_ids, work_kinds, strict=True):
                work_payload = payload | {
                    "allow_partial": len(work_kinds) > 1,
                }
                connection.execute(
                    """
                    insert into work_items (
                        work_item_id, job_id, kind, status, payload,
                        available_at, created_at, updated_at
                    ) values (%s, %s, %s, 'pending', %s, %s, %s, %s)
                    """,
                    (work_item_id, job_id, kind, Jsonb(work_payload), now, now, now),
                )
            connection.execute(
                """
                insert into work_items (
                    work_item_id, job_id, kind, status, payload, depends_on,
                    available_at, created_at, updated_at
                ) values (%s, %s, 'assembly', 'pending', '{}'::jsonb,
                          %s, %s, %s, %s)
                """,
                (assembly_id, job_id, work_item_ids, now, now, now),
            )
        return _public_create_response(job)

    def read_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "select document from parse_jobs where job_id = %s", (job_id,)
            ).fetchone()
            execution = self._read_job_execution(connection, job_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "job_not_found"})
        return dict(row["document"]) | {"execution": execution}

    def read_all_jobs(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "select document from parse_jobs order by created_at"
            ).fetchall()
        return [row["document"] for row in rows]

    def read_events(self, job_id: str, *, after: str | None = None) -> dict[str, Any]:
        self.read_job(job_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                select document from progress_events
                where job_id = %s order by sequence
                """,
                (job_id,),
            ).fetchall()
        events = [row["document"] for row in rows]
        if after is not None:
            events = _events_after(events, after)
        return {
            "events": events,
            "next_after": events[-1]["event_id"] if events else after,
        }

    def claim_work_item(
        self,
        kind: str,
        *,
        worker_id: str,
        lease_seconds: int = 300,
    ) -> ClaimedWorkItem | None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        lease_token = _id("lease")
        now = _now()
        with self._connect() as connection:
            self._fail_exhausted_expired_leases(connection, kind, now)
            candidate = connection.execute(
                """
                select work_item_id, job_id, kind, payload, attempt_count,
                       lease_token as previous_lease_token
                from work_items as candidate_work
                where kind = %s
                  and attempt_count < max_attempts
                  and not exists (
                      select 1
                      from unnest(candidate_work.depends_on) as dependency(work_item_id)
                      join work_items as required
                        on required.work_item_id = dependency.work_item_id
                      where required.status not in ('completed', 'failed', 'cancelled')
                  )
                  and (
                      (status = 'pending' and available_at <= %s)
                      or (status = 'leased' and lease_expires_at <= %s)
                  )
                order by available_at, created_at
                for update skip locked
                limit 1
                """,
                (kind, now, now),
            ).fetchone()
            if candidate is None:
                return None
            previous_lease_token = candidate["previous_lease_token"]
            if previous_lease_token:
                self._expire_attempt(
                    connection,
                    previous_lease_token,
                    now,
                    code="execution_lease_expired",
                    message="Worker heartbeat stopped before the lease expired",
                )
            attempt_count = candidate["attempt_count"] + 1
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            row = connection.execute(
                """
                update work_items
                set status = 'leased',
                    lease_owner = %s,
                    lease_token = %s,
                    lease_expires_at = %s,
                    heartbeat_at = %s,
                    attempt_count = %s,
                    updated_at = %s
                where work_item_id = %s
                returning work_item_id, job_id, kind, payload, attempt_count,
                          lease_owner, lease_token, lease_expires_at
                """,
                (
                    worker_id,
                    lease_token,
                    lease_expires_at,
                    now,
                    attempt_count,
                    now,
                    candidate["work_item_id"],
                ),
            ).fetchone()
            connection.execute(
                """
                insert into execution_attempts (
                    work_item_id, attempt_number, lease_token, worker_id,
                    status, started_at, heartbeat_at, lease_expires_at
                ) values (%s, %s, %s, %s, 'active', %s, %s, %s)
                """,
                (
                    candidate["work_item_id"],
                    attempt_count,
                    lease_token,
                    worker_id,
                    now,
                    now,
                    lease_expires_at,
                ),
            )
            self._mark_job_running(connection, row["job_id"], row["kind"])
        return ClaimedWorkItem(**row)

    def renew_lease(
        self,
        claim: ClaimedWorkItem,
        *,
        lease_seconds: int,
    ) -> bool:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        now = _now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            updated = connection.execute(
                """
                update work_items
                set heartbeat_at = %s, lease_expires_at = %s, updated_at = %s
                where work_item_id = %s and status = 'leased'
                  and lease_owner = %s and lease_token = %s
                  and attempt_count = %s and lease_expires_at > %s
                returning work_item_id
                """,
                (
                    now,
                    lease_expires_at,
                    now,
                    claim.work_item_id,
                    claim.lease_owner,
                    claim.lease_token,
                    claim.attempt_count,
                    now,
                ),
            ).fetchone()
            if updated is None:
                return False
            connection.execute(
                """
                update execution_attempts
                set heartbeat_at = %s, lease_expires_at = %s
                where lease_token = %s and status = 'active'
                """,
                (now, lease_expires_at, claim.lease_token),
            )
        return True

    def assert_lease_active(self, claim: ClaimedWorkItem) -> None:
        with self._connect() as connection:
            row = connection.execute(
                """
                select 1 from work_items
                where work_item_id = %s and status = 'leased'
                  and lease_owner = %s and lease_token = %s
                  and attempt_count = %s and lease_expires_at > now()
                """,
                (
                    claim.work_item_id,
                    claim.lease_owner,
                    claim.lease_token,
                    claim.attempt_count,
                ),
            ).fetchone()
        if row is None:
            raise LeaseLostError("Work Item lease is no longer active")

    def complete_text_parse(
        self,
        claim: ClaimedWorkItem,
        *,
        result_object_key: str,
        diagnostics: dict[str, Any],
        warnings: list[dict[str, Any]],
    ) -> str:
        result = {
            "result_object_key": result_object_key,
            "diagnostics": diagnostics,
            "warnings": warnings,
        }
        self.complete_work_item(claim, result=result)
        return self._assembly_work_item_id(claim.job_id)

    def complete_work_item(
        self,
        claim: ClaimedWorkItem,
        *,
        result: dict[str, Any],
    ) -> None:
        now = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                update work_items
                set status = 'completed', result = %s, lease_owner = null,
                    lease_token = null, lease_expires_at = null,
                    heartbeat_at = null, updated_at = %s
                where work_item_id = %s and status = 'leased'
                  and lease_owner = %s and lease_token = %s
                  and attempt_count = %s and lease_expires_at > %s
                returning job_id
                """,
                (
                    Jsonb(result),
                    now,
                    claim.work_item_id,
                    claim.lease_owner,
                    claim.lease_token,
                    claim.attempt_count,
                    now,
                ),
            ).fetchone()
            if updated is None:
                raise LeaseLostError("Work Item lease is no longer active")
            self._complete_attempt(connection, claim, now)
            self._set_job_progress(
                connection,
                claim.job_id,
                status="running",
                stage="assembling",
                percent=80,
                message="Assembling result package",
            )

    def read_work_items(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                select work_item_id, kind, status, result, last_error,
                       attempt_count, max_attempts
                from work_items where job_id = %s order by created_at
                """,
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _assembly_work_item_id(self, job_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "select work_item_id from work_items where job_id = %s and kind = 'assembly'",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Assembly Work Item missing for {job_id}")
        return str(row["work_item_id"])

    def fail_work_item(
        self,
        claim: ClaimedWorkItem,
        *,
        code: str,
        message: str,
        retryable: bool,
        retry_delay_seconds: int = 5,
    ) -> None:
        now = _now()
        error = {"code": code, "message": message}
        with self._connect() as connection:
            row = connection.execute(
                """
                select attempt_count, max_attempts
                from work_items
                where work_item_id = %s and status = 'leased'
                  and lease_owner = %s and lease_token = %s
                  and attempt_count = %s and lease_expires_at > %s
                for update
                """,
                (
                    claim.work_item_id,
                    claim.lease_owner,
                    claim.lease_token,
                    claim.attempt_count,
                    now,
                ),
            ).fetchone()
            if row is None:
                return
            self._fail_attempt(connection, claim, now, error)
            should_retry = retryable and row["attempt_count"] < row["max_attempts"]
            if should_retry:
                connection.execute(
                    """
                    update work_items
                    set status = 'pending', available_at = %s,
                        lease_owner = null, lease_token = null,
                        lease_expires_at = null,
                        heartbeat_at = null, last_error = %s, updated_at = %s
                    where work_item_id = %s
                    """,
                    (
                        now + timedelta(seconds=max(0, retry_delay_seconds)),
                        Jsonb(error),
                        now,
                        claim.work_item_id,
                    ),
                )
                return

            connection.execute(
                """
                update work_items
                set status = 'failed', lease_owner = null,
                    lease_token = null, lease_expires_at = null,
                    heartbeat_at = null,
                    last_error = %s, updated_at = %s
                where work_item_id = %s
                """,
                (Jsonb(error), now, claim.work_item_id),
            )
            if claim.payload.get("allow_partial"):
                self._set_job_progress(
                    connection,
                    claim.job_id,
                    status="running",
                    stage="assembling",
                    percent=80,
                    message="A modality failed; assembling partial result",
                )
            else:
                self._mark_job_failed(connection, claim.job_id, error, now)

    def complete_assembly(
        self,
        claim: ClaimedWorkItem,
        *,
        job_document: dict[str, Any],
        artifacts: list[dict[str, Any]],
    ) -> None:
        now = _now()
        with self._connect() as connection:
            updated = connection.execute(
                """
                update work_items
                set status = 'completed', result = %s, lease_owner = null,
                    lease_token = null, lease_expires_at = null,
                    heartbeat_at = null, updated_at = %s
                where work_item_id = %s and status = 'leased'
                  and lease_owner = %s and lease_token = %s
                  and attempt_count = %s and lease_expires_at > %s
                returning job_id
                """,
                (
                    Jsonb({"artifact_count": len(artifacts)}),
                    now,
                    claim.work_item_id,
                    claim.lease_owner,
                    claim.lease_token,
                    claim.attempt_count,
                    now,
                ),
            ).fetchone()
            if updated is None:
                raise LeaseLostError("Work Item lease is no longer active")
            self._complete_attempt(connection, claim, now)
            for artifact in artifacts:
                connection.execute(
                    """
                    insert into artifact_publications (
                        job_id, artifact_id, work_item_id, state, object_key,
                        media_type, sha256, size_bytes, document,
                        created_at, published_at
                    ) values (%s, %s, %s, 'published', %s, %s, %s, %s, %s, %s, %s)
                    on conflict (job_id, artifact_id) do nothing
                    """,
                    (
                        claim.job_id,
                        artifact["artifact_id"],
                        claim.work_item_id,
                        artifact["object_key"],
                        artifact["media_type"],
                        artifact["sha256"],
                        artifact["size_bytes"],
                        Jsonb(artifact["document"]),
                        now,
                        now,
                    ),
                )
            connection.execute(
                """
                update parse_jobs
                set status = %s, stage = %s, percent = 100,
                    document = %s, updated_at = %s
                where job_id = %s
                """,
                (
                    job_document["status"],
                    job_document["stage"],
                    Jsonb(job_document),
                    now,
                    claim.job_id,
                ),
            )
            self._append_event(
                connection,
                claim.job_id,
                job_document["stage"],
                100,
                "Result package assembled",
                now,
            )

    def read_runtime_metrics(self) -> dict[str, int | float]:
        with self._connect() as connection:
            row = connection.execute(
                """
                with metric_work as (
                    select work.*,
                           not exists (
                               select 1
                               from unnest(work.depends_on) as dependency(work_item_id)
                               join work_items as required
                                 on required.work_item_id = dependency.work_item_id
                               where required.status not in (
                                   'completed', 'failed', 'cancelled'
                               )
                           ) as dependencies_ready
                    from work_items as work
                )
                select
                    count(*) filter (
                        where dependencies_ready and (
                            (status = 'pending' and available_at <= now())
                            or (status = 'leased' and lease_expires_at <= now()
                                and attempt_count < max_attempts)
                        )
                    ) as queue_depth,
                    coalesce(extract(epoch from (
                        now() - min(
                            case
                                when dependencies_ready
                                     and status = 'pending'
                                     and available_at <= now()
                                    then available_at
                                when dependencies_ready
                                     and status = 'leased'
                                     and lease_expires_at <= now()
                                     and attempt_count < max_attempts
                                    then lease_expires_at
                            end
                        )
                    )), 0) as oldest_eligible_age_seconds,
                    count(*) filter (
                        where status = 'leased' and lease_expires_at > now()
                    ) as active_leases,
                    count(*) filter (
                        where status = 'leased' and lease_expires_at <= now()
                    ) as expired_leases,
                    coalesce(sum(greatest(attempt_count - 1, 0)), 0) as retries,
                    count(*) filter (where status = 'failed') as terminal_failures
                from metric_work
                """
            ).fetchone()
        return {
            "queue_depth": int(row["queue_depth"]),
            "oldest_eligible_age_seconds": max(
                0.0, float(row["oldest_eligible_age_seconds"])
            ),
            "active_leases": int(row["active_leases"]),
            "expired_leases": int(row["expired_leases"]),
            "retries": int(row["retries"]),
            "terminal_failures": int(row["terminal_failures"]),
        }

    def _read_job_execution(
        self,
        connection: psycopg.Connection,
        job_id: str,
    ) -> dict[str, Any]:
        rows = connection.execute(
            """
            select work.kind, work.status, work.attempt_count, work.max_attempts,
                   attempt.worker_id, attempt.status as attempt_status,
                   attempt.heartbeat_at, attempt.lease_expires_at
            from work_items as work
            left join execution_attempts as attempt
              on attempt.work_item_id = work.work_item_id
             and attempt.attempt_number = work.attempt_count
            where work.job_id = %s
            order by work.created_at,
                     case when work.kind = 'assembly' then 1 else 0 end,
                     work.kind
            """,
            (job_id,),
        ).fetchall()
        return {
            "work_items": [
                {
                    "kind": row["kind"],
                    "status": row["status"],
                    "attempt": row["attempt_count"],
                    "max_attempts": row["max_attempts"],
                    "worker_id": row["worker_id"],
                    "attempt_status": row["attempt_status"],
                    "last_heartbeat_at": _iso_or_none(row["heartbeat_at"]),
                    "lease_expires_at": _iso_or_none(row["lease_expires_at"]),
                }
                for row in rows
            ]
        }

    def _fail_exhausted_expired_leases(
        self,
        connection: psycopg.Connection,
        kind: str,
        now: datetime,
    ) -> None:
        rows = connection.execute(
            """
            select work_item_id, job_id, lease_token
            from work_items
            where kind = %s and status = 'leased'
              and lease_expires_at <= %s and attempt_count >= max_attempts
            for update skip locked
            """,
            (kind, now),
        ).fetchall()
        error = {
            "code": "work_item_attempts_exhausted",
            "message": "Work Item did not complete before all execution leases expired",
        }
        for row in rows:
            if row["lease_token"]:
                self._expire_attempt(
                    connection,
                    row["lease_token"],
                    now,
                    code=error["code"],
                    message=error["message"],
                )
            connection.execute(
                """
                update work_items
                set status = 'failed', lease_owner = null, lease_token = null,
                    lease_expires_at = null, heartbeat_at = null,
                    last_error = %s, updated_at = %s
                where work_item_id = %s
                """,
                (Jsonb(error), now, row["work_item_id"]),
            )
            payload = connection.execute(
                "select payload from work_items where work_item_id = %s",
                (row["work_item_id"],),
            ).fetchone()["payload"]
            if not payload.get("allow_partial"):
                self._mark_job_failed(connection, row["job_id"], error, now)

    def _expire_attempt(
        self,
        connection: psycopg.Connection,
        lease_token: str,
        now: datetime,
        *,
        code: str,
        message: str,
    ) -> None:
        connection.execute(
            """
            update execution_attempts
            set status = 'expired', finished_at = %s, error = %s
            where lease_token = %s and status = 'active'
            """,
            (now, Jsonb({"code": code, "message": message}), lease_token),
        )

    def _complete_attempt(
        self,
        connection: psycopg.Connection,
        claim: ClaimedWorkItem,
        now: datetime,
    ) -> None:
        connection.execute(
            """
            update execution_attempts
            set status = 'completed', heartbeat_at = %s, finished_at = %s
            where lease_token = %s and status = 'active'
            """,
            (now, now, claim.lease_token),
        )

    def _fail_attempt(
        self,
        connection: psycopg.Connection,
        claim: ClaimedWorkItem,
        now: datetime,
        error: dict[str, str],
    ) -> None:
        connection.execute(
            """
            update execution_attempts
            set status = 'failed', heartbeat_at = %s,
                finished_at = %s, error = %s
            where lease_token = %s and status = 'active'
            """,
            (now, now, Jsonb(error), claim.lease_token),
        )

    def _mark_job_failed(
        self,
        connection: psycopg.Connection,
        job_id: str,
        error: dict[str, str],
        now: datetime,
    ) -> None:
        job_row = connection.execute(
            "select document from parse_jobs where job_id = %s for update",
            (job_id,),
        ).fetchone()
        if job_row is None:
            return
        job = job_row["document"]
        if job["status"] in {"completed", "completed_with_warnings", "failed"}:
            return
        job.update(
            {
                "status": "failed",
                "stage": "failed",
                "percent": 100,
                "error": error,
                "latest_progress": _progress(
                    "failed", 100, error["message"], created_at=now
                ),
            }
        )
        connection.execute(
            """
            update parse_jobs
            set status = 'failed', stage = 'failed', percent = 100,
                document = %s, updated_at = %s where job_id = %s
            """,
            (Jsonb(job), now, job_id),
        )
        self._append_event(connection, job_id, "failed", 100, error["message"], now)

    def read_artifact_record(self, job_id: str, artifact_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                select document, object_key, media_type
                from artifact_publications
                where job_id = %s and artifact_id = %s and state = 'published'
                """,
                (job_id, artifact_id),
            ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "artifact_not_found"})
        return row["document"] | {
            "object_key": row["object_key"],
            "media_type": row["media_type"],
        }

    def read_work_item_result(self, job_id: str, kind: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                select result from work_items
                where job_id = %s and kind = %s and status = 'completed'
                """,
                (job_id, kind),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"Completed {kind} Work Item not found for {job_id}")
        return row["result"]

    def _mark_job_running(
        self,
        connection: psycopg.Connection,
        job_id: str,
        kind: str,
    ) -> None:
        if kind == "text_parse":
            self._set_job_progress(
                connection,
                job_id,
                status="running",
                stage="parser_started",
                percent=30,
                message="Parser started",
            )

    def _set_job_progress(
        self,
        connection: psycopg.Connection,
        job_id: str,
        *,
        status: str,
        stage: str,
        percent: int,
        message: str,
    ) -> None:
        now = _now()
        row = connection.execute(
            "select document from parse_jobs where job_id = %s for update", (job_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"Parse Job not found: {job_id}")
        job = row["document"]
        if job["status"] in {
            "completed",
            "completed_with_warnings",
            "failed",
            "expired",
        }:
            return
        job.update(
            {
                "status": status,
                "stage": stage,
                "percent": percent,
                "latest_progress": _progress(stage, percent, message, created_at=now),
            }
        )
        connection.execute(
            """
            update parse_jobs
            set status = %s, stage = %s, percent = %s,
                document = %s, updated_at = %s
            where job_id = %s
            """,
            (status, stage, percent, Jsonb(job), now, job_id),
        )
        self._append_event(connection, job_id, stage, percent, message, now)

    def _append_event(
        self,
        connection: psycopg.Connection,
        job_id: str,
        stage: str,
        percent: int,
        message: str,
        created_at: datetime,
    ) -> None:
        row = connection.execute(
            """
            select coalesce(max(sequence), 0) + 1 as next_sequence
            from progress_events where job_id = %s
            """,
            (job_id,),
        ).fetchone()
        event = {
            "event_id": _id("evt"),
            "stage": stage,
            "percent": percent,
            "message": message,
            "detail": {},
            "created_at": _iso(created_at),
        }
        connection.execute(
            """
            insert into progress_events (
                event_id, job_id, sequence, document, created_at
            ) values (%s, %s, %s, %s, %s)
            """,
            (
                event["event_id"],
                job_id,
                row["next_sequence"],
                Jsonb(event),
                created_at,
            ),
        )

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self.database_url, row_factory=dict_row)


def new_job_id() -> str:
    return _id("job")


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(12)}"


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _iso_or_none(value: datetime | None) -> str | None:
    return _iso(value) if value is not None else None


def _retention_delta(retention: str) -> timedelta:
    if retention == "expired":
        return -timedelta(seconds=1)
    if retention == "short":
        return timedelta(hours=6)
    if retention == "long":
        return timedelta(days=30)
    return timedelta(days=7)


def _progress(
    stage: str,
    percent: int,
    message: str,
    *,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "percent": percent,
        "message": message,
        "detail": {},
        "created_at": _iso(created_at),
    }


def _public_create_response(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: job[key]
        for key in (
            "job_id",
            "status",
            "stage",
            "percent",
            "poll_url",
            "created_at",
            "expires_at",
        )
    }


def _events_after(events: list[dict[str, Any]], after: str) -> list[dict[str, Any]]:
    for index, event in enumerate(events):
        if event["event_id"] == after:
            return events[index + 1 :]
    return events
