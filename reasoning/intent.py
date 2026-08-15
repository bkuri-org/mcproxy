import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Any


@dataclass
class Intent:
    """Schema representing a parsed user intent."""
    action: str
    target: str
    modifiers: List[str] = field(default_factory=list)


INTENT_CLASSIFICATION_PROMPT = """You are an expert intent classifier. Analyze the provided text and extract the user's intent.
Return a JSON object with exactly three keys:
- "action": The primary action being performed (e.g., "search", "delete", "create", "update").
- "target": The entity or object the action is applied to (e.g., "files", "user account", "database record").
- "modifiers": A list of specific constraints, filters, or contextual details (e.g., ["older than 30 days", "in the downloads folder"]). 

If there are no specific modifiers or constraints mentioned, return an empty list [] for "modifiers".

Text: "{text}"

JSON Response:"""


def normalize_intent(data: Dict[str, Any]) -> Intent:
    """Validates and normalizes a dictionary into an Intent schema.
    
    Args:
        data: Dictionary containing 'action', 'target', and optionally 'modifiers'.
        
    Returns:
        A validated Intent object.
        
    Raises:
        ValueError: If required fields are missing or types are invalid.
    """
    if not isinstance(data, dict):
        raise ValueError(f"Expected a dictionary for intent data, got {type(data).__name__}")
        
    action = data.get("action")
    target = data.get("target")
    modifiers_raw = data.get("modifiers")
    
    if action is None or not str(action).strip():
        raise ValueError("Missing required field: 'action'")
    if target is None or not str(target).strip():
        raise ValueError("Missing required field: 'target'")
        
    action = str(action).strip()
    target = str(target).strip()
    
    # Default missing modifiers to an empty list
    if modifiers_raw is None:
        modifiers = []
    elif isinstance(modifiers_raw, list):
        modifiers = [str(m).strip() for m in modifiers_raw if str(m).strip()]
    elif isinstance(modifiers_raw, str):
        # Handle cases where the LLM might return a single string modifier instead of a list
        modifiers = [modifiers_raw.strip()] if modifiers_raw.strip() else []
    else:
        modifiers = []
        
    return Intent(action=action, target=target, modifiers=modifiers)


def classify_intent(text: str, llm_call: Callable[[str], str]) -> Intent:
    """Classifies user text into an Intent using an injected LLM callable.
    
    Args:
        text: The raw user input text.
        llm_call: A callable that takes a prompt string and returns the LLM's string response.
        
    Returns:
        A normalized Intent object.
        
    Raises:
        ValueError: If the LLM fails to return valid JSON or required fields are missing.
    """
    prompt = INTENT_CLASSIFICATION_PROMPT.format(text=text)
    raw_response = llm_call(prompt)
    
    # Attempt to extract JSON from the response, handling markdown code blocks
    json_str = None
    json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw_response, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Fallback to finding the first { ... } block
        brace_match = re.search(r"\{.*?\}", raw_response, re.DOTALL)
        if brace_match:
            json_str = brace_match.group(0)
            
    if not json_str:
        raise ValueError(f"Could not extract JSON from LLM response: {raw_response}")
        
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse JSON from LLM response: {e}\nResponse: {raw_response}")
        
    return normalize_intent(data)
