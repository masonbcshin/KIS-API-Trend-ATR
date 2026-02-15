-- ═══════════════════════════════════════════════════════════════════════════════
-- KIS Trend-ATR Trading System - PostgreSQL 데이터베이스 스키마
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
--    psql -h localhost -U postgres -d kis_trading -f schema.sql
--
-- ═══════════════════════════════════════════════════════════════════════════════


-- ───────────────────────────────────────────────────────────────────────────────
-- 0. 데이터베이스 생성 (수동으로 실행)
-- ───────────────────────────────────────────────────────────────────────────────
-- CREATE DATABASE kis_trading;


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
    -- 기본 키: 종목 코드 + 모드 (DRY_RUN/PAPER/REAL 분리)
    symbol VARCHAR(20) NOT NULL,
    
    -- 진입 정보 ────────────────────────────────────────
    entry_price DECIMAL(15, 2) NOT NULL,        -- 매수 평균가
    quantity INTEGER NOT NULL,                   -- 보유 수량
    entry_time TIMESTAMP NOT NULL,               -- 최초 매수 시간
    
    -- ATR 정보 (★★★ 절대 변경 금지 ★★★) ─────────────
    -- 왜? 진입할 때의 변동성을 기준으로 손절/익절을 정했기 때문
    -- 익일에 ATR이 바뀌어도 기존 포지션의 손절선은 그대로 유지!
    atr_at_entry DECIMAL(15, 2) NOT NULL,       -- 진입 시점 ATR
    
    -- 손절/익절 가격 ──────────────────────────────────
    stop_price DECIMAL(15, 2) NOT NULL,         -- 손절가 (이 가격 이하면 손절)
    take_profit_price DECIMAL(15, 2),           -- 익절가 (이 가격 이상이면 익절, NULL 가능)
    trailing_stop DECIMAL(15, 2),               -- 트레일링 스탑 (최고가 기준으로 움직임)
    highest_price DECIMAL(15, 2),               -- 보유 중 최고가 (트레일링 계산용)
    mode VARCHAR(16) NOT NULL DEFAULT 'PAPER',  -- 실행 모드 네임스페이스
    
    -- 상태 ─────────────────────────────────────────────
    -- OPEN: 현재 보유 중
    -- CLOSED: 청산 완료 (히스토리용으로 남겨둠)
    status VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    
    -- 메타 정보 ─────────────────────────────────────────
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, mode)
);

-- 상태별 조회를 빠르게 하기 위한 인덱스
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_mode_status ON positions(mode, status);

-- 코멘트 추가
COMMENT ON TABLE positions IS '현재 보유 중인 포지션 정보. 서버 재시작 시 복구용.';
COMMENT ON COLUMN positions.atr_at_entry IS '★ 진입 시 ATR - 절대 재계산하여 변경하지 말 것!';


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
    id SERIAL PRIMARY KEY,
    
    -- 거래 정보 ────────────────────────────────────────
    symbol VARCHAR(20) NOT NULL,                -- 종목 코드
    side VARCHAR(10) NOT NULL,                  -- BUY(매수) / SELL(매도)
    price DECIMAL(15, 2) NOT NULL,              -- 체결 가격
    quantity INTEGER NOT NULL,                  -- 거래 수량
    executed_at TIMESTAMP NOT NULL,             -- 체결 시간
    
    -- 청산 사유 (매도 시에만 기록) ─────────────────────
    -- ATR_STOP      : ATR 기반 손절
    -- TAKE_PROFIT   : 익절 도달
    -- TRAILING_STOP : 트레일링 스탑
    -- TREND_BROKEN  : 추세 붕괴
    -- GAP_PROTECTION: 갭 하락 보호
    -- MANUAL        : 수동 청산
    -- SIGNAL_ONLY   : 신호만 기록 (실매매 없음)
    reason VARCHAR(50),
    
    -- 손익 정보 (매도 시에만 기록) ─────────────────────
    pnl DECIMAL(15, 2),                         -- 손익 금액 (원)
    pnl_percent DECIMAL(8, 4),                  -- 손익률 (%)
    entry_price DECIMAL(15, 2),                 -- 진입가 (손익 계산 확인용)
    
    -- 보유 기간 (매도 시에만 기록) ─────────────────────
    holding_days INTEGER,                       -- 보유 일수
    
    -- 메타 정보 ─────────────────────────────────────────
    order_no VARCHAR(50),                       -- 주문번호 (KIS API 응답값)
    mode VARCHAR(16) NOT NULL DEFAULT 'PAPER', -- 실행 모드 네임스페이스
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 조회 성능 향상을 위한 인덱스
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_executed_at ON trades(executed_at);
CREATE INDEX IF NOT EXISTS idx_trades_side ON trades(side);
CREATE INDEX IF NOT EXISTS idx_trades_reason ON trades(reason);
CREATE INDEX IF NOT EXISTS idx_trades_mode_executed_at ON trades(mode, executed_at);

-- 일별 집계용 인덱스
CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(DATE(executed_at));

COMMENT ON TABLE trades IS '모든 매수/매도 거래 기록. 성과 분석 및 히스토리 추적용.';
COMMENT ON COLUMN trades.reason IS '청산 사유 - ATR_STOP, TAKE_PROFIT, TRAILING_STOP, TREND_BROKEN, GAP_PROTECTION, MANUAL, SIGNAL_ONLY';


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
    -- 기본 키: 스냅샷 시간 + 모드 (DRY_RUN/PAPER/REAL 분리)
    snapshot_time TIMESTAMP NOT NULL,
    
    -- 자산 정보 ────────────────────────────────────────
    total_equity DECIMAL(15, 2) NOT NULL,       -- 총 평가금액
    cash DECIMAL(15, 2) NOT NULL,               -- 현금
    unrealized_pnl DECIMAL(15, 2) DEFAULT 0,    -- 미실현 손익
    realized_pnl DECIMAL(15, 2) DEFAULT 0,      -- 실현 손익 (누적)
    mode VARCHAR(16) NOT NULL DEFAULT 'PAPER',  -- 실행 모드 네임스페이스
    
    -- 포지션 정보 ───────────────────────────────────────
    position_count INTEGER DEFAULT 0,           -- 보유 포지션 수
    
    -- 메타 정보 ─────────────────────────────────────────
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snapshot_time, mode)
);

-- 날짜별 조회를 위한 인덱스
CREATE INDEX IF NOT EXISTS idx_snapshots_date ON account_snapshots(DATE(snapshot_time));
CREATE INDEX IF NOT EXISTS idx_snapshots_mode_time ON account_snapshots(mode, snapshot_time);

COMMENT ON TABLE account_snapshots IS '특정 시점의 계좌 상태 기록. 자산 변화 추적 및 성과 분석용.';


-- ───────────────────────────────────────────────────────────────────────────────
-- 4. daily_summary 테이블: 일별 요약
-- ───────────────────────────────────────────────────────────────────────────────
--
-- ★ 왜 필요한가?
--    - 매일 거래 요약을 빠르게 조회
--    - 텔레그램 일일 리포트 전송용
--
CREATE TABLE IF NOT EXISTS daily_summary (
    -- 기본 키: 날짜 + 모드 (DRY_RUN/PAPER/REAL 분리)
    trade_date DATE NOT NULL,
    mode VARCHAR(16) NOT NULL DEFAULT 'PAPER',
    
    -- 거래 요약 ────────────────────────────────────────
    total_trades INTEGER DEFAULT 0,             -- 총 거래 횟수
    buy_count INTEGER DEFAULT 0,                -- 매수 횟수
    sell_count INTEGER DEFAULT 0,               -- 매도 횟수
    
    -- 손익 요약 ────────────────────────────────────────
    realized_pnl DECIMAL(15, 2) DEFAULT 0,      -- 당일 실현 손익
    win_count INTEGER DEFAULT 0,                -- 수익 거래 횟수
    loss_count INTEGER DEFAULT 0,               -- 손실 거래 횟수
    
    -- 성과 지표 ────────────────────────────────────────
    win_rate DECIMAL(5, 2),                     -- 승률 (%)
    max_profit DECIMAL(15, 2),                  -- 최대 수익
    max_loss DECIMAL(15, 2),                    -- 최대 손실
    
    -- 시작/종료 자산 ────────────────────────────────────
    start_equity DECIMAL(15, 2),                -- 당일 시작 자산
    end_equity DECIMAL(15, 2),                  -- 당일 종료 자산
    
    -- 메타 정보 ─────────────────────────────────────────
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_date, mode)
);

COMMENT ON TABLE daily_summary IS '일별 거래 요약. 빠른 리포트 생성용.';
CREATE INDEX IF NOT EXISTS idx_daily_summary_mode_date ON daily_summary(mode, trade_date);


-- ───────────────────────────────────────────────────────────────────────────────
-- 5. 자동 업데이트 트리거
-- ───────────────────────────────────────────────────────────────────────────────
--
-- ★ 역할: 데이터가 수정될 때 updated_at을 자동으로 현재 시간으로 변경
--

-- 트리거 함수 생성
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE 'plpgsql';

-- positions 테이블에 트리거 적용
DROP TRIGGER IF EXISTS update_positions_updated_at ON positions;
CREATE TRIGGER update_positions_updated_at
    BEFORE UPDATE ON positions
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- daily_summary 테이블에 트리거 적용
DROP TRIGGER IF EXISTS update_daily_summary_updated_at ON daily_summary;
CREATE TRIGGER update_daily_summary_updated_at
    BEFORE UPDATE ON daily_summary
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- ═══════════════════════════════════════════════════════════════════════════════
-- 유용한 쿼리 모음 (참고용)
-- ═══════════════════════════════════════════════════════════════════════════════

-- 1. 열린 포지션 조회
-- SELECT * FROM positions WHERE status = 'OPEN';

-- 2. 오늘 거래 내역
-- SELECT * FROM trades WHERE DATE(executed_at) = CURRENT_DATE ORDER BY executed_at;

-- 3. 종목별 총 손익
-- SELECT symbol, SUM(pnl) as total_pnl, COUNT(*) as trade_count
-- FROM trades WHERE side = 'SELL'
-- GROUP BY symbol ORDER BY total_pnl DESC;

-- 4. 월별 손익
-- SELECT DATE_TRUNC('month', executed_at) as month, SUM(pnl) as monthly_pnl
-- FROM trades WHERE side = 'SELL'
-- GROUP BY month ORDER BY month DESC;

-- 5. 승률 계산
-- SELECT 
--     COUNT(*) as total_trades,
--     SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
--     SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
--     ROUND(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::DECIMAL / COUNT(*) * 100, 2) as win_rate
-- FROM trades WHERE side = 'SELL';

-- 6. 청산 사유별 통계
-- SELECT reason, COUNT(*) as count, SUM(pnl) as total_pnl, AVG(pnl) as avg_pnl
-- FROM trades WHERE side = 'SELL' AND reason IS NOT NULL
-- GROUP BY reason ORDER BY count DESC;


-- ═══════════════════════════════════════════════════════════════════════════════
-- 완료
-- ═══════════════════════════════════════════════════════════════════════════════
