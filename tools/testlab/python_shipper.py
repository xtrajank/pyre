#!/usr/bin/env python3
"""Replay newline-delimited JSON events into Event Hubs at a chosen rate.
Uses Managed Identity / az-login credentials (no keys). For load-testing scale
and cost, raise --rate and add --loop.

  python tools/testlab/python_shipper.py --namespace <ns>.servicebus.windows.net \
      --hub logs-in --file tools/testlab/samples/palo_sample.jsonl --rate 500 --loop
"""
import argparse
import itertools
import json
import time
from azure.identity import DefaultAzureCredential
from azure.eventhub import EventHubProducerClient, EventData


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--hub", required=True)
    ap.add_argument("--file", required=True)
    ap.add_argument("--rate", type=int, default=200, help="events/sec")
    ap.add_argument("--loop", action="store_true")
    args = ap.parse_args()

    cred = DefaultAzureCredential()
    producer = EventHubProducerClient(fully_qualified_namespace=args.namespace,
                                      eventhub_name=args.hub, credential=cred)
    lines = [l for l in open(args.file) if l.strip()]
    src = itertools.cycle(lines) if args.loop else iter(lines)

    sent, t0, batch = 0, time.time(), producer.create_batch()
    with producer:
        for line in src:
            try:
                batch.add(EventData(line.strip()))
            except ValueError:
                producer.send_batch(batch); batch = producer.create_batch(); batch.add(EventData(line.strip()))
            sent += 1
            if sent % args.rate == 0:
                producer.send_batch(batch); batch = producer.create_batch()
                elapsed = time.time() - t0
                if elapsed < sent / args.rate:
                    time.sleep(sent / args.rate - elapsed)
        if len(batch):
            producer.send_batch(batch)
    print(f"sent {sent} events")


if __name__ == "__main__":
    main()
