

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
import asyncio
import logging
from typing import Optional
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.gateway.config import get_settings
from src.gateway.domain.exceptions import AuthorizationException
from src.gateway.domain.identity import Principal
from src.gateway.infrastructure.persistence.principal_context import get_bound_principal, set_principal_context

logger = logging.getLogger(__name__)

def normalize_database_url(url: str) -> str:

    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_migration_engine: Optional[AsyncEngine] = None
_migration_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_worker_engine: Optional[AsyncEngine] = None
_worker_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_engine_loop = None


@event.listens_for(Session, "after_begin")
def _install_bound_principal_context(session, transaction, connection) -> None:
    principal = get_bound_principal()
    if principal is None:
        return
    if not principal.active:
        raise AuthorizationException()
    try:
        member_id = str(UUID(principal.subject_id))
    except ValueError as exc:
        raise AuthorizationException() from exc
    if connection.dialect.name != "postgresql":
        return
    connection.execute(
        text(
            "SELECT set_config('app.principal_id', :member_id, true), "
            "set_config('app.principal_restricted', :restricted, true)"
        ),
        {
            "member_id": member_id,
            "restricted": "true" if principal.restricted else "false",
        },
    )

def get_engine() -> AsyncEngine:

    global _engine, _session_factory, _engine_loop
    current_settings = get_settings()
    db_url = normalize_database_url(current_settings.database.url)
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if _engine is not None:
        if (
            _engine.url.render_as_string(hide_password=False) != db_url
            or (_engine_loop is not None and current_loop is not None and _engine_loop is not current_loop)
        ):
            _engine.sync_engine.dispose(close=False)
            _engine = None
            _session_factory = None

    if _engine is None:
        if "sqlite" in db_url:
            from sqlalchemy.pool import StaticPool
            _engine = create_async_engine(
                db_url,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
                echo=current_settings.gateway.log_level.upper() == "DEBUG" or getattr(current_settings.database, "echo", False),
            )
        else:
            _engine = create_async_engine(
                db_url,
                pool_size=getattr(current_settings.database, "pool_size", 20),
                max_overflow=getattr(current_settings.database, "max_overflow", 10),
                pool_pre_ping=True,
                pool_recycle=getattr(current_settings.database, "pool_recycle", 1800),
                pool_timeout=getattr(current_settings.database, "pool_timeout", 30.0),
                echo=current_settings.gateway.log_level.upper() == "DEBUG" or getattr(current_settings.database, "echo", False),
            )
        _engine_loop = current_loop
    return _engine

def get_session_factory() -> async_sessionmaker[AsyncSession]:

    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _session_factory


def get_migration_session_factory() -> async_sessionmaker[AsyncSession]:
    global _migration_engine, _migration_session_factory
    database = get_settings().database
    target_url = normalize_database_url(database.migration_url or database.url)
    if (
        _migration_engine is not None
        and _migration_engine.url.render_as_string(hide_password=False) != target_url
    ):
        _migration_engine = None
        _migration_session_factory = None
    if _migration_engine is None:
        _migration_engine = create_async_engine(target_url, pool_pre_ping=True)
    if _migration_session_factory is None:
        _migration_session_factory = async_sessionmaker(
            bind=_migration_engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _migration_session_factory


def get_worker_engine() -> AsyncEngine:
    global _worker_engine, _worker_session_factory
    worker_url = get_settings().database.worker_url
    if not worker_url:
        raise RuntimeError("A dedicated worker database URL is required")
    target_url = normalize_database_url(worker_url)
    if (
        _worker_engine is not None
        and _worker_engine.url.render_as_string(hide_password=False) != target_url
    ):
        _worker_engine.sync_engine.dispose(close=False)
        _worker_engine = None
        _worker_session_factory = None
    if _worker_engine is None:
        _worker_engine = create_async_engine(target_url, pool_pre_ping=True)
    return _worker_engine


def get_worker_session_factory() -> async_sessionmaker[AsyncSession]:
    global _worker_session_factory
    if _worker_session_factory is None:
        _worker_session_factory = async_sessionmaker(
            bind=get_worker_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
    return _worker_session_factory

def set_session_factory(factory: Optional[async_sessionmaker[AsyncSession]]) -> None:

    global _session_factory
    _session_factory = factory

async def get_db_session() -> AsyncGenerator[AsyncSession, None]:

    session_factory = get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def principal_session(
    factory: async_sessionmaker[AsyncSession],
    principal: Principal,
):
    async with factory.begin() as session:
        await set_principal_context(session, principal)
        yield session


async def _validate_runtime_database_connection(connection) -> None:
    runtime_tables = (
        "alembic_version",
        "api_key_scopes",
        "audit_events",
        "compatibility_principals",
        "document_chunks",
        "document_files",
        "document_revision_chunks",
        "document_revisions",
        "documents",
        "embedding_generations",
        "idempotency_records",
        "ingestion_jobs",
        "job_outbox",
        "knowledge_items",
        "knowledge_revisions",
        "login_throttle_buckets",
        "members",
        "mfa_factors",
        "mfa_recovery_codes",
        "password_credentials",
        "personal_api_keys",
        "provenance_links",
        "retrieval_units",
        "session_credentials",
        "session_families",
        "space_memberships",
        "workspaces",
    )
    role = (
        await connection.execute(
            text(
                "SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole, "
                "r.rolcreatedb, r.rolreplication "
                "FROM pg_roles r WHERE r.rolname = current_user"
            )
        )
    ).one()
    owned_count = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "AND c.relname = ANY(:tables) "
                "AND pg_get_userbyid(c.relowner) = current_user"
            ),
            {"tables": list(runtime_tables)},
        )
        or 0
    )
    owned_policy_functions = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "WHERE n.nspname = current_schema() "
                "AND p.proname = ANY(:functions) "
                "AND pg_get_userbyid(p.proowner) = current_user"
            ),
            {
                "functions": [
                    "gateway_actor_active",
                    "gateway_has_space_role",
                    "gateway_is_initial_space_owner",
                    "gateway_can_change_membership",
                ]
            },
        )
        or 0
    )
    public_policy_function_execute = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                "CROSS JOIN LATERAL aclexplode("
                "COALESCE(p.proacl, acldefault('f', p.proowner))"
                ") acl "
                "WHERE n.nspname = current_schema() "
                "AND p.proname = ANY(:functions) "
                "AND acl.grantee = 0 AND acl.privilege_type = 'EXECUTE'"
            ),
            {
                "functions": [
                    "gateway_actor_active",
                    "gateway_has_space_role",
                    "gateway_is_initial_space_owner",
                    "gateway_can_change_membership",
                ]
            },
        )
        or 0
    )
    inherited_privileged_roles = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM pg_roles inherited "
                "WHERE inherited.rolname <> current_user "
                "AND pg_has_role(current_user, inherited.oid, 'MEMBER') "
                "AND ("
                "inherited.rolsuper OR inherited.rolbypassrls "
                "OR inherited.rolcreaterole OR inherited.rolcreatedb "
                "OR inherited.rolreplication "
                "OR EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner = inherited.oid AND c.relname = ANY(:tables)) "
                "OR EXISTS (SELECT 1 FROM pg_proc p WHERE p.proowner = inherited.oid AND p.proname = ANY(:functions))"
                ")"
            ),
            {
                "tables": list(runtime_tables),
                "functions": [
                    "gateway_actor_active",
                    "gateway_has_space_role",
                    "gateway_is_initial_space_owner",
                    "gateway_can_change_membership",
                ],
            },
        )
        or 0
    )
    required_privileges = {
        "alembic_version": ("SELECT",),
        "api_key_scopes": ("SELECT", "INSERT", "DELETE"),
        "audit_events": ("SELECT", "INSERT"),
        "compatibility_principals": ("SELECT",),
        "document_chunks": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "document_files": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "document_revision_chunks": ("SELECT",),
        "document_revisions": ("SELECT", "INSERT"),
        "documents": ("SELECT", "INSERT"),
        "embedding_generations": ("SELECT",),
        "idempotency_records": ("SELECT", "INSERT", "UPDATE"),
        "ingestion_jobs": ("SELECT", "INSERT"),
        "job_outbox": ("INSERT",),
        "knowledge_items": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "knowledge_revisions": ("SELECT", "INSERT"),
        "login_throttle_buckets": ("SELECT", "INSERT", "UPDATE"),
        "members": ("SELECT", "INSERT", "UPDATE"),
        "mfa_factors": ("SELECT", "INSERT", "UPDATE"),
        "mfa_recovery_codes": ("SELECT", "INSERT", "UPDATE"),
        "password_credentials": ("SELECT", "INSERT", "UPDATE"),
        "personal_api_keys": ("SELECT", "INSERT", "UPDATE"),
        "provenance_links": ("SELECT", "INSERT"),
        "retrieval_units": ("SELECT", "INSERT"),
        "session_credentials": ("SELECT", "INSERT", "UPDATE"),
        "session_families": ("SELECT", "INSERT", "UPDATE"),
        "space_memberships": ("SELECT", "INSERT", "DELETE"),
        "workspaces": ("SELECT", "INSERT"),
    }
    missing_privileges = []
    if not await connection.scalar(
        text("SELECT has_schema_privilege(current_user, 'public', 'USAGE')")
    ):
        missing_privileges.append("public:USAGE")
    if await connection.scalar(
        text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    ):
        missing_privileges.append("public:CREATE:FORBIDDEN")
    for table_name, privileges in required_privileges.items():
        for privilege in privileges:
            allowed = await connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, :table_name, :privilege"
                    ")"
                ),
                {
                    "table_name": f"public.{table_name}",
                    "privilege": privilege,
                },
            )
            if not allowed:
                missing_privileges.append(f"{table_name}:{privilege}")
    all_table_privileges = [
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    ]
    server_version = int(
        await connection.scalar(text("SHOW server_version_num")) or 0
    )
    if server_version >= 170000:
        all_table_privileges.append("MAINTAIN")
    for table_name in runtime_tables:
        allowed_privileges = set(required_privileges[table_name])
        for privilege in all_table_privileges:
            if privilege in allowed_privileges:
                continue
            granted = await connection.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, :table_name, :privilege"
                    ")"
                ),
                {
                    "table_name": f"public.{table_name}",
                    "privilege": privilege,
                },
            )
            if granted:
                missing_privileges.append(
                    f"{table_name}:{privilege}:FORBIDDEN"
                )
    required_functions = (
        "public.gateway_actor_active()",
        "public.gateway_has_space_role(text,text[])",
        "public.gateway_is_initial_space_owner(text,uuid)",
        "public.gateway_can_change_membership(text,uuid,text)",
    )
    for function_name in required_functions:
        allowed = await connection.scalar(
            text(
                "SELECT has_function_privilege("
                "current_user, :function_name, 'EXECUTE'"
                ")"
            ),
            {"function_name": function_name},
        )
        if not allowed:
            missing_privileges.append(f"{function_name}:EXECUTE")
    required_column_privileges = {
        "compatibility_principals": ("revoked_at",),
        "documents": (
            "display_name",
            "current_revision_id",
            "archived_at",
            "revision",
            "updated_at",
        ),
        "document_revisions": ("staging_storage_key", "storage_key"),
        "ingestion_jobs": (
            "state",
            "cancellation_requested",
            "retry_requested",
            "updated_at",
        ),
        "retrieval_units": ("active", "deactivated_at"),
        "space_memberships": ("role", "updated_at"),
        "workspaces": ("name", "archived_at", "revision"),
    }
    for table_name, columns in required_column_privileges.items():
        for column_name in columns:
            allowed = await connection.scalar(
                text(
                    "SELECT has_column_privilege("
                    "current_user, :table_name, :column_name, 'UPDATE'"
                    ")"
                ),
                {
                    "table_name": f"public.{table_name}",
                    "column_name": column_name,
                },
            )
            if not allowed:
                missing_privileges.append(
                    f"{table_name}.{column_name}:UPDATE"
                )
    for table_name, permitted_columns in required_column_privileges.items():
        columns = tuple(
            await connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = :table_name"
                ),
                {"table_name": table_name},
            )
        )
        for column_name in set(columns) - set(permitted_columns):
            allowed = await connection.scalar(
                text(
                    "SELECT has_column_privilege("
                    "current_user, :table_name, :column_name, 'UPDATE'"
                    ")"
                ),
                {
                    "table_name": f"public.{table_name}",
                    "column_name": column_name,
                },
            )
            if allowed:
                missing_privileges.append(
                    f"{table_name}.{column_name}:FORBIDDEN_UPDATE"
                )
    for privilege in ("USAGE", "SELECT"):
        allowed = await connection.scalar(
            text(
                "SELECT has_sequence_privilege("
                "current_user, 'public.login_throttle_buckets_id_seq', :privilege"
                ")"
            ),
            {"privilege": privilege},
        )
        if not allowed:
            missing_privileges.append(
                f"login_throttle_buckets_id_seq:{privilege}"
            )
    if await connection.scalar(
        text(
            "SELECT has_sequence_privilege("
            "current_user, 'public.login_throttle_buckets_id_seq', 'UPDATE'"
            ")"
        )
    ):
        missing_privileges.append(
            "login_throttle_buckets_id_seq:UPDATE:FORBIDDEN"
        )
    if (
        role.rolsuper
        or role.rolbypassrls
        or role.rolcreaterole
        or role.rolcreatedb
        or role.rolreplication
        or owned_count
        or owned_policy_functions
        or public_policy_function_execute
        or inherited_privileged_roles
        or missing_privileges
    ):
        raise RuntimeError("Runtime database role violates the row-security contract")


async def validate_runtime_database_role(engine: AsyncEngine | None = None) -> None:
    target = engine or get_engine()
    async with target.connect() as connection:
        await _validate_runtime_database_connection(connection)


async def validate_runtime_database_connection(connection) -> None:
    await _validate_runtime_database_connection(connection)


async def _validate_worker_database_connection(connection) -> None:
    worker_tables = {
        "document_revision_chunks": ("SELECT", "INSERT"),
        "document_revisions": ("SELECT",),
        "documents": ("SELECT",),
        "embedding_generations": ("SELECT",),
        "ingestion_jobs": ("SELECT",),
        "job_outbox": ("SELECT", "INSERT"),
        "members": ("SELECT",),
        "operational_alerts": ("SELECT", "INSERT"),
        "retrieval_units": ("SELECT", "INSERT"),
        "space_memberships": ("SELECT",),
        "workspaces": ("SELECT",),
    }
    update_columns = {
        "documents": ("current_revision_id", "revision", "updated_at"),
        "document_revisions": (
            "staging_storage_key",
            "parser_version",
            "status",
            "failure_code",
            "ready_at",
            "activated_at",
        ),
        "ingestion_jobs": (
            "state",
            "progress",
            "attempt_count",
            "next_attempt_at",
            "cancellation_requested",
            "retry_requested",
            "last_error_code",
            "last_error_detail",
            "lease_owner",
            "lease_expires_at",
            "claim_token",
            "updated_at",
            "started_at",
            "finished_at",
        ),
        "job_outbox": (
            "state",
            "attempt_count",
            "last_error_code",
            "lease_owner",
            "lease_expires_at",
            "claim_token",
            "available_at",
            "published_at",
            "updated_at",
        ),
        "retrieval_units": ("active", "deactivated_at"),
    }
    role = (
        await connection.execute(
            text(
                "SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole, "
                "r.rolcreatedb, r.rolreplication "
                "FROM pg_roles r WHERE r.rolname = current_user"
            )
        )
    ).one()
    owned_objects = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() "
                "AND c.relname = ANY(:tables) "
                "AND pg_get_userbyid(c.relowner) = current_user"
            ),
            {"tables": list(worker_tables)},
        )
        or 0
    )
    inherited_privileged_roles = int(
        await connection.scalar(
            text(
                "SELECT count(*) FROM pg_roles inherited "
                "WHERE inherited.rolname <> current_user "
                "AND pg_has_role(current_user, inherited.oid, 'MEMBER') "
                "AND (inherited.rolsuper OR inherited.rolcreaterole "
                "OR inherited.rolcreatedb OR inherited.rolreplication "
                "OR EXISTS (SELECT 1 FROM pg_class c WHERE c.relowner = inherited.oid AND c.relname = ANY(:tables)))"
            ),
            {"tables": list(worker_tables)},
        )
        or 0
    )
    violations = []
    if not role.rolbypassrls:
        violations.append("role:BYPASSRLS:REQUIRED")
    if role.rolsuper or role.rolcreaterole or role.rolcreatedb or role.rolreplication:
        violations.append("role:ADMIN:FORBIDDEN")
    if owned_objects:
        violations.append("role:TABLE_OWNER:FORBIDDEN")
    if inherited_privileged_roles:
        violations.append("role:PRIVILEGED_INHERITANCE:FORBIDDEN")
    if not await connection.scalar(
        text("SELECT has_schema_privilege(current_user, 'public', 'USAGE')")
    ):
        violations.append("public:USAGE")
    if await connection.scalar(
        text("SELECT has_schema_privilege(current_user, 'public', 'CREATE')")
    ):
        violations.append("public:CREATE:FORBIDDEN")
    all_table_privileges = (
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
    )
    for table_name, required in worker_tables.items():
        for privilege in all_table_privileges:
            granted = await connection.scalar(
                text(
                    "SELECT has_table_privilege(current_user, :table_name, :privilege)"
                ),
                {
                    "table_name": f"public.{table_name}",
                    "privilege": privilege,
                },
            )
            if privilege in required and not granted:
                violations.append(f"{table_name}:{privilege}")
            elif privilege not in required and granted:
                violations.append(f"{table_name}:{privilege}:FORBIDDEN")
    for table_name, permitted_columns in update_columns.items():
        columns = tuple(
            await connection.scalars(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = :table_name"
                ),
                {"table_name": table_name},
            )
        )
        for column_name in columns:
            granted = await connection.scalar(
                text(
                    "SELECT has_column_privilege(current_user, :table_name, :column_name, 'UPDATE')"
                ),
                {
                    "table_name": f"public.{table_name}",
                    "column_name": column_name,
                },
            )
            if column_name in permitted_columns and not granted:
                violations.append(f"{table_name}.{column_name}:UPDATE")
            elif column_name not in permitted_columns and granted:
                violations.append(f"{table_name}.{column_name}:FORBIDDEN_UPDATE")
    if violations:
        raise RuntimeError("Worker database role violates the least-privilege contract")


async def validate_worker_database_role(engine: AsyncEngine | None = None) -> None:
    target = engine or get_worker_engine()
    async with target.connect() as connection:
        await _validate_worker_database_connection(connection)


async def validate_worker_database_connection(connection) -> None:
    await _validate_worker_database_connection(connection)

async def close_db_engine() -> None:

    global _engine, _session_factory, _migration_engine, _migration_session_factory, _worker_engine, _worker_session_factory, _engine_loop
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        _engine_loop = None
    if _migration_engine is not None:
        await _migration_engine.dispose()
        _migration_engine = None
        _migration_session_factory = None
    if _worker_engine is not None:
        await _worker_engine.dispose()
        _worker_engine = None
        _worker_session_factory = None
    logger.info("Database connection pools disposed.")
