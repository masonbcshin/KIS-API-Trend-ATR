"""
KIS Trend-ATR Trading System - 데이터 접근 계층 (Repository) - SQLAlchemy 버전
"""

from typing import List, Optional

from db.database import get_db_session
from db.models import Position as PositionORM
# ManagedPosition은 순환참조를 피하기 위해 타입 힌트로만 사용
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from engine.position_manager import ManagedPosition, PositionState


class PositionRepository:
    """
    포지션 데이터 접근 클래스 (SQLAlchemy ORM 사용)
    """

    def _to_domain(self, orm_pos: PositionORM) -> 'ManagedPosition':
        """ORM 객체를 도메인 객체(ManagedPosition)로 변환"""
        from engine.position_manager import ManagedPosition  # 런타임에 임포트
        
        data = {c.name: getattr(orm_pos, c.name) for c in orm_pos.__table__.columns}
        
        # SQLAlchemy가 반환하는 Enum 객체를 실제 Enum 멤버로 변환
        data['state'] = data['state']
        data['exit_reason'] = data.get('exit_reason')

        # 날짜/시간 객체를 문자열로 변환 (기존 ManagedPosition 호환)
        for key in ['entry_date', 'exit_date']:
            if data.get(key):
                data[key] = data[key].isoformat()
        
        for key in ['entry_time', 'exit_time', 'created_at', 'updated_at']:
            if data.get(key):
                data[key] = data[key].isoformat()

        return ManagedPosition.from_dict(data)

    def save(self, position: 'ManagedPosition'):
        """
        포지션을 저장하거나 업데이트합니다 (UPSERT).
        SQLAlchemy의 `merge`를 사용하여 원자적으로 처리합니다.
        """
        with get_db_session() as session:
            if not session:
                print("[REPO] DB 세션이 없어 포지션을 저장할 수 없습니다.")
                return

            position_data = position.to_dict()
            orm_position = PositionORM(**position_data)
            session.merge(orm_position)

    def get_open_positions(self) -> List['ManagedPosition']:
        """
        열려있는 모든 포지션을 조회합니다.
        """
        from engine.position_manager import PositionState
        
        open_positions = []
        with get_db_session() as session:
            if not session:
                print("[REPO] DB 세션이 없어 포지션을 조회할 수 없습니다.")
                return open_positions
            
            orm_positions = session.query(PositionORM).filter(
                PositionORM.state == PositionState.ENTERED
            ).all()
            
            for p in orm_positions:
                open_positions.append(self._to_domain(p))
        
        return open_positions

    def get_by_id(self, position_id: str) -> Optional['ManagedPosition']:
        """ID로 포지션을 조회합니다."""
        with get_db_session() as session:
            if not session:
                return None
            
            orm_position = session.get(PositionORM, position_id)
            if orm_position:
                return self._to_domain(orm_position)
            return None

    def get_by_stock_code(self, stock_code: str) -> Optional['ManagedPosition']:
        """종목 코드로 열려있는 포지션을 조회합니다."""
        from engine.position_manager import PositionState

        with get_db_session() as session:
            if not session:
                return None

            orm_position = session.query(PositionORM).filter(
                PositionORM.stock_code == stock_code,
                PositionORM.state == PositionState.ENTERED
            ).first()
            if orm_position:
                return self._to_domain(orm_position)
            return None

# 전역 저장소 인스턴스
_position_repository: Optional[PositionRepository] = None

def get_position_repository() -> 'PositionRepository':
    """싱글톤 PositionRepository 인스턴스를 반환합니다."""
    global _position_repository
    if _position_repository is None:
        _position_repository = PositionRepository()
    return _position_repository