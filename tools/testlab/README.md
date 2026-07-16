# tools/testlab (TEST ONLY)

Run the real engine against real logs. Nothing here is deployed to prod, and the
shippers only ever write to a dev Event Hub. See `docs/PRODUCTION.md` § 14 for
how to feed a private environment.

## Run the pipeline locally ($0, no Azure)

- `run_local.py`  the real engine + fakeredis + a local sink. **Start here.**
- `samples/*.jsonl`  starter Palo/Cloudflare events, stamped with `dataset` /
  `_time` — the engine's default field names (Cribl's, *not* Panther's `p_`
  convention). If you restamp these to `p_log_type`, every event is dropped as
  "missing the 'log type' field" unless you also set `LOG_TYPE_FIELD`.

For a higher-fidelity run — **real Redis 6.0**, Python 3.11, and the **prod**
routing branch — use [`../sim/`](../sim/README.md). fakeredis accepts Redis 7.0
syntax that Azure rejects, so `run_local.py` cannot tell you whether dedup works
in Azure.

## Make your own logs

- `capture_netlogs.py`  snapshots this machine's live network connections as
  pyre-shaped JSON. Stands in for Cribl: it stamps the routing field (`dataset`)
  and the time field (`_time`), which is the one job a normalizer does before
  anything reaches Event Hubs.

```bash
pip install psutil
python tools/testlab/capture_netlogs.py -o network_logs.jsonl   # run a few times
python tools/testlab/run_local.py --file network_logs.jsonl
```

Needs privileges to read other processes' connections (Administrator on Windows,
`sudo` elsewhere); without them `process_name` degrades to `"unknown"` rather
than failing.

> This file used to live at `tests/test_network_logs.py`, where pytest collected
> it. It defines no tests, and its `psutil` import — absent from
> `tests/requirements.txt` — failed collection for the **entire** suite, so
> `pyre test` and CI could not run at all. It is not a test; don't move it back.

## Ship logs to a deployed lab

- `fluent-bit.conf`   ship live logs (Pi/PC) to Event Hubs via its Kafka endpoint
- `python_shipper.py` replay a .jsonl into an Event Hub at a chosen rate
- `replay_samples.py` convenience wrapper over the starter samples

`--hub` must name a hub that `config/sources.yaml` creates **and** that the
processor actually consumes — see [../PRODUCTION_CHECKLIST.md](../PRODUCTION_CHECKLIST.md) §0.
