#!/usr/bin/env bash
# ===========================================================================
# goat.sh -- shared library for the oliveristhegoat toolkit.
#
# Sourced by: oliveristhegoat, ollama-chat, ollama-code, ollama-serve, setup.sh
# Provides:
#   * a color/style system   (truecolor -> 256 -> 16 -> plain; honors NO_COLOR,
#                             GOAT_COLOR=0/1, FORCE_COLOR, and non-TTY output)
#   * QOS-aware GPU sizing   (the cluster caps every user at 2 GPUs on
#                             mit_normal_gpu and 4 GPUs on mit_preemptable --
#                             requests above that pend forever, so we refuse
#                             to make them and route big models sensibly)
#   * live availability      (partition totals AND an exact node-level probe:
#                             a request only starts NOW if one node has the
#                             GPUs + idle CPUs + free RAM together)
#   * a shared launcher      (hunts the exact request across a fallback ladder
#                             of GPU types/partitions, then srun --immediate;
#                             WAIT=1 opts into queueing indefinitely)
#
# All UI goes to stderr so one-shot model output on stdout stays pipeable.
# ===========================================================================

# --------------------------------------------------------------------------
# Colors
# --------------------------------------------------------------------------
# Modes: true (24-bit) | 256 | 16 | none. Chosen once at source time.
#   GOAT_COLOR=0 / NO_COLOR  -> force off      GOAT_COLOR=1 / FORCE_COLOR -> force on
goat_color_init() {
  local mode=none ncolors=8
  if [ "${GOAT_COLOR:-}" = "0" ] || [ -n "${NO_COLOR:-}" ] || [ "${TERM:-}" = "dumb" ]; then
    mode=none
  elif [ "${GOAT_COLOR:-}" = "1" ] || [ -n "${FORCE_COLOR:-}" ] || [ -t 2 ]; then
    case "${COLORTERM:-}" in
      *truecolor*|*24bit*) mode=true ;;
      *)
        ncolors="$(tput colors 2>/dev/null || echo 8)"
        if [ "${ncolors:-8}" -ge 256 ]; then mode=256; else mode=16; fi ;;
    esac
  fi
  GOAT_COLOR_MODE="$mode"

  _gc() {  # _gc "R;G;B" <256-code> <16-attr>  -> escape sequence (or nothing)
    case "$GOAT_COLOR_MODE" in
      true) printf '\033[38;2;%sm' "$1" ;;
      256)  printf '\033[38;5;%sm' "$2" ;;
      16)   printf '\033[%sm'      "$3" ;;
    esac
  }

  if [ "$mode" = none ]; then
    G_RST='' G_BOLD='' G_DIMA=''
  else
    G_RST=$'\033[0m' G_BOLD=$'\033[1m' G_DIMA=$'\033[2m'
  fi

  # Brand palette (kept in the same family as engaging_coder.py).
  C_GOLD="$(_gc '255;191;0'   214 33)"   # brand / accents
  C_ORNG="$(_gc '255;145;40'  208 33)"
  C_OK="$(_gc   '126;199;148' 114 32)"   # green
  C_WARN="$(_gc '224;187;106' 179 33)"   # amber
  C_ERR="$(_gc  '233;110;110' 167 31)"   # red
  C_INFO="$(_gc '150;205;235' 117 36)"   # cyan
  C_MAG="$(_gc  '198;146;233' 141 35)"   # violet (preemptable tag)
  C_DIM="$(_gc  '140;140;152' 245 90)"   # gray
  C_FG="$(_gc   '226;226;231' 254 97)"   # near-white
}
goat_color_init

# --- message helpers (stderr) ----------------------------------------------
goat_say()  { printf '%s\n' "$*" >&2; }
goat_step() { printf '  %s▸%s %s\n'  "${C_GOLD}${G_BOLD}" "$G_RST" "$*" >&2; }
goat_ok()   { printf '  %s✓%s %s\n'  "${C_OK}${G_BOLD}"   "$G_RST" "$*" >&2; }
goat_warn() { printf '  %s⚠ %s%s\n'  "$C_WARN" "$*" "$G_RST" >&2; }
goat_err()  { printf '  %s✗ %s%s\n'  "${C_ERR}${G_BOLD}" "$*" "$G_RST" >&2; }
goat_dim()  { printf '  %s%s%s\n'    "$C_DIM" "$*" "$G_RST" >&2; }
goat_kv()   { printf '  %s%-11s%s %s\n' "$C_DIM" "$1" "$G_RST" "$2" >&2; }
goat_hr()   { printf '  %s%s%s\n' "$C_DIM" \
  '────────────────────────────────────────────────────────────' "$G_RST" >&2; }

# --- banners ----------------------------------------------------------------
goat_banner() {  # the big one (oliveristhegoat, setup.sh)
  local g1 g2 g3 g4 g5 g6 b1 b2 b3 b4 b5 b6 v
  # Two deliberately contrasting gradients: OLIVER runs cool (ice → violet),
  # GOAT runs warm (gold → ember) — same weight, opposite temperature.
  b1="$(_gc '150;235;255' 123 36)"; b2="$(_gc '115;205;255' 117 36)"
  b3="$(_gc '90;170;250'  75  36)"; b4="$(_gc '100;135;245' 69  34)"
  b5="$(_gc '130;110;240' 99  35)"; b6="$(_gc '165;95;235'  135 35)"
  g1="$(_gc '255;215;0'  220 33)"; g2="$(_gc '255;193;7'  220 33)"
  g3="$(_gc '255;170;20' 214 33)"; g4="$(_gc '255;145;40' 208 33)"
  g5="$(_gc '244;115;55' 202 31)"; g6="$(_gc '230;90;70'  196 31)"
  v="$(cat "${GOAT_HOME:-$(dirname "${BASH_SOURCE[0]}")/..}/VERSION" 2>/dev/null || true)"
  {
    printf '\n'
    printf '   %s ██████╗ ██╗     ██╗██╗   ██╗███████╗██████╗ %s\n' "$b1" "$G_RST"
    printf '   %s██╔═══██╗██║     ██║██║   ██║██╔════╝██╔══██╗%s\n' "$b2" "$G_RST"
    printf '   %s██║   ██║██║     ██║██║   ██║█████╗  ██████╔╝%s\n' "$b3" "$G_RST"
    printf '   %s██║   ██║██║     ██║╚██╗ ██╔╝██╔══╝  ██╔══██╗%s\n' "$b4" "$G_RST"
    printf '   %s╚██████╔╝███████╗██║ ╚████╔╝ ███████╗██║  ██║%s\n' "$b5" "$G_RST"
    printf '   %s ╚═════╝  ╚══════╝╚═╝  ╚═══╝  ╚══════╝╚═╝  ╚═╝%s\n' "$b6" "$G_RST"
    printf '   %sI S   T H E%s\n' "${C_DIM}${G_BOLD}" "$G_RST"
    printf '   %s ██████╗  ██████╗  █████╗ ████████╗%s\n' "$g1" "$G_RST"
    printf '   %s██╔════╝ ██╔═══██╗██╔══██╗╚══██╔══╝%s\n' "$g2" "$G_RST"
    printf '   %s██║  ███╗██║   ██║███████║   ██║%s   🐐\n' "$g3" "$G_RST"
    printf '   %s██║   ██║██║   ██║██╔══██║   ██║%s\n' "$g4" "$G_RST"
    printf '   %s╚██████╔╝╚██████╔╝██║  ██║   ██║%s\n' "$g5" "$G_RST"
    printf '   %s ╚═════╝  ╚═════╝ ╚═╝  ╚═╝   ╚═╝%s\n' "$g6" "$G_RST"
    printf '   %sv%s%s %s· frontier-scale local LLMs on MIT Engaging · Slurm handled for you%s\n\n' \
      "${C_GOLD}${G_BOLD}" "${v:-?}" "$G_RST" "$C_DIM" "$G_RST"
  } >&2
}

goat_mini_banner() {  # one-liner for the ollama-* CLIs:  goat_mini_banner <tool> <tag>
  printf '\n  %s🐐 %s%s %s·%s %s%s\n\n' \
    "${C_GOLD}${G_BOLD}" "$1" "$G_RST" "$C_DIM" "$G_RST" "${C_DIM}$2" "$G_RST" >&2
}

# --------------------------------------------------------------------------
# Cluster facts (verified 2026-07-01 on Engaging; override via env if they change)
# --------------------------------------------------------------------------
# Per-USER QOS caps -- a single job asking for more than this can NEVER start
# (it pends forever with reason QOSMaxGRESPerUser; there were 129 such stuck
# jobs in the queue the day this was written).
GOAT_CAP_NORMAL="${GOAT_CAP_NORMAL:-2}"     # mit_normal_gpu : max GPUs per user
GOAT_CAP_PREEMPT="${GOAT_CAP_PREEMPT:-4}"   # mit_preemptable: max GPUs per user
GOAT_PART_NORMAL="mit_normal_gpu"           # 6 h walltime cap, never preempted
GOAT_PART_PREEMPT="mit_preemptable"         # 2 d walltime cap, can be requeued
# Quick (CPU-only) jobs are submitted to ALL of these partitions at once --
# Slurm starts the job in whichever can host it first and silently skips any
# partition whose limits the request violates (e.g. 15-min mit_quicktest vs a
# 2 h chat; verified live 2026-07-02). mit_normal alone often has hundreds of
# Priority-pending jobs, so a new job there waits minutes BESIDE idle CPUs;
# the same request with mit_preemptable in the list granted in 5-13 s.
GOAT_PART_CPU="${GOAT_PART_CPU:-mit_normal,mit_quicktest,mit_preemptable}"
GOAT_WINDOW="${GOAT_WINDOW:-60}"            # seconds we give a confirmed-viable
                                            # request to actually allocate
GOAT_WINDOW_CPU="${GOAT_WINDOW_CPU:-90}"    # cap for the CPU grant -- the multi-
                                            # partition request lands in seconds
                                            # unless every CPU queue is choked

# GPU VRAM per card, for messages.
goat_gpu_vram() { case "$1" in h200) echo 141 ;; h100) echo 80 ;; l40s) echo 48 ;; a100) echo 80 ;; *) echo '?' ;; esac; }

# --------------------------------------------------------------------------
# goat_plan <model>  -- decide GPUs/CPUs/mem/partition for a model.
# Sets: G_TYPE G_N G_CPUS G_MEM G_PART G_VRAM G_FIT G_NOTE
# Env overrides GPU= PARTITION= CPUS= MEM= always win (applied at the end).
# --------------------------------------------------------------------------
_goat_tier() {  # <type> <n> <vram-estimate>
  G_TYPE="$1"; G_N="$2"; G_VRAM="$3"
  G_CPUS=$(( G_N * 8 )); [ "$G_CPUS" -gt 32 ] && G_CPUS=32
  case "$G_N" in
    1) [ "$G_TYPE" = l40s ] && G_MEM=32G || G_MEM=64G ;;
    2) G_MEM=128G ;;
    3) G_MEM=192G ;;
    *) G_MEM=256G ;;
  esac
  if [ "$G_N" -le "$GOAT_CAP_NORMAL" ]; then G_PART="$GOAT_PART_NORMAL"; else G_PART="$GOAT_PART_PREEMPT"; fi
}

goat_plan() {
  local m; m="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  G_FIT=1; G_NOTE=""

  # ---- quick mode (GOAT_CPU=1): CPU-only job, zero GPU queue ---------------
  # 16 cores + 64G fits the quick-mode MoE models with headroom and, measured,
  # still leaves plenty of viable nodes on every CPU partition. G_PART is a
  # comma LIST (see GOAT_PART_CPU above): Slurm picks the first that can start
  # the job, so one congested queue cannot hold quick mode hostage.
  if [ "${GOAT_CPU:-0}" = 1 ]; then
    G_TYPE=""; G_N=0; G_VRAM=0; G_GPU=""
    G_CPUS="${CPUS:-16}"; G_MEM="${MEM:-64G}"
    G_PART="${PARTITION:-$GOAT_PART_CPU}"
    return 0
  fi

  case "$m" in
    # ---- curated frontier models (VRAM = default q4 quant + context headroom)
    *kimi-k2*)                _goat_tier h200 5 620; G_FIT=0
                              G_NOTE="needs ~620 GB ≈ 5× H200, but the per-user cap is ${GOAT_CAP_PREEMPT} GPUs (564 GB). Closest that fit: deepseek-r1:671b, glm-5:744b." ;;
    *glm-5*|*744b*)           _goat_tier h200 4 460 ;;
    *671b*)                   _goat_tier h200 4 420 ;;
    *480b*)                   _goat_tier h200 3 300 ;;
    *scout*)                  _goat_tier h200 1 70  ;;
    *405b*|*maverick*|*llama4*) _goat_tier h200 2 250 ;;
    *8x22b*)                  _goat_tier h200 1 90  ;;
    *120b*|*90b*)             _goat_tier h200 1 80  ;;
    *65b*|*70b*|*72b*)        _goat_tier h200 1 50  ;;
    *)
      # Generic: estimate q4 VRAM from a NNNb suffix (~0.62 GB/B-param + 8 GB).
      local b est
      b="$(printf '%s' "$m" | sed -n 's/.*[^0-9]\([0-9][0-9]*\)b.*/\1/p')"
      if [ -n "$b" ]; then
        est=$(( b * 62 / 100 + 8 ))
        if   [ "$est" -le 40  ]; then _goat_tier l40s 1 "$est"
        elif [ "$est" -le 130 ]; then _goat_tier h200 1 "$est"
        elif [ "$est" -le 260 ]; then _goat_tier h200 2 "$est"
        elif [ "$est" -le 410 ]; then _goat_tier h200 3 "$est"
        elif [ "$est" -le 550 ]; then _goat_tier h200 4 "$est"
        else _goat_tier h200 4 "$est"; G_FIT=0
             G_NOTE="estimated ~${est} GB exceeds the biggest allowed request, ${GOAT_CAP_PREEMPT}× H200 = 564 GB (per-user QOS cap)."
        fi
      else
        _goat_tier l40s 1 8    # small/unknown model -> 1 plentiful L40S
      fi ;;
  esac

  # ---- environment overrides win ------------------------------------------
  [ -n "${PARTITION:-}" ] && G_PART="$PARTITION"
  [ -n "${CPUS:-}" ]      && G_CPUS="$CPUS"
  [ -n "${MEM:-}" ]       && G_MEM="$MEM"
  if [ -n "${GPU:-}" ]; then
    case "$GPU" in
      *:*) G_TYPE="${GPU%%:*}"; G_N="${GPU##*:}" ;;
      *)   G_TYPE=""; G_N="$GPU" ;;              # "-G 3" = any type
    esac
    G_FIT=1; G_NOTE=""                           # trust the explicit request...
    # ...and if it now fits the never-preempted partition, prefer that.
    if [ -z "${PARTITION:-}" ] && [ "${G_N:-1}" -le "$GOAT_CAP_NORMAL" ] 2>/dev/null; then
      G_PART="$GOAT_PART_NORMAL"
    fi
  fi

  # ...but never emit a request the QOS makes unrunnable.
  local cap
  case "$G_PART" in
    "$GOAT_PART_PREEMPT") cap="$GOAT_CAP_PREEMPT" ;;
    "$GOAT_PART_NORMAL")  cap="$GOAT_CAP_NORMAL" ;;
    *) cap="" ;;                                 # unknown partition: no cap logic
  esac
  if [ -n "$cap" ] && [ "${G_N:-1}" -gt "$cap" ] 2>/dev/null; then
    if [ "$G_PART" = "$GOAT_PART_NORMAL" ] && [ "$G_N" -le "$GOAT_CAP_PREEMPT" ] && [ -z "${PARTITION:-}" ]; then
      G_PART="$GOAT_PART_PREEMPT"                # auto-route 3-4 GPU jobs
    else
      G_FIT=0
      G_NOTE="${G_NOTE:-${G_N} GPUs exceeds the per-user cap on ${G_PART} (${cap}). Such jobs pend forever with QOSMaxGRESPerUser.}"
    fi
  fi
  G_GPU="${G_TYPE:+${G_TYPE}:}${G_N}"
}

goat_gpu_label() {  # "h200:4" -> "4× H200 (141 GB each)"
  if [ -z "${G_TYPE:-}" ]; then echo "${G_N}× (cluster default type)"; else
    echo "${G_N}× $(printf '%s' "$G_TYPE" | tr '[:lower:]' '[:upper:]') ($(goat_gpu_vram "$G_TYPE") GB each)"
  fi
}

# --------------------------------------------------------------------------
# Live availability (one cheap scheduler query; no filesystem traffic)
# --------------------------------------------------------------------------
# goat_gpu_overview <partition>  -> lines: "<type> <free> <total>"
goat_gpu_overview() {
  sinfo -N -p "$1" --noheader --Format="StateCompact:12,Gres:64,GresUsed:64" 2>/dev/null | awk '
    {
      state=$1
      delete tn; delete un
      n=split($2,a,","); for(i=1;i<=n;i++) if (match(a[i], /^gpu:[A-Za-z0-9_.]+:[0-9]+/)) {
        s=substr(a[i],RSTART,RLENGTH); split(s,b,":"); tn[b[2]]+=b[3] }
      n=split($3,a,","); for(i=1;i<=n;i++) if (match(a[i], /^gpu:[A-Za-z0-9_.]+:[0-9]+/)) {
        s=substr(a[i],RSTART,RLENGTH); split(s,b,":"); un[b[2]]+=b[3] }
      usable = (state !~ /down|drain|maint|resv|inval|comp|fail|plnd|boot|unk/)
      for (t in tn) {
        total[t]+=tn[t]
        if (usable) { f=tn[t]-un[t]; if (f>0) free[t]+=f }
      }
    }
    END { for (t in total) printf "%s %d %d\n", t, free[t]+0, total[t] }' | sort
}

# goat_free_gpus <type> <partition> -> integer on stdout
goat_free_gpus() {
  local n
  n="$(goat_gpu_overview "$2" | awk -v t="$1" '$1==t{print $2}')"
  printf '%s\n' "${n:-0}"
}

# --------------------------------------------------------------------------
# Exact-fit probing: partition totals lie. A request only starts NOW if a
# SINGLE node has enough free GPUs of that exact type AND enough idle CPUs
# AND enough unallocated RAM, all at once. These helpers check exactly that.
# --------------------------------------------------------------------------
goat_mem_mb() {  # "64G" / "512M" / raw MB -> MB
  case "$1" in
    *[Gg]) echo $(( ${1%[Gg]} * 1024 )) ;;
    *[Mm]) echo "${1%[Mm]}" ;;
    *)     echo "$1" ;;
  esac
}

# One sinfo call per partition per launch, cached in a shell variable so the
# candidate hunt below doesn't hammer the scheduler.
_goat_sinfo_nodes() {  # <partition> -> node table (cached)
  local part="$1" var
  var="GOAT_SINFO_$(printf '%s' "$part" | tr -c 'A-Za-z0-9' '_')"; var="${var%_}"
  if [ -z "${!var+x}" ]; then
    printf -v "$var" '%s' "$(sinfo -N -p "$part" --noheader \
      --Format="NodeList:20,StateCompact:12,CPUsState:16,Memory:10,AllocMem:10,Gres:64,GresUsed:64" 2>/dev/null)"
  fi
  printf '%s\n' "${!var}"
}

# goat_viable <type> <n> <cpus> <mem> <partition[,partition...]>
#   -> "<count> <up to 3 example nodes>"  (nodes that can host the job NOW;
#      a comma list probes them all -- nodes in several partitions count once)
goat_viable() {
  local memmb; memmb="$(goat_mem_mb "$4")"
  _goat_sinfo_nodes "$5" | awk -v t="$1" -v need="$2" -v cpus="$3" -v memmb="$memmb" '
    NF < 7 { next }
    seen[$1]++ { next }
    $2 ~ /down|drain|maint|resv|inval|comp|fail|plnd|boot|unk/ { next }
    {
      split($3, c, "/"); idle = c[2] + 0
      tot = 0; used = 0
      n = split($6, a, ","); for (i=1; i<=n; i++) if (match(a[i], "^gpu:" t ":[0-9]+")) {
        split(substr(a[i], RSTART, RLENGTH), b, ":"); tot += b[3] }
      n = split($7, a, ","); for (i=1; i<=n; i++) if (match(a[i], "^gpu:" t ":[0-9]+")) {
        split(substr(a[i], RSTART, RLENGTH), b, ":"); used += b[3] }
      if (tot - used >= need && idle >= cpus && $4 - $5 >= memmb) {
        cnt++; if (cnt <= 3) ex = ex " " $1
      }
    }
    END { printf "%d%s\n", cnt + 0, ex }'
}

# goat_held_gpus <partition> -> GPUs my RUNNING jobs already hold there.
# (The per-user cap counts these: holding 2 on mit_normal_gpu means a new
#  1-GPU job there pends even if the whole partition is empty.)
goat_held_gpus() {
  squeue --me --noheader -t running -p "$1" -o '%b' 2>/dev/null | \
    awk -F: '/gpu/ { s += $NF + 0 } END { print s + 0 }'
}

# goat_candidates -> lines "type n partition", best first, deduped.
# Uses G_TYPE/G_N/G_PART/G_VRAM from goat_plan. Ladder:
#   1. the planned request
#   2. the same request on the preemptable partition
#   3. other GPU types that hold the model within each partition's QOS cap
#      (H200 -> H100 -> L40S; drained/absent types cost nothing, the viability
#       probe just skips them)
goat_candidates() {
  local seen=" " t per n p cap line
  _gc_emit() {
    line="$1 $2 $3"
    case "$seen" in *" $line "*) return ;; esac
    seen="${seen}${line} "
    printf '%s\n' "$line"
  }
  _gc_emit "${G_TYPE:-l40s}" "$G_N" "$G_PART"
  if [ "$G_PART" = "$GOAT_PART_NORMAL" ] && [ "$G_N" -le "$GOAT_CAP_PREEMPT" ]; then
    _gc_emit "${G_TYPE:-l40s}" "$G_N" "$GOAT_PART_PREEMPT"
  fi
  for p in "$GOAT_PART_NORMAL" "$GOAT_PART_PREEMPT"; do
    [ "$p" = "$GOAT_PART_NORMAL" ] && cap="$GOAT_CAP_NORMAL" || cap="$GOAT_CAP_PREEMPT"
    for t in h200 h100 l40s; do
      per="$(goat_gpu_vram "$t")"
      n=$(( (${G_VRAM:-8} + per - 1) / per )); [ "$n" -lt 1 ] && n=1
      [ "$n" -le "$cap" ] && _gc_emit "$t" "$n" "$p"
    done
  done
  unset -f _gc_emit
}

# --------------------------------------------------------------------------
# Start-time forecasts -- Slurm's backfill scheduler will predict when a
# hypothetical request would start (srun --test-only: one scheduler RPC,
# nothing is submitted). Estimates assume running jobs use their full
# walltime, so they are upper bounds that move earlier as jobs end -- and
# they ignore per-user QOS caps and can be far too pessimistic when a node
# is actually free (the live probe stays the source of truth for "now").
# Consulted only when the hunt finds nothing free: a SHORT forecast means
# the scheduler already has a slot reserved, so waiting for it beats
# failing. Single partition only -- multi-partition forecasts are bogus
# (Slurm evaluates just one of them).
# --------------------------------------------------------------------------
GOAT_ETA_WAIT="${GOAT_ETA_WAIT:-120}"      # queue instead of failing when the
                                           # best forecast is <= this (s; 0=off)
GOAT_ETA_PROBES="${GOAT_ETA_PROBES:-4}"    # max candidates to ask Slurm about
GOAT_ETA_TIMEOUT="${GOAT_ETA_TIMEOUT:-8}"  # per-forecast budget (s)

goat_eta() {  # goat_eta <partition> <cpus> <mem> <time> [gpu-type] [n]
  # echoes "<seconds-from-now> <timestamp>" or fails if Slurm has no forecast
  local part="$1" cpus="$2" mem="$3" tlim="$4" gt="${5:-}" gn="${6:-0}"
  local gres=() out ts abs rel
  case "$part" in *,*) return 1 ;; esac
  [ "${gn:-0}" -gt 0 ] && gres=(-G "$gt:$gn")
  out="$(timeout "$GOAT_ETA_TIMEOUT" srun --test-only -p "$part" "${gres[@]}" \
         -c "$cpus" --mem="$mem" -t "$tlim" true 2>&1)" || return 1
  ts="$(printf '%s\n' "$out" | sed -n 's/.*to start at \([0-9T:-]*\).*/\1/p' | head -1)"
  [ -z "$ts" ] && return 1
  abs="$(date -d "$ts" +%s 2>/dev/null)" || return 1
  rel=$(( abs - $(date +%s) )); [ "$rel" -lt 0 ] && rel=0
  printf '%s %s\n' "$rel" "$ts"
}

goat_eta_human() {  # seconds -> "right now" / "~5 m" / "~1 h 05 m" / "~1 d 2 h"
  local s="$1"
  if   [ "$s" -le 60 ];    then printf 'right now\n'
  elif [ "$s" -lt 3600 ];  then printf 'in ~%d m\n' $(( (s + 59) / 60 ))
  elif [ "$s" -lt 86400 ]; then printf 'in ~%d h %02d m\n' $(( s / 3600 )) $(( s % 3600 / 60 ))
  else                          printf 'in ~%d d %d h\n' $(( s / 86400 )) $(( s % 86400 / 3600 ))
  fi
}

# --------------------------------------------------------------------------
# goat_preflight <model> <default-time>
# Runs goat_plan, prints the colored launch card, refuses unrunnable requests.
# Sets G_TIME and G_FREE in addition to goat_plan's variables.
# --------------------------------------------------------------------------
goat_preflight() {
  local model="$1" dtime="$2" ptag=""
  goat_plan "$model"
  G_TIME="${TIME:-$dtime}"

  if [ "$G_FIT" != 1 ]; then
    goat_err "cannot run ${model}: ${G_NOTE}"
    goat_dim "override at your own risk with GPU=<type:n> PARTITION=<p> (not recommended)"
    return 2
  fi

  # ---- quick mode: CPU-only card, no GPU bookkeeping -----------------------
  if [ "${G_N:-0}" -eq 0 ]; then
    local cres ccnt pdisp
    cres="$(goat_viable cpu 0 "$G_CPUS" "$G_MEM" "$G_PART")"; ccnt="${cres%% *}"
    G_FREE="$ccnt"
    case "$G_PART" in
      *,*) pdisp="${G_PART//,/ · }  ${C_DIM}all at once — first to grant wins${G_RST}" ;;
      *)   pdisp="$G_PART" ;;
    esac
    goat_hr
    goat_kv "model"     "${G_BOLD}${model}${G_RST}  ${C_DIM}CPU inference — no GPU needed${G_RST}"
    goat_kv "gpus"      "${C_GOLD}none — quick mode${G_RST}"
    goat_kv "partition" "$pdisp"
    goat_kv "resources" "${G_CPUS} CPUs · ${G_MEM} RAM · up to ${G_TIME}"
    if [ "${ccnt:-0}" -gt 0 ]; then
      goat_kv "free now" "${C_OK}${ccnt} node(s) can host this${G_RST} ${C_DIM}→ starts in seconds${G_RST}"
    else
      goat_kv "free now" "${C_WARN}no node has ${G_CPUS} CPUs + ${G_MEM} free${G_RST} ${C_DIM}→ try CPUS=8 MEM=32G${G_RST}"
    fi
    goat_hr
    return 0
  fi

  G_FREE="$(goat_free_gpus "${G_TYPE:-l40s}" "$G_PART")"
  [ "$G_PART" = "$GOAT_PART_PREEMPT" ] && \
    ptag="  ${C_MAG}⚡ preemptable${G_RST}"

  goat_hr
  goat_kv "model"     "${G_BOLD}${model}${G_RST}  ${C_DIM}~${G_VRAM} GB VRAM (q4)${G_RST}"
  goat_kv "gpus"      "${C_GOLD}$(goat_gpu_label)${G_RST}"
  goat_kv "partition" "${G_PART}${ptag}"
  goat_kv "resources" "${G_CPUS} CPUs · ${G_MEM} RAM · up to ${G_TIME}"
  if [ "${G_FREE:-0}" -ge "$G_N" ]; then
    goat_kv "free now" "${C_OK}${G_FREE}× ${G_TYPE:-l40s} free${G_RST} ${C_DIM}→ likely starts in seconds${G_RST}"
  else
    goat_kv "free now" "${C_WARN}only ${G_FREE}× ${G_TYPE:-l40s} free${G_RST} ${C_DIM}→ may queue${G_RST}"
  fi
  goat_hr

  # If the fast partition looks full but the preemptable one has room, say so
  # up front (measured: preemptable H200s often allocate in seconds).
  if [ "${G_FREE:-0}" -lt "$G_N" ] && [ "$G_PART" = "$GOAT_PART_NORMAL" ] \
     && [ -n "${G_TYPE:-}" ] && [ -z "${PARTITION:-}" ]; then
    local pfree
    pfree="$(goat_free_gpus "$G_TYPE" "$GOAT_PART_PREEMPT")"
    if [ "${pfree:-0}" -ge "$G_N" ]; then
      goat_dim "tip: ${pfree}× ${G_TYPE} free on ${GOAT_PART_PREEMPT} — prefix with PARTITION=${GOAT_PART_PREEMPT} to use them now (⚡ preemptable)"
    fi
  fi

  if [ "$G_PART" = "$GOAT_PART_PREEMPT" ]; then
    goat_warn "preemptable partition: a higher-priority job can requeue this one — save work often"
  fi
  # Warn if an explicit GPU= override is too small for the model.
  local per cap_gb
  per="$(goat_gpu_vram "${G_TYPE:-l40s}")"
  if [ "$per" != "?" ] && [ "${G_VRAM:-0}" -gt $(( per * G_N )) ] 2>/dev/null; then
    goat_warn "~${G_VRAM} GB model vs ${per}×${G_N}=$(( per * G_N )) GB of VRAM — it may not fit or will run partly on CPU"
  fi
}

# --------------------------------------------------------------------------
# goat_launch <mode> <self> <model> <default-time> [args...]
# The login-node half of ollama-chat / ollama-code: preflight, HUNT for a
# request that can start right now (exact node-level fit, walking the
# fallback ladder of GPU types/partitions), then srun it back onto the GPU
# node with --immediate. Never sits blindly in a queue unless WAIT=1.
# --pty only when we actually have a terminal.
# --------------------------------------------------------------------------
goat_launch() {
  local mode="$1" self="$2" model="$3" dtime="$4"; shift 4

  goat_preflight "$model" "$dtime" || exit $?
  export GOAT_GPUS="${G_GPU:-}"   # the REPL banner shows what we ended up with

  local extra=()
  if [ -t 0 ] && [ -t 1 ]; then extra+=(--pty); fi
  [ "$mode" = code ] && extra+=(--chdir "$PWD")

  # -G only when the plan actually wants GPUs (quick mode wants none)
  local gres=()
  [ "${G_N:-0}" -gt 0 ] && gres=(-G "$G_GPU")

  # ---- WAIT=1: the old behavior — queue indefinitely, no hunting ----------
  if [ "${WAIT:-0}" = 1 ]; then
    local weta
    if weta="$(goat_eta "$G_PART" "$G_CPUS" "$G_MEM" "$G_TIME" "${G_TYPE:-}" "${G_N:-0}")"; then
      goat_step "WAIT=1 — Slurm forecasts start $(goat_eta_human "${weta%% *}") (upper bound; moves earlier as jobs finish) ..."
    fi
    goat_step "WAIT=1 — queueing until the resources free up (Ctrl-C to give up) ..."
    goat_dim "srun -p $G_PART ${gres[*]:-} -c $G_CPUS --mem=$G_MEM -t $G_TIME"
    goat_say ""
    srun -p "$G_PART" "${gres[@]}" -c "$G_CPUS" --mem="$G_MEM" -t "$G_TIME" \
         -J "goat-${mode}" "${extra[@]}" "$self" "$model" "$@"
    return $?
  fi

  # ---- quick mode: no GPU ladder — the multi-partition request IS the hunt:
  # Slurm starts it in the first listed partition that can host it, so one
  # congested queue (mit_normal, routinely 100+ Priority-pending jobs) cannot
  # starve us while another has idle CPUs. -----------------------------------
  if [ "${G_N:-0}" -eq 0 ]; then
    local qres qcnt rc=0
    qres="$(goat_viable cpu 0 "$G_CPUS" "$G_MEM" "$G_PART")"; qcnt="${qres%% *}"
    if [ "${qcnt:-0}" -eq 0 ]; then
      goat_err "no CPU node in ${G_PART//,/ or } has ${G_CPUS} CPUs + ${G_MEM} free right now."
      goat_dim "  · CPUS=8 MEM=32G ...              a smaller footprint usually fits"
      goat_dim "  · WAIT=1 ...                      queue until one frees up"
      return 1
    fi
    goat_step "CPU-only — ${qcnt} node(s) across ${G_PART//,/ + } can host this now → fetching (up to ${GOAT_WINDOW_CPU}s) ..."
    case "$G_PART" in *preempt*) goat_dim \
      "may land on ⚡ mit_preemptable — rarely, a higher-priority job can requeue it" ;; esac
    goat_dim "srun -p $G_PART -c $G_CPUS --mem=$G_MEM -t $G_TIME"
    goat_say ""
    srun -p "$G_PART" -c "$G_CPUS" --mem="$G_MEM" -t "$G_TIME" \
         -J "goat-${mode}" --immediate="$GOAT_WINDOW_CPU" "${extra[@]}" "$self" "$model" "$@" || rc=$?
    if [ "$rc" -ne 0 ]; then
      goat_say ""
      goat_err "no allocation within ${GOAT_WINDOW_CPU}s (someone likely grabbed the node first) or the job failed."
      goat_dim "  · re-run me — the probe re-checks and picks whatever is free then"
    fi
    return "$rc"
  fi

  # ---- hunt: find the best request with a node that can host it NOW -------
  local plan_type="${G_TYPE:-l40s}" plan_n="$G_N" plan_part="$G_PART"
  local ctype cn cpart cap held res cnt nodes found=0
  goat_step "hunting for an exact fit that can start right now ..."

  # Explicit GPU=/PARTITION= overrides mean: this request only, no ladder.
  local cands
  if [ -n "${GPU:-}" ] || [ -n "${PARTITION:-}" ]; then
    cands="$plan_type $plan_n $plan_part"
  else
    cands="$(goat_candidates)"
  fi

  while read -r ctype cn cpart; do
    # my own QOS headroom: GPUs held by my running jobs count against the cap
    case "$cpart" in
      "$GOAT_PART_PREEMPT") cap="$GOAT_CAP_PREEMPT" ;;
      *)                    cap="$GOAT_CAP_NORMAL" ;;
    esac
    held="$(goat_held_gpus "$cpart")"
    if [ $(( held + cn )) -gt "$cap" ]; then
      goat_dim "· ${ctype}:${cn} on ${cpart} — your jobs already hold ${held} GPU(s) there (cap ${cap}) — skipping"
      continue
    fi

    # re-size CPUs/RAM for THIS candidate (explicit CPUS=/MEM= still win)
    _goat_tier "$ctype" "$cn" "$G_VRAM"; G_PART="$cpart"
    [ -n "${CPUS:-}" ] && G_CPUS="$CPUS"
    [ -n "${MEM:-}" ]  && G_MEM="$MEM"

    res="$(goat_viable "$ctype" "$cn" "$G_CPUS" "$G_MEM" "$cpart")"
    cnt="${res%% *}"; nodes="${res#"$cnt"}"; nodes="${nodes# }"
    if [ "${cnt:-0}" -eq 0 ]; then
      goat_dim "· ${ctype}:${cn} on ${cpart} — no single node has ${cn}× ${ctype} + ${G_CPUS} CPUs + ${G_MEM} free — next"
      continue
    fi

    found=1
    goat_ok "${ctype}:${cn} on ${cpart} — ${cnt} node(s) can host this now (e.g. ${nodes%% *}) → fetching"
    break
  done <<<"$cands"

  # ---- nothing free NOW: ask Slurm's scheduler when each option WOULD start.
  # A short forecast means a slot is already reserved for a request this
  # shape — waiting those few seconds beats failing and re-running by hand.
  if [ "$found" != 1 ]; then
    goat_say ""
    goat_err "nothing that fits ${model} can start within the next minute — not queueing blindly."
    local eta rel best_rel=-1 best_type="" best_n="" best_part="" best_cpus="" best_mem="" probes=0
    goat_step "asking Slurm's scheduler when each option would start ..."
    while read -r ctype cn cpart; do
      [ "$probes" -ge "$GOAT_ETA_PROBES" ] && break
      case "$cpart" in
        "$GOAT_PART_PREEMPT") cap="$GOAT_CAP_PREEMPT" ;;
        *)                    cap="$GOAT_CAP_NORMAL" ;;
      esac
      held="$(goat_held_gpus "$cpart")"
      [ $(( held + cn )) -gt "$cap" ] && continue
      _goat_tier "$ctype" "$cn" "$G_VRAM"
      [ -n "${CPUS:-}" ] && G_CPUS="$CPUS"
      [ -n "${MEM:-}" ]  && G_MEM="$MEM"
      probes=$(( probes + 1 ))
      if eta="$(goat_eta "$cpart" "$G_CPUS" "$G_MEM" "$G_TIME" "$ctype" "$cn")"; then
        rel="${eta%% *}"
        goat_dim "· ${ctype}:${cn} on ${cpart} — Slurm forecasts start $(goat_eta_human "$rel")"
        if [ "$best_rel" -lt 0 ] || [ "$rel" -lt "$best_rel" ]; then
          best_rel="$rel"; best_type="$ctype"; best_n="$cn"; best_part="$cpart"
          best_cpus="$G_CPUS"; best_mem="$G_MEM"
        fi
      else
        goat_dim "· ${ctype}:${cn} on ${cpart} — Slurm offers no forecast"
      fi
    done <<<"$cands"

    if [ "$best_rel" -ge 0 ] && [ "$GOAT_ETA_WAIT" -gt 0 ] && [ "$best_rel" -le "$GOAT_ETA_WAIT" ]; then
      local win=$(( best_rel + 90 ))
      G_GPU="${best_type}:${best_n}"; G_PART="$best_part"
      G_CPUS="$best_cpus"; G_MEM="$best_mem"
      export GOAT_GPUS="$G_GPU"
      goat_ok "Slurm expects ${best_n}× ${best_type} on ${best_part} $(goat_eta_human "$best_rel") — queueing for that slot (up to ${win}s, Ctrl-C to give up)"
      [ "$best_part" = "$GOAT_PART_PREEMPT" ] && \
        goat_warn "preemptable partition: a higher-priority job can requeue this one — save work often"
      goat_dim "srun -p $G_PART -G $G_GPU -c $G_CPUS --mem=$G_MEM -t $G_TIME"
      goat_say ""
      local rce=0
      srun -p "$G_PART" -G "$G_GPU" -c "$G_CPUS" --mem="$G_MEM" -t "$G_TIME" \
           -J "goat-${mode}" --immediate="$win" "${extra[@]}" "$self" "$model" "$@" || rce=$?
      if [ "$rce" -ne 0 ]; then
        goat_say ""
        goat_err "the forecast slot did not materialize within ${win}s — estimates shift as jobs end early or new ones arrive."
        goat_dim "  · re-run me — the hunt re-probes and re-asks the scheduler"
        goat_dim "  · WAIT=1 ollama-${mode} ...          queue until GPUs free up"
      fi
      return "$rce"
    fi

    if [ "$best_rel" -ge 0 ]; then
      goat_dim "  soonest per Slurm: ${best_n}× ${best_type} on ${best_part} $(goat_eta_human "$best_rel") — queue for exactly that:"
      goat_dim "  · WAIT=1 GPU=${best_type}:${best_n} PARTITION=${best_part} ollama-${mode} ${model}"
    fi
    goat_dim "  · oliveristhegoat status          see what is free right now"
    goat_dim "  · WAIT=1 ollama-${mode} ...          queue until GPUs free up"
    goat_dim "  · pick a smaller model            (e.g. llama3.3:70b → 1× H200)"
    return 1
  fi

  G_GPU="${ctype}:${cn}"
  export GOAT_GPUS="$G_GPU"
  if [ "$ctype" != "$plan_type" ] || [ "$cn" != "$plan_n" ]; then
    goat_warn "plan was ${plan_n}× ${plan_type} — switching to ${cn}× ${ctype} ($(( cn * $(goat_gpu_vram "$ctype") )) GB VRAM, model needs ~${G_VRAM} GB) because it can start now"
  fi
  if [ "$cpart" = "$GOAT_PART_PREEMPT" ] && [ "$plan_part" != "$GOAT_PART_PREEMPT" ]; then
    goat_warn "preemptable partition: a higher-priority job can requeue this one — save work often"
  fi

  goat_step "requesting ${cn}× $(printf '%s' "$ctype" | tr '[:lower:]' '[:upper:]') — confirmed viable, allowing ${GOAT_WINDOW}s to allocate ..."
  goat_dim "srun -p $G_PART -G $G_GPU -c $G_CPUS --mem=$G_MEM -t $G_TIME"
  goat_say ""

  local rc=0
  srun -p "$G_PART" -G "$G_GPU" -c "$G_CPUS" --mem="$G_MEM" -t "$G_TIME" \
       -J "goat-${mode}" --immediate="$GOAT_WINDOW" "${extra[@]}" "$self" "$model" "$@" || rc=$?

  if [ "$rc" -ne 0 ]; then
    goat_say ""
    goat_err "no allocation within ${GOAT_WINDOW}s (someone likely grabbed the node first) or the job failed."
    goat_dim "  · re-run me — the hunt re-probes and picks whatever is free then"
    goat_dim "  · WAIT=1 ollama-${mode} ...          queue until GPUs free up"
  fi
  return "$rc"
}

# --------------------------------------------------------------------------
# Compute-node helpers (used after srun lands us on the GPU node)
# --------------------------------------------------------------------------
# goat_ensure_server -- start `ollama serve` on our per-job port if needed.
goat_ensure_server() {
  command -v ollama >/dev/null 2>&1 || {
    goat_err "'ollama' not found — the module failed to load. Ask orcd-help-engaging@mit.edu."
    return 1
  }
  ollama list >/dev/null 2>&1 && return 0

  mkdir -p "$HOME/ollama-logs"
  GOAT_SERVE_LOG="$HOME/ollama-logs/serve.${SLURM_JOB_ID:-local}.log"
  ollama serve >"$GOAT_SERVE_LOG" 2>&1 &
  GOAT_SERVE_PID=$!
  trap 'kill "$GOAT_SERVE_PID" 2>/dev/null || true' EXIT   # frees VRAM/GPU on exit

  local i
  for i in $(seq 1 90); do
    if ollama list >/dev/null 2>&1; then
      goat_ok "ollama server up on ${OLLAMA_HOST} (${i}s)"
      return 0
    fi
    [ -t 2 ] && printf '\r  %s▸%s starting ollama server ... %ss ' "${C_GOLD}${G_BOLD}" "$G_RST" "$i" >&2
    sleep 1
  done
  [ -t 2 ] && printf '\n' >&2
  goat_err "ollama server did not come up within 90s — log: $GOAT_SERVE_LOG"
  tail -n 5 "$GOAT_SERVE_LOG" >&2 2>/dev/null || true
  return 1
}

# goat_cpu_tune <model> -- CPU-only jobs MUST pin the thread count: ollama
# defaults num_thread to ALL of the node's cores (96 here), but our cgroup
# only grants the cores we asked for. Measured on node1612: default = 0.03
# tok/s (96 threads thrashing 16 cores), num_thread=16 = 123 tok/s. There is
# no CLI/env override for `ollama run`, so derive a tag pinned to our core
# count -- it shares the base model's blobs, so creation is instant and tiny.
# Echoes the model name to use (falls back to the base name on any failure).
goat_cpu_tune() {
  local base="$1" n derived mf
  n="${SLURM_CPUS_PER_TASK:-16}"
  case "$base" in *:*) derived="${base}-cpu${n}" ;; *) derived="${base}:cpu${n}" ;; esac
  if ! ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qxF "$derived"; then
    mf="$(mktemp)"
    printf 'FROM %s\nPARAMETER num_thread %s\n' "$base" "$n" >"$mf"
    if ! ollama create "$derived" -f "$mf" >/dev/null 2>&1; then
      rm -f "$mf"; echo "$base"; return 0
    fi
    rm -f "$mf"
    goat_ok "pinned inference to the ${n} CPU cores we own (shared ${base} weights)"
  fi
  echo "$derived"
}

# goat_ensure_model <model> -- pull once; blobs persist on pool for all jobs.
goat_ensure_model() {
  local model="$1" want="$1"
  case "$want" in *:*) ;; *) want="${want}:latest" ;; esac
  if ollama list 2>/dev/null | awk 'NR>1{print $1}' | grep -qxF -e "$model" -e "$want"; then
    return 0
  fi
  goat_step "downloading ${model} — one-time; it persists on your pool space"
  ollama pull "$model"
}

# --------------------------------------------------------------------------
# self-update -- every oliveristhegoat run asks GitHub for the tip commit of
# this clone's branch (git ls-remote: one tiny request, no CDN cache, so a
# push is seen immediately -- raw.githubusercontent lags up to 5 min) and
# fast-forwards when GitHub has commits we don't. Local commits GitHub does
# not have yet (development) are NOT "out of date". Offline, non-clone
# copies, and GOAT_UPDATE=0 skip quietly -- an update check must never
# stand between you and a GPU.
# Return codes: 0 updated · 1 already current · 2 check skipped/unreachable
#               3 remote is newer but the pull failed (local edits?)
# --------------------------------------------------------------------------
GOAT_HOME="${GOAT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

goat_self_update() {
  [ "${GOAT_UPDATE:-1}" = 0 ] && return 2
  command -v git >/dev/null 2>&1 || return 2
  git -C "$GOAT_HOME" rev-parse --is-inside-work-tree >/dev/null 2>&1 || return 2
  local branch remote_sha local_sha local_v
  branch="$(git -C "$GOAT_HOME" rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 2
  [ "$branch" = HEAD ] && return 2   # detached checkout — leave it alone
  remote_sha="$(GIT_TERMINAL_PROMPT=0 timeout "${GOAT_UPDATE_TIMEOUT:-5}" \
    git -C "$GOAT_HOME" ls-remote --quiet origin "refs/heads/$branch" 2>/dev/null \
    | awk '{print $1; exit}')" || return 2
  [ -z "$remote_sha" ] && return 2
  local_sha="$(git -C "$GOAT_HOME" rev-parse HEAD 2>/dev/null)" || return 2
  [ "$remote_sha" = "$local_sha" ] && return 1
  if git -C "$GOAT_HOME" cat-file -e "$remote_sha" 2>/dev/null \
     && git -C "$GOAT_HOME" merge-base --is-ancestor "$remote_sha" HEAD 2>/dev/null; then
    return 1   # we are strictly ahead of GitHub (unpushed work) — nothing to pull
  fi
  local_v="$(cat "$GOAT_HOME/VERSION" 2>/dev/null || true)"
  goat_step "GitHub has a newer version than ${G_BOLD}v${local_v:-?}${G_RST} — updating…"
  if GIT_TERMINAL_PROMPT=0 git -C "$GOAT_HOME" pull --ff-only --quiet >/dev/null 2>&1; then
    goat_ok "updated to ${G_BOLD}v$(cat "$GOAT_HOME/VERSION" 2>/dev/null || echo '?')${G_RST} ($(git -C "$GOAT_HOME" rev-parse --short HEAD 2>/dev/null))"
    return 0
  fi
  goat_warn "could not fast-forward (local edits or diverged history?)"
  goat_dim  "  inspect with:  git -C $GOAT_HOME status"
  return 3
}
