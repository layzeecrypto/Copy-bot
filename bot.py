"""
====================================================
  SOLANA MEMECOIN COPY TRADING BOT
  Tracks wallets & auto-copies trades via Jupiter DEX
====================================================
"""

import asyncio
import aiohttp
import json
import base64
import time
import os
from datetime import datetime
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from config import Config

# ─────────────────────────────────────────
#  COLORS for terminal output
# ─────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def log(msg, color=RESET):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}")


# ─────────────────────────────────────────
#  TOKEN INFO — get name/symbol from mint
# ─────────────────────────────────────────
async def get_token_info(session: aiohttp.ClientSession, mint: str) -> dict:
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            pairs = data.get("pairs", [])
            if pairs:
                p = pairs[0]
                return {
                    "name":   p.get("baseToken", {}).get("name", "Unknown"),
                    "symbol": p.get("baseToken", {}).get("symbol", "???"),
                    "price":  p.get("priceUsd", "N/A"),
                    "volume": p.get("volume", {}).get("h24", 0),
                }
    except:
        pass
    return {"name": "Unknown", "symbol": "???", "price": "N/A", "volume": 0}


# ─────────────────────────────────────────
#  JUPITER SWAP — buy/sell token
# ─────────────────────────────────────────
SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

async def jupiter_swap(
    session: aiohttp.ClientSession,
    keypair: Keypair,
    client: AsyncClient,
    input_mint: str,
    output_mint: str,
    amount_lamports: int,
    slippage_bps: int = 300,
) -> str | None:
    """
    Execute a swap via Jupiter Aggregator.
    Returns transaction signature or None on failure.
    """
    try:
        # 1. Get quote
        quote_url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={input_mint}"
            f"&outputMint={output_mint}"
            f"&amount={amount_lamports}"
            f"&slippageBps={slippage_bps}"
        )
        async with session.get(quote_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log(f"Jupiter quote failed: {r.status}", RED)
                return None
            quote = await r.json()

        # 2. Get swap transaction
        swap_url = "https://quote-api.jup.ag/v6/swap"
        payload = {
            "quoteResponse":         quote,
            "userPublicKey":         str(keypair.pubkey()),
            "wrapAndUnwrapSol":      True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": Config.PRIORITY_FEE_LAMPORTS,
        }
        async with session.post(swap_url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log(f"Jupiter swap failed: {r.status}", RED)
                return None
            swap_data = await r.json()

        # 3. Deserialize & sign transaction
        raw_tx = base64.b64decode(swap_data["swapTransaction"])
        tx = VersionedTransaction.from_bytes(raw_tx)
        signed_tx = keypair.sign_message(bytes(tx.message))

        # 4. Send transaction
        resp = await client.send_raw_transaction(
            bytes(tx),
            opts={"skip_preflight": False, "preflight_commitment": Confirmed}
        )
        sig = str(resp.value)
        log(f"Swap TX sent: https://solscan.io/tx/{sig}", CYAN)
        return sig

    except Exception as e:
        log(f"Swap error: {e}", RED)
        return None


# ─────────────────────────────────────────
#  DETECT TRADE from transaction
# ─────────────────────────────────────────
async def detect_trade(
    session: aiohttp.ClientSession,
    client: AsyncClient,
    sig: str,
    tracked_wallet: str
) -> dict | None:
    """
    Parse a transaction to detect if it's a token buy/sell.
    Returns trade info dict or None.
    """
    try:
        tx = await client.get_transaction(
            sig,
            encoding="jsonParsed",
            commitment=Confirmed,
            max_supported_transaction_version=0
        )
        if not tx.value:
            return None

        meta = tx.value.transaction.meta
        if meta is None or meta.err is not None:
            return None

        pre_balances  = meta.pre_token_balances  or []
        post_balances = meta.post_token_balances or []

        # Find token balance changes
        pre_map  = {b.account_index: b for b in pre_balances}
        post_map = {b.account_index: b for b in post_balances}

        all_indexes = set(pre_map.keys()) | set(post_map.keys())

        for idx in all_indexes:
            pre  = pre_map.get(idx)
            post = post_map.get(idx)

            if not post:
                continue

            mint  = post.mint
            owner = str(post.owner) if post.owner else ""

            if owner != tracked_wallet:
                continue

            # Skip SOL/USDC wrapped
            if mint in (SOL_MINT, USDC_MINT):
                continue

            pre_amt  = float(pre.ui_token_amount.ui_amount  or 0) if pre  else 0
            post_amt = float(post.ui_token_amount.ui_amount or 0)
            diff     = post_amt - pre_amt

            if abs(diff) < 0.0001:
                continue

            action = "BUY" if diff > 0 else "SELL"

            # Estimate SOL spent (from SOL balance change)
            sol_pre  = meta.pre_balances[0]  / 1e9 if meta.pre_balances  else 0
            sol_post = meta.post_balances[0] / 1e9 if meta.post_balances else 0
            sol_diff = abs(sol_pre - sol_post)

            info = await get_token_info(session, mint)

            return {
                "action":     action,
                "mint":       mint,
                "symbol":     info["symbol"],
                "name":       info["name"],
                "price_usd":  info["price"],
                "amount":     diff,
                "sol_amount": sol_diff,
                "signature":  sig,
                "wallet":     tracked_wallet,
            }

    except Exception as e:
        log(f"Parse error for {sig[:16]}: {e}", YELLOW)

    return None


# ─────────────────────────────────────────
#  WALLET MONITOR — watch one trader
# ─────────────────────────────────────────
class WalletMonitor:
    def __init__(self, wallet_address: str, label: str):
        self.wallet  = wallet_address
        self.label   = label
        self.seen    = set()

    async def get_recent_sigs(self, client: AsyncClient) -> list[str]:
        try:
            pubkey = Pubkey.from_string(self.wallet)
            resp = await client.get_signatures_for_address(
                pubkey,
                limit=10,
                commitment=Confirmed
            )
            return [str(s.signature) for s in resp.value]
        except Exception as e:
            log(f"Sig fetch error [{self.label}]: {e}", YELLOW)
            return []

    async def poll(
        self,
        session: aiohttp.ClientSession,
        client: AsyncClient,
        on_trade_callback
    ):
        log(f"👀 Monitoring {self.label} ({self.wallet[:8]}...)", CYAN)
        while True:
            try:
                sigs = await self.get_recent_sigs(client)
                new_sigs = [s for s in sigs if s not in self.seen]

                for sig in new_sigs:
                    self.seen.add(sig)
                    trade = await detect_trade(session, client, sig, self.wallet)
                    if trade:
                        trade["trader_label"] = self.label
                        await on_trade_callback(trade)

                # Keep seen set small
                if len(self.seen) > 200:
                    self.seen = set(list(self.seen)[-100:])

            except Exception as e:
                log(f"Monitor error [{self.label}]: {e}", YELLOW)

            await asyncio.sleep(Config.POLL_INTERVAL)


# ─────────────────────────────────────────
#  COPY TRADING ENGINE
# ─────────────────────────────────────────
class CopyTradingBot:
    def __init__(self):
        self.keypair      = Keypair.from_base58_string(Config.MY_PRIVATE_KEY)
        self.my_wallet    = str(self.keypair.pubkey())
        self.trades_today = 0
        self.pnl_today    = 0.0
        self.active       = True
        self.positions    = {}  # mint -> {amount, cost_sol, buy_time}

        log(f"🤖 Bot wallet: {self.my_wallet}", CYAN)

    async def on_trade_detected(self, trade: dict):
        """Called whenever a tracked trader makes a trade."""
        action = trade["action"]
        symbol = trade["symbol"]
        label  = trade["trader_label"]
        mint   = trade["mint"]
        sol_amt = trade["sol_amount"]

        if action == "BUY":
            log(f"📡 {label} BOUGHT {symbol} ({mint[:8]}...) for ~{sol_amt:.4f} SOL", GREEN)
        else:
            log(f"📡 {label} SOLD {symbol} ({mint[:8]}...) ~{sol_amt:.4f} SOL", RED)

        if not self.active:
            log("Bot paused — skipping copy", YELLOW)
            return

        if self.trades_today >= Config.MAX_TRADES_PER_DAY:
            log(f"Max daily trades reached ({Config.MAX_TRADES_PER_DAY})", YELLOW)
            return

        await self.execute_copy(trade)

    async def execute_copy(self, trade: dict):
        action = trade["action"]
        mint   = trade["mint"]
        symbol = trade["symbol"]

        async with aiohttp.ClientSession() as session:
            client = AsyncClient(Config.SOLANA_RPC_URL)

            if action == "BUY":
                # Buy: spend fixed SOL amount
                lamports = int(Config.TRADE_AMOUNT_SOL * 1e9)
                log(f"🚀 Copying BUY {symbol} — spending {Config.TRADE_AMOUNT_SOL} SOL", GREEN)

                sig = await jupiter_swap(
                    session, self.keypair, client,
                    input_mint=SOL_MINT,
                    output_mint=mint,
                    amount_lamports=lamports,
                    slippage_bps=Config.SLIPPAGE_BPS
                )
                if sig:
                    self.positions[mint] = {
                        "symbol":   symbol,
                        "cost_sol": Config.TRADE_AMOUNT_SOL,
                        "buy_time": time.time(),
                        "buy_sig":  sig,
                    }
                    self.trades_today += 1
                    log(f"✅ BUY copied! {symbol} | TX: {sig[:20]}...", GREEN)

            elif action == "SELL" and mint in self.positions:
                log(f"🔴 Copying SELL {symbol}", RED)

                # Get current token balance
                balance = await self.get_token_balance(client, mint)
                if balance > 0:
                    # Sell all held tokens
                    sig = await jupiter_swap(
                        session, self.keypair, client,
                        input_mint=mint,
                        output_mint=SOL_MINT,
                        amount_lamports=int(balance),
                        slippage_bps=Config.SLIPPAGE_BPS
                    )
                    if sig:
                        pos = self.positions.pop(mint, {})
                        self.trades_today += 1
                        log(f"✅ SELL copied! {symbol} | TX: {sig[:20]}...", RED)
                else:
                    log(f"No {symbol} balance to sell", YELLOW)

            await client.close()

    async def get_token_balance(self, client: AsyncClient, mint: str) -> int:
        """Get raw token balance (in smallest units) for a mint."""
        try:
            mint_pubkey = Pubkey.from_string(mint)
            owner_pubkey = self.keypair.pubkey()
            resp = await client.get_token_accounts_by_owner(
                owner_pubkey,
                {"mint": mint_pubkey},
                encoding="jsonParsed"
            )
            accounts = resp.value
            if accounts:
                amount = accounts[0].account.data.parsed["info"]["tokenAmount"]["amount"]
                return int(amount)
        except Exception as e:
            log(f"Balance check error: {e}", YELLOW)
        return 0

    async def check_stop_loss_take_profit(self):
        """Periodically check open positions for SL/TP."""
        while True:
            for mint, pos in list(self.positions.items()):
                try:
                    async with aiohttp.ClientSession() as session:
                        info = await get_token_info(session, mint)
                    price = float(info["price"] or 0)
                    if price <= 0:
                        continue

                    # Estimate current value (rough)
                    cost = pos["cost_sol"]
                    # We'd need entry price for exact PnL — simplified here
                    hold_time = time.time() - pos["buy_time"]

                    # Auto-sell after max hold time
                    if hold_time > Config.MAX_HOLD_SECONDS:
                        log(f"⏰ Max hold time reached for {pos['symbol']} — selling", YELLOW)
                        await self.force_sell(mint, pos["symbol"])

                except Exception as e:
                    log(f"SL/TP check error: {e}", YELLOW)

            await asyncio.sleep(30)

    async def force_sell(self, mint: str, symbol: str):
        async with aiohttp.ClientSession() as session:
            client = AsyncClient(Config.SOLANA_RPC_URL)
            balance = await self.get_token_balance(client, mint)
            if balance > 0:
                sig = await jupiter_swap(
                    session, self.keypair, client,
                    input_mint=mint,
                    output_mint=SOL_MINT,
                    amount_lamports=balance,
                    slippage_bps=Config.SLIPPAGE_BPS
                )
                if sig:
                    self.positions.pop(mint, None)
                    log(f"🔴 Force sold {symbol}", RED)
            await client.close()

    def print_stats(self):
        log("─" * 50, CYAN)
        log(f"  Wallet:       {self.my_wallet}", CYAN)
        log(f"  Trades today: {self.trades_today}", CYAN)
        log(f"  Open positions: {len(self.positions)}", CYAN)
        for mint, pos in self.positions.items():
            log(f"    • {pos['symbol']} — cost {pos['cost_sol']} SOL", CYAN)
        log("─" * 50, CYAN)

    async def run(self):
        log(f"{BOLD}{'='*50}", CYAN)
        log(f"  SOLANA MEMECOIN COPY BOT STARTED", CYAN)
        log(f"{'='*50}{RESET}", CYAN)
        log(f"  Tracking {len(Config.TRACKED_WALLETS)} wallet(s)", GREEN)
        log(f"  Trade size: {Config.TRADE_AMOUNT_SOL} SOL per copy", GREEN)
        log(f"  Slippage: {Config.SLIPPAGE_BPS/100}%", GREEN)
        log(f"  Max trades/day: {Config.MAX_TRADES_PER_DAY}", GREEN)
        self.print_stats()

        async with aiohttp.ClientSession() as session:
            client = AsyncClient(Config.SOLANA_RPC_URL)

            monitors = [
                WalletMonitor(addr, label)
                for addr, label in Config.TRACKED_WALLETS.items()
            ]

            tasks = [
                m.poll(session, client, self.on_trade_detected)
                for m in monitors
            ]
            tasks.append(self.check_stop_loss_take_profit())

            await asyncio.gather(*tasks)


# ─────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    bot = CopyTradingBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        log("Bot stopped by user", YELLOW)
