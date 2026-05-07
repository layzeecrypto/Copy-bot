"""
====================================================
  CONFIG — Edit this file before running the bot
====================================================
"""

class Config:

    # ── YOUR WALLET ───────────────────────────────
    # Paste your Solana wallet private key (base58)
    # NEVER share this with anyone!
    MY_PRIVATE_KEY = "PASTE_YOUR_PRIVATE_KEY_HERE"

    # ── SOLANA RPC ────────────────────────────────
    # Free: https://api.mainnet-beta.solana.com
    # Faster (recommended): Get free key from https://helius.dev
    SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"

    # ── TRADERS TO COPY ───────────────────────────
    # Add wallet addresses of traders you want to copy
    # Format: "wallet_address": "nickname"
    TRACKED_WALLETS = {
        "TRADER_WALLET_ADDRESS_1": "CryptoWhale",
        "TRADER_WALLET_ADDRESS_2": "MemeKing",
        # Add more wallets here...
    }

    # ── TRADE SETTINGS ────────────────────────────
    # How much SOL to spend per copied trade
    TRADE_AMOUNT_SOL = 0.05          # 0.05 SOL per trade (~$7-10)

    # Max trades to copy per day (safety limit)
    MAX_TRADES_PER_DAY = 10

    # Slippage tolerance in basis points (300 = 3%)
    SLIPPAGE_BPS = 300

    # How often to check wallet for new trades (seconds)
    POLL_INTERVAL = 3

    # Priority fee for faster transactions (lamports)
    PRIORITY_FEE_LAMPORTS = 100_000  # ~0.0001 SOL

    # ── RISK MANAGEMENT ───────────────────────────
    # Auto-sell after this many seconds if not sold
    MAX_HOLD_SECONDS = 3600          # 1 hour max hold

    # Stop loss % (future feature)
    STOP_LOSS_PCT = 15

    # Take profit % (future feature)
    TAKE_PROFIT_PCT = 50
