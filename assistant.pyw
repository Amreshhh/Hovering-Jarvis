import time
import os
import asyncio
import threading
import re
import socket
import json
import pyaudio
import numpy as np
import logging
import zipfile
import concurrent.futures
import random

# --- NEAT TERMINAL LOGGER ---
class LogColors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    GREEN = '\033[92m'
    WARNING = '\033[93m'
    ERROR = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'

def log_msg(msg, level="INFO"):
    if level == "INFO":
        print(f"{LogColors.BLUE}[*] {msg}{LogColors.ENDC}")
    elif level == "SUCCESS":
        print(f"{LogColors.GREEN}[+] {msg}{LogColors.ENDC}")
    elif level == "WARNING":
        print(f"{LogColors.WARNING}[!] {msg}{LogColors.ENDC}")
    elif level == "ERROR":
        print(f"{LogColors.ERROR}[X] {msg}{LogColors.ENDC}")
    elif level == "TRIGGER":
        print(f"\n{LogColors.HEADER}{LogColors.BOLD}>>> {msg}{LogColors.ENDC}")

# Suppress warnings
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  
logging.getLogger('openwakeword').setLevel(logging.ERROR)
os.environ['PYGAME_HIDE_SUPPORT_PROMPT'] = "hide"

import openwakeword
from openwakeword.model import Model
import customtkinter as ctk
import speech_recognition as sr
from dotenv import load_dotenv
import edge_tts
import pygame
import pyttsx3
import nltk
from nltk.corpus import wordnet
from groq import Groq  
from google import genai
from google.genai import types

# --- 1. SECURE CONFIGURATION & PRE-FLIGHT CHECK ---
load_dotenv()
USER_NAME = os.getenv("USER_NAME", "User") 
WAKE_WORD = os.getenv("WAKE_WORD", "alexa").lower() 
VOSK_MODEL_PATH = "vosk-model-small-en-us" # Ensure this folder exists!

# Dynamically gather all keys from .env
raw_groq_keys = [os.getenv(f"GROQ_API_KEY_{i}") for i in range(1, 4) if os.getenv(f"GROQ_API_KEY_{i}")]
raw_gemini_keys = [os.getenv(f"GEMINI_API_KEY_{i}") for i in range(1, 3) if os.getenv(f"GEMINI_API_KEY_{i}")]

# Strict Pre-Flight Validation
if not raw_groq_keys:
    log_msg("CRITICAL ERROR: Minimum one GROQ_API_KEY_x is required in the .env file.", "ERROR")
    exit(1)
if not raw_gemini_keys:
    log_msg("CRITICAL ERROR: Minimum one GEMINI_API_KEY_x is required in the .env file.", "ERROR")
    exit(1)

# Initialize Client Pools for Load Balancing
groq_clients = [Groq(api_key=key) for key in raw_groq_keys]
gemini_clients = [genai.Client(api_key=key) for key in raw_gemini_keys]
log_msg(f"API Pools Loaded: {len(groq_clients)} Groq nodes, {len(gemini_clients)} Gemini nodes.", "SUCCESS")


# --- CONVERSATION HISTORY & STATE ---
MAX_EXCHANGES = 3  
conversation_history = []
is_widget_open = False
dictation_enabled = True  

# --- NETWORK CHECKER ---
def check_internet(timeout=1):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("8.8.8.8", 53))
        return True
    except OSError:
        return False

# --- OFFLINE DICTIONARY SETUP ---
log_msg("Initializing offline dictionary...", "INFO")
try:
    nltk.data.find('corpora/wordnet.zip')
    _ = wordnet.synsets("hello")
except (LookupError, zipfile.BadZipFile):
    log_msg("WordNet missing. Downloading fresh copy...", "WARNING")
    nltk.download('wordnet', quiet=True, force=True)
    _ = wordnet.synsets("hello")

# --- AUDIO & TTS SETUP ---
try:
    openwakeword.utils.download_models()
    model = Model(wakeword_models=[WAKE_WORD])
except ValueError:
    WAKE_WORD = "alexa"
    model = Model(wakeword_models=[WAKE_WORD])

recognizer = sr.Recognizer()
pygame.mixer.init()
offline_tts = pyttsx3.init() 
offline_tts.setProperty('rate', 170) 

audio = pyaudio.PyAudio()
mic_stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, 
                        input=True, frames_per_buffer=1280)

# --- 2. DUAL SYNCHRONIZED AUDIO ENGINE ---
def generate_audio(text, on_ready_callback):
    is_online = check_internet()
    
    def online_task():
        output_file = "temp_response.mp3"
        communicate = edge_tts.Communicate(text, "en-US-GuyNeural")
        asyncio.run(communicate.save(output_file))
        on_ready_callback(output_file)
        
    def offline_task():
        output_file = "temp_response.wav"
        offline_tts.save_to_file(text, output_file)
        offline_tts.runAndWait()
        on_ready_callback(output_file)

    target = online_task if is_online else offline_task
    threading.Thread(target=target, daemon=True).start()

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
        except OSError: pass
        if on_complete_callback:
            on_complete_callback()
    threading.Thread(target=task, daemon=True).start()

# --- 3. THE MASTER ROUTING ENGINE (WITH LOAD BALANCING) ---
def run_with_timeout(func, timeout_sec, *args, **kwargs):
    """Forces slow APIs to fail fast by wrapping them in a strict timeout thread."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except concurrent.futures.TimeoutError:
            raise TimeoutError(f"API timed out after {timeout_sec}s.")

def get_offline_definition(user_text):
    match = re.search(r'^(define|meaning of|what is the meaning of)\s+(.+)', user_text.lower().strip())
    if not match: return None
    word = re.sub(r'[^\w\s]', '', match.group(2).strip())
    synsets = wordnet.synsets(word)
    if synsets: return f"{word.capitalize()} means: {synsets[0].definition()}."
    return None

def get_groq_response(client, user_text, timeout_val):
    global conversation_history
    history_copy = conversation_history[- (MAX_EXCHANGES * 2):]
    messages = [{"role": "system", "content": "You are a fast, minimalist assistant. Answer in 1 or 2 short sentences."}] + history_copy + [{"role": "user", "content": user_text}]
    response = client.chat.completions.create(messages=messages, model="llama-3.1-8b-instant", timeout=timeout_val)
    ans = response.choices[0].message.content
    conversation_history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": ans}])
    return ans

def get_gemini_response(client, user_text):
    global conversation_history
    history_copy = []
    for msg in conversation_history[- (MAX_EXCHANGES * 2):]:
        role = "user" if msg["role"] == "user" else "model"
        history_copy.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"])]))
    history_copy.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
    
    config = types.GenerateContentConfig(system_instruction="You are a fast, minimalist assistant. Answer in 1 or 2 short sentences.")
    response = client.models.generate_content(model='gemini-2.5-flash', contents=history_copy, config=config)
    ans = response.text
    conversation_history.extend([{"role": "user", "content": user_text}, {"role": "assistant", "content": ans}])
    return ans

def process_query_master(user_text):
    """Dynamic Fallback Chain with Prioritized Randomizer"""
    
    # 1. OFFLINE FORK
    if not check_internet():
        log_msg("Offline Mode Active.", "WARNING")
        ans = get_offline_definition(user_text)
        return ans if ans else "I am currently offline. I can only provide dictionary definitions right now."

    # 2. ONLINE WATERFALL (Shuffle pools for load balancing)
    active_groqs = list(groq_clients)
    random.shuffle(active_groqs)
    
    active_geminis = list(gemini_clients)
    random.shuffle(active_geminis)

    # Tier 1: Try all Groq nodes sequentially
    for i, current_client in enumerate(active_groqs):
        # 1.0s timeout for the very first attempt, 2.0s for the backups
        timeout_val = 1.0 if i == 0 else 2.0 
        try:
            log_msg(f"Attempting Groq Node {i+1} ({timeout_val}s limit)...", "INFO")
            return run_with_timeout(get_groq_response, timeout_val, current_client, user_text, timeout_val)
        except Exception as e:
            log_msg(f"Groq Node {i+1} Failed: {e}", "ERROR")

    # Tier 2: Try all Gemini nodes sequentially IF Groq is totally down
    for i, current_client in enumerate(active_geminis):
        try:
            log_msg(f"Attempting Gemini Node {i+1} (4.0s limit)...", "INFO")
            return run_with_timeout(get_gemini_response, 4.0, current_client, user_text)
        except Exception as e:
            log_msg(f"Gemini Node {i+1} Failed: {e}", "ERROR")

    # Tier 3: The Absolute Failsafe
    ans = get_offline_definition(user_text)
    if ans:
        log_msg("All cloud APIs down. NLTK Definition matched.", "SUCCESS")
        return ans
        
    return "Sorry, all cloud servers are unreachable right now."


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
    pos = {"x": screen_width - window_width - 25, "y": 60, "drag_x": 0, "drag_y": 0}
    root.geometry(f"{window_width}x130+{pos['x']}+{pos['y']}")

    def safe_gui(func, *args, **kwargs):
        if is_widget_open and root.winfo_exists():
            try: root.after(0, lambda: func(*args, **kwargs))
            except Exception: pass

    def safe_close():
        global is_widget_open
        is_widget_open = False
        log_msg("Closing Widget.", "INFO")
        for after_id in root.tk.eval('after info').split(): root.after_cancel(after_id)
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
        fg_color="#FF5F56", hover_color="#C93F3A", command=safe_close, font=("Arial", 11, "bold") 
    )
    close_btn.pack(side="left", padx=4)

    def on_enter_close(e): close_btn.configure(text="×", text_color="#330000") 
    def on_leave_close(e): close_btn.configure(text="")
    close_btn.bind("<Enter>", on_enter_close)
    close_btn.bind("<Leave>", on_leave_close)
    
    for color in ["#FFBD2E", "#27C93F"]:
        ctk.CTkFrame(btn_frame, width=12, height=12, corner_radius=6, fg_color=color).pack(side="left", padx=4)

    # --- OPACITY SLIDER ---
    def change_opacity(val):
        root.attributes("-alpha", float(val))
    
    opacity_slider = ctk.CTkSlider(
        toolbar, from_=0.2, to=1.0, width=60, height=10, 
        command=change_opacity, border_width=0, 
        button_color="#555555", button_hover_color="#777777", progress_color="#888888"
    )
    opacity_slider.set(1.0)
    opacity_slider.pack(side="left", padx=(15, 5))

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
    dictation_btn.pack(side="right", padx=(5, 5))

    mic_btn = ctk.CTkButton(
        toolbar, text="● Mic Off", font=("Consolas", 11, "bold"), text_color="#AAAAAA",
        fg_color="#3A3A3A", hover_color="#4A4A4A", corner_radius=5, width=95, height=24,
        command=lambda: trigger_followup()
    )
    mic_btn.pack(side="right", padx=(5, 10))

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
        if close_timer_id[0]: root.after_cancel(close_timer_id[0])
        close_timer_id[0] = root.after(seconds * 1000, safe_close)

    def run_sequence(q_str, a_str, is_followup=False, skip_query_typing=False):
        if close_timer_id[0]: root.after_cancel(close_timer_id[0])
        safe_gui(mic_btn.configure, text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
        if not skip_query_typing: safe_gui(query_label.configure, text="")
        safe_gui(response_label.configure, text="")
        
        if not is_followup: a_str = f"Hey {USER_NAME}, {a_str}"
            
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
            safe_gui(response_label.configure, text="") 
            play_and_cleanup(filepath, audio_finished_callback)
            safe_gui(type_response)

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
                if not dictation_enabled: root.after(500, activate_listening_ui)

        def audio_finished_callback():
            safe_gui(activate_listening_ui)

        if skip_query_typing: start_response_phase()
        else: type_query()

    def activate_listening_ui(is_first=False):
        mic_btn.configure(text="● Listening", fg_color="#FF5F56", text_color="#FFFFFF")
        reset_close_timer(10)
        threading.Thread(target=lambda: listen_for_followup(is_first), daemon=True).start()

    def listen_for_followup(is_first=False):
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.3)
            try:
                audio_capture = recognizer.listen(source, timeout=5, phrase_time_limit=5)
                safe_gui(mic_btn.configure, text="● Transcribing", fg_color="#FFBD2E", text_color="#000000")
                
                # --- HYBRID OFFLINE/ONLINE STT ---
                followup_q = ""
                if check_internet():
                    try:
                        followup_q = recognizer.recognize_google(audio_capture).strip()
                    except sr.RequestError:
                        log_msg("Google STT API Failed. Falling back to Vosk.", "WARNING")
                        res = json.loads(recognizer.recognize_vosk(audio_capture, model_path=VOSK_MODEL_PATH))
                        followup_q = res.get("text", "").strip()
                else:
                    log_msg("Wi-Fi Dead. Using offline Vosk transcription.", "WARNING")
                    res = json.loads(recognizer.recognize_vosk(audio_capture, model_path=VOSK_MODEL_PATH))
                    followup_q = res.get("text", "").strip()
                # ---------------------------------

                log_msg(f"Heard: '{followup_q}'", "INFO")
                
                if followup_q:
                    safe_gui(query_label.configure, text="")
                    safe_gui(mic_btn.configure, text="● Processing", fg_color="#3A3A3A", text_color="#AAAAAA")
                    try:
                        answer = process_query_master(followup_q)
                        safe_gui(run_sequence, followup_q, answer, not is_first, False)
                    except Exception as e:
                        log_msg(f"Critical System Error: {e}", "ERROR")
                        safe_gui(response_label.configure, text="System Failure.")
                        if close_timer_id[0]: safe_gui(root.after_cancel, close_timer_id[0])
                        safe_gui(root.after, 4000, safe_close)
            
            except sr.WaitTimeoutError:
                log_msg("Listening timed out.", "WARNING")
                safe_gui(safe_close)
            except Exception as e:
                log_msg(f"Speech processing error: {e}", "WARNING")
                safe_gui(safe_close)

    def trigger_followup():
        reset_close_timer(10)
        activate_listening_ui(is_first=False)

    root.attributes("-alpha", 1.0)
    
    if initial_query and initial_answer:
        root.after(10, lambda: run_sequence(initial_query, initial_answer, is_followup=False))
    else:
        root.after(10, lambda: activate_listening_ui(is_first=True))
        
    root.mainloop()

# --- 5. MAIN WAKE LOOP ---
log_msg(f"Agent online. Configured User: '{USER_NAME}' | Wake word: '{WAKE_WORD}'", "SUCCESS")

while True:
    try:
        if not is_widget_open:
            if mic_stream.is_stopped():
                mic_stream.start_stream()

            audio_data = mic_stream.read(1280, exception_on_overflow=False)
            audio_frame = np.frombuffer(audio_data, dtype=np.int16)
            prediction = model.predict(audio_frame)
            
            if model.prediction_buffer[WAKE_WORD][-1] > 0.5:
                log_msg(f"Detected wake phrase: '{WAKE_WORD}'", "TRIGGER")
                mic_stream.stop_stream()
                model.reset()
                
                display_response()
                time.sleep(1)
        else:
            time.sleep(0.5)

    except KeyboardInterrupt:
        log_msg("Shutting down assistant...", "INFO")
        break
    except Exception:
        time.sleep(1)