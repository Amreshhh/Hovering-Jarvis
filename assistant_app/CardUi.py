import threading

import customtkinter as ctk
import pygame
import speech_recognition as sr

from .Logger import log_msg


class AssistantHUD:
    def __init__(self, service):
        self.service = service
        self.config = service.config
        self.root = None
        self.window_width = 440
        self.transparent_color = "#000001"
        self.position = None
        self.close_timer_id = [None]
        self.widgets = {}

    def _build_window(self):
        self.root = ctk.CTk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.wm_attributes("-transparentcolor", self.transparent_color)
        self.root.configure(fg_color=self.transparent_color)

        screen_width = self.root.winfo_screenwidth()
        self.position = {"x": screen_width - self.window_width - 25, "y": 60, "drag_x": 0, "drag_y": 0}
        self.root.geometry(f"{self.window_width}x130+{self.position['x']}+{self.position['y']}")

    def _safe_gui(self, func, *args, **kwargs):
        if self.service.is_widget_open and self.root and self.root.winfo_exists():
            try:
                self.root.after(0, lambda: func(*args, **kwargs))
            except Exception:
                pass

    def _safe_close(self):
        with self.service.state_lock:
            self.service.is_widget_open = False
        log_msg("Closing Widget.", "INFO")
        if self.root:
            for after_id in self.root.tk.eval("after info").split():
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
            self.root.quit()
            self.root.destroy()

    def _start_move(self, event):
        self.position["drag_x"] = event.x
        self.position["drag_y"] = event.y

    def _move_window(self, event):
        delta_x = event.x - self.position["drag_x"]
        delta_y = event.y - self.position["drag_y"]
        self.position["x"] = self.root.winfo_x() + delta_x
        self.position["y"] = self.root.winfo_y() + delta_y
        self.root.geometry(f"+{self.position['x']}+{self.position['y']}")

    def _change_opacity(self, value):
        self.root.attributes("-alpha", float(value))

    def _reset_close_timer(self, seconds=8):
        if self.close_timer_id[0]:
            try:
                self.root.after_cancel(self.close_timer_id[0])
            except Exception:
                pass
        self.close_timer_id[0] = self.root.after(seconds * 1000, self._safe_close)

    def _update_height(self, text_len):
        lines = (text_len // 50) + 1
        new_height = 135 + (lines * 22)
        self.root.geometry(f"{self.window_width}x{new_height}+{self.position['x']}+{self.position['y']}")

    def _transcribe_followup(self):
        with sr.Microphone() as source:
            self.service.recognizer.adjust_for_ambient_noise(source, duration=0.3)
            audio_capture = self.service.recognizer.listen(source, timeout=5, phrase_time_limit=5)
            return self.service.transcribe_audio(audio_capture)

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

        mic_btn = self.widgets["mic_btn"]
        query_label = self.widgets["query_label"]
        response_label = self.widgets["response_label"]

        self._safe_gui(mic_btn.configure, text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
        if not skip_query_typing:
            self._safe_gui(query_label.configure, text="")
        self._safe_gui(response_label.configure, text="")

        if not is_followup:
            a_str = f"Hey {self.config.user_name}, {a_str}"

        q_idx = [0]
        r_idx = [0]
        response_render_complete = [False]
        audio_playback_complete = [not self.service.dictation_enabled]
        response_step = 3

        def activate_listening_ui(is_first=False):
            if self.service.barge_in_triggered:
                return
            mic_btn.configure(text="● Listening", fg_color="#FF5F56", text_color="#FFFFFF")
            self._reset_close_timer(10)
            threading.Thread(target=lambda: self._listen_for_followup(is_first), daemon=True).start()

        def finalize_response_stage():
            if response_render_complete[0] and audio_playback_complete[0]:
                activate_listening_ui()

        def type_query():
            if self.service.barge_in_triggered:
                return
            if q_idx[0] < len(q_str):
                query_label.configure(text=q_str[: q_idx[0] + 1] + "█")
                q_idx[0] += 1
                self.root.after(20, type_query)
            else:
                query_label.configure(text=q_str)
                start_response_phase()

        def start_response_phase():
            if self.service.barge_in_triggered:
                return
            if self.service.dictation_enabled:
                response_label.configure(text="Thinking...█")
                self.service.generate_audio(a_str, on_audio_ready)
                type_response()
            else:
                response_label.configure(text="")
                type_response()

        def on_audio_ready(filepath):
            if self.service.barge_in_triggered:
                return
            if filepath:
                self.service.play_and_cleanup(filepath, audio_finished_callback)
            else:
                audio_finished_callback()

        def type_response():
            if self.service.barge_in_triggered:
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
                if not self.service.barge_in_triggered:
                    finalize_response_stage()

        def audio_finished_callback():
            if not self.service.barge_in_triggered:
                audio_playback_complete[0] = True
                self._safe_gui(finalize_response_stage)

        if skip_query_typing:
            start_response_phase()
        else:
            type_query()

    def _listen_for_followup(self, is_first=False):
        mic_btn = self.widgets["mic_btn"]
        query_label = self.widgets["query_label"]
        try:
            self._safe_gui(mic_btn.configure, text="● Transcribing", fg_color="#FFBD2E", text_color="#000000")
            followup_q = self._transcribe_followup()
            log_msg(f"Heard: '{followup_q}'", "INFO")

            if followup_q and not self.service.barge_in_triggered:
                self._safe_gui(query_label.configure, text="")
                self._safe_gui(mic_btn.configure, text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
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
                    self._safe_gui(self._safe_close)
            else:
                self._safe_gui(self._safe_close)
        except Exception:
            self._safe_gui(self._safe_close)

    def _trigger_followup_or_interrupt(self):
        mic_btn = self.widgets["mic_btn"]
        response_label = self.widgets["response_label"]
        query_label = self.widgets["query_label"]
        current_state = mic_btn.cget("text")

        if current_state in ["● Processing", "● Transcribing"] or pygame.mixer.music.get_busy():
            log_msg("Barge-in triggered by user button tap! Interrupting...", "WARNING")
            with self.service.state_lock:
                self.service.barge_in_triggered = True
            if pygame.mixer.music.get_busy():
                pygame.mixer.music.stop()
            mic_btn.configure(text="● Listening", fg_color="#FF5F56", text_color="#FFFFFF")
            response_label.configure(text="")
            query_label.configure(text="")
            self.root.after(100, self._reset_barge_flag_and_listen)
        else:
            self._reset_close_timer(10)
            self._activate_listening_ui(is_first=False)

    def _reset_barge_flag_and_listen(self):
        with self.service.state_lock:
            self.service.barge_in_triggered = False
        self._reset_close_timer(10)
        threading.Thread(target=lambda: self._listen_for_followup(False), daemon=True).start()

    def _activate_listening_ui(self, is_first=False):
        if self.service.barge_in_triggered:
            return
        mic_btn = self.widgets["mic_btn"]
        mic_btn.configure(text="● Listening", fg_color="#FF5F56", text_color="#FFFFFF")
        self._reset_close_timer(10)
        threading.Thread(target=lambda: self._listen_for_followup(is_first), daemon=True).start()

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

        panel = ctk.CTkFrame(
            self.root,
            corner_radius=12,
            fg_color="#1E1E1E",
            bg_color=self.transparent_color,
            border_width=1,
            border_color="#333333",
        )
        panel.pack(fill="both", expand=True, padx=12, pady=12)

        toolbar = ctk.CTkFrame(panel, corner_radius=12, fg_color="#2D2D2D", bg_color="#1E1E1E", height=35)
        toolbar.pack(fill="x", padx=0, pady=0)
        toolbar.pack_propagate(False)

        for widget in (toolbar, panel):
            widget.bind("<ButtonPress-1>", self._start_move)
            widget.bind("<B1-Motion>", self._move_window)

        btn_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        btn_frame.pack(side="left", padx=12)

        close_btn = ctk.CTkButton(
            btn_frame,
            text="",
            width=12,
            height=12,
            corner_radius=6,
            fg_color="#FF5F56",
            hover_color="#C93F3A",
            command=self._safe_close,
            font=("Arial", 11, "bold"),
        )
        close_btn.pack(side="left", padx=4)
        close_btn.bind("<Enter>", lambda e: close_btn.configure(text="×", text_color="#330000"))
        close_btn.bind("<Leave>", lambda e: close_btn.configure(text=""))

        for color in ["#FFBD2E", "#27C93F"]:
            ctk.CTkFrame(btn_frame, width=12, height=12, corner_radius=6, fg_color=color).pack(side="left", padx=4)

        opacity_slider = ctk.CTkSlider(
            toolbar,
            from_=0.2,
            to=1.0,
            width=60,
            height=10,
            command=self._change_opacity,
            border_width=0,
            button_color="#555555",
            button_hover_color="#777777",
            progress_color="#888888",
        )
        opacity_slider.set(1.0)
        opacity_slider.pack(side="left", padx=(15, 5))

        def toggle_dictation():
            self.service.dictation_enabled = not self.service.dictation_enabled
            if self.service.dictation_enabled:
                dictation_btn.configure(text="🔊", fg_color="#008000", hover_color="#006400")
                pygame.mixer.music.set_volume(1.0)
            else:
                dictation_btn.configure(text="🔇", fg_color="#FF0000", hover_color="#CC0000")
                pygame.mixer.music.set_volume(0.0)

        dictation_btn = ctk.CTkButton(
            toolbar,
            text="🔊" if self.service.dictation_enabled else "🔇",
            width=26,
            height=26,
            corner_radius=13,
            font=("Segoe UI Emoji", 14),
            text_color="#FFFFFF",
            fg_color="#008000" if self.service.dictation_enabled else "#FF0000",
            hover_color="#333333",
            command=toggle_dictation,
        )
        dictation_btn.pack(side="right", padx=(5, 5))

        mic_btn = ctk.CTkButton(
            toolbar,
            text="● Mic Off",
            font=("Consolas", 11, "bold"),
            text_color="#AAAAAA",
            fg_color="#3A3A3A",
            hover_color="#4A4A4A",
            corner_radius=5,
            width=95,
            height=24,
            command=self._trigger_followup_or_interrupt,
        )
        mic_btn.pack(side="right", padx=(5, 10))

        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=15, pady=10)
        body.bind("<ButtonPress-1>", self._start_move)
        body.bind("<B1-Motion>", self._move_window)

        prompt_frame = ctk.CTkFrame(body, fg_color="transparent")
        prompt_frame.pack(fill="x", anchor="w")

        ctk.CTkLabel(prompt_frame, text=f"{self.config.user_name}:", font=("Consolas", 13, "bold"), text_color="#00FF9C").pack(side="left")
        ctk.CTkLabel(prompt_frame, text="~", font=("Consolas", 13, "bold"), text_color="#0066FF").pack(side="left", padx=(6, 0))
        ctk.CTkLabel(prompt_frame, text="$", font=("Consolas", 13, "bold"), text_color="#FF00FF").pack(side="left", padx=(6, 10))

        query_label = ctk.CTkLabel(prompt_frame, text="", font=("Consolas", 13), text_color="#FFFFFF")
        query_label.pack(side="left")

        response_label = ctk.CTkLabel(body, text="", font=("Consolas", 13), text_color="#CCCCCC", justify="left", wraplength=370)
        response_label.pack(anchor="w", pady=(10, 0))

        self.widgets = {"mic_btn": mic_btn, "query_label": query_label, "response_label": response_label}

        self.root.attributes("-alpha", 1.0)
        self.root.after(100, self._process_queue_events)
        self.root.after(10, self._activate_listening_ui)
        self.root.mainloop()
