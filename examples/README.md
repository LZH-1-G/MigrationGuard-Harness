# Examples

All examples use the benchmark projects in [`benchmarks/cases`](../benchmarks/cases); each
`repo/` directory is a small but realistic Pydantic v1 service with its own tests.

## 1. Guided demo (deterministic, no API key needed)

```bash
./examples/demo.sh            # copies the billing service to a temp dir, then:
                              #   analyze → run --agent none → status → report
```

The billing service is the instructive one: after the deterministic codemods the whole
test suite is green, but the differential probes find 13 behavioural differences (v2 no
longer coerces the CRM's integer ids to strings, `Decimal` is serialised as a JSON string,
fractional quantities are rejected). MigrationGuard rejects the migration and the report
shows each difference with the baseline and migrated values.

## 2. Claude-powered migration

```bash
./examples/demo.sh --claude   # same project, with the Claude repair + reviewer agents
```

Expected: one repair iteration restores the v1 behaviour (`coerce_numbers_to_str`, a JSON
serializer for `Decimal`, float truncation), all gates pass, the reviewer approves, and the
run is accepted. A recorded run cost $0.50 and took 81 s.

## 3. Crash and resume

```bash
MIGRATIONGUARD_FAULT=verify:crash migrationguard run /tmp/mg-demo/billing --agent none
echo $?                       # 137: the process died in the middle of the run
migrationguard status         # the run is checkpointed at phase "verify"
migrationguard resume <run-id>
```

## 4. User probes for exact outputs

[`user-probes/migrationguard_probes.py`](user-probes/migrationguard_probes.py) pins the
HMAC-signed payload of the webhooks case. v2's `model_dump_json()` is compact where v1's
`.json()` used `", "` separators, which no model-level probe can see because the parsed JSON
is identical. Copy the file to the root of `benchmarks/cases/webhooks/repo` (in a scratch
copy) and the same migration that was accepted before is rejected with a
`user:probe_signing_payload` difference.
