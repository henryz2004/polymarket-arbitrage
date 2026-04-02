#!/usr/bin/env python3
"""
Polymarket Token Approval Script
==================================

Approves USDC.e and Conditional Tokens for Polymarket's neg-risk contracts.

Required approvals (6 total):
  1. USDC.e -> CTF Exchange
  2. USDC.e -> Neg Risk CTF Exchange
  3. USDC.e -> Neg Risk Adapter
  4. Conditional Tokens -> CTF Exchange
  5. Conditional Tokens -> Neg Risk CTF Exchange
  6. Conditional Tokens -> Neg Risk Adapter

Prerequisites:
  - Funded wallet with MATIC for gas (Polygon network)
  - USDC.e balance for trading
  - Environment variables:
      POLYMARKET_PRIVATE_KEY=0x...
      POLYMARKET_FUNDER=0x...  (optional, derived from private key if not set)

Usage:
  export POLYMARKET_PRIVATE_KEY=0x...
  python scripts/approve_polymarket.py

  # Check-only mode (no transactions):
  python scripts/approve_polymarket.py --check-only

  # Specify RPC URL:
  python scripts/approve_polymarket.py --rpc https://polygon-rpc.com
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Optional

import httpx

# Polygon contract addresses
CONTRACTS = {
    "USDC.e": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
    "Conditional Tokens (CTF)": "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045",
    "CTF Exchange": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "Neg Risk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Neg Risk Adapter": "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
}

# ERC-20 approve ABI (same for USDC.e)
ERC20_APPROVE_SIG = "0x095ea7b3"  # approve(address,uint256)
# ERC-1155 setApprovalForAll ABI (for Conditional Tokens)
ERC1155_APPROVE_SIG = "0xa22cb465"  # setApprovalForAll(address,bool)

# Max uint256 for unlimited approval
MAX_UINT256 = "0x" + "f" * 64

# Spender contracts that need approval
SPENDERS = [
    CONTRACTS["CTF Exchange"],
    CONTRACTS["Neg Risk CTF Exchange"],
    CONTRACTS["Neg Risk Adapter"],
]

DEFAULT_RPC = "https://polygon-rpc.com"


def _encode_approve(spender: str, amount: str = MAX_UINT256) -> str:
    """Encode ERC-20 approve(address, uint256) calldata."""
    spender_padded = spender.lower().replace("0x", "").zfill(64)
    amount_padded = amount.replace("0x", "").zfill(64)
    return ERC20_APPROVE_SIG + spender_padded + amount_padded


def _encode_set_approval_for_all(operator: str, approved: bool = True) -> str:
    """Encode ERC-1155 setApprovalForAll(address, bool) calldata."""
    operator_padded = operator.lower().replace("0x", "").zfill(64)
    approved_padded = "1".zfill(64) if approved else "0".zfill(64)
    return ERC1155_APPROVE_SIG + operator_padded + approved_padded


def _encode_allowance(owner: str, spender: str) -> str:
    """Encode ERC-20 allowance(address, address) calldata."""
    sig = "0xdd62ed3e"
    owner_padded = owner.lower().replace("0x", "").zfill(64)
    spender_padded = spender.lower().replace("0x", "").zfill(64)
    return sig + owner_padded + spender_padded


def _encode_is_approved_for_all(owner: str, operator: str) -> str:
    """Encode ERC-1155 isApprovedForAll(address, address) calldata."""
    sig = "0xe985e9c5"
    owner_padded = owner.lower().replace("0x", "").zfill(64)
    operator_padded = operator.lower().replace("0x", "").zfill(64)
    return sig + owner_padded + operator_padded


async def _eth_call(client: httpx.AsyncClient, rpc_url: str, to: str, data: str) -> str:
    """Make an eth_call and return the result."""
    resp = await client.post(rpc_url, json={
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
        "id": 1,
    })
    result = resp.json()
    if "error" in result:
        raise Exception(f"RPC error: {result['error']}")
    return result.get("result", "0x0")


async def _get_balance(client: httpx.AsyncClient, rpc_url: str, address: str) -> dict:
    """Get MATIC and USDC.e balances."""
    # MATIC balance
    resp = await client.post(rpc_url, json={
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    })
    matic_wei = int(resp.json().get("result", "0x0"), 16)
    matic = matic_wei / 1e18

    # USDC.e balance (6 decimals)
    balance_sig = "0x70a08231" + address.lower().replace("0x", "").zfill(64)
    usdc_hex = await _eth_call(client, rpc_url, CONTRACTS["USDC.e"], balance_sig)
    usdc = int(usdc_hex, 16) / 1e6

    return {"matic": matic, "usdc": usdc}


async def check_approvals(
    wallet_address: str,
    rpc_url: str = DEFAULT_RPC,
) -> list[dict]:
    """
    Check current approval status for all required contracts.

    Returns list of dicts with: token, spender, approved, allowance
    """
    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for spender_name, spender_addr in [
            ("CTF Exchange", CONTRACTS["CTF Exchange"]),
            ("Neg Risk CTF Exchange", CONTRACTS["Neg Risk CTF Exchange"]),
            ("Neg Risk Adapter", CONTRACTS["Neg Risk Adapter"]),
        ]:
            # Check USDC.e allowance
            calldata = _encode_allowance(wallet_address, spender_addr)
            allowance_hex = await _eth_call(client, rpc_url, CONTRACTS["USDC.e"], calldata)
            allowance = int(allowance_hex, 16)
            results.append({
                "token": "USDC.e",
                "spender": spender_name,
                "spender_addr": spender_addr,
                "approved": allowance > 0,
                "allowance": allowance / 1e6 if allowance < 2**128 else float("inf"),
            })

            # Check Conditional Tokens approval
            calldata = _encode_is_approved_for_all(wallet_address, spender_addr)
            approved_hex = await _eth_call(client, rpc_url, CONTRACTS["Conditional Tokens (CTF)"], calldata)
            is_approved = int(approved_hex, 16) != 0
            results.append({
                "token": "Conditional Tokens",
                "spender": spender_name,
                "spender_addr": spender_addr,
                "approved": is_approved,
                "allowance": "unlimited" if is_approved else 0,
            })

    return results


async def run_approvals(
    private_key: str,
    wallet_address: Optional[str] = None,
    rpc_url: str = DEFAULT_RPC,
    check_only: bool = False,
) -> dict:
    """
    Check and optionally submit approval transactions.

    Args:
        private_key: Wallet private key (0x-prefixed)
        wallet_address: Wallet address (derived from key if not provided)
        rpc_url: Polygon RPC URL
        check_only: If True, only check status without submitting transactions

    Returns:
        Summary dict with balances, approval status, and transaction hashes
    """
    # Derive wallet address from private key if not provided
    if not wallet_address:
        try:
            from eth_account import Account
            acct = Account.from_key(private_key)
            wallet_address = acct.address
        except ImportError:
            print("ERROR: eth-account package required. Install with: pip install eth-account")
            sys.exit(1)

    print(f"Wallet: {wallet_address}")
    print(f"RPC: {rpc_url}")
    print()

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Check balances
        balances = await _get_balance(client, rpc_url, wallet_address)
        print(f"Balances:")
        print(f"  MATIC: {balances['matic']:.4f}")
        print(f"  USDC.e: ${balances['usdc']:.2f}")
        print()

        if balances["matic"] < 0.01:
            print("WARNING: Low MATIC balance. You need MATIC for gas on Polygon.")
            print("  Fund your wallet with at least 0.1 MATIC for approvals.")
            if not check_only:
                print("Aborting. Fund wallet first.")
                return {"error": "insufficient_matic"}

    # Check current approvals
    print("Checking current approvals...")
    approvals = await check_approvals(wallet_address, rpc_url)

    needed = []
    for a in approvals:
        status = "APPROVED" if a["approved"] else "NOT APPROVED"
        print(f"  {a['token']} -> {a['spender']}: {status}")
        if not a["approved"]:
            needed.append(a)

    print()

    if not needed:
        print("All approvals already in place!")
        return {"status": "all_approved", "balances": balances}

    print(f"{len(needed)} approvals needed:")
    for n in needed:
        print(f"  {n['token']} -> {n['spender']}")
    print()

    if check_only:
        print("(--check-only mode, not submitting transactions)")
        return {"status": "needs_approval", "needed": len(needed), "balances": balances}

    # Submit approval transactions
    print("Submitting approval transactions...")
    print("NOTE: This requires eth-account and web3 for transaction signing.")
    print("      For manual approval, use the contract addresses above with")
    print("      a wallet like MetaMask or Rabby on Polygon network.")
    print()

    try:
        from eth_account import Account
        acct = Account.from_key(private_key)

        # Get nonce
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0",
                "method": "eth_getTransactionCount",
                "params": [wallet_address, "latest"],
                "id": 1,
            })
            nonce = int(resp.json()["result"], 16)

            # Get gas price
            resp = await client.post(rpc_url, json={
                "jsonrpc": "2.0",
                "method": "eth_gasPrice",
                "params": [],
                "id": 1,
            })
            gas_price = int(resp.json()["result"], 16)

            tx_hashes = []
            for i, n in enumerate(needed):
                # Build calldata
                if n["token"] == "USDC.e":
                    calldata = _encode_approve(n["spender_addr"])
                    to = CONTRACTS["USDC.e"]
                else:
                    calldata = _encode_set_approval_for_all(n["spender_addr"])
                    to = CONTRACTS["Conditional Tokens (CTF)"]

                # Estimate gas with 20% buffer
                est_resp = await client.post(rpc_url, json={
                    "jsonrpc": "2.0",
                    "method": "eth_estimateGas",
                    "params": [{"from": wallet_address, "to": to, "data": calldata}],
                    "id": 1,
                })
                est_result = est_resp.json()
                if "error" in est_result:
                    gas_limit = 100000  # Fallback
                    print(f"  Gas estimate failed, using default {gas_limit}")
                else:
                    gas_limit = int(int(est_result["result"], 16) * 1.2)

                # Build transaction
                tx = {
                    "to": to,
                    "value": 0,
                    "gas": gas_limit,
                    "gasPrice": gas_price,
                    "nonce": nonce + i,
                    "chainId": 137,
                    "data": bytes.fromhex(calldata[2:]),  # Strip 0x prefix
                }

                # Sign
                signed = acct.sign_transaction(tx)
                raw_tx = "0x" + signed.raw_transaction.hex()

                # Send
                resp = await client.post(rpc_url, json={
                    "jsonrpc": "2.0",
                    "method": "eth_sendRawTransaction",
                    "params": [raw_tx],
                    "id": 1,
                })
                result = resp.json()
                if "error" in result:
                    print(f"  ERROR: {n['token']} -> {n['spender']}: {result['error']}")
                else:
                    tx_hash = result["result"]
                    tx_hashes.append(tx_hash)
                    print(f"  SENT: {n['token']} -> {n['spender']}")
                    print(f"    TX: https://polygonscan.com/tx/{tx_hash}")

            print()
            print(f"Submitted {len(tx_hashes)} transactions. Waiting for confirmations...")
            print("Check status on Polygonscan or re-run with --check-only")

            return {"status": "submitted", "tx_hashes": tx_hashes, "balances": balances}

    except ImportError:
        print("ERROR: eth-account package required for transaction signing.")
        print("Install with: pip install eth-account")
        print()
        print("Alternative: Approve manually via MetaMask/Rabby on Polygon:")
        for n in needed:
            token_addr = CONTRACTS["USDC.e"] if n["token"] == "USDC.e" else CONTRACTS["Conditional Tokens (CTF)"]
            print(f"  Token: {token_addr}")
            print(f"  Spender: {n['spender_addr']}")
            print(f"  Function: {'approve' if n['token'] == 'USDC.e' else 'setApprovalForAll'}")
            print()
        return {"status": "manual_required", "needed": len(needed)}


async def main():
    parser = argparse.ArgumentParser(description="Polymarket token approval setup")
    parser.add_argument("--check-only", action="store_true",
                       help="Only check approval status, don't submit transactions")
    parser.add_argument("--rpc", type=str, default=DEFAULT_RPC,
                       help=f"Polygon RPC URL (default: {DEFAULT_RPC})")
    args = parser.parse_args()

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    wallet_address = os.environ.get("POLYMARKET_FUNDER")

    if not private_key and not args.check_only:
        print("ERROR: POLYMARKET_PRIVATE_KEY environment variable required")
        print("  export POLYMARKET_PRIVATE_KEY=0x...")
        sys.exit(1)

    if args.check_only and not private_key and not wallet_address:
        print("ERROR: Need either POLYMARKET_PRIVATE_KEY or POLYMARKET_FUNDER for --check-only")
        sys.exit(1)

    result = await run_approvals(
        private_key=private_key or "",
        wallet_address=wallet_address,
        rpc_url=args.rpc,
        check_only=args.check_only,
    )
    print()
    print(f"Result: {json.dumps(result, indent=2, default=str)}")


if __name__ == "__main__":
    asyncio.run(main())
