import time
import random
import os
import asyncio
import threading
import pyaudio
import numpy as np
import openwakeword
from openwakeword.model import Model
import customtkinter as ctk
import speech_recognition as sr
from google import genai
from google.genai import types
from dotenv import load_dotenv
import edge_tts
import pygame

# --- 1. CONFIGURATION ---
WAKE_WORD = "alexa"

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    print("CRITICAL ERROR: API Key missing.")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
openwakeword.utils.download_models()
model = Model(wakeword_models=[WAKE_WORD])
recognizer = sr.Recognizer()

pygame.mixer.init()

audio = pyaudio.PyAudio()
mic_stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, 
                        input=True, frames_per_buffer=1280)

# Global flag to signal background main loop status
is_widget_open = False

# --- 2. INSTANT AUDIO MIXER ---
def pre_generate_and_play_audio(text, callback=None):
    voice = "en-US-GuyNeural"
    output_file = "temp_response.mp3"
    
    communicate = edge_tts.Communicate(text, voice)
    asyncio.run(communicate.save(output_file))
    
    def play_loop():
        pygame.mixer.music.load(output_file)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.music.unload()
        try:
            os.remove(output_file)
        except OSError:
            pass
        if callback:
            callback()

    threading.Thread(target=play_loop, daemon=True).start()

# --- 3. NATIVE TERMINAL HUD WITH LISTEN BUTTON ---
def display_response(initial_query, initial_answer):
    global is_widget_open
    is_widget_open = True
    
    root = ctk.CTk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    TRANSPARENT_COLOR = "#000001"
    root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
    root.configure(fg_color=TRANSPARENT_COLOR)
    
    window_width = 440
    screen_width = root.winfo_screenwidth()
    x_coordinate = screen_width - window_width - 25
    y_coordinate = 60 
    root.geometry(f"{window_width}x110+{x_coordinate}+{y_coordinate}")

    panel = ctk.CTkFrame(root, corner_radius=10, fg_color="#1E1E1E", bg_color=TRANSPARENT_COLOR, border_width=1, border_color="#333333")
    panel.pack(fill="both", expand=True, padx=10, pady=10)

    toolbar = ctk.CTkFrame(panel, corner_radius=10, fg_color="#2D2D2D", height=35)
    toolbar.pack(fill="x", padx=0, pady=0)
    toolbar.pack_propagate(False) 

    btn_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
    btn_frame.pack(side="left", padx=12)
    for color in ["#FF5F56", "#FFBD2E", "#27C93F"]:
        ctk.CTkFrame(btn_frame, width=12, height=12, corner_radius=6, fg_color=color).pack(side="left", padx=4)

    ctk.CTkLabel(toolbar, text="Tony: ~", font=("Consolas", 12, "bold"), text_color="#FFFFFF").pack(side="left", padx=25)
    
    # NEW: Voice status mic icon button on the toolbar right side
    mic_btn = ctk.CTkButton(
        toolbar, 
        text="● Mic Off", 
        font=("Consolas", 11, "bold"), 
        text_color="#AAAAAA",
        fg_color="#3A3A3A", 
        hover_color="#4A4A4A",
        corner_radius=5, 
        width=75, 
        height=24,
        command=lambda: trigger_followup()
    )
    mic_btn.pack(side="right", padx=10)

    body = ctk.CTkFrame(panel, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=15, pady=10)

    prompt_frame = ctk.CTkFrame(body, fg_color="transparent")
    prompt_frame.pack(fill="x", anchor="w")
    
    ctk.CTkLabel(prompt_frame, text="Tony:", font=("Consolas", 13, "bold"), text_color="#00FF9C").pack(side="left")
    ctk.CTkLabel(prompt_frame, text="~", font=("Consolas", 13, "bold"), text_color="#0066FF").pack(side="left", padx=(6,0))
    ctk.CTkLabel(prompt_frame, text="$", font=("Consolas", 13, "bold"), text_color="#FF00FF").pack(side="left", padx=(6,10))

    query_label = ctk.CTkLabel(prompt_frame, text="", font=("Consolas", 13), text_color="#FFFFFF")
    query_label.pack(side="left")

    response_label = ctk.CTkLabel(body, text="", font=("Consolas", 13), text_color="#CCCCCC", justify="left", wraplength=380)
    response_label.pack(anchor="w", pady=(10, 0))

    # Auto-close timer variable that we can reset
    close_timer_id = [None]

    def update_height(text_len):
        lines = (text_len // 50) + 1
        new_height = 110 + (lines * 22) 
        root.geometry(f"{window_width}x{new_height}+{x_coordinate}+{y_coordinate}")

    def safe_close():
        global is_widget_open
        is_widget_open = False
        for after_id in root.tk.eval('after info').split():
            root.after_cancel(after_id)
        root.quit()
        root.destroy()

    def reset_close_timer(seconds=8):
        if close_timer_id[0]:
            root.after_cancel(close_timer_id[0])
        close_timer_id[0] = root.after(seconds * 1000, safe_close)

    # --- THE RUNTIME WRITER SEQUENCE ---
    def run_sequence(q_str, a_str):
        if close_timer_id[0]:
            root.after_cancel(close_timer_id[0])
            
        mic_btn.configure(text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
        query_label.configure(text="")
        response_label.configure(text="")
        
        full_q = f"Hey Tony {q_str}"
        q_idx = [0]
        r_idx = [0]

        def type_query():
            if q_idx[0] < len(full_q):
                query_label.configure(text=full_q[:q_idx[0]+1] + "█")
                q_idx[0] += 1
                root.after(20, type_query)
            else:
                query_label.configure(text=full_q)
                root.after(50, type_response)

        def type_response():
            if r_idx[0] < len(a_str):
                current_t = a_str[:r_idx[0]+1]
                update_height(len(current_t))
                response_label.configure(text=current_t + "█")
                r_idx[0] += 1
                delay = 120 if a_str[r_idx[0]-1] in ['.','!','?'] else 20
                root.after(delay, type_response)
            else:
                response_label.configure(text=a_str)
                # Once audio + typing is complete, flash the microphone into listening mode
                def audio_finished_callback():
                    root.after(0, activate_listening_ui)
                
                pre_generate_and_play_audio(a_str, callback=audio_finished_callback)

        type_query()

    def activate_listening_ui():
        mic_btn.configure(text="● Listening", fg_color="#FF5F56", text_color="#FFFFFF")
        reset_close_timer(10) # Gives 10 seconds to follow up before auto-closing
        
        # Open threaded follow-up mic interceptor automatically
        threading.Thread(target=listen_for_followup, daemon=True).start()

    def listen_for_followup():
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.3)
            try:
                # Listens for a brief reply window
                audio_capture = recognizer.listen(source, timeout=5, phrase_time_limit=5)
                followup_q = recognizer.recognize_google(audio_capture).strip()
                
                if followup_q:
                    config = types.GenerateContentConfig(
                        system_instruction="You are a minimalist terminal assistant. Answer in 1 or 2 short sentences."
                    )
                    response = client.models.generate_content(
                        model='gemini-2.5-flash',
                        contents=followup_q,
                        config=config
                    )
                    # Loop back onto widget execution line smoothly
                    root.after(0, lambda: run_sequence(followup_q, response.text))
            except Exception:
                # If no audio or timeout occurs, let it naturally wind down to sleep state
                pass

    def trigger_followup():
        # Manual click fallback override
        reset_close_timer(10)
        activate_listening_ui()

    # Initial boot block trigger
    root.attributes("-alpha", 1.0)
    root.after(10, lambda: run_sequence(initial_query, initial_answer))
    root.mainloop()

# --- 4. MAIN WAKE LOOP ---
print("Agent is active. Follow-up listening mechanics operational.")

while True:
    try:
        # Only run the wake word engine if the active HUD overlay is closed
        if not is_widget_open:
            if mic_stream.is_stopped():
                mic_stream.start_stream()

            audio_data = mic_stream.read(1280, exception_on_overflow=False)
            audio_frame = np.frombuffer(audio_data, dtype=np.int16)
            prediction = model.predict(audio_frame)
            
            if model.prediction_buffer[WAKE_WORD][-1] > 0.5:
                print("\n[Trigger] Main wake event...")
                mic_stream.stop_stream()
                
                with sr.Microphone() as source:
                    recognizer.adjust_for_ambient_noise(source, duration=0.4)
                    try:
                        audio_capture = recognizer.listen(source, timeout=4, phrase_time_limit=6)
                        user_question = recognizer.recognize_google(audio_capture).strip()
                        
                        config = types.GenerateContentConfig(
                            system_instruction="You are a minimalist terminal assistant. Answer in 1 or 2 short sentences."
                        )
                        response = client.models.generate_content(
                            model='gemini-2.5-flash',
                            contents=user_question,
                            config=config
                        )
                        
                        display_response(user_question, response.text)
                            
                    except Exception as e:
                        print(f"Error: {e}")
                model.reset()
                time.sleep(1)
        else:
            time.sleep(0.5)

    except KeyboardInterrupt:
        break
    except Exception:
        time.sleep(1)