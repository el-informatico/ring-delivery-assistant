"""ring-delivery-assistant: an intent and routing layer for doorbell event streams.

The core thesis: doorbell platforms already tell you WHAT triggered the
camera (ding, motion, a person, a vehicle). What they do not give you is
INTENT (a package was deposited; the deposited package was picked up),
CROSS-EVENT STATE (deposited -> picked up, surviving out-of-order
delivery), and ROUTING (who gets told, through which channel, and what
is deliberately suppressed as noise).

This package is that layer, built offline-first:
- ``ring_assistant.schema``   Ring-style webhook payloads -> typed events
- ``ring_assistant.verify``   HMAC-SHA256 webhook signature verification
- ``ring_assistant.ingest``   webhook-shaped entry point (framework-free)
- ``ring_assistant.synth``    deterministic synthetic event generator
- ``ring_assistant.classify`` pluggable intent classifiers (rules stub)
- ``ring_assistant.llm``      multimodal LLM adapter (endpoint from env)
- ``ring_assistant.state``    cross-event deposited->picked_up machine
- ``ring_assistant.routing``  intent-conditioned notification routing
- ``ring_assistant.replay``   end-to-end pipeline + timeline rendering
- ``ring_assistant.demo``     the offline demo CLI (``uv run demo``)
- ``ring_assistant.server``   optional FastAPI webhook adapter (extra)
"""

__version__ = "0.1.0"
