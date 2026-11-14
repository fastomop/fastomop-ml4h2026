"""clean_fastomop — minimal agno NL->SQL pipeline for OMOP CDM.

Two agents (semantic, database), each configurable by model + provider, with
prompts composed from markdown skills, talking to two MCP servers:

  - concept-omcp : semantic OMOP-vocabulary search (semantic agent)
  - omcp         : SQL execution against the clinical DB (database agent)
"""

__version__ = "0.1.0"
