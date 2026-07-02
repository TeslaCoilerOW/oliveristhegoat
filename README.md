# 🐐 oliveristhegoat — local LLMs on Engaging

Run a frontier-scale local LLM on H200 GPUs in **one word**. All the cluster
details — Slurm, GPU sizing, per-user quotas, storage, networking — are handled
for you, in full color.

---

## Setup — do this once

```bash
~/ollama-on-engaging/deliverables/setup.sh
```

That's the whole setup. It puts the commands below on your PATH.

## The one word

```bash
oliveristhegoat
```

Run that from a login node. It shows a menu (chat / code / serve / status /
models), a curated model list **with live availability** — each entry shows
whether it starts now or will queue — and then launches the right tool.
Nothing to memorize.

Skip the menus by giving the answers as arguments:

```bash
oliveristhegoat chat   deepseek-r1:671b
oliveristhegoat code   qwen3-coder:480b
oliveristhegoat serve  llama3.1:405b
oliveristhegoat status            # free GPUs right now · your jobs · storage
oliveristhegoat models            # the curated list + what's already on disk
```

Everything below is what `oliveristhegoat` calls under the hood — use the
commands directly whenever you like.

---

## How much GPU can you actually get? (measured, not vibes)

The cluster's QOS caps **every user** — these are hard scheduler limits,
verified on 2026-07-01:

| Partition | Max GPUs/user | Max CPUs | Max RAM | Walltime | Preemption |
|---|---|---|---|---|---|
| `mit_normal_gpu` | **2** | 32 | 515 GB | 6 h | never |
| `mit_preemptable` | **4** | 1024 | 4 TB | 2 d | can be requeued |

So the biggest possible requests are **2× H200 = 282 GB** VRAM on the fast
partition and **4× H200 = 564 GB** on the preemptable one. A job asking for
more (e.g. `h200:8`) is *accepted* and then **pends forever** with reason
`QOSMaxGRESPerUser` — there were 129 such stuck jobs in the queue the day this
was measured. These tools therefore never make such a request: 1–2 GPU models
go to `mit_normal_gpu`, 3–4 GPU models are routed to `mit_preemptable`, and
anything bigger is refused with an explanation.

**The exact-fit hunt.** Every launch probes the cluster node by node: a
request can only start *now* if a single node has the free GPUs of the exact
type **and** enough idle CPUs **and** enough unallocated RAM together
(partition-wide "free GPU" counts hide nodes whose CPUs/RAM are exhausted).
The launcher walks a fallback ladder — planned request, same GPUs on the
preemptable partition, then the next-best GPU type that still fits the model
(H200 → H100 → L40S) — and only submits a request confirmed to be hostable,
allowing it **60 seconds** to allocate. If nothing that fits can start within
the next minute, it says so and stops instead of queueing blindly.
`WAIT=1 ollama-chat ...` opts into queueing indefinitely instead.

**Quick vs powerful.** The chat/code menus ask right after you pick a mode:

- **Quick** — a CPU-only job (no GPU request at all; 16 cores + 64 GB asked
  of `mit_normal`, `mit_quicktest` and `mit_preemptable` **at the same time**
  — Slurm starts it in the first partition that can host it and skips any
  whose limits the request exceeds, so it grants in seconds even when
  `mit_normal` alone has hundreds of higher-priority jobs pending, which is
  routine). Runs the most powerful models that need no GPU: **`gpt-oss:20b`** for chat
  and **`qwen3-coder:30b`** for code. Both are MoE with ~3B *active* params —
  the biggest models that stay usable on shared CPU cores (~1–2 tok/s; a
  dense 70B would be ~10× slower). Inference is automatically pinned to the
  cores the job owns (`num_thread`), without which ollama spawns a thread per
  *node* core (96) and throughput collapses ~100×. Force quick mode anywhere
  with `GOAT_CPU=1 ollama-chat ...`.
- **Powerful** — the curated frontier models on GPUs via the exact-fit hunt.

**The allocation is held between prompts.** After `ollama-chat` answers a
first prompt it keeps the node and drops into interactive chat, so follow-up
prompts cost zero queue time (`/bye` frees it). The coding CLI already works
this way. Piped/scripted runs — or `ONESHOT=1` — answer once and exit.

## Chat

```bash
ollama-chat                                   # small default model
ollama-chat deepseek-r1:671b                  # frontier model on 4× H200
ollama-chat llama3.3:70b "Explain RAG briefly."   # answer, then hold for more
```

The curated list (largest published sizes — this is a cluster, use it):

| Model | Params | ~VRAM (q4) | Auto-request | Partition |
|---|---|---|---|---|
| `deepseek-r1:671b` | 671B | ~420 GB | `h200:4` | preemptable |
| `glm-5:744b` | 744B | ~460 GB | `h200:4` | preemptable |
| `qwen3-coder:480b` | 480B MoE | ~300 GB | `h200:3` | preemptable |
| `llama4:maverick` | 400B MoE | ~250 GB | `h200:2` | fast (6 h) |
| `llama3.1:405b` | 405B | ~250 GB | `h200:2` | fast (6 h) |
| `llama3.3:70b` | 70B | ~50 GB | `h200:1` | fast (6 h) |
| `llama3.2:3b` | 3B | ~3 GB | `l40s:1` | fast (6 h) |

Ollama shards a model across the allocated GPUs automatically — no config.
Anything else on <https://ollama.com/library> works too; unknown models are
sized by their `NNNb` tag.

> **Why no kimi-k2.7-code?** At ~1T params (~620 GB, q4) it needs 5+ H200s,
> and the per-user cap is 4 GPUs = 564 GB. That request can never start, so
> we don't offer it. Closest that fit: `deepseek-r1:671b`, `glm-5:744b`.

## Code — the **Engaging Coder** CLI

Run **from your project directory** on a login node:

```bash
ollama-code                          # default: qwen3-coder:480b
ollama-code qwen3-coder:480b "add tests for utils.py"
ollama-code llama3.3:70b             # smaller/faster, never preempted
```

It grabs the right GPUs, serves the model, and drops you into **Engaging
Coder** — a bundled agentic coding CLI with a Claude Code / Codex-style
interface:

```
╭──────────────────────────────────────────────────────────────────╮
│ ▓▒░ ENGAGING CODER                  local · on-cluster · agentic │
│                                                                  │
│ model  qwen3-coder:480b                                          │
│ node   node1247  ·  3× H200                                      │
│ dir    ~/myproject                                               │
╰──────────────────────────────────────────────────────────────────╯
  /help for commands · /exit to quit and free the GPU

› refactor the parser and run the tests

● qwen3-coder:480b
I'll read the parser, make the change, and run pytest.
  ⚙ edit  src/parser.py
  ✓ - def parse(s): ...
    + def parse(s: str) -> AST: ...   (1x)
  ⚙ run   pytest -q
  ✓ 12 passed in 3.4s
    [exit 0]
Done — tightened the parser and all tests pass.
```

The model can **read, write, and edit** your files and **run shell commands on
the GPU node** — every edit and command asks for your approval (toggle with
`/auto`). It streams replies, shows a thinking spinner, and supports slash
commands (`/help`, `/add <file>`, `/run <cmd>`, `/auto`, `/clear`, `/exit`).
Exit to free the GPUs.

> **Zero install** — Engaging Coder is pure-Python (stdlib only). Prefer a
> different front-end? `CODER=aider ollama-code` uses [aider](https://aider.chat)
> instead (any OpenAI-compatible CLI works via the exported `/v1` endpoint).

## Serve — an API for your laptop, notebook, or scripts

```bash
ollama-serve llama3.1:405b
```

Submits a batch job that keeps running after you log out, pre-pulls the
model(s), and **prints the exact SSH tunnel command** for your laptop. The
model is then at `http://127.0.0.1:11434`, including an OpenAI-compatible
endpoint at `/v1` — see `examples/` for Python and curl clients. On the
preemptable partition the job is submitted with `--requeue`, so the server
comes back by itself if it's ever preempted. Stop it (and free the GPUs) with
the printed `scancel <jobid>`.

---

## Colors

The whole system is colorful when `oliveristhegoat` is called — banner, menus,
launch cards, the coding CLI — with graceful fallback (truecolor → 256 → 16 →
plain) and standard opt-outs: `NO_COLOR=1` or `GOAT_COLOR=0`. Piped output
stays clean: UI goes to stderr, model answers to stdout.

## Staying up to date (automatic)

This directory is a git clone of
[TeslaCoilerOW/oliveristhegoat](https://github.com/TeslaCoilerOW/oliveristhegoat).
Every `oliveristhegoat` run asks GitHub for the branch's tip commit (one
tiny request, ~0.3 s, no CDN lag) and compares it with the local clone:

- **GitHub is newer** → the clone fast-forwards itself and re-runs your
  exact command as the new version. Updating is **mandatory**: if the pull
  is blocked (edited files, diverged history), it refuses to start rather
  than run stale code.
- **Same commit, or local is ahead** (unpushed work) → starts normally.
- **GitHub unreachable** (e.g. an offline compute node) → starts normally.

`oliveristhegoat update` checks on demand; `GOAT_UPDATE=0` skips the check
for one run (emergency bypass). To ship a new version: commit, bump
`VERSION`, push — everyone picks it up on their next invocation.

## Tuning (env vars)

| Variable | Effect | Example |
|---|---|---|
| `GPU=` | override the GPU request | `GPU=h200:2 ollama-chat glm-5:744b` |
| `PARTITION=` | pick the partition yourself | `PARTITION=mit_preemptable` |
| `CPUS=` `MEM=` `TIME=` | resources & walltime | `TIME=05:59:00` |
| `WAIT=1` | queue indefinitely instead of hunting | `WAIT=1 ollama-chat ...` |
| `GOAT_CPU=1` | quick mode: CPU-only job, no GPU | `GOAT_CPU=1 ollama-chat gpt-oss:20b` |
| `ONESHOT=1` | answer once and exit (don't hold the node) | `ONESHOT=1 ollama-chat ... "q"` |
| `GOAT_CPU_CHAT_MODEL=` `GOAT_CPU_CODE_MODEL=` | quick-mode model picks | default `gpt-oss:20b` / `qwen3-coder:30b` |
| `GOAT_WINDOW_CPU=` | allocation window for CPU jobs (s) | default `90` |
| `GOAT_PART_CPU=` | quick-mode partitions, asked all at once (first to grant wins) | default `mit_normal,mit_quicktest,mit_preemptable` |
| `CODER=` | coding front-end (`engaging`/`aider`/any) | `CODER=aider ollama-code` |
| `GOAT_UPDATE=0` | skip the GitHub self-update check this run | `GOAT_UPDATE=0 oliveristhegoat …` |
| `GOAT_UPDATE_TIMEOUT=` | seconds allowed for the update check | default `5` |
| `GOAT_ETA_WAIT=` | when nothing is free, auto-queue if Slurm forecasts a start within this many seconds (`0` = always fail fast) | default `120` |
| `GOAT_ETA_PROBES=` | max fallback options to ask the scheduler about | default `4` |

Overrides are still checked against the QOS caps — a request that can never
start is refused, not submitted.

## Good to know

- **Never run models on a login node.** These commands make sure everything
  heavy happens on a GPU compute node — that's the one rule on the cluster.
- **First download is one-time.** Models are saved to your `pool` space and
  reused by every future run.
- **Free the GPU when done.** `ollama-chat` frees it when you `/bye`; the
  coding CLI when you `/exit`; for `ollama-serve`, run the printed
  `scancel <jobid>`. Check what you're holding with `oliveristhegoat status`.

## If something goes wrong

| Symptom | Fix |
|---|---|
| `command not found` | Run `setup.sh` (above), then open a new shell. |
| "nothing that fits can start within the next minute" | The hunt probed every viable GPU type/partition and found no node with the GPUs + CPUs + RAM free together. It then asks Slurm's scheduler when each option *would* start: an imminent forecast (≤ `GOAT_ETA_WAIT`, default 120 s) is queued for automatically; otherwise the forecasts are printed with an exact `WAIT=1 GPU=… PARTITION=…` command for the soonest one. Forecasts are upper bounds — jobs usually end early. |
| "no allocation within 60s (someone likely grabbed the node first)" | The node was free when probed but taken before the request landed. Just re-run — the hunt re-probes and picks whatever is free then. |
| A job pends with `QOSMaxGRESPerUser` / `QOSMaxCpuPerUserLimit` | It asked for more than the per-user cap (2 GPUs / 32 CPUs on `mit_normal_gpu`) and will never start — `scancel` it. The goat tools never submit such requests. |
| Model answers slowly / on CPU | Model too big for the requested VRAM — use the auto-sizing (no `GPU=` override) or a bigger request. |
| Quick (CPU) mode feels slow | Expected ceiling is ~1–2 tok/s: the CPU nodes are shared and memory-bandwidth-bound. Threads are already pinned to your cores (`<model>-cpu16` tag). First load of a big model from pool can take minutes; held sessions keep it in RAM between prompts. |
| Session died on `mit_preemptable` | You were preempted by a priority job. `ollama-serve` requeues itself; for chat/code just relaunch — the model is already on disk. |
| "must update before it runs" | GitHub has a newer version but `git pull --ff-only` failed — local edits or diverged history in the clone. Run the printed `git status`, stash/commit your changes (or `git reset --hard origin/main` to discard them), or bypass once with `GOAT_UPDATE=0`. |

**Full reference and background:** see the main [`../README.md`](../README.md).
Cluster help: `orcd-help-engaging@mit.edu`.
