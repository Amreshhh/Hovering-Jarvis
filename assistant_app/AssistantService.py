import asyncio
from collections import deque
import json
import os
import random
import re
import socket
import queue
import tempfile
import threading
import time
import uuid
import zipfile
import logging
import warnings

import concurrent.futures

import edge_tts
import nltk
from nltk.corpus import wordnet
from groq import Groq
from google import genai
from google.genai import types
import pyaudio
warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API.*",
    category=UserWarning,
    module=r"pygame\.pkgdata",
)

logging.getLogger().setLevel(logging.ERROR)

import openwakeword
from openwakeword.model import Model
import pygame
import pyttsx3
import speech_recognition as sr

from .AppConfig import AppConfig
from .Logger import log_msg


class AssistantService:
    def __init__(self, config: AppConfig):
        self.config = config
        self.log = log_msg

        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
        os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "hide"
        logging.getLogger("openwakeword").setLevel(logging.ERROR)

        self.state_lock = threading.Lock()
        self.query_lock = threading.Lock()
        self.is_widget_open = False
        self.dictation_enabled = True
        self.barge_in_triggered = False
        self.widget_command_queue = queue.Queue()
        self.conversation_history = deque(maxlen=self.config.max_exchanges * 2)
        self.key_fail_counts: dict[str, int] = {}
        self.global_executor = concurrent.futures.ThreadPoolExecutor(max_workers=6)
        self.playback_epoch = 0

        self.recognizer = sr.Recognizer()
        self.offline_tts = pyttsx3.init()
        self.offline_tts.setProperty("rate", self.config.tts_rate)

        pygame.mixer.init()

        self.audio = pyaudio.PyAudio()
        self.mic_stream = self.audio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=16000,
            input=True,
            frames_per_buffer=1280,
        )

        self.groq_clients = []
        for index, key in enumerate(config.groq_keys, start=1):
            client_id = f"Groq_Node_{index}"
            self.key_fail_counts[client_id] = 0
            self.groq_clients.append({"id": client_id, "client": Groq(api_key=key)})

        self.gemini_clients = []
        for index, key in enumerate(config.gemini_keys, start=1):
            client_id = f"Gemini_Node_{index}"
            self.key_fail_counts[client_id] = 0
            self.gemini_clients.append({"id": client_id, "client": genai.Client(api_key=key)})

        self.active_wake_word = config.wake_word
        self.wake_model = self._load_wake_model()
        self._prime_wordnet()

    def _prime_wordnet(self):
        try:
            nltk.data.find("corpora/wordnet.zip")
            _ = wordnet.synsets("hello")
        except (LookupError, zipfile.BadZipFile):
            nltk.download("wordnet", quiet=True, force=True)

    def _load_wake_model(self):
        try:
            return Model(wakeword_models=[self.config.wake_word])
        except Exception:
            self.active_wake_word = "alexa"
            self.log("Wake word model not available. Falling back to 'alexa'.", "WARNING")
            return Model(wakeword_models=[self.active_wake_word])

    @staticmethod
    def check_internet(timeout=1):
        try:
            with socket.create_connection(("8.8.8.8", 53), timeout=timeout):
                return True
        except OSError:
            return False

    def run_with_timeout(self, func, timeout_sec, *args, **kwargs):
        future = self.global_executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=timeout_sec)
        except Exception:
            future.cancel()
            raise

    @staticmethod
    def _sanitize_word(text):
        return re.sub(r"[^\w\s]", "", text.strip())

    def get_offline_definition(self, user_text):
        match = re.search(r"^(define|meaning of|what is the meaning of)\s+(.+)", user_text.lower().strip())
        if not match:
            return None
        word = self._sanitize_word(match.group(2))
        synsets = wordnet.synsets(word)
        if synsets:
            return f"{word.capitalize()} means: {synsets[0].definition()}."
        return None

    def _history_messages_for_groq(self):
        return list(self.conversation_history)

    def _history_messages_for_gemini(self):
        messages = []
        for msg in self.conversation_history:
            role = "user" if msg["role"] == "user" else "model"
            messages.append(types.Content(role=role, parts=[types.Part.from_text(text=msg["content"]) ]))
        return messages

    def _remember_exchange(self, user_text, answer):
        if not answer:
            return
        self.conversation_history.append({"role": "user", "content": user_text})
        self.conversation_history.append({"role": "assistant", "content": answer})

    def get_groq_response(self, client, user_text, timeout_val):
        messages = [
            {"role": "system", "content": "You are a fast, minimalist assistant. Explain using layman's terms and avoid bombastic definitions. When defining words, include one example sentence. Answer in 1 or 2 sentences."}
        ] + self._history_messages_for_groq() + [{"role": "user", "content": user_text}]
        response = client.chat.completions.create(messages=messages, model=self.config.groq_model, timeout=timeout_val)
        answer = (response.choices[0].message.content or "").strip()
        if not answer:
            raise ValueError("Empty response from Groq.")
        self._remember_exchange(user_text, answer)
        return answer

    def get_gemini_response(self, client, user_text, timeout_val):
        history = self._history_messages_for_gemini()
        history.append(types.Content(role="user", parts=[types.Part.from_text(text=user_text)]))
        config = types.GenerateContentConfig(
            system_instruction="You are a fast, minimalist assistant. Explain using layman's terms and avoid bombastic definitions. When defining words, include one example sentence. Answer in 1 or 2 sentences.",
            http_options=types.HttpOptions(timeout=int(timeout_val * 1000)),
        )
        response = client.models.generate_content(model=self.config.gemini_model, contents=history, config=config)
        answer = (response.text or "").strip()
        if not answer:
            raise ValueError("Empty response from Gemini.")
        self._remember_exchange(user_text, answer)
        return answer

    def process_query_master(self, user_text):
        with self.query_lock:
            if not self.check_internet():
                answer = self.get_offline_definition(user_text)
                return answer if answer else "System is offline. Local database returned empty content."

            active_groqs = [node for node in self.groq_clients if self.key_fail_counts[node["id"]] < 2]
            active_geminis = [node for node in self.gemini_clients if self.key_fail_counts[node["id"]] < 2]

            if not active_groqs:
                for node in self.groq_clients:
                    self.key_fail_counts[node["id"]] = 0
                active_groqs = list(self.groq_clients)

            if not active_geminis:
                for node in self.gemini_clients:
                    self.key_fail_counts[node["id"]] = 0
                active_geminis = list(self.gemini_clients)

            random.shuffle(active_groqs)
            random.shuffle(active_geminis)

            for index, node in enumerate(active_groqs):
                if self.barge_in_triggered:
                    return None
                timeout_val = 5.0 if index == 0 else 6.0
                try:
                    self.log(f"Attempting {node['id']} ({timeout_val}s limit)...", "INFO")
                    answer = self.run_with_timeout(
                        self.get_groq_response,
                        timeout_val,
                        node["client"],
                        user_text,
                        timeout_val,
                    )
                    self.key_fail_counts[node["id"]] = 0
                    return answer
                except Exception as exc:
                    self.key_fail_counts[node["id"]] += 1
                    self.log(f"{node['id']} Failed: {exc}", "ERROR")

            for node in active_geminis:
                if self.barge_in_triggered:
                    return None
                timeout_val = 4.0
                try:
                    self.log(f"Attempting {node['id']} ({timeout_val}s limit)...", "INFO")
                    answer = self.run_with_timeout(
                        self.get_gemini_response,
                        timeout_val,
                        node["client"],
                        user_text,
                        timeout_val,
                    )
                    self.key_fail_counts[node["id"]] = 0
                    return answer
                except Exception as exc:
                    self.key_fail_counts[node["id"]] += 1
                    self.log(f"{node['id']} Failed: {exc}", "ERROR")

            answer = self.get_offline_definition(user_text)
            return answer if answer else "All network nodes and search fallback clusters are unreachable."

    def transcribe_audio(self, audio_capture):
        if self.check_internet():
            try:
                return self.recognizer.recognize_google(audio_capture).strip()
            except sr.RequestError:
                pass
            except sr.UnknownValueError:
                return ""

        try:
            result = json.loads(self.recognizer.recognize_vosk(audio_capture))
            return result.get("text", "").strip()
        except Exception:
            return ""

    def capture_microphone_text(self, timeout=5, phrase_time_limit=5, ambient_noise=0.3):
        with sr.Microphone() as source:
            self.recognizer.adjust_for_ambient_noise(source, duration=ambient_noise)
            audio_capture = self.recognizer.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
            return self.transcribe_audio(audio_capture)

    def generate_audio(self, text, on_ready_callback):
        def write_online():
            try:
                output_file = os.path.join(tempfile.gettempdir(), f"assistant_response_{uuid.uuid4().hex}.mp3")
                communicate = edge_tts.Communicate(text, self.config.tts_voice)
                asyncio.run(communicate.save(output_file))
                on_ready_callback(output_file)
            except Exception as exc:
                self.log(f"Online TTS failed: {exc}", "WARNING")
                write_offline()

        def write_offline():
            try:
                output_file = os.path.join(tempfile.gettempdir(), f"assistant_response_{uuid.uuid4().hex}.wav")
                self.offline_tts.save_to_file(text, output_file)
                self.offline_tts.runAndWait()
                on_ready_callback(output_file)
            except Exception as exc:
                self.log(f"Offline TTS failed: {exc}", "ERROR")
                on_ready_callback(None)

        target = write_online if self.check_internet() else write_offline
        threading.Thread(target=target, daemon=True).start()

    def play_and_cleanup(self, filepath, on_complete_callback):
        with self.state_lock:
            self.playback_epoch += 1
            my_epoch = self.playback_epoch

        def is_stale():
            return my_epoch != self.playback_epoch

        def task():
            try:
                # Defensively stop any prior track first so a lagging old
                # thread can never keep monitoring/steering this new one.
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
                if is_stale():
                    return
                pygame.mixer.music.load(filepath)
                pygame.mixer.music.set_volume(1.0 if self.dictation_enabled else 0.0)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    if self.barge_in_triggered or is_stale():
                        pygame.mixer.music.stop()
                        break
                    time.sleep(0.05)
                if not is_stale():
                    pygame.mixer.music.unload()
            except Exception as exc:
                self.log(f"Audio playback failed: {exc}", "WARNING")
            finally:
                try:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                except OSError:
                    pass
                if on_complete_callback and not self.barge_in_triggered and not is_stale():
                    on_complete_callback()

        threading.Thread(target=task, daemon=True).start()

    def shutdown(self):
        try:
            if self.mic_stream.is_active():
                self.mic_stream.stop_stream()
        except Exception:
            pass
        try:
            self.mic_stream.close()
        except Exception:
            pass
        try:
            self.audio.terminate()
        except Exception:
            pass
        try:
            pygame.mixer.quit()
        except Exception:
            pass
        try:
            self.global_executor.shutdown(cancel_futures=True)
        except Exception:
            pass
