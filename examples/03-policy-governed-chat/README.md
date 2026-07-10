# Policy-Governed Chat

The two policy profiles contrast bounded completion of an already admitted turn
with immediate hard stop. They specify new-work denial, continuation bounds,
provider cancellation, draft disposition, durable output, and effect atomicity.

```bash
python examples/03-policy-governed-chat/run.py
```

The script validates the profiles as resource documents; it does not start a
chat server or model session.
