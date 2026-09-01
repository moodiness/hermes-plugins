# CLI reference

- `doctor [--json]`: dependencies, state, service backend and prohibited-coupling indicators.
- `create NAME --cwd DIR --model MODEL --mission TEXT [routing/options]`: persist and optionally install/start.
- `adopt NAME --inspection FILE --mission TEXT`: adopt only inspected argv with explicit `--resume ID`; does not kill a source process.
- `list [--json]`, `status NAME [--json]`, `logs NAME --lines N`.
- `send NAME MESSAGE`: durable follow-up queue.
- `stop NAME`, `restart NAME`, `remove NAME`.
- `inbound NAME ...`: replaceable public inbound bridge entry point.
- `run NAME`: service-only supervisor entry point.

`adopt` intentionally requires an externally produced, trusted inspection JSON. Operators must stop the source only after proving the new service owns the same ID; RC1 never mutates unknown processes.
