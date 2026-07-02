#!/usr/bin/env bash
# ===========================================================================
# setup.sh -- one-time setup. Puts `oliveristhegoat`, `ollama-chat`,
# `ollama-serve`, and `ollama-code` on your PATH so you can run them from
# anywhere. Safe to re-run. Nothing runs on the login node and no GPU is
# used here.
#
#   ~/ollama-on-engaging/deliverables/setup.sh
#
# Undo: delete the symlinks it reports below.
# ===========================================================================
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
. "$HERE/lib/goat.sh"

goat_banner

BIN="$HOME/.local/bin"
mkdir -p "$BIN"

for cmd in oliveristhegoat ollama-chat ollama-serve ollama-code; do
  chmod +x "$HERE/$cmd"
  ln -sf "$HERE/$cmd" "$BIN/$cmd"
  goat_ok "linked ${G_BOLD}$BIN/$cmd${G_RST} ${C_DIM}→ $HERE/$cmd${G_RST}"
done
chmod +x "$HERE/lib/engaging_coder.py" 2>/dev/null || true

# Pre-create the model store so the very first run has nothing to trip on.
mkdir -p "${OLLAMA_MODELS:-$HOME/orcd/pool/ollama/models}" 2>/dev/null || true

goat_say ""
case ":$PATH:" in
  *":$BIN:"*)
    goat_ok "PATH already includes $BIN — you're ready to go" ;;
  *)
    goat_warn "one more step: add $BIN to your PATH, then open a new shell:"
    goat_dim  '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc' ;;
esac

{
  printf '\n  %sSTART HERE%s — one word, then just answer the menus:\n\n' "${C_GOLD}${G_BOLD}" "$G_RST"
  printf '      %soliveristhegoat%s\n\n' "${C_GOLD}${G_BOLD}" "$G_RST"
  printf '  %sOr skip the menus%s %s(GPUs are auto-sized within the per-user caps:\n' "${C_GOLD}${G_BOLD}" "$G_RST" "$C_DIM"
  printf '  2 on mit_normal_gpu, 4 on mit_preemptable — nothing bigger can ever start)%s\n\n' "$G_RST"
  printf '      ollama-chat  deepseek-r1:671b      %s# 671B reasoning → 4× H200%s\n'   "$C_DIM" "$G_RST"
  printf '      ollama-chat  glm-5:744b            %s# 744B            → 4× H200%s\n'  "$C_DIM" "$G_RST"
  printf '      ollama-chat  llama3.1:405b         %s# 405B            → 2× H200%s\n'  "$C_DIM" "$G_RST"
  printf '      ollama-chat  llama3.3:70b "hi"     %s# answer, then hold the node for more%s\n' "$C_DIM" "$G_RST"
  printf '      ollama-code  qwen3-coder:480b      %s# agentic coding in your project%s\n' "$C_DIM" "$G_RST"
  printf '      ollama-serve llama3.1:405b         %s# API server + SSH tunnel%s\n\n'  "$C_DIM" "$G_RST"
  printf '      oliveristhegoat status             %s# free GPUs · your jobs · storage%s\n' "$C_DIM" "$G_RST"
  printf '      oliveristhegoat models             %s# the curated model list%s\n\n'   "$C_DIM" "$G_RST"
} >&2
