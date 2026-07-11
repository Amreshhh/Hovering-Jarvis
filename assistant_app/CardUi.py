import ctypes
import threading

import customtkinter as ctk
import pygame
import speech_recognition as sr

from .Logger import log_msg

# GetSystemMetrics indices for the full virtual desktop (spans every
# connected monitor, not just the primary one). Coordinates can be negative
# for monitors placed to the left of or above the primary display.
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


class AssistantAlexa:
    # theme1 is the original look; theme2 is a darker, more minimal "toolbar"
    # look (separate-toned header bar, muted grayscale controls). Both share
    # the exact same widget structure - only colors/fonts/labels differ - so
    # toggling between them is a safe in-place reconfigure, never a rebuild.
    THEMES = {
        "theme1": {
            "bullet": "•",
            "panel_fg": "#6B6F73",
            "panel_border": "#333333",
            "header_fg": "#6B6F73",
            "slider_track": "#333333",
            "slider_button": "#CCCCCC",
            "slider_button_hover": "#FFFFFF",
            "slider_progress": "#ffbd44",
            "idle_pill_bg": "#333333",
            "idle_pill_text": "#AAAAAA",
            "status_corner_radius": 6,
            "status_height": 26,
            "dictation_font": "Consolas",
            "dictation_swap_icon": False,
            "dictation_on_color": "#27C93F",
            "dictation_on_hover": "#20A032",
            "dictation_off_color": "#ff605c",
            "dictation_off_hover": "#d04040",
            # theme1 reads as the "light" toolbar look, so the drag handle
            # uses red to stay visible against its lighter panel.
            "drag_handle_color": "#FF3B30",
            "drag_handle_hover": "#D9342B",
        },
        "theme2": {
            "bullet": "●",
            "panel_fg": "#1E1E1E",
            "panel_border": "#333333",
            "header_fg": "#2D2D2D",
            "slider_track": "#3A3A3A",
            "slider_button": "#555555",
            "slider_button_hover": "#777777",
            "slider_progress": "#888888",
            "idle_pill_bg": "#3A3A3A",
            "idle_pill_text": "#AAAAAA",
            "status_corner_radius": 5,
            "status_height": 24,
            "dictation_font": "Segoe UI Emoji",
            "dictation_swap_icon": True,
            "dictation_on_color": "#008000",
            "dictation_on_hover": "#006400",
            "dictation_off_color": "#FF0000",
            "dictation_off_hover": "#CC0000",
            # theme2 is the "dark" toolbar look - green drag handle.
            "drag_handle_color": "#27C93F",
            "drag_handle_hover": "#20A032",
        },
    }

    def __init__(self, service):
        self.service = service
        self.config = service.config
        self.root = None
        self.window_width = 440
        self.window_height = 130
        self.min_window_width = 440
        self.min_window_height = 130
        # Floor the auto-grow height logic can't shrink below - stays at the
        # built-in minimum until the user drags the resize grip, at which
        # point it sticks to whatever size they chose (persists across
        # queries and reopens, same as the theme/pinned state below).
        self.height_floor = self.min_window_height
        # Ceiling the auto-grow logic won't cross on its own - past this, the
        # response body scrolls internally instead of the window ballooning
        # down the screen. A manual resize-grip drag can still go higher.
        self.max_auto_height = 420
        self._resize_drag = None
        self.transparent_color = "#000001"
        self.position = None
        self.close_timer_id = [None]
        self.widgets = {}
        self.theme_widgets = {}
        # Up to the last 3 question/answer exchanges, kept visible in the
        # scrollable body instead of each new answer replacing the last.
        # Each entry: {"prompt_frame", "query_label", "response_label", "copy_btn"}.
        self.history_blocks = []
        self.max_history_blocks = 3
        self.theme = "theme1"
        self.active_token = 0
        self.listening_active = False
        self.is_pinned = False
        self._meeting_prev_pinned = False
        self._meeting_prev_dictation = True

    def _build_window(self):
        self.root = ctk.CTk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # Slight overall window transparency to simulate acrylic
        try:
            self.root.attributes("-alpha", 0.95)
        except Exception:
            pass
        self.root.wm_attributes("-transparentcolor", self.transparent_color)
        self.root.configure(fg_color=self.transparent_color)

        screen_width = self.root.winfo_screenwidth()
        self.position = {"x": screen_width - self.window_width - 25, "y": 60, "drag_x": 0, "drag_y": 0}
        self._apply_geometry()

    def _apply_geometry(self):
        if not self.root or not self.root.winfo_exists() or not self.position:
            return
        self.root.geometry(f"{self.window_width}x{self.window_height}+{self.position['x']}+{self.position['y']}")

    def _virtual_screen_bounds(self):
        # Spans every connected monitor rather than just the primary one, so
        # dragging isn't clamped back onto a single display. Falls back to
        # the primary screen's own size if the Win32 call is unavailable.
        try:
            user32 = ctypes.windll.user32
            x = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
            y = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
            width = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
            height = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
            if width > 0 and height > 0:
                return x, y, width, height
        except Exception:
            pass
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def set_position(self, x, y):
        if self.position is None:
            self.position = {"x": int(x), "y": int(y), "drag_x": 0, "drag_y": 0}
            return

        if self.root and self.root.winfo_exists():
            vx, vy, vwidth, vheight = self._virtual_screen_bounds()
            min_x, min_y = vx, vy
            max_x = max(min_x, vx + vwidth - self.window_width)
            max_y = max(min_y, vy + vheight - self.window_height)
            self.position["x"] = max(min_x, min(int(x), max_x))
            self.position["y"] = max(min_y, min(int(y), max_y))
            self._apply_geometry()
        else:
            self.position["x"] = int(x)
            self.position["y"] = int(y)

    def _safe_gui(self, func, *args, **kwargs):
        if self.service.is_widget_open and self.root and self.root.winfo_exists():
            try:
                self.root.after(0, lambda: func(*args, **kwargs))
            except Exception:
                pass

    def _safe_close(self, force=False):
        if self.is_pinned and not force:
            log_msg("Close suppressed - widget is pinned.", "INFO")
            return
        if force and self.service.meeting_mode_enabled:
            self.service.stop_meeting_mode()
        self.is_pinned = False
        with self.service.state_lock:
            self.service.is_widget_open = False
            self.listening_active = False
        # Stop any in-progress voice dictation immediately - closing the
        # widget should silence Alexa's speech too, not just hide the UI.
        try:
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
        except Exception:
            pass
        log_msg("Closing Widget.", "INFO")
        root = self.root
        # Full reset up front so the Alexa instance is immediately ready for
        # the next wake-word trigger - this only tears down this window/
        # mainloop, it does not touch the outer wake-word listening loop
        # in AppRuntime.
        self.root = None
        self.widgets = {}
        self.history_blocks = []
        self.close_timer_id = [None]
        if root:
            # Hide the window immediately and synchronously - withdraw()
            # issues a direct OS-level hide rather than relying on Tk's
            # event loop, so the widget visually disappears right away
            # instead of leaving a "ghost" frame on screen once mainloop
            # stops pumping events (a known quirk with overrideredirect +
            # topmost + color-key transparent windows on Windows).
            try:
                root.withdraw()
            except Exception:
                pass

            def _teardown():
                for after_id in root.tk.eval("after info").split():
                    try:
                        root.after_cancel(after_id)
                    except Exception:
                        pass
                try:
                    root.quit()
                    root.destroy()
                except Exception:
                    pass

            # Deferred by one tick: this is invoked as the close button's
            # own Tcl command callback, and destroying a widget tree
            # synchronously from inside its own callback raises
            # "can't delete Tcl command". Scheduling it via `after` runs
            # the teardown once that callback frame has fully returned -
            # the same safe context the natural auto-timeout close already
            # uses (it fires from a plain `after` callback, never a
            # button's own click handler).
            root.after(1, _teardown)

    def _start_move(self, event):
        self.position["drag_x"] = event.x_root - self.position["x"]
        self.position["drag_y"] = event.y_root - self.position["y"]

    def _move_window(self, event):
        self.set_position(event.x_root - self.position["drag_x"], event.y_root - self.position["drag_y"])

    def _start_resize(self, event):
        self._resize_drag = {
            "x": event.x_root,
            "y": event.y_root,
            "w": self.window_width,
            "h": self.window_height,
        }

    def _do_resize(self, event):
        if not self._resize_drag or not self.root or not self.root.winfo_exists():
            return
        vx, vy, vwidth, vheight = self._virtual_screen_bounds()
        max_width = max(self.min_window_width, vx + vwidth - self.position["x"])
        max_height = max(self.min_window_height, vy + vheight - self.position["y"])

        dx = event.x_root - self._resize_drag["x"]
        dy = event.y_root - self._resize_drag["y"]
        self.window_width = int(max(self.min_window_width, min(self._resize_drag["w"] + dx, max_width)))
        self.window_height = int(max(self.min_window_height, min(self._resize_drag["h"] + dy, max_height)))
        # A manual drag sets a new sticky floor so the next auto-grow/shrink
        # pass (triggered by typing out a response) never snaps the widget
        # back below the size the user just chose.
        self.height_floor = self.window_height

        self._apply_geometry()
        self._update_wrap_length()

    def _update_wrap_length(self):
        wrap = max(120, self.window_width - 70)
        for block in self.history_blocks:
            block["response_label"].configure(wraplength=wrap)

    def _change_opacity(self, value):
        self.root.attributes("-alpha", float(value))

    def _flash_widget_color(self, widget, attr="text_color", color="#27C93F", delay=350):
        # Purely a color flash for feedback - never touches label *text*,
        # since the typewriter animation keeps rewriting response text on
        # its own schedule and a swapped-then-restored string could race it.
        if not widget:
            return
        original = widget.cget(attr)
        widget.configure(**{attr: color})
        self.root.after(delay, lambda: widget.winfo_exists() and widget.configure(**{attr: original}))

    def _copy_text_from_label(self, label):
        # Right-click (or Ctrl+C for the current/latest response) copies the
        # text as-is - CTkLabel text isn't natively selectable, so this is
        # the only way to grab it.
        if not label:
            return
        content = label.cget("text").rstrip("█").strip()
        if not content:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
        except Exception:
            return
        self._flash_widget_color(label)

    def _copy_response_text(self, event=None):
        self._copy_text_from_label(self.widgets.get("response_label"))

    def _copy_all_history(self):
        # Copies all currently visible Q&A blocks (up to the last 3), not
        # just the latest one - the header's "copy everything" action.
        parts = []
        for block in self.history_blocks:
            q = block["query_label"].cget("text").rstrip("█").strip()
            a = block["response_label"].cget("text").rstrip("█").strip()
            if not q and not a:
                continue
            parts.append(f"{self.config.user_name}: {q}\n{a}")
        content = "\n\n".join(parts)
        if not content:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(content)
        except Exception:
            return
        self._flash_widget_color(self.widgets.get("copy_all_btn"))

    def _bullet(self):
        return self.THEMES[self.theme]["bullet"]

    def _apply_theme(self):
        # Re-colors the already-built widgets in place - never rebuilds the
        # window, so a theme toggle can't reintroduce the kind of crash a
        # structural rebuild risks.
        w = self.theme_widgets
        if not w:
            return
        theme = self.THEMES[self.theme]
        bullet = theme["bullet"]

        w["panel"].configure(fg_color=theme["panel_fg"], border_color=theme["panel_border"])
        w["header"].configure(fg_color=theme["header_fg"])
        w["opacity_slider"].configure(
            button_color=theme["slider_button"],
            button_hover_color=theme["slider_button_hover"],
            progress_color=theme["slider_progress"],
            fg_color=theme["slider_track"],
        )
        w["body"].configure(
            fg_color=theme["panel_fg"],
            scrollbar_fg_color=theme["slider_track"],
            scrollbar_button_color=theme["slider_button"],
            scrollbar_button_hover_color=theme["slider_button_hover"],
        )
        w["drag_handle"].configure(fg_color=theme["drag_handle_color"])
        w["copy_all_btn"].configure(fg_color=theme["idle_pill_bg"], hover_color=theme["slider_track"], text_color=theme["idle_pill_text"])
        for block in self.history_blocks:
            block["copy_btn"].configure(hover_color=theme["idle_pill_bg"], text_color=theme["idle_pill_text"])

        status_pill = w["status_pill"]
        status_pill.configure(corner_radius=theme["status_corner_radius"], height=theme["status_height"])
        current_text = status_pill.cget("text")
        if current_text.endswith("Mic Off"):
            status_pill.configure(text=f"{bullet} Mic Off", fg_color=theme["idle_pill_bg"], text_color=theme["idle_pill_text"])
        elif current_text.endswith("Processing"):
            status_pill.configure(text=f"{bullet} Processing")
        elif current_text.endswith("Transcribing"):
            status_pill.configure(text=f"{bullet} Transcribing")
        elif current_text.endswith("Listening"):
            status_pill.configure(text=f"{bullet} Listening")

        speaker_pill = w["speaker_pill"]
        speaker_pill.configure(font=(theme["dictation_font"], 14))
        if self.service.dictation_enabled:
            speaker_pill.configure(
                text="🔊",
                fg_color=theme["dictation_on_color"],
                hover_color=theme["dictation_on_hover"],
            )
        else:
            speaker_pill.configure(
                text="🔇" if theme["dictation_swap_icon"] else "🔊",
                fg_color=theme["dictation_off_color"],
                hover_color=theme["dictation_off_hover"],
            )

        meeting_pill = w["meeting_pill"]
        meeting_pill.configure(font=(theme["dictation_font"], 14))
        if not self.service.system_audio_available:
            meeting_pill.configure(fg_color="#555555", hover_color="#555555", text_color="#888888")
        elif self.service.meeting_mode_enabled:
            meeting_pill.configure(fg_color=theme["dictation_on_color"], hover_color=theme["dictation_on_hover"], text_color="#FFFFFF")
        else:
            meeting_pill.configure(fg_color=theme["idle_pill_bg"], hover_color="#444444", text_color="#FFFFFF")

    def _create_qa_block(self):
        # Appends a fresh, blank query/response row to the scrollable
        # history instead of reusing a single pair of labels - this is what
        # lets the last few exchanges stay visible instead of each new
        # answer replacing the last. Oldest block is evicted once the cap
        # is exceeded. Returns (query_label, response_label) for the new
        # block and also registers them as the "current" ones in
        # self.widgets, since most of the sequencing code just wants
        # whatever the latest block is.
        body = self.widgets["body"]
        theme = self.THEMES[self.theme]

        prompt_frame = ctk.CTkFrame(body, fg_color="transparent")
        prompt_frame.pack(fill="x", anchor="w", pady=(10, 0) if self.history_blocks else (0, 0))
        prompt_frame.bind("<ButtonPress-1>", self._start_move)
        prompt_frame.bind("<B1-Motion>", self._move_window)

        ctk.CTkLabel(prompt_frame, text=f"{self.config.user_name}:", font=("Consolas", 13, "bold"), text_color="#00FF9C").pack(side="left")
        ctk.CTkLabel(prompt_frame, text="~", font=("Consolas", 13, "bold"), text_color="#0066FF").pack(side="left", padx=(6, 0))
        ctk.CTkLabel(prompt_frame, text="$", font=("Consolas", 13, "bold"), text_color="#FF00FF").pack(side="left", padx=(6, 10))

        query_label = ctk.CTkLabel(prompt_frame, text="", font=("Consolas", 13), text_color="#FFFFFF")
        query_label.pack(side="left")

        response_label = ctk.CTkLabel(
            body, text="", font=("Consolas", 13), text_color="#CCCCCC", justify="left", wraplength=max(120, self.window_width - 70)
        )
        response_label.pack(anchor="w", pady=(10, 0))
        response_label.bind("<ButtonPress-1>", self._start_move)
        response_label.bind("<B1-Motion>", self._move_window)
        response_label.bind("<Button-3>", lambda e, lbl=response_label: self._copy_text_from_label(lbl))

        # Small copy icon for this specific response, right-aligned on its
        # own query line - distinct from the header's "copy everything".
        copy_btn = ctk.CTkButton(
            prompt_frame,
            text="⧉",
            width=22,
            height=18,
            corner_radius=4,
            font=("Consolas", 11),
            fg_color="transparent",
            hover_color=theme["idle_pill_bg"],
            text_color=theme["idle_pill_text"],
            command=lambda lbl=response_label: self._copy_text_from_label(lbl),
        )
        copy_btn.pack(side="right", padx=(4, 0))

        block = {"prompt_frame": prompt_frame, "query_label": query_label, "response_label": response_label, "copy_btn": copy_btn}
        self.history_blocks.append(block)
        if len(self.history_blocks) > self.max_history_blocks:
            oldest = self.history_blocks.pop(0)
            oldest["prompt_frame"].destroy()
            oldest["response_label"].destroy()

        self.widgets["query_label"] = query_label
        self.widgets["response_label"] = response_label

        body.update_idletasks()
        body._parent_canvas.yview_moveto(1.0)
        return query_label, response_label

    def _discard_current_block(self):
        # Drops the latest (in-progress) block entirely rather than leaving
        # a blank/partial entry in the history - used when a barge-in
        # interrupts a response before it ever finished.
        if not self.history_blocks:
            return
        block = self.history_blocks.pop()
        block["prompt_frame"].destroy()
        block["response_label"].destroy()

    def _reset_close_timer(self, seconds=5):
        if self.is_pinned:
            return
        if self.close_timer_id[0]:
            try:
                self.root.after_cancel(self.close_timer_id[0])
            except Exception:
                pass
        self.close_timer_id[0] = self.root.after(seconds * 1000, self._safe_close)

    def _update_height(self, text_len):
        # Estimate chars-per-line from the current (possibly user-resized)
        # width instead of a fixed 50, so a widened widget wraps into fewer,
        # longer lines and doesn't grow taller than it needs to.
        chars_per_line = max(20, (self.window_width - 90) // 7)
        lines = (text_len // chars_per_line) + 1
        computed = min(135 + (lines * 22), self.max_auto_height)
        self.window_height = max(computed, self.height_floor)
        self._apply_geometry()
        # Keep the actively-typing response in view as it grows past the
        # visible area, instead of leaving the scroll position wherever it
        # happened to be when this block was first created.
        body = self.widgets.get("body")
        if body:
            body.update_idletasks()
            body._parent_canvas.yview_moveto(1.0)

    def _transcribe_followup(self, timeout=5):
        with sr.Microphone() as source:
            self.service.recognizer.adjust_for_ambient_noise(source, duration=0.3)
            audio_capture = self.service.recognizer.listen(source, timeout=timeout, phrase_time_limit=5)
            return self.service.transcribe_audio(audio_capture)

    def _end_listening_stage(self):
        # Ends just the listening/transcribing attempt. While pinned, the
        # widget itself must stay open - revert to an idle mic state instead
        # of closing, so the user can click the mic pill again to re-listen.
        # Nothing was heard, so no new history block was ever created here -
        # the last completed exchange stays exactly as it is.
        self.listening_active = False
        if self.is_pinned:
            theme = self.THEMES[self.theme]
            status_pill = self.widgets.get("status_pill")
            if status_pill:
                status_pill.configure(text=f"{self._bullet()} Mic Off", fg_color=theme["idle_pill_bg"], text_color=theme["idle_pill_text"])
        else:
            self._safe_close()

    def _process_queue_events(self):
        while not self.service.widget_command_queue.empty():
            cmd = self.service.widget_command_queue.get_nowait()
            if cmd["action"] == "start_sequence":
                self._safe_gui(self._run_sequence, cmd["q"], cmd["a"], cmd["is_followup"], cmd["skip_typing"])
            elif cmd["action"] == "close":
                self._safe_close()

        if self.service.is_widget_open:
            self.root.after(100, self._process_queue_events)

    def _run_sequence(self, q_str, a_str, is_followup=False, skip_query_typing=False):
        if self.close_timer_id[0]:
            try:
                self.root.after_cancel(self.close_timer_id[0])
            except Exception:
                pass

        # Bump the generation token so any stale callback from a previous,
        # interrupted sequence (e.g. a delayed audio-playback thread) can
        # recognize it no longer belongs to the active sequence and no-op
        # instead of firing a duplicate "listening" activation.
        self.active_token += 1
        token = self.active_token
        self.listening_active = False

        def is_stale():
            return token != self.active_token or self.service.barge_in_triggered

        status_pill = self.widgets["status_pill"]
        # A fresh, blank row appended to the scrollable history rather than
        # reusing the previous turn's labels - keeps the last few exchanges
        # visible instead of each new answer overwriting the last.
        query_label, response_label = self._create_qa_block()

        # UPDATED: Use the new status pill and image dot notation
        self._safe_gui(status_pill.configure, text=f"{self._bullet()} Processing", fg_color="#3A3A3A", text_color="#AAAAAA")

        if not is_followup:
            a_str = f"Hey {self.config.user_name}, {a_str}"

        q_idx = [0]
        r_idx = [0]
        response_render_complete = [False]
        audio_playback_complete = [False]
        audio_ready = [False]
        audio_started = [False]
        pending_audio_file = [None]
        response_step = 3

        def activate_listening_ui(is_first=False):
            if is_stale():
                return
            # UPDATED: Style change for the status pill
            status_pill.configure(text=f"{self._bullet()} Listening", fg_color="#FF5F56", text_color="#FFFFFF")
            self._reset_close_timer(5)
            self._start_listening_thread(is_first)

        def finalize_response_stage():
            if response_render_complete[0] and audio_playback_complete[0]:
                activate_listening_ui()

        def start_audio_playback_if_ready():
            if is_stale() or audio_started[0] or not audio_ready[0]:
                return
            # Checked live (not captured once at generation time) so that an
            # unmute after the audio finished generating still plays it.
            if not pending_audio_file[0] or not self.service.dictation_enabled:
                audio_playback_complete[0] = True
                finalize_response_stage()
                return
            audio_started[0] = True
            self.service.play_and_cleanup(pending_audio_file[0], audio_finished_callback)

        def type_query():
            if is_stale():
                return
            if q_idx[0] < len(q_str):
                query_label.configure(text=q_str[: q_idx[0] + 1] + "█")
                q_idx[0] += 1
                self.root.after(20, type_query)
            else:
                query_label.configure(text=q_str)
                start_response_phase()

        def start_response_phase():
            if is_stale():
                return
            if self.service.dictation_enabled:
                response_label.configure(text="Thinking...█")
            type_response()

        def on_audio_ready(filepath):
            if is_stale():
                return
            pending_audio_file[0] = filepath
            audio_ready[0] = True
            # Start playback as soon as the audio is ready so voice dictation
            # and the text typewriter run simultaneously instead of the
            # audio waiting for the typing animation to finish first.
            start_audio_playback_if_ready()

        def type_response():
            if is_stale():
                return
            if r_idx[0] < len(a_str):
                next_index = min(r_idx[0] + response_step, len(a_str))
                current_text = a_str[:next_index]
                self._update_height(len(current_text))
                response_label.configure(text=current_text + "█")
                r_idx[0] = next_index
                last_char = a_str[next_index - 1]
                delay = 70 if last_char in [".", "!", "?"] else 15
                self.root.after(delay, type_response)
            else:
                response_label.configure(text=a_str)
                response_render_complete[0] = True
                if not is_stale():
                    start_audio_playback_if_ready()

        def audio_finished_callback():
            if not is_stale():
                audio_playback_complete[0] = True
                self._safe_gui(finalize_response_stage)

        # Kick off TTS generation immediately, in parallel with whatever
        # typing animation runs next, instead of waiting for the query text
        # to finish typing first - minimizes the delay between the answer
        # existing and its audio being ready. Always generated (even while
        # muted) so a later unmute still has something to play back.
        self.service.generate_audio(a_str, on_audio_ready)

        if skip_query_typing:
            start_response_phase()
        else:
            type_query()

    def _listen_for_followup(self, is_first=False):
        status_pill = self.widgets["status_pill"]
        # Pinned sessions get a slightly longer listening window since the
        # user has explicitly signalled they intend to keep talking.
        listen_timeout = 6 if self.is_pinned else 5
        try:
            # UPDATED: Style change for the status pill
            self._safe_gui(status_pill.configure, text=f"{self._bullet()} Transcribing", fg_color="#FFBD2E", text_color="#000000")
            followup_q = self._transcribe_followup(timeout=listen_timeout)
            log_msg(f"Heard: '{followup_q}'", "INFO")

            if followup_q and not self.service.barge_in_triggered:
                # The new query/answer gets its own block via _run_sequence
                # below - nothing to clear on the previous (still valid,
                # still-displayed) history entry here.
                self._safe_gui(status_pill.configure, text=f"{self._bullet()} Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
                try:
                    answer = self.service.process_query_master(followup_q)
                    if not self.service.barge_in_triggered:
                        self._safe_gui(
                            self._run_sequence,
                            followup_q,
                            answer,
                            not is_first,
                            False,
                        )
                except Exception as exc:
                    log_msg(f"Pipeline failure: {exc}", "ERROR")
                    self._safe_gui(self._end_listening_stage)
            else:
                self._safe_gui(self._end_listening_stage)
        except Exception:
            self._safe_gui(self._end_listening_stage)

    def _trigger_followup_or_interrupt(self):
        status_pill = self.widgets["status_pill"]
        current_state = status_pill.cget("text")

        # UPDATED: Check for 'Listening' or other active states in the pill text.
        # Suffix-matched (not exact-matched) so it stays correct regardless of
        # which theme's bullet character ("•" vs "●") is currently in use.
        if current_state.endswith("Processing") or current_state.endswith("Transcribing") or pygame.mixer.music.get_busy():
            log_msg("Barge-in triggered by user button tap! Interrupting...", "WARNING")
            with self.service.state_lock:
                self.service.barge_in_triggered = True
            # Invalidate the running sequence immediately so any of its
            # in-flight callbacks (e.g. a lagging audio-finished callback)
            # recognize themselves as stale the instant we barge in, rather
            # than only after barge_in_triggered gets reset 100ms later.
            self.active_token += 1
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
            status_pill.configure(text=f"{self._bullet()} Listening", fg_color="#FF5F56", text_color="#FFFFFF")
            # The in-progress block never finished, so drop it from history
            # entirely rather than leaving a blank/partial entry behind.
            self._discard_current_block()
            self.root.after(100, self._reset_barge_flag_and_listen)
        else:
            self._reset_close_timer(5)
            self._activate_listening_ui(is_first=False)

    def _reset_barge_flag_and_listen(self):
        with self.service.state_lock:
            self.service.barge_in_triggered = False
        self._reset_close_timer(5)
        self.listening_active = False
        self._start_listening_thread(False)

    def _start_listening_thread(self, is_first=False):
        # Central gate so at most one follow-up capture thread is ever
        # active — button mashing or an interrupt racing with the normal
        # post-response activation can no longer spawn duplicate listeners.
        if self.listening_active:
            return
        self.listening_active = True
        threading.Thread(target=lambda: self._listen_for_followup(is_first), daemon=True).start()

    def _activate_listening_ui(self, is_first=False):
        if self.service.barge_in_triggered:
            return
        status_pill = self.widgets["status_pill"]
        status_pill.configure(text=f"{self._bullet()} Listening", fg_color="#FF5F56", text_color="#FFFFFF")
        self._reset_close_timer(5)
        self._start_listening_thread(is_first)

    def capture_initial_query(self):
        try:
            q = self.service.capture_microphone_text(timeout=4, phrase_time_limit=6, ambient_noise=0.4)
            if q:
                log_msg(f"Processing Query: {q}", "INFO")
                answer = self.service.process_query_master(q)
                if answer:
                    self.service.widget_command_queue.put(
                        {"action": "start_sequence", "q": q, "a": answer, "is_followup": False, "skip_typing": False}
                    )
            else:
                self.service.widget_command_queue.put({"action": "close"})
        except Exception as exc:
            log_msg(f"Initial capture aborted: {exc}", "WARNING")
            self.service.widget_command_queue.put({"action": "close"})

    def display_response(self):
        with self.service.state_lock:
            self.service.is_widget_open = True
            self.service.barge_in_triggered = False

        self._build_window()
        theme = self.THEMES[self.theme]

        # UPDATED: Create a single, translucent background panel for the entire UI
        # fg_color includes a hex value with alpha (e.g., #555555CC) for transparency.
        # Use a lighter grey panel; overall window alpha provides translucency
        panel = ctk.CTkFrame(
            self.root,
            corner_radius=12,
            fg_color=theme["panel_fg"],
            bg_color=self.transparent_color,
            border_width=1,
            border_color=theme["panel_border"],
        )
        panel.pack(fill="both", expand=True, padx=12, pady=12)

        # Dragging bindings for the panel
        panel.bind("<ButtonPress-1>", self._start_move)
        panel.bind("<B1-Motion>", self._move_window)

        # UPDATED: Header container, no separate toolbar frame, just place controls on top of the translucent panel.
        # header_fg is intentionally its own theme value (distinct from
        # panel_fg in theme2) so the header can read as a separate toolbar
        # bar without needing a second nested frame.
        header = ctk.CTkFrame(panel, fg_color=theme["header_fg"])
        header.pack(fill="x", side="top", padx=12, pady=(10, 5))

        # Mac Dots Frame
        btn_frame = ctk.CTkFrame(header, fg_color="transparent")
        btn_frame.pack(side="left", padx=(2, 10))

        # UPDATED: Stylized dots. Red dot elongates to a "Close" label on hover.
        CLOSE_IDLE_WIDTH = 12
        CLOSE_HOVER_WIDTH = 46

        def _close_idle_style():
            close_btn.configure(width=CLOSE_IDLE_WIDTH, text="")

        def _close_hover_style():
            close_btn.configure(width=CLOSE_HOVER_WIDTH, text="Close", text_color="#330000")

        close_btn = ctk.CTkButton(
            btn_frame,
            text="",
            width=CLOSE_IDLE_WIDTH,
            height=12,
            corner_radius=6,
            fg_color="#FF5F56",
            hover_color="#C93F3A",
            command=lambda: self._safe_close(force=True),
            font=("Arial", 9, "bold"),
        )
        close_btn.pack(side="left", padx=4)
        close_btn.bind("<Enter>", lambda e: _close_hover_style())
        close_btn.bind("<Leave>", lambda e: _close_idle_style())

        # UPDATED: Yellow dot doubles as a pin toggle - keeps the widget from
        # auto-timing-out until clicked again. On hover it elongates to the
        # right (away from the green dot) and reveals a "Pin"/"Unpin" label.
        PIN_IDLE_WIDTH = 12
        PIN_HOVER_WIDTH = 42

        def _pin_idle_style():
            if self.is_pinned:
                pin_btn.configure(width=PIN_IDLE_WIDTH, text="📌", fg_color="#FFD666", hover_color="#FFD666", text_color="#402d00")
            else:
                pin_btn.configure(width=PIN_IDLE_WIDTH, text="", fg_color="#FFBD2E", hover_color="#E0A527")

        def _pin_hover_style():
            pin_btn.configure(width=PIN_HOVER_WIDTH, text="Unpin" if self.is_pinned else "Pin", text_color="#402d00")

        def toggle_pin():
            self.is_pinned = not self.is_pinned
            if self.is_pinned:
                if self.close_timer_id[0]:
                    try:
                        self.root.after_cancel(self.close_timer_id[0])
                    except Exception:
                        pass
                    self.close_timer_id[0] = None
            else:
                self._reset_close_timer(5)
            # Cursor is still over the dot right after the click, so keep the
            # elongated label in sync instead of waiting for the next hover.
            _pin_hover_style()

        pin_btn = ctk.CTkButton(
            btn_frame,
            text="",
            width=PIN_IDLE_WIDTH,
            height=12,
            corner_radius=6,
            fg_color="#FFBD2E",
            hover_color="#E0A527",
            command=toggle_pin,
            font=("Arial", 9, "bold"),
        )
        pin_btn.pack(side="left", padx=4)
        pin_btn.bind("<Enter>", lambda e: _pin_hover_style())
        pin_btn.bind("<Leave>", lambda e: _pin_idle_style())

        # UPDATED: Green dot toggles between theme1 and theme2. On hover it
        # elongates like the other two dots, revealing a "Theme" label.
        THEME_IDLE_WIDTH = 12
        THEME_HOVER_WIDTH = 50

        def _theme_idle_style():
            theme_btn.configure(width=THEME_IDLE_WIDTH, text="")

        def _theme_hover_style():
            theme_btn.configure(width=THEME_HOVER_WIDTH, text="Theme", text_color="#123d1a")

        def toggle_theme():
            self.theme = "theme2" if self.theme == "theme1" else "theme1"
            self._apply_theme()
            # Cursor is still over the dot right after the click, so keep
            # the elongated label in sync instead of waiting for the next hover.
            _theme_hover_style()

        theme_btn = ctk.CTkButton(
            btn_frame,
            text="",
            width=THEME_IDLE_WIDTH,
            height=12,
            corner_radius=6,
            fg_color="#27C93F",
            hover_color="#20A032",
            command=toggle_theme,
            font=("Arial", 9, "bold"),
        )
        theme_btn.pack(side="left", padx=4)
        theme_btn.bind("<Enter>", lambda e: _theme_hover_style())
        theme_btn.bind("<Leave>", lambda e: _theme_idle_style())

        # Flat, subtle Opacity Slider
        opacity_slider = ctk.CTkSlider(
            header,
            from_=0.2,
            to=1.0,
            width=60,
            height=10,
            command=self._change_opacity,
            border_width=0,
            button_color=theme["slider_button"],
            button_hover_color=theme["slider_button_hover"],
            progress_color=theme["slider_progress"],
            fg_color=theme["slider_track"],
        )
        opacity_slider.set(1.0)
        opacity_slider.pack(side="left", padx=(15, 5))

        # Copies every visible Q&A block (up to the last 3) at once - the
        # per-response "⧉" button on each row only copies that one response.
        copy_all_btn = ctk.CTkButton(
            header,
            text="📋",
            width=26,
            height=22,
            corner_radius=5,
            font=("Consolas", 12),
            fg_color=theme["idle_pill_bg"],
            hover_color=theme["slider_track"],
            text_color=theme["idle_pill_text"],
            command=self._copy_all_history,
        )
        copy_all_btn.pack(side="left", padx=(5, 0))

        # UPDATED: The new pill-shaped green speaker button on the far right
        # Style mapped to image: green pill, white speaker icon.
        def _sync_speaker_pill():
            active_theme = self.THEMES[self.theme]
            if self.service.dictation_enabled:
                speaker_pill.configure(text="🔊", fg_color=active_theme["dictation_on_color"], hover_color=active_theme["dictation_on_hover"])
            else:
                icon = "🔇" if active_theme["dictation_swap_icon"] else "🔊"
                speaker_pill.configure(text=icon, fg_color=active_theme["dictation_off_color"], hover_color=active_theme["dictation_off_hover"])

        def toggle_dictation():
            self.service.dictation_enabled = not self.service.dictation_enabled
            pygame.mixer.music.set_volume(1.0 if self.service.dictation_enabled else 0.0)
            _sync_speaker_pill()

        # Speaker emoji "🔊" as icon, green pill style
        speaker_pill = ctk.CTkButton(
            header,
            text="🔊",
            font=(theme["dictation_font"], 14),
            text_color="#FFFFFF",
            fg_color=theme["dictation_on_color"] if self.service.dictation_enabled else theme["dictation_off_color"],
            hover_color=theme["dictation_on_hover"] if self.service.dictation_enabled else theme["dictation_off_hover"],
            corner_radius=13, # Pill shape for height 26
            width=30,
            height=26,
            command=toggle_dictation,
        )
        speaker_pill.pack(side="right", padx=(5, 5))

        # Meeting mode toggle: switches listening source to system/loopback
        # audio (e.g. a call's speaker output) so the assistant can answer
        # questions it overhears in a meeting instead of the user's mic.
        # Forces the widget pinned open and mutes TTS while active so
        # answers only ever show as text and never talk over the call.
        def _sync_meeting_pill():
            active_theme = self.THEMES[self.theme]
            if not self.service.system_audio_available:
                meeting_pill.configure(text="🖥", fg_color="#555555", hover_color="#555555", text_color="#888888")
            elif self.service.meeting_mode_enabled:
                meeting_pill.configure(text="🖥", fg_color=active_theme["dictation_on_color"], hover_color=active_theme["dictation_on_hover"], text_color="#FFFFFF")
            else:
                meeting_pill.configure(text="🖥", fg_color=active_theme["idle_pill_bg"], hover_color="#444444", text_color="#FFFFFF")

        def toggle_meeting_mode():
            if not self.service.system_audio_available:
                log_msg("Meeting mode unavailable - no WASAPI loopback device found.", "WARNING")
                return
            if self.service.meeting_mode_enabled:
                self.service.stop_meeting_mode()
                self.is_pinned = self._meeting_prev_pinned
                if not self.is_pinned:
                    self._reset_close_timer(5)
                self.service.dictation_enabled = self._meeting_prev_dictation
                pygame.mixer.music.set_volume(1.0 if self.service.dictation_enabled else 0.0)
            else:
                self._meeting_prev_pinned = self.is_pinned
                self._meeting_prev_dictation = self.service.dictation_enabled
                self.is_pinned = True
                if self.close_timer_id[0]:
                    try:
                        self.root.after_cancel(self.close_timer_id[0])
                    except Exception:
                        pass
                    self.close_timer_id[0] = None
                self.service.dictation_enabled = False
                pygame.mixer.music.set_volume(0.0)
                self.service.start_meeting_mode()
            _sync_meeting_pill()
            _sync_speaker_pill()

        meeting_pill = ctk.CTkButton(
            header,
            text="🖥",
            font=(theme["dictation_font"], 14),
            text_color="#FFFFFF",
            corner_radius=13,
            width=30,
            height=26,
            command=toggle_meeting_mode,
        )
        meeting_pill.pack(side="right", padx=(5, 5))
        _sync_meeting_pill()

        # UPDATED: Pill-shaped status button (replacing mic_btn) with text "• Mic Off"
        # Style mapped to image: dark, pill shape. targeted by statuses like Processing.
        # uses the specific dot '•' notation.
        status_pill = ctk.CTkButton(
            header,
            text=f"{theme['bullet']} Mic Off",
            font=("Consolas", 11, "bold"),
            text_color=theme["idle_pill_text"],
            fg_color=theme["idle_pill_bg"],
            hover_color="#444444",
            corner_radius=theme["status_corner_radius"],
            width=95,
            height=theme["status_height"],
            command=self._trigger_followup_or_interrupt,
        )
        status_pill.pack(side="right", padx=(5, 10))

        # Body area for text content - scrollable so a long response grows a
        # themed scrollbar instead of pushing the window past a sane height
        # (see max_auto_height in _update_height). fg_color is pinned to the
        # panel's own color (never "transparent") - CTkScrollableFrame
        # resolves a "transparent" fg_color by inheriting bg_color up its
        # own internal parent chain rather than the panel's actual color,
        # which here lands on the root window's OS-level transparency key
        # and punches a see-through hole showing whatever is behind the
        # widget. Matching panel_fg exactly keeps it visually seamless with
        # the panel behind it, same as the old plain fg_color="transparent"
        # CTkFrame looked, without invoking that resolution path.
        body = ctk.CTkScrollableFrame(
            panel,
            fg_color=theme["panel_fg"],
            scrollbar_fg_color=theme["slider_track"],
            scrollbar_button_color=theme["slider_button"],
            scrollbar_button_hover_color=theme["slider_button_hover"],
        )
        body.pack(fill="both", expand=True, padx=15, pady=(5, 10))

        # Preserve dragging logic on body - bound on both the content frame
        # and its backing canvas, since the canvas is what's actually under
        # the cursor in any empty space below short content.
        body.bind("<ButtonPress-1>", self._start_move)
        body.bind("<B1-Motion>", self._move_window)
        body._parent_canvas.bind("<ButtonPress-1>", self._start_move)
        body._parent_canvas.bind("<B1-Motion>", self._move_window)

        # self.widgets needs "status_pill" and "body" before the first
        # history block can be built, since _create_qa_block() reads both.
        self.widgets = {"status_pill": status_pill, "body": body}
        self.history_blocks = []
        self._create_qa_block()

        # Ctrl+C anywhere while the widget is focused copies the current
        # (latest) response, since CTkLabel offers no native text selection.
        self.root.bind("<Control-c>", self._copy_response_text)
        self.root.bind("<Control-C>", self._copy_response_text)

        # Resize grip - bottom-right corner handle the user can drag to
        # widen/lengthen the widget. Placed last so it stacks visually above
        # the packed panel/body content, overlaid via place() rather than
        # pack() so it doesn't consume layout space.
        resize_grip = ctk.CTkLabel(
            panel,
            text="⇲",
            font=("Arial", 13, "bold"),
            text_color="#AAAAAA",
            fg_color="transparent",
            cursor="size_nw_se",
            width=16,
            height=16,
        )
        resize_grip.place(relx=1.0, rely=1.0, x=-3, y=-3, anchor="se")
        resize_grip.bind("<ButtonPress-1>", self._start_resize)
        resize_grip.bind("<B1-Motion>", self._do_resize)

        # Small centered drag handle sitting just above the header (the
        # "functions tab") at the very top of the widget - a sleek pill
        # (mirroring a bottom-sheet grab handle), not a full-width strip.
        # Overlaid via place() like the resize grip, so it doesn't reserve
        # any layout space of its own. Green in the dark theme, red in the
        # light theme (see drag_handle_color per theme).
        drag_handle = ctk.CTkFrame(
            panel,
            fg_color=theme["drag_handle_color"],
            corner_radius=3,
            width=48,
            height=5,
            cursor="fleur",
        )
        drag_handle.place(relx=0.5, rely=0.0, y=3, anchor="n")
        drag_handle.bind("<ButtonPress-1>", self._start_move)
        drag_handle.bind("<B1-Motion>", self._move_window)
        drag_handle.bind("<Enter>", lambda e: drag_handle.configure(fg_color=self.THEMES[self.theme]["drag_handle_hover"]))
        drag_handle.bind("<Leave>", lambda e: drag_handle.configure(fg_color=self.THEMES[self.theme]["drag_handle_color"]))

        # self.widgets already carries status_pill/body/query_label/
        # response_label (set above) - just add the header's copy-all button.
        self.widgets["copy_all_btn"] = copy_all_btn
        self.theme_widgets = {
            "panel": panel,
            "header": header,
            "opacity_slider": opacity_slider,
            "speaker_pill": speaker_pill,
            "meeting_pill": meeting_pill,
            "status_pill": status_pill,
            "body": body,
            "drag_handle": drag_handle,
            "copy_all_btn": copy_all_btn,
        }

        # Initialize window state and processing
        # keep the overall slight transparency for the frosted effect
        try:
            self.root.attributes("-alpha", 0.95)
        except Exception:
            pass
        self.root.after(100, self._process_queue_events)
        self.root.mainloop()