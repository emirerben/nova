#!/bin/bash
# Nova — Cofounder Setup Script
# Run once: bash setup-cofounder.sh
set -euo pipefail

NOVA_DIR="$HOME/projects/nova"
WORKSPACE_DIR="$HOME/.openclaw/workspace/startups/nova"
BUILDER_STARTUP="$HOME/.openclaw/workspaces/builder/CURRENT_STARTUP.md"

echo "=== Nova cofounder setup ==="

# 1. Clone code repo
if [ -d "$NOVA_DIR" ]; then
  echo "✓ Nova code repo already exists at $NOVA_DIR"
else
  echo "Cloning nova code repo into ~/projects/nova ..."
  mkdir -p "$HOME/projects"
  git clone git@github.com:emirerben/nova.git "$NOVA_DIR"
fi
(cd "$NOVA_DIR" && git checkout dev)
echo "✓ On branch: dev"

# 2. Clone nova-workspace (product docs only)
if [ -d "$WORKSPACE_DIR" ]; then
  echo "✓ nova-workspace already exists at $WORKSPACE_DIR"
else
  echo "Cloning nova-workspace (product docs)..."
  mkdir -p "$(dirname "$WORKSPACE_DIR")"
  git clone git@github.com:emirerben/nova-workspace.git "$WORKSPACE_DIR"
fi
echo "✓ Workspace docs at $WORKSPACE_DIR"

# 3. Remind about CURRENT_STARTUP.md
echo ""
echo "⚠️  Manual step required:"
echo "   Update $BUILDER_STARTUP"
echo "   Set the active startup to nova with code path: $NOVA_DIR"
echo ""

# 4. Verify
echo "=== Verification ==="
[ -f "$NOVA_DIR/CLAUDE.md" ]              && echo "✓ CLAUDE.md" || echo "✗ CLAUDE.md missing"
[ -f "$NOVA_DIR/.env.example" ]            && echo "✓ .env.example" || echo "✗ .env.example missing"
[ -f "$NOVA_DIR/agents/VIDEO_CONTEXT.md" ] && echo "✓ agents/VIDEO_CONTEXT.md" || echo "✗ VIDEO_CONTEXT.md missing"
[ -f "$WORKSPACE_DIR/PROJECT.md" ]         && echo "✓ PROJECT.md (workspace)" || echo "✗ PROJECT.md missing"

echo ""
echo "Next steps:"
echo "  cd $NOVA_DIR"
echo "  cp .env.example .env    # fill in your values"
echo "  make dev                 # start all services"
echo ""
echo "Session workflow:"
echo "  make workspace-pull      # before every agent session (syncs product docs)"
echo "  make workspace-push      # after agent writes a significant doc"
echo "  (agents/ context syncs automatically via normal git pull/push)"
