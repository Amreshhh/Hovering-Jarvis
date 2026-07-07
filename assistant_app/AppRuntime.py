import threading
import time

import numpy as np

from .AppConfig import ConfigError, load_config
from .Logger import log_msg
from .AssistantService import AssistantService
from .CardUi import AssistantAlexa


def main():
    try:
        config = load_config()
    except ConfigError as exc:
        log_msg(str(exc), "ERROR")
        return

    service = AssistantService(config)
    Alexa = AssistantAlexa(service)

    log_msg(
        f"Agent initialized successfully. Tracking profile: '{config.user_name}' | Wake: '{service.active_wake_word}'",
        "SUCCESS",
    )

    try:
        while True:
            try:
                if not service.is_widget_open:
                    if service.meeting_open_request.is_set():
                        service.meeting_open_request.clear()
                        Alexa.display_response()
                        continue

                    if service.mic_stream.is_stopped():
                        service.mic_stream.start_stream()

                    audio_data = service.mic_stream.read(1280, exception_on_overflow=False)
                    audio_frame = np.frombuffer(audio_data, dtype=np.int16)
                    service.wake_model.predict(audio_frame)

                    if service.wake_model.prediction_buffer[service.active_wake_word][-1] > service.config.wake_threshold:
                        log_msg(f"System Trigger match event: '{service.active_wake_word}'", "TRIGGER")
                        service.mic_stream.stop_stream()
                        service.wake_model.reset()

                        threading.Thread(target=Alexa.capture_initial_query, daemon=True).start()
                        Alexa.display_response()
                        time.sleep(1)
                else:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                break
            except Exception:
                time.sleep(1)
    finally:
        service.shutdown()
