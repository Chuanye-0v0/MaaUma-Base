"""Small standalone desktop control panel; opening it never starts the game."""
from pathlib import Path
import argparse
import json
import os
import sys
import uuid
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.larc_panel_runner import create_larc_control_service, submit_button
from core.auto_trainer.larc_policy import policy_version
from scripts.larc_panel_setup import (
    DEFAULT_TARGETS, DEVICES, STATS, STRATEGIES, create_panel_run,
    parse_targets, parse_strategy_options, update_goal, parse_mei_breaks, save_mei_breaks, save_ramonu_present, parse_early_skill_allowlist,
)

LABELS = {"idle": "待开始", "starting": "正在启动", "running": "运行中",
          "pause_requested": "正在暂停", "paused": "已暂停", "resume_requested": "正在继续",
          "stop_requested": "正在结束", "stopped": "已结束", "unresponsive": "任务无响应"}
ACTIVE = {"starting", "running", "pause_requested", "paused", "resume_requested", "stop_requested", "unresponsive"}


def _load_tk():
    # Native Pacer imports build_argv in the portable runtime without Tk.
    global tk, filedialog, messagebox, ttk
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk


def selected_policy_version(branch, *, new_run, configured_policy=None):
    settings = {'strategy_refinement': True, 'training_balance_v2': True} if new_run else dict(configured_policy or {})
    settings['variant'] = branch
    return policy_version(SimpleNamespace(scenario='larc', larc_policy=settings))


def build_argv(folder, limit, *, device_port=None, retain_training=None):
    root = Path(folder).resolve(strict=True)
    progress = json.loads((root / "progress.json").read_text(encoding="utf-8-sig"))
    if progress.get("run_session_id") != root.name:
        raise ValueError("所选目录与局号不一致，请选择本局的记录目录。")
    from core.cut_portable_runtime import device_ports
    ports = {f"127.0.0.1:{port}": port for port in device_ports()}
    port = ports.get(progress.get("device"))
    if port is None or device_port is not None and port != device_port:
        raise ValueError("所选记录不是当前 MuMu 安卓设备或设备 2，请核对设备。")
    for file in ("goal.json", "session/transition_session.json", "session/skip_session.json", "session/larc_member_roster.json"):
        if not (root / file).is_file():
            raise ValueError(f"本局记录尚未准备好，缺少：{file}")
    limit = int(limit)
    if not 1 <= limit <= 72:
        raise ValueError("本次最多推进回合数应为 1～72。")
    argv = ["--run-root", str(root), "--adb-port", str(port), "--max-actions", str(limit),
            "--execute", "--confirm-token", "LARC_AUTONOMY_TEST", "--allow-fixed-races", "--allow-adaptations"]
    from core.auto_trainer.larc_skill_driver import load_driver
    driver = load_driver(root)
    if driver:
        argv += ["--d1-skill-root",driver["d1_root"],"--external-skill-db-path",driver["external_skill_db_path"]]
        configured_goal = json.loads((root / "goal.json").read_text(encoding="utf-8-sig"))
        if not configured_goal.get("skill_timing", {}).get("arc_policy", {}).get("enabled"):
            argv.append("--d1-startup-only")
    if driver and driver.get("startup_skill_catalog_dir"):
        argv += ["--startup-skill-catalog-dir",driver["startup_skill_catalog_dir"]]
    if retain_training is not None:
        argv += ["--training-records", "keep" if retain_training else "discard"]
    return argv


def duration(value):
    seconds = max(0, int(value or 0))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class ControlWindow:
    def __init__(self, root, *, service_factory=create_larc_control_service, run_root="", with_ramonu=False,
                 max_actions=72, retain_training=False):
        _load_tk()
        self.root, self.service_factory = root, service_factory
        self.service = None
        self.closing = False
        self.timer = None
        self.folder = tk.StringVar(value=run_root)
        self.device = tk.StringVar(value="MuMu 安卓设备")
        self.new_run = tk.BooleanVar(value=not bool(run_root))
        self.retain_training = tk.BooleanVar(value=retain_training)
        self.strategy = tk.StringVar(value="统一基线")
        self.failure_limit = tk.StringVar(value="25")
        self.mei_breaks = tk.StringVar(value="")
        self.with_ramonu = tk.BooleanVar(value=with_ramonu)
        self.early_skill_allowlist = tk.StringVar(value="")
        self.arc_policy = {}
        self.configured_policy = {}
        self.targets = {key: tk.StringVar(value=str(value)) for key, value in DEFAULT_TARGETS.items()}
        if run_root:
            self.load_existing(run_root)
        self.limit = tk.StringVar(value=str(max_actions))
        self.status = tk.StringVar(value="待开始 · 尚未连接任务")
        self.detail = tk.StringVar(value="游戏开好一局，停在第一回合主页即可")
        self.timing = tk.StringVar(value="运行 00:00   暂停 00:00")
        self.note = tk.StringVar(value="填目标后点开始。新局记录自动建立；暂停后的开始是继续。")
        root.title("Uma 育成控制 · 后台静默版")
        root.geometry("660x490")
        root.minsize(630, 490)
        frame = ttk.Frame(root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="凯旋门 · 临时控制台", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.status, font=("Microsoft YaHei UI", 12)).pack(anchor="w", pady=(10, 2))
        ttk.Label(frame, textvariable=self.detail).pack(anchor="w")
        ttk.Label(frame, textvariable=self.timing).pack(anchor="w", pady=(3, 12))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="设备").pack(side="left", padx=(0, 6))
        self.device_combo = ttk.Combobox(row, textvariable=self.device, values=list(DEVICES), state="readonly", width=20)
        self.device_combo.pack(side="left")
        self.new_check = ttk.Checkbutton(row, text="新局（已在第一回合）", variable=self.new_run)
        self.new_check.pack(side="left", padx=12)
        stats_frame = ttk.LabelFrame(frame, text="目标属性", padding=10)
        stats_frame.pack(fill="x", pady=(0, 12))
        self.target_entries = []
        for index, (key, label) in enumerate(STATS.items()):
            cell = ttk.Frame(stats_frame)
            cell.grid(row=0, column=index, sticky="ew", padx=5)
            stats_frame.columnconfigure(index, weight=1)
            ttk.Label(cell, text=label).pack(anchor="w")
            widget = ttk.Entry(cell, textvariable=self.targets[key], width=8)
            widget.pack(fill="x", pady=(4, 0))
            self.target_entries.append(widget)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="策略").pack(side="left", padx=(0, 6))
        self.strategy_combo = ttk.Combobox(row, textvariable=self.strategy, values=list(STRATEGIES),
                                            state="readonly", width=10)
        self.strategy_combo.pack(side="left")
        ttk.Label(row, text="最高训练失败率").pack(side="left", padx=(24, 6))
        self.failure_entry = ttk.Entry(row, textvariable=self.failure_limit, width=7)
        self.failure_entry.pack(side="left")
        ttk.Label(row, text="%（超过则不选）").pack(side="left", padx=5)
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="黄帽 SSR 突破数").pack(side="left", padx=(0, 6))
        self.mei_entry = ttk.Entry(row, textvariable=self.mei_breaks, width=7)
        self.mei_entry.pack(side="left")
        ttk.Label(row, text="0～4 破，可留空；填写后按当前破数已升满计算").pack(side="left", padx=6)
        self.ramonu_check = ttk.Checkbutton(frame, text="本局带智高峰 SSR（燦爛）：启用中途实用速度技能学习",
                                            variable=self.with_ramonu)
        self.ramonu_check.pack(anchor="w", pady=(0, 12))
        ttk.Label(frame,text="高峰提前学习技能（每行一个，留空关闭）").pack(anchor="w")
        self.early_skills_text=tk.Text(frame,height=3,wrap="none",font=("Microsoft YaHei UI",10))
        self.early_skills_text.insert("1.0",self.early_skill_allowlist.get())
        self.early_skills_text.pack(fill="x",pady=(3,12))
        self.arc_skills_button = ttk.Button(frame, text="设置最终用途与凯旋门提前技能清单…", command=self.edit_arc_skills)
        self.arc_skills_button.pack(anchor="w", pady=(0, 10))
        self.retain_check = ttk.Checkbutton(frame,
            text="实验：保留每回合五页训练截图与决策，供后续复盘", variable=self.retain_training)
        self.retain_check.pack(anchor="w", pady=(0, 3))
        ttk.Label(frame, text="不勾选：停止后清理完成回合的训练截图，异常现场保留。", foreground="#666666").pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        ttk.Label(row, text="续跑记录").pack(side="left", padx=(0, 6))
        self.entry = ttk.Entry(row, textvariable=self.folder)
        self.entry.pack(side="left", fill="x", expand=True)
        self.browse = ttk.Button(row, text="选择已有记录", command=self.choose_folder)
        self.browse.pack(side="left", padx=(8, 0))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=10)
        ttk.Label(row, text="本次最多推进").pack(side="left")
        self.count = ttk.Spinbox(row, from_=1, to=72, textvariable=self.limit, width=5)
        self.count.pack(side="left", padx=5)
        ttk.Label(row, text="回合（使用本局已有策略配置）").pack(side="left")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 10))
        self.buttons = {}
        for action, title in (("start", "开始"), ("pause", "暂停"), ("stop", "结束")):
            button = ttk.Button(row, text=title, command=lambda a=action: self.command(a))
            button.pack(side="left", expand=True, fill="x", padx=3, ipady=7)
            self.buttons[action] = button
        ttk.Label(frame, textvariable=self.note, wraplength=570).pack(anchor="w")
        ttk.Label(frame, text="结束只停脚本，不放弃育成；关闭窗口时也会先停止任务。", foreground="#666666").pack(anchor="w", pady=(5, 0))
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.update_idletasks()
        required_height = max(490, frame.winfo_reqheight())
        root.geometry(f"660x{required_height}")
        root.minsize(630, required_height)
        self.refresh()

    def choose_folder(self):
        folder = filedialog.askdirectory(parent=self.root, title="选择当前这局包含 progress.json 的目录", initialdir=str(ROOT / "runs"))
        if folder:
            self.folder.set(folder)
            self.new_run.set(False)
            if not self.load_existing(folder):
                self.note.set("记录已选择，开始时会检查该记录与设备。")

    def edit_arc_skills(self):
        from scripts.larc_skill_picker import edit_arc_skill_policy
        policy = edit_arc_skill_policy(self.root, self.arc_policy)
        if policy is not None:
            self.arc_policy = policy
            self.note.set("技能设置已暂存，开始新任务时应用。经典年提前清单共 "
                          + str(len(policy.get("classic_allowlist", []))) + " 项。")

    def load_existing(self, folder):
        try:
            progress = json.loads((Path(folder) / "progress.json").read_text(encoding="utf-8-sig"))
            self.device.set(next(label for label, port in DEVICES.items() if progress["device"] == f"127.0.0.1:{port}"))
            goal = json.loads((Path(folder) / "goal.json").read_text(encoding="utf-8-sig"))
            self.configured_policy = dict(goal.get('larc_policy', {}))
            for key, variable in self.targets.items():
                variable.set(str(goal["target_stats"][key]))
            from core.auto_trainer.larc_policy import VARIANT_BRANCHES
            branch = VARIANT_BRANCHES[goal.get("larc_policy", {}).get("variant", "alpha")]
            self.strategy.set("统一基线")
            self.early_skill_allowlist.set("\n".join(goal.get("skill_timing",{}).get("early_skill_allowlist",[])))
            self.arc_policy = goal.get("skill_timing", {}).get("arc_policy", {})
            if hasattr(self,"early_skills_text"):
                previous=self.early_skills_text.cget("state")
                self.early_skills_text.configure(state="normal")
                self.early_skills_text.delete("1.0","end")
                self.early_skills_text.insert("1.0",self.early_skill_allowlist.get())
                self.early_skills_text.configure(state=previous)
            limit = goal.get("training_failure_rate_limit")
            self.failure_limit.set(str(25 if limit is None else limit))
            from core.auto_trainer.run_support_profile import load_profile
            profile = load_profile(folder, Path(folder).name)
            mei = next((card for card in (profile or {}).get("cards", [])
                        if card.get("character_id") == "satake_mei"), {})
            self.mei_breaks.set("" if mei.get("limit_breaks") is None else str(mei["limit_breaks"]))
            self.with_ramonu.set(any(str(card.get("card_id")) == "30134"
                                    for card in (profile or {}).get("cards", [])))
            return True
        except (OSError, KeyError, ValueError, StopIteration):
            return False

    def command(self, action):
        try:
            snapshot = self.service.snapshot() if self.service else {"state": "idle"}
            if action == "start" and snapshot["state"] in {"idle", "stopped"}:
                targets = parse_targets({key: value.get() for key, value in self.targets.items()})
                port = DEVICES[self.device.get()]
                strategy, threshold = parse_strategy_options(self.strategy.get(), self.failure_limit.get())
                breaks = parse_mei_breaks(self.mei_breaks.get())
                allowlist=parse_early_skill_allowlist(self.early_skills_text.get("1.0","end-1c"))
                if self.new_run.get():
                    folder = create_panel_run(device_port=port, targets=targets,
                        strategy=strategy, failure_limit=threshold, mei_breaks=breaks,
                        with_ramonu=self.with_ramonu.get(),early_skill_allowlist=allowlist, arc_policy=self.arc_policy)
                    self.folder.set(str(folder))
                    self.new_run.set(False)
                elif not self.folder.get().strip():
                    raise ValueError("新局勾选“新局”；续跑请选择这局的记录。")
                argv = build_argv(self.folder.get().strip(), self.limit.get(),
                    device_port=port, retain_training=self.retain_training.get())
                update_goal(self.folder.get().strip(), targets, strategy=strategy, failure_limit=threshold,early_skill_allowlist=allowlist, arc_policy=self.arc_policy)
                self.configured_policy = json.loads((Path(self.folder.get().strip()) / 'goal.json').read_text(encoding='utf-8-sig')).get('larc_policy', {})
                if self.arc_policy.get("enabled") and "--d1-startup-only" in argv:
                    argv.remove("--d1-startup-only")
                save_mei_breaks(self.folder.get().strip(), breaks)
                save_ramonu_present(self.folder.get().strip(), self.with_ramonu.get())
                if self.service:
                    self.service.close()
                self.service = self.service_factory(ROOT / "runs" / "standalone-panel-control" / str(port), {"current": argv})
            if self.service is None:
                return
            submit_button(self.service, action, request_id=uuid.uuid4().hex, profile_id="current")
            self.note.set("控制指令已发送；状态以任务实际响应为准。")
        except Exception as exc:
            self.note.set(str(exc))
            messagebox.showerror("暂时无法执行", str(exc), parent=self.root)
        self.render()

    def render(self):
        snapshot = self.service.snapshot() if self.service else {"state": "idle"}
        state = snapshot["state"]
        self.status.set(LABELS.get(state, state))
        runtime = snapshot.get("runtime") or {}
        branch = 'baseline' if self.new_run.get() else (self.configured_policy or {}).get('variant','alpha')
        version = selected_policy_version(branch,
            new_run=self.new_run.get(), configured_policy=self.configured_policy).rsplit('_v', 1)[-1].replace('_', '.')
        label = '统一基线' if branch=='baseline' else '旧策略记录（保存后升级，续跑保持原策略）'
        self.detail.set(f"{self.device.get()} · 当前回合 {runtime.get('turn') or '—'} · {label} {version}")
        self.timing.set(f"运行 {duration(runtime.get('active_seconds'))}   暂停 {duration(runtime.get('paused_seconds'))}")
        allowed = {"start": state in {"idle", "stopped", "paused"}, "pause": state == "running",
                   "stop": state in ACTIVE - {"stop_requested", "unresponsive"}}
        for action, button in self.buttons.items():
            button.configure(state="normal" if allowed[action] and not self.closing else "disabled")
        self.buttons["start"].configure(text="继续" if state == "paused" else "开始")
        for widget in (self.entry, self.browse, self.count, self.new_check, self.retain_check,
                       self.failure_entry, self.mei_entry, self.ramonu_check, self.arc_skills_button, *self.target_entries):
            widget.configure(state="disabled" if state in ACTIVE or self.closing else "normal")
        self.device_combo.configure(state="disabled" if state in ACTIVE or self.closing else "readonly")
        self.early_skills_text.configure(state="disabled" if state in ACTIVE or self.closing else "normal")
        self.strategy_combo.configure(state="disabled" if state in ACTIVE or self.closing else "readonly")
        if snapshot.get("last_result") and state == "stopped":
            self.note.set("任务已停止：" + str(snapshot["last_result"].get("reason", "结束")))
        elif snapshot.get("last_error"):
            self.note.set(str(snapshot["last_error"]))
        if self.closing and state not in ACTIVE:
            if self.service:
                self.service.close()
            self.root.destroy()
            return False
        return True

    def refresh(self):
        if self.render():
            self.timer = self.root.after(250, self.refresh)

    def close(self):
        snapshot = self.service.snapshot() if self.service else {"state": "idle"}
        if snapshot["state"] in ACTIVE:
            if not messagebox.askyesno("停止并关闭", "关闭窗口会停止脚本，但不会放弃游戏中的育成。确定关闭？", parent=self.root):
                return
            self.closing = True
            if snapshot["state"] not in {"stop_requested", "unresponsive"}:
                self.command("stop")
            # Also reaches the runtime when a heartbeat has timed out.
            self.service.close()
            self.note.set("正在等待任务停止，完成记录后关闭窗口……")
        else:
            if self.timer:
                self.root.after_cancel(self.timer)
            if self.service:
                self.service.close()
            self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", default="")
    parser.add_argument("--with-ramonu", action="store_true")
    parser.add_argument("--max-actions", type=int, choices=range(1, 73), default=72)
    parser.add_argument("--retain-training", action="store_true")
    args = parser.parse_args()
    os.chdir(ROOT)
    # pythonw has no console streams; retain diagnostics rather than failing on print.
    log_dir = ROOT / "runs" / "standalone-panel-control"
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    _load_tk()
    root = tk.Tk()
    ControlWindow(root, run_root=args.run_root, with_ramonu=args.with_ramonu,
                  max_actions=args.max_actions, retain_training=args.retain_training)
    root.mainloop()


if __name__ == "__main__":
    main()
