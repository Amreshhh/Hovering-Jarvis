# Voice-Triggered Assistant Architecture
This project is a modular, voice-triggered assistant designed for rapid response and high availability. It listens for a wake word, captures spoken queries, routes them through a layered response pipeline, and presents the results via a compact floating Alexa.

## Module Layout and Responsibilities
The application is cleanly divided into specific modules to separate the UI, runtime, configuration, and core logic.

- MainLauncher.pyw: The runtime entry point that kicks off the application.
- assistant_app/AppRuntime.py: Manages the top-level execution loop and orchestrates the interaction between the service and the UI.
- assistant_app/AppConfig.py: Handles environment loading, variable parsing, and API key validation.
- assistant_app/AssistantService.py: The core engine. Owns the wake-word detection, API routing, speech-to-text (STT) transcription, text-to-speech (TTS) generation, audio playback, and resource cleanup.
- assistant_app/CardUi.py: The frontend. Owns the floating Alexa, manages follow-up interactions, and handles user barge-in (interrupt) behaviors.

## Runtime Execution Flow
The lifecycle of a single user interaction follows a strict pipeline with guarded fallbacks.

### Phase 1: Initialization
- Boot: MainLauncher.pyw launches the runtime.
- Configuration: AppConfig.load_config() reads the .env file and validates that the mandatory API keys are present.
- Service Setup: AssistantService initializes the wake-word model, configures the audio stack, prepares the recognizer, TTS engines, and API clients, and primes the offline WordNet corpus.

### Phase 2: Active Listening
- Wake Loop: The system continuously reads microphone frames, checking for the active wake word.
- Trigger Event: Once the wake word is detected, the assistant pauses wake-word capture, opens the Alexa immediately, and starts a thread for initial speech capture.

### Phase 3: Processing and Routing
- Transcription: The spoken query is captured, transcribed to text, and sent to process_query_master().
- Routing Guard: The query pipeline is serialized with a query lock so only one response chain can run at a time.
- Online Waterfall: The query is routed through a prioritized pipeline to ensure a response even if primary nodes fail. The priority is:
	- Tier 1: Groq Clients (fastest)
	- Tier 2: Gemini Clients (secondary LLM)
	- Tier 3: Offline WordNet fallback for definition-style queries only
- Offline Definition Routing: Local fallback is only triggered for prompts that match definition patterns such as `define`, `meaning of`, or `what is the meaning of`.

### Phase 4: Output and Reset
- Display and Speak: The Alexa types the query and answer while TTS is prepared in the background. Audio playback starts only after the response is ready, and stale playback threads are invalidated with a playback epoch.
- Follow-up Control: The Alexa stays open until both text and audio complete, then it switches back to listening mode.
- Return to Loop: Once complete, the system returns to listening mode, ready for follow-up prompts.

## Environment Configuration
The application relies on specific environment variables mapped in the .env file.

- GROQ_API_KEY_1
	- Status: Mandatory
	- Description: Primary routing node. The app will fail to launch if this is missing.
- GEMINI_API_KEY_1
	- Status: Mandatory
	- Description: Secondary routing node. The app will fail to launch if this is missing.
- GROQ_API_KEY_2 through GROQ_API_KEY_9
	- Status: Optional
	- Description: Additional load-balancing keys for the Groq cluster.
- GEMINI_API_KEY_2 through GEMINI_API_KEY_9
	- Status: Optional
	- Description: Additional load-balancing keys for the Gemini cluster.
- USER_NAME
	- Status: Optional
	- Description: The name used by the Alexa and TTS engine. Defaults to User.
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
- Request-Level Timeouts: Both the Groq and Gemini calls carry a hard timeout on the underlying HTTP request itself (not just on the future awaiting it), so a stalled network call can't permanently pin down one of the shared worker-pool threads and starve later queries.
- Playback/Sequence Staleness Guards: Each Alexa response cycle carries a generation token, and each TTS playback carries its own epoch counter. A barge-in (or any interrupted sequence) immediately invalidates these, so a stale audio thread or callback from a previous exchange can never keep speaking over, or trigger a duplicate follow-up listener for, the exchange that superseded it.
- Single Listener Guarantee: Follow-up capture threads are spawned through one gated entry point, ensuring at most one microphone-listening thread is ever active at a time even under rapid barge-in/button-mash conditions.
