#!/bin/sh
set -eu

# This check never opens the database. Configuration errors must fail before
# main.js can apply a schema migration to a persistent ledger.
node server/dist/src/cli/check-config.js
exec node server/dist/src/main.js
