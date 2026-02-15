-- MySQL runtime migration
-- 목적:
--   daily_summary를 DRY_RUN/PAPER/REAL 모드별로 분리 저장할 수 있도록
--   mode 컬럼과 (trade_date, mode) 복합 PK를 보장합니다.
--
-- 사용:
--   mysql -h <host> -u <user> -p <database> < migration_20260215_daily_summary_mode_split.sql

SET @db_name := DATABASE();

-- 0) daily_summary 테이블 존재 확인
SELECT COUNT(*) INTO @has_daily_summary
FROM information_schema.tables
WHERE table_schema = @db_name
  AND table_name = 'daily_summary';

-- 1) mode 컬럼 보정
SELECT COUNT(*) INTO @has_daily_summary_mode
FROM information_schema.columns
WHERE table_schema = @db_name
  AND table_name = 'daily_summary'
  AND column_name = 'mode';

SET @sql_daily_summary_mode := CASE
  WHEN @has_daily_summary = 0 THEN "SELECT 'SKIP daily_summary table not found'"
  WHEN @has_daily_summary_mode = 0 THEN "ALTER TABLE daily_summary ADD COLUMN mode VARCHAR(16) NOT NULL DEFAULT 'PAPER' AFTER trade_date"
  ELSE "SELECT 'SKIP daily_summary.mode already exists'"
END;
PREPARE stmt FROM @sql_daily_summary_mode;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2) PK를 (trade_date, mode)로 보정
SELECT GROUP_CONCAT(column_name ORDER BY ordinal_position) INTO @pk_cols
FROM information_schema.key_column_usage
WHERE table_schema = @db_name
  AND table_name = 'daily_summary'
  AND constraint_name = 'PRIMARY';

SET @sql_daily_summary_pk := CASE
  WHEN @has_daily_summary = 0 THEN "SELECT 'SKIP daily_summary table not found'"
  WHEN @pk_cols = 'trade_date,mode' THEN "SELECT 'SKIP daily_summary PK already (trade_date,mode)'"
  WHEN @pk_cols = 'trade_date' THEN "ALTER TABLE daily_summary DROP PRIMARY KEY, ADD PRIMARY KEY (trade_date, mode)"
  WHEN @pk_cols IS NULL THEN "ALTER TABLE daily_summary ADD PRIMARY KEY (trade_date, mode)"
  ELSE "SELECT 'SKIP daily_summary has unexpected PK definition - manual check required'"
END;
PREPARE stmt FROM @sql_daily_summary_pk;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 3) 조회 성능 인덱스 보정
SELECT COUNT(*) INTO @has_idx_daily_summary_mode_date
FROM information_schema.statistics
WHERE table_schema = @db_name
  AND table_name = 'daily_summary'
  AND index_name = 'idx_daily_summary_mode_date';

SET @sql_idx_daily_summary_mode_date := CASE
  WHEN @has_daily_summary = 0 THEN "SELECT 'SKIP daily_summary table not found'"
  WHEN @has_idx_daily_summary_mode_date = 0 THEN "CREATE INDEX idx_daily_summary_mode_date ON daily_summary(mode, trade_date)"
  ELSE "SELECT 'SKIP idx_daily_summary_mode_date already exists'"
END;
PREPARE stmt FROM @sql_idx_daily_summary_mode_date;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
