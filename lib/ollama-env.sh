#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Ollama environment for the MIT ORCD "Engaging" cluster (self-contained).
#
# You normally never source this by hand -- the `ollama-chat` / `ollama-serve`
# commands do it for you. It is here so the deliverables folder is standalone.
#
# It must be sourced on a COMPUTE node (inside a Slurm job), never on a login
# node: login nodes have no GPU and a hard 10 GB / 4-core cap shared by everyone.
#
# Nothing is hard-coded to one account, so the whole folder is safe to share.
# ---------------------------------------------------------------------------

# 1) Make sure Lmod's `module` function exists (it may not in a batch shell).
if ! command -v module >/dev/null 2>&1; then
  for f in /etc/profile.d/lmod.sh /etc/profile.d/z00_lmod.sh /etc/profile.d/modules.sh; do
    [ -r "$f" ] && source "$f" && break
  done
fi

# 2) Put the `ollama` binary on PATH via the system module (no install needed).
#    Ollama is a COMMUNITY module -- its tree is not on the default MODULEPATH,
#    so add it first (otherwise this only works if the submitting shell
#    happened to have it, which is exactly the kind of bug we don't want).
_goat_community="/orcd/software/community/001/modulefiles"
case ":${MODULEPATH:-}:" in
  *":${_goat_community}:"*) ;;
  *) module use "$_goat_community" 2>/dev/null \
       || export MODULEPATH="${_goat_community}${MODULEPATH:+:$MODULEPATH}" ;;
esac
module load ollama/0.13.5 2>/dev/null || module load ollama 2>/dev/null || true

# Last resort if the module system misbehaves: use the known install directly.
if ! command -v ollama >/dev/null 2>&1; then
  for _goat_bin in /orcd/software/community/001/pkg/ollama/*/bin; do
    [ -x "$_goat_bin/ollama" ] && PATH="$_goat_bin:$PATH"
  done
  export PATH
fi

# 3) Store model blobs OFF home (home has a 1M-file quota and is backed up).
#    Each user's ~/orcd/pool resolves to THEIR OWN persistent space, so this is
#    portable. Models are downloaded once and reused by every future job.
export OLLAMA_MODELS="${OLLAMA_MODELS:-$HOME/orcd/pool/ollama/models}"
mkdir -p "$OLLAMA_MODELS" 2>/dev/null || true

# 4) Bind the server to localhost only (GPU nodes are shared between users),
#    on a per-job port so two jobs on one node don't collide. Reach it from a
#    laptop via an SSH tunnel (ollama-serve prints the exact command).
export OLLAMA_HOST="127.0.0.1:$(( 11434 + ${SLURM_JOB_ID:-0} % 5000 ))"

# 5) How long an idle model stays resident in GPU memory (default 5 minutes).
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-5m}"
