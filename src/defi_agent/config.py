"""환경설정 로딩. 모든 수치 한도는 여기서만 읽는다.

로딩 순서 (앞이 우선):
1. 프로세스 환경변수
2. ~/projects/defi-agent/.env   (프로젝트 전용 — WALLET_PRIVATE_KEY 등)
3. ~/quant/.env                 (공유 — TG 토큰, HL 키 재사용)

quant/.env 변수명 별칭:
  TELEGRAM_BOT_TOKEN → TG_BOT_TOKEN · TELEGRAM_ALLOWED_USER_ID → TG_CHAT_ID
  HL_API_WALLET_KEY → HL_API_PRIVATE_KEY · HL_TESTNET 지원
DB는 quant의 DB_PATH를 절대 쓰지 않는다 (DEFI_DB_PATH만 인식).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

QUANT_ENV = Path.home() / "quant" / ".env"


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    base_rpc: str
    wallet_private_key: str
    lp_tick_spacing: int
    lp_range_pct: float
    lp_max_usdc: float
    rerange_trigger: float
    rerange_cooldown_h: float
    hl_api_private_key: str
    hl_account_address: str
    hl_coin: str
    hedge_drift_pct: float
    hl_max_leverage: float
    hl_testnet: bool
    tg_bot_token: str
    tg_chat_id: str
    rebalance_interval_sec: int
    status_notify_min: int
    daily_report_hour_kst: int
    db_path: str


def load_settings() -> Settings:
    # override=False: 먼저 로드된 값이 우선 → 프로젝트 .env가 quant/.env를 이긴다
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    load_dotenv(QUANT_ENV, override=False)

    def env(key: str, default: str = "", *aliases: str) -> str:
        for k in (key, *aliases):
            v = os.environ.get(k, "").strip()
            if v:
                return v
        return default

    from .keys import get_key  # 순환 임포트 방지용 지연 임포트

    return Settings(
        dry_run=env("DRY_RUN", "true").lower() != "false",
        base_rpc=env("BASE_RPC", "https://mainnet.base.org"),
        wallet_private_key=get_key("wallet"),
        lp_tick_spacing=int(env("LP_TICK_SPACING", "100")),
        lp_range_pct=float(env("LP_RANGE_PCT", "35")),
        lp_max_usdc=float(env("LP_MAX_USDC", "5000")),
        rerange_trigger=float(env("RERANGE_TRIGGER", "0.90")),
        rerange_cooldown_h=float(env("RERANGE_COOLDOWN_H", "12")),
        hl_api_private_key=get_key("hl-api"),
        hl_account_address=env("HL_ACCOUNT_ADDRESS"),
        hl_coin=env("HL_COIN", "ETH"),
        hedge_drift_pct=float(env("HEDGE_DRIFT_PCT", "5")),
        hl_max_leverage=float(env("HL_MAX_LEVERAGE", "3")),
        hl_testnet=env("HL_TESTNET", "false").lower() == "true",
        tg_bot_token=env("TG_BOT_TOKEN", "", "TELEGRAM_BOT_TOKEN"),
        tg_chat_id=env("TG_CHAT_ID", "", "TELEGRAM_ALLOWED_USER_ID"),
        rebalance_interval_sec=int(env("REBALANCE_INTERVAL_SEC", "600")),
        # 주기 상태 알림 간격(분). 0이면 비활성 — 액션·경보 알림만 발송.
        # 사이클(REBALANCE_INTERVAL_SEC)에 얹혀 발송되므로 실제 간격은 사이클 단위로 올림된다.
        status_notify_min=int(env("STATUS_NOTIFY_MIN", "60")),
        daily_report_hour_kst=int(env("DAILY_REPORT_HOUR_KST", "9")),
        # 주의: quant의 DB_PATH와 공유 금지 — DEFI_DB_PATH만 인식
        db_path=env("DEFI_DB_PATH", str(Path(__file__).resolve().parents[2] / "defi_agent.db")),
    )
