# Intents Architecture

## Overview
The intent system is responsible for parsing raw text into a structured `Intent` schema. It utilizes an LLM for classification and includes robust validation and normalization logic to ensure consistent downstream consumption.

## Schema
An intent is defined by three core components:
- **action** (`str`): The operation to perform (e.g., "create", "delete", "update").
- **target** (`str`): The entity the action is applied to (e.g., "user", "file", "record").
- **modifiers** (`list[str]`): A list of additional constraints, parameters, or options for the action.

## Validation & Normalization
All intents are validated and normalized immediately upon parsing:
- If the `modifiers` field is missing from the LLM output or parsed JSON, it defaults to an empty list `[]`.
- This guarantees that downstream consumers can always safely iterate over `modifiers` without performing null/`None` checks.

## LLM Classification
Intent classification is handled entirely within `reasoning/intent.py`. It relies on an injected `llm_call(text: str) -> str` callable.

- **Dependency Injection**: The `llm_call` function is injected into the classification pipeline. This decouples the intent logic from any specific API provider (e.g., OpenAI, Anthropic) and simplifies testing by allowing a simple mock callable to be passed in.
- **Prompts**: The module defines the specific system and user prompts used to instruct the LLM to output a strict JSON object matching the `Intent` schema.

## File Structure

- **`reasoning/intent.py`**: 
  - Defines the `Intent` schema (action, target, modifiers).
  - Contains the LLM classification prompts.
  - Implements the validation and normalization logic (handling the missing `modifiers` default).
  - Exposes the main classification function that accepts the raw text and the injected `llm_call` callable.

- **`api_parallel.py`**: 
  - Contains *only* a callable-returning adapter helper.
  - Acts as a bridge from the underlying parallel API client implementations to the `llm_call(text) -> str` signature required by the intent module.
  - **Strictly no intent logic lives in this file.**

- **`tests/test_intent.py`**: 
  - Unit tests for the intent system.
  - Tests schema validation and normalization (specifically verifying that missing modifiers default to `[]`).
  - Tests the classification pipeline by injecting mock `llm_call` callables that return predefined JSON strings.
