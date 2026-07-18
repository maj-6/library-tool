-- 005_member_roles_approval — historical ledger bridge.
--
-- An account-approval experiment with this id was applied to the hosted
-- project before its client and RLS review was complete.  It is intentionally
-- NOT replayed on new projects: 007 restores the released authorization model
-- on the hosted project, while retaining the dormant role/status columns for
-- a future, fully reviewed membership migration.

insert into schema_migrations (id) values ('005_member_roles_approval') on conflict do nothing;
