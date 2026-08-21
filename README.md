# ADVI

**Autonomous Desktop for Visually Impaired**

A clean rebuild of the ADVI desktop-agent project.

The old project is treated as source material and reference material, not as the new architecture.

## Current Foundation

The foundation currently provides:

- deterministic startup and shutdown
- one Python package / one import path
- explicit environment configuration
- structured logging
- clean separation between runtime, I/O, providers, memory, personality, brain, and tools
- optional local Piper TTS support
- testable startup lifecycle

## Project Structure

```text
src/advi/
├── brain/          # Reasoning and orchestration
├── core/           # Runtime, configuration, logging
├── io/             # Console and voice I/O
├── memory/         # Future memory systems
├── personality/    # Future identity and personality
├── providers/      # Future LLM/model providers
└── tools/          # Future executable tools