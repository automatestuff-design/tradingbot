"""Settings: environment for connection/safety, runtime-editable dict for strategy & risk."""
import os
from dataclasses import dataclass, field, fields, asdict
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass
class Connection:
    mode: str = os.getenv("MODE", "sim").lower()  # sim | paper | live
    broker: str = os.getenv("BROKER", "ibkr").lower()  # ibkr | alpaca (used when MODE is paper/live)
    ib_host: str = os.getenv("IB_HOST", "127.0.0.1")
    ib_port: int = int(os.getenv("IB_PORT", "0") or 0)  # 0 = IB Gateway default for the mode
    ib_client_id: int = int(os.getenv("IB_CLIENT_ID", "17"))
    ib_account: str = os.getenv("IB_ACCOUNT", "")
    alpaca_key: str = os.getenv("ALPACA_API_KEY", "")
    alpaca_secret: str = os.getenv("ALPACA_API_SECRET", "")
    data_feed: str = os.getenv("ALPACA_DATA_FEED", "iex")  # iex (free) | sip (paid)
    live_ack: bool = os.getenv("I_UNDERSTAND_LIVE_TRADING", "").lower() == "yes"

    def __post_init__(self):
        if not self.ib_port:
            self.ib_port = 4001 if self.mode == "live" else 4002

    def validate(self) -> None:
        if self.mode not in ("sim", "paper", "live"):
            raise SystemExit(f"MODE must be sim, paper or live (got {self.mode!r})")
        if self.broker not in ("ibkr", "alpaca"):
            raise SystemExit(f"BROKER must be ibkr or alpaca (got {self.broker!r})")
        if self.mode in ("paper", "live") and self.broker == "alpaca" and not (self.alpaca_key and self.alpaca_secret):
            raise SystemExit("BROKER=alpaca needs ALPACA_API_KEY and ALPACA_API_SECRET in .env")
        if self.mode == "live" and self.broker == "ibkr" and self.ib_port in (4002, 7497):
            raise SystemExit(f"MODE=live but IB_PORT={self.ib_port} is a paper-trading port.")
        if self.mode == "live" and not self.live_ack:
            raise SystemExit(
                "Refusing to start in LIVE mode. Set I_UNDERSTAND_LIVE_TRADING=yes in .env "
                "only after you have tested thoroughly in sim and paper modes."
            )


@dataclass
class Settings:
    symbols: list = field(default_factory=lambda: ["RY.TO", "TD.TO", "SHOP.TO", "ENB.TO", "CNQ.TO", "XIU.TO"])
    strategy: str = "sma_cross"          # sma_cross | rsi_reversion | breakout
    fast: int = 9                        # sma_cross fast period
    slow: int = 21                       # sma_cross slow period
    rsi_period: int = 14
    rsi_low: float = 30.0
    rsi_high: float = 70.0
    breakout_lookback: int = 20
    position_pct: float = 10.0           # % of equity per new position
    max_positions: int = 4
    stop_loss_pct: float = 1.5           # exit if down this % from entry
    take_profit_pct: float = 3.0         # exit if up this % from entry
    cooldown_bars: int = 10              # no re-entry into a symbol for this many bars after exiting
    max_daily_loss_pct: float = 3.0      # flatten + halt if equity drops this % on the day
    tick_seconds: float = float(os.getenv("TICK_SECONDS", "0") or 0)  # 0 = mode default
    # --- trade approvals (live/paper/sim only; the backtester never asks) ---
    require_approval: bool = True        # new buys wait for Approve/Reject on the dashboard
    approve_exits: bool = False          # also ask before strategy SELL signals (stops never wait)
    approval_timeout_s: int = 90         # unanswered requests expire after this many seconds
    max_drift_pct: float = 0.5           # refuse an approved buy if the price rose more than this since the request
    ai_summary: bool = True              # add a Claude summary to requests when ANTHROPIC_API_KEY is set

    def to_dict(self) -> dict:
        return asdict(self)

    def update(self, data: dict) -> None:
        types = {f.name: f.type for f in fields(self)}  # cast by declared type, not the current value's type
        for k, v in data.items():
            if k not in types:
                continue
            if k == "symbols":
                if isinstance(v, str):
                    v = v.split(",")
                v = [s.strip().upper() for s in v if s.strip()]
            elif types[k] is bool:
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
            elif types[k] is int:
                v = int(float(v))
            elif types[k] is float:
                v = float(v)
            else:
                v = str(v)
            setattr(self, k, v)
        if self.fast >= self.slow:
            raise ValueError("fast period must be smaller than slow period")
        if self.approval_timeout_s < 10:
            raise ValueError("approval timeout must be at least 10 seconds")
        if not (0 < self.position_pct <= 100):
            raise ValueError("position_pct must be between 0 and 100")
