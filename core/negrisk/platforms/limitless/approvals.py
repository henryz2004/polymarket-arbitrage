"""
Token Approval Utility for Limitless Exchange
===============================================

One-time setup for on-chain ERC-20 approvals required before placing orders.

Approvals needed:
- USDC → venue.exchange  (for BUY orders)
- Conditional tokens → venue.exchange  (for SELL orders)
- Conditional tokens → venue.adapter   (for SELL on neg-risk group markets)

Run once per wallet via: python negrisk_long_test.py --platform limitless --setup-approvals
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Base chain USDC contract
USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
# Max approval amount (uint256 max)
MAX_APPROVAL = 2**256 - 1
# ERC-20 approve function selector
APPROVE_SELECTOR = "0x095ea7b3"
# ERC-20 allowance function selector
ALLOWANCE_SELECTOR = "0xdd62ed9e"

# Minimum allowance threshold — re-approve if below this (1M USDC)
MIN_ALLOWANCE_THRESHOLD = 1_000_000 * 10**6


async def check_and_approve(
    private_key: str,
    api_key: str,
    market_slugs: Optional[list[str]] = None,
    rpc_url: str = "https://mainnet.base.org",
) -> dict:
    """
    Check and set token approvals for Limitless Exchange trading.

    Fetches active markets to discover venue contracts, then checks/sets
    USDC approvals for the exchange (and adapter for neg-risk markets).

    Args:
        private_key: Wallet private key for signing approval txns
        api_key: Limitless API key for fetching market data
        rpc_url: Base chain RPC URL

    Returns:
        Dict with approval results: {checked, approved, already_approved, errors}
    """
    from limitless_sdk.api import HttpClient
    from limitless_sdk.markets import MarketFetcher
    from eth_account import Account

    wallet = Account.from_key(private_key)
    http_client = HttpClient(api_key=api_key)
    market_fetcher = MarketFetcher(http_client)

    logger.info(f"Checking token approvals for wallet {wallet.address}")

    stats = {"checked": 0, "approved": 0, "already_approved": 0, "errors": []}

    # Collect unique venue addresses from active markets
    exchange_addrs = set()
    adapter_addrs = set()

    try:
        response = await market_fetcher.get_active_markets()
        markets = response.data
    except Exception as e:
        logger.error(f"Failed to fetch active markets: {e}")
        stats["errors"].append(f"fetch_markets: {e}")
        return stats

    for market in markets:
        if market.venue:
            exchange_addrs.add(market.venue.exchange)
            if market.venue.adapter:
                adapter_addrs.add(market.venue.adapter)

    all_spenders = exchange_addrs | adapter_addrs
    logger.info(
        f"Found {len(exchange_addrs)} exchange(s), {len(adapter_addrs)} adapter(s) "
        f"across {len(markets)} active markets"
    )

    # Check and approve USDC for each spender
    for spender in all_spenders:
        stats["checked"] += 1
        try:
            approved = await _check_and_send_approval(
                wallet=wallet,
                token_address=USDC_ADDRESS,
                spender=spender,
                rpc_url=rpc_url,
            )
            if approved:
                stats["approved"] += 1
                logger.info(f"Approved USDC for spender {spender}")
            else:
                stats["already_approved"] += 1
                logger.info(f"USDC already approved for spender {spender}")
        except Exception as e:
            logger.error(f"Approval failed for spender {spender}: {e}")
            stats["errors"].append(f"{spender}: {e}")

    logger.info(
        f"Approval check complete: {stats['checked']} checked, "
        f"{stats['approved']} newly approved, "
        f"{stats['already_approved']} already approved, "
        f"{len(stats['errors'])} errors"
    )
    return stats


async def _check_and_send_approval(
    wallet,
    token_address: str,
    spender: str,
    rpc_url: str,
) -> bool:
    """
    Check current allowance and send approval if needed.

    Returns True if a new approval was sent, False if already sufficient.
    """
    import aiohttp

    # Check current allowance via eth_call
    allowance = await _get_allowance(
        rpc_url=rpc_url,
        token=token_address,
        owner=wallet.address,
        spender=spender,
    )

    if allowance >= MIN_ALLOWANCE_THRESHOLD:
        return False  # Already approved

    # Build and send approve transaction
    tx_hash = await _send_approve_tx(
        rpc_url=rpc_url,
        wallet=wallet,
        token=token_address,
        spender=spender,
    )
    logger.info(f"Approval tx sent: {tx_hash}")
    return True


async def _get_allowance(rpc_url: str, token: str, owner: str, spender: str) -> int:
    """Read ERC-20 allowance via eth_call."""
    import aiohttp

    # allowance(address,address) calldata
    owner_padded = owner[2:].lower().zfill(64)
    spender_padded = spender[2:].lower().zfill(64)
    data = f"{ALLOWANCE_SELECTOR}{owner_padded}{spender_padded}"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": token, "data": data}, "latest"],
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(rpc_url, json=payload) as resp:
            result = await resp.json()
            hex_val = result.get("result", "0x0")
            return int(hex_val, 16)


async def _send_approve_tx(rpc_url: str, wallet, token: str, spender: str) -> str:
    """Send ERC-20 approve(spender, MAX) transaction."""
    import aiohttp

    # approve(address,uint256) calldata
    spender_padded = spender[2:].lower().zfill(64)
    amount_hex = hex(MAX_APPROVAL)[2:].zfill(64)
    data = f"{APPROVE_SELECTOR}{spender_padded}{amount_hex}"

    # Get nonce
    async with aiohttp.ClientSession() as session:
        nonce_resp = await (await session.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getTransactionCount",
            "params": [wallet.address, "latest"],
        })).json()
        nonce = int(nonce_resp["result"], 16)

        # Get gas price
        gas_resp = await (await session.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_gasPrice",
            "params": [],
        })).json()
        gas_price = int(gas_resp["result"], 16)

    # Build transaction
    tx = {
        "to": token,
        "value": 0,
        "gas": 60000,
        "gasPrice": gas_price,
        "nonce": nonce,
        "data": bytes.fromhex(data[2:] if data.startswith("0x") else data),
        "chainId": 8453,  # Base chain
    }

    signed = wallet.sign_transaction(tx)
    raw_tx = signed.raw_transaction.hex()
    if not raw_tx.startswith("0x"):
        raw_tx = "0x" + raw_tx

    # Send and wait for confirmation
    async with aiohttp.ClientSession() as session:
        send_resp = await (await session.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_sendRawTransaction",
            "params": [raw_tx],
        })).json()

        if "error" in send_resp:
            raise RuntimeError(f"tx failed: {send_resp['error']}")

        tx_hash = send_resp["result"]

        # Poll for receipt (up to 30s)
        for _ in range(15):
            await asyncio.sleep(2)
            receipt_resp = await (await session.post(rpc_url, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_getTransactionReceipt",
                "params": [tx_hash],
            })).json()
            receipt = receipt_resp.get("result")
            if receipt:
                if int(receipt["status"], 16) == 1:
                    logger.info(f"Approval tx confirmed: {tx_hash}")
                    return tx_hash
                else:
                    raise RuntimeError(f"Approval tx reverted: {tx_hash}")

        raise RuntimeError(f"Approval tx not confirmed after 30s: {tx_hash}")
