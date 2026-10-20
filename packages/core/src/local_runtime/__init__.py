"""Local inference runtimes (llama.cpp server, Ollama) behind one small client. Experimental, not wired.

Not a Model Router provider: nothing routes here until a provider is added deliberately,
priced and quality-graded. Both runtimes speak the OpenAI chat-completions dialect on a
loopback port; the client refuses any other host, so it can never become a way out to
the network.
"""
