"""Small offline skill/target editor used by the existing local control panel."""
from copy import deepcopy
import tkinter as tk
from tkinter import messagebox, ttk

from scripts.larc_panel_setup import parse_arc_skill_policy
from scripts.larc_skill_choices import library, search_choices

DISTANCES = {"未指定": None, "短距离": "short", "英里": "mile", "中距离": "middle", "长距离": "long"}
STYLES = {"未指定": None, "领跑": "leader", "跟前": "front", "居中": "betweener", "后追": "chaser"}
SURFACES = {"未指定": None, "草地": "turf", "泥地": "dirt"}


def edit_arc_skill_policy(parent, policy):
    """Return a policy on Save, or None on Cancel. Never touches a live run."""
    original = deepcopy(policy or {})
    target = original.get("final_target", {})
    result = []
    dialog = tk.Toplevel(parent)
    dialog.title("技能学习 · 最终用途与提前清单")
    dialog.geometry("840x640")
    dialog.transient(parent)
    enabled = tk.BooleanVar(value=original.get("enabled", False))
    selected = {library().get(item)["skill_id"]: library().get(item)["name_zh_cn"]
                for item in original.get("classic_allowlist", [])}
    frame = ttk.Frame(dialog, padding=14)
    frame.pack(fill="both", expand=True)
    ttk.Checkbutton(frame, text="启用两次凯旋门赛前学习，并在赛后补学", variable=enabled).pack(anchor="w")
    ttk.Label(frame, text="这里填写最后要参加的比赛用途；育成中的凯旋门不会替换这个目标。",
              wraplength=800).pack(anchor="w", pady=8)
    target_row = ttk.Frame(frame)
    target_row.pack(fill="x")
    variables = {}
    for key, label, choices in (("distance_type", "距离", DISTANCES), ("style", "跑法", STYLES), ("surface", "场地", SURFACES)):
        ttk.Label(target_row, text=label).pack(side="left", padx=(0, 6))
        value = next((name for name, code in choices.items() if code == target.get(key)), "未指定")
        variable = tk.StringVar(value=value)
        variables[key] = (variable, choices)
        ttk.Combobox(target_row, textvariable=variable, values=list(choices), state="readonly", width=10).pack(side="left", padx=(0, 18))
    ttk.Label(frame, text="经典年：只学清单内已解锁、有折扣且符合用途的技能。第三年：按最终用途正常选择。",
              wraplength=800).pack(anchor="w", pady=10)
    query = tk.StringVar()
    row = ttk.Frame(frame)
    row.pack(fill="x")
    ttk.Entry(row, textvariable=query).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="搜索技能", command=lambda: search()).pack(side="left", padx=6)
    tree = ttk.Treeview(frame, columns=("name", "fit", "description"), show="headings", selectmode="extended", height=9)
    for name, title, width in (("name", "中文名称", 170), ("fit", "目标条件", 95), ("description", "效果与限制", 490)):
        tree.heading(name, text=title)
        tree.column(name, width=width)
    tree.pack(fill="both", expand=True, pady=8)
    ttk.Button(frame, text="加入提前清单 ↓", command=lambda: add()).pack(anchor="w")
    chosen = tk.Listbox(frame, height=5, selectmode="extended", exportselection=False)
    chosen.pack(fill="x", pady=7)
    ttk.Button(frame, text="移出选中技能", command=lambda: remove()).pack(anchor="w")
    note = tk.StringVar(value="")
    ttk.Label(frame, textvariable=note, wraplength=800).pack(anchor="w", pady=7)

    def final_target():
        value = deepcopy(target)
        for key, (variable, choices) in variables.items():
            item = choices[variable.get()]
            if item is None:
                value.pop(key, None)
            else:
                value[key] = item
        # A changed category invalidates an old precise distance; do not silently
        # keep contradictory facts from a previous build.
        if value.get("distance_type") != target.get("distance_type"):
            value.pop("distance_m", None)
        return value

    def search():
        tree.delete(*tree.get_children())
        rows = search_choices(query.get(), target=final_target(), limit=200)
        for skill in rows["items"]:
            tree.insert("", "end", iid=skill["skill_id"], values=(skill["name"],
                "无已知冲突" if skill["target_compatible"] else "不符合目标", skill["description"]))
        note.set(f"匹配 {rows['total']} 项，显示前 {len(rows['items'])} 项。折扣和实际价格在局内读取；此处不代表已解锁。")

    def refresh_selected():
        chosen.delete(0, "end")
        for name in selected.values():
            chosen.insert("end", name)

    def add():
        for identity in tree.selection():
            selected[identity] = library().get(identity)["name_zh_cn"]
        refresh_selected()

    def remove():
        identities = list(selected)
        for index in chosen.curselection():
            selected.pop(identities[index], None)
        refresh_selected()

    def save():
        try:
            result.append(parse_arc_skill_policy({"enabled": enabled.get(), "final_target": final_target(),
                                                  "classic_allowlist": list(selected)}))
        except ValueError as exc:
            messagebox.showerror("请检查技能设置", str(exc), parent=dialog)
            return
        dialog.destroy()

    buttons = ttk.Frame(frame)
    buttons.pack(fill="x")
    ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right")
    ttk.Button(buttons, text="保存设置", command=save).pack(side="right", padx=8)
    refresh_selected()
    search()
    dialog.grab_set()
    parent.wait_window(dialog)
    return result[0] if result else None
