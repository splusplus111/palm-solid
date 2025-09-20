# config.py
import os, json
from typing import List, Optional

# ---------- Load .env (robust) ----------
def _load_env():
    """
    Load environment variables from a .env file in the current working directory.
    Uses python-dotenv when available; otherwise falls back to a tiny parser.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()  # loads .env from CWD by default
        return
    except Exception:
        pass  # fall back below

    # Minimal fallback loader
    env_path = os.path.join(os.getcwd(), ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    if "=" not in s:
                        continue
                    k, v = s.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    # strip surrounding quotes if present
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    os.environ.setdefault(k, v)
        except Exception:
            # ignore; env may already be set by the shell
            pass

_load_env()

# ---------- Helpers ----------
def _getenv_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip().lower()
    return val in ("1", "true", "t", "yes", "y", "on")

def _getenv_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def _getenv_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default

def _getenv_list(name: str) -> List[str]:
    raw = os.getenv(name, "")
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]

def _parse_key_from_env() -> str:
    """
    Returns a JSON array string for a Solana keypair.
    Accepts either WALLET_PRIVATE_KEY_JSON or legacy WALLET_SECRET_KEY as CSV/space ints.
    """
    kp_json = os.getenv("WALLET_PRIVATE_KEY_JSON", "").strip()
    if kp_json:
        # Allow either a JSON array string or a path to a file
        if kp_json.startswith("["):
            return kp_json
        try:
            with open(kp_json, "r", encoding="utf-8") as f:
                txt = f.read().strip()
                if txt.startswith("["):
                    return txt
        except Exception:
            pass

    legacy = os.getenv("WALLET_SECRET_KEY", "").strip()
    if legacy:
        # Accept comma- or space-separated ints
        parts = [p for p in legacy.replace(",", " ").split() if p]
        try:
            arr = [int(p) for p in parts]
            return json.dumps(arr)
        except Exception:
            raise ValueError("WALLET_SECRET_KEY must be a list of ints separated by comma/space.")

    raise EnvironmentError(
        "Set WALLET_PRIVATE_KEY_JSON to the JSON array (or file path), or provide WALLET_SECRET_KEY."
    )

# ---------- RPC / WS ----------
SOLANA_RPC_URL: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
SOLANA_WS_URL: str = os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com/").strip()

# ---------- Programs / Mints ----------
PUMPFUN_PROGRAM_ID: str = os.getenv(
    "PUMPFUN_PROGRAM_ID",
    "DezXAZ8z7PfnVsKXcE4cYGP33aDDoa5zQPKcTgUX5bC9",  # default Pump.fun mainnet program
).strip()
# Program IDs can be supplied explicitly via WATCH_PROGRAM_IDS, else fall back to individual envs
_default_watch = [pid for pid in [
    os.getenv("RAYDIUM_AMM_V4", "").strip(),
    os.getenv("RAYDIUM_CLMM", "").strip(),
    os.getenv("RAYDIUM_CPMM", "").strip(),
    PUMPFUN_PROGRAM_ID,  # use the exported constant
] if pid]
WATCH_PROGRAM_IDS: List[str] = _getenv_list("WATCH_PROGRAM_IDS") or _default_watch

# Optional: force scanning/trading only this mint (used by pool_watcher override)
FORCE_TOKEN_MINT: Optional[str] = os.getenv("FORCE_TOKEN_MINT", "").strip() or None

# Common mints (canonical USDC by default)
WSOL_MINT: str = os.getenv("WSOL_MINT", "So11111111111111111111111111111111111111112").strip()
USDC_MINT: str = os.getenv(
    "USDC_MINT",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
).strip()
SOL_MINT: str = WSOL_MINT  # alias used by some modules

# Core program IDs (with sane defaults; override via .env if needed)
ASSOCIATED_TOKEN_PROGRAM_ID: str = os.getenv(
    "ASSOCIATED_TOKEN_PROGRAM_ID",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
).strip()
TOKEN_PROGRAM_ID: str = os.getenv(
    "TOKEN_PROGRAM_ID",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
).strip()
TOKEN_2022_PROGRAM_ID: Optional[str] = os.getenv("TOKEN_2022_PROGRAM_ID", "").strip() or None

# System & sysvars & compute budget
SYS_PROGRAM_ID: str = os.getenv(
    "SYS_PROGRAM_ID",
    "11111111111111111111111111111111",
).strip()
COMPUTE_BUDGET_PROGRAM_ID: str = os.getenv(
    "COMPUTE_BUDGET_PROGRAM_ID",
    "ComputeBudget111111111111111111111111111111",
).strip()
RENT_SYSVAR_ID: str = os.getenv(
    "RENT_SYSVAR_ID",
    "SysvarRent111111111111111111111111111111111",
).strip()

# ---------- Trading (core knobs) ----------
BUY_USD: float = _getenv_float("BUY_USD", 10.0)

ENTRY_MAX_AGE_SECONDS: float = _getenv_float("ENTRY_MAX_AGE_SECONDS", 60.0)
EXIT_AFTER_SECONDS: float  = _getenv_float("EXIT_AFTER_SECONDS", 5.0)

# SELL_PERCENT accepts either percent (e.g., 90, 99.5) or fraction (e.g., 0.9, 0.995)
_sp = _getenv_float("SELL_PERCENT", 99.5)  # default to 99.5% if unset
if _sp > 1.0:
    _sp = _sp / 100.0
SELL_PERCENT: float = max(0.0, min(1.0, _sp))

# Retry behaviour
SELL_RETRY_INTERVAL_SECS: float = _getenv_float("SELL_RETRY_INTERVAL_SECS", 0.6)
SELL_RETRY_MAX_TRIES: int = _getenv_int("SELL_RETRY_MAX_TRIES", 5)
# Derived offsets: [interval, 2*interval, ..., N*interval]
SELL_RETRY_OFFSETS: List[float] = [round(SELL_RETRY_INTERVAL_SECS * i, 3) for i in range(1, SELL_RETRY_MAX_TRIES + 1)]

MAX_BUYS_PER_SEC: float = _getenv_float("MAX_BUYS_PER_SEC", 0.1)

# Heuristics / filters
MIN_LIQUIDITY_USD: float = _getenv_float("MIN_LIQUIDITY_USD", 0.0)

# Slippage (basis points)
# Prefer SLIPPAGE_BPS if present for backward-compat
SLIPPAGE_BPS_DEFAULT: int = _getenv_int("SLIPPAGE_BPS", _getenv_int("SLIPPAGE_BPS_DEFAULT", 500))  # 5.00%
SLIPPAGE_BPS_ON_ERROR: int = _getenv_int("SLIPPAGE_BPS_ON_ERROR", 700)  # 7.00% when 0x1771/minOut

# Priority fees (lamports) for compute budget
# Support a single PRIORITY_FEE_LAMPORTS from .env, with optional side-specific overrides
_PF = _getenv_int("PRIORITY_FEE_LAMPORTS", 1_100_000)
PRIORITY_FEE_LAMPORTS_BUY: int  = _getenv_int("PRIORITY_FEE_LAMPORTS_BUY", _PF)
PRIORITY_FEE_LAMPORTS_SELL: int = _getenv_int("PRIORITY_FEE_LAMPORTS_SELL", PRIORITY_FEE_LAMPORTS_BUY)

# Also allow USD-based priority fee if your code uses it (default 0 = disabled)
PRIORITY_FEE_USD: float = _getenv_float("PRIORITY_FEE_USD", 0.0)

# Rent figure for SPL token accounts (approx; can vary)
ATA_RENT_LAMPORTS: int = _getenv_int("ATA_RENT_LAMPORTS", 2_039_280)

# WS stability
WS_PING_INTERVAL: int = _getenv_int("WS_PING_INTERVAL", 20)
WS_PING_TIMEOUT: int  = _getenv_int("WS_PING_TIMEOUT", 20)

# Logging
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper()

# ---------- Keypair ----------
WALLET_PRIVATE_KEY_JSON: str = _parse_key_from_env()

# ---------- Backward-compat aliases (keep old imports working) ----------
PRIORITY_FEE_LAMPORTS: int = PRIORITY_FEE_LAMPORTS_BUY
SLIPPAGE_BPS: int = SLIPPAGE_BPS_DEFAULT
WALLET_SECRET_KEY: str = WALLET_PRIVATE_KEY_JSON

# === ATA Janitor ===
CLOSE_ATA_ENABLED = os.getenv("CLOSE_ATA_ENABLED", "true").lower() == "true"
CLOSE_ATA_COOLDOWN_SECS = float(os.getenv("CLOSE_ATA_COOLDOWN_SECS", "172800"))   # 48h
CLOSE_ATA_INTERVAL_SECS = float(os.getenv("CLOSE_ATA_INTERVAL_SECS", "12"))       # one attempt ~12s
CLOSE_ATA_MAX_PER_MIN = int(os.getenv("CLOSE_ATA_MAX_PER_MIN", "5"))
CLOSE_ATA_MIN_SOL_RESERVE = float(os.getenv("CLOSE_ATA_MIN_SOL_RESERVE", "0.5"))
CLOSE_ATA_IDLE_WINDOW_SECS = float(os.getenv("CLOSE_ATA_IDLE_WINDOW_SECS", "20"))

# small optional tip for close txs (lamports); harmless if unused
CLOSE_ATA_TIP_LAMPORTS = int(os.getenv("CLOSE_ATA_TIP_LAMPORTS", "150000"))

# parse as comma-separated list; keeps single-mint default
CLOSE_ATA_EXCLUDE_MINTS = _getenv_list("CLOSE_ATA_EXCLUDE_MINTS") or [
    "So11111111111111111111111111111111111111112"
]

# --- NEW (bottom of config.py) ---
def _b(name, default="false"):
    import os
    return os.getenv(name, default).strip().lower() == "true"
def _f(name, default):
    import os
    return float(os.getenv(name, str(default)))
def _i(name, default):
    import os
    return int(os.getenv(name, str(default)))
def _s(name, default):
    import os
    return os.getenv(name, str(default)).strip()

STAIRS_MODE = _b("STAIRS_MODE", "false")

SPIKE_WINDOW_SEC  = _f("SPIKE_WINDOW_SEC", 60)
SPIKE_MIN_USD     = _f("SPIKE_MIN_USD", 1000)
SPIKE_REQUIRED    = _i("SPIKE_REQUIRED", 4)
SPIKE_GAP_MIN_MS  = _i("SPIKE_GAP_MIN_MS", 800)
SPIKE_GAP_MAX_MS  = _i("SPIKE_GAP_MAX_MS", 6000)
STEP_MAX_DROP_PCT = _f("STEP_MAX_DROP_PCT", 1.5)

SCALP_HOLD_SEC          = _f("SCALP_HOLD_SEC", 3.0)
SCALP_REENTER_UNTIL_LOSS= _b("SCALP_REENTER_UNTIL_LOSS", "true")
SCALP_COOLDOWN_SEC      = _f("SCALP_COOLDOWN_SEC", 1.0)
BLACKLIST_COOLDOWN_SEC  = _f("BLACKLIST_COOLDOWN_SEC", 120)
ENTRY_CLIP_USD          = _f("ENTRY_CLIP_USD", 10)

SLIPPAGE_BPS_BUY  = _i("SLIPPAGE_BPS_BUY", 9000)
SLIPPAGE_BPS_SELL = _i("SLIPPAGE_BPS_SELL", 800)

MAX_BUYS_PER_SEC = _f("MAX_BUYS_PER_SEC", 0.5)

MOON_BAG_ENABLED = _b("MOON_BAG_ENABLED", "false")
SELL_LADDER = [float(x) for x in _s("SELL_LADDER","0.6,0.25,0.15").split(",") if x.strip()]

DEV_MIRROR_ENABLED   = _b("DEV_MIRROR_ENABLED", "false")
HARD_EXIT_IF_DEV_SELL= _b("HARD_EXIT_IF_DEV_SELL","true")
DEV_SELL_SUPPLY_PCT  = _f("DEV_SELL_SUPPLY_PCT", 0.30)

# --- MCAP ladder/stops (NEW) ---
def _listfloats(env, default):
    s = os.getenv(env, default)
    return [float(x) for x in s.split(",") if x.strip()]

MCAP_TP_LEVELS      = _listfloats("MCAP_TP_LEVELS", "120000,130000,140000,150000")
MCAP_TP_FRACTIONS   = _listfloats("MCAP_TP_FRACTIONS","0.30,0.25,0.20,0.15")
MCAP_SELL_ALL_LEVEL = _f("MCAP_SELL_ALL_LEVEL", 160000)

MCAP_ARM_STOP_AFTER = _f("MCAP_ARM_STOP_AFTER", 115000)
MCAP_STOP_LOSS      = _f("MCAP_STOP_LOSS", 110000)
INSTANT_DROP_STOP_PCT = _f("INSTANT_DROP_STOP_PCT", 3.5)

TOKEN_TOTAL_SUPPLY  = int(_s("TOKEN_TOTAL_SUPPLY", "1000000000"))
TOKEN_DECIMALS      = _i("TOKEN_DECIMALS", 6)
MCAP_CHECK_INTERVAL_MS = _i("MCAP_CHECK_INTERVAL_MS", 250)

# --- keep everything above exactly as in your file ---

# --- MCAP ladder/stops (NEW) ---
def _listfloats(env, default):
    s = os.getenv(env, default)
    return [float(x) for x in s.split(",") if x.strip()]

MCAP_TP_LEVELS      = _listfloats("MCAP_TP_LEVELS", "120000,130000,140000,150000")
MCAP_TP_FRACTIONS   = _listfloats("MCAP_TP_FRACTIONS","0.30,0.25,0.20,0.15")
MCAP_SELL_ALL_LEVEL = _f("MCAP_SELL_ALL_LEVEL", 160000)

MCAP_ARM_STOP_AFTER = _f("MCAP_ARM_STOP_AFTER", 115000)
MCAP_STOP_LOSS      = _f("MCAP_STOP_LOSS", 110000)
INSTANT_DROP_STOP_PCT = _f("INSTANT_DROP_STOP_PCT", 3.5)

TOKEN_TOTAL_SUPPLY  = int(_s("TOKEN_TOTAL_SUPPLY", "1000000000"))
TOKEN_DECIMALS      = _i("TOKEN_DECIMALS", 6)
MCAP_CHECK_INTERVAL_MS = _i("MCAP_CHECK_INTERVAL_MS", 250)

# --- Bucket detector + re-entry gate (APPEND HERE) ---
SPIKE_USE_BUCKETS = _b("SPIKE_USE_BUCKETS", "false")
SPIKE_BUCKET_SECS = _i("SPIKE_BUCKET_SECS", 2)
# We still keep non-bucket SPIKE_MIN_USD/SPIKE_GAP_* above

# Cumulative fallback
PUMP_CUM_WINDOW_SEC = _f("PUMP_CUM_WINDOW_SEC", 12)
PUMP_CUM_MIN_USD    = _f("PUMP_CUM_MIN_USD", 65000)

# Re-entry gating
REENTER_NEEDS_NEXT_POP = _b("REENTER_NEEDS_NEXT_POP", "true")
REENTER_POP_TIMEOUT_MS = _i("REENTER_POP_TIMEOUT_MS", 6000)

# --- Concurrency ---
STAIRS_MAX_CONCURRENT = _i("STAIRS_MAX_CONCURRENT", 3)

# --- Dynamic moon-bag ladder (milestones every +$10k after $120k) ---
DYNAMIC_BAG_ENABLED = _b("DYNAMIC_BAG_ENABLED", "false")
DYNAMIC_BAG_START_USD = _f("DYNAMIC_BAG_START_USD", 120000)
DYNAMIC_BAG_STEP_USD  = _f("DYNAMIC_BAG_STEP_USD", 10000)
# clamp 0..1 for safety
_db_frac = _f("DYNAMIC_BAG_SELL_FRAC", 0.10)
DYNAMIC_BAG_SELL_FRAC = min(1.0, max(0.0, _db_frac))
DYNAMIC_BAG_MAX_USD   = _f("DYNAMIC_BAG_MAX_USD", 2_000_000)
DYNAMIC_BAG_IDLE_TIMEOUT_SEC = _f("DYNAMIC_BAG_IDLE_TIMEOUT_SEC", 10)
DYNAMIC_BAG_MAX_DURATION_SEC = _f("DYNAMIC_BAG_MAX_DURATION_SEC", 600)

# --- Jupiter request pacing ---
JUP_MAX_RPS            = _f("JUP_MAX_RPS", 6.0)
JUP_MAX_BURST          = _i("JUP_MAX_BURST", 6)
JUP_MAX_RETRIES        = _i("JUP_MAX_RETRIES", 5)
JUP_BACKOFF_BASE_MS    = _i("JUP_BACKOFF_BASE_MS", 200)

# --- MCAP polling / SOL price cache ---
MCAP_QUOTE_MIN_INTERVAL_MS = _i("MCAP_QUOTE_MIN_INTERVAL_MS", 750)
SOL_PRICE_TTL_SEC          = _f("SOL_PRICE_TTL_SEC", 15.0)
