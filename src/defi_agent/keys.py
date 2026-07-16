"""프라이빗키 보관: Windows Credential Manager (DPAPI 암호화) 우선.

우선순위:
1. OS 키체인 (service='defi-agent', 항목 'wallet' / 'hl-api')
2. 환경변수/.env 폴백 — 사용 시 경고 로그 (마이그레이션 유예용)

CLI:
    python -m defi_agent.keys set wallet     # 프롬프트로 키 입력 → 키체인 저장
    python -m defi_agent.keys set hl-api
    python -m defi_agent.keys show           # 저장 여부·주소만 표시 (키는 미표시)
    python -m defi_agent.keys delete wallet

키체인 저장 후 .env의 WALLET_PRIVATE_KEY / HL_API_WALLET_KEY 줄은 지울 것.
한계: DPAPI는 파일 유출·타 계정·백업 노출은 막지만, 같은 Windows 계정으로
실행되는 악성코드까지는 못 막는다. 그 수준은 Safe 세션 키/원격 서명 단계.
"""
from __future__ import annotations

import logging
import os
import sys

import keyring

log = logging.getLogger(__name__)

SERVICE = "defi-agent"
ENTRIES = {"wallet": "WALLET_PRIVATE_KEY", "hl-api": "HL_API_PRIVATE_KEY"}
ENV_ALIASES = {"WALLET_PRIVATE_KEY": [], "HL_API_PRIVATE_KEY": ["HL_API_WALLET_KEY"]}


def get_key(entry: str) -> str:
    """키체인 → 환경변수 순으로 키 조회. 없으면 빈 문자열."""
    try:
        v = keyring.get_password(SERVICE, entry)
        if v:
            return v.strip()
    except Exception:  # noqa: BLE001 — 키체인 불가 환경이면 env로
        log.exception("키체인 조회 실패 (%s)", entry)
    env_key = ENTRIES[entry]
    for k in (env_key, *ENV_ALIASES.get(env_key, [])):
        v = os.environ.get(k, "").strip()
        if v:
            log.warning("%s를 평문 환경변수에서 로드함 — "
                        "`python -m defi_agent.keys set %s` 로 키체인 이전 권장", k, entry)
            return v
    return ""


def _address_of(key: str) -> str:
    from eth_account import Account
    try:
        return Account.from_key(key).address
    except Exception:  # noqa: BLE001
        return "(유효하지 않은 키)"


def _cli():
    args = sys.argv[1:]
    if not args or args[0] not in ("set", "show", "delete"):
        print(__doc__)
        return
    cmd = args[0]
    if cmd == "show":
        for entry in ENTRIES:
            v = keyring.get_password(SERVICE, entry)
            print(f"{entry:<8}: {'키체인에 저장됨 → ' + _address_of(v) if v else '키체인에 없음'}")
        return
    if len(args) < 2 or args[1] not in ENTRIES:
        print(f"항목을 지정하세요: {' | '.join(ENTRIES)}")
        return
    entry = args[1]
    if cmd == "set":
        import getpass
        key = getpass.getpass(f"{entry} 프라이빗키 입력 (화면 미표시): ").strip()
        if not key.startswith("0x"):
            key = "0x" + key
        addr = _address_of(key)
        if addr.startswith("("):
            print("유효하지 않은 키 — 저장 취소")
            return
        keyring.set_password(SERVICE, entry, key)
        print(f"저장 완료 (Windows Credential Manager): {entry} → {addr}")
        print("이제 .env에서 해당 평문 키 줄을 삭제하세요.")
    elif cmd == "delete":
        keyring.delete_password(SERVICE, entry)
        print(f"삭제 완료: {entry}")


if __name__ == "__main__":
    _cli()
