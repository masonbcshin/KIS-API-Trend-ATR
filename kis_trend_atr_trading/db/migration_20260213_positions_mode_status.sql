-- MySQL runtime hotfix migration
-- 목적:
--   구버전 DB에서 positions.status / positions.mode 컬럼이 없어
--   조회 실패(Unknown column)가 발생하는 문제를 즉시 보정합니다.
--
-- 사용:
--   mysql -h <host> -u <user> -p <database> < migration_20260213_positions_mode_status.sql

SET @db_name := DATABASE();

-- 1) positions.mode 컬럼 보정
SELECT COUNT(*) INTO @has_positions_mode
FROM information_schema.columns
WHERE table_schema = @db_name
  AND table_name = 'positions'
  AND column_name = 'mode';

SET @sql_positions_mode := IF(
  @has_positions_mode = 0,
  "ALTER TABLE positions ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'PAPER'",
  "SELECT 'SKIP positions.mode already exists'"
);
PREPARE stmt FROM @sql_positions_mode;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2) positions.status 컬럼 보정
SELECT COUNT(*) INTO @has_positions_status
FROM information_schema.columns
WHERE table_schema = @db_name
  AND table_name = 'positions'
  AND column_name = 'status';

SET @sql_positions_status := IF(
  @has_positions_status = 0,
  "ALTER TABLE positions ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'OPEN'",
  "SELECT 'SKIP positions.status already exists'"
);
PREPARE stmt FROM @sql_positions_status;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 3) 인덱스 보정
SELECT COUNT(*) INTO @has_idx_positions_status
FROM information_schema.statistics
WHERE table_schema = @db_name
  AND table_name = 'positions'
  AND index_name = 'idx_positions_status';

SET @sql_idx_positions_status := IF(
  @has_idx_positions_status = 0,
  "CREATE INDEX idx_positions_status ON positions(status)",
  "SELECT 'SKIP idx_positions_status already exists'"
);
PREPARE stmt FROM @sql_idx_positions_status;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT COUNT(*) INTO @has_idx_positions_mode_status
FROM information_schema.statistics
WHERE table_schema = @db_name
  AND table_name = 'positions'
  AND index_name = 'idx_positions_mode_status';

SET @sql_idx_positions_mode_status := IF(
  @has_idx_positions_mode_status = 0,
  "CREATE INDEX idx_positions_mode_status ON positions(mode, status)",
  "SELECT 'SKIP idx_positions_mode_status already exists'"
);
PREPARE stmt FROM @sql_idx_positions_mode_status;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

