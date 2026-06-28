# SPDX-License-Identifier: Apache-2.0
"""Framework adapters — expose the browser agent inside other orchestrators.

M1 ships the PydanticAI adapter (`agenticbrowser.adapters.pydantic_ai`). LangChain /
CrewAI / LlamaIndex / Temporal adapters are thin wrappers over the same
`BrowserAgent` and land on demand (see PACKAGING_PLAN.md §4 1b)."""
