from __future__ import annotations

import colorsys
import json
import random
import sqlite3
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import simpledialog

try:
    import winsound
except ImportError:
    winsound = None

CONFIG_PATH = Path(__file__).with_name("config.json")


@dataclass
class ThreadRow:
    thread_id: str
    title: str
    updated_at: int
    archived: int
    rollout_path: str


@dataclass
class ThreadStatus:
    thread: ThreadRow
    working: bool


@dataclass
class PollResult:
    statuses: List[ThreadStatus]
    entered_working: List[ThreadRow]
    entered_completed: List[ThreadRow]


class ActionRunner:
    """Run optional beeps/commands from config."""

    @staticmethod
    def run(action_cfg: Dict[str, Any], thread: Optional[ThreadRow]) -> None:
        if not action_cfg:
            return
        if action_cfg.get("beep") and winsound:
            winsound.MessageBeep()

        command = (action_cfg.get("command") or "").strip()
        if not command:
            return

        thread_id = thread.thread_id if thread else ""
        title = thread.title if thread else ""
        safe_title = title.replace("\n", " ").replace('"', "'")
        subprocess.Popen(command.replace("{thread_id}", thread_id).replace("{title}", safe_title), shell=True)


class CodexThreadMonitor:
    """Poll thread status from Codex sqlite + rollout events."""

    def __init__(self, db_path: str, active_timeout_sec: int, rollout_tail_lines: int, monitor_thread_count: int) -> None:
        self.db_path = Path(db_path).expanduser()
        self.active_timeout_sec = max(1, int(active_timeout_sec))
        self.rollout_tail_lines = max(100, int(rollout_tail_lines))
        self.monitor_thread_count = max(1, int(monitor_thread_count))
        self.previous_working_by_id: Dict[str, bool] = {}
        self.rollout_state_cache: Dict[str, Tuple[int, int, Optional[str]]] = {}

    def _fetch_latest_threads(self) -> List[ThreadRow]:
        if not self.db_path.exists():
            return []

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT id, title, updated_at, archived, rollout_path
                FROM threads
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (self.monitor_thread_count,),
            ).fetchall()
        finally:
            conn.close()

        out: List[ThreadRow] = []
        for row in rows:
            out.append(
                ThreadRow(
                    thread_id=row["id"],
                    title=row["title"] or "",
                    updated_at=int(row["updated_at"] or 0),
                    archived=int(row["archived"] or 0),
                    rollout_path=row["rollout_path"] or "",
                )
            )
        return out

    def _read_rollout_state(self, rollout_path: str) -> Optional[str]:
        if not rollout_path:
            return None
        path = Path(rollout_path)
        if not path.exists() or not path.is_file():
            return None

        stat = path.stat()
        key = str(path)
        cached = self.rollout_state_cache.get(key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]

        lines: Deque[str] = deque(maxlen=self.rollout_tail_lines)
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line:
                    lines.append(line.strip())

        last_task_state: Optional[bool] = None
        pending_call_ids: set[str] = set()
        pending_call_without_id = 0
        saw_activity_after_complete = False
        saw_complete = False
        saw_wait_keyword = False
        seen_signal = False

        for line in lines:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = obj.get("type")
            payload = obj.get("payload") or {}
            sub_type = str(payload.get("type") or "")

            if typ == "event_msg":
                if sub_type == "task_started":
                    last_task_state = True
                    saw_complete = False
                    seen_signal = True
                elif sub_type == "task_complete":
                    last_task_state = False
                    saw_complete = True
                    seen_signal = True
                elif sub_type and sub_type != "token_count":
                    seen_signal = True
                    if saw_complete:
                        saw_activity_after_complete = True
                    low = sub_type.lower()
                    if ("approval" in low) or ("permission" in low) or ("wait" in low) or ("input" in low):
                        saw_wait_keyword = True
                continue

            if typ != "response_item":
                continue

            if sub_type in {"function_call", "custom_tool_call"}:
                seen_signal = True
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id.strip():
                    pending_call_ids.add(call_id)
                else:
                    pending_call_without_id += 1
                if saw_complete:
                    saw_activity_after_complete = True
                continue

            if sub_type in {"function_call_output", "custom_tool_call_output"}:
                seen_signal = True
                call_id = payload.get("call_id")
                if isinstance(call_id, str):
                    pending_call_ids.discard(call_id)
                elif pending_call_without_id > 0:
                    pending_call_without_id -= 1
                if saw_complete:
                    saw_activity_after_complete = True
                continue

            if sub_type:
                seen_signal = True
                if saw_complete:
                    saw_activity_after_complete = True

        if pending_call_ids or pending_call_without_id > 0 or saw_wait_keyword:
            state: Optional[str] = "waiting"
        elif last_task_state is True:
            state = "working"
        elif last_task_state is False:
            state = "working" if saw_activity_after_complete else "completed"
        else:
            state = "working" if seen_signal else None

        self.rollout_state_cache[key] = (stat.st_mtime_ns, stat.st_size, state)
        return state
    def _is_working(self, thread: ThreadRow) -> bool:
        if thread.archived == 1:
            return False
        age = int(time.time()) - thread.updated_at
        rollout_state = self._read_rollout_state(thread.rollout_path)
        if rollout_state == "waiting":
            return True
        if rollout_state == "working":
            return age <= self.active_timeout_sec
        if rollout_state == "completed":
            return False
        return age <= self.active_timeout_sec

    def poll(self) -> PollResult:
        statuses = [ThreadStatus(thread=t, working=self._is_working(t)) for t in self._fetch_latest_threads()]
        entered_working: List[ThreadRow] = []
        entered_completed: List[ThreadRow] = []
        next_prev: Dict[str, bool] = {}

        for s in statuses:
            prev = self.previous_working_by_id.get(s.thread.thread_id)
            if prev is None:
                if s.working:
                    entered_working.append(s.thread)
            else:
                if (not prev) and s.working:
                    entered_working.append(s.thread)
                elif prev and (not s.working):
                    entered_completed.append(s.thread)
            next_prev[s.thread.thread_id] = s.working

        self.previous_working_by_id = next_prev
        return PollResult(statuses=statuses, entered_working=entered_working, entered_completed=entered_completed)

class PetWindow:
    """A single cat window bound to one thread."""

    def __init__(
        self,
        root: tk.Tk,
        thread: ThreadRow,
        x: int,
        y: int,
        always_on_top: bool,
        sprite_cfg: Dict[str, Any],
        motion_cfg: Dict[str, Any],
        cat_name: str,
        name_color: str,
        lane_offset: int = 0,
    ) -> None:
        self.root = root
        self.thread = thread
        self.hidden_by_user = False
        self.state = "completed"
        self.animation_step = 0
        self.done_animation_left = 0
        self.drag_offset_x = 0
        self.drag_offset_y = 0
        self._dialog_open = False
        self._last_dialog_at = 0.0

        self.cat_name = cat_name
        self.name_color = name_color
        self.transparent_color = str(sprite_cfg.get("transparent_key", "#000000"))
        self.sprite_scale = max(1, int(sprite_cfg.get("sprite_scale", 1)))

        self.fallback_completed_faces = ["( -.- )", "( -_- )"]
        self.fallback_working_faces = ["( o_o )", "( O_O )", "( o_o )", "( -o- )"]
        self.fallback_done_faces = ["( ^_^ )", "( ^o^ )", "( ^_^ )", "( >_< )"]

        self.working_frames = self._load_animation_frames("working", sprite_cfg)
        self.completed_frames = self._apply_subsample(
            self._load_animation_frames("completed", sprite_cfg),
            int(sprite_cfg.get("completed_subsample", 1)),
        )
        self.done_frames = self._apply_subsample(
            self._load_animation_frames("done", sprite_cfg),
            int(sprite_cfg.get("done_subsample", 1)),
        )
        self.done_hold_ticks = max(1, int(motion_cfg.get("done_hold_ticks", 18)))

        self.render_w = 0
        self.render_h = 0
        self._normalize_frames()
        self.working_frames_left = self._mirror_frames(self.working_frames)
        self.completed_frames_left = self._mirror_frames(self.completed_frames)
        self.done_frames_left = self._mirror_frames(self.done_frames)
        self.current_image: Optional[tk.PhotoImage] = None

        self.bottom_walk_enabled = bool(motion_cfg.get("bottom_walk_enabled", True))
        self.bottom_margin = int(motion_cfg.get("bottom_margin", 30))
        self.lane_offset = int(lane_offset)  # 兼容参数：保留旧调用，不改变默认同一底线行为
        self.random_walk_enabled = bool(motion_cfg.get("random_walk_enabled", True))
        self.turn_chance = float(motion_cfg.get("turn_chance", 0.08))
        self.min_target_step = max(10, int(motion_cfg.get("min_target_step", 80)))
        self.max_target_step = max(self.min_target_step, int(motion_cfg.get("max_target_step", 280)))

        self.random_speed_enabled = bool(motion_cfg.get("random_speed_enabled", True))
        base_speed = max(1, int(motion_cfg.get("walk_speed_px", 8)))
        speed_min = max(1, int(motion_cfg.get("cat_speed_min_px", 6)))
        speed_max = max(speed_min, int(motion_cfg.get("cat_speed_max_px", 14)))
        self.walk_speed_px = random.randint(speed_min, speed_max) if self.random_speed_enabled else base_speed

        self.walk_x = float(x)
        self.walk_direction = 1 if (sum(ord(c) for c in thread.thread_id) % 2 == 0) else -1
        self.walk_target_x: Optional[float] = None

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", always_on_top)
        self.win.configure(bg=self.transparent_color)

        try:
            self.win.attributes("-transparentcolor", self.transparent_color)
        except tk.TclError:
            pass

        self.opacity = max(0.2, min(1.0, float(motion_cfg.get("default_opacity", 1.0))))
        try:
            self.win.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass

        self.name_label = tk.Label(
            self.win,
            text=self.cat_name,
            font=("Consolas", 10, "bold"),
            fg=self.name_color,
            bg=self.transparent_color,
            bd=0,
            highlightthickness=0,
        )
        self.name_label.pack(padx=0, pady=(24, 0))

        self.face_label = tk.Label(
            self.win,
            text="( -.- )",
            font=("Consolas", 22),
            fg="#333333",
            bg=self.transparent_color,
            bd=0,
            highlightthickness=0,
        )
        self.face_label.pack(padx=0, pady=0)

        self.menu = tk.Menu(self.win, tearoff=0)
        self.menu.add_command(label="改名", command=self.edit_name)
        opacity_menu = tk.Menu(self.menu, tearoff=0)
        for v in [100, 90, 80, 70, 60]:
            opacity_menu.add_command(label=f"{v}%", command=lambda vv=v: self.set_opacity(vv / 100.0))
        opacity_menu.add_separator()
        opacity_menu.add_command(label="自定义...", command=self.edit_opacity)
        self.menu.add_cascade(label="透明度", menu=opacity_menu)
        self.menu.add_command(label="隐藏", command=self.hide)

        for w in (self.win, self.name_label, self.face_label):
            w.bind("<ButtonPress-1>", self._on_press)
            w.bind("<B1-Motion>", self._on_drag)
            w.bind("<Button-3>", self._show_context_menu)
            w.bind("<Double-Button-1>", self._on_double_click)

        self.win.geometry(f"+{x}+{y}")

    def _resolve_asset_path(self, asset_path: str) -> Path:
        p = Path(asset_path)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent / p
        return p

    def _load_sequence_frames(self, sheet_path: str, cols: int, rows: int) -> List[tk.PhotoImage]:
        if not sheet_path:
            return []
        p = self._resolve_asset_path(sheet_path)
        if not p.exists():
            return []
        try:
            sheet = tk.PhotoImage(file=str(p))
        except tk.TclError:
            return []

        cols = max(1, int(cols))
        rows = max(1, int(rows))
        fw = max(1, sheet.width() // cols)
        fh = max(1, sheet.height() // rows)
        out: List[tk.PhotoImage] = []
        for r in range(rows):
            for c in range(cols):
                x1 = c * fw
                y1 = r * fh
                frame = tk.PhotoImage(width=fw, height=fh)
                frame.tk.call(str(frame), "copy", str(sheet), "-from", x1, y1, x1 + fw, y1 + fh, "-to", 0, 0)
                if self.sprite_scale > 1:
                    frame = frame.zoom(self.sprite_scale, self.sprite_scale)
                out.append(frame)
        return out

    def _load_gif_frames(self, gif_path: str) -> List[tk.PhotoImage]:
        if not gif_path:
            return []
        p = self._resolve_asset_path(gif_path)
        if not p.exists():
            return []

        frames: List[tk.PhotoImage] = []
        i = 0
        while True:
            try:
                frame = tk.PhotoImage(file=str(p), format=f"gif -index {i}")
                if self.sprite_scale > 1:
                    frame = frame.zoom(self.sprite_scale, self.sprite_scale)
                frames.append(frame)
                i += 1
            except tk.TclError:
                break
        return frames

    def _load_animation_frames(self, state_name: str, sprite_cfg: Dict[str, Any]) -> List[tk.PhotoImage]:
        sheet = str(sprite_cfg.get(f"{state_name}_sheet", "") or "").strip()
        if sheet:
            frames = self._load_sequence_frames(sheet, int(sprite_cfg.get(f"{state_name}_sheet_cols", 1)), int(sprite_cfg.get(f"{state_name}_sheet_rows", 1)))
            if frames:
                return frames
        return self._load_gif_frames(str(sprite_cfg.get(f"{state_name}_gif", "") or ""))

    @staticmethod
    def _apply_subsample(frames: List[tk.PhotoImage], subsample: int) -> List[tk.PhotoImage]:
        s = max(1, int(subsample))
        if s <= 1:
            return frames
        return [f.subsample(s, s) for f in frames]

    def _normalize_frame_to_size(self, frame: tk.PhotoImage, tw: int, th: int) -> tk.PhotoImage:
        if frame.width() == tw and frame.height() == th:
            return frame
        canvas = tk.PhotoImage(width=tw, height=th)
        canvas.put(self.transparent_color, to=(0, 0, tw, th))
        dx = max(0, (tw - frame.width()) // 2)
        dy = max(0, (th - frame.height()) // 2)
        canvas.tk.call(str(canvas), "copy", str(frame), "-to", dx, dy)
        return canvas

    def _normalize_frames(self) -> None:
        all_frames = self.working_frames + self.completed_frames + self.done_frames
        if not all_frames:
            self.render_w = 0
            self.render_h = 0
            return
        tw = max(f.width() for f in all_frames)
        th = max(f.height() for f in all_frames)
        self.render_w, self.render_h = tw, th
        self.working_frames = [self._normalize_frame_to_size(f, tw, th) for f in self.working_frames]
        self.completed_frames = [self._normalize_frame_to_size(f, tw, th) for f in self.completed_frames]
        self.done_frames = [self._normalize_frame_to_size(f, tw, th) for f in self.done_frames]

    def _mirror_frame(self, frame: tk.PhotoImage) -> tk.PhotoImage:
        out = tk.PhotoImage(width=frame.width(), height=frame.height())
        out.tk.call(str(out), "copy", str(frame), "-from", 0, 0, frame.width(), frame.height(), "-to", 0, 0, "-subsample", -1, 1)
        return out

    def _mirror_frames(self, frames: List[tk.PhotoImage]) -> List[tk.PhotoImage]:
        return [self._mirror_frame(f) for f in frames]

    def _pick_frames(self, right_frames: List[tk.PhotoImage], left_frames: List[tk.PhotoImage]) -> List[tk.PhotoImage]:
        return left_frames if self.walk_direction < 0 and left_frames else right_frames

    def _on_press(self, event: tk.Event) -> None:
        self.drag_offset_x = event.x_root - self.win.winfo_x()
        self.drag_offset_y = event.y_root - self.win.winfo_y()

    def _on_drag(self, event: tk.Event) -> None:
        x = event.x_root - self.drag_offset_x
        y = event.y_root - self.drag_offset_y
        self.walk_x = float(x)
        self.win.geometry(f"+{x}+{y}")

    def _show_context_menu(self, event: tk.Event) -> str:
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()
        return "break"

    def _on_double_click(self, _event: tk.Event) -> str:
        self.edit_name()
        return "break"

    def hide(self) -> None:
        self.hidden_by_user = True
        self.win.withdraw()

    def show(self) -> None:
        self.hidden_by_user = False
        self.win.deiconify()
        self.win.lift()

    def get_position(self) -> Tuple[int, int]:
        return self.win.winfo_x(), self.win.winfo_y()

    def get_name(self) -> str:
        return self.cat_name

    def update_thread(self, thread: ThreadRow) -> None:
        self.thread = thread

    def set_name(self, name: str) -> None:
        n = (name or "").strip()
        if not n:
            return
        self.cat_name = n
        self.name_label.config(text=n)

    def _askstring_safe(self, title: str, prompt: str, initialvalue: str) -> Optional[str]:
        now = time.time()
        # 防抖：右键/双击事件可能短时间重复触发。
        if self._dialog_open or (now - self._last_dialog_at) < 0.25:
            return None
        self._dialog_open = True
        self._last_dialog_at = now
        try:
            if self.win.winfo_exists():
                try:
                    return simpledialog.askstring(title, prompt, initialvalue=initialvalue, parent=self.win)
                except tk.TclError:
                    pass
            if self.root.winfo_exists():
                return simpledialog.askstring(title, prompt, initialvalue=initialvalue, parent=self.root)
            return simpledialog.askstring(title, prompt, initialvalue=initialvalue)
        except tk.TclError:
            return None
        finally:
            self._dialog_open = False

    def edit_name(self) -> None:
        new_name = self._askstring_safe("Rename Cat", "输入新的名字:", self.cat_name)
        if new_name is not None:
            self.set_name(new_name)

    def set_opacity(self, value: float) -> None:
        self.opacity = max(0.2, min(1.0, float(value)))
        try:
            self.win.attributes("-alpha", self.opacity)
        except tk.TclError:
            pass

    def edit_opacity(self) -> None:
        raw = self._askstring_safe("Opacity", "输入透明度(20-100):", str(int(self.opacity * 100)))
        if raw is None:
            return
        try:
            p = int(raw.strip())
        except ValueError:
            return
        self.set_opacity(max(20, min(100, p)) / 100.0)

    def update_state(self, working: bool, entered_working: bool, entered_completed: bool) -> None:
        if entered_working and self.hidden_by_user:
            self.show()
        self.state = "working" if working else "completed"
        if entered_completed:
            self.done_animation_left = self.done_hold_ticks

    def _set_frame_or_text(self, frame: Optional[tk.PhotoImage], fallback: str) -> None:
        if frame is not None:
            self.current_image = frame
            self.face_label.config(image=frame, text="")
            sw = self.render_w if self.render_w > 0 else frame.width()
            sh = self.render_h if self.render_h > 0 else frame.height()
        else:
            self.current_image = None
            self.face_label.config(image="", text=fallback)
            self.face_label.update_idletasks()
            sw = self.render_w if self.render_w > 0 else self.face_label.winfo_reqwidth()
            sh = self.render_h if self.render_h > 0 else self.face_label.winfo_reqheight()

        self.name_label.update_idletasks()
        nw = self.name_label.winfo_reqwidth()
        nh = self.name_label.winfo_reqheight()
        self.win.geometry(f"{max(sw, nw)}x{sh + nh}+{self.win.winfo_x()}+{self.win.winfo_y()}")

    def _pick_random_target_x(self, min_x: int, max_x: int) -> float:
        if max_x <= min_x:
            return float(min_x)
        direction = self.walk_direction if random.random() < 0.7 else -self.walk_direction
        if direction == 0:
            direction = 1
        step = random.randint(self.min_target_step, self.max_target_step)
        target = self.walk_x + direction * step
        target = float(max(min_x, min(max_x, int(target))))
        if abs(target - self.walk_x) < self.walk_speed_px * 2:
            target = float(min_x if abs(self.walk_x - min_x) > abs(self.walk_x - max_x) else max_x)
        self.walk_direction = 1 if target >= self.walk_x else -1
        return target

    def _move_bottom_walk(self) -> None:
        if not self.bottom_walk_enabled or self.state != "working" or self.hidden_by_user:
            return

        self.win.update_idletasks()
        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        ww = self.win.winfo_width()
        wh = self.win.winfo_height()
        min_x = 0
        max_x = max(0, screen_w - ww)

        if self.random_walk_enabled:
            if self.walk_target_x is None:
                self.walk_target_x = self._pick_random_target_x(min_x, max_x)
            if random.random() < self.turn_chance:
                self.walk_target_x = self._pick_random_target_x(min_x, max_x)
            delta = self.walk_target_x - self.walk_x
            if abs(delta) <= self.walk_speed_px:
                self.walk_x = float(self.walk_target_x)
                self.walk_target_x = self._pick_random_target_x(min_x, max_x)
            else:
                self.walk_direction = 1 if delta > 0 else -1
                self.walk_x += self.walk_speed_px * self.walk_direction
        else:
            self.walk_x += self.walk_speed_px * self.walk_direction
            if self.walk_x <= min_x:
                self.walk_x = float(min_x)
                self.walk_direction = 1
            elif self.walk_x >= max_x:
                self.walk_x = float(max_x)
                self.walk_direction = -1

        self.walk_x = float(max(min_x, min(max_x, int(self.walk_x))))
        y = max(0, screen_h - wh - self.bottom_margin)
        self.win.geometry(f"+{int(self.walk_x)}+{int(y)}")

    def tick_animation(self) -> None:
        self.animation_step += 1

        if self.done_animation_left > 0:
            frames = self._pick_frames(self.done_frames, self.done_frames_left)
            frame = frames[self.animation_step % len(frames)] if frames else None
            self._set_frame_or_text(frame, self.fallback_done_faces[self.animation_step % len(self.fallback_done_faces)])
            self.done_animation_left -= 1
            self._move_bottom_walk()
            return

        if self.state == "working":
            frames = self._pick_frames(self.working_frames, self.working_frames_left)
            frame = frames[self.animation_step % len(frames)] if frames else None
            self._set_frame_or_text(frame, self.fallback_working_faces[self.animation_step % len(self.fallback_working_faces)])
            self._move_bottom_walk()
            return

        frames = self._pick_frames(self.completed_frames, self.completed_frames_left)
        frame = frames[self.animation_step % len(frames)] if frames else None
        self._set_frame_or_text(frame, self.fallback_completed_faces[self.animation_step % len(self.fallback_completed_faces)])

class DesktopPetApp:
    """Coordinator: monitor threads, update pets, and trigger actions."""

    def __init__(self, root: tk.Tk, config: Dict[str, Any]) -> None:
        self.root = root
        self.root.withdraw()
        self.config = config

        self.monitor = CodexThreadMonitor(
            db_path=str(config.get("db_path", "")),
            active_timeout_sec=int(config.get("active_timeout_sec", 10)),
            rollout_tail_lines=int(config.get("rollout_tail_lines", 500)),
            monitor_thread_count=int(config.get("monitor_thread_count", 8)),
        )

        self.poll_interval_ms = max(50, int(config.get("poll_interval_ms", 120)))
        self.working_action_interval_sec = max(1, int(config.get("working_action_interval_sec", 12)))
        self.last_periodic_action_ts = 0.0

        window_cfg = config.get("window", {})
        self.start_y = int(window_cfg.get("y", 30))
        self.always_on_top = bool(window_cfg.get("always_on_top", True))

        self.sprite_cfg = config.get("sprites", {})
        self.motion_cfg = config.get("motion", {})

        self.pets: Dict[str, PetWindow] = {}
        self.saved_positions: Dict[str, Tuple[int, int]] = {}
        self.saved_names: Dict[str, str] = {}
        self.saved_name_colors: Dict[str, str] = {}
        self.name_counter = 1

        self._tick()

    @staticmethod
    def _make_name_color(idx: int) -> str:
        """Distinct name color using golden-angle hue spacing."""
        hue = (idx * 0.61803398875) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 0.75, 0.95)
        return f"#{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"

    def _new_random_spawn(self) -> Tuple[int, int]:
        screen_w = self.root.winfo_screenwidth()
        return random.randint(0, max(0, screen_w - 120)), self.start_y

    def _ensure_pet(self, thread: ThreadRow) -> PetWindow:
        existing = self.pets.get(thread.thread_id)
        if existing is not None:
            existing.update_thread(thread)
            return existing

        if thread.thread_id in self.saved_positions:
            x, y = self.saved_positions[thread.thread_id]
        else:
            x, y = self._new_random_spawn()

        if thread.thread_id not in self.saved_names:
            self.saved_names[thread.thread_id] = f"chimi{self.name_counter}"
            self.saved_name_colors[thread.thread_id] = self._make_name_color(self.name_counter)
            self.name_counter += 1

        pet = PetWindow(
            root=self.root,
            thread=thread,
            x=x,
            y=y,
            always_on_top=self.always_on_top,
            sprite_cfg=self.sprite_cfg,
            motion_cfg=self.motion_cfg,
            lane_offset=0,
            cat_name=self.saved_names[thread.thread_id],
            name_color=self.saved_name_colors[thread.thread_id],
        )
        self.pets[thread.thread_id] = pet
        return pet

    def _run_actions(self, result: PollResult) -> None:
        actions = self.config.get("actions", {})
        for t in result.entered_working:
            ActionRunner.run(actions.get("on_working_enter", {}), t)
        for t in result.entered_completed:
            ActionRunner.run(actions.get("on_completed", {}), t)

        working = [s.thread for s in result.statuses if s.working]
        if not working:
            return
        now = time.time()
        if now - self.last_periodic_action_ts >= self.working_action_interval_sec:
            ActionRunner.run(actions.get("on_working_periodic", {}), working[0])
            self.last_periodic_action_ts = now

    def _update_pets(self, result: PollResult) -> None:
        entered_working = {t.thread_id for t in result.entered_working}
        entered_completed = {t.thread_id for t in result.entered_completed}
        monitored = {s.thread.thread_id for s in result.statuses}

        for s in result.statuses:
            if s.working or s.thread.thread_id in self.pets:
                pet = self._ensure_pet(s.thread)
                pet.update_state(
                    working=s.working,
                    entered_working=s.thread.thread_id in entered_working,
                    entered_completed=s.thread.thread_id in entered_completed,
                )

        for tid, pet in self.pets.items():
            if tid not in monitored:
                pet.update_state(False, False, False)

        for tid, pet in self.pets.items():
            pet.tick_animation()
            self.saved_positions[tid] = pet.get_position()
            self.saved_names[tid] = pet.get_name()

    def _tick(self) -> None:
        try:
            result = self.monitor.poll()
            self._run_actions(result)
            self._update_pets(result)
        except Exception as exc:
            print(f"[desktop-pet] tick error: {exc}")
        finally:
            self.root.after(self.poll_interval_ms, self._tick)


DEFAULT_CONFIG: Dict[str, Any] = {
    "db_path": "C:/Users/Lenovo/.codex/state_5.sqlite",
    "poll_interval_ms": 120,
    "active_timeout_sec": 10,
    "working_action_interval_sec": 12,
    "rollout_tail_lines": 500,
    "monitor_thread_count": 8,
    "window": {
        "x": 30,
        "y": 30,
        "always_on_top": True,
    },
    "sprites": {
        "transparent_key": "#000000",
        "sprite_scale": 2,
        "working_sheet": "C:/Users/Lenovo/Downloads/working.png",
        "working_sheet_cols": 4,
        "working_sheet_rows": 3,
        "completed_sheet": "C:/Users/Lenovo/Downloads/completed.png",
        "completed_sheet_cols": 4,
        "completed_sheet_rows": 3,
        "done_sheet": "C:/Users/Lenovo/Downloads/done.png",
        "done_sheet_cols": 4,
        "done_sheet_rows": 3,
        "working_gif": "",
        "completed_gif": "",
        "done_gif": "",
        "completed_subsample": 2,
        "done_subsample": 2,
    },
    "motion": {
        "default_opacity": 1.0,
        "bottom_walk_enabled": True,
        "walk_speed_px": 10,
        "bottom_margin": 30,
        "random_walk_enabled": True,
        "turn_chance": 0.08,
        "min_target_step": 80,
        "max_target_step": 280,
        "random_speed_enabled": True,
        "cat_speed_min_px": 6,
        "cat_speed_max_px": 14,
        "done_hold_ticks": 18,
    },
    "actions": {
        "on_working_enter": {"beep": False, "command": ""},
        "on_working_periodic": {"beep": False, "command": ""},
        "on_completed": {"beep": True, "command": ""},
    },
    "_comment": "turn_chance 越大转向越频繁；*_subsample=2 表示宽高缩小一半。",
}


def load_config() -> Dict[str, Any]:
    """Load user config (BOM-safe) and merge with defaults."""
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)

    with CONFIG_PATH.open("r", encoding="utf-8-sig") as f:
        user_cfg = json.load(f)

    cfg = dict(DEFAULT_CONFIG)
    for k, v in user_cfg.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            merged = dict(cfg[k])
            merged.update(v)
            cfg[k] = merged
        else:
            cfg[k] = v
    return cfg


def main() -> None:
    config = load_config()
    root = tk.Tk()
    DesktopPetApp(root, config)
    root.mainloop()


if __name__ == "__main__":
    main()








