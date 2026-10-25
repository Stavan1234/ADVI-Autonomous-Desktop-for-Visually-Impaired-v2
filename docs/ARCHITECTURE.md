# ADVI architecture

This page describes the architecture as it is implemented in `src/advi/` (version 0.4.0). Every box in the
diagrams is a real module or class; dashed lines mark optional or conditional paths. The full narrative,
with design rationale and test evidence, is in [ADVI_TECHNICAL_REPORT.md](ADVI_TECHNICAL_REPORT.md).

**The rule the code follows:** the language model decides *what* to do. ADVI's own code owns the computer,
the task state and the evidence.
- The model only ever proposes a tool call.
- `ToolGuard` validates the call and enforces confirmation for sends, deletes of mail and similar actions.
- A capability performs the action.
- `ToolVerifier` and the capability's own read-back decide whether it really happened.
- `AgentSession` refuses to let the model's prose claim more than that evidence shows.

Each of these is a code path with tests, not just a prompt instruction. Section 7 of the report lists where
the rule is only partly enforced.

## 1. System context

```mermaid
flowchart LR
    user(["User<br/>keyboard, microphone, speakers"])
    subgraph pc["User's Windows PC"]
        advi["ADVI process<br/>python -m advi.app"]
        apps["Windows apps via UI Automation<br/>WhatsApp, Telegram, Notepad, Calculator, ..."]
        word["Microsoft Word<br/>COM automation"]
        chrome["Google Chrome<br/>ADVI's own profile, CDP port 9222"]
        fs[("Files and folders")]
        tess["Tesseract OCR"]
        tts["Speech output<br/>Piper (optional) or Windows SAPI"]
        db[("runtime/memory/advi_memory.db<br/>SQLite")]
    end
    groq["Groq API<br/>gpt-oss-120b (brain)<br/>whisper-large-v3-turbo (speech)"]
    gemini["Google Gemini API<br/>backup brain, screen description"]
    gmail["Gmail API (optional OAuth)"]
    hf["Hugging Face<br/>one-time model downloads"]
    mcp["MCP servers (optional)"]

    user -- "typed or spoken request" --> advi
    advi -- "spoken and printed answer" --> user
    advi --> apps
    advi --> word
    advi --> chrome
    advi --> fs
    advi --> tess
    advi --> tts
    advi --> db
    advi <-- "HTTPS" --> groq
    advi <-. "HTTPS, fallback / vision" .-> gemini
    advi <-. "HTTPS" .-> gmail
    advi -. "first start only" .-> hf
    advi <-. "stdio / HTTP" .-> mcp
```

## 2. Components

```mermaid
flowchart TB
    subgraph io["Input and output (io/, voice/)"]
        hub["InputHub<br/>io/console.py"]
        voice["VoiceConversation<br/>voice/conversation.py"]
        duplex["DuplexAudio + WebRTC AEC<br/>voice/audio_io.py"]
        rec["Recorder + Silero VAD<br/>voice/recorder.py"]
        stt["DualTranscriber<br/>voice/transcriber.py"]
        out["OutputManager<br/>io/output.py"]
        ttsmod["PiperTTS / SAPI worker<br/>io/tts.py"]
    end
    subgraph agent["Agent (agent/)"]
        session["AgentSession<br/>agent/session.py"]
        local["local_intents.py<br/>built-in commands"]
        names["names.py<br/>spelled names, corrections"]
        psm["ProviderSessionManager<br/>agent/provider_session.py"]
    end
    subgraph providers["Providers (providers/)"]
        groqp["GroqProvider x3 keys"]
        gemp["GeminiProvider x3 keys"]
    end
    subgraph tools["Tool boundary (tools/, core/)"]
        guard["ToolGuard<br/>validation + confirmation"]
        runner["ToolRunner"]
        engine["ExecutionEngine"]
        verifier["ToolVerifier<br/>core/verification.py"]
    end
    subgraph caps["Capabilities (capabilities/, registry.py)"]
        desk["desktop_control<br/>DesktopCapability"]
        ui["desktop_ui / messaging<br/>WhatsApp, Telegram skills"]
        brow["browser_control<br/>BrowserCapability"]
        files["file_management<br/>FileCapability"]
        office["office<br/>OfficeCapability (Word)"]
        email["email<br/>GmailCapability"]
        memcap["memory / system / mcp"]
        david["david_fallback<br/>DavidFallbackCapability"]
    end
    subgraph fb["DAVID fallback (fallback/)"]
        fsvc["FallbackService<br/>intent + planner prompts,<br/>own executor and CDP browser"]
    end
    subgraph mem["Memory (memory/)"]
        msvc["MemoryService"]
        ltm["LongTermMemory (SQLite)"]
        retr["MemoryRetriever<br/>bge-small embeddings"]
    end

    voice --> rec
    rec -- "echo-cancelled mic frames" --> duplex
    voice --> stt
    voice -- "transcript" --> hub
    hub -- "one request at a time" --> session
    session --> local
    session --> names
    session --> psm
    psm --> groqp
    psm -.-> gemp
    session --> runner
    runner --> guard
    runner --> engine
    engine --> caps
    engine --> verifier
    david -.-> fsvc
    session --> msvc
    msvc --> ltm
    msvc --> retr
    session -- "AdviResponse" --> out
    out --> ttsmod
    ttsmod -- "speech samples" --> duplex
```

The wake listener (`src/advi/wake/`) is a separate process, not part of this diagram; see section 6.

## 3. One request, end to end

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant H as InputHub
    participant S as AgentSession
    participant P as ProviderSessionManager
    participant R as ToolRunner + ToolGuard
    participant E as ExecutionEngine
    participant C as Capability
    participant V as ToolVerifier
    participant O as OutputManager

    U->>H: "send hello to Siyu on WhatsApp"
    H->>S: respond(text)
    Note over S: local checks first: pending confirmation,<br/>sleep/stop/time, diagnosis questions
    S->>P: send_user(text + context, selected tools)
    P-->>S: tool call whatsapp_send_message(chat, text)
    Note over S: requires confirmation, so not executed
    S-->>O: "Shall I send 'hello' to Siyu? Yes or no?"
    U->>H: "yes"
    H->>S: respond("yes")
    S->>R: run(call, approved=True)
    R->>R: guard.approve() then check(): schema + approval
    R->>E: execute_tool_call(call)
    E->>C: execute(call)
    C-->>E: ToolResult (success, evidence)
    E->>V: verify(call, result)
    V-->>E: verified / uncertain / failed
    E-->>S: ToolResult
    S->>P: send_tool_results(result)
    P-->>S: final text
    Note over S: _authoritative_task_response() and<br/>_no_unbacked_send_claim() check the text<br/>against the recorded evidence
    S-->>O: AdviResponse
    O-->>U: printed and spoken
```

If every provider fails, `AgentSession._local_fallback()` tries a built-in command from
`local_intents.py` (open an app, a site search, play/pause, simple file moves, a message that still needs
a "yes"), and otherwise says plainly that the request needs the AI.

## 4. Desktop interaction

```mermaid
flowchart TB
    call["tool call, e.g. open_application,<br/>type_text, close_window"] --> dc["DesktopCapability<br/>capabilities/desktop/executor.py"]
    dc --> launch["application_launcher.py<br/>built-in names, Office paths,<br/>shortcuts, shell:AppsFolder"]
    dc --> perc["perception.py<br/>UI Automation tree (pywinauto/comtypes)"]
    dc --> keys["pyautogui / pywinauto<br/>keys, clicks, typing"]
    perc --> obs{"elements found?"}
    obs -- "yes" --> act["act on element refs"]
    obs -- "fewer than 4, or stalled" --> ocr["vision.py read_screen_text<br/>Tesseract OCR of the window"]
    ocr -. "still unclear (max 2 looks per task)" .-> look["screen_describer.py<br/>Gemini describes a screenshot in words"]
    act --> verify["result + verification details<br/>(window title, process, focus)"]
    look --> verify

    skills["WhatsApp / Telegram skills<br/>capabilities/apps/"] --> uia["WindowsUiaDriver<br/>apps/uia.py"]
    uia --> ff["force_foreground():<br/>plain, Alt-key unlock, input attach"]
    uia --> composer["search box, chat row, composer,<br/>Send button via UIA Invoke"]
    composer --> sent["verified when the composer clears<br/>and the chat title matches"]
```

Vision returns a description in words only; it never supplies click coordinates and is never used as proof
that an action succeeded (`docs/audits/PHASE9_VISION.md`).

## 5. Browser interaction

```mermaid
flowchart TB
    bcall["navigate / search / click_web_element /<br/>type_web_element / read_web_page / play_media"] --> bc["BrowserCapability<br/>capabilities/browser/executor.py"]
    bc --> pw["PlaywrightBrowser<br/>browser/pw_session.py"]
    pw -- "connect_over_cdp(127.0.0.1:9222)<br/>or launch channel=chrome" --> chrome["Chrome with ADVI profile"]
    bc --> snap["snapshot(): numbered element refs<br/>(retry after DOM settles if empty)"]
    bc --> gate{"consequential link?<br/>buy, pay, submit, delete, send"}
    gate -- "yes" --> confirm["requires_confirmation result"]
    gate -- "no" --> click["click / type"]
    bc --> text["main-text reader, 3000-character pages"]
    bc --> media["media_control: pause/resume/next<br/>checked against the page's media elements"]
    session["AgentSession"] -. "a search naming a known site<br/>goes to that site's search URL" .-> bc
```

## 6. Voice interaction

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant M as DuplexAudio (16 kHz, WebRTC AEC)
    participant R as Recorder (Silero VAD)
    participant V as VoiceConversation
    participant T as DualTranscriber
    participant H as InputHub
    participant O as OutputManager

    loop while ADVI runs
        O->>M: ADVI's speech and sounds (the echo reference)
        M->>R: 32 ms mic frames with ADVI's voice removed (+0.3 s pre-roll)
        R->>V: on_speech_start after 0.09 s of speech: if ADVI is talking, pause it
        R-->>V: Recording after 1.2 s silence (2 s after long speech)
        V->>T: transcribe(audio)
        alt online
            T->>T: Groq whisper-large-v3-turbo
        else offline or Groq error
            T->>T: local faster-whisper (distil-small.en)
        end
        T-->>V: text (hallucination filter applied)
        alt started while ADVI was speaking
            V->>V: own words or noise? resume ADVI. Stop words? stop. Else end its answer and continue.
        end
        alt sleep / exit
            V->>H: request_exit: ADVI closes completely
        else sound guide / "wake up ADVI"
            V->>V: handled locally
        else command
            V->>H: put_voice(text)
            H-->>O: answer is spoken
            O-->>U: soft "your turn" chime when ADVI finishes
        end
    end
```

### The wake word (a separate process)

```mermaid
flowchart TB
    signin["Windows sign-in<br/>HKCU Run: pythonw -m advi.wake"] --> sup["Supervisor<br/>one copy (named mutex);<br/>restarts the worker after a crash"]
    advistart["ADVI start-up<br/>autostart.ensure_running()"] -.-> sup
    sup --> worker["Worker (wake/listener.py)"]
    worker --> running{"ADVI running?<br/>(its named mutex)"}
    running -- "yes" --> idle["let go of mic and hotkey;<br/>check again in 1 s"] --> running
    running -- "no" --> vad["mic + Silero VAD:<br/>utterances with 0.3-4 s of speech"]
    vad --> whisper["Whisper tiny.en on this PC,<br/>biased to 'ADVI'"]
    whisper --> match{"phrase.is_wake_phrase?"}
    match -- "no" --> vad
    match -- "yes" --> launch["wake sound + run_advi.bat<br/>in its own window"]
    hotkey["ADVI hotkey while closed"] --> launch
    porcupine["optional: Porcupine keyword engine<br/>(PICOVOICE_ACCESS_KEY + .ppn)"] -.-> launch
    launch --> running
```

## 7. Fallback and recovery

```mermaid
flowchart TB
    subgraph prov["Provider failover (agent/provider_session.py)"]
        p1["groq-1"] -- "429 / timeout / 5xx" --> p2["groq-2"] --> p3["groq-3"] --> g1["gemini-1..3"]
        g1 -- "all failed" --> lf["_local_fallback(): built-in command<br/>or a plain 'this needs the AI' answer"]
        short["short per-minute limit (under 25 s):<br/>wait once, retry"] -.-> p1
    end
    subgraph tool["Tool failure recovery (agent/session.py)"]
        fail["native tool failed"] --> obsv["_capture_native_recovery_observation()<br/>UIA / OCR / bounded vision look"]
        obsv --> model["model chooses the next step"]
        fail -- "tool in the recoverable set,<br/>or Gmail API recommends fallback,<br/>or user said 'use David'" --> avail["task.context fallback_available"]
        avail -.-> dtool["david_fallback tool offered to the model"]
        dtool -.-> davidsvc["FallbackService (fallback/)<br/>own planner, executor, perception, CDP browser"]
        davidsvc -. "ToolResult; verified by DAVID's own loop" .-> model
    end
```

DAVID is a separate, isolated subsystem. It is never on the primary path: the primary model can only reach
it through the `david_fallback` tool, which is offered only in the three situations above.

## 8. Setup and testing

```mermaid
flowchart LR
    clone["clone or download"] --> setup["SETUP_ADVI.bat<br/>runs scripts/setup_windows.ps1"]
    setup --> venv[".venv with Python 3.13<br/>pip install -e .[dev]"]
    venv --> envf[".env created from .env.example"]
    envf --> model["offline speech + wake-word models<br/>(non-fatal)"]
    model --> checks["Piper / Tesseract / Chrome checks"]
    checks --> tests["compileall + pytest<br/>(live Groq test skipped)"]
    tests --> wake["wake listener registered<br/>at Windows sign-in"]
    wake --> key["user adds GROQ_API_KEY<br/>and/or GEMINI_API_KEY"]
    key --> run["run_advi.bat"]
    run --> ready["'Everything is ready'<br/>startup health check passed"]
    ready --> optional["optional: smoke_windows.ps1,<br/>benchmarks (benchmarks/README.md)"]
```

## Where things live

| Folder | Contents |
|---|---|
| `src/advi/app.py` | Process entry point: builds every component, runs the input loop, shuts down |
| `src/advi/agent/` | `AgentSession` (the task loop), provider session management, built-in commands, name handling |
| `src/advi/providers/` | Groq and Gemini clients behind one `LLMProvider` interface |
| `src/advi/tools/` | `ToolRegistry`, `ToolGuard`, `ToolRunner`, `ToolCall` / `ToolResult` contracts |
| `src/advi/core/` | Execution engine, verification, action contracts, strategy, health checks, logging, config |
| `src/advi/capabilities/` | Everything that touches the computer: desktop, browser, files, office, apps, gmail, memory, system, mcp |
| `src/advi/voice/` | Always-on listening, echo-cancelled duplex audio, speech recognition, hotkey, earcons, output level meter |
| `src/advi/wake/` | The wake-word listener process: phrase matching, engines, supervisor, start at sign-in |
| `src/advi/io/` | Console input hub, output manager, speech output |
| `src/advi/memory/` | SQLite long-term memory, embeddings retriever, experience lessons, session consolidation |
| `src/advi/fallback/` | DAVID, the isolated fallback planner and executor |
| `src/advi/environment/` | Known Windows folders, file-system watcher |
| `benchmarks/` | Desktop Suite, capability sweep, WebVoyager sample harness |
| `tests/current/` | Test suite (963 tests at the time of writing) |
