-- 목적:
--   positions / account_snapshots 테이블을
--   DRY_RUN/PAPER/REAL 모드별로 분리 저장할 수 있도록
--   PRIMARY KEY를 mode 포함 복합키로 전환한다.
--
-- 실행 예시:
--   mysql -h <host> -u <user> -p <database> < migration_20260215_positions_snapshots_mode_split.sql

-- =========================
-- 1) positions.mode 컬럼 보장
-- =========================
SELECT COUNT(*) INTO @has_positions
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name = 'positions';

SELECT COUNT(*) INTO @has_positions_mode
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'positions'
  AND column_name = 'mode';

SET @sql_positions_mode := CASE
  WHEN @has_positions = 0 THEN "SELECT 'SKIP positions table not found'"
  WHEN @has_positions_mode = 0 AND EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = DATABASE()
      AND table_name = 'positions'
      AND column_name = 'highest_price'
  ) THEN "ALTER TABLE positions ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'PAPER' AFTER highest_price"
  WHEN @has_positions_mode = 0 THEN "ALTER TABLE positions ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'PAPER'"
  ELSE "SELECT 'SKIP positions.mode already exists'"
END;
PREPARE stmt FROM @sql_positions_mode;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- =========================
-- 2) positions PK -> (position_id, mode) 또는 (symbol, mode)
-- =========================
SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index) INTO @positions_pk_cols
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name = 'positions'
  AND index_name = 'PRIMARY';

SELECT COUNT(*) INTO @has_positions_position_id
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'positions'
  AND column_name = 'position_id';

SELECT COUNT(*) INTO @has_positions_symbol
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'positions'
  AND column_name = 'symbol';

SELECT COUNT(*) INTO @has_positions_stock_code
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'positions'
  AND column_name = 'stock_code';

SET @sql_positions_pk := CASE
  WHEN @has_positions = 0 THEN "SELECT 'SKIP positions table not found'"
  WHEN @has_positions_position_id > 0 AND @positions_pk_cols = 'position_id,mode' THEN "SELECT 'SKIP positions PK already (position_id,mode)'"
  WHEN @has_positions_position_id > 0 AND @positions_pk_cols = 'position_id' THEN "ALTER TABLE positions DROP PRIMARY KEY, ADD PRIMARY KEY (position_id, mode)"
  WHEN @has_positions_position_id > 0 AND @positions_pk_cols IS NULL THEN "ALTER TABLE positions ADD PRIMARY KEY (position_id, mode)"
  WHEN @has_positions_symbol > 0 AND @positions_pk_cols = 'symbol,mode' THEN "SELECT 'SKIP positions PK already (symbol,mode)'"
  WHEN @has_positions_symbol > 0 AND @positions_pk_cols = 'symbol' THEN "ALTER TABLE positions DROP PRIMARY KEY, ADD PRIMARY KEY (symbol, mode)"
  WHEN @has_positions_symbol > 0 AND @positions_pk_cols IS NULL THEN "ALTER TABLE positions ADD PRIMARY KEY (symbol, mode)"
  WHEN @has_positions_stock_code > 0 AND @positions_pk_cols = 'stock_code,mode' THEN "SELECT 'SKIP positions PK already (stock_code,mode)'"
  WHEN @has_positions_stock_code > 0 AND @positions_pk_cols = 'stock_code' THEN "ALTER TABLE positions DROP PRIMARY KEY, ADD PRIMARY KEY (stock_code, mode)"
  WHEN @has_positions_stock_code > 0 AND @positions_pk_cols IS NULL THEN "ALTER TABLE positions ADD PRIMARY KEY (stock_code, mode)"
  WHEN @has_positions_position_id = 0 AND @has_positions_symbol = 0 AND @has_positions_stock_code = 0 THEN "SELECT 'SKIP positions target PK columns not found'"
  ELSE "SELECT 'SKIP positions has unexpected PK definition - manual check required'"
END;
PREPARE stmt FROM @sql_positions_pk;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT COUNT(*) INTO @has_idx_positions_mode_status
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name = 'positions'
  AND index_name = 'idx_positions_mode_status';

SET @sql_idx_positions_mode_status := CASE
  WHEN @has_positions = 0 THEN "SELECT 'SKIP positions table not found'"
  WHEN @has_idx_positions_mode_status = 0 THEN "CREATE INDEX idx_positions_mode_status ON positions(mode, status)"
  ELSE "SELECT 'SKIP idx_positions_mode_status already exists'"
END;
PREPARE stmt FROM @sql_idx_positions_mode_status;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ==================================
-- 3) account_snapshots.mode 컬럼 보장
-- ==================================
SELECT COUNT(*) INTO @has_snapshots
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND table_name = 'account_snapshots';

SELECT COUNT(*) INTO @has_snapshots_mode
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'account_snapshots'
  AND column_name = 'mode';

SET @sql_snapshots_mode := CASE
  WHEN @has_snapshots = 0 THEN "SELECT 'SKIP account_snapshots table not found'"
  WHEN @has_snapshots_mode = 0 THEN "ALTER TABLE account_snapshots ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'PAPER' AFTER realized_pnl"
  ELSE "SELECT 'SKIP account_snapshots.mode already exists'"
END;
PREPARE stmt FROM @sql_snapshots_mode;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ==================================
-- 4) account_snapshots PK -> (snapshot_time, mode)
-- ==================================
SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index) INTO @snapshots_pk_cols
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name = 'account_snapshots'
  AND index_name = 'PRIMARY';

SET @sql_snapshots_pk := CASE
  WHEN @has_snapshots = 0 THEN "SELECT 'SKIP account_snapshots table not found'"
  WHEN @snapshots_pk_cols = 'snapshot_time,mode' THEN "SELECT 'SKIP account_snapshots PK already (snapshot_time,mode)'"
  WHEN @snapshots_pk_cols = 'snapshot_time' THEN "ALTER TABLE account_snapshots DROP PRIMARY KEY, ADD PRIMARY KEY (snapshot_time, mode)"
  WHEN @snapshots_pk_cols IS NULL THEN "ALTER TABLE account_snapshots ADD PRIMARY KEY (snapshot_time, mode)"
  ELSE "SELECT 'SKIP account_snapshots has unexpected PK definition - manual check required'"
END;
PREPARE stmt FROM @sql_snapshots_pk;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT COUNT(*) INTO @has_idx_snapshots_mode_time
FROM information_schema.statistics
WHERE table_schema = DATABASE()
  AND table_name = 'account_snapshots'
  AND index_name = 'idx_snapshots_mode_time';

SET @sql_idx_snapshots_mode_time := CASE
  WHEN @has_snapshots = 0 THEN "SELECT 'SKIP account_snapshots table not found'"
  WHEN @has_idx_snapshots_mode_time = 0 THEN "CREATE INDEX idx_snapshots_mode_time ON account_snapshots(mode, snapshot_time)"
  ELSE "SELECT 'SKIP idx_snapshots_mode_time already exists'"
END;
PREPARE stmt FROM @sql_idx_snapshots_mode_time;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
