"""
═══════════════════════════════════════════════════════════════════════════════
KIS Trend-ATR Trading System - 장 운영 스케줄러
═══════════════════════════════════════════════════════════════════════════════

한국 주식시장 거래시간에 맞춰 자동매매를 스케줄링합니다.

★ 운영 스케줄:
    - 08:50 ~ 09:00: 장 시작 준비 (포지션 복구, 설정 로드)
    - 09:00 ~ 15:20: 정규장 매매
    - 15:20 ~ 15:30: 장 마감 처리 (일일 리포트 등)

★ 안전장치:
    - 휴장일 자동 인식 및 대기
    - 장외 시간 매매 차단
    - 장 시작 전 포지션 정합성 검증
"""

import time
import threading
from datetime import datetime, date, timedelta
from typing import Callable, Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum

from utils.market_hours import (
    is_market_open,
    is_holiday,
    is_weekend,
    get_market_status,
    get_time_to_market_open,
    get_next_trading_day,
    MARKET_OPEN,
    MARKET_CLOSE
)
from utils.logger import get_logger
from utils.telegram_notifier import get_telegram_notifier

logger = get_logger("market_scheduler")


# ═══════════════════════════════════════════════════════════════════════════════
# 열거형 및 데이터 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class MarketPhase(Enum):
    """시장 단계"""
    PRE_MARKET = "PRE_MARKET"           # 장 시작 전 (08:00 ~ 09:00)
    MARKET_OPEN = "MARKET_OPEN"         # 정규장 (09:00 ~ 15:20)
    MARKET_CLOSING = "MARKET_CLOSING"   # 장 마감 (15:20 ~ 15:30)
    AFTER_MARKET = "AFTER_MARKET"       # 장 종료 후
    HOLIDAY = "HOLIDAY"                 # 휴장일
    WEEKEND = "WEEKEND"                 # 주말


@dataclass
class ScheduledTask:
    """스케줄된 작업"""
    name: str
    callback: Callable
    interval_seconds: int
    phase: MarketPhase = MarketPhase.MARKET_OPEN
    enabled: bool = True
    last_run: datetime = None
    run_count: int = 0


class SchedulerState(Enum):
    """스케줄러 상태"""
    STOPPED = "STOPPED"
    WAITING = "WAITING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


# ═══════════════════════════════════════════════════════════════════════════════
# 장 스케줄러 클래스
# ═══════════════════════════════════════════════════════════════════════════════

class MarketScheduler:
    """
    장 운영 스케줄러
    
    KRX 거래시간에 맞춰 자동매매를 스케줄링합니다.
    
    Usage:
        scheduler = MarketScheduler()
        
        # 콜백 등록
        scheduler.on_pre_market(setup_callback)
        scheduler.on_market_open(trading_callback, interval=60)
        scheduler.on_market_close(cleanup_callback)
        
        # 스케줄러 시작
        scheduler.start()
    """
    
    def __init__(
        self,
        auto_wait_for_market: bool = True,
        pre_market_minutes: int = 10,
        post_market_minutes: int = 10
    ):
        """
        장 스케줄러 초기화
        
        Args:
            auto_wait_for_market: 장 시작까지 자동 대기
            pre_market_minutes: 장 시작 전 준비 시간 (분)
            post_market_minutes: 장 종료 후 정리 시간 (분)
        """
        self.auto_wait_for_market = auto_wait_for_market
        self.pre_market_minutes = pre_market_minutes
        self.post_market_minutes = post_market_minutes
        
        # 상태
        self._state = SchedulerState.STOPPED
        self._current_phase = MarketPhase.AFTER_MARKET
        
        # 스레드
        self._main_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # 작업 등록
        self._pre_market_tasks: List[ScheduledTask] = []
        self._market_tasks: List[ScheduledTask] = []
        self._post_market_tasks: List[ScheduledTask] = []
        
        # 텔레그램
        self._telegram = get_telegram_notifier()
        
        logger.info("[SCHEDULER] 장 스케줄러 초기화 완료")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 콜백 등록
    # ═══════════════════════════════════════════════════════════════════════════
    
    def on_pre_market(
        self,
        callback: Callable,
        name: str = "pre_market_task"
    ) -> None:
        """
        장 시작 전 콜백을 등록합니다.
        
        Args:
            callback: 콜백 함수
            name: 작업 이름
        """
        task = ScheduledTask(
            name=name,
            callback=callback,
            interval_seconds=0,  # 1회 실행
            phase=MarketPhase.PRE_MARKET
        )
        self._pre_market_tasks.append(task)
        logger.debug(f"[SCHEDULER] Pre-market 작업 등록: {name}")
    
    def on_market_open(
        self,
        callback: Callable,
        interval: int = 60,
        name: str = "market_task"
    ) -> None:
        """
        정규장 콜백을 등록합니다.
        
        Args:
            callback: 콜백 함수
            interval: 실행 간격 (초)
            name: 작업 이름
        """
        task = ScheduledTask(
            name=name,
            callback=callback,
            interval_seconds=max(60, interval),  # 최소 60초
            phase=MarketPhase.MARKET_OPEN
        )
        self._market_tasks.append(task)
        logger.debug(f"[SCHEDULER] Market 작업 등록: {name} (간격={interval}초)")
    
    def on_market_close(
        self,
        callback: Callable,
        name: str = "post_market_task"
    ) -> None:
        """
        장 종료 후 콜백을 등록합니다.
        
        Args:
            callback: 콜백 함수
            name: 작업 이름
        """
        task = ScheduledTask(
            name=name,
            callback=callback,
            interval_seconds=0,  # 1회 실행
            phase=MarketPhase.MARKET_CLOSING
        )
        self._post_market_tasks.append(task)
        logger.debug(f"[SCHEDULER] Post-market 작업 등록: {name}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 스케줄러 제어
    # ═══════════════════════════════════════════════════════════════════════════
    
    def start(self, blocking: bool = True) -> None:
        """
        스케줄러를 시작합니다.
        
        Args:
            blocking: True면 메인 스레드에서 실행, False면 백그라운드
        """
        if self._state != SchedulerState.STOPPED:
            logger.warning("[SCHEDULER] 이미 실행 중입니다")
            return
        
        self._stop_event.clear()
        
        if blocking:
            self._run_scheduler()
        else:
            self._main_thread = threading.Thread(
                target=self._run_scheduler,
                daemon=True
            )
            self._main_thread.start()
        
        logger.info("[SCHEDULER] 스케줄러 시작됨")
    
    def stop(self, wait: bool = True) -> None:
        """
        스케줄러를 중지합니다.
        
        Args:
            wait: True면 작업 완료까지 대기
        """
        self._stop_event.set()
        self._state = SchedulerState.STOPPED
        
        if wait and self._main_thread and self._main_thread.is_alive():
            self._main_thread.join(timeout=10)
        
        logger.info("[SCHEDULER] 스케줄러 중지됨")
    
    def pause(self) -> None:
        """스케줄러를 일시 정지합니다."""
        if self._state == SchedulerState.RUNNING:
            self._state = SchedulerState.PAUSED
            logger.info("[SCHEDULER] 스케줄러 일시 정지")
    
    def resume(self) -> None:
        """스케줄러를 재개합니다."""
        if self._state == SchedulerState.PAUSED:
            self._state = SchedulerState.RUNNING
            logger.info("[SCHEDULER] 스케줄러 재개")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 메인 루프
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _run_scheduler(self) -> None:
        """스케줄러 메인 루프"""
        self._state = SchedulerState.RUNNING
        
        logger.info("[SCHEDULER] 메인 루프 시작")
        self._telegram.notify_info("장 스케줄러가 시작되었습니다.")
        
        try:
            while not self._stop_event.is_set():
                # 현재 단계 업데이트
                self._update_market_phase()
                
                if self._state == SchedulerState.PAUSED:
                    time.sleep(1)
                    continue
                
                # 단계별 처리
                if self._current_phase == MarketPhase.WEEKEND:
                    self._handle_weekend()
                    
                elif self._current_phase == MarketPhase.HOLIDAY:
                    self._handle_holiday()
                    
                elif self._current_phase == MarketPhase.PRE_MARKET:
                    self._handle_pre_market()
                    
                elif self._current_phase == MarketPhase.MARKET_OPEN:
                    self._handle_market_open()
                    
                elif self._current_phase == MarketPhase.MARKET_CLOSING:
                    self._handle_market_closing()
                    
                elif self._current_phase == MarketPhase.AFTER_MARKET:
                    self._handle_after_market()
                
                # 짧은 대기
                time.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("[SCHEDULER] 사용자에 의해 중단됨")
        except Exception as e:
            logger.error(f"[SCHEDULER] 오류 발생: {e}")
            self._telegram.notify_error("스케줄러 오류", str(e))
        finally:
            self._state = SchedulerState.STOPPED
            logger.info("[SCHEDULER] 메인 루프 종료")
    
    def _update_market_phase(self) -> None:
        """현재 시장 단계를 업데이트합니다."""
        now = datetime.now()
        today = now.date()
        current_time = now.time()
        
        # 주말 체크
        if is_weekend(today):
            self._current_phase = MarketPhase.WEEKEND
            return
        
        # 휴장일 체크
        if is_holiday(today):
            self._current_phase = MarketPhase.HOLIDAY
            return
        
        # 시간대별 단계
        from datetime import time as dt_time
        
        pre_market_start = dt_time(
            MARKET_OPEN.hour, 
            MARKET_OPEN.minute - self.pre_market_minutes
        )
        
        if current_time < pre_market_start:
            self._current_phase = MarketPhase.AFTER_MARKET
        elif current_time < MARKET_OPEN:
            self._current_phase = MarketPhase.PRE_MARKET
        elif current_time < MARKET_CLOSE:
            self._current_phase = MarketPhase.MARKET_OPEN
        elif current_time < dt_time(
            MARKET_CLOSE.hour, 
            MARKET_CLOSE.minute + self.post_market_minutes
        ):
            self._current_phase = MarketPhase.MARKET_CLOSING
        else:
            self._current_phase = MarketPhase.AFTER_MARKET
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 단계별 핸들러
    # ═══════════════════════════════════════════════════════════════════════════
    
    def _handle_weekend(self) -> None:
        """주말 처리"""
        next_day = get_next_trading_day()
        wait_seconds = get_time_to_market_open()
        
        if self.auto_wait_for_market:
            logger.info(
                f"[SCHEDULER] 주말 - 다음 거래일: {next_day}, "
                f"대기 시간: {wait_seconds // 3600}시간"
            )
            
            # 1시간마다 체크
            sleep_time = min(wait_seconds, 3600)
            time.sleep(sleep_time)
        else:
            time.sleep(60)
    
    def _handle_holiday(self) -> None:
        """휴장일 처리"""
        next_day = get_next_trading_day()
        wait_seconds = get_time_to_market_open()
        
        logger.info(
            f"[SCHEDULER] 휴장일 - 다음 거래일: {next_day}"
        )
        
        if self.auto_wait_for_market:
            sleep_time = min(wait_seconds, 3600)
            time.sleep(sleep_time)
        else:
            time.sleep(60)
    
    def _handle_pre_market(self) -> None:
        """장 시작 전 처리"""
        if self._state != SchedulerState.RUNNING:
            return
        
        logger.info("[SCHEDULER] 장 시작 전 준비 단계")
        
        # Pre-market 작업 실행
        for task in self._pre_market_tasks:
            if task.enabled:
                try:
                    logger.info(f"[SCHEDULER] Pre-market 작업 실행: {task.name}")
                    task.callback()
                    task.last_run = datetime.now()
                    task.run_count += 1
                except Exception as e:
                    logger.error(f"[SCHEDULER] Pre-market 작업 오류 ({task.name}): {e}")
        
        # 장 시작까지 대기
        wait_seconds = get_time_to_market_open()
        if wait_seconds > 0:
            logger.info(f"[SCHEDULER] 장 시작까지 {wait_seconds}초 대기")
            time.sleep(min(wait_seconds, 60))
    
    def _handle_market_open(self) -> None:
        """정규장 처리"""
        if self._state != SchedulerState.RUNNING:
            return
        
        # Market 작업 실행
        now = datetime.now()
        
        for task in self._market_tasks:
            if not task.enabled:
                continue
            
            # 실행 간격 체크
            should_run = False
            if task.last_run is None:
                should_run = True
            else:
                elapsed = (now - task.last_run).total_seconds()
                if elapsed >= task.interval_seconds:
                    should_run = True
            
            if should_run:
                try:
                    logger.debug(f"[SCHEDULER] Market 작업 실행: {task.name}")
                    task.callback()
                    task.last_run = now
                    task.run_count += 1
                except Exception as e:
                    logger.error(f"[SCHEDULER] Market 작업 오류 ({task.name}): {e}")
        
        # 짧은 대기 (CPU 부하 방지)
        time.sleep(1)
    
    def _handle_market_closing(self) -> None:
        """장 마감 처리"""
        if self._state != SchedulerState.RUNNING:
            return
        
        logger.info("[SCHEDULER] 장 마감 처리 단계")
        
        # Post-market 작업 실행
        for task in self._post_market_tasks:
            if task.enabled and task.run_count == 0:  # 당일 1회만
                try:
                    logger.info(f"[SCHEDULER] Post-market 작업 실행: {task.name}")
                    task.callback()
                    task.last_run = datetime.now()
                    task.run_count += 1
                except Exception as e:
                    logger.error(f"[SCHEDULER] Post-market 작업 오류 ({task.name}): {e}")
        
        time.sleep(60)
    
    def _handle_after_market(self) -> None:
        """장 종료 후 처리"""
        if self.auto_wait_for_market:
            wait_seconds = get_time_to_market_open()
            
            if wait_seconds > 0:
                # 작업 카운터 리셋 (다음 날을 위해)
                self._reset_task_counters()
                
                logger.info(
                    f"[SCHEDULER] 장 종료 - 다음 장까지 {wait_seconds // 3600}시간 대기"
                )
                
                # 1시간마다 체크
                sleep_time = min(wait_seconds, 3600)
                time.sleep(sleep_time)
        else:
            time.sleep(60)
    
    def _reset_task_counters(self) -> None:
        """작업 카운터를 리셋합니다."""
        for task in self._pre_market_tasks:
            task.run_count = 0
            task.last_run = None
        
        for task in self._market_tasks:
            task.last_run = None
        
        for task in self._post_market_tasks:
            task.run_count = 0
            task.last_run = None
    
    # ═══════════════════════════════════════════════════════════════════════════
    # 상태 조회
    # ═══════════════════════════════════════════════════════════════════════════
    
    @property
    def state(self) -> SchedulerState:
        """현재 상태"""
        return self._state
    
    @property
    def current_phase(self) -> MarketPhase:
        """현재 시장 단계"""
        return self._current_phase
    
    def get_status(self) -> Dict[str, Any]:
        """스케줄러 상태를 반환합니다."""
        is_open, status_msg = get_market_status()
        
        return {
            "state": self._state.value,
            "phase": self._current_phase.value,
            "market_open": is_open,
            "market_status": status_msg,
            "pre_market_tasks": len(self._pre_market_tasks),
            "market_tasks": len(self._market_tasks),
            "post_market_tasks": len(self._post_market_tasks),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    def print_status(self) -> None:
        """현재 상태를 출력합니다."""
        status = self.get_status()
        
        print("\n" + "═" * 50)
        print("         [MARKET SCHEDULER STATUS]")
        print("═" * 50)
        print(f"  상태: {status['state']}")
        print(f"  단계: {status['phase']}")
        print(f"  시장: {status['market_status']}")
        print("-" * 50)
        print(f"  Pre-market 작업: {status['pre_market_tasks']}개")
        print(f"  Market 작업: {status['market_tasks']}개")
        print(f"  Post-market 작업: {status['post_market_tasks']}개")
        print("═" * 50 + "\n")


# ═══════════════════════════════════════════════════════════════════════════════
# 편의 함수
# ═══════════════════════════════════════════════════════════════════════════════

_scheduler_instance: Optional[MarketScheduler] = None


def get_market_scheduler(**kwargs) -> MarketScheduler:
    """
    싱글톤 MarketScheduler를 반환합니다.
    
    Returns:
        MarketScheduler: 장 스케줄러
    """
    global _scheduler_instance
    
    if _scheduler_instance is None:
        _scheduler_instance = MarketScheduler(**kwargs)
    
    return _scheduler_instance
