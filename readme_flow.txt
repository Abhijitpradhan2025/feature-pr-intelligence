PR Intelligence Dashboard - flow
================================

Two flows exist: the agents' development sequence and the dashboard's runtime
data flow. Agents help build and verify the product; they do not run in the
background every time a user opens the dashboard. The running website now has
explicit runtime specialist components in `python_script/orchestrator.py`,
separate from the Copilot development-agent definitions.

Agent development sequence
--------------------------

1. PR Intelligence Orchestrator
   Inspects the request, current files, shared contract, dependencies, and
   acceptance criteria. It assigns work and coordinates handoffs.

2. GitHub Data and Backend Agent
   Implements the read-only GitHub API collection, accessible repository
   discovery, pagination, rate-limit handling, normalized records, and
   explicit per-resource/per-PR completeness. It does not decide metric
   formulas or build the UI.

3. PR Analytics and Review Intelligence Agent
   Defines and tests evidence-based PR lifecycle metrics and review-feedback
   analysis after the collection contract is stable. It must distinguish
   actual comments from inferred categories and report incomplete evidence.

4. Code Quality and Test Reliability Agent
   Defines explainable hygiene and CI signals when repository check/test
   evidence is available. A single failure is not enough to label a test
   flaky.

5. Dashboard UI and User Experience Agent
   Builds the Streamlit pages and filters against the agreed live-data and
   analytics contracts. It distinguishes zero from unavailable/partial data
   and links findings to source evidence.

6. PR Intelligence Orchestrator
   Integrates the work, checks ownership and contracts, runs focused tests,
   reviews security-sensitive behavior, and reports verified results and
   remaining gaps.

Dependency order:
   Orchestrator -> Backend contract/collection -> Analytics and Quality
   definitions and tested evidence -> UI -> Orchestrator integration/acceptance

Analytics, quality, and UI work must not invent fields or claim support before
the required evidence and contracts exist. Agents have separate ownership and
do not all edit shared files at once.

Mermaid flow diagram
--------------------

```mermaid
flowchart TD
    subgraph DEV["Development workflow - agents are invoked for engineering work"]
        O1["PR Intelligence Orchestrator: inspect and plan"]
        B["GitHub Data and Backend Agent: collection and data contract"]
        A["PR Analytics and Review Intelligence Agent: formulas and feedback signals"]
        Q["Code Quality and Test Reliability Agent: hygiene and CI evidence"]
        U["Dashboard UI and User Experience Agent: Streamlit interface"]
        O2["PR Intelligence Orchestrator: integrate, test, report"]
        O1 --> B
        B --> A
        B --> Q
        A --> U
        Q --> U
        U --> O2
    end

    subgraph RUN["Dashboard runtime - explicit Python specialist stages run"]
        USER["User starts app and selects repositories and date range"]
        APP["Streamlit app reads a password-masked session token"]
        AUTH{"User token entered?"}
        DISC["Optional authenticated repository discovery"]
        API["Read-only GitHub REST API collection"]
        NORMALIZE["Normalize records and retain completeness and errors"]
        METRICS["Runtime analytics specialist: metrics, taxonomy, action plan"]
        QUALITY["Runtime quality specialist: PR hygiene and current-head checks"]
        UIAGENT["Runtime dashboard specialist: section and coverage contract"]
        ANALYSIS["Build all-role comment evidence for selected PRs"]
        LOCAL_READY{"Any text-bearing comment evidence?"}
        OLLAMA["One local Qwen2.5 3B summary call via Ollama on 127.0.0.1"]
        VALIDATE["Validate JSON shape, citations, and source coverage"]
        EMPTY["Show an honest empty or incomplete-coverage state; skip model call"]
        VIEW["Render metrics, action plan, local summary, and linked evidence"]
        BLOCK["Show setup/authentication error; do not collect"]
        USER --> APP --> AUTH
        AUTH -- Yes --> DISC
        DISC --> API
        AUTH -- No --> BLOCK
        API --> NORMALIZE --> METRICS
        METRICS --> QUALITY --> VIEW
        METRICS --> UIAGENT --> VIEW
        NORMALIZE --> ANALYSIS --> LOCAL_READY
        ANALYSIS --> VIEW
        LOCAL_READY -- Yes --> OLLAMA --> VALIDATE --> VIEW
        LOCAL_READY -- No --> EMPTY --> VIEW
    end
```

Dashboard runtime data flow
---------------------------

User starts the local Streamlit app
  -> User enters a read-only token in the password-masked sidebar field;
     it stays in session memory and the app uses the fixed GitHub Enterprise
     API root https://github.azc.ext.hp.com/api/v3
  -> User discovers/selects accessible repositories and chooses a date window
  -> Collector calls the configured GitHub REST API with read-only requests
  -> PR list records are enriched from each PR's detail endpoint for additions,
     deletions, and changed-file counts; detail coverage is tracked separately
  -> Responses are paginated and normalized with repository/PR IDs and source
     links; resource completeness and errors are retained
  -> Deterministic metrics and the reviewer-only Action Plan use their existing,
     documented evidence and coverage rules
  -> After collection, the app builds one all-role summary request from text-
     bearing review summaries, inline review comments, and conversation comments
  -> If evidence exists, one local Qwen2.5 3B model call runs through Ollama on
     127.0.0.1; its output must pass schema and evidence-ID validation
  -> Population completeness and all three comment-source coverage statuses are
     shown independently; complete histories for returned PRs do not imply a
     complete PR listing
  -> Streamlit displays source-level coverage, model themes, original cited
     comments, and the existing repository metrics and linked PR details

The token is not entered into a dashboard field, shown in the UI, or saved to
project files or sent to Ollama. Set it in the same terminal/process that
starts Streamlit. The project does not load a `.env` file. The API endpoint is
fixed in the collector configuration and does not require a URL environment
variable. Comment text is sent only to the local Ollama service; the
adapter is hard-bound to loopback and has no remote fallback. Comments are
passed without truncation; if the complete set exceeds the model context limit,
the app reports the problem instead of summarizing a shortened set. All
commenter roles are included in the model summary. Missing/invalid timestamps
and partial source histories are shown as caveats; they are not treated as
zero. This is an application-code destination boundary, not OS-level network
egress enforcement. If no text-bearing evidence is available, the model is not
called.

Current local mode does not persist or cache collected data outside the active
Streamlit session. Review-thread resolution is not available from the current
REST-only collector. The deterministic PR-by-category matrix and Action Plan
remain rule-based and reviewer-focused; the separate local model summary covers
reviewers, PR-author responses, and unknown identities. Neither output asserts
that feedback was resolved or judges employee performance.

Local summarizer setup
----------------------

Install Ollama and download the small local model once:

```powershell
winget install --id Ollama.Ollama --exact
ollama pull qwen2.5:3b
```

Keep the Ollama service running before selecting “Load PR metrics.” The
dashboard still renders collected metrics if Ollama is unavailable; it shows
an explicit local-summary error and offers a retry. Pulling the model downloads
model files only. PR comments are not included in the model download request.

Current limitations
-------------------

This is an incremental local dashboard, not the entire requested product.
Some advanced lifecycle, delay-attribution, quality, and review-thread metrics
are unavailable until their data sources and definitions are implemented.
The standalone collector uses GITHUB_TOKEN from its process environment.
The Streamlit dashboard instead asks each user for a password-masked token and
keeps it in session memory only.
