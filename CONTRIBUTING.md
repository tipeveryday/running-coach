# Contributing

Thanks for helping improve Running Coach. Bug reports, ideas and pull requests are all welcome.

## How changes get in

- `main` is protected. Nobody pushes to it directly. Every change lands through a pull request.
- Fork the repo, make your change on a branch in your fork, and open a pull request against `main`.
- The maintainer reviews every pull request and is the only one who can approve and merge it.
- Keep pull requests small and focused; one fix or feature per PR is easiest to review.

## Getting set up

```bash
uv run python backend/app.py --demo     # run against the synthetic demo athlete
uv run python tests/test_smoke.py       # smoke tests — run these before opening a PR
```

Use `--demo` or `--offline` while developing so you don't hit Garmin repeatedly (Garmin rate-limits
repeated logins).

## Guidelines

- **No personal data in commits.** Never commit `.env`, `data/`, Garmin tokens, or screenshots of
  your own data. Use demo mode for screenshots.
- **UI changes** follow [DESIGN_SYSTEM.md](DESIGN_SYSTEM.md): colours, sizes and spacing come from
  `frontend/design/tokens.css` only.
- **Coaching logic** should degrade gracefully: if a metric is missing, show a "no data yet" state
  rather than erroring.
- Describe what you changed and how you tested it in the pull request.

## Reporting bugs

Open an issue with what you expected, what happened, and your watch model if it's data-related.
Please don't paste Garmin tokens, API keys or personal health data into issues.
