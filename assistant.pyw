import time
import random
import os
import pyaudio
import numpy as np
import openwakeword
from openwakeword.model import Model
import customtkinter as ctk
import speech_recognition as sr
from google import genai
from google.genai import types
from dotenv import load_dotenv

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

audio = pyaudio.PyAudio()
mic_stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, 
                        input=True, frames_per_buffer=1280)

# --- 2. NATIVE TERMINAL HUD (NO BROWSER ENGINE) ---
# --- 2. NATIVE TERMINAL HUD (ADJUSTED WIDTH) ---
def display_response(query, answer):
    root = ctk.CTk()
    
    # Force transparency using the Windows chroma-key trick
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    TRANSPARENT_COLOR = "#000001"
    root.wm_attributes("-transparentcolor", TRANSPARENT_COLOR)
    root.configure(fg_color=TRANSPARENT_COLOR)
    
    # INCREASED WIDTH: Bumped from 360 to 440 for a roomier console layout
    window_width = 440
    screen_width = root.winfo_screenwidth()
    x_coordinate = screen_width - window_width - 25
    y_coordinate = 60 

    # Base height setup
    root.geometry(f"{window_width}x110+{x_coordinate}+{y_coordinate}")

    # Main Terminal Container
    panel = ctk.CTkFrame(
        root, 
        corner_radius=10, 
        fg_color="#1E1E1E",      
        bg_color=TRANSPARENT_COLOR,
        border_width=1,
        border_color="#333333"
    )
    panel.pack(fill="both", expand=True, padx=10, pady=10)

    # Top Toolbar
    toolbar = ctk.CTkFrame(panel, corner_radius=10, fg_color="#2D2D2D", height=35)
    toolbar.pack(fill="x", padx=0, pady=0)
    toolbar.pack_propagate(False) 

    # Mac Window Buttons
    btn_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
    btn_frame.pack(side="left", padx=12)
    for color in ["#FF5F56", "#FFBD2E", "#27C93F"]:
        ctk.CTkFrame(btn_frame, width=12, height=12, corner_radius=6, fg_color=color).pack(side="left", padx=4)

    # Toolbar Title & Plus Icon
    ctk.CTkLabel(toolbar, text="Tony: ~", font=("Consolas", 12, "bold"), text_color="#FFFFFF").pack(side="left", padx=25)
    ctk.CTkLabel(toolbar, text="+", font=("Consolas", 16, "bold"), text_color="#FFFFFF", fg_color="#3A3A3A", corner_radius=5, width=24, height=24).pack(side="right", padx=10)

    # Terminal Body Area
    body = ctk.CTkFrame(panel, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=15, pady=10)

    # Custom Prompt Line
    prompt_frame = ctk.CTkFrame(body, fg_color="transparent")
    prompt_frame.pack(fill="x", anchor="w")
    
    ctk.CTkLabel(prompt_frame, text="Tony:", font=("Consolas", 13, "bold"), text_color="#00FF9C").pack(side="left")
    ctk.CTkLabel(prompt_frame, text="~", font=("Consolas", 13, "bold"), text_color="#0066FF").pack(side="left", padx=(6,0))
    ctk.CTkLabel(prompt_frame, text="$", font=("Consolas", 13, "bold"), text_color="#FF00FF").pack(side="left", padx=(6,10))

    # Text Elements
    query_label = ctk.CTkLabel(prompt_frame, text="", font=("Consolas", 13), text_color="#FFFFFF")
    query_label.pack(side="left")

    # OPTIMIZED: Increased wraplength to 380 so text fills the expanded card nicely
    response_label = ctk.CTkLabel(body, text="", font=("Consolas", 13), text_color="#CCCCCC", justify="left", wraplength=380)
    response_label.pack(anchor="w", pady=(10, 0))

    # --- THE TYPEWRITER & DYNAMIC RESIZER ---
    def update_height(text_len):
        # Adjusted line character estimation for the wider panel geometry (~50 chars per line)
        lines = (text_len // 50) + 1
        new_height = 110 + (lines * 22) 
        root.geometry(f"{window_width}x{new_height}+{x_coordinate}+{y_coordinate}")

    q_idx = [0]
    r_idx = [0]

    def type_query():
        if q_idx[0] < len(query):
            query_label.configure(text=query[:q_idx[0]+1] + "█") 
            q_idx[0] += 1
            root.after(30, type_query)
        else:
            query_label.configure(text=query) 
            root.after(150, type_response)

    def type_response():
        if r_idx[0] < len(answer):
            current_text = answer[:r_idx[0]+1]
            update_height(len(current_text)) 
            response_label.configure(text=current_text + "█")
            
            r_idx[0] += 1
            delay = 150 if answer[r_idx[0]-1] in ['.','!','?'] else 25
            root.after(delay, type_response)
        else:
            response_label.configure(text=answer)
            root.after(7000, safe_close)

    def safe_close():
        for after_id in root.tk.eval('after info').split():
            root.after_cancel(after_id)
        root.quit()
        root.destroy()

    # Fade in window
    root.attributes("-alpha", 0.0)
    def fade_in(alpha=0.0):
        if alpha < 1.0: 
            alpha += 0.1
            root.attributes("-alpha", alpha)
            root.after(20, lambda: fade_in(alpha))
        else:
            root.after(100, type_query)

    fade_in()
    root.mainloop()

# --- 3. THE AI ENGINE LOOP ---
print("Agent is active. Native Terminal UI loaded and protected from Windows bugs.")

while True:
    try:
        if mic_stream.is_stopped():
            mic_stream.start_stream()

        audio_data = mic_stream.read(1280, exception_on_overflow=False)
        audio_frame = np.frombuffer(audio_data, dtype=np.int16)
        prediction = model.predict(audio_frame)
        
        if model.prediction_buffer[WAKE_WORD][-1] > 0.5:
            print("\n[Trigger] Listening...")
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
                        
                except sr.UnknownValueError:
                    print("Audio unclear.")
                except sr.RequestError as req_err:
                    print(f"Network Error: {req_err}")
                except Exception as e:
                    print(f"API Error: {e}")
            
            model.reset()
            time.sleep(1)

    except KeyboardInterrupt:
        print("\nForce quitting via Ctrl+C.")
        break
    except IOError:
        time.sleep(0.1)
    except Exception as e:
        time.sleep(1)