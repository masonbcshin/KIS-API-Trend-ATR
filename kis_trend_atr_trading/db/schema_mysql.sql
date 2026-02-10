-- ═══════════════════════════════════════════════════════════════════════════════
-- KIS Trend-ATR Trading System - MySQL(InnoDB) 데이터베이스 스키마
-- ═══════════════════════════════════════════════════════════════════════════════
--
-- ★ 이 파일의 역할:
--    - 자동매매 시스템에 필요한 테이블들을 정의합니다.
--    - 처음 시스템을 세팅할 때 이 SQL을 실행하면 됩니다.
--
-- ★ 테이블 설명 (중학생도 이해할 수 있게):
--    1. positions: "지금 내가 어떤 주식을 얼마에 몇 주 가지고 있나" 저장
--    2. trades: "언제 무슨 주식을 사고 팔았나" 기록
--    3. account_snapshots: "그때그때 내 계좌에 돈이 얼마 있었나" 스냅샷
--
-- ★ 실행 방법:
--    mysql -h localhost -u root -p kis_trading < schema_mysql.sql
--
-- ★ Oracle Cloud Infrastructure Free Tier MySQL 호환
--    - InnoDB 스토리지 엔진 사용
--    - 표준 SQL 문법 기반
--
-- ═══════════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────────
-- 0. 데이터베이스 생성 (수동으로 실행)
-- ───────────────────────────────────────────────────────────────────────────────
-- CREATE DATABASE IF NOT EXISTS kis_trading CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
-- USE kis_trading;


-- ───────────────────────────────────────────────────────────────────────────────
-- 1. positions 테이블: 현재 보유 포지션
-- ───────────────────────────────────────────────────────────────────────────────
-- 
-- ★ 왜 필요한가?
--    - 서버가 갑자기 꺼져도 "내가 뭘 들고 있었지?" 알 수 있어야 함
--    - 다시 켰을 때 이 테이블을 보고 포지션을 복구함
--
-- ★ 필드 설명:
--    - symbol: 종목 코드 (예: 005930 = 삼성전자)
--    - entry_price: 내가 산 가격
--    - quantity: 몇 주 샀는지
--    - atr_at_entry: 샀을 때의 ATR 값 (절대 바꾸면 안 됨!)
--    - stop_price: 손절 가격 (이 가격 아래로 내려가면 팔아야 함)
--    - status: OPEN(들고있음) / CLOSED(다 팔았음)
--
CREATE TABLE IF NOT EXISTS positions (
    position_id VARCHAR(64) NOT NULL PRIMARY KEY COMMENT '포지션 고유 ID',
    stock_code VARCHAR(20) NOT NULL COMMENT '종목 코드',
    stock_name VARCHAR(100) NULL COMMENT '종목명',
    
    -- 포지션 상태
    state VARCHAR(20) NOT NULL COMMENT 'PENDING, ENTERED, EXITED',
    side VARCHAR(10) NOT NULL DEFAULT 'LONG' COMMENT '포지션 방향 (현재는 LONG만 지원)',
    
    -- 진입 정보
    entry_price DECIMAL(15, 2) NOT NULL COMMENT '매수 평균가',
    quantity INT NOT NULL COMMENT '보유 수량',
    entry_date DATE NOT NULL COMMENT '최초 매수 날짜',
    entry_time DATETIME NOT NULL COMMENT '최초 매수 시간',
    entry_order_no VARCHAR(50) NULL COMMENT '진입 주문 번호',
    
    -- 리스크 관리 정보 (진입 시 고정)
    atr_at_entry DECIMAL(15, 2) NOT NULL COMMENT '진입 시점 ATR',
    stop_loss DECIMAL(15, 2) NOT NULL COMMENT '기본 손절가',
    take_profit DECIMAL(15, 2) NOT NULL COMMENT '기본 익절가',
    
    -- 동적 업데이트 정보
    trailing_stop DECIMAL(15, 2) NULL COMMENT '트레일링 스탑 가격',
    highest_price DECIMAL(15, 2) NULL COMMENT '보유 중 최고가 (트레일링 계산용)',
    current_price DECIMAL(15, 2) NULL COMMENT '현재가 (업데이트용)',
    unrealized_pnl DECIMAL(15, 2) NULL COMMENT '미실현 손익',
    unrealized_pnl_pct DECIMAL(10, 4) NULL COMMENT '미실현 손익률',
    
    -- 청산 정보
    exit_price DECIMAL(15, 2) NULL COMMENT '매도 평균가',
    exit_date DATE NULL COMMENT '청산 날짜',
    exit_time DATETIME NULL COMMENT '청산 시간',
    exit_reason VARCHAR(50) NULL COMMENT '청산 사유',
    exit_order_no VARCHAR(50) NULL COMMENT '청산 주문 번호',
    realized_pnl DECIMAL(15, 2) NULL COMMENT '실현 손익',
    realized_pnl_pct DECIMAL(10, 4) NULL COMMENT '실현 손익률',
    commission DECIMAL(15, 2) NULL COMMENT '수수료',
    holding_days INT NULL COMMENT '보유 기간(일)',
    
    -- 메타 정보
    created_at DATETIME NOT NULL COMMENT '생성 시간',
    updated_at DATETIME NOT NULL COMMENT '수정 시간'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='모든 포지션의 전체 생명주기를 기록하는 테이블';

-- 상태와 종목코드는 자주 조회되므로 인덱스 추가
CREATE INDEX idx_positions_state ON positions(state);
CREATE INDEX idx_positions_stock_code ON positions(stock_code);


-- ───────────────────────────────────────────────────────────────────────────────
-- 2. trades 테이블: 모든 매매 기록
-- ───────────────────────────────────────────────────────────────────────────────
--
-- ★ 왜 필요한가?
--    - 나중에 "이 전략 승률이 얼마지?" 분석할 때 필요
--    - "왜 그때 팔았지?" 추적할 때 필요 (reason 컬럼)
--    - 세금 신고할 때 필요
--
-- ★ 필드 설명:
--    - side: BUY(샀다) / SELL(팔았다)
--    - reason: 왜 팔았는지 (손절? 익절? 트레일링?)
--    - pnl: 손익 금액 (매도 시에만 기록)
--    - pnl_percent: 손익률 (매도 시에만 기록)
--
CREATE TABLE IF NOT EXISTS trades (
    -- 기본 키: 자동 증가 ID
    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '거래 고유 ID',
    
    -- 거래 정보 ────────────────────────────────────────
    symbol VARCHAR(20) NOT NULL COMMENT '종목 코드',
    side VARCHAR(10) NOT NULL COMMENT 'BUY(매수) / SELL(매도)',
    price DECIMAL(15, 2) NOT NULL COMMENT '체결 가격',
    quantity INT NOT NULL COMMENT '거래 수량',
    executed_at DATETIME NOT NULL COMMENT '체결 시간',
    
    -- 청산 사유 (매도 시에만 기록) ─────────────────────
    -- ATR_STOP      : ATR 기반 손절
    -- TAKE_PROFIT   : 익절 도달
    -- TRAILING_STOP : 트레일링 스탑
    -- TREND_BROKEN  : 추세 붕괴
    -- GAP_PROTECTION: 갭 하락 보호
    -- MANUAL        : 수동 청산
    -- SIGNAL_ONLY   : 신호만 기록 (실매매 없음)
    reason VARCHAR(50) NULL COMMENT '청산 사유',
    
    -- 손익 정보 (매도 시에만 기록) ─────────────────────
    pnl DECIMAL(15, 2) NULL COMMENT '손익 금액 (원)',
    pnl_percent DECIMAL(8, 4) NULL COMMENT '손익률 (%)',
    entry_price DECIMAL(15, 2) NULL COMMENT '진입가 (손익 계산 확인용)',
    
    -- 보유 기간 (매도 시에만 기록) ─────────────────────
    holding_days INT NULL COMMENT '보유 일수',
    
    -- 메타 정보 ─────────────────────────────────────────
    order_no VARCHAR(50) NULL COMMENT '주문번호 (KIS API 응답값)',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '생성 시간'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='모든 매수/매도 거래 기록. 성과 분석 및 히스토리 추적용.';

-- 조회 성능 향상을 위한 인덱스
CREATE INDEX idx_trades_symbol ON trades(symbol);
CREATE INDEX idx_trades_executed_at ON trades(executed_at);
CREATE INDEX idx_trades_side ON trades(side);
CREATE INDEX idx_trades_reason ON trades(reason);

-- 일별 집계용 인덱스
CREATE INDEX idx_trades_date ON trades((DATE(executed_at)));


-- ───────────────────────────────────────────────────────────────────────────────
-- 3. account_snapshots 테이블: 계좌 스냅샷
-- ───────────────────────────────────────────────────────────────────────────────
--
-- ★ 왜 필요한가?
--    - "어제 내 계좌에 얼마 있었더라?" 추적
--    - 일별/월별 자산 변화 그래프 그리기
--    - 최대 낙폭(MDD) 계산
--
-- ★ 필드 설명:
--    - total_equity: 총 평가금액 (현금 + 주식 평가금)
--    - cash: 현금 (예수금)
--    - unrealized_pnl: 미실현 손익 (아직 안 판 주식의 손익)
--    - realized_pnl: 실현 손익 (판 주식의 손익 합계)
--
CREATE TABLE IF NOT EXISTS account_snapshots (
    -- 기본 키: 스냅샷 시간 (1분에 하나씩만)
    snapshot_time DATETIME NOT NULL PRIMARY KEY COMMENT '스냅샷 시간',
    
    -- 자산 정보 ────────────────────────────────────────
    total_equity DECIMAL(15, 2) NOT NULL COMMENT '총 평가금액',
    cash DECIMAL(15, 2) NOT NULL COMMENT '현금',
    unrealized_pnl DECIMAL(15, 2) DEFAULT 0 COMMENT '미실현 손익',
    realized_pnl DECIMAL(15, 2) DEFAULT 0 COMMENT '실현 손익 (누적)',
    
    -- 포지션 정보 ───────────────────────────────────────
    position_count INT DEFAULT 0 COMMENT '보유 포지션 수',
    
    -- 메타 정보 ─────────────────────────────────────────
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '생성 시간'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='특정 시점의 계좌 상태 기록. 자산 변화 추적 및 성과 분석용.';

-- 날짜별 조회를 위한 인덱스
CREATE INDEX idx_snapshots_date ON account_snapshots((DATE(snapshot_time)));


-- ───────────────────────────────────────────────────────────────────────────────
-- 4. daily_summary 테이블: 일별 요약
-- ───────────────────────────────────────────────────────────────────────────────
--
-- ★ 왜 필요한가?
--    - 매일 거래 요약을 빠르게 조회
--    - 텔레그램 일일 리포트 전송용
--
CREATE TABLE IF NOT EXISTS daily_summary (
    -- 기본 키: 날짜
    trade_date DATE NOT NULL PRIMARY KEY COMMENT '거래 날짜',
    
    -- 거래 요약 ────────────────────────────────────────
    total_trades INT DEFAULT 0 COMMENT '총 거래 횟수',
    buy_count INT DEFAULT 0 COMMENT '매수 횟수',
    sell_count INT DEFAULT 0 COMMENT '매도 횟수',
    
    -- 손익 요약 ────────────────────────────────────────
    realized_pnl DECIMAL(15, 2) DEFAULT 0 COMMENT '당일 실현 손익',
    win_count INT DEFAULT 0 COMMENT '수익 거래 횟수',
    loss_count INT DEFAULT 0 COMMENT '손실 거래 횟수',
    
    -- 성과 지표 ────────────────────────────────────────
    win_rate DECIMAL(5, 2) NULL COMMENT '승률 (%)',
    max_profit DECIMAL(15, 2) NULL COMMENT '최대 수익',
    max_loss DECIMAL(15, 2) NULL COMMENT '최대 손실',
    
    -- 시작/종료 자산 ────────────────────────────────────
    start_equity DECIMAL(15, 2) NULL COMMENT '당일 시작 자산',
    end_equity DECIMAL(15, 2) NULL COMMENT '당일 종료 자산',
    
    -- 메타 정보 ─────────────────────────────────────────
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '생성 시간',
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '수정 시간'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='일별 거래 요약. 빠른 리포트 생성용.';


-- ═══════════════════════════════════════════════════════════════════════════════
-- 유용한 쿼리 모음 (참고용)
-- ═══════════════════════════════════════════════════════════════════════════════

-- 1. 열린 포지션 조회
-- SELECT * FROM positions WHERE status = 'OPEN';

-- 2. 오늘 거래 내역
-- SELECT * FROM trades WHERE DATE(executed_at) = CURDATE() ORDER BY executed_at;

-- 3. 종목별 총 손익
-- SELECT symbol, SUM(pnl) as total_pnl, COUNT(*) as trade_count
-- FROM trades WHERE side = 'SELL'
-- GROUP BY symbol ORDER BY total_pnl DESC;

-- 4. 월별 손익
-- SELECT DATE_FORMAT(executed_at, '%Y-%m') as month, SUM(pnl) as monthly_pnl
-- FROM trades WHERE side = 'SELL'
-- GROUP BY month ORDER BY month DESC;

-- 5. 승률 계산
-- SELECT 
--     COUNT(*) as total_trades,
--     SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
--     SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
--     ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) / COUNT(*) * 100, 2) as win_rate
-- FROM trades WHERE side = 'SELL';

-- 6. 청산 사유별 통계
-- SELECT reason, COUNT(*) as count, SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl
-- FROM trades WHERE side = 'SELL' AND reason IS NOT NULL
-- GROUP BY reason ORDER BY count DESC;


-- ═══════════════════════════════════════════════════════════════════════════════
-- 완료
-- ═══════════════════════════════════════════════════════════════════════════════
