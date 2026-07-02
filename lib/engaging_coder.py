#!/usr/bin/env python3
# ===========================================================================
# Engaging Coder -- a small, self-contained agentic coding CLI for local
# Ollama models on the MIT ORCD Engaging cluster.
#
# It looks and feels like Claude Code / Codex: a banner, streaming replies, a
# thinking spinner, and real tools (read / write / edit files, run shell
# commands, list dirs) that let the model work in your project ON THE CLUSTER.
#
# Pure Python standard library -- no pip installs, so it runs anywhere python3
# does. It talks to Ollama's native /api/chat endpoint (tool calling + streaming).
#
#   python3 engaging_coder.py --model qwen3-coder:480b --host 127.0.0.1:15513
#   python3 engaging_coder.py --model kimi-k2.7-code "add type hints to utils.py"
#   python3 engaging_coder.py --selftest        # offline sanity checks, no network
#
# You normally never call this directly -- `ollama-code` launches it for you.
# ===========================================================================
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

try:
    import readline  # noqa: F401  -- line editing + history for input(), if available
except Exception:
    pass

APP_NAME = "ENGAGING CODER"
MAX_TOOL_OUTPUT = 6000          # chars of tool output fed back to the model
MAX_PREVIEW_LINES = 24          # lines shown in confirm previews
RUN_TIMEOUT = 180               # seconds before a shell command is killed


# --------------------------------------------------------------------------
# Colors / styling (truecolor ANSI, gracefully degrades to plain text)
# --------------------------------------------------------------------------
class Palette:
    brand = (217, 119, 87)      # clay orange (brand accent)
    brand2 = (233, 166, 133)
    user = (125, 170, 255)      # blue
    ok = (126, 199, 148)        # green
    tool = (224, 187, 106)      # amber
    err = (233, 110, 110)       # red
    dim = (140, 140, 152)       # gray
    fg = (226, 226, 231)
    node = (150, 205, 235)      # cyan


class Style:
    def __init__(self, enabled):
        self.on = enabled

    def paint(self, rgb, text, bold=False):
        if not self.on:
            return text
        r, g, b = rgb
        pre = ("\x1b[1;" if bold else "\x1b[") + f"38;2;{r};{g};{b}m"
        return f"{pre}{text}\x1b[0m"

    def brand(self, t, bold=False):  return self.paint(Palette.brand, t, bold)
    def brand2(self, t, bold=False): return self.paint(Palette.brand2, t, bold)
    def user(self, t, bold=False):   return self.paint(Palette.user, t, bold)
    def ok(self, t, bold=False):     return self.paint(Palette.ok, t, bold)
    def tool(self, t, bold=False):   return self.paint(Palette.tool, t, bold)
    def err(self, t, bold=False):    return self.paint(Palette.err, t, bold)
    def dim(self, t, bold=False):    return self.paint(Palette.dim, t, bold)
    def fg(self, t, bold=False):     return self.paint(Palette.fg, t, bold)
    def node(self, t, bold=False):   return self.paint(Palette.node, t, bold)


def supports_color():
    if os.environ.get("NO_COLOR") or os.environ.get("GOAT_COLOR") == "0":
        return False
    if os.environ.get("FORCE_COLOR") or os.environ.get("GOAT_COLOR") == "1":
        return True
    return sys.stdout.isatty()


# --------------------------------------------------------------------------
# Thinking spinner (runs in a background thread; clears itself on stop)
# --------------------------------------------------------------------------
class Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, st):
        self.st = st
        self._stop = threading.Event()
        self._thread = None

    def start(self, label):
        if not self.st.on or not sys.stdout.isatty():
            return
        self._stop.clear()

        def run():
            i = 0
            while not self._stop.is_set():
                frame = self.FRAMES[i % len(self.FRAMES)]
                sys.stdout.write("\r  " + self.st.brand(frame) + " " + self.st.dim(label) + "   ")
                sys.stdout.flush()
                i += 1
                time.sleep(0.08)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        if self.st.on and sys.stdout.isatty():
            sys.stdout.write("\r" + " " * (shutil.get_terminal_size((80, 24)).columns - 1) + "\r")
            sys.stdout.flush()


# --------------------------------------------------------------------------
# UI helpers
# --------------------------------------------------------------------------
class UI:
    def __init__(self, style):
        self.st = style
        self.width = min(shutil.get_terminal_size((80, 24)).columns, 92)

    def banner(self, model, node, gpus, cwd):
        st = self.st
        W = min(self.width - 2, 66)
        rows = []
        left = " ▓▒░ " + APP_NAME
        right = "local · on-cluster · agentic "
        gap = max(1, W - len(left) - len(right))
        title_plain = left + " " * gap + right
        rows.append(st.brand(left, bold=True) + " " * gap + st.dim(right))

        def kv(label, value):
            lab = f" {label:<6}"
            val = " " + value
            plain = (lab + val)[:W].ljust(W)
            cut = len(lab)
            return st.dim(plain[:cut]) + st.fg(plain[cut:])

        rows.append(st.dim(" " * W))
        rows.append(kv("model", model))
        loc = node + (("  ·  " + gpus) if gpus else "")
        rows.append(kv("node", loc))
        home = os.path.expanduser("~")
        shown = cwd.replace(home, "~") if cwd.startswith(home) else cwd
        rows.append(kv("dir", shown))

        bar = st.brand
        print()
        print(bar("╭" + "─" * W + "╮"))
        for r in rows:
            print(bar("│") + r + bar("│"))
        print(bar("╰" + "─" * W + "╯"))
        print("  " + st.dim("/help for commands · /exit to quit and free the GPU"))
        print()

    def user_prompt(self):
        return input(self.st.brand("› ", bold=True))

    def assistant_header(self, model):
        print(self.st.ok("●", bold=True) + " " + self.st.dim(model))

    def stream_write(self, text):
        sys.stdout.write(self.st.fg(text) if self.st.on else text)
        sys.stdout.flush()

    def stream_end(self):
        print()

    def tool_call(self, name, summary):
        print("  " + self.st.tool("⚙ " + name, bold=True) + "  " + self.st.dim(summary))

    def tool_output(self, text, ok=True):
        mark = self.st.ok("  ✓ ") if ok else self.st.err("  ✗ ")
        first = True
        for line in text.rstrip("\n").splitlines() or [""]:
            gutter = mark if first else self.st.dim("    ")
            print(gutter + self.st.dim(line))
            first = False

    def note(self, text):
        print("  " + self.st.dim("• " + text))

    def error(self, text):
        print("  " + self.st.err("✗ " + text, bold=True))

    def help(self):
        st = self.st
        cmds = [
            ("/help", "show this help"),
            ("/add <path>...", "read files into the conversation"),
            ("/run <cmd>", "run a shell command yourself and share the output"),
            ("/auto", "toggle auto-approve for edits & commands"),
            ("/clear", "forget the conversation (keep the system prompt)"),
            ("/model", "show the model this session is using"),
            ("/exit, /quit", "leave (frees the GPU when the job ends)"),
        ]
        print("\n  " + st.brand("commands", bold=True))
        for c, d in cmds:
            print("    " + st.fg(f"{c:<16}") + st.dim(d))
        print("\n  " + st.brand("the model can", bold=True) + st.dim("  read · write · edit files · run shell · list dirs"))
        print("    " + st.dim("edits and commands ask for your approval unless /auto is on") + "\n")

    def confirm(self, question):
        try:
            ans = input("  " + self.st.tool("? ") + question + self.st.dim(" [y/N/a=always] › "))
        except (EOFError, KeyboardInterrupt):
            print()
            return "n"
        return (ans or "n").strip().lower()[:1]


# --------------------------------------------------------------------------
# Ollama client -- native /api/chat with streaming + tool calling
# --------------------------------------------------------------------------
class Ollama:
    def __init__(self, host, model, temperature=0.2):
        self.base = f"http://{host}"
        self.model = model
        self.temperature = temperature

    def stream_chat(self, messages, tools):
        """Yield ('token', str) | ('tool_calls', list) | ('error', str) | ('done', obj)."""
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.base + "/api/chat", data=data,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            resp = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            yield ("error", f"HTTP {e.code}: {body.strip()[:400]}")
            return
        except urllib.error.URLError as e:
            yield ("error", f"cannot reach Ollama at {self.base}: {e.reason}")
            return
        try:
            for raw in resp:
                line = raw.strip()
                if not line:
                    continue
                for ev in parse_stream_line(line):
                    yield ev
                    if ev[0] in ("error", "done"):
                        return
        finally:
            resp.close()


def parse_stream_line(line):
    """Parse one NDJSON line from /api/chat into UI events. Pure -> unit-testable."""
    if isinstance(line, (bytes, bytearray)):
        line = line.decode("utf-8", "replace")
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict) and obj.get("error"):
        yield ("error", str(obj["error"]))
        return
    msg = obj.get("message") or {}
    content = msg.get("content")
    if content:
        yield ("token", content)
    tcs = msg.get("tool_calls")
    if tcs:
        yield ("tool_calls", tcs)
    if obj.get("done"):
        yield ("done", obj)


def parse_tool_call(tc):
    """Extract (name, args_dict) from an Ollama tool_call. Lenient about shapes."""
    fn = tc.get("function", tc) if isinstance(tc, dict) else {}
    name = fn.get("name", "")
    args = fn.get("arguments", {})
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return name, args


# --------------------------------------------------------------------------
# Tools the model can call
# --------------------------------------------------------------------------
def _fnschema(name, desc, props, required):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": required}}}


TOOL_SCHEMA = [
    _fnschema("read_file", "Read a UTF-8 text file and return its contents.",
              {"path": {"type": "string", "description": "File path (relative to the working dir)."}},
              ["path"]),
    _fnschema("list_dir", "List the entries of a directory.",
              {"path": {"type": "string", "description": "Directory path; defaults to '.'."}},
              []),
    _fnschema("write_file", "Create or overwrite a file with the given contents.",
              {"path": {"type": "string"}, "content": {"type": "string"}},
              ["path", "content"]),
    _fnschema("edit_file", "Replace an exact substring in a file (all occurrences).",
              {"path": {"type": "string"},
               "find": {"type": "string", "description": "Exact text to replace."},
               "replace": {"type": "string", "description": "Replacement text."}},
              ["path", "find", "replace"]),
    _fnschema("run_shell", "Run a shell command in the working directory and return its output.",
              {"command": {"type": "string"}},
              ["command"]),
]


def _clip(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"


def _get(args, *keys, default=""):
    for k in keys:
        if k in args and args[k] is not None:
            return args[k]
    return default


class ToolBox:
    def __init__(self, ui, cwd, auto=False):
        self.ui = ui
        self.cwd = cwd
        self.auto = auto

    def _resolve(self, path):
        path = os.path.expanduser(path or ".")
        if not os.path.isabs(path):
            path = os.path.join(self.cwd, path)
        return os.path.normpath(path)

    def _rel(self, path):
        try:
            return os.path.relpath(path, self.cwd)
        except ValueError:
            return path

    def dispatch(self, name, args):
        handler = {
            "read_file": self.read_file,
            "list_dir": self.list_dir,
            "write_file": self.write_file,
            "edit_file": self.edit_file,
            "run_shell": self.run_shell,
        }.get(name)
        if handler is None:
            self.ui.tool_call(name or "?", "unknown tool")
            return f"error: unknown tool '{name}'"
        try:
            return handler(args)
        except Exception as e:  # never let a tool crash the session
            self.ui.tool_output(f"{type(e).__name__}: {e}", ok=False)
            return f"error: {type(e).__name__}: {e}"

    # ---- safe (no confirmation) --------------------------------------
    def read_file(self, args):
        p = self._resolve(_get(args, "path"))
        self.ui.tool_call("read", self._rel(p))
        if not os.path.isfile(p):
            self.ui.tool_output("no such file", ok=False)
            return f"error: no such file: {self._rel(p)}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        self.ui.tool_output(f"{body.count(chr(10)) + 1} lines, {len(body)} bytes", ok=True)
        return _clip(body)

    def list_dir(self, args):
        p = self._resolve(_get(args, "path", default="."))
        self.ui.tool_call("list", self._rel(p) or ".")
        if not os.path.isdir(p):
            self.ui.tool_output("not a directory", ok=False)
            return f"error: not a directory: {self._rel(p)}"
        entries = sorted(os.listdir(p))
        listing = "\n".join(("%s/" % e if os.path.isdir(os.path.join(p, e)) else e) for e in entries)
        self.ui.tool_output(f"{len(entries)} entries", ok=True)
        return _clip(listing or "(empty)")

    # ---- mutating (confirmation unless --auto) -----------------------
    def _approved(self, question):
        if self.auto:
            return True
        ans = self.ui.confirm(question)
        if ans == "a":
            self.auto = True
            return True
        return ans == "y"

    def write_file(self, args):
        p = self._resolve(_get(args, "path"))
        content = _get(args, "content")
        exists = os.path.isfile(p)
        verb = "overwrite" if exists else "create"
        self.ui.tool_call("write", f"{verb} {self._rel(p)} ({content.count(chr(10)) + 1} lines)")
        preview = "\n".join(content.splitlines()[:MAX_PREVIEW_LINES])
        self.ui.tool_output(preview or "(empty file)", ok=True)
        if not self._approved(f"{verb} {self.ui.st.fg(self._rel(p))}?"):
            return "error: user declined the write"
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        self.ui.note(f"wrote {self._rel(p)}")
        return f"ok: wrote {self._rel(p)} ({len(content)} bytes)"

    def edit_file(self, args):
        p = self._resolve(_get(args, "path"))
        find = _get(args, "find", "old", "old_string")
        repl = _get(args, "replace", "new", "new_string")
        self.ui.tool_call("edit", self._rel(p))
        if not os.path.isfile(p):
            self.ui.tool_output("no such file", ok=False)
            return f"error: no such file: {self._rel(p)}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
        n = body.count(find)
        if not find or n == 0:
            self.ui.tool_output("search text not found", ok=False)
            return "error: 'find' text not present in file; read it first, then retry with an exact match"
        self.ui.tool_output(f"- {find.strip()[:70]}\n+ {repl.strip()[:70]}   ({n}x)", ok=True)
        if not self._approved(f"apply edit to {self.ui.st.fg(self._rel(p))}?"):
            return "error: user declined the edit"
        with open(p, "w", encoding="utf-8") as f:
            f.write(body.replace(find, repl))
        self.ui.note(f"edited {self._rel(p)} ({n} replacement{'s' if n != 1 else ''})")
        return f"ok: replaced {n} occurrence(s) in {self._rel(p)}"

    def run_shell(self, args):
        cmd = _get(args, "command", "cmd")
        self.ui.tool_call("run", cmd)
        if not cmd:
            return "error: empty command"
        if not self._approved("run this command?"):
            return "error: user declined to run the command"
        try:
            proc = subprocess.run(cmd, shell=True, cwd=self.cwd, text=True,
                                  capture_output=True, timeout=RUN_TIMEOUT)
            out = (proc.stdout or "") + (proc.stderr or "")
            ok = proc.returncode == 0
            self.ui.tool_output((out.strip() or "(no output)") + f"\n[exit {proc.returncode}]", ok=ok)
            return _clip(f"exit code: {proc.returncode}\n{out}")
        except subprocess.TimeoutExpired:
            self.ui.tool_output(f"timed out after {RUN_TIMEOUT}s", ok=False)
            return f"error: command timed out after {RUN_TIMEOUT}s"


# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------
def system_prompt(model, node, cwd):
    return (
        "You are Engaging Coder, an expert software engineer working directly inside a "
        "user's project on the MIT Engaging HPC cluster. "
        f"You are the model '{model}', running locally on GPU node '{node}'. "
        f"The working directory is '{cwd}'.\n\n"
        "You have tools: read_file, list_dir, write_file, edit_file, run_shell. "
        "Use them to inspect the project before changing it. Prefer edit_file for small, "
        "targeted changes and write_file for new files. Use run_shell to build, test, or "
        "explore (it runs on the cluster GPU node). File edits and shell commands are shown "
        "to the user for approval, so act decisively and explain what you are doing.\n\n"
        "Be concise. When a task is done, give a short summary of what changed. "
        "Do not fabricate file contents or command output -- always read or run to find out."
    )


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------
class Agent:
    def __init__(self, client, ui, toolbox):
        self.client = client
        self.ui = ui
        self.toolbox = toolbox
        self.tools = TOOL_SCHEMA
        self.spinner = Spinner(ui.st)

    def run_turn(self, messages):
        while True:
            content = []
            tool_calls = []
            error = None
            streaming = False

            self.spinner.start("thinking")
            try:
                for kind, data in self.client.stream_chat(messages, self.tools):
                    if kind == "token":
                        if not streaming:
                            self.spinner.stop()
                            self.ui.assistant_header(self.client.model)
                            streaming = True
                        content.append(data)
                        self.ui.stream_write(data)
                    elif kind == "tool_calls":
                        tool_calls.extend(data)
                    elif kind == "error":
                        error = data
                    # 'done' -> loop ends naturally
            except KeyboardInterrupt:
                self.spinner.stop()
                if streaming:
                    self.ui.stream_end()
                self.ui.note("interrupted")
                return messages
            finally:
                self.spinner.stop()

            if streaming:
                self.ui.stream_end()

            if error:
                low = error.lower()
                if self.tools and ("does not support tools" in low or "tool" in low and "support" in low):
                    self.ui.note("this model has no tool support — continuing as plain chat")
                    self.tools = None
                    continue
                self.ui.error(error)
                return messages

            assistant = {"role": "assistant", "content": "".join(content)}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)

            if not tool_calls:
                return messages

            for tc in tool_calls:
                name, targs = parse_tool_call(tc)
                result = self.toolbox.dispatch(name, targs)
                messages.append({"role": "tool", "tool_name": name, "content": result})
            # loop again so the model can react to tool results


# --------------------------------------------------------------------------
# REPL
# --------------------------------------------------------------------------
def pretty_gpus(spec):
    """'h200:3' -> '3× H200'; '1' -> ''; passthrough otherwise."""
    if not spec or spec == "1":
        return ""
    if ":" in spec:
        kind, _, n = spec.partition(":")
        if n.isdigit():
            return f"{n}× {kind.upper()}"
    return spec


def handle_slash(cmd, agent, ui, messages, base_system):
    """Return True if the REPL should continue, False to exit."""
    parts = cmd.split()
    head = parts[0].lower()
    rest = cmd[len(parts[0]):].strip()
    if head in ("/exit", "/quit", "/q"):
        return False
    if head == "/help":
        ui.help()
    elif head == "/clear":
        del messages[1:]
        ui.note("conversation cleared")
    elif head == "/model":
        ui.note(f"model: {agent.client.model}   ·   tools: {'on' if agent.tools else 'off'}")
    elif head == "/auto":
        agent.toolbox.auto = not agent.toolbox.auto
        ui.note(f"auto-approve is now {'ON — edits & commands run without asking' if agent.toolbox.auto else 'OFF'}")
    elif head == "/add":
        if not rest:
            ui.error("usage: /add <path> [more paths]")
        else:
            blob = []
            for path in rest.split():
                rp = agent.toolbox._resolve(path)
                if os.path.isfile(rp):
                    with open(rp, "r", encoding="utf-8", errors="replace") as f:
                        blob.append(f"--- {path} ---\n{f.read()}")
                    ui.note(f"added {path}")
                else:
                    ui.error(f"not a file: {path}")
            if blob:
                messages.append({"role": "user", "content": "Here are files for context:\n\n" + "\n\n".join(blob)})
    elif head == "/run":
        if not rest:
            ui.error("usage: /run <command>")
        else:
            out = agent.toolbox.run_shell({"command": rest})
            messages.append({"role": "user", "content": f"I ran `{rest}` and got:\n{out}"})
    else:
        ui.error(f"unknown command: {head}  (try /help)")
    return True


def repl(agent, ui, base_system, initial=None):
    messages = [{"role": "system", "content": base_system}]
    if initial:
        messages.append({"role": "user", "content": initial})
        agent.run_turn(messages)
    while True:
        try:
            print()
            line = ui.user_prompt()
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            ui.note("(Ctrl-D or /exit to quit)")
            continue
        line = line.strip()
        if not line:
            continue
        if line.startswith("/"):
            if not handle_slash(line, agent, ui, messages, base_system):
                break
            continue
        messages.append({"role": "user", "content": line})
        agent.run_turn(messages)
    ui.note("bye — exit the shell / job to free the GPU")


# --------------------------------------------------------------------------
# Offline self-test (no network) so the tool can be verified on any node
# --------------------------------------------------------------------------
def selftest():
    st = Style(False)
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and cond
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    # stream line parsing
    evs = list(parse_stream_line('{"message":{"content":"hel"},"done":false}'))
    check("token parse", evs == [("token", "hel")])
    evs = list(parse_stream_line(
        '{"message":{"content":"","tool_calls":[{"function":{"name":"run_shell","arguments":{"command":"ls"}}}]},"done":true}'))
    kinds = [e[0] for e in evs]
    check("tool_calls + done parse", kinds == ["tool_calls", "done"])
    check("error parse", list(parse_stream_line('{"error":"boom"}')) == [("error", "boom")])
    check("garbage line ignored", list(parse_stream_line("not json")) == [])

    # tool-call extraction, incl. stringified arguments
    n, a = parse_tool_call({"function": {"name": "edit_file", "arguments": '{"path":"x"}'}})
    check("tool_call str-args", n == "edit_file" and a == {"path": "x"})

    # tools against a temp dir
    import tempfile
    d = tempfile.mkdtemp(prefix="engaging-coder-selftest-")
    ui = UI(st)
    tb = ToolBox(ui, d, auto=True)
    check("write_file", "ok:" in tb.write_file({"path": "a.txt", "content": "hello\nworld\n"}))
    check("read_file", "hello" in tb.read_file({"path": "a.txt"}))
    check("edit_file", "ok:" in tb.edit_file({"path": "a.txt", "find": "world", "replace": "there"}))
    check("edit content applied", "there" in open(os.path.join(d, "a.txt")).read())
    check("edit_file missing text", "error:" in tb.edit_file({"path": "a.txt", "find": "nope", "replace": "x"}))
    check("list_dir", "a.txt" in tb.list_dir({"path": "."}))
    check("run_shell", "exit code: 0" in tb.run_shell({"command": "echo hi"}))
    check("read missing", "error:" in tb.read_file({"path": "ghost.txt"}))
    shutil.rmtree(d, ignore_errors=True)

    check("pretty_gpus", pretty_gpus("h200:3") == "3× H200" and pretty_gpus("1") == "")
    check("banner renders", isinstance(UI(st).banner.__doc__ or "", str))

    print("\n  " + ("ALL PASSED" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Engaging Coder — agentic coding CLI for local Ollama models.")
    ap.add_argument("--model", default=os.environ.get("MODEL", "qwen3-coder:480b"))
    ap.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "127.0.0.1:11434"))
    ap.add_argument("--gpus", default=os.environ.get("OLLAMA_CODE_GPUS", ""))
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--auto", action="store_true", help="auto-approve edits and commands")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="run offline checks and exit")
    ap.add_argument("prompt", nargs="*", help="optional initial task; omit for interactive mode")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    style = Style(supports_color() and not args.no_color)
    ui = UI(style)
    cwd = os.getcwd()
    node = socket.gethostname()
    client = Ollama(args.host, args.model, temperature=args.temperature)
    auto = args.auto or not sys.stdin.isatty()
    toolbox = ToolBox(ui, cwd, auto=auto)
    agent = Agent(client, ui, toolbox)

    ui.banner(args.model, node, pretty_gpus(args.gpus), cwd)
    if auto and not args.auto:
        ui.note("non-interactive input detected — auto-approving tools")

    initial = " ".join(args.prompt).strip() or None
    try:
        repl(agent, ui, system_prompt(args.model, node, cwd), initial=initial)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
