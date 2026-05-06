# -*- coding: utf-8 -*-

APP_VERSION = "v14-left400-square-cells-2026-02-11"

"""
万年历备忘录（增强修复版）
- 修复：Treeview 排序导致的编辑/删除错位（使用事件ID作为iid）
- 修复：ID 冲突（uuid）
- 修复：后台线程不直接操作 Tk UI；自动保存线程仅写磁盘
- 新增：搜索过滤 + 按优先级/状态筛选（主窗口 + 迷你窗口）
- 新增：排序切换（时间/优先级/状态/逾期优先）+ 逾期筛选
"""
import os
import json
import time
import uuid
import math
import threading
import platform
from datetime import datetime, timedelta, date

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
# -----------------------------
# 数据模型
# -----------------------------
class Event:
    """备忘录事件"""
    def __init__(self, date_str, time_str, content,
                 priority=0, repeat=0, reminder=True,
                 completed=False, event_id=None):
        self.id = event_id or uuid.uuid4().hex
        self.date = date_str          # YYYY-MM-DD
        self.time = time_str          # HH:MM
        self.content = content.strip()
        self.priority = int(priority) # 0-3
        self.repeat = int(repeat)     # 0/1/7（不重复/每天/每周）
        self.reminder = bool(reminder)
        self.completed = bool(completed)

    # ---- 兼容旧数据 ----
    @classmethod
    def from_dict(cls, d: dict):
        return cls(
            date_str=d.get("date", ""),
            time_str=d.get("time", "09:00"),
            content=d.get("content", ""),
            priority=int(d.get("priority", 0)),
            repeat=int(d.get("repeat", 0)),
            reminder=bool(d.get("reminder", True)),
            completed=bool(d.get("completed", False)),
            event_id=d.get("id") or d.get("event_id"),
        )

    def to_dict(self):
        return {
            "id": self.id,
            "date": self.date,
            "time": self.time,
            "content": self.content,
            "priority": self.priority,
            "repeat": self.repeat,
            "reminder": self.reminder,
            "completed": self.completed,
        }

    @property
    def priority_text(self):
        return {
            0: "不重要不紧急",
            1: "不重要紧急",
            2: "重要不紧急",
            3: "重要紧急",
        }.get(self.priority, "不重要不紧急")

    @property
    def priority_color(self):
        return {
            0: "#4CAF50",  # 绿
            1: "#2196F3",  # 蓝
            2: "#FFC107",  # 黄
            3: "#F44336",  # 红
        }.get(self.priority, "#4CAF50")

    def get_status(self):
        if self.completed:
            return "已完成"
        if not self.reminder:
            return "已提醒"
        return "待提醒"

    def is_today_event(self):
        return self.date == datetime.now().strftime("%Y-%m-%d")

    def get_datetime(self):
        """返回事件的 datetime；解析失败返回 None"""
        try:
            return datetime.strptime(f"{self.date} {self.time}", "%Y-%m-%d %H:%M")
        except Exception:
            return None

    def is_overdue(self, now=None):
        """逾期：未完成 且 事件时间早于当前时间"""
        if self.completed:
            return False
        dt = self.get_datetime()
        if not dt:
            return False
        now = now or datetime.now()
        return dt < now


# -----------------------------
# 提醒弹窗的小动画（可选）
# -----------------------------
class AnimatedSmile:
    def __init__(self, canvas: tk.Canvas):
        self.canvas = canvas
        self.t = 0
        self.items = []
        self.running = False

    def start(self):
        self.running = True
        self._tick()

    def stop(self):
        self.running = False

    def _tick(self):
        if not self.running:
            return
        self.canvas.delete("all")
        w = int(self.canvas["width"])
        h = int(self.canvas["height"])
        cx, cy = w // 2, h // 2
        r = min(w, h) // 2 - 6

        # 轻微抖动/呼吸
        scale = 1.0 + 0.04 * math.sin(self.t / 6.0)
        rr = int(r * scale)

        # 脸
        self.canvas.create_oval(cx-rr, cy-rr, cx+rr, cy+rr, fill="#FFEB3B", outline="")
        # 眼睛
        eye_y = cy - rr // 4
        eye_dx = rr // 3
        self.canvas.create_oval(cx-eye_dx-6, eye_y-6, cx-eye_dx+6, eye_y+6, fill="#333", outline="")
        self.canvas.create_oval(cx+eye_dx-6, eye_y-6, cx+eye_dx+6, eye_y+6, fill="#333", outline="")
        # 嘴（弧线）
        mouth_w = rr * 0.9
        mouth_h = rr * 0.7
        self.canvas.create_arc(cx-mouth_w/2, cy-mouth_h/4, cx+mouth_w/2, cy+mouth_h,
                               start=200, extent=140, style=tk.ARC, width=4, outline="#333")

        self.t += 1
        self.canvas.after(60, self._tick)


# -----------------------------
# 迷你窗口
# -----------------------------
class MiniWindow:
    def __init__(self, app):
        self.app = app
        self.window = tk.Toplevel(app.root)
        self.window.title("待办事项")
        self.window.geometry("320x520+50+50")
        self.window.attributes("-topmost", True)
        self.window.resizable(False, False)

        # 关闭：隐藏不销毁（避免主窗还在刷新它）
        self.window.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.filter_status = tk.StringVar(value="今日")  # 今日/待提醒/已提醒/已完成/全部
        self.filter_priority = tk.StringVar(value="全部")  # 全部/四象限
        self.search_var = tk.StringVar()

        # 最近一次迷你窗筛选结果中的逾期ID（用于批量操作）
        self._last_overdue_ids = []

        self._build_ui()
        self.update_todo_list()

    def on_closing(self):
        self.window.withdraw()

    def show(self):
        self.window.deiconify()
        self.window.lift()

    def _build_ui(self):
        # 顶部栏（可拖拽）
        title_frame = tk.Frame(self.window, bg="#1565C0", height=34)
        title_frame.pack(fill=tk.X)
        title_frame.pack_propagate(False)

        def start_drag(e):
            self.window._dx = e.x
            self.window._dy = e.y

        def do_drag(e):
            x = self.window.winfo_x() + (e.x - self.window._dx)
            y = self.window.winfo_y() + (e.y - self.window._dy)
            self.window.geometry(f"+{x}+{y}")

        title_frame.bind("<ButtonPress-1>", start_drag)
        title_frame.bind("<B1-Motion>", do_drag)

        tk.Label(title_frame, text="📋 待办事项", bg="#1565C0", fg="white",
                 font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT, padx=10)

        close_btn = tk.Label(title_frame, text="×", bg="#1565C0", fg="white",
                             font=("Arial", 16, "bold"), cursor="hand2")
        close_btn.pack(side=tk.RIGHT, padx=10)
        close_btn.bind("<Button-1>", lambda e: self.on_closing())

        # 状态筛选
        control_frame = tk.Frame(self.window, bg="#E3F2FD")
        control_frame.pack(fill=tk.X, padx=6, pady=6)

        status_frame = tk.Frame(control_frame, bg="#E3F2FD")
        status_frame.pack(fill=tk.X)

        for text in ["今日", "待提醒", "已提醒", "已完成", "逾期", "全部"]:
            tk.Radiobutton(status_frame, text=text, variable=self.filter_status, value=text,
                           command=self.update_todo_list, bg="#E3F2FD",
                           font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=2)

        # 逾期一键处理（迷你窗：对当前列表范围生效）
        self.overdue_menu_btn = tk.Menubutton(status_frame, text="逾期处理", relief=tk.RAISED)
        self.overdue_menu = tk.Menu(self.overdue_menu_btn, tearoff=0)
        self.overdue_menu.add_command(label="批量完成逾期（当前列表）", command=self.bulk_complete_overdue)
        self.overdue_menu.add_command(label="批量恢复提醒（当前列表）", command=self.bulk_restore_overdue)
        self.overdue_menu_btn.configure(menu=self.overdue_menu)
        self.overdue_menu_btn.pack(side=tk.RIGHT, padx=6)

        # 搜索 + 优先级
        bar = tk.Frame(self.window, bg="#FFFFFF")
        bar.pack(fill=tk.X, padx=6, pady=(0, 6))

        tk.Label(bar, text="🔎", bg="#FFFFFF").pack(side=tk.LEFT, padx=(2, 0))
        ent = tk.Entry(bar, textvariable=self.search_var, relief=tk.GROOVE)
        ent.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6, pady=6)
        ent.bind("<KeyRelease>", lambda e: self.update_todo_list())

        self.priority_combo = ttk.Combobox(
            bar, textvariable=self.filter_priority, state="readonly", width=10,
            values=["全部", "不重要不紧急", "不重要紧急", "重要不紧急", "重要紧急"]
        )
        self.priority_combo.pack(side=tk.RIGHT, padx=(0, 6))
        self.priority_combo.bind("<<ComboboxSelected>>", lambda e: self.update_todo_list())

        # 列表区域（可滚动）
        outer = tk.Frame(self.window, bg="#FFFFFF")
        outer.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        self.canvas = tk.Canvas(outer, bg="#FFFFFF", highlightthickness=0)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 使用 tk.Scrollbar 更显眼；事项多时可拖拽滚动条查看
        sb = tk.Scrollbar(outer, orient=tk.VERTICAL, command=self.canvas.yview, width=12)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.configure(yscrollcommand=sb.set)

        self.todo_frame = tk.Frame(self.canvas, bg="#FFFFFF")
        self._todo_window_id = self.canvas.create_window((0, 0), window=self.todo_frame, anchor="nw")

        # 内容变化时更新滚动区域
        def on_frame_configure(_):
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        self.todo_frame.bind("<Configure>", on_frame_configure)

        # 画布尺寸变化时，让内部内容宽度跟随（避免被遮挡）
        def on_canvas_configure(_):
            try:
                self.canvas.itemconfigure(self._todo_window_id, width=self.canvas.winfo_width())
            except Exception:
                pass

        self.canvas.bind("<Configure>", on_canvas_configure)

        # 鼠标滚轮滚动（进入列表区域时绑定，离开时解绑，避免影响其它控件）
        def _on_mousewheel(event):
            # Windows/macOS: event.delta; Linux 兼容见下方 Button-4/5
            try:
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
            except Exception:
                pass

        def _on_linux_up(_event):
            self.canvas.yview_scroll(-3, "units")

        def _on_linux_down(_event):
            self.canvas.yview_scroll(3, "units")

        def _bind_wheel(_event=None):
            self.window.bind_all("<MouseWheel>", _on_mousewheel)
            self.window.bind_all("<Button-4>", _on_linux_up)
            self.window.bind_all("<Button-5>", _on_linux_down)

        def _unbind_wheel(_event=None):
            try:
                self.window.unbind_all("<MouseWheel>")
                self.window.unbind_all("<Button-4>")
                self.window.unbind_all("<Button-5>")
            except Exception:
                pass

        self.canvas.bind("<Enter>", _bind_wheel)
        self.canvas.bind("<Leave>", _unbind_wheel)

    def update_todo_list(self):
        for w in self.todo_frame.winfo_children():
            w.destroy()

        status_filter = self.filter_status.get()
        priority_filter = self.filter_priority.get()
        q = self.search_var.get().strip().lower()

        with self.app.lock:
            events = list(self.app.events)

        filtered = []
        for ev in events:
            # status filter
            st = ev.get_status()
            if status_filter == "今日" and not ev.is_today_event():
                continue
            if status_filter == "待提醒" and (ev.completed or not ev.reminder):
                continue
            if status_filter == "已提醒" and (ev.completed or ev.reminder):
                continue
            if status_filter == "已完成" and not ev.completed:
                continue
            if status_filter == "逾期" and not ev.is_overdue():
                continue

            # priority filter
            if priority_filter != "全部" and ev.priority_text != priority_filter:
                continue

            # search
            if q:
                hay = f"{ev.date} {ev.time} {ev.content} {ev.priority_text} {st}".lower()
                if q not in hay:
                    continue

            filtered.append(ev)

        filtered.sort(key=lambda e: (0 if e.is_overdue() else 1, e.date, e.time, -e.priority))

        # 记录逾期事项（当前列表范围），用于“逾期处理”批量操作
        self._last_overdue_ids = [ev.id for ev in filtered if ev.is_overdue() and (not ev.completed)]
        try:
            if hasattr(self, "overdue_menu_btn") and self.overdue_menu_btn.winfo_exists():
                self.overdue_menu_btn.configure(state=(tk.NORMAL if self._last_overdue_ids else tk.DISABLED))
        except Exception:
            pass

        if not filtered:
            tk.Label(self.todo_frame, text="暂无匹配事项", bg="#FFFFFF", fg="#777",
                     font=("Microsoft YaHei", 10)).pack(pady=24)
            return

        current_date = None
        for ev in filtered:
            if ev.date != current_date:
                current_date = ev.date
                tk.Label(self.todo_frame, text=current_date, bg="#FFFFFF", fg="#333",
                         font=("Microsoft YaHei", 10, "bold")).pack(anchor="w", pady=(10, 2))

            card = tk.Frame(self.todo_frame, bg="#FFFFFF", bd=1, relief="solid")
            card.pack(fill=tk.X, pady=4)

            # 左色条（优先级）
            tk.Frame(card, bg=ev.priority_color, width=6).pack(side=tk.LEFT, fill=tk.Y)

            body = tk.Frame(card, bg="#FFFFFF")
            body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=6)

            top = tk.Frame(body, bg="#FFFFFF")
            top.pack(fill=tk.X)
            tk.Label(top, text=ev.time, bg="#FFFFFF", fg="#555",
                     font=("Microsoft YaHei", 9)).pack(side=tk.LEFT)
            tk.Label(top, text=ev.priority_text, bg="#FFFFFF", fg=ev.priority_color,
                     font=("Microsoft YaHei", 9, "bold")).pack(side=tk.RIGHT)

            txt = ev.content if len(ev.content) <= 60 else ev.content[:60] + "…"
            tk.Label(body, text=txt, bg="#FFFFFF", fg="#111",
                     font=("Microsoft YaHei", 10), wraplength=240, justify="left").pack(anchor="w", pady=(2, 0))

            overdue = ev.is_overdue()
            st = ev.get_status()
            display_st = f"逾期·{st}" if overdue else st
            if overdue:
                st_fg = "#B71C1C"
            else:
                st_fg = "#999" if st == "已完成" else ("#666" if st == "已提醒" else "#2E7D32")
            tk.Label(body, text=display_st, bg="#FFFFFF", fg=st_fg, font=("Microsoft YaHei", 9)).pack(anchor="w")

            # 点击定位
            card.bind("<Button-1>", lambda e, _id=ev.id: self.app.highlight_event(_id))
            for child in card.winfo_children():
                child.bind("<Button-1>", lambda e, _id=ev.id: self.app.highlight_event(_id))
                for g in getattr(child, "winfo_children", lambda: [])():
                    g.bind("<Button-1>", lambda e, _id=ev.id: self.app.highlight_event(_id))


    # ---------- 逾期一键处理（迷你窗） ----------
    def bulk_complete_overdue(self):
        """批量完成：当前列表范围内的逾期事项"""
        ids = list(getattr(self, "_last_overdue_ids", []))
        if not ids:
            messagebox.showinfo("提示", "当前列表中没有逾期事项。")
            return
        if not messagebox.askyesno(
            "批量完成逾期",
            f"将对当前列表中的 {len(ids)} 条逾期事项执行【标记完成】。\n\n继续吗？"
        ):
            return
        changed = self.app._bulk_set_completed(ids, completed=True)
        # 刷新自身列表
        self.update_todo_list()
        messagebox.showinfo("完成", f"已标记完成 {changed} 条逾期事项。")

    def bulk_restore_overdue(self):
        """批量恢复提醒：当前列表范围内的逾期事项"""
        ids = list(getattr(self, "_last_overdue_ids", []))
        if not ids:
            messagebox.showinfo("提示", "当前列表中没有逾期事项。")
            return
        if not messagebox.askyesno(
            "批量恢复提醒（逾期）",
            f"将对当前列表中的 {len(ids)} 条逾期事项执行【恢复提醒】。\n\n提示：逾期事项的时间已在过去，如需再次弹窗提醒，请把时间改到未来。\n\n继续吗？"
        ):
            return
        changed = self.app._bulk_restore_reminder(ids)
        self.update_todo_list()
        messagebox.showinfo("完成", f"已恢复提醒 {changed} 条逾期事项。")


# -----------------------------
# 主应用
# -----------------------------
class MemoReminderApp:
    def __init__(self, root):
        self.root = root

        # --- Compatibility / safety aliases (must exist before _build_ui uses them) ---
        # Some older versions referenced update_event_list; keep a stable refresh entrypoint.
        if not hasattr(self, "refresh_event_list"):
            # Fallback: use existing list updater if it has another name
            if hasattr(self, "update_event"):
                self.refresh_event_list = self.update_event  # type: ignore
        # If update_event_list is missing, alias it to refresh_event_list.
        if not hasattr(self, "update_event_list"):
            self.update_event_list = getattr(self, "refresh_event_list", lambda *a, **k: None)

        self.root.title("智能备忘录提醒系统（增强版）")

        # 统一主题
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei", 10), borderwidth=1, relief="solid")
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 10, "bold"))
        style.configure("TButton", padding=(10, 6))

        # 窗口大小
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w = min(1360, sw - 80)
        h = min(820, sh - 80)
        self.root.geometry(f"{w}x{h}")
        # 最小尺寸：右侧可滚动，所以允许更小的窗口
        self.root.minsize(860, 620)

        # 数据
        self.events = []  # list of Event
        self.lock = threading.RLock()

        # 线程控制
        self.stop_alarm = threading.Event()
        self._last_checked_minute = -1

        # 当前选中事件ID（编辑/更新用）
        self.current_event_id=None

        # 日历选中日期（用于高亮）
        self.selected_date = None
        # 日期筛选（用于列表过滤；点击日历日期会设置；“今日”按钮会清空）
        self.filter_date = None

        # 数据文件
        self.data_file = self._get_data_file()

        # 主题颜色（固定；已移除“切换季节”功能）
        self.colors = {
            "bg": "#F0F8FF",
            "fg": "#2F4F4F",
            "accent": "#1565C0",
            "line": "#D1D5DB",
        }
        self.root.configure(bg=self.colors["bg"])

        # 过滤变量（主窗口）
        self.search_var = tk.StringVar()
        self.filter_priority_var = tk.StringVar(value="全部")
        self.filter_status_var = tk.StringVar(value="全部")
        self.sort_var = tk.StringVar(value="时间 ↑")
        # 最近一次主列表过滤结果（用于批量操作）
        self._last_filtered_ids = []
        self._last_overdue_ids = []

        # 兼容兜底：若你手动合并代码导致 update_event_list 丢失，先绑定一个可用的刷新函数，避免启动时报错
        if not hasattr(self, "update_event_list"):
            self.update_event_list = getattr(self, "refresh_event_list", None) or self.update_calendar


        # UI
        self._build_ui()
        self.apply_theme()

        # 迷你窗
        self.mini_window = MiniWindow(self)

        # 线程
        self._start_alarm_thread()
        self._start_auto_save_thread()

        # 关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # 加载数据并刷新
        self.load_memos()
        self.update_calendar()

        self._update_clock()

    # ---------- 数据目录 ----------
    def _get_data_file(self):
        if platform.system() == "Windows":
            base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "MemoReminder")
        else:
            base = os.path.join(os.path.expanduser("~"), ".memo_reminder")
        os.makedirs(base, exist_ok=True)
        return os.path.join(base, "memo_data.json")

    # ---------- 主题 ----------
    def _get_current_season(self):
        m = datetime.now().month
        if 3 <= m <= 5:
            return "spring"
        if 6 <= m <= 8:
            return "summer"
        if 9 <= m <= 11:
            return "autumn"
        return "winter"

    def apply_theme(self):
        """应用固定主题（已移除“切换季节”功能）"""
        colors = getattr(self, "colors", {"bg": "#F0F8FF", "fg": "#2F4F4F"})
        try:
            self.root.configure(bg=colors.get("bg", "#F0F8FF"))
        except Exception:
            pass

        # 顶部标题区（若已创建）
        if hasattr(self, "title_label") and self.title_label.winfo_exists():
            self.title_label.configure(bg=colors.get("bg", "#F0F8FF"), fg=colors.get("fg", "#2F4F4F"))
        if hasattr(self, "clock_label") and self.clock_label.winfo_exists():
            self.clock_label.configure(bg=colors.get("bg", "#F0F8FF"))

    # 兼容旧方法名（避免旧版本调用时报错）
    def apply_season_theme(self):
        self.apply_theme()

    def change_season(self, season: str):
        """季节切换已移除：保留接口避免旧代码报错"""
        self.apply_theme()

    # --
    # ---------- UI ----------
    def _build_ui(self):
        colors = self.colors

        # 顶部
        title_frame = tk.Frame(self.root, height=80, bg=colors["bg"])
        title_frame.pack(fill=tk.X, padx=10, pady=6)
        title_frame.pack_propagate(False)

        left_header = tk.Frame(title_frame, bg=colors["bg"])
        left_header.pack(side=tk.LEFT, fill=tk.Y, padx=18)

        self.title_label = tk.Label(left_header, text="智能备忘录提醒系统",
                                    font=("Microsoft YaHei", 20, "bold"),
                                    bg=colors["bg"], fg=colors["fg"])
        self.title_label.pack(anchor="w")

        self.clock_label = tk.Label(left_header, text="", font=("Microsoft YaHei", 12),
                                    bg=colors["bg"], fg="red")
        self.clock_label.pack(anchor="w", pady=6)

        # 右上角：导入/导出（备份）
        right_header = tk.Frame(title_frame, bg=colors["bg"])
        right_header.pack(side=tk.RIGHT, fill=tk.Y, padx=10)

        ttk.Button(right_header, text="导出备份", width=10, command=self.export_backup).pack(side=tk.RIGHT, padx=(6, 0), pady=22)
        ttk.Button(right_header, text="导入备份", width=10, command=self.import_backup).pack(side=tk.RIGHT, pady=22)

        # 已移除：切换季节（固定主题）

        # 主内容（左右可拖拽；默认右侧更宽）
        main_pane = ttk.Panedwindow(self.root, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        # 边框统一为 1px（更“紧凑”）
        left_frame = tk.LabelFrame(main_pane, text="万年历", padx=4, pady=4, bd=1, relief="solid")
        right_frame = tk.LabelFrame(main_pane, text="备忘录管理", padx=4, pady=4, bd=1, relief="solid")

        main_pane.add(left_frame, weight=1)
        main_pane.add(right_frame, weight=4)

        # 保障左侧日历最小宽度，避免被挤压裁切
        try:
            main_pane.paneconfigure(left_frame, minsize=300)
            main_pane.paneconfigure(right_frame, minsize=520)
        except Exception:
            pass

        # 初始分隔条位置：让右侧空间更充足
        self.root.update_idletasks()
        try:
            main_pane.sashpos(0, 400)
        except Exception:
            pass

        ctrl = tk.Frame(left_frame)
        ctrl.pack(fill=tk.X, pady=2)

        tk.Label(ctrl, text="年份:").pack(side=tk.LEFT, padx=5)
        self.year_var = tk.StringVar(value=str(datetime.now().year))
        years = [str(datetime.now().year + i) for i in range(-10, 11)]
        self.year_combo = ttk.Combobox(ctrl, textvariable=self.year_var, values=years, width=7, state="readonly")
        self.year_combo.pack(side=tk.LEFT, padx=5)
        self.year_combo.bind("<<ComboboxSelected>>", self.update_calendar)

        tk.Label(ctrl, text="月份:").pack(side=tk.LEFT, padx=5)
        self.month_var = tk.StringVar(value=str(datetime.now().month))
        self.month_combo = ttk.Combobox(ctrl, textvariable=self.month_var,
                                        values=[str(i) for i in range(1, 13)], width=4, state="readonly")
        self.month_combo.pack(side=tk.LEFT, padx=5)
        self.month_combo.bind("<<ComboboxSelected>>", self.update_calendar)

        tk.Button(ctrl, text="今日", command=self.goto_today).pack(side=tk.RIGHT, padx=5)

                # 日历区域（Canvas 绘制 1px 网格线，避免 Label 边框叠加变粗）
        self.calendar_frame = tk.Frame(left_frame, bg="#FFFFFF")
        self.calendar_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        self.cal_canvas = tk.Canvas(self.calendar_frame, bg="#FFFFFF", highlightthickness=0)
        self.cal_canvas.pack(fill=tk.BOTH, expand=True)

        # 日历数据矩阵：6 行 x 7 列（值为 day 或 None）
        self._cal_matrix = [[None for _ in range(7)] for _ in range(6)]
        self._cal_year = int(self.year_var.get())
        self._cal_month = int(self.month_var.get())

        # 记录当前布局参数（用于点击换算）
        self._cal_layout = {"header_h": 26, "cell": 1, "x0": 0, "y0": 26, "grid_w": 0, "grid_h": 0}

        self.cal_canvas.bind("<Button-1>", self._on_calendar_click)
        self.cal_canvas.bind("<Double-Button-1>", self._on_calendar_double_click)
        self.cal_canvas.bind("<Configure>", lambda e: self._redraw_calendar())

# 右：管理（已在上方创建）


                # 右侧表单区：可滚动（窗口再小也不会遮挡下方表单/按钮）
        right_canvas = tk.Canvas(right_frame, highlightthickness=0)
        right_vsb = ttk.Scrollbar(right_frame, orient=tk.VERTICAL, command=right_canvas.yview)
        right_canvas.configure(yscrollcommand=right_vsb.set)

        # 用 grid 便于“自动隐藏滚动条”
        right_frame.grid_rowconfigure(0, weight=1)
        right_frame.grid_columnconfigure(0, weight=1)

        right_canvas.grid(row=0, column=0, sticky="nsew")
        right_vsb.grid(row=0, column=1, sticky="ns")

        right_body = tk.Frame(right_canvas)
        _right_win = right_canvas.create_window((0, 0), window=right_body, anchor="nw")

        self._right_scroll_visible = True

        def _toggle_right_scrollbar(need_scroll: bool):
            # 自动隐藏：内容不足一屏时不显示滚动条（更清爽）
            if need_scroll and not self._right_scroll_visible:
                right_vsb.grid()
                self._right_scroll_visible = True
            elif (not need_scroll) and self._right_scroll_visible:
                right_vsb.grid_remove()
                self._right_scroll_visible = False

        def _update_right_scroll_state():
            bbox = right_canvas.bbox("all")
            if not bbox:
                _toggle_right_scrollbar(False)
                return
            content_h = bbox[3] - bbox[1]
            canvas_h = right_canvas.winfo_height()
            # +6 做容差：避免因为像素四舍五入导致“明明够用却出现滚动条”
            need = content_h > (canvas_h + 6)
            _toggle_right_scrollbar(need)

        def _on_right_body_config(_e):
            right_canvas.configure(scrollregion=right_canvas.bbox("all"))
            _update_right_scroll_state()

        def _on_right_canvas_config(e):
            # 让内部容器宽度始终等于可视宽度（避免横向滚动）
            right_canvas.itemconfigure(_right_win, width=e.width)
            _update_right_scroll_state()

        right_body.bind("<Configure>", _on_right_body_config)
        right_canvas.bind("<Configure>", _on_right_canvas_config)

        # 鼠标滚轮（仅在右侧区域生效）
        def _right_wheel(e):
            # 鼠标在 Treeview / Text 上时，交给控件自身处理（避免外层抢滚动）
            try:
                if e.widget.winfo_class() in ("Treeview", "Text"):
                    return
            except Exception:
                pass
            if hasattr(e, "delta") and e.delta:
                right_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        def _right_wheel_up(e):
            try:
                if e.widget.winfo_class() in ("Treeview", "Text"):
                    return
            except Exception:
                pass
            right_canvas.yview_scroll(-3, "units")

        def _right_wheel_down(e):
            try:
                if e.widget.winfo_class() in ("Treeview", "Text"):
                    return
            except Exception:
                pass
            right_canvas.yview_scroll(3, "units")

        def _bind_right_wheel(_e):
            right_canvas.bind_all("<MouseWheel>", _right_wheel)
            right_canvas.bind_all("<Button-4>", _right_wheel_up)   # Linux
            right_canvas.bind_all("<Button-5>", _right_wheel_down) # Linux

        def _unbind_right_wheel(_e):
            right_canvas.unbind_all("<MouseWheel>")
            right_canvas.unbind_all("<Button-4>")
            right_canvas.unbind_all("<Button-5>")

        right_canvas.bind("<Enter>", _bind_right_wheel)
        right_canvas.bind("<Leave>", _unbind_right_wheel)

        filter_bar = tk.Frame(right_body)
        filter_bar.pack(fill=tk.X, pady=(0, 6))

        tk.Label(filter_bar, text="搜索:").pack(side=tk.LEFT, padx=(0, 6))
        search_entry = ttk.Entry(filter_bar, textvariable=self.search_var, width=22)
        search_entry.pack(side=tk.LEFT)
        search_entry.bind("<KeyRelease>", lambda e: self.update_event_list())

        tk.Label(filter_bar, text="优先级:").pack(side=tk.LEFT, padx=(12, 6))
        self.priority_filter_combo = ttk.Combobox(
            filter_bar, textvariable=self.filter_priority_var, state="readonly", width=12,
            values=["全部", "不重要不紧急", "不重要紧急", "重要不紧急", "重要紧急"]
        )
        self.priority_filter_combo.pack(side=tk.LEFT)
        self.priority_filter_combo.bind("<<ComboboxSelected>>", lambda e: self.update_event_list())

        tk.Label(filter_bar, text="状态:").pack(side=tk.LEFT, padx=(12, 6))
        self.status_filter_combo = ttk.Combobox(
            filter_bar, textvariable=self.filter_status_var, state="readonly", width=8,
            values=["全部", "待提醒", "已提醒", "已完成", "逾期"]
        )
        self.status_filter_combo.pack(side=tk.LEFT)
        self.status_filter_combo.bind("<<ComboboxSelected>>", lambda e: self.update_event_list())

        tk.Label(filter_bar, text="排序:").pack(side=tk.LEFT, padx=(12, 6))
        self.sort_combo = ttk.Combobox(
            filter_bar, textvariable=self.sort_var, state="readonly", width=12,
            values=["时间 ↑", "时间 ↓", "优先级 高→低", "优先级 低→高", "状态", "逾期优先"]
        )
        self.sort_combo.pack(side=tk.LEFT)
        self.sort_combo.bind("<<ComboboxSelected>>", lambda e: self.update_event_list())
        # 逾期一键处理（批量完成/批量恢复提醒）
        self.overdue_menu_btn = tk.Menubutton(filter_bar, text="逾期一键处理", relief=tk.RAISED)
        self.overdue_menu = tk.Menu(self.overdue_menu_btn, tearoff=0)
        self.overdue_menu.add_command(label="批量完成逾期（当前筛选）", command=self.bulk_complete_overdue)
        self.overdue_menu.add_command(label="批量恢复提醒（当前筛选）", command=self.bulk_restore_overdue)
        self.overdue_menu_btn.configure(menu=self.overdue_menu)
        self.overdue_menu_btn.pack(side=tk.RIGHT, padx=5)

        ttk.Button(filter_bar, text="清空筛选", command=self.clear_filters).pack(side=tk.RIGHT, padx=5)

        # 信息提示（两行，避免被筛选栏挤压）
        info_bar = tk.Frame(right_body)
        info_bar.pack(fill=tk.X, pady=(0, 6))

        self.count_label = tk.Label(
            info_bar, text="",
            fg="#555", bg="#f5f7fb",
            bd=1, relief="solid",
            padx=10, pady=6,
            anchor="w", justify="left"
        )
        self.count_label.pack(fill=tk.X)
# 列表
        list_frame = tk.Frame(right_body)
        list_frame.pack(fill=tk.X)

        cols = ("日期", "时间", "内容", "优先级", "状态")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=8, selectmode="extended")
        for col in cols:
            self.tree.heading(col, text=col)
            self.tree.column(col, width=90 if col != "内容" else 240)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Delete>", lambda e: self.delete_event())

        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)

        # 鼠标滚轮优先滚动列表（避免外层滚动抢占）
        def _tree_wheel(e):
            if hasattr(e, "delta") and e.delta:
                self.tree.yview_scroll(int(-1 * (e.delta / 120)), "units")
            return "break"

        def _tree_wheel_up(e):
            self.tree.yview_scroll(-3, "units")
            return "break"

        def _tree_wheel_down(e):
            self.tree.yview_scroll(3, "units")
            return "break"

        self.tree.bind("<MouseWheel>", _tree_wheel)
        self.tree.bind("<Button-4>", _tree_wheel_up)
        self.tree.bind("<Button-5>", _tree_wheel_down)

        # tag 样式
        self.tree.tag_configure("p0", background="#E8F5E9")
        self.tree.tag_configure("p1", background="#E3F2FD")
        self.tree.tag_configure("p2", background="#FFF8E1")
        self.tree.tag_configure("p3", background="#FFEBEE")
        self.tree.tag_configure("overdue", background="#FFCDD2", foreground="#B71C1C")
        self.tree.tag_configure("reminded", background="#FFF3E0", foreground="#666666")
        self.tree.tag_configure("completed", background="#F5F5F5", foreground="#999999")

        # 按钮
        btn_frame = tk.Frame(right_body)
        btn_frame.pack(fill=tk.X, pady=6)

        ttk.Button(btn_frame, text="添加事件", command=self.add_event, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="更新事件", command=self.update_event, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="删除事件", command=self.delete_event, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="标记完成", command=self.mark_completed, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="恢复提醒", command=self.restore_reminder, width=12).pack(side=tk.LEFT, padx=3)
        ttk.Button(btn_frame, text="迷你窗", command=lambda: self.mini_window.show(), width=10).pack(side=tk.RIGHT, padx=3)

        # 编辑区
        edit_frame = tk.LabelFrame(right_body, text="添加/编辑事件", padx=8, pady=8, bd=1, relief="solid")
        edit_frame.pack(fill=tk.X, pady=6)

        row1 = tk.Frame(edit_frame)
        row1.pack(fill=tk.X, pady=4)

        tk.Label(row1, text="日期(YYYY-MM-DD):").pack(side=tk.LEFT)
        self.event_date = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        ttk.Entry(row1, textvariable=self.event_date, width=12).pack(side=tk.LEFT, padx=6)

        tk.Label(row1, text="时间(HH:MM):").pack(side=tk.LEFT, padx=(12, 0))
        self.event_time = tk.StringVar(value="09:00")
        ttk.Entry(row1, textvariable=self.event_time, width=8).pack(side=tk.LEFT, padx=6)

        row2 = tk.Frame(edit_frame)
        row2.pack(fill=tk.X, pady=4)

        tk.Label(row2, text="优先级:").pack(side=tk.LEFT)
        self.event_priority = tk.StringVar(value="不重要不紧急")
        self.priority_combo = ttk.Combobox(row2, textvariable=self.event_priority, state="readonly",
                                           values=["不重要不紧急", "不重要紧急", "重要不紧急", "重要紧急"], width=12)
        self.priority_combo.pack(side=tk.LEFT, padx=6)

        tk.Label(row2, text="重复:").pack(side=tk.LEFT, padx=(12, 0))
        self.event_repeat = tk.StringVar(value="不重复")
        self.repeat_combo = ttk.Combobox(row2, textvariable=self.event_repeat, state="readonly",
                                         values=["不重复", "每天", "每周"], width=8)
        self.repeat_combo.pack(side=tk.LEFT, padx=6)

        self.event_reminder = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="启用提醒", variable=self.event_reminder).pack(side=tk.LEFT, padx=(12, 0))

        row3 = tk.Frame(edit_frame)
        row3.pack(fill=tk.X, pady=4)

        tk.Label(row3, text="内容:").pack(anchor="w")
        self.content_text = tk.Text(edit_frame, height=3, wrap="word", font=("Microsoft YaHei", 10))
        self.content_text.pack(fill=tk.X, pady=(0, 6))

        # 状态栏（底部，显示保存状态/数据文件）
        self._status_base = f"数据文件：{self.data_file}"
        self.status_var = tk.StringVar(value=self._status_base)
        status_bar = tk.Frame(self.root, bd=1, relief="solid", bg="#FFFFFF")
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Label(status_bar, textvariable=self.status_var, anchor="w",
                 bg="#FFFFFF", fg="#444", padx=8).pack(fill=tk.X)

    def _set_status(self, msg: str):
        try:
            base = getattr(self, "_status_base", "")
            if base:
                self.status_var.set(f"{msg}    |    {base}")
            else:
                self.status_var.set(msg)
        except Exception:
            pass

    def clear_filters(self):
        self.search_var.set("")
        self.filter_priority_var.set("全部")
        self.filter_status_var.set("全部")
        self.sort_var.set("时间 ↑")
        # 清除日期筛选（列表恢复显示全部日期）
        self.filter_date = None
        self.update_event_list()

    # ---------- 逾期一键处理（批量） ----------
    def _update_overdue_menu_state(self):
        """根据当前筛选结果，自动启用/禁用“逾期一键处理”按钮"""
        try:
            ids = getattr(self, "_last_overdue_ids", [])
            if hasattr(self, "overdue_menu_btn") and self.overdue_menu_btn.winfo_exists():
                self.overdue_menu_btn.configure(state=(tk.NORMAL if ids else tk.DISABLED))
        except Exception:
            pass

    def bulk_complete_overdue(self):
        """批量完成：当前筛选结果中的逾期事项"""
        ids = list(getattr(self, "_last_overdue_ids", []))
        if not ids:
            messagebox.showinfo("提示", "当前筛选结果中没有逾期事项。")
            return
        if not messagebox.askyesno(
            "批量完成逾期",
            f"将对当前筛选结果中的 {len(ids)} 条逾期事项执行【标记完成】。\n\n继续吗？"
        ):
            return
        changed = self._bulk_set_completed(ids, completed=True)
        messagebox.showinfo("完成", f"已标记完成 {changed} 条逾期事项。")

    def bulk_restore_overdue(self):
        """批量恢复提醒：当前筛选结果中的逾期事项"""
        ids = list(getattr(self, "_last_overdue_ids", []))
        if not ids:
            messagebox.showinfo("提示", "当前筛选结果中没有逾期事项。")
            return
        if not messagebox.askyesno(
            "批量恢复提醒（逾期）",
            f"将对当前筛选结果中的 {len(ids)} 条逾期事项执行【恢复提醒】。\n\n提示：逾期事项的时间已在过去，如需再次弹窗提醒，请把时间改到未来，或后续增加“Snooze 稍后提醒”。\n\n继续吗？"
        ):
            return
        changed = self._bulk_restore_reminder(ids)
        messagebox.showinfo("完成", f"已恢复提醒 {changed} 条逾期事项。")

    def _bulk_set_completed(self, ids, completed=True):
        """按 event_id 列表批量设置完成状态（仅在 UI 线程调用）"""
        idset = set(ids)
        changed = 0
        with self.lock:
            for ev in self.events:
                if ev.id not in idset:
                    continue
                if completed and not ev.completed:
                    ev.completed = True
                    ev.reminder = False
                    changed += 1
                elif (not completed) and ev.completed:
                    ev.completed = False
                    changed += 1
        # 有改动就落盘并刷新；无改动也刷新列表以同步按钮状态
        if changed:
            self.save_memos()
        else:
            self.update_event_list()
        return changed

    def _bulk_restore_reminder(self, ids):
        """按 event_id 列表批量恢复提醒（不改时间；逾期不会自动再次弹窗）"""
        idset = set(ids)
        changed = 0
        with self.lock:
            for ev in self.events:
                if ev.id not in idset:
                    continue
                touched = False
                # ✅ 允许从“已完成”恢复
                if ev.completed:
                    ev.completed = False
                    touched = True
                if not ev.reminder:
                    ev.reminder = True
                    touched = True
                if touched:
                    changed += 1

        if changed:
            self.save_memos()
            self._set_status(f"✅ 批量恢复提醒 {changed} 条：{datetime.now():%H:%M:%S}")
        else:
            self.update_event_list()
            self._set_status(f"ℹ️ 无需恢复：{datetime.now():%H:%M:%S}")
        return changed


    # ---------- 时钟 ----------
    def _update_clock(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.clock_label.config(text=f"当前时间: {now}")
        self.root.after(1000, self._update_clock)

    # ---------- 日历 ----------
    def goto_today(self):
        """跳到今天，并清除“日期筛选”（列表显示全部日期）。"""
        today = datetime.now()
        self.year_var.set(str(today.year))
        self.month_var.set(str(today.month))
        self.update_calendar()

        ds = today.strftime("%Y-%m-%d")
        # 仅用于日历高亮 + 表单日期默认值
        self.selected_date = ds
        self.event_date.set(ds)

        # 清除列表日期筛选（显示全部）
        self.filter_date = None
        self._redraw_calendar()
        self.update_event_list()

    def update_calendar(self, _event=None):

        """刷新日历数据（矩阵）并重绘画布。"""
        try:
            y = int(self.year_var.get())
            m = int(self.month_var.get())
        except Exception:
            return

        self._cal_year = y
        self._cal_month = m

        first = date(y, m, 1)
        start_col = first.weekday()  # Mon=0..Sun=6（列从周一开始）
        next_month = date(y + (m // 12), (m % 12) + 1, 1) if m != 12 else date(y + 1, 1, 1)
        days = (next_month - first).days

        # 清空矩阵
        self._cal_matrix = [[None for _ in range(7)] for _ in range(6)]

        # 填充矩阵
        d = 1
        for idx in range(start_col, start_col + days):
            r = idx // 7
            c = idx % 7
            if 0 <= r < 6:
                self._cal_matrix[r][c] = d
            d += 1

        self._redraw_calendar()
        self.update_event_list()

    def select_date(self, r, c):
        """兼容旧版本：如果外部仍调用 select_date(r,c)，这里按矩阵取 day。"""
        try:
            day = self._cal_matrix[r][c]
        except Exception:
            day = None
        if not day:
            return
        y = self._cal_year
        m = self._cal_month
        self.selected_date = f"{y:04d}-{m:02d}-{day:02d}"
        self.filter_date = self.selected_date
        self.event_date.set(self.selected_date)
        self._redraw_calendar()
        self.update_event_list()

    def _on_calendar_click(self, event):
        """点击日历画布：换算到 cell(row,col)，选中日期。"""
        try:
            header_h = int(self._cal_layout.get("header_h", 26))
            cell = float(self._cal_layout.get("cell", 1))
            x0 = float(self._cal_layout.get("x0", 0))
            y0 = float(self._cal_layout.get("y0", header_h))
            grid_w = float(self._cal_layout.get("grid_w", 0))
            grid_h = float(self._cal_layout.get("grid_h", 0))
        except Exception:
            return
        if cell <= 0:
            return
        # 仅在网格区域内响应点击
        if event.x < x0 or event.y < y0:
            return
        if grid_w > 0 and event.x > x0 + grid_w:
            return
        if grid_h > 0 and event.y > y0 + grid_h:
            return
        col = int((event.x - x0) // cell)
        row = int((event.y - y0) // cell)
        if not (0 <= row < 6 and 0 <= col < 7):
            return
        day = self._cal_matrix[row][col]
        if not day:
            return
        y = self._cal_year
        m = self._cal_month
        self.selected_date = f"{y:04d}-{m:02d}-{day:02d}"
        self.filter_date = self.selected_date
        self.event_date.set(self.selected_date)
        self._redraw_calendar()
        self.update_event_list()

    def _on_calendar_double_click(self, event):
        """双击日历日期：快速新增事件。"""
        try:
            header_h = int(self._cal_layout.get("header_h", 26))
            cell = float(self._cal_layout.get("cell", 1))
            x0 = float(self._cal_layout.get("x0", 0))
            y0 = float(self._cal_layout.get("y0", header_h))
            grid_w = float(self._cal_layout.get("grid_w", 0))
            grid_h = float(self._cal_layout.get("grid_h", 0))
        except Exception:
            return
        if cell <= 0:
            return
        if event.x < x0 or event.y < y0:
            return
        if grid_w > 0 and event.x > x0 + grid_w:
            return
        if grid_h > 0 and event.y > y0 + grid_h:
            return
        col = int((event.x - x0) // cell)
        row = int((event.y - y0) // cell)
        if not (0 <= row < 6 and 0 <= col < 7):
            return
        day = self._cal_matrix[row][col]
        if not day:
            return

        y = self._cal_year
        m = self._cal_month
        ds = f"{y:04d}-{m:02d}-{day:02d}"

        # 双击视为“选中并按日期筛选”
        self.selected_date = ds
        self.filter_date = ds
        self.event_date.set(ds)
        self._redraw_calendar()
        self.update_event_list()

        self._open_quick_add(ds)

    def _open_quick_add(self, date_str: str):
        """快速新增窗口：双击日历调用。"""
        # 默认时间：当前时间向上取整到 5 分钟
        now = datetime.now()
        minute = (now.minute + 4) // 5 * 5
        if minute >= 60:
            now = now.replace(minute=0) + timedelta(hours=1)
            minute = 0
        default_time = f"{now.hour:02d}:{minute:02d}"

        win = tk.Toplevel(self.root)
        win.title("快速新增")
        win.transient(self.root)
        try:
            win.grab_set()
        except Exception:
            pass

        frm = tk.Frame(win, padx=12, pady=12)
        frm.pack(fill=tk.BOTH, expand=True)

        tk.Label(frm, text="日期(YYYY-MM-DD)：").grid(row=0, column=0, sticky="w")
        d_var = tk.StringVar(value=date_str)
        ttk.Entry(frm, textvariable=d_var, width=14).grid(row=0, column=1, sticky="w", padx=(6, 0))

        tk.Label(frm, text="时间(HH:MM)：").grid(row=0, column=2, sticky="w", padx=(12, 0))
        t_var = tk.StringVar(value=default_time)
        ttk.Entry(frm, textvariable=t_var, width=8).grid(row=0, column=3, sticky="w", padx=(6, 0))

        tk.Label(frm, text="优先级：").grid(row=1, column=0, sticky="w", pady=(10, 0))
        p_var = tk.StringVar(value="不重要不紧急")
        p_combo = ttk.Combobox(frm, textvariable=p_var, state="readonly", width=12,
                               values=["不重要不紧急", "不重要紧急", "重要不紧急", "重要紧急"])
        p_combo.grid(row=1, column=1, sticky="w", padx=(6, 0), pady=(10, 0))

        tk.Label(frm, text="重复：").grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(10, 0))
        r_var = tk.StringVar(value="不重复")
        r_combo = ttk.Combobox(frm, textvariable=r_var, state="readonly", width=8,
                               values=["不重复", "每天", "每周"])
        r_combo.grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(10, 0))

        rem_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frm, text="启用提醒", variable=rem_var).grid(row=2, column=0, sticky="w", pady=(10, 0), columnspan=2)

        tk.Label(frm, text="内容：").grid(row=3, column=0, sticky="nw", pady=(10, 0))
        txt = tk.Text(frm, width=50, height=6)
        txt.grid(row=3, column=1, columnspan=3, sticky="we", padx=(6, 0), pady=(10, 0))
        frm.grid_columnconfigure(3, weight=1)

        btns = tk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=4, sticky="e", pady=(12, 0))

        def _save():
            d = d_var.get().strip()
            t = t_var.get().strip()
            content = txt.get("1.0", tk.END).strip()
            if not content:
                messagebox.showwarning("提示", "请输入内容", parent=win)
                return
            if not self._validate_date_time(d, t):
                messagebox.showwarning("提示", "日期或时间格式不正确", parent=win)
                return
            p = self._priority_to_int(p_var.get())
            rep = self._repeat_to_int(r_var.get())
            rem = bool(rem_var.get())
            self._add_event_values(d, t, content, p, rep, rem)

            # 同步主表单日期（更贴合使用习惯）
            self.event_date.set(d)
            self.event_time.set(t)
            win.destroy()

        ttk.Button(btns, text="保存", command=_save).pack(side=tk.RIGHT, padx=(6, 0))
        ttk.Button(btns, text="取消", command=win.destroy).pack(side=tk.RIGHT)

        # 快捷键：Ctrl+Enter 保存
        win.bind("<Control-Return>", lambda e: _save())

    def _add_event_values(self, d: str, t: str, content: str, p: int, rep: int, rem: bool):
        """直接以参数新增事件（供快速新增窗口调用）。"""
        ev = Event(d, t, content, priority=p, repeat=rep, reminder=rem, completed=False)
        with self.lock:
            self.events.append(ev)
        # 立即保存（写盘失败会弹窗提示）
        self.save_memos()
        # 刷新 UI：列表 + 日历徽标
        self.update_event_list()
        self._redraw_calendar()
        try:
            if self.mini_window and self.mini_window.window.winfo_exists():
                self.mini_window.update_todo_list()
        except Exception:
            pass

    def _redraw_calendar(self):
        """重绘日历画布（1px 网格线；今日/选中高亮）。"""
        if not hasattr(self, "cal_canvas"):
            return

        c = self.cal_canvas
        c.delete("all")

        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 2 or h <= 2:
            return

        header_h = 26
        avail_w = max(1, w)
        avail_h = max(1, h - header_h)

        # 让单元格宽高相等：取能同时容纳 7 列、6 行的最小边长，并居中绘制网格
        cell = min(avail_w / 7.0, avail_h / 6.0)
        grid_w = cell * 7.0
        grid_h = cell * 6.0

        x0 = (w - grid_w) / 2.0
        y0 = header_h + (avail_h - grid_h) / 2.0

        self._cal_layout = {"header_h": header_h, "cell": cell, "x0": x0, "y0": y0, "grid_w": grid_w, "grid_h": grid_h}

        line = "#D0D7DE"
        header_bg = "#F6F8FA"
        today_bg = "#E3F2FD"
        sel_bg = "#BBDEFB"
        weekend_fg = "#D32F2F"

        # Header background
        c.create_rectangle(x0, 0, x0 + grid_w, header_h, fill=header_bg, outline=line, width=1)

        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        for i, t in enumerate(weekdays):
            x0c = x0 + i * cell
            x1c = x0 + (i + 1) * cell
            c.create_line(x1c, 0, x1c, header_h, fill=line, width=1)
            c.create_text((x0c + x1c) / 2, header_h / 2, text=t,
                          font=("Microsoft YaHei", 9, "bold"),
                          fill=weekend_fg if i >= 5 else "#111827")

        # Grid lines (body) — 使用方格单元格，并居中
        grid_x0 = x0
        grid_y0 = y0
        for r in range(7):
            y_line = grid_y0 + r * cell
            c.create_line(grid_x0, y_line, grid_x0 + grid_w, y_line, fill=line, width=1)
        for col in range(8):
            x_line = grid_x0 + col * cell
            c.create_line(x_line, grid_y0, x_line, grid_y0 + grid_h, fill=line, width=1)

        # Highlights + day numbers
        today_str = datetime.now().strftime("%Y-%m-%d")
        y = self._cal_year
        m = self._cal_month

        # 统计当月各日期的备忘数量（用于“多个点/数字徽标”）
        prefix = f"{y:04d}-{m:02d}-"
        count_by_date = {}
        try:
            with self.lock:
                _events = list(self.events)
            for ev in _events:
                if isinstance(getattr(ev, "date", None), str) and ev.date.startswith(prefix):
                    count_by_date[ev.date] = count_by_date.get(ev.date, 0) + 1
        except Exception:
            count_by_date = {}
        for r in range(6):
            for col in range(7):
                day = self._cal_matrix[r][col]
                if not day:
                    continue
                ds = f"{y:04d}-{m:02d}-{day:02d}"
                x0c = x0 + col * cell
                y0c = y0 + r * cell
                x1c = x0 + (col + 1) * cell
                y1c = y0 + (r + 1) * cell

                if ds == today_str:
                    c.create_rectangle(x0c + 1, y0c + 1, x1c - 1, y1c - 1,
                                       fill=today_bg, outline="")
                if self.selected_date and ds == self.selected_date:
                    c.create_rectangle(x0c + 1, y0c + 1, x1c - 1, y1c - 1,
                                       fill=sel_bg, outline="")

                c.create_text(x0c + 8, y0c + 9, text=str(day),
                              anchor="w",
                              font=("Microsoft YaHei", 9),
                              fill=weekend_fg if col >= 5 else "#111827")
                # --- 当天备忘“多个点/数字徽标” ---
                cnt = count_by_date.get(ds, 0) if 'count_by_date' in locals() else 0
                if cnt:
                    # 1~3 条：显示 1~3 个小圆点（底部居中）
                    if cnt <= 3:
                        rdot = 2
                        spacing = 7
                        cx0 = (x0c + x1c) / 2 - (cnt - 1) * spacing / 2
                        cy = y1c - 10
                        for i in range(cnt):
                            cx = cx0 + i * spacing
                            c.create_oval(cx - rdot, cy - rdot, cx + rdot, cy + rdot,
                                          fill="#2563EB", outline="")
                    else:
                        # ≥4 条：显示数字徽标（右上角）
                        label = str(cnt) if cnt < 10 else "9+"
                        by = y0c + 14
                        bx = x1c - 14
                        half_w = 9 if len(label) > 1 else 7
                        half_h = 7
                        c.create_oval(bx - half_w, by - half_h, bx + half_w, by + half_h,
                                      fill="#2563EB", outline="")
                        c.create_text(bx, by, text=label, fill="#FFFFFF",
                                      font=("Microsoft YaHei", 8, "bold"))

    # ---------- 事件操作 ----------
    def _priority_to_int(self, txt: str):
        mp = {"不重要不紧急": 0, "不重要紧急": 1, "重要不紧急": 2, "重要紧急": 3}
        return mp.get(txt, 0)

    def _repeat_to_int(self, txt: str):
        return {"不重复": 0, "每天": 1, "每周": 7}.get(txt, 0)

    def _validate_date_time(self, date_str: str, time_str: str):
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            datetime.strptime(time_str, "%H:%M")
            return True
        except Exception:
            return False

    def add_event(self):
        d = self.event_date.get().strip()
        t = self.event_time.get().strip()
        content = self.content_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "请输入内容")
            return
        if not self._validate_date_time(d, t):
            messagebox.showwarning("提示", "日期或时间格式不正确")
            return

        p = self._priority_to_int(self.event_priority.get())
        rep = self._repeat_to_int(self.event_repeat.get())
        rem = bool(self.event_reminder.get())

        new_events = []
        base_dt = datetime.strptime(d, "%Y-%m-%d").date()

        if rep == 0:
            new_events.append(Event(d, t, content, p, rep, rem))
        elif rep == 1:
            # 每天，生成未来365条（含当天）
            for i in range(365):
                dd = (base_dt + timedelta(days=i)).strftime("%Y-%m-%d")
                new_events.append(Event(dd, t, content, p, rep, rem))
        elif rep == 7:
            # 每周，生成未来52条（含本周）
            for i in range(52):
                dd = (base_dt + timedelta(days=7*i)).strftime("%Y-%m-%d")
                new_events.append(Event(dd, t, content, p, rep, rem))

        with self.lock:
            self.events.extend(new_events)
            self._ensure_unique_ids_locked()
        self.save_memos()
        self._reset_form(keep_date=True)

    def on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        event_id = sel[0]
        with self.lock:
            ev = self._get_event_by_id_locked(event_id)
        if not ev:
            return

        self.current_event_id = ev.id
        self.event_date.set(ev.date)
        self.event_time.set(ev.time)
        self.event_priority.set(ev.priority_text)
        self.event_repeat.set({0: "不重复", 1: "每天", 7: "每周"}.get(ev.repeat, "不重复"))
        self.event_reminder.set(ev.reminder)
        self.content_text.delete("1.0", tk.END)
        self.content_text.insert("1.0", ev.content)

    def update_event(self):
        if not self.current_event_id:
            messagebox.showinfo("提示", "请先在列表中选择要更新的事件")
            return

        d = self.event_date.get().strip()
        t = self.event_time.get().strip()
        content = self.content_text.get("1.0", tk.END).strip()
        if not content:
            messagebox.showwarning("提示", "请输入内容")
            return
        if not self._validate_date_time(d, t):
            messagebox.showwarning("提示", "日期或时间格式不正确")
            return

        p = self._priority_to_int(self.event_priority.get())
        rep = self._repeat_to_int(self.event_repeat.get())
        rem = bool(self.event_reminder.get())

        with self.lock:
            ev = self._get_event_by_id_locked(self.current_event_id)
            if not ev:
                messagebox.showwarning("提示", "找不到该事件（可能已被删除）")
                return
            ev.date = d
            ev.time = t
            ev.content = content
            ev.priority = p
            ev.repeat = rep
            ev.reminder = rem
            # 更新后，未完成状态保持；如果你希望“更新即恢复待提醒”，可以取消下一行注释：
            # if not ev.completed: ev.reminder = True
            self._ensure_unique_ids_locked()

        self.save_memos()
        self._reset_form(keep_date=True)

    def delete_event(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要删除的事件")
            return

        # 支持多选删除：Ctrl/Shift 可多选
        count = len(sel)
        if not messagebox.askyesno("确认", f"确定删除选中的 {count} 条事件吗？\n\n删除后无法恢复。"):
            return

        ids = set(sel)
        with self.lock:
            self.events = [e for e in self.events if e.id not in ids]

        # 如果当前正在编辑的事件被删了，清空编辑态
        if self.current_event_id in ids:
            self.current_event_id = None

        self.save_memos()
        self._reset_form(keep_date=True)

    def mark_completed(self):
        """将选中事件标记为已完成（支持多选批量）"""
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择事件")
            return
    
        if len(sel) > 1:
            ok = messagebox.askyesno(
                "确认",
                f"确定将选中的 {len(sel)} 条事件标记为已完成吗？\n\n完成后将关闭提醒。"
            )
            if not ok:
                return
    
        changed_events = 0
        with self.lock:
            for event_id in sel:
                ev = self._get_event_by_id_locked(event_id)
                if not ev:
                    continue
                ev_changed = False
                # 完成：关闭提醒
                if not ev.completed:
                    ev.completed = True
                    ev_changed = True
                if ev.reminder:
                    ev.reminder = False
                    ev_changed = True
                if ev_changed:
                    changed_events += 1
    
        if changed_events:
            self.save_memos()
            self._set_status(f"✅ 已完成 {changed_events} 条：{datetime.now():%H:%M:%S}")
        else:
            self.update_event_list()
            self._set_status(f"ℹ️ 无需变更：{datetime.now():%H:%M:%S}")
    
    def restore_reminder(self):
        """恢复选中事件为“待提醒”（支持多选批量；已完成会自动恢复为未完成）"""
        sel = list(self.tree.selection())
        if not sel:
            messagebox.showinfo("提示", "请先选择事件")
            return
    
        if len(sel) > 1:
            ok = messagebox.askyesno(
                "确认",
                f"确定恢复选中的 {len(sel)} 条事件为待提醒吗？\n\n已完成的事件会恢复为未完成。"
            )
            if not ok:
                return
    
        changed_events = 0
        with self.lock:
            for event_id in sel:
                ev = self._get_event_by_id_locked(event_id)
                if not ev:
                    continue
                ev_changed = False
                # ✅ 允许从“已完成”恢复：恢复为未完成 + 待提醒
                if ev.completed:
                    ev.completed = False
                    ev_changed = True
                if not ev.reminder:
                    ev.reminder = True
                    ev_changed = True
                if ev_changed:
                    changed_events += 1
    
        if changed_events:
            self.save_memos()
            self._set_status(f"✅ 已恢复 {changed_events} 条：{datetime.now():%H:%M:%S}")
        else:
            self.update_event_list()
            self._set_status(f"ℹ️ 已是待提醒：{datetime.now():%H:%M:%S}")
    
    def _reset_form(self, keep_date: bool = True):
        if not keep_date:
            self.event_date.set(datetime.now().strftime("%Y-%m-%d"))
        self.event_time.set("09:00")
        self.event_priority.set("不重要不紧急")
        self.event_repeat.set("不重复")
        self.event_reminder.set(True)
        self.content_text.delete("1.0", tk.END)

    # ---------- 事件列表（含筛选） ----------
    def update_event_list(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        q = self.search_var.get().strip().lower()
        pfilter = self.filter_priority_var.get()
        sfilter = self.filter_status_var.get()
        sel_date = getattr(self, 'filter_date', None)

        with self.lock:
            events = list(self.events)

        now_dt = datetime.now()
        sort_mode = self.sort_var.get().strip() or "时间 ↑"

        # 排序
        if sort_mode == "时间 ↓":
            events.sort(key=lambda e: (e.date, e.time), reverse=True)
        elif sort_mode == "优先级 高→低":
            events.sort(key=lambda e: (-e.priority, e.date, e.time))
        elif sort_mode == "优先级 低→高":
            events.sort(key=lambda e: (e.priority, e.date, e.time))
        elif sort_mode == "逾期优先":
            events.sort(key=lambda e: (0 if e.is_overdue(now_dt) else 1, e.date, e.time))
        elif sort_mode == "状态":
            def _rank(ev: Event):
                if ev.completed:
                    return 3
                if ev.is_overdue(now_dt):
                    return 0
                if ev.reminder:
                    return 1  # 待提醒
                return 2      # 已提醒
            events.sort(key=lambda e: (_rank(e), e.date, e.time))
        else:
            events.sort(key=lambda e: (e.date, e.time))

        # 过滤
        filtered = []
        tokens = [t for t in q.split() if t] if q else []
        for ev in events:
            overdue = ev.is_overdue(now_dt)
            st = ev.get_status()
            display_st = f"逾期·{st}" if overdue else st

            if sel_date and ev.date != sel_date:
                continue
            if pfilter != "全部" and ev.priority_text != pfilter:
                continue
            if sfilter == "逾期":
                if not overdue:
                    continue
            elif sfilter != "全部" and st != sfilter:
                continue
            if tokens:
                hay = f"{ev.date} {ev.time} {ev.content} {ev.priority_text} {display_st}".lower()
                ok = True
                for tok in tokens:
                    if tok not in hay:
                        ok = False
                        break
                if not ok:
                    continue

            filtered.append(ev)

        for ev in filtered:
            overdue = ev.is_overdue(now_dt)
            st = ev.get_status()
            display_st = f"逾期·{st}" if overdue else st
            values = (
                ev.date,
                ev.time,
                (ev.content[:30] + "…") if len(ev.content) > 30 else ev.content,
                ev.priority_text,
                display_st,
            )
            tag = "completed" if ev.completed else ("overdue" if overdue else ("reminded" if not ev.reminder else f"p{ev.priority}"))
            self.tree.insert("", tk.END, iid=ev.id, values=values, tags=(tag,))

        date_text = sel_date if sel_date else "全部"
        self.count_label.config(text=f"显示 {len(filtered)} / 总 {len(events)}\n日期：{date_text}")

        # 记录本次筛选结果，供“逾期一键处理”使用
        self._last_filtered_ids = [ev.id for ev in filtered]
        self._last_overdue_ids = [ev.id for ev in filtered if ev.is_overdue(now_dt) and (not ev.completed)]
        self._update_overdue_menu_state()

        # 同步刷新迷你窗（不会太重，迷你窗自己也有过滤）
        try:
            if self.mini_window and self.mini_window.window.winfo_exists():
                self.mini_window.update_todo_list()
        except Exception:
            pass


    def refresh_event_list(self):
        """兼容别名：刷新事件列表（等价于 update_event_list）"""
        return self.update_event_list()

    def highlight_event(self, event_id):
        """从迷你窗定位到主列表某条事件"""
        if not event_id:
            return
        # 若当前筛选导致不可见，先清筛选（更符合“定位”预期）
        if not self.tree.exists(event_id):
            self.clear_filters()
        if self.tree.exists(event_id):
            self.tree.selection_set(event_id)
            self.tree.see(event_id)
            self.on_tree_select()

    # ---------- 加载/保存 ----------
    def _ensure_unique_ids_locked(self):
        seen = set()
        for ev in self.events:
            if not ev.id or ev.id in seen:
                ev.id = uuid.uuid4().hex
            seen.add(ev.id)

    def _get_event_by_id_locked(self, event_id: str):
        for ev in self.events:
            if ev.id == event_id:
                return ev
        return None

    def load_memos(self):
        if not os.path.exists(self.data_file):
            self.update_event_list()
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            events = [Event.from_dict(x) for x in (data or [])]
            with self.lock:
                self.events = events
                self._ensure_unique_ids_locked()
            self.update_event_list()
        except Exception as e:
            messagebox.showerror("错误", f"加载数据失败：{e}")

    def _save_to_disk(self):
        """线程安全+原子写入（不触碰Tk）"""
        with self.lock:
            data = [e.to_dict() for e in self.events]

        # 确保目录存在
        data_dir = os.path.dirname(self.data_file)
        if data_dir:
            os.makedirs(data_dir, exist_ok=True)

        tmp = self.data_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.replace(tmp, self.data_file)

    def save_memos(self):
        """UI线程可调用：写磁盘并刷新UI"""
        ok = True
        err = ""
        try:
            self._save_to_disk()
        except Exception as e:
            ok = False
            err = str(e)
            print("保存失败:", e)
            try:
                messagebox.showerror("保存失败", f"保存数据时发生错误：\n{err}\n\n数据文件：\n{self.data_file}")
            except Exception:
                pass

        if ok:
            self._set_status(f"✅ 已保存：{datetime.now():%H:%M:%S}")
        else:
            self._set_status(f"❌ 保存失败：{datetime.now():%H:%M:%S}")

        # 日历刷新会顺带刷新列表
        self.update_calendar()

    # ---------- 导入/导出（备份） ----------
    def export_backup(self):
        """导出当前全部事件到一个 JSON 文件（用于备份/迁移）。"""
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M")
            default_name = f"memo_backup_{ts}.json"
            path = filedialog.asksaveasfilename(
                title="导出备份（JSON）",
                defaultextension=".json",
                initialfile=default_name,
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
            )
            if not path:
                return

            with self.lock:
                data = [e.to_dict() for e in self.events]

            # 原子写入
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, path)

            self._set_status(f"✅ 已导出备份：{datetime.now():%H:%M:%S}")
            messagebox.showinfo("导出成功", f"备份已导出：\n{path}")
        except Exception as e:
            messagebox.showerror("导出失败", f"导出备份失败：\n{e}")

    def import_backup(self):
        """从 JSON 备份文件导入事件（支持覆盖/合并）。"""
        try:
            path = filedialog.askopenfilename(
                title="导入备份（JSON）",
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")]
            )
            if not path:
                return

            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            if not isinstance(raw, list):
                raise ValueError("备份文件格式不正确：顶层应为数组(list)。")

            imported = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                imported.append(Event.from_dict(item))

            if not imported:
                messagebox.showwarning("导入提示", "备份文件中没有可导入的数据。")
                return

            # 选择导入方式：是=覆盖，否=合并，取消=返回
            choice = messagebox.askyesnocancel(
                "导入方式",
                "请选择导入方式：\n\n"
                "【是】覆盖当前所有数据（当前数据会被替换）\n"
                "【否】合并到当前数据（不会删除现有数据）\n"
                "【取消】不导入"
            )
            if choice is None:
                return

            with self.lock:
                if choice is True:
                    # 覆盖
                    self.events = imported
                    self._ensure_unique_ids_locked()
                else:
                    # 合并
                    self.events.extend(imported)
                    self._ensure_unique_ids_locked()

            self.save_memos()
            self._set_status(f"✅ 已导入备份：{datetime.now():%H:%M:%S}")
            messagebox.showinfo("导入成功", f"已导入 {len(imported)} 条备忘。")
        except Exception as e:
            messagebox.showerror("导入失败", f"导入备份失败：\n{e}")

    # ---------- 提醒 ----------
    def _start_alarm_thread(self):
        t = threading.Thread(target=self._alarm_loop, daemon=True)
        t.start()

    def _alarm_loop(self):
        while not self.stop_alarm.is_set():
            now = datetime.now()
            cur_min = now.minute
            if cur_min != self._last_checked_minute:
                self._last_checked_minute = cur_min
                cur_date = now.strftime("%Y-%m-%d")
                cur_time = now.strftime("%H:%M")

                due_ids = []
                with self.lock:
                    for ev in self.events:
                        if (ev.date == cur_date and ev.time == cur_time and
                                ev.reminder and (not ev.completed)):
                            due_ids.append(ev.id)

                for eid in due_ids:
                    self.root.after(0, lambda _id=eid: self._fire_alarm_on_ui(_id))

            time.sleep(1)

    def _fire_alarm_on_ui(self, event_id):
        with self.lock:
            ev = self._get_event_by_id_locked(event_id)
            if not ev:
                return
            # 再次校验避免重复触发
            if ev.completed or (not ev.reminder):
                return
            ev.reminder = False  # 标记已提醒
        try:
            self._save_to_disk()
        except Exception:
            pass
        self.update_event_list()
        self._show_alarm(ev)

    def _show_alarm(self, ev):
        win = tk.Toplevel(self.root)
        win.title("提醒")
        win.geometry("420x260")
        win.attributes("-topmost", True)
        win.resizable(False, False)

        bg = "#FFF9C4"
        win.configure(bg=bg)

        tk.Label(win, text="⏰ 备忘录提醒", bg=bg, fg="#333",
                 font=("Microsoft YaHei", 14, "bold")).pack(pady=(14, 6))

        tk.Label(win, text=f"{ev.date}  {ev.time}", bg=bg, fg="#555",
                 font=("Microsoft YaHei", 11)).pack()

        body = tk.Frame(win, bg=bg)
        body.pack(fill=tk.BOTH, expand=True, padx=16, pady=10)

        # 优先级色条
        tk.Frame(body, bg=ev.priority_color, width=8).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))

        txt = tk.Text(body, height=5, wrap="word", font=("Microsoft YaHei", 11))
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        txt.insert("1.0", ev.content)
        txt.configure(state="disabled")

        # 动画
        canvas = tk.Canvas(win, width=80, height=80, bg=bg, highlightthickness=0)
        canvas.place(x=320, y=30)
        anim = AnimatedSmile(canvas)
        anim.start()

        def close():
            anim.stop()
            win.destroy()

        ttk.Button(win, text="知道了", command=close).pack(pady=10)

        # Windows 尝试蜂鸣
        try:
            if platform.system() == "Windows":
                import winsound
                winsound.Beep(1200, 200)
                winsound.Beep(1000, 200)
        except Exception:
            pass

    # ---------- 自动保存 ----------
    def _start_auto_save_thread(self):
        t = threading.Thread(target=self._auto_save_loop, daemon=True)
        t.start()

    def _auto_save_loop(self):
        # 每5分钟写一次磁盘，不触碰任何Tk
        while not self.stop_alarm.is_set():
            time.sleep(300)
            try:
                self._save_to_disk()
            except Exception:
                pass

    # ---------- 退出 ----------
    def on_closing(self):
        # 关闭前尽量保存一次
        self.stop_alarm.set()
        try:
            self._save_to_disk()
        except Exception as e:
            try:
                messagebox.showerror("保存失败", f"退出前保存失败：\n{e}\n\n数据文件：\n{self.data_file}")
            except Exception:
                pass
        self.root.destroy()


def main():
    import os, sys, io

    print(f"[MemoReminder] {APP_VERSION} | file={os.path.abspath(__file__)}")

    # 说明：libpng iCCP 警告来自底层图片解码（通常与 Tk/主题资源有关），不影响程序功能。
    # 这里在 Tk 初始化阶段做一次“过滤输出”，避免控制台刷屏；若有其它错误信息，会原样输出。
    _stderr = sys.stderr
    _buf = io.StringIO()
    try:
        sys.stderr = _buf
        root = tk.Tk()
    finally:
        sys.stderr = _stderr
        captured = _buf.getvalue()
        if captured:
            for line in captured.splitlines():
                if "libpng warning: iCCP" in line:
                    continue
                print(line, file=sys.stderr)

    app = MemoReminderApp(root)
    root.mainloop()



if __name__ == "__main__":
    main()
