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
        self.active_token = 0
        self.listening_active = False
        self.is_pinned = False

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
        self.root.geometry(f"{self.window_width}x130+{self.position['x']}+{self.position['y']}")

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
        # Full reset up front so the HUD instance is immediately ready for
        # the next wake-word trigger - this only tears down this window/
        # mainloop, it does not touch the outer wake-word listening loop
        # in AppRuntime.
        self.root = None
        self.widgets = {}
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
        if self.is_pinned:
            return
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

    def _transcribe_followup(self, timeout=5):
        with sr.Microphone() as source:
            self.service.recognizer.adjust_for_ambient_noise(source, duration=0.3)
            audio_capture = self.service.recognizer.listen(source, timeout=timeout, phrase_time_limit=5)
            return self.service.transcribe_audio(audio_capture)

    def _end_listening_stage(self):
        # Ends just the listening/transcribing attempt. While pinned, the
        # widget itself must stay open - revert to an idle mic state instead
        # of closing, so the user can click the mic pill again to re-listen.
        self.listening_active = False
        if self.is_pinned:
            status_pill = self.widgets.get("status_pill")
            query_label = self.widgets.get("query_label")
            if status_pill:
                status_pill.configure(text="• Mic Off", fg_color="#333333", text_color="#AAAAAA")
            if query_label:
                query_label.configure(text="")
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
        query_label = self.widgets["query_label"]
        response_label = self.widgets["response_label"]

        # UPDATED: Use the new status pill and image dot notation
        self._safe_gui(status_pill.configure, text="• Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
        if not skip_query_typing:
            self._safe_gui(query_label.configure, text="")
        self._safe_gui(response_label.configure, text="")

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
            status_pill.configure(text="• Listening", fg_color="#FF5F56", text_color="#FFFFFF")
            self._reset_close_timer(10)
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
        query_label = self.widgets["query_label"]
        # Pinned sessions get a slightly longer listening window since the
        # user has explicitly signalled they intend to keep talking.
        listen_timeout = 6 if self.is_pinned else 5
        try:
            # UPDATED: Style change for the status pill
            self._safe_gui(status_pill.configure, text="• Transcribing", fg_color="#FFBD2E", text_color="#000000")
            followup_q = self._transcribe_followup(timeout=listen_timeout)
            log_msg(f"Heard: '{followup_q}'", "INFO")

            if followup_q and not self.service.barge_in_triggered:
                self._safe_gui(query_label.configure, text="")
                self._safe_gui(status_pill.configure, text="• Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
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
        response_label = self.widgets["response_label"]
        query_label = self.widgets["query_label"]
        current_state = status_pill.cget("text")

        # UPDATED: Check for 'Listening' or other active states in the pill text
        if current_state in ["• Processing", "• Transcribing"] or pygame.mixer.music.get_busy():
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
            status_pill.configure(text="• Listening", fg_color="#FF5F56", text_color="#FFFFFF")
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
        status_pill.configure(text="• Listening", fg_color="#FF5F56", text_color="#FFFFFF")
        self._reset_close_timer(10)
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

        # UPDATED: Create a single, translucent background panel for the entire UI
        # fg_color includes a hex value with alpha (e.g., #555555CC) for transparency.
        # Use a lighter grey panel; overall window alpha provides translucency
        panel = ctk.CTkFrame(
            self.root,
            corner_radius=12,
            fg_color="#6B6F73",
            bg_color=self.transparent_color,
            border_width=1,
            border_color="#333333",
        )
        panel.pack(fill="both", expand=True, padx=12, pady=12)

        # Dragging bindings for the panel
        panel.bind("<ButtonPress-1>", self._start_move)
        panel.bind("<B1-Motion>", self._move_window)

        # UPDATED: Header container, no separate toolbar frame, just place controls on top of the translucent panel
        # Keep header same tint as panel to create a seamless frosted look
        header = ctk.CTkFrame(panel, fg_color="#6B6F73")
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
                self._reset_close_timer(10)
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

        # Green dot remains decorative
        ctk.CTkFrame(btn_frame, width=12, height=12, corner_radius=6, fg_color="#27C93F").pack(side="left", padx=4)

        # Flat, subtle Opacity Slider
        opacity_slider = ctk.CTkSlider(
            header,
            from_=0.2,
            to=1.0,
            width=60,
            height=10,
            command=self._change_opacity,
            border_width=0,
            button_color="#CCCCCC",
            button_hover_color="#FFFFFF",
            progress_color="#ffbd44",
            fg_color="#333333"
        )
        opacity_slider.set(1.0)
        opacity_slider.pack(side="left", padx=(15, 5))

        # UPDATED: The new pill-shaped green speaker button on the far right
        # Style mapped to image: green pill, white speaker icon.
        def toggle_dictation():
            self.service.dictation_enabled = not self.service.dictation_enabled
            if self.service.dictation_enabled:
                speaker_pill.configure(fg_color="#27C93F", hover_color="#20A032")
                pygame.mixer.music.set_volume(1.0)
            else:
                # Still use green for appearance, just toggle audio logic
                speaker_pill.configure(fg_color="#ff605c", hover_color="#d04040")
                pygame.mixer.music.set_volume(0.0)

        # Speaker emoji "🔊" as icon, green pill style
        speaker_pill = ctk.CTkButton(
            header,
            text="🔊",
            font=("Consolas", 14),
            text_color="#FFFFFF",
            fg_color="#27C93F" if self.service.dictation_enabled else "#ff605c",
            hover_color="#20A032",
            corner_radius=13, # Pill shape for height 26
            width=30,
            height=26,
            command=toggle_dictation,
        )
        speaker_pill.pack(side="right", padx=(5, 5))

        # UPDATED: Pill-shaped status button (replacing mic_btn) with text "• Mic Off"
        # Style mapped to image: dark, pill shape. targeted by statuses like Processing.
        # uses the specific dot '•' notation.
        status_pill = ctk.CTkButton(
            header,
            text="• Mic Off",
            font=("Consolas", 11, "bold"),
            text_color="#AAAAAA",
            fg_color="#333333", # Dark background like image
            hover_color="#444444",
            corner_radius=6,
            width=95,
            height=26,
            command=self._trigger_followup_or_interrupt,
        )
        status_pill.pack(side="right", padx=(5, 10))

        # Body area for text content
        body = ctk.CTkFrame(panel, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=15, pady=(5, 10))

        # Preserve dragging logic on body
        body.bind("<ButtonPress-1>", self._start_move)
        body.bind("<B1-Motion>", self._move_window)

        # Prompt frame (user text line)
        prompt_frame = ctk.CTkFrame(body, fg_color="transparent")
        prompt_frame.pack(fill="x", anchor="w")

        # Console-style user line formatting
        ctk.CTkLabel(prompt_frame, text=f"{self.config.user_name}:", font=("Consolas", 13, "bold"), text_color="#00FF9C").pack(side="left")
        ctk.CTkLabel(prompt_frame, text="~", font=("Consolas", 13, "bold"), text_color="#0066FF").pack(side="left", padx=(6, 0))
        ctk.CTkLabel(prompt_frame, text="$", font=("Consolas", 13, "bold"), text_color="#FF00FF").pack(side="left", padx=(6, 10))

        query_label = ctk.CTkLabel(prompt_frame, text="", font=("Consolas", 13), text_color="#FFFFFF")
        query_label.pack(side="left")

        # Response label (typing area)
        response_label = ctk.CTkLabel(body, text="", font=("Consolas", 13), text_color="#CCCCCC", justify="left", wraplength=370)
        response_label.pack(anchor="w", pady=(10, 0))

        # Register widgets with updated names
        self.widgets = {"status_pill": status_pill, "query_label": query_label, "response_label": response_label}

        # Initialize window state and processing
        # keep the overall slight transparency for the frosted effect
        try:
            self.root.attributes("-alpha", 0.95)
        except Exception:
            pass
        self.root.after(100, self._process_queue_events)
        self.root.mainloop()