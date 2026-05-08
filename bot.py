"""
====================================================
  SOLANA MEMECOIN COPY TRADING BOT - FIXED VERSION
====================================================
"""

import asyncio
import aiohttp
import base64
import time
import os
from datetime import datetime

GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
RESET  = "\033[0m"

def log(msg, color=RESET):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{color}[{ts}] {msg}{RESET}", flush=True)

# ─── CONFIG (reads from Railway environment variables) ───
class Config:
    MY_PRIVATE_KEY     = os.environ.get("MY_PRIVATE_KEY", "").strip()
    SOLANA_RPC_URL     = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
    TRADE_AMOUNT_SOL   = float(os.environ.get("TRADE_AMOUNT_SOL", "0.05"))
    MAX_TRADES_PER_DAY = int(os.environ.get("MAX_TRADES_PER_DAY", "10"))
    SLIPPAGE_BPS       = int(os.environ.get("SLIPPAGE_BPS", "300"))
    POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "3"))
    PRIORITY_FEE       = int(os.environ.get("PRIORITY_FEE_LAMPORTS", "100000"))

    _raw = os.environ.get("TRACKED_WALLETS", "")
    TRACKED_WALLETS = {}
    for _entry in _raw.split(","):
        _entry = _entry.strip()
        if ":" in _entry:
            _a, _n = _entry.split(":", 1)
            TRACKED_WALLETS[_a.strip()] = _n.strip()
        elif _entry:
            TRACKED_WALLETS[_entry] = "Trader"

SOL_MINT  = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

def validate_config():
    if not Config.MY_PRIVATE_KEY:
        log("ERROR: MY_PRIVATE_KEY not set!", RED)
        return False
    if len(Config.MY_PRIVATE_KEY) < 40:
        log("ERROR: MY_PRIVATE_KEY too short — check Railway Variables!", RED)
        return False
    if not Config.TRACKED_WALLETS:
        log("WARNING: No TRACKED_WALLETS set!", YELLOW)
    log(f"Config OK — Tracking {len(Config.TRACKED_WALLETS)} wallet(s)", GREEN)
    return True

# ─── TOKEN INFO ───
async def get_token_info(session, mint):
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as r:
            data = await r.json()
            pairs = data.get("pairs", [])
            if pairs:
                p = pairs[0]
                return {
                    "symbol": p.get("baseToken", {}).get("symbol", "???"),
                    "price":  p.get("priceUsd", "N/A"),
                }
    except:
        pass
    return {"symbol": "???", "price": "N/A"}

# ─── JUPITER SWAP ───
async def jupiter_swap(session, input_mint, output_mint, amount_lamports):
    try:
        from solders.keypair import Keypair
        from solders.transaction import VersionedTransaction
        from solana.rpc.async_api import AsyncClient
        from solana.rpc.commitment import Confirmed

        key_str = Config.MY_PRIVATE_KEY.strip()
        keypair = Keypair.from_base58_string(key_str)
        client  = AsyncClient(Config.SOLANA_RPC_URL)

        quote_url = (
            f"https://quote-api.jup.ag/v6/quote"
            f"?inputMint={input_mint}&outputMint={output_mint}"
            f"&amount={amount_lamports}&slippageBps={Config.SLIPPAGE_BPS}"
        )
        async with session.get(quote_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                log(f"Quote failed: {r.status}", RED)
                return None
            quote = await r.json()

        payload = {
            "quoteResponse":    quote,
            "userPublicKey":    str(keypair.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": Config.PRIORITY_FEE,
        }
        async with session.post(
            "https://quote-api.jup.ag/v6/swap",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10)
        ) as r:
            if r.status != 200:
                log(f"Swap failed: {r.status}", RED)
                return None
            swap_data = await r.json()

        raw_tx = base64.b64decode(swap_data["swapTransaction"])
        tx     = VersionedTransaction.from_bytes(raw_tx)
        resp   = await client.send_raw_transaction(bytes(tx))
        sig    = str(resp.value)
        await client.close()
        return sig

    except Exception as e:
        log(f"Swap error: {e}", RED)
        return None

# ─── DETECT TRADE ───
async def detect_trade(session, sig, tracked_wallet):
    try:
        from solana.rpc.async_api import AsyncClient
        from solders.pubkey import Pubkey
        from solana.rpc.commitment import Confirmed

        client = AsyncClient(Config.SOLANA_RPC_URL)
        tx = await client.get_transaction(
            sig,
            encoding="jsonParsed",
            commitment=Confirmed,
            max_supported_transaction_version=0
        )
        await client.close()

        if not tx.value:
            return None
        meta = tx.value.transaction.meta
        if not meta or meta.err:
            return None

        pre_map  = {b.account_index: b for b in (meta.pre_token_balances  or [])}
        post_map = {b.account_index: b for b in (meta.post_token_balances or [])}

        for idx in set(pre_map) | set(post_map):
            post  = post_map.get(idx)
            if not post:
                continue
            mint  = post.mint
            owner = str(post.owner) if post.owner else ""
            if owner != tracked_wallet or mint in (SOL_MINT, USDC_MINT):
                continue

            pre      = pre_map.get(idx)
            pre_amt  = float(pre.ui_token_amount.ui_amount  or 0) if pre else 0
            post_amt = float(post.ui_token_amount.ui_amount or 0)
            diff     = post_amt - pre_amt

            if abs(diff) < 0.0001:
                continue

            info = await get_token_info(session, mint)
            return {
                "action": "BUY" if diff > 0 else "SELL",
                "mint":   mint,
                "symbol": info["symbol"],
                "wallet": tracked_wallet,
            }
    except Exception as e:
        log(f"Parse error: {e}", YELLOW)
    return None

# ─── WALLET MONITOR ───
class WalletMonitor:
    def __init__(self, wallet, label):
        self.wallet = wallet
        self.label  = label
        self.seen   = set()

    async def get_sigs(self):
        try:
            from solana.rpc.async_api import AsyncClient
            from solders.pubkey import Pubkey
            from solana.rpc.commitment import Confirmed
            client = AsyncClient(Config.SOLANA_RPC_URL)
            pubkey = Pubkey.from_string(self.wallet)
            resp   = await client.get_signatures_for_address(pubkey, limit=10, commitment=Confirmed)
            await client.close()
            return [str(s.signature) for s in resp.value]
        except Exception as e:
            log(f"Sig error [{self.label}]: {e}", YELLOW)
            return []

    async def poll(self, session, on_trade):
        log(f"👀 Watching {self.label} ({self.wallet[:8]}...)", CYAN)
        while True:
            try:
                for sig in await self.get_sigs():
                    if sig in self.seen:
                        continue
                    self.seen.add(sig)
                    trade = await detect_trade(session, sig, self.wallet)
                    if trade:
                        trade["trader_label"] = self.label
                        await on_trade(trade)
                if len(self.seen) > 200:
                    self.seen = set(list(self.seen)[-100:])
            except Exception as e:
                log(f"Poll error [{self.label}]: {e}", YELLOW)
            await asyncio.sleep(Config.POLL_INTERVAL)

# ─── COPY BOT ───
class CopyBot:
    def __init__(self):
        self.trades_today = 0
        self.positions    = {}

    async def on_trade(self, trade):
        action = trade["action"]
        symbol = trade["symbol"]
        label  = trade["trader_label"]
        mint   = trade["mint"]

        if action == "BUY":
            log(f"📡 {label} BOUGHT {symbol}", GREEN)
            if self.trades_today >= Config.MAX_TRADES_PER_DAY:
                log("Max trades reached today", YELLOW)
                return
            lamports = int(Config.TRADE_AMOUNT_SOL * 1e9)
            log(f"🚀 Copying BUY {symbol} — {Config.TRADE_AMOUNT_SOL} SOL", GREEN)
            async with aiohttp.ClientSession() as s:
                sig = await jupiter_swap(s, SOL_MINT, mint, lamports)
            if sig:
                self.positions[mint] = {"symbol": symbol, "time": time.time()}
                self.trades_today += 1
                log(f"✅ BUY copied! TX: {sig[:20]}...", GREEN)
                log(f"🔗 solscan.io/tx/{sig}", CYAN)

        elif action == "SELL":
            log(f"📡 {label} SOLD {symbol}", RED)
            if mint in self.positions:
                log(f"🔴 Copying SELL {symbol}", RED)
                async with aiohttp.ClientSession() as s:
                    sig = await jupiter_swap(s, mint, SOL_MINT, 1000000)
                if sig:
                    self.positions.pop(mint, None)
                    self.trades_today += 1
                    log(f"✅ SELL copied! TX: {sig[:20]}...", RED)

    async def run(self):
        log("=" * 45, CYAN)
        log("  SOLANA COPY BOT — STARTED", CYAN)
        log("=" * 45, CYAN)
        log(f"RPC      : {Config.SOLANA_RPC_URL}", GREEN)
        log(f"Trade    : {Config.TRADE_AMOUNT_SOL} SOL per copy", GREEN)
        log(f"Wallets  : {len(Config.TRACKED_WALLETS)}", GREEN)
        for addr, name in Config.TRACKED_WALLETS.items():
            log(f"  • {name}: {addr[:12]}...", CYAN)

        async with aiohttp.ClientSession() as session:
            monitors = [WalletMonitor(a, n) for a, n in Config.TRACKED_WALLETS.items()]
            await asyncio.gather(*[m.poll(session, self.on_trade) for m in monitors])

if __name__ == "__main__":
    if not validate_config():
        exit(1)
    try:
        asyncio.run(CopyBot().run())
    except KeyboardInterrupt:
        log("Bot stopped!", YELLOW)
  
