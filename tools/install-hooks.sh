#!/bin/bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chmod +x tools/hooks/*
git config core.hooksPath tools/hooks
echo "git hooks enabled (tools/hooks)"
