-- #3787 rollback for 00_roles.sql. Run AFTER both per-database rollbacks
-- (a role that still owns objects cannot be dropped).
DROP ROLE IF EXISTS tower_remote_ro;
DROP ROLE IF EXISTS tower_remote_read_owner;
