# Enterprise Deep Research Agent (G3)

An enterprise-grade Retrieval-Augmented Generation (RAG) research agent built under strict memory, cost, and execution constraints. This workflow is designed to execute zero-cost, high-fidelity market research without exceeding strict token budgets or relying on expensive third-party search APIs.

## 🏢 Business Impact & Real-World Value
For private equity, consulting, or enterprise sales clients, manual due diligence and market research are highly labor-intensive. 
This solution provides a **scalable, zero-API-cost research pipeline**. By leveraging a custom Python data-gathering tool that intelligently compresses search results *before* they hit the LLM context window, clients can execute thousands of automated, high-quality research tasks (e.g., supply chain monitoring, competitor analysis) with near-zero token bloat and no recurring Search API subscription fees.

## 🏗️ Architecture Diagram

```mermaid
graph TD
    A[User Query] --> B[Planner LLM: Gemini 3 Flash]
    B -->|Generates Strict Search String| C(Python Code Node: Data Gatherer)
    C -->|Bypasses SSL | D[Google News RSS]
    C -->|Fallback Scrape| E[DuckDuckGo HTML]
    D --> F{Summarization & Compression Cascade}
    E --> F
    F -->|Passes strictly < 450 tokens| G[Synthesizer LLM: Gemini 3 Flash]
    G -->|Formats Bulleted Report| H[Final Answer Output]
