# Operational Checklist

To be completed before taking any strategy live.

- [ ] **Infrastructure Check**: Run `python -m auth.cli validate` to ensure secrets are mounted.
- [ ] **Static IP Check**: Confirm VPS IP is explicitly whitelisted on Upstox/Dhan consoles.
- [ ] **Broker Login**: Run `python -m auth.cli upstox` and authorize the app.
- [ ] **Health Monitoring**: Run `python -m auth.cli status` and ensure `token_valid` is `true`.
- [ ] **Risk Engine**: Verify that the global kill-switch is active.
- [ ] **Database Connection**: Ensure `SUPABASE_URL` is responsive.
