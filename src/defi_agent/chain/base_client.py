"""Base 체인 접속, 트랜잭션 전송, DRY_RUN 게이트."""
from __future__ import annotations

import logging

from eth_account import Account
from web3 import Web3

from ..config import Settings

log = logging.getLogger(__name__)


class BaseClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.base_rpc, request_kwargs={"timeout": 30}))
        self.account = Account.from_key(settings.wallet_private_key) if settings.wallet_private_key else None
        # 로컬 nonce 캐시 — 로드밸런싱 RPC의 지연 노드가 낡은 nonce를 주는 문제 방어
        self._nonce: int | None = None

    def _next_nonce(self) -> int:
        chain = self.w3.eth.get_transaction_count(self.address, "pending")
        if self._nonce is None or chain > self._nonce:
            self._nonce = chain
        return self._nonce

    @property
    def address(self) -> str:
        if not self.account:
            raise RuntimeError("WALLET_PRIVATE_KEY 미설정")
        return self.account.address

    def contract(self, address: str, abi: list):
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)

    def has_code(self, address: str) -> bool:
        return len(self.w3.eth.get_code(Web3.to_checksum_address(address))) > 2

    def send(self, fn, value: int = 0, gas_buffer: float = 1.3) -> str | None:
        """컨트랙트 함수 호출 전송. DRY_RUN이면 시뮬레이션만 하고 None 반환."""
        tx_desc = f"{fn.fn_name}({', '.join(str(a)[:40] for a in fn.args)})"
        if self.s.dry_run:
            try:
                fn.call({"from": self.address, "value": value})
                log.info("[DRY_RUN] 시뮬레이션 OK: %s", tx_desc)
            except Exception as e:  # noqa: BLE001
                log.warning("[DRY_RUN] 시뮬레이션 실패: %s → %s", tx_desc, e)
            return None
        gas = int(fn.estimate_gas({"from": self.address, "value": value}) * gas_buffer)
        for attempt in (1, 2):
            tx = fn.build_transaction({
                "from": self.address,
                "value": value,
                "gas": gas,
                "nonce": self._next_nonce(),
                "maxFeePerGas": self.w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": self.w3.to_wei(0.001, "gwei"),
                "chainId": 8453,
            })
            signed = self.account.sign_transaction(tx)
            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            except Exception as e:  # noqa: BLE001
                if "nonce too low" in str(e) and attempt == 1:
                    # 지연 노드가 준 낡은 nonce — 체인 기준으로 재동기화 후 1회 재시도
                    self._nonce = None
                    log.warning("nonce too low — 재동기화 후 재시도: %s", tx_desc)
                    continue
                raise
            break
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"tx revert: {tx_hash.hex()} ({tx_desc})")
        self._nonce = tx["nonce"] + 1
        log.info("tx 확정: %s %s", tx_hash.hex(), tx_desc)
        return tx_hash.hex()
