# Voice-Triggered Assistant Architecture
This project is a modular, voice-triggered assistant designed for rapid response and high availability. It listens for a specific wake word, captures spoken queries, routes them through a multi-layered API pipeline, and presents the results via a compact, floating HUD.

## Module Layout and Responsibilities
The application is cleanly divided into specific modules to separate the UI, runtime, configuration, and core logic.

- MainLauncher.pyw: The runtime entry point that kicks off the application.
- assistant_app/AppRuntime.py: Manages the top-level execution loop and orchestrates the interaction between the service and the UI.
- assistant_app/AppConfig.py: Handles environment loading, variable parsing, and API key validation.
- assistant_app/AssistantService.py: The core engine. Owns the wake-word detection, API routing, speech-to-text (STT) transcription, text-to-speech (TTS) generation, audio playback, and resource cleanup.
- assistant_app/CardUi.py: The frontend. Owns the floating HUD, manages follow-up interactions, and handles user barge-in (interrupt) behaviors.

## Runtime Execution Flow
The lifecycle of a single user interaction follows a strict, sequential pipeline.

### Phase 1: Initialization
- Boot: MainLauncher.pyw launches the runtime.
- Configuration: AppConfig.load_config() reads the .env file and validates that the mandatory API keys are present.
- Service Setup: AssistantService initializes the wake-word model, configures the audio stack, and loads the recognizer, TTS engines, and API clients.

### Phase 2: Active Listening
- Wake Loop: The system continuously reads microphone frames, checking for the active wake word.
- Trigger Event: Once the wake word is detected, the assistant pauses wake-word capture, opens the HUD immediately, and starts a thread for initial speech capture.

### Phase 3: Processing and Routing
- Transcription: The spoken query is captured, transcribed to text, and sent to process_query_master().
- Waterfall Routing: The query is routed through a prioritized pipeline to ensure a response even if primary nodes fail. The priority is:
	- Tier 1: Groq Clients (Fastest)
	- Tier 2: Gemini Clients (Secondary LLM)
	- Tier 3: Offline WordNet (Local Database Fallback)

### Phase 4: Output and Reset
- Display and Speak: The HUD visually types out the query and the generated answer. Simultaneously, it plays the generated TTS audio.
- Return to Loop: Once complete, the system returns to listening mode, ready for follow-up prompts.

## Environment Configuration
The application relies on specific environment variables mapped in the .env file.

- GROQ_API_KEY_1
	- Status: Mandatory
	- Description: Primary routing node. The app will fail to launch if this is missing.
- GEMINI_API_KEY_1
	- Status: Mandatory
	- Description: Secondary routing node. The app will fail to launch if this is missing.
- GROQ_API_KEY_2 & GROQ_API_KEY_3
	- Status: Optional
	- Description: Additional load-balancing keys for the Groq cluster.
- GEMINI_API_KEY_2 & GEMINI_API_KEY_3
	- Status: Optional
	- Description: Additional load-balancing keys for the Gemini cluster.
- USER_NAME
	- Status: Optional
	- Description: The name used by the HUD and TTS engine. Defaults to User.
- WAKE_WORD
	- Status: Optional
	- Description: The trigger word to activate the assistant. Defaults to alexa.

## Resilience and Edge Case Handling
The system is built to handle common failures gracefully without crashing.

- Thread Safety: Cross-thread UI commands are securely managed using a thread-safe queue, replacing the previous plain list implementation.
- Memory Management: Conversation history is capped using a bounded deque, preventing unbounded memory growth during long sessions.
- File I/O Conflicts: TTS temporary audio files are generated with unique UUIDs, ensuring that overlapping responses do not overwrite or clobber one another.
- Offline Fallbacks: If the primary online TTS engine fails, the system automatically falls back to the offline TTS engine rather than silently dropping the response.
- Model Availability: If the user's custom wake-word model is missing or unavailable, the system logs a warning and safely defaults to alexa.
