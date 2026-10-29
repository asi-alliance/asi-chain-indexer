# ASI Chain Indexer Database Migrations

## Current Migration Strategy

This directory contains a **single comprehensive initial migration** that includes all necessary database schema components for the ASI Chain indexer.

### Active Migration

- **`000_comprehensive_initial_schema.sql`** - Complete database schema including:
  - Core blockchain tables (blocks, deployments, pending_deploys, transfers)
  - Validator and staking tables with extended name fields (VARCHAR(160))
  - Balance tracking with bonded/unbonded separation
  - Network statistics and epoch transition tracking
  - All necessary indexes, triggers, and constraints
  - Documentation comments

### Migration History

Legacy migrations were consolidated into `000_comprehensive_initial_schema.sql`
and removed from the active tree. Earlier versions remain available in Git history.

## For New Deployments

**Use only the comprehensive migration:**

```bash
# Apply the complete schema
psql -U indexer -d asichain -f 000_comprehensive_initial_schema.sql
```

## Schema Features

### Enhanced Tables
- **Extended validator names**: VARCHAR(160) to accommodate full public keys
- **JSONB fields**: bonds_map, justifications for rich data storage
- **Balance tracking**: Separate bonded/unbonded balance states
- **Pending deploys**: Ephemeral snapshot of the node's deploy buffers (`pending_deploys`), fully refreshed every sync cycle — no block_hash/FK by design, `sig` matches `deployments.deploy_id` once included
- **Network statistics**: Real-time consensus and validator metrics
- **Computed columns**: Auto-calculated total balances

### Performance Optimizations
- Comprehensive indexing strategy
- Partial indexes for pattern matching
- Optimized foreign key relationships
- Real-time notification triggers

### Compatibility
- Fully compatible with Hasura GraphQL auto-generation
- Supports all current indexer functionality
- Includes all enhancements from previous migrations
- Ready for production deployment

## Benefits of Single Migration Approach

1. **Simplified deployment**: One file contains everything
2. **No migration conflicts**: Eliminates dependency issues  
3. **Complete documentation**: All schema elements in one place
4. **Version clarity**: Clear schema version (000) for fresh deployments
