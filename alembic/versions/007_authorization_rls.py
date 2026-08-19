from typing import Sequence, Union

from alembic import op


revision: str = "007"
down_revision: Union[str, None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CONTENT_TABLES = ("knowledge_items", "document_files", "document_chunks")


def _create_content_policies(table: str, space_expression: str) -> None:
    read = f"gateway_has_space_role({space_expression}, ARRAY['owner','editor','reader'])"
    write = f"gateway_has_space_role({space_expression}, ARRAY['owner','editor'])"
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_member_select ON {table} FOR SELECT USING ({read})")
    op.execute(f"CREATE POLICY {table}_member_insert ON {table} FOR INSERT WITH CHECK ({write})")
    op.execute(
        f"CREATE POLICY {table}_member_update ON {table} "
        f"FOR UPDATE USING ({read}) WITH CHECK ({write})"
    )
    op.execute(f"CREATE POLICY {table}_member_delete ON {table} FOR DELETE USING ({write})")


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION gateway_actor_active() RETURNS boolean "
        "LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT EXISTS ("
        "SELECT 1 FROM public.members m "
        "WHERE m.id::text = current_setting('app.principal_id', true) "
        "AND m.status = 'active' "
        "AND current_setting('app.principal_restricted', true) IS DISTINCT FROM 'true'"
        ") $$"
    )
    op.execute(
        "CREATE FUNCTION gateway_has_space_role(target_space_id text, accepted_roles text[]) "
        "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT EXISTS ("
        "SELECT 1 FROM public.space_memberships sm "
        "JOIN public.members m ON m.id = sm.member_id "
        "JOIN public.workspaces w ON w.id = sm.space_id "
        "WHERE sm.space_id = target_space_id "
        "AND sm.member_id::text = current_setting('app.principal_id', true) "
        "AND sm.role = ANY(accepted_roles) "
        "AND m.status = 'active' "
        "AND public.gateway_actor_active() "
        "AND w.archived_at IS NULL"
        ") $$"
    )
    op.execute(
        "CREATE FUNCTION gateway_is_initial_space_owner(target_space_id text, target_member_id uuid) "
        "RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ "
        "SELECT EXISTS ("
        "SELECT 1 FROM public.workspaces w "
        "WHERE w.id = target_space_id "
        "AND w.archived_at IS NULL "
        "AND w.created_by_member_id = target_member_id "
        "AND target_member_id::text = current_setting('app.principal_id', true) "
        "AND public.gateway_actor_active() "
        "AND NOT EXISTS (SELECT 1 FROM public.space_memberships sm WHERE sm.space_id = w.id)"
        ") $$"
    )
    op.execute(
        "CREATE FUNCTION gateway_can_change_membership("
        "target_space_id text, target_member_id uuid, target_role text"
        ") RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER "
        "SET search_path = pg_catalog, public "
        "AS $$ BEGIN "
        "PERFORM 1 FROM public.workspaces w WHERE w.id = target_space_id FOR UPDATE; "
        "RETURN public.gateway_has_space_role(target_space_id, ARRAY['owner']) "
        "AND (target_role <> 'owner' OR ("
        "SELECT count(*) FROM public.space_memberships sm "
        "WHERE sm.space_id = target_space_id AND sm.role = 'owner'"
        ") > 1); END $$"
    )
    op.execute("REVOKE ALL ON FUNCTION gateway_actor_active() FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION gateway_has_space_role(text, text[]) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION gateway_is_initial_space_owner(text, uuid) FROM PUBLIC"
    )
    op.execute(
        "REVOKE ALL ON FUNCTION gateway_can_change_membership(text, uuid, text) FROM PUBLIC"
    )

    for table in _CONTENT_TABLES:
        _create_content_policies(table, f"{table}.workspace_id")
    revision_space = (
        "(SELECT item.workspace_id FROM knowledge_items item "
        "WHERE item.id = knowledge_revisions.item_id)"
    )
    _create_content_policies("knowledge_revisions", revision_space)

    op.execute("ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY workspaces_member_select ON workspaces FOR SELECT USING ("
        "gateway_has_space_role(id, ARRAY['owner','editor','reader']) "
        "OR gateway_is_initial_space_owner(id, created_by_member_id)"
        ")"
    )
    op.execute(
        "CREATE POLICY workspaces_member_insert ON workspaces FOR INSERT WITH CHECK ("
        "created_by_member_id::text = current_setting('app.principal_id', true) "
        "AND gateway_actor_active()"
        ")"
    )
    op.execute(
        "CREATE POLICY workspaces_owner_update ON workspaces FOR UPDATE USING ("
        "gateway_has_space_role(id, ARRAY['owner'])"
        ") WITH CHECK (gateway_actor_active())"
    )
    op.execute("ALTER TABLE space_memberships ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY space_memberships_member_select ON space_memberships FOR SELECT USING ("
        "gateway_has_space_role(space_id, ARRAY['owner','editor','reader'])"
        ")"
    )
    op.execute(
        "CREATE POLICY space_memberships_owner_insert ON space_memberships FOR INSERT WITH CHECK ("
        "gateway_has_space_role(space_id, ARRAY['owner']) "
        "OR (role = 'owner' AND gateway_is_initial_space_owner(space_id, member_id))"
        ")"
    )
    op.execute(
        "CREATE POLICY space_memberships_owner_update ON space_memberships FOR UPDATE USING ("
        "gateway_can_change_membership(space_id, member_id, role)"
        ") WITH CHECK (gateway_actor_active())"
    )
    op.execute(
        "CREATE POLICY space_memberships_owner_delete ON space_memberships FOR DELETE USING ("
        "gateway_can_change_membership(space_id, member_id, role)"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS ("
        "SELECT 1 FROM pg_policies WHERE schemaname = current_schema() "
        "AND tablename = 'members' AND policyname = 'members_family_select'"
        ") THEN "
        "DROP POLICY members_family_select ON public.members; "
        "ALTER TABLE public.members NO FORCE ROW LEVEL SECURITY; "
        "ALTER TABLE public.members DISABLE ROW LEVEL SECURITY; "
        "END IF; END $$"
    )
    op.execute("DROP POLICY IF EXISTS space_memberships_owner_delete ON space_memberships")
    op.execute("DROP POLICY IF EXISTS space_memberships_owner_touch ON space_memberships")
    op.execute("DROP POLICY IF EXISTS space_memberships_owner_update ON space_memberships")
    op.execute("DROP POLICY IF EXISTS space_memberships_owner_insert ON space_memberships")
    op.execute("DROP POLICY IF EXISTS space_memberships_member_select ON space_memberships")
    op.execute("ALTER TABLE space_memberships DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS workspaces_owner_update ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspaces_member_insert ON workspaces")
    op.execute("DROP POLICY IF EXISTS workspaces_member_select ON workspaces")
    op.execute("ALTER TABLE workspaces DISABLE ROW LEVEL SECURITY")
    for table in (*_CONTENT_TABLES, "knowledge_revisions"):
        op.execute(f"DROP POLICY IF EXISTS {table}_member_delete ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_member_update ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_member_insert ON {table}")
        op.execute(f"DROP POLICY IF EXISTS {table}_member_select ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS gateway_can_change_membership(text, uuid, text)")
    op.execute("DROP FUNCTION IF EXISTS gateway_is_initial_space_owner(text, uuid)")
    op.execute("DROP FUNCTION IF EXISTS gateway_has_space_role(text, text[])")
    op.execute("DROP FUNCTION IF EXISTS gateway_actor_active()")
