# PostgreSQL → MySQL 마이그레이션 가이드

## 변경 요약

이 문서는 KIS Trend-ATR Trading System의 데이터베이스를 PostgreSQL에서 MySQL(InnoDB)로 전환한 변경 사항을 설명합니다.

**마이그레이션 이유:**
- Oracle Cloud Infrastructure Free Tier MySQL 호환성 확보
- 개인용 자동매매 시스템에 적합한 무료 클라우드 DB 활용
- 실계좌 자동매매의 안정성과 트랜잭션 무결성 유지

---

## 1. 주요 변경 사항

### 1.1 PostgreSQL 전용 문법 제거

| PostgreSQL | MySQL | 설명 |
|------------|-------|------|
| `SERIAL` | `INT AUTO_INCREMENT` | 자동 증가 ID |
| `BOOLEAN` | `TINYINT(1)` | 불리언 타입 |
| `TIMESTAMP` | `DATETIME` | 날짜/시간 타입 |
| `RETURNING *` | `LAST_INSERT_ID()` + SELECT | INSERT 후 결과 반환 |
| `ON CONFLICT ... DO UPDATE` | `INSERT ... ON DUPLICATE KEY UPDATE` | UPSERT 처리 |
| `DATE_TRUNC()` | `DATE_FORMAT()` | 날짜 절삭 |
| `INTERVAL '30 days'` | `INTERVAL 30 DAY` | 날짜 간격 |
| `CREATE OR REPLACE FUNCTION` | (트리거로 대체) | 함수 생성 |
| `COMMENT ON` | `COMMENT` (컬럼 정의 내) | 코멘트 |

### 1.2 파일별 변경 내역

| 파일 | 변경 유형 | 설명 |
|------|----------|------|
| `db/schema_mysql.sql` | **신규** | MySQL용 DDL 스키마 |
| `db/mysql.py` | **신규** | MySQL 연결 관리자 |
| `db/postgres.py` | 유지 | 레거시 호환 (사용 안 함) |
| `db/repository.py` | **수정** | PostgreSQL 문법 제거 |
| `db/__init__.py` | **수정** | MySQL import로 변경 |
| `trading/trader.py` | **수정** | MySQLManager 사용 |
| `report/performance.py` | **수정** | MySQL 호환 쿼리 |
| `requirements.txt` | **수정** | mysql-connector-python 추가 |
| `.env.example` | **수정** | MySQL 설정 가이드 |
| `tests/test_db.py` | **수정** | MySQL 테스트 |

---

## 2. 설치 및 설정

### 2.1 의존성 설치

```bash
pip install -r requirements.txt
```

또는 개별 설치:

```bash
pip install mysql-connector-python>=8.2.0
```

### 2.2 환경변수 설정

`.env` 파일에 다음 설정 추가:

```bash
# MySQL 데이터베이스 설정
DB_ENABLED=true
DB_TYPE=mysql
DB_HOST=localhost
DB_PORT=3306
DB_NAME=kis_trading
DB_USER=root
DB_PASSWORD=your_password
```

### 2.3 데이터베이스 생성

```sql
-- MySQL 접속
mysql -u root -p

-- 데이터베이스 생성
CREATE DATABASE kis_trading 
CHARACTER SET utf8mb4 
COLLATE utf8mb4_unicode_ci;

-- 스키마 적용
USE kis_trading;
SOURCE db/schema_mysql.sql;
```

### 2.4 Oracle Cloud Infrastructure (OCI) Free Tier 설정

1. OCI 콘솔에서 MySQL Database System 생성
2. 네트워크 설정에서 3306 포트 허용
3. 퍼블릭 IP 또는 VPN 설정
4. `.env`에 호스트 IP 입력

---

## 3. 코드 변경 상세

### 3.1 RETURNING 절 대체

**기존 (PostgreSQL):**
```python
result = db.execute_command(
    """
    INSERT INTO trades (symbol, side, price)
    VALUES (%s, %s, %s)
    RETURNING *
    """,
    (symbol, side, price),
    returning=True
)
```

**변경 (MySQL):**
```python
# INSERT 실행 후 LAST_INSERT_ID 반환
trade_id = db.execute_insert(
    """
    INSERT INTO trades (symbol, side, price)
    VALUES (%s, %s, %s)
    """,
    (symbol, side, price)
)

# 필요 시 별도 조회
if trade_id:
    result = db.execute_query(
        "SELECT * FROM trades WHERE id = %s",
        (trade_id,),
        fetch_one=True
    )
```

### 3.2 ON CONFLICT 대체

**기존 (PostgreSQL):**
```python
db.execute_command(
    """
    INSERT INTO positions (symbol, entry_price, ...)
    VALUES (%s, %s, ...)
    ON CONFLICT (symbol) DO UPDATE SET
        entry_price = EXCLUDED.entry_price,
        ...
    """
)
```

**변경 (MySQL):**
```python
db.execute_command(
    """
    INSERT INTO positions (symbol, entry_price, ...)
    VALUES (%s, %s, ...)
    ON DUPLICATE KEY UPDATE
        entry_price = VALUES(entry_price),
        ...
    """
)
```

### 3.3 날짜 함수 변경

**기존 (PostgreSQL):**
```sql
SELECT DATE_TRUNC('month', executed_at) as month
FROM trades
WHERE executed_at >= CURRENT_DATE - INTERVAL '30 days'
```

**변경 (MySQL):**
```sql
SELECT DATE_FORMAT(executed_at, '%Y-%m-01') as month
FROM trades
WHERE executed_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
```

---

## 4. 트랜잭션 안정성

### 4.1 InnoDB 엔진 사용

모든 테이블이 InnoDB 스토리지 엔진을 사용하여:
- ACID 트랜잭션 보장
- 외래키 제약 지원
- 행 수준 잠금 (동시성 향상)

### 4.2 명시적 커밋/롤백

```python
with db.transaction() as cursor:
    # 매도 기록
    cursor.execute("INSERT INTO trades ...")
    # 포지션 종료
    cursor.execute("UPDATE positions SET status = 'CLOSED' ...")
# with 블록 종료 시 자동 커밋
# 예외 발생 시 자동 롤백
```

### 4.3 autocommit 비활성화

```python
# DatabaseConfig 기본 설정
autocommit = False  # 명시적 커밋 필수
```

---

## 5. 기존 기능 유지

감사 보고서의 핵심 지적 사항이 유지됩니다:

✅ **체결 확인 후 상태 갱신**
- 주문 API 성공 확인 후 DB 업데이트

✅ **단일 인스턴스 보장**
- ENFORCE_SINGLE_INSTANCE 설정 유지

✅ **동적 실행 간격**
- NEAR_STOPLOSS_EXECUTION_INTERVAL 설정 유지

✅ **ATR 고정값 유지**
- 진입 시 ATR이 포지션 생애 주기 동안 변경 불가

---

## 6. 성능 고려 사항

### 6.1 인덱스 전략

```sql
-- 자주 조회되는 컬럼에 인덱스
CREATE INDEX idx_positions_status ON positions(status);
CREATE INDEX idx_trades_executed_at ON trades(executed_at);
CREATE INDEX idx_trades_symbol ON trades(symbol);
```

### 6.2 커넥션 풀링

```python
# DatabaseConfig 설정
pool_size = 5        # 동시 연결 수
pool_name = "kis_trading_pool"
pool_reset_session = True
```

---

## 7. 테스트

```bash
# 전체 테스트 실행
pytest tests/ -v

# DB 모듈만 테스트
pytest tests/test_db.py -v

# DB 비활성화 상태로 테스트 (기본)
DB_ENABLED=false pytest tests/test_db.py -v
```

---

## 8. 롤백 가이드

MySQL로 전환 후 문제 발생 시 PostgreSQL로 복귀:

1. `db/__init__.py`에서 import 변경:
   ```python
   from db.postgres import ...
   ```

2. `requirements.txt`에서 psycopg2-binary 복원

3. `.env`에서 DB_PORT=5432로 변경

---

## 9. FAQ

**Q: 기존 PostgreSQL 데이터는 어떻게 마이그레이션하나요?**

A: 데이터 마이그레이션 스크립트 필요 (별도 문서 참조)

**Q: OCI Free Tier의 제한 사항은?**

A: 스토리지 20GB, OCPU 1개, RAM 8GB (개인 자동매매에 충분)

**Q: SSL 연결이 필요한가요?**

A: OCI MySQL은 SSL 연결 권장. mysql-connector-python에서 지원

---

## 변경 이력

| 날짜 | 버전 | 설명 |
|------|------|------|
| 2026-01-30 | 1.0 | PostgreSQL → MySQL 마이그레이션 |
