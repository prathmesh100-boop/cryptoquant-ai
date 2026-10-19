# Security Policy

- **Secrets** live only in `.env` (git-ignored) or the process environment. `.env.example` holds placeholders only.
  Placeholder values are treated as *missing* credentials.
- **Logging** redacts values under keys matching api key / secret / token / password / signature / authorization /
  credential. Credentials are `SecretStr` and never appear in `repr()` or JSON dumps.
- **Mode safety:** default `PAPER`. `DEMO`/`LIVE` need credentials; `LIVE` also needs `CQ_LIVE_TRADING_CONFIRMED=true`.
  The application never switches modes at runtime.
- **Exchange API keys:** when Phase 7 lands, use keys with trading permission only (no withdrawal) and IP allow-listing.
- **Tests** never place real orders; exchange access is mocked.
- Report vulnerabilities privately to the repository owner rather than in a public issue.
