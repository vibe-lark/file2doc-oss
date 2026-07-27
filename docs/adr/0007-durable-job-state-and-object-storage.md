# Durable job state and object storage

File2Doc uses environment-isolated, Volcengine-managed PostgreSQL as the source
of truth for Parse Jobs, Progress Events, execution leases, retries, and the
first durable work queue. Source files and Result Packages are stored in a
dedicated TOS object storage namespace so API and worker replicas do not share
a writable local filesystem. File2Doc owns database schema migrations but does
not operate PostgreSQL inside VKE. Redis is not required for the first
implementation because PostgreSQL leasing provides durable dispatch with fewer
operational dependencies; a separate broker may be introduced later if
measured queue pressure justifies it.

Every Work Item claim creates an Execution Attempt with a unique lease token.
Workers renew that lease on a bounded heartbeat interval, and all completion,
failure, and artifact-publication transactions are fenced by the token,
attempt number, owner, and unexpired lease. An expired attempt can be claimed
again until its maximum attempt count is reached; exhaustion fails the Work
Item and Parse Job with `work_item_attempts_exhausted`. Attempt output is
written under an attempt-specific TOS prefix, so a late worker cannot overwrite
the objects selected by a newer successful publication.

The Parse Job status response exposes current Work Item attempts and heartbeat
timestamps. Runtime metrics expose eligible queue depth, oldest eligible age,
active and expired leases, retries, and terminal Work Item failures. These
queries are also the contract for later queue-driven autoscaling.

SQLite remains available only for isolated local development. Production
cutover is deliberately destructive: existing SQLite job records and PVC
artifacts are neither migrated nor retained, and previously issued job IDs are
not readable after cutover. SIT must verify the new persistence and recovery
path before the production PVC is removed.

ASR model files are immutable contents of a dedicated ASR worker image target,
not shared runtime state. This allows ASR replicas to start independently after
the destructive PVC cutover. KEDA remains a cluster platform component;
File2Doc releases only the authentication and scaling resources for its worker
pools.

Video ASR and frame extraction are separate Work Items. Their terminal failures
are inputs to Assembly rather than automatic Parse Job failures. Assembly emits
a partial result with stable warnings whenever at least one modality remains
usable, and fails only when no coherent transcript or frame result exists.
