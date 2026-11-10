# Node wire profiles

Generate each profile as a complete set with `scripts/generate-protos.py`.
The generated directory is ignored and includes a build-profile marker.
Never mix messages from the two profiles in one descriptor pool/process.

- `classic`: root definitions synchronized with node development schemas by
  indexer #23; exact-transfer fields from node #896 at
  `92903a1d81c49fa0a035fb7ee729002e38428280`, as used by indexer #22.
- `uint256`: all six definitions under `uint256/` are byte-preserved copies from
  [node #881](https://github.com/F1R3FLY-io/f1r3node-rust/pull/881) final head
  [`6233501064492d0cac9c7ebc598daf70c501a215`](https://github.com/F1R3FLY-io/f1r3node-rust/tree/6233501064492d0cac9c7ebc598daf70c501a215/models/src).
  Five files come from `models/src/main/protobuf/`; `scalapb/scalapb.proto`
  comes from `models/src/scalapb/scalapb.proto`. They retain upstream notices
  and options and are regenerated together with the locked generator.

The uint256 profile includes #896/#947's reconciled contract: transfer amounts
use signed canonical `amountRaw` text at field 6, old field 3 is reserved, and
exact-transfer request flags are reserved/absent. BondInfo.stake is string field 3;
DeployInfo.phloPrice is string field 16; Status.minPhloPrice is string field 18.
DeployDataProto.phloPrice is exactly 32 big-endian bytes at field 15, including
pending deploys; BondProto.stake uses the same encoding at field 3.

The indexer converts unsigned monetary strings/bytes to checked Python integers.
Other bytes retain their original handling (pending deployer/signature as hex).
Compact signed attempts retain text, and unavailable summaries stay unknown.
Replay reports still determine successful ledger movements. Block/deploy/bond
reads, both light-block streams, finalized snapshots, status and pending snapshots
share the selected definitions. Report event definitions and typed parameters
(including `big_int_value`) come from the same pinned set. No peer writes, deploy
signing or stored-node-state migration are performed by the indexer.

Generation/build and runtime default to `uint256`. Select
`NODE_PROTOCOL_PROFILE=classic` explicitly at both stages for legacy nodes using
the pre-#881 contract. The legacy profile retains its existing `classic` name.
Rebuild/regenerate when changing profiles; a runtime setting alone cannot change
the generated bindings. Profile selection does not select token precision or the
separate cost-accounting schema.
See the main README for SQL migration 004 and the remaining live acceptance gate.
