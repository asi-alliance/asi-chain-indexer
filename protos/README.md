# Cost-accounting protocol source

This branch vendors `CasperMessage.proto`, `DeployServiceCommon.proto`,
`DeployServiceV1.proto` and `RhoTypes.proto` from maintained
[f1r3node-rust #216 at 1b5a92f13ca7e23605150aa66fd795a36a7ae8f1](https://github.com/F1R3FLY-io/f1r3node-rust/tree/1b5a92f13ca7e23605150aa66fd795a36a7ae8f1/models/src/main/protobuf).
Regenerate bindings with the repository Dockerfile or `make protos`.

The four schemas form one cost-accounting wire contract. In particular,
`DeployDataProto.cosigners` uses field 14 and `parameters` uses field 22.
Classic node dev instead uses field 14 for parameters. Do not concatenate both
definitions or describe the cost schema as wire-compatible with every classic
node payload. Use the matching cost-accounted node/revision for deployment.
Common API deploy parameters and reporting phases from the merged dev are retained.

Serialization respects message presence because signature messages are recursive.
Pending offers use deployId and the first allOf authorization member as the
displayed deployer; empty/threshold-only policies have no inferred deployer.
Legacy signature/deployer fields remain readable when deployId is absent. This
fallback does not reconcile the incompatible parameter tags above or verify
an authorization policy.

The pinned node implementation emits USER event batches for offered deploys as
well as native cost evidence/settlement receipts. An older schema comment says
offers have no event batches; the implementation is authoritative. Successful
native movements still require execution events. Settlement amounts are not a
replacement transfer ledger, and an unavailable trace must fail/retry.

Indexer #22/node #896 and node #881 introduce separate amount/API changes.
They are not silently incorporated into this schema refresh. No additional
database migration is added beyond merged dev migrations 000–003.
