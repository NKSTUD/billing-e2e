# billing-e2e

Test your Stripe subscription lifecycle end to end: renewals, failed payments, proration and cancellation, in minutes instead of waiting for real billing cycles.

> Status: early work, nothing usable yet.

## The idea

You describe a scenario; the tool plays it on a Stripe test clock and checks that **your application** follows at every step.

```yaml
- subscribe: business_monthly
- advance: 1 month
- expect: invoice.paid
- check: GET /api/account -> plan == "Business"
- card: fails
- advance: 1 month
- check: plan == "Free"
```

## Safety

- Test mode only: live keys are refused, and the account is checked with Stripe.
- Nothing leaves your machine: no telemetry, no server of ours; keys never appear in logs or reports.
- Every run creates its own customer on its own test clock and deletes everything at the end. Existing data is never touched.

## License

MIT
