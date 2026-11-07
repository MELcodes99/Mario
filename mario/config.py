"""Mario configuration.

Every number here comes from the MASTER TRADING DIRECTIVE v1.0 unless noted.
Override any value with an environment variable (or a line in .env) of the
same name in UPPER_CASE, e.g. MIN_LIQUIDITY_USD=75000.

Hard rule from the directive: the bot never loosens these at runtime.
Learning may re-weight scores, never security rules or risk limits.
"""
from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, field, fields
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ.setdefault(k, v)


@dataclass
class Settings:
    # ---- infrastructure -------------------------------------------------
    rpc_url: str = "https://api.mainnet-beta.solana.com"  # use Helius/Triton/QuickNode for real use
    jupiter_base: str = "https://api.jup.ag/swap/v2"
    jupiter_api_key: str = ""          # free key at developers.jup.ag/portal; keyless = 0.5 req/s
    jupiter_rps: float = 0.5
    dexscreener_base: str = "https://api.dexscreener.com"
    rugcheck_base: str = "https://api.rugcheck.xyz/v1"
    rugcheck_api_key: str = ""
    pumpportal_ws: str = "wss://pumpportal.fun/api/data"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_web_search: bool = True
    anthropic_web_search_tool: str = "web_search_20250305"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    data_dir: str = str(Path.home() / ".mario")
    wallet_path: str = ""               # default: <data_dir>/trading-wallet.json
    report_tz: str = "Africa/Lagos"

    # ---- loop timing (seconds) -----------------------------------------
    discovery_interval: int = 60
    evaluate_interval: int = 30
    position_interval: int = 10
    wallet_poll_interval: int = 60
    security_cache_ttl: int = 600
    ai_cache_ttl: int = 1800
    watch_max_tokens: int = 250

    # ---- A. mint / authority -------------------------------------------
    max_transfer_fee_bps: int = 0      # any Token-2022 transfer fee = reject (conservative default)

    # ---- B. liquidity ---------------------------------------------------
    min_liquidity_usd: float = 50_000
    min_exit_liquidity_usd: float = 25_000
    min_lp_locked_pct: float = 95.0
    max_liquidity_drop_pct_15m: float = 20.0
    liquidity_crosscheck_tolerance: float = 0.5   # DexScreener vs RugCheck may differ by <=50%

    # ---- C. holders -----------------------------------------------------
    max_top_holder_pct: float = 5.0
    max_top10_pct: float = 20.0
    max_deployer_pct: float = 5.0

    # ---- E. sellability -------------------------------------------------
    max_roundtrip_loss_pct: float = 6.0   # buy->sell quote round trip loss allowed (fees+impact)
    live_test_trade: bool = False         # tiny real buy+sell before a live entry
    live_test_trade_sol: float = 0.005

    # ---- F. manipulation ------------------------------------------------
    max_vol_to_liq_h1: float = 5.0
    max_buy_sell_ratio: float = 4.0
    min_avg_trade_usd: float = 15.0

    # ---- scoring / entry -------------------------------------------------
    min_score: float = 85
    scan_call_score: float = 85         # scan mode "calls" use the same bar by default
    min_pair_age_min: float = 15
    max_pair_age_h: float = 48
    min_reward_risk: float = 2.0
    anti_fomo_pct_15m: float = 35.0
    retest_wait_max_min: float = 90

    # ---- position sizing --------------------------------------------------
    risk_per_trade_pct: float = 0.25
    max_position_pct: float = 2.0
    max_exposure_pct: float = 6.0
    max_positions: int = 3

    # ---- portfolio limits -------------------------------------------------
    daily_loss_pct: float = 3.0
    weekly_loss_pct: float = 7.0
    max_drawdown_pct: float = 10.0
    consecutive_loss_pause: int = 3
    consecutive_loss_disable: int = 5
    pause_after_losses_min: int = 60

    # ---- exits --------------------------------------------------------------
    stop_loss_pct: float = 12.0
    tp_levels: list = field(default_factory=lambda: [15.0, 30.0, 50.0])
    tp_fraction: float = 0.25
    trailing_pct: float = 15.0
    trailing_tight_pct: float = 10.0
    breakeven_after_tp1: bool = True
    emergency_liq_drop_pct: float = 30.0
    max_quote_failures: int = 3
    authority_recheck_s: int = 300

    # ---- execution ---------------------------------------------------------
    max_slippage_pct_liquid: float = 1.5
    max_slippage_pct_volatile: float = 3.0
    max_price_impact_pct: float = 2.0
    exit_max_slippage_pct: float = 5.0          # normal exits (stops / take-profits)
    emergency_exit_max_slippage_pct: float = 15.0  # rug-in-progress exits: getting out beats price
    network_fee_sol: float = 0.0005      # per tx estimate (base + priority); used in paper & sizing
    max_extra_sol_transfer: float = 0.002  # tolerated top-level SOL transfer (tips) in a swap tx
    extra_allowed_programs: str = ""     # comma-separated program ids you have reviewed

    # ---- paper / live gating -------------------------------------------------
    paper_start_sol: float = 10.0
    min_paper_trades_for_live: int = 100  # directive suggests 500; set 0 only after you review

    def __post_init__(self):
        for f in fields(self):
            default = f.default_factory() if f.default_factory is not MISSING else f.default
            if getattr(self, f.name) != default:
                continue                      # explicit constructor argument wins over env
            env = os.environ.get(f.name.upper())
            if env is None or env == "":
                continue
            cur = getattr(self, f.name)
            if isinstance(cur, bool):
                setattr(self, f.name, env.lower() in ("1", "true", "yes", "on"))
            elif isinstance(cur, int):
                setattr(self, f.name, int(float(env)))
            elif isinstance(cur, float):
                setattr(self, f.name, float(env))
            elif isinstance(cur, list):
                setattr(self, f.name, [float(x) for x in env.split(",") if x.strip()])
            else:
                setattr(self, f.name, env)
        Path(self.data_dir).mkdir(parents=True, exist_ok=True)
        if not self.wallet_path:
            self.wallet_path = str(Path(self.data_dir) / "trading-wallet.json")
        if self.jupiter_api_key and self.jupiter_rps <= 0.5:
            self.jupiter_rps = 1.0

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / "mario.db")

    @property
    def control_path(self) -> str:
        return str(Path(self.data_dir) / "control.json")

    @property
    def reports_dir(self) -> Path:
        p = Path(self.data_dir) / "reports"
        p.mkdir(parents=True, exist_ok=True)
        return p


def load_settings() -> Settings:
    _load_dotenv(Path.cwd() / ".env")
    _load_dotenv(Path.home() / ".mario" / ".env")
    return Settings()
