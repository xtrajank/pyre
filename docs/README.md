# docs/ — the guides

Start with the top three.

| Doc | What it's for | Read it when |
|---|---|---|
| **[GLOSSARY.md](GLOSSARY.md)** | Every term used anywhere in this repo, in plain English. | You're new, or a word is unfamiliar. Read first. |
| **[PRODUCTION.md](PRODUCTION.md)** | The complete master guide: architecture, every variable, spin up dev **and** prod end to end, what it looks like in Azure, monitoring, debugging, scaling, spin-down, and a boss-walkthrough script. | You want to understand the whole system or run/operate it. **This is the main one.** |
| **[architecture.md](architecture.md)** | One page: how the pieces fit and why each Azure choice keeps it cheap. Includes the detection-freshness (hot-reload) design. | You want the mental model fast. |
| [local-dev.md](local-dev.md) | The $0, no-Azure loop for developing and testing the engine/detections on your laptop. | You're changing code in this repo and want to test it. |
| [security.md](security.md) | The security posture and what to check before deploying. | Before any real deployment; for review/compliance. |

For a topic tied to a specific part of the codebase, each directory has its own `README.md` (see the table in the [root README](../README.md)).
