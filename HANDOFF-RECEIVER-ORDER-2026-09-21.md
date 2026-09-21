# Handoff B: receiver-owned mail order

## Owner authority

`D-CODEX-0009` records Maxim's direct decision in the current Codex task:

> Делай всё, что тобой предложено, и отправляй на B для пуша и деплоя.

Scope: receiver-owned registration time and monotonic sequence for agent mail,
tests, push and deploy through Claude B, without rewriting historical messages.
The owner repeated that authorization immediately afterward.

## Problem fixed

The live readers used lexicographic filename order. A sender-controlled or wrong
timestamp could therefore reorder mail. Sender `created_utc` and filename dates
are now metadata only.

For each exact `(channel, filename, sha256)` version, the receiver writes once:

- `received_at_utc`
- `receive_sequence`
- `observation_batch`
- `order_within_batch: unknown`
- `registration_source`

The registry is durable and locked at:
`state/receiver-order/receive-order.json`.

A changed SHA gets a new registration. Re-reading the same version is
idempotent. Existing watcher batches migrate using their receiver-side
`created_at`; archive files are not renamed or rewritten. Files first observed
in one poll have no claimed causal ordering; `reply_to` remains the separate
source for a partial causal order.

## Files to review and commit

- `receiver_order.py`
- `mail_channel.py`
- `outbox_watch.py`
- `owner_watch.py`
- `test_receiver_order.py`
- `test_mail_channel.py`
- `test_mailroom.py`
- `test_outbox_watch.py`
- `README.md`

Do not commit `state/receiver-order/*`, mailbox contents, logs, caches, or other
working files.

## Verification already completed

From `agent-dispatcher`:

```bash
python3 -m unittest discover -q
python3 -m py_compile receiver_order.py mail_channel.py outbox_watch.py owner_watch.py
```

Result: 69 tests passed, compilation passed.

Production migration state after the first background load:

- 271 historical exact versions registered
- 239 Outbox versions and 32 owner-outbox versions
- all came from `legacy_receiver_state`
- zero current Outbox problems
- historical message files unchanged

A test-isolation defect discovered during acceptance was fixed in
`test_mailroom.py`; the one synthetic registry row was removed. Tests no longer
write into production receiver state.

## SHA-256

```text
42b8e3448e5ce56282e640131a02df9b4b76fb1ab86367bb4a843e8180d51e85  receiver_order.py
0c1da58c87675f50f21a407bad4f5b86b539e8590344e22c9b572a532b6256bd  mail_channel.py
e3e1e14824b0e162fc8a3467f0e8ebdc9451bf9e8d4904ccd5ad6f0e02107c78  outbox_watch.py
254006f94a7e11d08531bdd082f5c284b3188b5779d6200f67d13f7c014ebcca  owner_watch.py
7b75fb5a8dba1402e1675bf2226042c2f9a8b5c85caa8d3c8d69b0a038430411  test_receiver_order.py
b55423cb20f64bdc70983a99566f36d05b794444ff9b90fce76ce1f05aa9f578  test_mail_channel.py
489571ac1667bc110843fd622e08f84687473a2cda07d165922e109ad2e73205  test_mailroom.py
0987999827827f3f62700e0b8020b94de0f170365df0d9b03c59c3485db0680f  test_outbox_watch.py
3d71918df75909e23d177891c896e80df3a682965cc40b605d9e36a49fe9802b  README.md
```

## Push and deploy requested from B

1. Review the exact files and hashes above.
2. Run the full test command again.
3. Commit only this bounded change to the canonical coordination source and
   push without force.
4. Deploy the verified files to the live `agent-dispatcher` directory if the
   canonical source is elsewhere. The current shared directory has already
   been loaded by the scheduled process, so reconcile rather than overwrite it
   with an older copy.
5. Restart or kick the two relevant agents after the commit is established:
   `com.biosingularity.codex-outbox` and `com.biosingularity.codex-mailroom`.
6. Verify both exit successfully, the registry remains readable, and a
   controlled temporary test proves that a later-arriving past-dated filename
   cannot jump ahead of an earlier-arriving future-dated filename.
7. Report commit SHA, remote branch, deployment result, and smoke-test evidence.

Rollback: restore the prior code revision and restart both agents. Preserve the
registry file; old code ignores it and deleting it would remove audit evidence.
