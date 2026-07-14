# tools/testlab (TEST ONLY)

Feed REAL logs from your PC/Raspberry Pi into the same pipeline. See
`docs/PRODUCTION.md` § 14 for how to feed a private environment. Three inputs:

- `fluent-bit.conf`   ship live logs (Pi/PC) to Event Hubs via its Kafka endpoint
- `python_shipper.py` replay a .jsonl of real Palo/Cloudflare events at a chosen rate
- `replay_samples.py` convenience wrapper over the starter samples

Nothing here is deployed to prod; it only writes to a dev Event Hub.
