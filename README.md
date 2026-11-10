# MT5 relay 3.4.0

TradingView alerts -> HTTPS relay -> polling MT5 EA. This repository hosts the relay, not MQL5 source. The Flask entry point is `bitget_bot:app`; the legacy filename is kept to preserve the existing host start command.

## Run on another host

Use Python 3.12+, install `requirements.txt`, configure `WEBHOOK_SECRET` privately, and run `gunicorn bitget_bot:app --workers 1 --bind 0.0.0.0:$PORT`. Configure HTTPS at the hosting edge. Never put a real secret in source control, URLs shared with others, screenshots, or examples. Set `RELAY_DB_PATH` to a writable SQLite file under a genuinely persistent volume. Copying code alone does not migrate pending signals or credentials.

Render's default filesystem is ephemeral, including `/tmp`. The default SQLite database can coordinate workers and survive a Python worker reload when its file remains, but does not survive Render redeploys or instance replacement without persistent storage. Persistent disks require a paid Render service. This release does not purchase services or promise lossless delivery.

## Interface

- POST `/mt5order`: JSON object with `secret`, `action`, `symbol`; optional `lots`, `strategy`, `id`.
- GET `/mt5poll`: `symbol`, optional `strategy`; prefer `Authorization: Bearer <secret>`. Query authentication remains enabled for existing EAs.
- GET `/healthz` or `/`: public, reports readiness and version only. It never polls or submits an order.
- GET `/status`: requires authentication; reports pending counts and configuration flags.

Actions: `long`, `short`, `close_long`, `close_short`. A signal is an instruction to evaluate: entry and exit rules remain in each EA. The EA may deliberately ignore a close alert. It calculates its own position size; relay `lots` is not a guarantee of actual size.

Signals are FIFO per symbol and strategy. Strategy omission selects the legacy queue. A supplied `id` is deduplicated for at least 7 days (pending signals stay until delivered/expired). Without a supplied `id`, repeated messages remain distinct to preserve existing alert behavior. There is no execution acknowledgment in v7.27: receiving a poll response marks a signal delivered, not traded. A lost response may lose a signal. Do not add automatic redelivery without persistent EA deduplication and order-result reconciliation.

Unknown symbols, malformed JSON, invalid actions, nonfinite/nonpositive lots, oversized bodies, invalid IDs/strategies are rejected. A full queue returns 429 instead of silently dropping its oldest signal. Body limit: 16 KiB; per-queue limit: 100; total pending: 1000. These bounds are not financial risk controls. HTTP 200 from `/mt5order` means queued, not executed.

## Environment

| Name | Default | Meaning |
|---|---|---|
| WEBHOOK_SECRET | Required | Missing/empty refuses startup. Rotate any publicly disclosed value in TradingView, host and all active EAs together. |
| ALLOW_LEGACY_QUERY_AUTH | true | Compatibility for v7.27. Set false only after all polling EAs use a header. HTTPS does not keep query strings out of edge logs. |
| ENABLE_LEGACY_BITGET | false | Legacy Bitget functions retained for future migration, but endpoints are inactive unless explicitly enabled. Do not enable without reviewing current exchange API, permissions and account mode. |
| MT5_ALLOWED_SYMBOLS | Existing 15 symbols | Comma-separated allowed MT5 symbol names. Adjust to broker naming; no wildcards. |
| RELAY_DB_PATH | temp directory file | Queue database path. Must be persistent for cross-redeploy retention. |
| SIGNAL_TTL_SECONDS | 0 | Queue expiry by relay receive time. Disabled to preserve existing strategy behavior. Enable only with an agreed maximum delay. Does not verify TradingView creation time. |

Old exchange API credentials are not needed for MT5. Revoke unused credentials at their issuer; disabling a route does not revoke credentials.

## Deployment and rollback

1. Run offline contract tests before changing the live branch. Back up known-live source and host settings without exposing credentials.
2. Use a commit with `[skip render]` to stage code without auto-deployment. Then deploy that reviewed commit deliberately.
3. Check pending queue counts before cutover. The legacy in-memory queue cannot be migrated through the health API; deploying can lose signals during cutover. Choose a demo maintenance interval and reconcile TradingView alerts with EA RAW_SIGNAL logs afterward.
4. Confirm Live status and `/healthz` version. Check real alert-to-RAW_SIGNAL delivery without injecting a test market order.
5. Roll back to the backed-up normal Python commit on failure. Rolling back does not undo trades or restore a lost ephemeral queue.

The TradingView VIP indicator may be closed-source. This relay does not recreate its proprietary signal formula. Moving MT5 brokers requires symbol, lot step, contract value, stop distance, spread and trading-session checks. Broker positions and funds cannot be transferred by redeploying this relay.
