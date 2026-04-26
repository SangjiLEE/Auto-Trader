# Auto Trader

🌐 **Language**: [한국어](README.md) · **English** · [日本語](README.ja.md)

> **A multi-strategy automated trading system, built solo in 8 days.**
> Python + Claude Code (AI pair programming) + KIS Open API.

Trades Korean and US stocks automatically on weekdays. Classifies the market regime (BULL / RANGE / BEAR) every day and applies different rules for each. Splits orders at Fear & Greed extremes. Every trade is reported in real time via Telegram.

**Currently running on a paper account** (3–6 months of validation before switching to real money).

---

## What I built

```
┌─────────────────────────────────────────────────────────┐
│  KIS brokerage account (paper, ₩50,000,000)             │
├─────────────────────────────────────────────────────────┤
│  ┌─ DM slot 70%  ── Monthly Dual Momentum                │
│  │   1 of {069500 / 133690 / 360750 / 148070}            │
│  │                                                       │
│  ├─ v3 KR slot 15% ── Daily regime-adaptive swing        │
│  │   069500, 005930, 035420                              │
│  │                                                       │
│  └─ v3 US slot 15% ── Daily regime-adaptive swing (US)   │
│      AAPL, NVDA, TSLA                                    │
│                                                          │
│  + Fear & Greed extreme split orders (≤7 buy / ≥92 sell) │
│  + Daily snapshot + US closing report                    │
│  + Real-time Telegram alerts (5–7 per weekday)           │
└─────────────────────────────────────────────────────────┘
```

**Five macOS launchd jobs run automatically on weekdays.** All the user has to do is read Telegram.

---

## Why I built it

### Initial goal
> "Cover my AI subscription fees (~₩300K / month) with automated trading profits."

### Real goal (revised after validation)
> "**A personal income pipeline that systematically reduces the chance of losing money, while gradually improving returns.**"

What the data taught me in 8 days:
1. **₩300K/month from ₩1M is mathematically impossible** (requires 30% monthly return).
2. **No strategy beats simple Buy & Hold in absolute terms** in long-term strong-trend markets.
3. **But MDD (max drawdown) can be cut to less than half**.
4. → "Losing well" is more realistic than "winning more."

### Conclusion
- At a small capital base, treat this as **learning + infrastructure cost**.
- Re-evaluate **after 6–12 months of paper validation**.
- A well-built system can be **applied immediately when capital grows in 3 years**.

---

## Tech stack

| Area | Tool |
|------|------|
| **Language** | Python 3.12 |
| **Brokerage API** | Korea Investment & Securities (KIS) Open API (REST + OAuth2 + hashkey) |
| **Market data** | FinanceDataReader (FDR), Yahoo Finance |
| **Database** | SQLite |
| **Analytics** | pandas, numpy |
| **Notifications** | Telegram Bot API |
| **Fear & Greed** | Alternative.me F&G API |
| **Scheduling** | macOS launchd (5 plists) |
| **Dev environment** | Claude Code (AI pair programming), VS Code, zsh |
| **System** | macOS (Apple Silicon) |

**Deliberately avoided:**
- External backtest libraries (`backtrader`, `vectorbt`) — writing the rules myself was better for learning.
- Intraday minute bars — daily bars provided enough validation.
- Docker / cloud — local launchd is simpler.

---

## How I built it

### 8-day evolution (deliberately staged)

| Day | Milestone | Theme |
|-----|-----------|-------|
| 1 (Sun) | Open KIS account + issue API keys + set up environment | Infrastructure |
| 2 (Mon) | OAuth2 auth, Hello World, balance / quote queries | API basics |
| 3 (Tue) | DM backtest + **3 robustness checks** (parameters, OOS, yearly) | Data validation |
| 4 (Wed) | **First paper trade** (069500, 508 shares) + Telegram integration | First live trade |
| 5 (Thu) | Daily auto report + US market support | Automation |
| 6 (Fri) | Multi-symbol backtests + Enhanced Swing v3 (regime-adaptive) | Strategy evolution |
| 7 (Sat) | F&G integration + v4 (extreme split orders) | Sentiment signal |
| 8 (Sun) | GitHub push + documentation | Cleanup |

Every stage went through a 4-step gate: **backtest → robustness check → dry-run → live order**. Strategies that didn't pass (intraday scalping, RSI/BB mean reversion) were dropped — the data taught the lesson.

Full evolution log: [docs/JOURNEY.md](docs/JOURNEY.md)

### Development style: Claude Code + AI pair programming

**Pair-programmed with Claude (Anthropic).**

- **User**: intent, domain knowledge, decisions, validation
- **Claude**: code, debugging, backtest design, tests
- **Loop**: every piece of code is run by the user, results reviewed, next decision taken

This loop produced ~7,000 lines of code across 30+ modules in 8 days. **AI writes code; the human owns decisions and validation.**

### Harness Engineering

The core of this system is **repeatable validation infrastructure** (= the "harness").

Full detail: [docs/HARNESS.md](docs/HARNESS.md)

Summary:
1. **5 backtest harnesses** — same shape, so new strategies are quick to validate.
2. **Robustness harness** — automatic parameter sweep, IS/OOS split, year-by-year breakdown.
3. **Strategy plugin architecture** — swap strategy modules to try different rules.
4. **Regime classification harness** — single source of truth for BULL / RANGE / BEAR.
5. **Dry-run ↔ execute toggle** — one `--execute` flag separates safe and live mode.
6. **State reconstruction harness** — position, regime, and partial-exit state all rebuilt from the DB `trades` table.
7. **5 distributed launchd jobs** — one job failing doesn't take down the others.

Thanks to this harness, **5 large strategy redesigns** (slow → fast → v3 → v4 → relaxed v4) fit into 8 days.

---

## Key results

### System validation (backtest)

| Strategy | Symbol / universe | Period | Cumulative | Sharpe | MDD | vs BH |
|----------|------------------|--------|-----------|--------|-----|------|
| DM | 4 assets | 5.7y | +225% | **1.10** | -22.6% | -10pp (≈ tied) |
| v3 | NVDA | 5y | +64% | 0.67 | -17% | half the MDD of BH |
| v3 | TSLA | 5y | +55% | 0.68 | -18% | -30pp (close behind) |
| **v4** (v3+F&G) | **069500** | 10y | **+29%** | – | -19% | v3 **+9.7pp** |

**MDD cut by more than half vs. BH** — that's the real value of the system.

### Live operation (currently running)

- **5 macOS launchd jobs** — automatic on weekdays.
- **2026-04-22**: first paper fill (069500 × 508).
- **2026-04-26**: v4 (with F&G) deployed — Auto Trader 2.0.
- **First full automated cycle scheduled for 2026-05-04**.

---

## Detailed docs

Each document exists for a different reason — here's how they differ:

| File | One-liner | Audience | How it's different |
|------|-----------|----------|--------------------|
| **README** (.md / .en.md / .ja.md) | Project showcase | A visitor scanning the repo for 3–5 min | At-a-glance "what / why / how". No depth |
| [docs/STRATEGIES.md](docs/STRATEGIES.md) | **Trading rule reference** | Anyone who wants to understand or extend the rules | The "**what** to buy/sell" — entry/exit conditions, regime-specific rules, F&G formulas |
| [docs/HARNESS.md](docs/HARNESS.md) | **Engineering deep dive** | A developer learning from the code | The "**how** it was built" — 12 reusable patterns (backtest harness, plugins, toggles, state reconstruction) |
| [docs/SETUP.md](docs/SETUP.md) | **Installation manual** | Someone who wants to actually run it | Step-by-step from "install Python" to "5 launchd jobs registered" |
| [docs/SCHEDULES.md](docs/SCHEDULES.md) | **Operations runbook** | The day-to-day operator | When each job runs, sample Telegram messages, holiday handling |
| [docs/BACKTESTS.md](docs/BACKTESTS.md) | **Evidence archive** | Someone checking the credibility of claims | Every backtest result that informed a decision (including dropped strategies). Proof in numbers |
| [docs/JOURNEY.md](docs/JOURNEY.md) | **8-day diary** | Someone interested in the process | Chronological story — attempts, failures, lessons, next decisions |
| [DESIGN_DOC.md](DESIGN_DOC.md) | **Day-1 original blueprint** | Someone curious about the initial intent | The 8 trading-style comparison written on day 1. Preserved (deliberately not updated) |
| [CLAUDE.md](CLAUDE.md) | **AI workflow context** | The next Claude Code session (machine reader) | AI behavior rules ("never commit .env") + code patterns. Humans can read it too |

> **README vs other docs**: README is the entrance ("why is this interesting"). The docs/ folder is the deep manual ("how it actually works").
> **STRATEGIES vs HARNESS**: STRATEGIES = trading logic (domain). HARNESS = the infrastructure that validates that logic (engineering).
> **SETUP vs SCHEDULES**: SETUP = first-time install (one-shot). SCHEDULES = daily operation (recurring).
> **BACKTESTS vs JOURNEY**: BACKTESTS = the result numbers (static). JOURNEY = the decision process (chronological).
> **DESIGN_DOC vs README**: DESIGN_DOC = the day-1 starting point (frozen). README = the day-8 endpoint (current state).

> Note: documentation files in `docs/` are written in Korean. The README is provided in three languages.

---

## Quick start

```bash
git clone https://github.com/SangjiLEE/Auto-Trader.git
cd Auto-Trader
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env  # fill in KIS API keys, Telegram token
nano .env

python -m src.hello_world         # KIS auth check
python -m src.daily_swing_v3_kr   # KR swing dry-run
python -m src.swing_backtest_v4   # backtest
```

Full setup: [docs/SETUP.md](docs/SETUP.md)

---

## Disclaimer

This project is for **personal learning and experimentation**.

- **Use at your own risk in live trading.**
- **Backtest results do not guarantee future performance.**
- **Never share API keys or account numbers** — confirm `.env` is in `.gitignore`.
- **Validate on the paper account (`KIS_ENV=paper`) for at least 3–6 months** before going live.

More on risks: [docs/SETUP.md#디스클레이머](docs/SETUP.md#디스클레이머)

---

## License

MIT — free to use, modify, and redistribute. **You are responsible for how you use it.**

---

## Author

[**SangjiLEE**](https://github.com/SangjiLEE) — Spring 2026, 8 days.

> One step a day. Code that hasn't passed a backtest doesn't get deployed.
> Pair programmed with Claude Code; decisions and validation owned by the human.
