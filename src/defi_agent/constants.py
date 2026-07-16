"""Base 메인넷 주소와 최소 ABI.

주소는 배포 전 반드시 재검증할 것:
- Aerodrome 공식 문서: https://aerodrome.finance/security  (Contract addresses)
- BaseScan에서 각 주소의 컨트랙트명이 CLFactory / NonfungiblePositionManager /
  SwapRouter 인지 확인.
main.py 기동 시 startup_verify()가 factory→pool 해석과 코드 존재 여부를 검사한다.
"""

# ── 토큰 (Base 표준 배포, 검증됨) ─────────────────────────────
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_DECIMALS = 18
USDC_DECIMALS = 6

# ── Aerodrome Slipstream (배포 전 재검증 필요) ────────────────
CL_FACTORY = "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
NPM = "0x827922686190790b37229fd06084350E74485b72"  # NonfungiblePositionManager
SWAP_ROUTER = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"

Q96 = 2**96

# ── 최소 ABI ─────────────────────────────────────────────────
ERC20_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "a", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "bool"}]},
    {"name": "decimals", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"type": "uint8"}]},
]

FACTORY_ABI = [
    {"name": "getPool", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "address"}, {"type": "int24"}],
     "outputs": [{"type": "address"}]},
]

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "sqrtPriceX96", "type": "uint160"}, {"name": "tick", "type": "int24"},
                 {"type": "uint16"}, {"type": "uint16"}, {"type": "uint16"}, {"type": "bool"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "uint128"}]},
    {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view", "inputs": [],
     "outputs": [{"type": "address"}]},
]

NPM_ABI = [
    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "uint256"}],
     "outputs": [{"name": "nonce", "type": "uint96"}, {"name": "operator", "type": "address"},
                 {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
                 {"name": "tickSpacing", "type": "int24"}, {"name": "tickLower", "type": "int24"},
                 {"name": "tickUpper", "type": "int24"}, {"name": "liquidity", "type": "uint128"},
                 {"name": "feeGrowthInside0LastX128", "type": "uint256"},
                 {"name": "feeGrowthInside1LastX128", "type": "uint256"},
                 {"name": "tokensOwed0", "type": "uint128"}, {"name": "tokensOwed1", "type": "uint128"}]},
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}]},
    {"name": "tokenOfOwnerByIndex", "type": "function", "stateMutability": "view",
     "inputs": [{"type": "address"}, {"type": "uint256"}], "outputs": [{"type": "uint256"}]},
    {"name": "mint", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "token0", "type": "address"}, {"name": "token1", "type": "address"},
         {"name": "tickSpacing", "type": "int24"}, {"name": "tickLower", "type": "int24"},
         {"name": "tickUpper", "type": "int24"},
         {"name": "amount0Desired", "type": "uint256"}, {"name": "amount1Desired", "type": "uint256"},
         {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
         {"name": "recipient", "type": "address"}, {"name": "deadline", "type": "uint256"},
         {"name": "sqrtPriceX96", "type": "uint160"}]}],
     "outputs": [{"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
                 {"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "decreaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "liquidity", "type": "uint128"},
         {"name": "amount0Min", "type": "uint256"}, {"name": "amount1Min", "type": "uint256"},
         {"name": "deadline", "type": "uint256"}]}],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "collect", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId", "type": "uint256"}, {"name": "recipient", "type": "address"},
         {"name": "amount0Max", "type": "uint128"}, {"name": "amount1Max", "type": "uint128"}]}],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
    {"name": "burn", "type": "function", "stateMutability": "payable",
     "inputs": [{"type": "uint256"}], "outputs": []},
]

ROUTER_ABI = [
    {"name": "exactInputSingle", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenIn", "type": "address"}, {"name": "tokenOut", "type": "address"},
         {"name": "tickSpacing", "type": "int24"}, {"name": "recipient", "type": "address"},
         {"name": "deadline", "type": "uint256"}, {"name": "amountIn", "type": "uint256"},
         {"name": "amountOutMinimum", "type": "uint256"},
         {"name": "sqrtPriceLimitX96", "type": "uint160"}]}],
     "outputs": [{"name": "amountOut", "type": "uint256"}]},
]
