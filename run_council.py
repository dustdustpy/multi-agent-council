"""Run council directly from command line with in-place ANSI display."""
import asyncio
import copy
import os
import re
import sys
import time
from pathlib import Path

# Enable ANSI on Windows + fix encoding
os.system("")
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Colors ────────────────────────────────────────────────────
R = "\033[0m"
B = "\033[1m"
D = "\033[2m"
RED = "\033[91m"
GRN = "\033[92m"
YLW = "\033[93m"
BLU = "\033[94m"
MGT = "\033[95m"
CYN = "\033[96m"

LOG_FILE = "council_run.log"
_log_f = None


def _log(msg: str):
    if _log_f:
        _log_f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        _log_f.flush()


def _short(model: str, n: int = 24) -> str:
    name = model.split("/")[-1]
    return name[:n - 1] + "~" if len(name) > n else name


# ── In-place display ─────────────────────────────────────────
class Display:
    """
    Fixed-slot ANSI display. Layout:

      [1/4] EXPLORE  3 agents | 1/3 done | 68s        <- slot 0  (phase)
      ----------------------------------------------------------   <- slot 1  (sep)
      #01  claude-opus-4-6          OK  Done! 11 sugg  <- slot 2  (agent 0)
      #02  claude-sonnet-4-5        <>  Streaming 5k   <- slot 3  (agent 1)
      #03  gpt-5.3-codex            ->  Calling LLM    <- slot 4  (agent 2)
      Syn  claude-opus-4-6          ..  waiting         <- slot 5  (synth)
      ----------------------------------------------------------   <- slot 6  (sep2)
      Tokens: 3,644 in / 20,696 out  |  267s           <- slot 7  (footer)
      <cursor>                                          <- bottom
    """

    def __init__(self, num_agents: int, models: list[str], synth_model: str):
        self.n = num_agents
        self.models = models
        self.names = [_short(m) for m in models]
        self.synth_name = _short(synth_model)
        # slots: phase, sep, agent*N, synth, sep2, footer = N + 5
        self.total_slots = num_agents + 5
        self.start = time.monotonic()
        self.tokens_in = 0
        self.tokens_out = 0
        self._lock = asyncio.Lock()

    def _ts(self) -> str:
        e = time.monotonic() - self.start
        m, s = divmod(int(e), 60)
        return f"{m:02d}:{s:02d}"

    def init(self):
        """Print initial empty slots."""
        # phase
        print(f"  {D}[--:--] waiting...{R}")
        # sep
        print(f"  {D}{'- ' * 33}{R}")
        # agents
        for i in range(self.n):
            print(f"  {D}#{i+1:02d}  {self.names[i]:24s}  ..  waiting{R}")
        # synth
        print(f"  {D}Syn  {self.synth_name:24s}  ..  waiting{R}")
        # sep2
        print(f"  {D}{'- ' * 33}{R}")
        # footer
        print(f"  {D}0 tok in / 0 tok out{R}")
        # cursor line (we count from here)

    def _write_slot(self, slot: int, text: str):
        """Overwrite a slot line in-place using ANSI escape."""
        lines_up = self.total_slots - slot
        sys.stdout.write(f"\033[{lines_up}A")   # up
        sys.stdout.write(f"\r\033[K{text}")      # clear + write
        sys.stdout.write(f"\033[{lines_up}B")   # back down
        sys.stdout.write("\r")
        sys.stdout.flush()

    async def set_phase(self, text: str):
        async with self._lock:
            line = f"  {B}{YLW}[{self._ts()}]{R} {B}{text}{R}"
            self._write_slot(0, line)
            _log(f"PHASE: {text}")

    async def set_agent(self, idx: int, status: str, level: str = "info"):
        async with self._lock:
            name = self.names[idx]
            colors = {"info": BLU, "read": CYN, "call": YLW, "stream": MGT,
                      "parse": BLU, "ok": GRN, "err": RED}
            icons = {"info": "..", "read": ">>", "call": "->", "stream": "<>",
                     "parse": "..", "ok": "OK", "err": "XX"}
            c = colors.get(level, "")
            ic = icons.get(level, "  ")
            # truncate status
            maxw = 42
            st = status[:maxw - 3] + "..." if len(status) > maxw else status
            line = f"  {c}{ic}{R}  {B}#{idx+1:02d}{R}  {D}{name:24s}{R}  {c}{st}{R}"
            self._write_slot(idx + 2, line)  # +2 for phase+sep
            _log(f"  #{idx+1} ({name}): [{level}] {status}")

    async def set_synth(self, status: str, level: str = "info"):
        async with self._lock:
            colors = {"info": MGT, "ok": GRN, "err": RED, "stream": MGT}
            icons = {"info": "..", "ok": "OK", "err": "XX", "stream": "<>"}
            c = colors.get(level, "")
            ic = icons.get(level, "  ")
            maxw = 42
            st = status[:maxw - 3] + "..." if len(status) > maxw else status
            line = f"  {c}{ic}{R}  {B}Syn{R}  {D}{self.synth_name:24s}{R}  {c}{st}{R}"
            self._write_slot(self.n + 2, line)  # agents end at n+1, synth = n+2
            _log(f"  Synth: [{level}] {status}")

    async def set_footer(self, text: str = ""):
        async with self._lock:
            if not text:
                text = (f"Tokens: {self.tokens_in:,} in / {self.tokens_out:,} out  "
                        f"|  {self._ts()}")
            line = f"  {D}{text}{R}"
            self._write_slot(self.total_slots - 1, line)

    def finish_display(self):
        """Move cursor below display and print final results."""
        print()  # move past cursor line


# ── Progress callback ─────────────────────────────────────────
def make_progress_cb(display: Display):
    """Route engine progress messages to display slots."""
    last_stream: dict[int, float] = {}
    STREAM_INTERVAL = 3.0

    async def cb(msg: str):
        msg = msg.strip()

        # [N/4] phase header
        m = re.match(r"\[(\d)/4\]\s*(.*)", msg)
        if m:
            step = int(m.group(1))
            detail = m.group(2)
            names = {1: "EXPLORE", 2: "SYNTHESIZE", 3: "VOTE", 4: "COMPILE"}
            phase = f"[{step}/4] {names.get(step, '?')}"

            if "Quorum" in detail or "complete" in detail or "voted" in detail:
                await display.set_phase(f"{phase}  {detail}")
            else:
                await display.set_phase(f"{phase}  {detail}")
                # Reset agent slots for new phase (vote/explore)
                if step in (1, 3):
                    for i in range(display.n):
                        await display.set_agent(i, "waiting...", "info")
                if step == 2:
                    await display.set_synth("waiting...", "info")
            await display.set_footer()
            return

        # Agent: "  #N (model): status"
        m = re.match(r"\s*#(\d+)\s*\(.+\):\s*(.*)", msg)
        if m:
            idx = int(m.group(1)) - 1
            status = m.group(2)

            if idx < 0 or idx >= display.n:
                return

            if "Reading" in status:
                await display.set_agent(idx, status, "read")
            elif "Calling LLM" in status or "Voting on" in status:
                await display.set_agent(idx, status, "call")
            elif "Streaming" in status:
                now = time.monotonic()
                if idx in last_stream and (now - last_stream[idx]) < STREAM_INTERVAL:
                    _log(f"  #{idx+1}: {status}")
                    return
                last_stream[idx] = now
                await display.set_agent(idx, status, "stream")
            elif "Parsing" in status:
                await display.set_agent(idx, status, "parse")
            elif "Done!" in status:
                await display.set_agent(idx, status, "ok")
                # Extract tokens
                tok = re.search(r"\[(\d[\d,]*)\s*in\s*\+\s*(\d[\d,]*)\s*out", status)
                if tok:
                    display.tokens_in += int(tok.group(1).replace(",", ""))
                    display.tokens_out += int(tok.group(2).replace(",", ""))
                    await display.set_footer()
            elif "FAIL" in status or "ERROR" in status:
                await display.set_agent(idx, status, "err")
            elif "votes cast" in status:
                await display.set_agent(idx, status, "ok")
            elif "Vote FAIL" in status:
                await display.set_agent(idx, status, "err")
            elif "Smart-truncated" in status:
                await display.set_agent(idx, status, "read")
            elif "Round" in status or "Max tool" in status:
                await display.set_agent(idx, status, "info")
            else:
                await display.set_agent(idx, status, "info")
            return

        # Synthesizer: "  Synthesizer (model): status"
        m = re.match(r"\s*Synthesizer\s*\([^)]+\):\s*(.*)", msg)
        if m:
            status = m.group(1)
            if "Streaming" in status:
                now = time.monotonic()
                if -1 in last_stream and (now - last_stream[-1]) < STREAM_INTERVAL:
                    _log(f"  Synth: {status}")
                    return
                last_stream[-1] = now
                await display.set_synth(status, "stream")
            elif "Done!" in status:
                await display.set_synth(status, "ok")
            elif "FAIL" in status:
                await display.set_synth(status, "err")
            else:
                await display.set_synth(status, "info")
            return

        # Models line / other
        if msg.startswith("Models:"):
            await display.set_footer(msg)
            return

        # Fallback to footer
        if msg:
            await display.set_footer(msg[:64])

    return cb


# ── Main ──────────────────────────────────────────────────────
async def main():
    global _log_f

    from council.config import load_config
    from council.engine import CouncilEngine
    from council.file_reader import read_paths_async
    from council.formatters.markdown import format_report
    from council.llm.factory import LLMClientFactory
    from council.project_indexer import index_project, build_context_for_tier
    from council.security import set_denylist_keys

    # Parse args
    request = sys.argv[1] if len(sys.argv) > 1 else "Review this code and suggest improvements"
    file_paths = sys.argv[2] if len(sys.argv) > 2 else "D:\\AI\\highcode\\council\\tools.py"
    mode = sys.argv[3] if len(sys.argv) > 3 else "quick"

    # Init log
    _log_f = open(LOG_FILE, "w", encoding="utf-8")
    _log(f"Council started | {request} | {file_paths} | {mode}")

    config = load_config()
    keys = []
    for m in config.council.members:
        try:
            keys.append(m.resolved_api_key())
        except Exception:
            pass
    set_denylist_keys(keys)

    # Quick mode
    if mode == "quick":
        n = config.settings.quick_council_size
        config = copy.deepcopy(config)
        config.council.members = config.council.members[:n]

    members = config.council.members
    models = [m.model for m in members]
    synth_model = config.council.synthesizer.model

    # Header
    print(f"\n{B}{CYN}Council Review v2.3.0{R}  "
          f"{B}{'QUICK' if mode == 'quick' else 'FULL'}{R} ({len(members)} agents)")
    print(f"  {D}{request[:70]}{R}")
    print(f"  {D}{file_paths[:70]}{R}")

    # Load context
    print(f"\n  {D}Loading context...{R}", end="", flush=True)
    t0 = time.monotonic()
    paths = [p.strip() for p in file_paths.split(",") if p.strip()]
    folder_paths = [p for p in paths if Path(p).expanduser().resolve().is_dir()]
    file_only_paths = [p for p in paths if not Path(p).expanduser().resolve().is_dir()]

    files_content = ""
    project_index = None

    if folder_paths:
        primary = Path(folder_paths[0]).expanduser().resolve()
        project_index = await asyncio.to_thread(index_project, primary)
        files_content = await asyncio.to_thread(build_context_for_tier, project_index)
        if file_only_paths or len(folder_paths) > 1:
            extra = await read_paths_async(file_only_paths + folder_paths[1:])
            if extra:
                files_content += "\n\n## Additional Files\n" + extra
        dt = time.monotonic() - t0
        print(f"\r  {GRN}OK{R} Context: {len(files_content):,} chars "
              f"| tier={project_index.tier} | {project_index.total_files} files | {dt:.1f}s")
    elif file_only_paths:
        files_content = await read_paths_async(file_only_paths)
        dt = time.monotonic() - t0
        print(f"\r  {GRN}OK{R} Context: {len(files_content):,} chars | {dt:.1f}s")
    else:
        print(f"\r  {YLW}!!{R} No files")

    # Init display
    print()
    display = Display(len(members), models, synth_model)
    display.init()

    progress_cb = make_progress_cb(display)

    # Run
    factory = LLMClientFactory()
    engine = CouncilEngine(config, client_factory=factory)

    try:
        agent_results, suggestions, votes, final = await engine.run(
            request, files_content, progress_cb, project_index
        )
    except Exception as e:
        display.finish_display()
        print(f"\n  {RED}ERROR: {e}{R}")
        if _log_f:
            _log_f.close()
        return

    elapsed = time.monotonic() - display.start
    display.tokens_in = engine.total_in
    display.tokens_out = engine.total_out
    await display.set_footer()

    display.finish_display()

    # Save report
    report = format_report(
        request, agent_results, suggestions, votes, final,
        engine.total_in, engine.total_out, engine.total_cached, elapsed,
    )
    out_dir = Path("council_reports")
    out_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"council_{ts}_{len(suggestions)}suggestions.md"
    out_file.write_text(report, encoding="utf-8")

    # Final
    print(f"{B}{GRN}  DONE!{R}  {B}{len(suggestions)}{R} suggestions  "
          f"|  {engine.total_in:,}+{engine.total_out:,} tok  "
          f"|  {elapsed:.0f}s ({elapsed/60:.1f}m)")
    print(f"  Report: {CYN}{out_file}{R}")
    print(f"  Log:    {D}{LOG_FILE}{R}\n")

    _log(f"Done: {len(suggestions)} suggestions, {elapsed:.0f}s")
    if _log_f:
        _log_f.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n{YLW}Interrupted{R}")
