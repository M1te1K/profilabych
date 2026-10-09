"""LLM provider boundary: async contract, OpenRouter adapter and offline fake.

Provider implementation owns timeouts, response parsing and API errors.
The model identifier comes from Settings, independently of the agent core.
"""
