# AutoBot — Autonomous Paper-Trading Desk (GitHub Actions edition)

A $2,000 paper-trading account run entirely by Claude: it designs its own
strategy and risk parameters, trades BTC/ETH/SOL on closed 1-minute
candles, journals every trade, reviews its own journal every 8 closed
trades, and deploys improved strategy versions with a changelog.

GitHub Actions executes it every ~5 minutes. GitHub Pages hosts your
live dashboard at: https://YOURUSERNAME.github.io/YOURREPO/

## Files
- bot/run.py        — entry point for each scheduled run
- bot/engine.py     — feeds (Kraken→Coinbase→Binance), indicators, execution, journal, guards
- bot/brain.py      — Claude strategy design + self-review (hard risk caps)
- docs/index.html   — dashboard page (served by GitHub Pages)
- SETUP-workflow.yml — COPY of the workflow; paste it into .github/workflows/bot.yml (see README steps)
- requirements.txt

## Setup summary
1. Create a PUBLIC repo, upload bot/, docs/, requirements.txt, README.md
2. Add file .github/workflows/bot.yml — paste the contents of SETUP-workflow.yml
3. Settings → Secrets and variables → Actions → New repository secret:
   name ANTHROPIC_API_KEY, value = your key from console.anthropic.com
4. Settings → Pages → Deploy from a branch → main / /docs → Save
5. Actions tab → trading-bot → Run workflow (first manual run)
6. Open https://YOURUSERNAME.github.io/YOURREPO/ — bookmark it

## Notes
- Paper trading only. Hard caps: ≤2% risk/trade, ≤3% daily loss halt
  (auto-flatten, resumes next UTC day) regardless of what the strategy asks.
- To reset the account: delete state.json from the repo (the next run starts fresh at $2,000).
- If GitHub emails that the scheduled workflow was disabled for inactivity, open Actions and click Enable.
- The repo is public, so the journal/dashboard are publicly viewable.
