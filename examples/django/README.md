# Django example

For a Django app that creates its local subscription when it receives `checkout.session.completed`.

1. Copy [`billing_e2e.py`](billing_e2e.py) to `<your_app>/management/commands/billing_e2e.py` and adapt the three functions marked `ADAPT` to your models.
2. Copy [`billing-e2e.yml`](billing-e2e.yml) to your project root and put your prices under `vars:`.
3. Copy [`scenarios/`](scenarios), or run `billing-e2e init` for the built-in scenarios, and add checks such as `python manage.py billing_e2e expect --plan Business`.
4. Run your app, forward webhooks to it (`stripe listen --forward-to localhost:8000/your/webhook/`), then:

```bash
STRIPE_TEST_KEY=sk_test_... billing-e2e run
```

This is the setup used to test a production Django app: five scenarios, 49 steps, about four months of billing in two minutes. Removing a one-line fix from its webhook handler made the renewal scenario fail with the exact cause (its message, translated):

```
✗ check `python manage.py billing_e2e expect --plan Business --status active --synced`
    next billing date 2026-11-16 23:50 in the app, 2026-12-16 23:50 at Stripe
```
