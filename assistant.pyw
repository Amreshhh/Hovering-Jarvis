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

# --- 1. SECURE CONFIGURATION & GLOBALS ---
load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
USER_NAME = os.getenv("USER_NAME", "User") 
WAKE_WORD = os.getenv("WAKE_WORD", "alexa").lower() 

if not GEMINI_API_KEY:
    print("CRITICAL ERROR: API Key missing in .env file.")
    exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# --- CONVERSATION HISTORY & STATE ---
MAX_EXCHANGES = 3  # Keeps the last 3 questions and 3 answers (6 messages total)
conversation_history = []
is_widget_open = False
dictation_enabled = True  

# --- AUDIO SETUP ---
try:
    openwakeword.utils.download_models()
    model = Model(wakeword_models=[WAKE_WORD])
except ValueError:
    print(f"\n[ERROR] '{WAKE_WORD}' is not a valid pre-trained wake word.")
    print("Defaulting to standard 'alexa'. Update your .env file.")
    WAKE_WORD = "alexa"
    model = Model(wakeword_models=[WAKE_WORD])

recognizer = sr.Recognizer()
pygame.mixer.init()

audio = pyaudio.PyAudio()
mic_stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, 
                        input=True, frames_per_buffer=1280)

# --- 2. SYNCHRONIZED AUDIO ENGINE ---
def generate_audio(text, on_ready_callback):
    def task():
        voice = "en-US-GuyNeural"
        output_file = "temp_response.mp3"
        communicate = edge_tts.Communicate(text, voice)
        asyncio.run(communicate.save(output_file))
        on_ready_callback(output_file)
    threading.Thread(target=task, daemon=True).start()

def play_and_cleanup(filepath, on_complete_callback):
    def task():
        pygame.mixer.music.load(filepath)
        pygame.mixer.music.set_volume(1.0 if dictation_enabled else 0.0)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            time.sleep(0.05)
        pygame.mixer.music.unload()
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError:
            pass
        if on_complete_callback:
            on_complete_callback()
    threading.Thread(target=task, daemon=True).start()

# --- 3. GEMINI API WITH SLIDING HISTORY ---
def get_gemini_response(user_text):
    global conversation_history
    
    conversation_history.append(
        types.Content(role="user", parts=[types.Part.from_text(text=user_text)])
    )
    
    if len(conversation_history) > (MAX_EXCHANGES * 2):
        conversation_history = conversation_history[-(MAX_EXCHANGES * 2):]
        
    config = types.GenerateContentConfig(
        system_instruction="You are a minimalist terminal assistant. Answer in 1 or 2 short sentences."
    )
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=conversation_history,
            config=config
        )
        
        if response.text:
            conversation_history.append(
                types.Content(role="model", parts=[types.Part.from_text(text=response.text)])
            )
            
        return response.text
        
    except Exception as e:
        conversation_history.pop()
        raise e

# --- 4. DRAGGABLE NATIVE HUD ---
def display_response(initial_query=None, initial_answer=None):
    global is_widget_open, dictation_enabled
    is_widget_open = True
    
    root = ctk.CTk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    TRANSPARENT_COLOR = "#000001"
    root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
    root.configure(fg_color=TRANSPARENT_COLOR)
    
    window_width = 440
    screen_width = root.winfo_screenwidth()
    
    pos = {
        "x": screen_width - window_width - 25,
        "y": 60,
        "drag_x": 0,
        "drag_y": 0
    }
    
    root.geometry(f"{window_width}x130+{pos['x']}+{pos['y']}")

    def safe_close():
        global is_widget_open
        is_widget_open = False
        for after_id in root.tk.eval('after info').split():
            root.after_cancel(after_id)
        root.quit()
        root.destroy()

    def start_move(event):
        pos["drag_x"] = event.x
        pos["drag_y"] = event.y

    def move_window(event):
        deltax = event.x - pos["drag_x"]
        deltay = event.y - pos["drag_y"]
        pos["x"] = root.winfo_x() + deltax
        pos["y"] = root.winfo_y() + deltay
        root.geometry(f"+{pos['x']}+{pos['y']}")

    panel = ctk.CTkFrame(root, corner_radius=12, fg_color="#1E1E1E", bg_color=TRANSPARENT_COLOR, border_width=1, border_color="#333333")
    panel.pack(fill="both", expand=True, padx=12, pady=12)

    toolbar = ctk.CTkFrame(panel, corner_radius=12, fg_color="#2D2D2D", height=35)
    toolbar.pack(fill="x", padx=0, pady=0)
    toolbar.pack_propagate(False) 

    for widget in (toolbar, panel):
        widget.bind("<ButtonPress-1>", start_move)
        widget.bind("<B1-Motion>", move_window)

    btn_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
    btn_frame.pack(side="left", padx=12)
    
    close_btn = ctk.CTkButton(
        btn_frame, text="", width=12, height=12, corner_radius=6, 
        fg_color="#FF5F56", hover_color="#C93F3A", command=safe_close,
        font=("Arial", 11, "bold") 
    )
    close_btn.pack(side="left", padx=4)

    def on_enter_close(e):
        close_btn.configure(text="×", text_color="#330000") 
        
    def on_leave_close(e):
        close_btn.configure(text="")

    close_btn.bind("<Enter>", on_enter_close)
    close_btn.bind("<Leave>", on_leave_close)
    
    for color in ["#FFBD2E", "#27C93F"]:
        ctk.CTkFrame(btn_frame, width=12, height=12, corner_radius=6, fg_color=color).pack(side="left", padx=4)

    def toggle_dictation():
        global dictation_enabled
        dictation_enabled = not dictation_enabled
        if dictation_enabled:
            dictation_btn.configure(text="🔊", fg_color="#008000", hover_color="#006400")
            pygame.mixer.music.set_volume(1.0) 
        else:
            dictation_btn.configure(text="🔇", fg_color="#FF0000", hover_color="#CC0000")
            pygame.mixer.music.set_volume(0.0) 

    initial_color = "#008000" if dictation_enabled else "#FF0000"
    initial_icon = "🔊" if dictation_enabled else "🔇"

    dictation_btn = ctk.CTkButton(
        toolbar, text=initial_icon, width=26, height=26, corner_radius=13, 
        font=("Segoe UI Emoji", 14), text_color="#FFFFFF",
        fg_color=initial_color, hover_color="#333333", command=toggle_dictation
    )
    dictation_btn.pack(side="left", padx=(10, 0))

    mic_btn = ctk.CTkButton(
        toolbar, text="● Mic Off", font=("Consolas", 11, "bold"), text_color="#AAAAAA",
        fg_color="#3A3A3A", hover_color="#4A4A4A", corner_radius=5, width=95, height=24,
        command=lambda: trigger_followup()
    )
    mic_btn.pack(side="right", padx=10)

    body = ctk.CTkFrame(panel, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=15, pady=10)
    body.bind("<ButtonPress-1>", start_move)
    body.bind("<B1-Motion>", move_window)

    prompt_frame = ctk.CTkFrame(body, fg_color="transparent")
    prompt_frame.pack(fill="x", anchor="w")
    
    ctk.CTkLabel(prompt_frame, text=f"{USER_NAME}:", font=("Consolas", 13, "bold"), text_color="#00FF9C").pack(side="left")
    ctk.CTkLabel(prompt_frame, text="~", font=("Consolas", 13, "bold"), text_color="#0066FF").pack(side="left", padx=(6,0))
    ctk.CTkLabel(prompt_frame, text="$", font=("Consolas", 13, "bold"), text_color="#FF00FF").pack(side="left", padx=(6,10))

    query_label = ctk.CTkLabel(prompt_frame, text="", font=("Consolas", 13), text_color="#FFFFFF")
    query_label.pack(side="left")

    response_label = ctk.CTkLabel(body, text="", font=("Consolas", 13), text_color="#CCCCCC", justify="left", wraplength=370)
    response_label.pack(anchor="w", pady=(10, 0))

    close_timer_id = [None]

    def update_height(text_len):
        lines = (text_len // 50) + 1
        new_height = 135 + (lines * 22) 
        root.geometry(f"{window_width}x{new_height}+{pos['x']}+{pos['y']}")

    def reset_close_timer(seconds=8):
        if close_timer_id[0]:
            root.after_cancel(close_timer_id[0])
        close_timer_id[0] = root.after(seconds * 1000, safe_close)

    def run_sequence(q_str, a_str, is_followup=False, skip_query_typing=False):
        if close_timer_id[0]:
            root.after_cancel(close_timer_id[0])
            
        mic_btn.configure(text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
        if not skip_query_typing:
            query_label.configure(text="")
        response_label.configure(text="")
        
        if not is_followup:
            a_str = f"Hey {USER_NAME}, {a_str}"
            
        q_idx = [0]
        r_idx = [0]
        
        def type_query():
            if q_idx[0] < len(q_str):
                query_label.configure(text=q_str[:q_idx[0]+1] + "█")
                q_idx[0] += 1
                root.after(20, type_query)
            else:
                query_label.configure(text=q_str)
                start_response_phase()

        def start_response_phase():
            if dictation_enabled:
                response_label.configure(text="Thinking...█")
                generate_audio(a_str, on_audio_ready)
            else:
                response_label.configure(text="")
                type_response()

        def on_audio_ready(filepath):
            response_label.configure(text="") 
            play_and_cleanup(filepath, audio_finished_callback)
            type_response()

        def type_response():
            if r_idx[0] < len(a_str):
                current_t = a_str[:r_idx[0]+1]
                update_height(len(current_t))
                response_label.configure(text=current_t + "█")
                r_idx[0] += 1
                delay = 120 if a_str[r_idx[0]-1] in ['.','!','?'] else 40
                root.after(delay, type_response)
            else:
                response_label.configure(text=a_str)
                if not dictation_enabled:
                    root.after(500, activate_listening_ui)

        def audio_finished_callback():
            root.after(0, activate_listening_ui)

        if skip_query_typing:
            start_response_phase()
        else:
            type_query()

    def activate_listening_ui(is_first=False):
        mic_btn.configure(text="● Listening", fg_color="#FF5F56", text_color="#FFFFFF")
        reset_close_timer(10)
        threading.Thread(target=lambda: listen_for_followup(is_first), daemon=True).start()

    def listen_for_followup(is_first=False):
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.3)
            try:
                audio_capture = recognizer.listen(source, timeout=5, phrase_time_limit=5)
                
                # Visual indicator that it's converting speech to text
                root.after(0, lambda: mic_btn.configure(text="● Transcribing", fg_color="#FFBD2E", text_color="#000000"))
                followup_q = recognizer.recognize_google(audio_capture).strip()
                
                if followup_q:
                    # Instantly display transcribed text and switch to processing
                    root.after(0, lambda: query_label.configure(text=followup_q))
                    root.after(0, lambda: mic_btn.configure(text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA"))
                    try:
                        answer = get_gemini_response(followup_q)
                        # Skip the typing animation for the query since we already displayed it
                        root.after(0, lambda: run_sequence(followup_q, answer, is_followup=not is_first, skip_query_typing=True))
                    except Exception as e:
                        print(f"Follow-up Error: {e}")
                        root.after(0, lambda: response_label.configure(text="API Error occurred."))
                        root.after(3000, safe_close)
            
            except sr.WaitTimeoutError:
                root.after(0, safe_close)
            except Exception:
                root.after(0, safe_close)

    def trigger_followup():
        reset_close_timer(10)
        activate_listening_ui(is_first=False)

    root.attributes("-alpha", 1.0)
    
    if initial_query and initial_answer:
        root.after(10, lambda: run_sequence(initial_query, initial_answer, is_followup=False))
    else:
        # Launch immediately into listening mode
        root.after(10, lambda: activate_listening_ui(is_first=True))
        
    root.mainloop()

# --- 5. MAIN WAKE LOOP ---
print(f"Agent online. Configured User: '{USER_NAME}' | Wake word: '{WAKE_WORD}'")

while True:
    try:
        if not is_widget_open:
            if mic_stream.is_stopped():
                mic_stream.start_stream()

            audio_data = mic_stream.read(1280, exception_on_overflow=False)
            audio_frame = np.frombuffer(audio_data, dtype=np.int16)
            prediction = model.predict(audio_frame)
            
            if model.prediction_buffer[WAKE_WORD][-1] > 0.5:
                print(f"\n[Trigger] Detected wake phrase: '{WAKE_WORD}'")
                mic_stream.stop_stream()
                model.reset()
                
                # Instantly display the widget empty and let it handle the listening
                display_response()
                
                time.sleep(1)
        else:
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nShutting down assistant...")
        break
    except Exception as e:
        time.sleep(1)